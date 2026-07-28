"""Attachment download paths with a fake async httpx client."""

import hashlib
from pathlib import Path
from typing import Any

import httpx as _httpx

from miniteams import attachments as A
from miniteams.config import Settings


class _Resp:
    def __init__(
        self, content: bytes = b"", headers: dict | None = None, data: Any = None, status_code: int = 200
    ) -> None:
        self.content = content
        self.headers = headers or {}
        self._data = data
        self.status_code = status_code  # read by http.aget_with_retry before raise_for_status

    def raise_for_status(self) -> None:
        # `response=self` mirrors httpx: archive._on_fail reads exc.response.status_code.
        if self.status_code >= 400:
            raise _httpx.HTTPStatusError(f"{self.status_code}", request=None, response=self)  # type: ignore[arg-type]

    def json(self) -> Any:
        return self._data

    # streaming-download support: `async with client.stream(...) as resp: async for c in resp.aiter_bytes()`
    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def aiter_bytes(self, size: int = 65536) -> Any:
        for i in range(0, len(self.content), size):
            yield self.content[i : i + size]


class _AsyncClient:
    def __init__(self, handler: Any) -> None:
        self._handler = handler

    async def __aenter__(self) -> _AsyncClient:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def aclose(self) -> None:  # process() now owns/closes the client explicitly
        return None

    async def get(self, url: str, headers: Any = None, cookies: Any = None) -> _Resp:
        return self._handler(url)

    def stream(self, method: str, url: str, headers: Any = None, cookies: Any = None) -> _Resp:
        return self._handler(url)  # _Resp doubles as its own async context manager


def _patch_client(monkeypatch, handler: Any) -> None:
    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _AsyncClient(handler))


IMG = '<img itemtype="http://schema.skype.com/AMSImage" src="https://api.asm.skype.com/v1/objects/o1/views/imgo">'


async def test_process_downloads_optim_and_full(settings: Settings, monkeypatch) -> None:
    seen: list[str] = []

    def handler(url: str) -> _Resp:
        seen.append(url)
        return _Resp(content=b"PNGDATA", headers={"content-type": "image/png"})

    _patch_client(monkeypatch, handler)
    notes = await A.process(IMG, "RichText/Html", "sk", settings.media_dir, download=True)
    # both the optimized (imgo) and the full (imgpsh_fullsize) views are fetched
    assert any(u.endswith("/views/imgo") for u in seen)
    assert any(u.endswith("/views/imgpsh_fullsize") for u in seen)
    assert (settings.media_dir / "o1.png").read_bytes() == b"PNGDATA"  # optimized
    assert (settings.media_dir / "o1.full.png").read_bytes() == b"PNGDATA"  # full
    # annotation shows original URL plus a file:// link
    assert any("image:" in n and "file://" in n and "optim" in n for n in notes)


async def test_process_full_view_404_keeps_optim(settings: Settings, monkeypatch) -> None:
    def handler(url: str) -> _Resp:
        if url.endswith("/views/imgpsh_fullsize"):
            raise RuntimeError("404")
        return _Resp(content=b"PNGDATA", headers={"content-type": "image/png"})

    _patch_client(monkeypatch, handler)
    notes = await A.process(IMG, "RichText/Html", "sk", settings.media_dir, download=True)
    assert (settings.media_dir / "o1.png").read_bytes() == b"PNGDATA"
    assert not (settings.media_dir / "o1.full.png").exists()
    assert any("image:" in n and "optim" not in n for n in notes)


async def test_process_non_downloadable_host_is_ref_only(settings: Settings, monkeypatch) -> None:
    bad = '<img itemtype="http://schema.skype.com/AMSImage" src="https://evil.com/x">'
    notes = await A.process(bad, "RichText/Html", "sk", settings.media_dir, download=True)
    assert notes == ["[image: https://evil.com/x]"]


async def test_process_download_disabled_is_ref_only(settings: Settings, monkeypatch) -> None:
    notes = await A.process(IMG, "RichText/Html", "sk", settings.media_dir, download=False)
    assert notes == ["[image: https://api.asm.skype.com/v1/objects/o1/views/imgo]"]


async def test_process_downloads_file_two_hop(settings: Settings, monkeypatch) -> None:
    status = {
        "content_state": "ready",
        "content_full_length": 5,
        "original_filename": "r.pdf",
        "view_location": "https://api.asm.skype.com/v1/objects/f1/views/original",
    }

    def handler(url: str) -> _Resp:
        return _Resp(data=status) if url.endswith("/status") else _Resp(content=b"PDFXX")

    _patch_client(monkeypatch, handler)
    content = '<URIObject uri="https://api.asm.skype.com/v1/objects/f1">f</URIObject>'
    notes = await A.process(content, "RichText/Media_GenericFile", "sk", settings.media_dir, download=True)
    assert any("r.pdf" in n and "5B" in n for n in notes)
    assert (settings.media_dir / "r.pdf").read_bytes() == b"PDFXX"


async def test_process_file_not_ready_is_ref_only(settings: Settings, monkeypatch) -> None:
    status = {"content_state": "uploading", "original_filename": "x.bin"}
    _patch_client(monkeypatch, lambda url: _Resp(data=status))
    content = '<URIObject uri="https://api.asm.skype.com/v1/objects/f2">f</URIObject>'
    notes = await A.process(content, "RichText/Media_GenericFile", "sk", settings.media_dir, download=True)
    assert any("x.bin" in n and "→" not in n for n in notes)


async def test_stream_to_removes_partial_file_on_failure(settings: Settings) -> None:
    """A download failing mid-body must not leave truncated bytes that a later
    skip-if-exists check would mistake for a complete file."""

    class _BoomResp(_Resp):
        async def aiter_bytes(self, size: int = 65536) -> Any:
            yield b"partial"
            raise RuntimeError("connection reset")

    settings.media_dir.mkdir(parents=True, exist_ok=True)
    dest = settings.media_dir / "victim.bin"
    client = _AsyncClient(lambda url: _BoomResp(content=b"x"))
    try:
        await A._stream_to(client, "https://api.asm.skype.com/v1/objects/z", "sk", dest)  # type: ignore[arg-type]
    except RuntimeError:
        pass
    assert not dest.exists()


async def test_process_relative_media_dir_still_links(settings: Settings, monkeypatch, tmp_path) -> None:
    """Regression: a relative media_dir must not break file:// URI building (Path.as_uri needs
    absolute). The download note must still carry the '→ <local>' link, so archive counts it."""
    monkeypatch.chdir(tmp_path)

    def handler(url: str) -> _Resp:
        return _Resp(content=b"IMG", headers={"content-type": "image/png"})

    _patch_client(monkeypatch, handler)
    notes = await A.process(IMG, "RichText/Html", "sk", Path("rel/media"), download=True)
    assert any("→" in n for n in notes), notes


_OBJ = "https://fr-prod.asyncgw.teams.microsoft.com/v1/objects/rec1/views"
REC = (
    f'<URIObject type="Video.2/CallRecording.1" url_thumbnail="{_OBJ}/thumbnail_small" uri="">'
    '<a href="https://sp.example/play">Play</a>'
    f'<item type="amsVideo" uri="{_OBJ}/video" />'
    f'<item type="amsTranscript" uri="{_OBJ}/transcript" />'
    "</URIObject>"
)


def test_extract_recording_pulls_transcript_not_video() -> None:
    items = A.extract(REC, "RichText/Media_CallRecording")
    kinds = {(i["kind"], i["url"].rsplit("/", 1)[-1]) for i in items}
    assert ("transcript", "transcript") in kinds  # transcript captured
    assert ("file", "thumbnail_small") in kinds  # thumbnail (URIObject url_thumbnail)
    assert not any(i["url"].endswith("/video") for i in items)  # video intentionally skipped


async def test_process_downloads_transcript(settings: Settings, monkeypatch) -> None:
    def handler(url: str) -> _Resp:
        return _Resp(content=b"WEBVTT\n\nhi", headers={"content-type": "text/vtt"})

    _patch_client(monkeypatch, handler)
    notes = await A.process(REC, "RichText/Media_CallRecording", "sk", settings.media_dir, download=True)
    assert (settings.media_dir / "rec1.transcript.vtt").read_bytes() == b"WEBVTT\n\nhi"
    assert any(n.startswith("[transcript:") and "→" in n for n in notes)


SP = "https://contoso-my.sharepoint.com/personal/x/_api/v2.1/drives/b!abc/items/01ABC?foo=bar"
REC_SP = (
    '<URIObject type="Video.2/CallRecording.1" uri="">'
    f'<item type="onedriveForBusinessTranscript" uri="{SP}" /></URIObject>'
)


def test_extract_pulls_sharepoint_transcript() -> None:
    items = A.extract(REC_SP, "RichText/Media_CallRecording")
    assert any(i["kind"] == "sp_transcript" and i["url"] == SP for i in items)


async def test_process_downloads_sp_transcript_with_bearer(settings: Settings, monkeypatch) -> None:
    seen_auth = {}

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, cookies=None, **kw):
            seen_auth["hdr"] = headers
            seen_auth.setdefault("params", []).append(kw.get("params"))
            if kw.get("params", {}).get("format") == "json":
                return _Resp(content=b'{"entries":[]}', headers={"content-type": "application/json"})
            return _Resp(content=b"WEBVTT\n\nsp", headers={"content-type": "text/vtt"})

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    notes = await A.process(
        REC_SP,
        "RichText/Media_CallRecording",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda host: f"SPTOK::{host}",
    )
    assert any(n.startswith("[transcript:") and "→" in n for n in notes)
    assert seen_auth["hdr"]["Authorization"] == "Bearer SPTOK::contoso-my.sharepoint.com"
    # Both renditions: format=json has the speakers, the default VTT the sub-cue timings.
    assert seen_auth["params"] == [{"format": "json"}, {}]
    js = list(settings.media_dir.glob("sp-*.transcript.json"))
    vtt = list(settings.media_dir.glob("sp-*.transcript.vtt"))
    assert len(js) == 1 and js[0].read_bytes() == b'{"entries":[]}'
    assert len(vtt) == 1 and vtt[0].read_bytes() == b"WEBVTT\n\nsp"
    assert not list(settings.media_dir.glob("*.part"))


async def test_process_sp_transcript_mislabelled_body_is_not_written(settings: Settings, monkeypatch) -> None:
    """A server ignoring `format` must not land VTT bytes under .json — the archive would lie."""

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, cookies=None, **kw):
            return _Resp(content=b"WEBVTT\n\nnope", headers={"content-type": "text/vtt"})

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    notes = await A.process(
        REC_SP,
        "RichText/Media_CallRecording",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda host: "T",
    )
    assert not list(settings.media_dir.glob("*.json"))
    assert list(settings.media_dir.glob("sp-*.transcript.vtt"))  # the VTT rendition still lands
    assert any(n.startswith("[transcript:") for n in notes)


async def test_process_sp_transcript_refetches_over_untagged_vtt(settings: Settings, monkeypatch) -> None:
    """An archive holding the old untagged VTT must still pull the tagged JSON — and keep the VTT."""
    stale = (
        settings.media_dir / f"sp-{hashlib.sha1(SP.split('?')[0].encode()).hexdigest()[:16]}.transcript.vtt"
    )
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"WEBVTT\n\nuntagged")
    calls = []

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, cookies=None, **kw):
            calls.append(url)
            return _Resp(
                content=b'{"entries":[{"speakerDisplayName":"A"}]}',
                headers={"content-type": "application/json"},
            )

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    for _ in range(2):  # second pass must be a no-op: skip-if-exists on the JSON
        await A.process(
            REC_SP,
            "RichText/Media_CallRecording",
            "sk",
            settings.media_dir,
            download=True,
            sp_token=lambda host: "T",
        )
    assert len(calls) == 1
    assert stale.read_bytes() == b"WEBVTT\n\nuntagged"  # never deleted — the 404 case keeps its only copy
    assert list(settings.media_dir.glob("sp-*.transcript.json"))


async def test_process_sp_transcript_both_renditions_denied_keeps_status(
    settings: Settings, monkeypatch
) -> None:
    """Both renditions 403 → on_fail must receive the original error, status readable."""

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, cookies=None, **kw):
            return _Resp(status_code=403)

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    seen: list[Exception] = []
    notes = await A.process(
        REC_SP,
        "RichText/Media_CallRecording",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda host: "T",
        on_fail=lambda url, exc: seen.append(exc),
    )
    assert notes == [f"[transcript(sp): {SP}]"]
    assert not list(settings.media_dir.glob("sp-*"))
    # archive._on_fail reads exc.response.status_code to fill denied_assets: wrapping the original
    # exception (or re-raising a bare RuntimeError) silently disables the whole denied cache.
    assert len(seen) == 1
    assert getattr(getattr(seen[0], "response", None), "status_code", None) == 403


async def test_process_sp_transcript_partial_failure_is_reported(settings: Settings, monkeypatch) -> None:
    """One rendition denied while the other serves: swallowing it re-requests a dead URL forever."""

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, cookies=None, **kw):
            if kw.get("params", {}).get("format") == "json":
                return _Resp(status_code=403)
            return _Resp(content=b"WEBVTT\n\nsp", headers={"content-type": "text/vtt"})

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    seen: list[Exception] = []
    notes = await A.process(
        REC_SP,
        "RichText/Media_CallRecording",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda host: "T",
        on_fail=lambda url, exc: seen.append(exc),
    )
    assert list(settings.media_dir.glob("sp-*.transcript.vtt"))  # the surviving rendition lands
    assert not list(settings.media_dir.glob("sp-*.transcript.json"))
    assert any("→" in n for n in notes)  # still counted as fetched — hence the on_fail below
    assert len(seen) == 1
    assert getattr(getattr(seen[0], "response", None), "status_code", None) == 403


async def test_process_sp_transcript_written_owner_only(settings: Settings, monkeypatch) -> None:
    """Named speaker turns for every participant: 0600, not umask-dependent 0644."""

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, cookies=None, **kw):
            if kw.get("params", {}).get("format") == "json":
                return _Resp(content=b'{"entries":[]}', headers={"content-type": "application/json"})
            return _Resp(content=b"WEBVTT\n\nsp", headers={"content-type": "text/vtt"})

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    await A.process(
        REC_SP,
        "RichText/Media_CallRecording",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda h: "T",
    )
    modes = {f.suffix: f.stat().st_mode & 0o777 for f in settings.media_dir.glob("sp-*.transcript.*")}
    assert modes == {".json": 0o600, ".vtt": 0o600}


async def test_process_sp_transcript_write_failure_leaves_no_part(settings: Settings, monkeypatch) -> None:
    """A truncated .part must not survive: a later run would trust it as a complete rendition."""

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, cookies=None, **kw):
            return _Resp(content=b'{"entries":[]}', headers={"content-type": "application/json"})

    def _boom(self, data):
        # ENOSPC mid-write: the bytes already on disk are what the cleanup has to remove.
        with open(self, "wb") as fh:
            fh.write(data[:4])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    monkeypatch.setattr(Path, "write_bytes", _boom)
    notes = await A.process(
        REC_SP,
        "RichText/Media_CallRecording",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda h: "T",
    )
    assert not list(settings.media_dir.glob("*.part"))
    assert notes == [f"[transcript(sp): {SP}]"]


async def test_process_sp_transcript_retries_on_429(settings: Settings, monkeypatch) -> None:
    """The download path must go through aget_with_retry, not a bare client.get."""
    codes = [429, 200]

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, cookies=None, **kw):
            if kw.get("params", {}).get("format") != "json":
                return _Resp(content=b"WEBVTT\n\nsp", headers={"content-type": "text/vtt"})
            code = codes.pop(0)
            if code == 429:
                return _Resp(status_code=429, headers={"Retry-After": "0"})
            return _Resp(content=b'{"entries":[]}', headers={"content-type": "application/json"})

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    await A.process(
        REC_SP,
        "RichText/Media_CallRecording",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda h: "T",
    )
    assert not codes  # both responses consumed: the 429 was retried, not surfaced as a failure
    assert list(settings.media_dir.glob("sp-*.transcript.json"))


async def test_process_sp_transcript_ref_only_without_token(settings: Settings) -> None:
    notes = await A.process(REC_SP, "RichText/Media_CallRecording", "sk", settings.media_dir, download=True)
    assert notes == [f"[transcript(sp): {SP}]"]  # no sp_token → ref only, no crash


FILE_A = (
    '<a itemtype="http://schema.skype.com/HyperLink/Files" '
    'href="https://c.sharepoint.com/:x:/r/sites/RD/Shared/z.xlsx?d=w1&web=1">z.xlsx</a>'
)
VIDEO_A = '<a href="https://c.sharepoint.com/:v:/r/personal/x/rec.mp4?web=1">rec</a>'
FOLDER_A = '<a href="https://c.sharepoint.com/:f:/r/sites/RD/Shared/Infras?web=1">folder</a>'


def test_extract_sp_files_docs_only_no_video_no_folder() -> None:
    items = A.extract(FILE_A + VIDEO_A + FOLDER_A, "RichText/Html")
    sp = [i["url"] for i in items if i["kind"] == "sp_file"]
    assert sp == ["https://c.sharepoint.com/:x:/r/sites/RD/Shared/z.xlsx?d=w1&web=1"]  # doc yes, :v:/:f: no


async def test_process_downloads_sp_file_via_graph(settings: Settings, monkeypatch) -> None:
    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, **kw):
            if url.endswith("/driveItem"):
                return _Resp(data={"name": "z.xlsx", "file": {"mimeType": "x"}})
            return _Resp(content=b"XLSXBYTES")  # /content

        def json_get(self): ...

    # _Resp.json returns _data; content path returns bytes
    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    notes = await A.process(
        FILE_A, "RichText/Html", "sk", settings.media_dir, download=True, graph_token=lambda: "GTOK"
    )
    assert (settings.media_dir / "z.xlsx").read_bytes() == b"XLSXBYTES"
    assert any(n.startswith("[file:") and "→" in n for n in notes)


async def test_process_sp_file_write_failure_leaves_no_part(settings: Settings, monkeypatch) -> None:
    """Same truncation hazard as the transcript path: a surviving .part reads as a complete file."""

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, **kw):
            if url.endswith("/driveItem"):
                return _Resp(data={"name": "z.xlsx", "file": {"mimeType": "x"}})
            return _Resp(content=b"XLSXBYTES")

    def _boom(self, data):
        with open(self, "wb") as fh:
            fh.write(data[:4])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    monkeypatch.setattr(Path, "write_bytes", _boom)
    notes = await A.process(
        FILE_A, "RichText/Html", "sk", settings.media_dir, download=True, graph_token=lambda: "GTOK"
    )
    assert not list(settings.media_dir.glob("*.part"))
    assert not (settings.media_dir / "z.xlsx").exists()
    assert any(n.startswith("[file(sp):") for n in notes)  # ref-only, download not claimed


async def test_process_sp_file_folder_is_ref_only(settings: Settings, monkeypatch) -> None:
    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, **kw):
            return _Resp(data={"name": "Infras", "folder": {"childCount": 3}})  # no 'file' facet

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    notes = await A.process(
        FOLDER_A.replace(":f:", ":x:"),
        "RichText/Html",
        "sk",
        settings.media_dir,
        download=True,
        graph_token=lambda: "GTOK",
    )
    assert any(n.startswith("[file(sp):") for n in notes)  # folder → not downloaded


def test_extract_excludes_sharepoint_videos() -> None:
    mp4_files_itemtype = (
        '<a itemtype="http://schema.skype.com/HyperLink/Files" '
        'href="https://c.sharepoint.com/:v:/r/personal/x/clip.mp4?web=1">clip</a>'
    )
    mp4_by_ext = '<a itemtype="http://schema.skype.com/HyperLink/Files" href="https://c.sharepoint.com/sites/x/clip.mp4">c</a>'
    items = A.extract(mp4_files_itemtype + mp4_by_ext, "RichText/Html")
    assert not any(i["kind"] == "sp_file" for i in items)  # videos excluded even as HyperLink/Files


def _403(url: str) -> _httpx.HTTPStatusError:
    req = _httpx.Request("GET", url)
    return _httpx.HTTPStatusError("403", request=req, response=_httpx.Response(403, request=req))


async def test_process_skip_url_prevents_network(settings: Settings, monkeypatch) -> None:
    def handler(url: str) -> _Resp:
        raise AssertionError("network must not be touched for a skipped url")

    _patch_client(monkeypatch, handler)
    notes = await A.process(
        IMG, "RichText/Html", "sk", settings.media_dir, download=True, skip_url=lambda u: True
    )
    assert notes == ["[image: https://api.asm.skype.com/v1/objects/o1/views/imgo]"]  # ref-only


async def test_process_on_fail_sees_403(settings: Settings, monkeypatch) -> None:
    def handler(url: str) -> _Resp:
        raise _403(url)

    _patch_client(monkeypatch, handler)
    failures: list[tuple[str, int]] = []
    await A.process(
        IMG,
        "RichText/Html",
        "sk",
        settings.media_dir,
        download=True,
        on_fail=lambda url, exc: failures.append((url, exc.response.status_code)),  # type: ignore[attr-defined]
    )
    assert failures == [("https://api.asm.skype.com/v1/objects/o1/views/imgo", 403)]


async def test_full_view_403_recorded_and_skippable(settings: Settings, monkeypatch) -> None:
    """Optimized view OK but full view 403: the full URL must reach on_fail (so the archive can
    deny-cache it), and a skip_url veto on it must prevent the fetch while keeping the optim."""
    full_url = "https://api.asm.skype.com/v1/objects/o1/views/imgpsh_fullsize"

    def handler(url: str) -> _Resp:
        if url == full_url:
            raise _403(url)
        return _Resp(content=b"PNGDATA", headers={"content-type": "image/png"})

    _patch_client(monkeypatch, handler)
    failures: list[str] = []
    await A.process(
        IMG,
        "RichText/Html",
        "sk",
        settings.media_dir,
        download=True,
        on_fail=lambda url, exc: failures.append(url),
    )
    assert failures == [full_url]  # recorded under the full-view URL
    assert (settings.media_dir / "o1.png").exists()

    def no_full(url: str) -> _Resp:
        assert url != full_url, "denied full view must not be fetched"
        return _Resp(content=b"PNGDATA", headers={"content-type": "image/png"})

    _patch_client(monkeypatch, no_full)
    notes = await A.process(
        IMG,
        "RichText/Html",
        "sk",
        settings.media_dir,
        download=True,
        skip_url=lambda url: url == full_url,
    )
    assert any("image:" in n and "optim" not in n for n in notes)  # optim kept, full skipped
