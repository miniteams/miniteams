"""Attachment download paths with a fake async httpx client."""

import base64
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx as _httpx
import pytest

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

    def stream(self, method: str, url: str, headers: Any = None, cookies: Any = None, **kw: Any) -> _Resp:
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


class _AwaitedResp:
    """`async with client.stream(...)` over a fake whose `get` is a coroutine."""

    def __init__(self, pending: Any) -> None:
        self._pending = pending

    async def __aenter__(self) -> _Resp:
        return await self._pending

    async def __aexit__(self, *_: Any) -> bool:
        return False


class _StreamFromGet:
    """SharePoint transcripts are streamed: serve `stream()` from the fake's `get()`."""

    def stream(self, method: str, url: str, **kw: Any) -> _AwaitedResp:
        return _AwaitedResp(self.get(url, **kw))  # type: ignore[attr-defined]


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

    class _C(_StreamFromGet):
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

    class _C(_StreamFromGet):
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

    class _C(_StreamFromGet):
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

    class _C(_StreamFromGet):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, cookies=None, **kw):
            return _Resp(status_code=403)

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    seen: list[tuple[str, Exception]] = []
    notes = await A.process(
        REC_SP,
        "RichText/Media_CallRecording",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda host: "T",
        on_fail=lambda key, exc: seen.append((key, exc)),
    )
    assert notes == [f"[transcript(sp): {SP}]"]
    assert not list(settings.media_dir.glob("sp-*"))
    # Both renditions reported under their OWN key — a total failure recorded against the bare URL
    # would never be consulted again, since the skip check is per rendition.
    assert [k for k, _ in seen] == [f"{SP}#.json", f"{SP}#.vtt"]
    # archive._on_fail reads exc.response.status_code to fill denied_assets: wrapping the original
    # exception (or re-raising a bare RuntimeError) silently disables the whole denied cache.
    assert all(getattr(getattr(e, "response", None), "status_code", None) == 403 for _, e in seen)


async def test_process_sp_transcript_partial_failure_is_reported(settings: Settings, monkeypatch) -> None:
    """One rendition denied while the other serves: swallowing it re-requests a dead URL forever."""

    class _C(_StreamFromGet):
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

    class _C(_StreamFromGet):
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

    class _C(_StreamFromGet):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, cookies=None, **kw):
            ctype = "application/json" if (kw.get("params") or {}).get("format") else "text/vtt"
            return _BoomResp(content=b'{"entries":[]}', headers={"content-type": ctype})

    class _BoomResp(_Resp):
        async def aiter_bytes(self, size: int = 65536):
            # ENOSPC mid-write: the bytes already on disk are what the cleanup has to remove.
            yield self.content[:4]
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
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


class _NoBodyResp(_Resp):
    async def aiter_bytes(self, size: int = 65536):
        raise AssertionError("body read before the content-type check")
        yield b""


async def test_sp_transcript_wrong_content_type_reads_no_body(settings: Settings) -> None:
    class _C(_StreamFromGet):
        async def get(self, url, headers=None, **kw):
            return _NoBodyResp(content=b"\0" * 1024, headers={"content-type": "video/mp4"})

    with pytest.raises(A._Refused, match="content-type"):
        await A._download_sp_transcript(_C(), SP, "T", settings.media_dir)
    assert not list(settings.media_dir.iterdir())


async def test_sp_transcript_over_the_size_cap_stops_reading(settings: Settings, monkeypatch) -> None:
    """A transcript URI pointing at a huge file: the read stops at the cap, nothing is kept."""
    monkeypatch.setattr(A, "_SP_TRANSCRIPT_MAX", 100)
    read: list[int] = []

    class _Big(_Resp):
        async def aiter_bytes(self, size: int = 65536):
            for _ in range(1000):  # 40 kB in 40-byte chunks
                read.append(40)
                yield b"x" * 40

    class _C(_StreamFromGet):
        async def get(self, url, headers=None, **kw):
            ctype = "application/json" if (kw.get("params") or {}).get("format") else "text/vtt"
            return _Big(headers={"content-type": ctype})

    with pytest.raises(A._Refused, match="size cap"):
        await A._download_sp_transcript(_C(), SP, "T", settings.media_dir)
    assert sum(read) <= 2 * 120  # each rendition stopped at its third chunk
    assert not list(settings.media_dir.iterdir())


async def test_sp_transcript_content_length_over_the_cap_reads_no_body(
    settings: Settings, monkeypatch
) -> None:
    monkeypatch.setattr(A, "_SP_TRANSCRIPT_MAX", 100)

    class _C(_StreamFromGet):
        async def get(self, url, headers=None, **kw):
            ctype = "application/json" if (kw.get("params") or {}).get("format") else "text/vtt"
            return _NoBodyResp(headers={"content-type": ctype, "content-length": "101"})

    failed: list[Exception] = []
    with pytest.raises(A._Refused, match="size cap"):
        await A._download_sp_transcript(
            _C(), SP, "T", settings.media_dir, on_fail=lambda u, e: failed.append(e)
        )
    assert [getattr(e, "status", None) for e in failed] == [413, 413]
    assert not list(settings.media_dir.iterdir())


@pytest.mark.parametrize("encoding", ["gzip", "br"])
async def test_sp_transcript_compressed_anyway_reads_no_body(settings: Settings, encoding: str) -> None:
    """The server ignored Accept-Encoding: httpx would inflate before the cap counts."""

    class _C(_StreamFromGet):
        async def get(self, url, headers=None, **kw):
            ctype = "application/json" if (kw.get("params") or {}).get("format") else "text/vtt"
            return _NoBodyResp(headers={"content-type": ctype, "content-encoding": encoding})

    failed: list[Exception] = []
    with pytest.raises(A._Refused, match="content-encoding"):
        await A._download_sp_transcript(
            _C(), SP, "T", settings.media_dir, on_fail=lambda u, e: failed.append(e)
        )
    assert [getattr(e, "status", None) for e in failed] == [415, 415]
    assert not list(settings.media_dir.iterdir())


async def test_sp_transcript_asks_for_an_uncompressed_body(settings: Settings) -> None:
    """The cap counts wire bytes only if nothing is inflated behind it."""
    sent: list[dict[str, str]] = []

    class _C(_StreamFromGet):
        async def get(self, url, headers=None, **kw):
            sent.append(headers)
            ctype = "application/json" if (kw.get("params") or {}).get("format") else "text/vtt"
            return _Resp(content=b"{}", headers={"content-type": ctype})

    await A._download_sp_transcript(_C(), SP, "T", settings.media_dir)
    assert len(sent) == 2 and all(h["Accept-Encoding"] == "identity" for h in sent)


async def test_sp_transcript_kept_under_the_escaped_uri_hash_is_not_refetched(settings: Settings) -> None:
    """Older archives hashed the URI as written in the HTML (`&amp;`)."""
    url = "https://contoso-my.sharepoint.com/personal/x/a&b/content?x=1"
    legacy = (
        "sp-" + hashlib.sha1(b"https://contoso-my.sharepoint.com/personal/x/a&amp;b/content").hexdigest()[:16]
    )
    settings.media_dir.mkdir(parents=True)
    for ext in (".json", ".vtt"):
        (settings.media_dir / f"{legacy}.transcript{ext}").write_bytes(b"kept")

    class _C(_StreamFromGet):
        async def get(self, url, headers=None, **kw):
            raise AssertionError("an archived rendition must not be fetched again")

    path = await A._download_sp_transcript(_C(), url, "T", settings.media_dir)
    assert Path(path).name == f"{legacy}.transcript.json"


async def test_process_sp_transcript_retries_on_429(settings: Settings, monkeypatch) -> None:
    """The download path must go through aget_with_retry, not a bare client.get."""
    codes = [429, 200]

    class _C(_StreamFromGet):
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


async def test_process_sp_transcript_skip_is_per_rendition(settings: Settings, monkeypatch) -> None:
    """A suppressed rendition must cost no request, while the other still downloads."""
    asked: list[dict] = []

    class _C(_StreamFromGet):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, cookies=None, **kw):
            asked.append(kw.get("params"))
            return _Resp(content=b"WEBVTT\n\nsp", headers={"content-type": "text/vtt"})

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    await A.process(
        REC_SP,
        "RichText/Media_CallRecording",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda host: "T",
        skip_url=lambda key: key == f"{SP}#.json",  # the JSON is backing off
    )
    assert asked == [{}]  # only the VTT rendition was requested
    assert list(settings.media_dir.glob("sp-*.transcript.vtt"))
    assert not list(settings.media_dir.glob("sp-*.transcript.json"))


async def test_process_sp_transcript_all_suppressed_is_not_a_failure(settings: Settings, monkeypatch) -> None:
    """Steady state of a dead asset: no network, no failure log — 126 of them run this every pass."""
    logged: list[str] = []

    class _Log:  # replacing the module attribute: setattr on structlog's proxy does not bind
        def __getattr__(self, level):
            return lambda event, **kw: logged.append(event)

    monkeypatch.setattr(A, "log", _Log())

    class _C(_StreamFromGet):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, cookies=None, **kw):
            raise AssertionError("a suppressed rendition must cost no request")

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    notes = await A.process(
        REC_SP,
        "RichText/Media_CallRecording",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda host: "T",
        # Only the per-rendition keys back off; the bare URL must NOT match, or process()'s early
        # skip short-circuits the item and this never reaches the code under test.
        skip_url=lambda key: key.startswith(f"{SP}#"),
    )
    assert notes == [f"[transcript(sp): {SP}]"]
    assert "sp_transcript_failed" not in logged  # nominal skip must not read as a failure


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


async def test_process_downloads_sp_file_via_sharepoint_rest(settings: Settings, monkeypatch) -> None:
    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, **kw):
            return _Resp(data={"Name": "z.xlsx"})  # GetFileByUrl

        def stream(self, method, url, headers=None, **kw):
            return _Resp(content=b"XLSXBYTES")  # /content — streamed to disk

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    notes = await A.process(
        FILE_A, "RichText/Html", "sk", settings.media_dir, download=True, sp_token=lambda host: "SPTOK"
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
            return _Resp(data={"Name": "z.xlsx"})  # GetFileByUrl

        def stream(self, method, url, headers=None, **kw):
            return _BoomResp(content=b"XLSXBYTES")  # /content — dies mid-stream

    class _BoomResp(_Resp):
        async def aiter_bytes(self, size: int = 65536):
            yield self.content[:4]  # partial bytes hit the .part file, then the stream dies
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    notes = await A.process(
        FILE_A, "RichText/Html", "sk", settings.media_dir, download=True, sp_token=lambda host: "SPTOK"
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
            if "GetFileByUrl" in url:
                return _Resp(status_code=404)  # REST resolves files only
            return _Resp(data={"name": "Infras", "folder": {"childCount": 3}})  # no 'file' facet

        def stream(self, method, url, headers=None, **kw):
            streams.append(url)
            return _Resp(content=b"ZIP")

    streams: list[str] = []
    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    notes = await A.process(
        FOLDER_A.replace(":f:", ":x:"),
        "RichText/Html",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda host: "SPTOK",
    )
    assert any(n.startswith("[file(sp):") for n in notes)  # folder → not downloaded
    assert streams == []


class _SpClient:
    """Records every SharePoint call; `meta(url)` answers the metadata GETs."""

    def __init__(self, meta: Any) -> None:
        self.meta, self.gets, self.streams, self.auth = meta, [], [], set()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aclose(self):
        return None

    async def get(self, url, headers=None, **kw):
        self.gets.append(url)
        self.auth.add((headers or {}).get("Authorization"))
        return self.meta(url)

    def stream(self, method, url, headers=None, **kw):
        self.streams.append(url)
        self.auth.add((headers or {}).get("Authorization"))
        return _Resp(content=b"BYTES")


def _rest(site: str, url: str) -> str:
    return f"{site}/_api/web/GetFileByUrl(@u)?@u=" + quote("'" + url.replace("'", "''") + "'", safe="")


def _shares(host: str, url: str) -> str:
    return (
        f"{host}/_api/v2.0/shares/u!{base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')}/driveItem"
    )


@pytest.mark.parametrize(
    ("href", "site"),
    [
        # HTML-escaped query, as stored: the REST call carries the real URL
        (
            "https://c.sharepoint.com/:x:/r/sites/RD/Shared/z.xlsx?d=w1&amp;web=1",
            "https://c.sharepoint.com/sites/RD",
        ),
        (
            "https://c-my.sharepoint.com/:x:/g/personal/u_c_com/EAbc?e=x1",
            "https://c-my.sharepoint.com/personal/u_c_com",
        ),
        ("https://c.sharepoint.com/:p:/s/Sales/EAbc?e=x1", "https://c.sharepoint.com/sites/Sales"),
        (
            "https://c.sharepoint.com/teams/Ops/Shared%20Documents/it's.docx",
            "https://c.sharepoint.com/teams/Ops",
        ),
        ("https://c.sharepoint.com/:x:/g/EAbc", "https://c.sharepoint.com"),  # root site
        ("https://c-my.sharepoint.com/:x:/p/u_c_com/EAbc", "https://c-my.sharepoint.com/personal/u_c_com"),
    ],
)
async def test_sp_file_uses_sharepoint_rest_on_the_files_site(
    settings: Settings, monkeypatch, href: str, site: str
) -> None:
    client = _SpClient(lambda url: _Resp(data={"Name": "z.xlsx"}))
    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: client)
    hosts: list[str] = []
    notes = await A.process(
        f'<a itemtype="http://schema.skype.com/HyperLink/Files" href="{href}">z</a>',
        "RichText/Html",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda host: hosts.append(host) or f"SP-{host}",
    )
    url = href.replace("&amp;", "&")
    host = url.split("/")[2]
    assert hosts == [host]  # the bearer is the file host's own
    assert client.gets == [_rest(site, url)]  # resolved by REST: no shares fallback
    assert client.streams == [_rest(site, url).replace("(@u)?", "(@u)/$value?")]
    assert client.auth == {f"Bearer SP-{host}"}
    assert (settings.media_dir / "z.xlsx").read_bytes() == b"BYTES"
    assert any(n.startswith("[file:") and "→" in n for n in notes)


async def test_sp_file_share_link_falls_back_to_sharepoint_shares_on_the_same_host(
    settings: Settings, monkeypatch
) -> None:
    url = "https://c-my.sharepoint.com/:x:/g/personal/u_c_com/EAbc?e=x1"
    client = _SpClient(
        lambda u: (
            _Resp(status_code=404) if "GetFileByUrl" in u else _Resp(data={"name": "s.xlsx", "file": {}})
        )
    )
    path = await A._download_sp_file(client, url, "SPTOK", settings.media_dir)
    item = _shares("https://c-my.sharepoint.com", url)
    assert client.gets == [_rest("https://c-my.sharepoint.com/personal/u_c_com", url), item]
    assert client.streams == [f"{item}/content"]
    assert path and Path(path).name == "s.xlsx"


@pytest.mark.parametrize(("rest", "shares", "recorded"), [(404, 403, 403), (403, 404, 404), (500, 403, 403)])
async def test_sp_file_failure_reports_the_fallback_status(
    settings: Settings, monkeypatch, rest: int, shares: int, recorded: int
) -> None:
    """The denied-asset cache sees the last answer: 403 permanent, 404 backed off, as under Graph."""
    client = _SpClient(lambda u: _Resp(status_code=rest if "GetFileByUrl" in u else shares))
    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: client)
    failed: list[tuple[str, int]] = []
    notes = await A.process(
        FILE_A,
        "RichText/Html",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda host: "SPTOK",
        on_fail=lambda u, exc: failed.append((u, exc.response.status_code)),  # type: ignore[attr-defined]
    )
    assert failed == [("https://c.sharepoint.com/:x:/r/sites/RD/Shared/z.xlsx?d=w1&web=1", recorded)]
    assert client.streams == []
    assert any(n.startswith("[file(sp):") for n in notes)


async def test_sp_file_ref_only_without_sharepoint_token(settings: Settings, monkeypatch) -> None:
    client = _SpClient(lambda u: _Resp(data={"Name": "z.xlsx"}))
    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: client)
    notes = await A.process(FILE_A, "RichText/Html", "sk", settings.media_dir, download=True)
    assert client.gets == [] and any(n.startswith("[file(sp):") for n in notes)


# Message HTML is written by any chat member: a SharePoint bearer goes to a tenant host or nowhere.
_HOSTILE = [
    "https://evil.example&sol;@contoso.sharepoint.com/:w:/r/x.docx",  # entity hides the userinfo
    "https://evil.example/?q=sharepoint.com/:x:/r/z",  # "sharepoint.com" only in the query
    "https://user@contoso.sharepoint.com/:x:/r/sites/a/z.xlsx",  # userinfo
    "https://contoso.sharepoint.com:8443/:x:/r/sites/a/z.xlsx",  # port
    "https://c\u00f6ntoso.sharepoint.com/:x:/r/sites/a/z.xlsx",  # IDN
    "https://CONTOSO.sharepoint.com/:x:/r/sites/a/z.xlsx",  # uppercase
    "https://contoso.sharepoint.com./:x:/r/sites/a/z.xlsx",  # trailing dot
    "https://evil.example\\@contoso.sharepoint.com/:x:/r/sites/a/z.xlsx",  # backslash
    "http://contoso.sharepoint.com/:x:/r/sites/a/z.xlsx",  # plain http
    "https://contoso.sharepoint.com.evil.example/:x:/r/sites/a/z.xlsx",  # suffix
    "https://contoso.sharepoint.com/:x:/r/sites/a/z&#9;.xlsx",  # tab: urlsplit drops it, httpx refuses
    "https://contoso.sharepoint.com&#10;/:x:/r/sites/a/z.xlsx",  # newline after the host
    " https://contoso.sharepoint.com/:x:/r/sites/a/z.xlsx",  # leading space
    "https://contoso.sharepoint.com/:x:/r/sites/a/z\x7f.xlsx",  # DEL
]


@pytest.mark.parametrize("href", _HOSTILE)
@pytest.mark.parametrize("kind", ["sp_file", "sp_transcript"])
async def test_no_sharepoint_bearer_off_a_tenant_host(
    settings: Settings, monkeypatch, href: str, kind: str
) -> None:
    content = (
        f'<a itemtype="http://schema.skype.com/HyperLink/Files" href="{href}">z</a>'
        if kind == "sp_file"
        else f'<item type="onedriveForBusinessTranscript" uri="{href}" />'
    )
    assert [i["kind"] for i in A.extract(content, "RichText/Html")] == [kind]  # the gate is in process()
    client = _SpClient(lambda u: _Resp(data={"Name": "z.xlsx"}))
    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: client)
    asked: list[str] = []
    notes = await A.process(
        content,
        "RichText/Html",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda host: asked.append(host) or "SPTOK",
    )
    assert asked == [] and client.gets == [] and client.streams == []
    assert len(notes) == 1 and "→" not in notes[0]


@pytest.mark.parametrize("url", _HOSTILE)
async def test_download_sp_file_refuses_a_foreign_host(settings: Settings, url: str) -> None:
    client = _SpClient(lambda u: _Resp(data={"Name": "z.xlsx"}))
    with pytest.raises(ValueError):
        await A._download_sp_file(client, A.html.unescape(url), "SPTOK", settings.media_dir)
    assert client.gets == [] and client.streams == []


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://contoso.sharepoint.com/sites/a/z.xlsx", "contoso.sharepoint.com"),
        ("https://contoso-my.sharepoint.com/:x:/g/personal/u/E", "contoso-my.sharepoint.com"),
        *((u, None) for u in _HOSTILE),
        ("https://evil.example/@contoso.sharepoint.com/x", None),
    ],
)
def test_sp_host(url: str, host: str | None) -> None:
    assert A._sp_host(A.html.unescape(url)) == host


_MALFORMED = [
    "https://[x.sharepoint.com/:x:/r/z.xlsx",
    "https://contoso.sharepoint.com\uff03@evil.example/:x:/r/z",
]


@pytest.mark.parametrize("bad", _MALFORMED)
def test_malformed_links_are_not_hosts(bad: str) -> None:
    assert A._sp_host(bad) is None
    assert A._downloadable(bad.replace("sharepoint.com", "asyncgw.teams.microsoft.com")) is False


@pytest.mark.parametrize("bad", _MALFORMED)
async def test_a_malformed_link_keeps_the_messages_other_attachments(
    settings: Settings, monkeypatch, bad: str
) -> None:
    def handler(url: str) -> _Resp:
        return _Resp(content=b"PNGDATA", headers={"content-type": "image/png"})

    _patch_client(monkeypatch, handler)
    ams_bad = bad.replace("sharepoint.com", "asyncgw.teams.microsoft.com")
    content = (
        f'<a itemtype="http://schema.skype.com/HyperLink/Files" href="{bad}">x</a>'
        f'<img itemtype="http://schema.skype.com/AMSImage" src="{ams_bad}">' + IMG
    )
    notes = await A.process(
        content, "RichText/Html", "sk", settings.media_dir, download=True, sp_token=lambda h: "SPTOK"
    )
    assert (settings.media_dir / "o1.png").exists()
    assert len(notes) == 3 and sum("→" in n for n in notes) == 1


def test_extract_unescapes_sharepoint_urls_once() -> None:
    href = "https://c.sharepoint.com/:x:/r/sites/RD/z.xlsx?d=w1&amp;web=1"
    items = A.extract(
        f'<a itemtype="http://schema.skype.com/HyperLink/Files" href="{href}">z</a>'
        '<item type="onedriveForBusinessTranscript" uri="https://c.sharepoint.com/t?a=1&amp;b=2" />',
        "RichText/Media_CallRecording",
    )
    assert {i["url"] for i in items} == {
        "https://c.sharepoint.com/:x:/r/sites/RD/z.xlsx?d=w1&web=1",
        "https://c.sharepoint.com/t?a=1&b=2",
    }


def test_extract_excludes_sharepoint_videos() -> None:
    mp4_files_itemtype = (
        '<a itemtype="http://schema.skype.com/HyperLink/Files" '
        'href="https://c.sharepoint.com/:v:/r/personal/x/clip.mp4?web=1">clip</a>'
    )
    mp4_by_ext = '<a itemtype="http://schema.skype.com/HyperLink/Files" href="https://c.sharepoint.com/sites/x/clip.mp4">c</a>'
    items = A.extract(mp4_files_itemtype + mp4_by_ext, "RichText/Html")
    assert not any(i["kind"] == "sp_file" for i in items)  # videos excluded even as HyperLink/Files


_SP_VID = "https://c-my.sharepoint.com/:v:/g/personal/x/EQrec123"
REC_VID = (
    '<URIObject type="Video.2/CallRecording.1" uri="">'
    f'<a href="{_SP_VID}">Play</a>'
    f'<item type="amsVideo" uri="{_OBJ}/video" />'
    f'<item type="onedriveForBusinessVideo" uri="{_SP_VID}" driveId="b!x" driveItemId="01A" />'
    "</URIObject>"
)


def test_extract_videos_flag_includes_recording_once() -> None:
    items = A.extract(REC_VID, "RichText/Media_CallRecording", media=A.DEFAULT_MEDIA | {"videos"})
    sp = [i["url"] for i in items if i["kind"] == "sp_file"]
    assert sp == [_SP_VID]  # the <item> and the Play <a> carry the same URL — captured once
    # AMS fallback copy is captured too, and LAST — process() must see the SP outcome first.
    assert items[-1] == {"kind": "video", "url": f"{_OBJ}/video"}


def test_extract_videos_flag_includes_shared_video_files() -> None:
    by_prefix = '<a href="https://c.sharepoint.com/:v:/r/personal/x/rec.mp4?web=1">rec</a>'
    by_ext = '<a itemtype="http://schema.skype.com/HyperLink/Files" href="https://c.sharepoint.com/sites/x/clip.mkv">c</a>'
    items = A.extract(by_prefix + by_ext, "RichText/Html", media=A.DEFAULT_MEDIA | {"videos"})
    sp = {i["url"] for i in items if i["kind"] == "sp_file"}
    assert sp == {
        "https://c.sharepoint.com/:v:/r/personal/x/rec.mp4?web=1",
        "https://c.sharepoint.com/sites/x/clip.mkv",
    }


def test_extract_videos_default_off() -> None:
    items = A.extract(REC_VID, "RichText/Media_CallRecording")
    assert not any(i["kind"] in ("sp_file", "video") for i in items)


async def test_process_ams_video_skipped_when_sp_copy_downloads(settings: Settings, monkeypatch) -> None:
    fetched: list[str] = []

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, **kw):
            return _Resp(data={"Name": "rec.mp4"})  # GetFileByUrl

        def stream(self, method, url, headers=None, **kw):
            fetched.append(url)
            return _Resp(content=b"MP4", headers={"content-type": "video/mp4"})

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    notes = await A.process(
        REC_VID,
        "RichText/Media_CallRecording",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda host: "SPTOK",
        media=A.DEFAULT_MEDIA | {"videos"},
    )
    assert (settings.media_dir / "rec.mp4").exists()  # durable SharePoint copy
    assert not any(u.endswith("/video") for u in fetched)  # AMS copy NOT pulled — redundant
    assert any(n == f"[video: {_OBJ}/video]" for n in notes)  # ref-only note


async def test_process_ams_video_fallback_when_sp_denied(settings: Settings, monkeypatch) -> None:
    def handler(url: str) -> _Resp:
        assert url.endswith("/video")  # only the AMS copy may be fetched here
        return _Resp(content=b"MP4BYTES", headers={"content-type": "video/mp4"})

    _patch_client(monkeypatch, handler)
    notes = await A.process(
        REC_VID,
        "RichText/Media_CallRecording",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda host: "SPTOK",
        # SharePoint copy previously 403'd (block-download policy) → denied cache vetoes it.
        skip_url=lambda u: "sharepoint" in u,
        media=A.DEFAULT_MEDIA | {"videos"},
    )
    assert (settings.media_dir / "rec1.video.mp4").read_bytes() == b"MP4BYTES"
    assert any(n.startswith("[video:") and "→" in n for n in notes)


async def test_process_ams_video_failure_feeds_on_fail(settings: Settings, monkeypatch) -> None:
    """Without on_fail the denied cache never learns, and an expired AMS object is re-polled forever."""

    def handler(url: str) -> _Resp:
        return _Resp(status_code=403)  # expired/denied AMS object

    _patch_client(monkeypatch, handler)
    failed: list[str] = []
    notes = await A.process(
        REC_VID,
        "RichText/Media_CallRecording",
        "sk",
        settings.media_dir,
        download=True,
        skip_url=lambda u: "sharepoint" in u,  # SP copy already denied → AMS attempted
        on_fail=lambda u, exc: failed.append(u),
        media=A.DEFAULT_MEDIA | {"videos"},
    )
    assert failed == [f"{_OBJ}/video"]  # the AMS URL lands in the denied cache
    assert any(n == f"[video: {_OBJ}/video]" for n in notes)  # ref-only, no download claimed
    assert not list(settings.media_dir.glob("*.part"))


async def test_process_ams_video_fallback_when_sp_fails(settings: Settings, monkeypatch) -> None:
    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, **kw):
            return _Resp(status_code=403)  # SharePoint metadata denied, REST and shares alike

        def stream(self, method, url, headers=None, **kw):
            assert url.endswith("/video")  # SP content is never reached; AMS is
            return _Resp(content=b"MP4BYTES", headers={"content-type": "video/mp4"})

    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _C())
    notes = await A.process(
        REC_VID,
        "RichText/Media_CallRecording",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda host: "SPTOK",
        media=A.DEFAULT_MEDIA | {"videos"},
    )
    assert (settings.media_dir / "rec1.video.mp4").read_bytes() == b"MP4BYTES"
    assert any(n.startswith("[video:") and "→" in n for n in notes)


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


# --- per-destination download serialization ------------------------------------------------------
# Duplicate messages reference the same object; concurrent downloads once shared one deterministic
# `.part` path — two writers truncated/mutated each other's bytes and a late failure after the
# winner's rename could leave a corrupt dest that skip-if-exists trusts forever.


class _SlowResp(_Resp):
    async def aiter_bytes(self, size: int = 65536):
        import asyncio

        for i in range(0, len(self.content), size):
            await asyncio.sleep(0.001)  # widen the race window across event-loop turns
            yield self.content[i : i + size]


async def test_concurrent_same_ams_object_downloads_once(settings: Settings, monkeypatch) -> None:
    import asyncio

    calls: list[str] = []

    def handler(url: str) -> _Resp:
        calls.append(url)
        return _SlowResp(content=b"V" * 200_000, headers={"content-type": "video/mp4"})

    client = _AsyncClient(handler)
    url = f"{_OBJ}/video"
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    paths = await asyncio.gather(
        A.fetch_image(client, "sk", url, settings.media_dir, suffix=".video"),
        A.fetch_image(client, "sk", url, settings.media_dir, suffix=".video"),
    )
    assert len(calls) == 1  # loser waited, then took the skip-if-exists path
    assert paths[0] == paths[1]
    assert (settings.media_dir / "rec1.video.mp4").read_bytes() == b"V" * 200_000
    assert not A._dl_locks and not A._dl_refs  # bounded: entries dropped once free


async def test_concurrent_same_sp_file_downloads_once(settings: Settings, monkeypatch) -> None:
    import asyncio

    streams: list[str] = []

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, **kw):
            return _Resp(data={"Name": "rec.mp4"})

        def stream(self, method, url, headers=None, **kw):
            streams.append(url)
            return _SlowResp(content=b"S" * 200_000, headers={"content-type": "video/mp4"})

    client = _C()
    url = "https://c-my.sharepoint.com/:v:/g/personal/x/EQrec123"
    paths = await asyncio.gather(
        A._download_sp_file(client, url, "SPTOK", settings.media_dir),
        A._download_sp_file(client, url, "SPTOK", settings.media_dir),
    )
    assert len(streams) == 1  # one content fetch; the duplicate returned the cached file
    assert paths[0] == paths[1]
    assert (settings.media_dir / "rec.mp4").read_bytes() == b"S" * 200_000
    assert not A._dl_locks and not A._dl_refs


def test_recording_files_pairs_video_and_transcripts(settings: Settings) -> None:
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    sp_url = "https://c-my.sharepoint.com/personal/x/_layouts/15/transcript.ashx?id=1"
    stem = "sp-" + hashlib.sha1(sp_url.split("?")[0].encode()).hexdigest()[:16]
    content = (
        '<URIObject type="Video.2/CallRecording.1" uri="">'
        "<Title>CIR OPS</Title>"
        '<OriginalName v="CIR OPS-20260818-Meeting Recording.mp4" />'
        f'<item type="amsVideo" uri="{_OBJ}/video" />'
        f'<item type="amsTranscript" uri="{_OBJ}/transcript" />'
        f'<item type="onedriveForBusinessTranscript" uri="{sp_url}" />'
        '<RecordingContent duration="1:07:09.315" />'
        "</URIObject>"
    )
    for name in (
        "CIR OPS-20260818-Meeting Recording.mp4",
        "rec1.video.mp4",
        "rec1.video.mp4.part",  # in-flight leftovers must never be listed
        "rec1.transcript.vtt",
        f"{stem}.transcript.json",
        f"{stem}.transcript.vtt",
    ):
        (settings.media_dir / name).write_bytes(b"x")
    entry = A.recording_files(content, settings.media_dir)
    assert entry == {
        "title": "CIR OPS",
        "duration": "1:07:09.315",
        "videos": ["CIR OPS-20260818-Meeting Recording.mp4", "rec1.video.mp4"],
        "transcripts": ["rec1.transcript.vtt", f"{stem}.transcript.json", f"{stem}.transcript.vtt"],
    }


def test_recording_files_finds_a_transcript_saved_from_an_escaped_uri(settings: Settings) -> None:
    """The stem is hashed from the URL extract() stores and fetches: the unescaped one."""
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    stem = "sp-" + hashlib.sha1(b"https://c-my.sharepoint.com/personal/x/a&b").hexdigest()[:16]
    (settings.media_dir / f"{stem}.transcript.vtt").write_bytes(b"WEBVTT")
    content = (
        '<URIObject type="Video.2/CallRecording.1" uri="">'
        '<item type="onedriveForBusinessTranscript" '
        'uri="https://c-my.sharepoint.com/personal/x/a&amp;b?x=1" />'
        "</URIObject>"
    )
    assert A.recording_files(content, settings.media_dir)["transcripts"] == [f"{stem}.transcript.vtt"]


def test_recording_files_finds_a_transcript_saved_under_the_escaped_uri_hash(settings: Settings) -> None:
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    stem = "sp-" + hashlib.sha1(b"https://c-my.sharepoint.com/personal/x/a&amp;b").hexdigest()[:16]
    (settings.media_dir / f"{stem}.transcript.vtt").write_bytes(b"WEBVTT")
    content = (
        '<URIObject type="Video.2/CallRecording.1" uri="">'
        '<item type="onedriveForBusinessTranscript" '
        'uri="https://c-my.sharepoint.com/personal/x/a&amp;b?x=1" />'
        "</URIObject>"
    )
    assert A.recording_files(content, settings.media_dir)["transcripts"] == [f"{stem}.transcript.vtt"]


def test_recording_files_empty_when_nothing_on_disk(settings: Settings) -> None:
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    content = (
        f'<URIObject type="Video.2/CallRecording.1"><item type="amsVideo" uri="{_OBJ}/video" /></URIObject>'
    )
    entry = A.recording_files(content, settings.media_dir)
    assert (
        entry["videos"] == [] and entry["transcripts"] == []
    )  # recording gone: entry still records it existed


# --- 429 on a streamed content GET ------------------------------------------------------------
# SharePoint and the AMS object host throttle bursts; the streamed paths must back off exactly like
# `aget_with_retry` does, or one throttled pass silently loses the file.


async def test_sp_file_content_429_is_retried(settings: Settings) -> None:
    streams: list[str] = []

    class _C:
        async def get(self, url, headers=None, **kw):
            return _Resp(data={"Name": "rec.mp4"})

        def stream(self, method, url, headers=None, **kw):
            streams.append(url)
            if len(streams) == 1:
                return _Resp(status_code=429, headers={"Retry-After": "0"})
            return _Resp(content=b"MP4", headers={"content-type": "video/mp4"})

    path = await A._download_sp_file(_C(), _SP_VID, "SPTOK", settings.media_dir)
    assert len(streams) == 2
    assert Path(path).read_bytes() == b"MP4"


async def test_ams_stream_429_is_retried(settings: Settings) -> None:
    calls: list[str] = []

    def handler(url: str) -> _Resp:
        calls.append(url)
        if len(calls) == 1:
            return _Resp(status_code=429, headers={"Retry-After": "0"})
        return _Resp(content=b"IMG", headers={"content-type": "image/png"})

    settings.media_dir.mkdir(parents=True, exist_ok=True)
    path = await A.fetch_image(_AsyncClient(handler), "sk", f"{_OBJ}/imgpsh_fullsize", settings.media_dir, "")
    assert len(calls) == 2
    assert Path(path).read_bytes() == b"IMG"


async def test_live_image_rebuilds_the_url_and_keeps_only_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """011-AC3: the web's live fetch: imgo view of the id on the given host, the token as the AMS cookie,
    an image body kept in memory; a refusal, a non-image or an oversized body is None."""
    seen: list[_httpx.Request] = []
    answers = {
        "img": _httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"}),
        "html": _httpx.Response(200, content=b"<script>", headers={"content-type": "text/html"}),
        "svg": _httpx.Response(200, content=b"<svg/>", headers={"content-type": "image/svg+xml"}),
        "big": _httpx.Response(200, content=b"x" * 11, headers={"content-type": "image/jpeg"}),
        "gone": _httpx.Response(403),
    }

    def handler(request: _httpx.Request) -> _httpx.Response:
        seen.append(request)
        return answers[request.url.path.split("/")[3]]

    monkeypatch.setattr(A, "_LIVE_MAX", 10)
    host = "fr-prod.asyncgw.teams.microsoft.com"
    async with _httpx.AsyncClient(transport=_httpx.MockTransport(handler)) as client:
        assert await A.live_image(client, "tok", host, "img") == (b"\x89PNG", "image/png")
        for oid in ("html", "svg", "big", "gone"):
            assert await A.live_image(client, "tok", host, oid) is None, oid
    assert str(seen[0].url) == f"https://{host}/v1/objects/img/views/imgo"
    assert seen[0].headers["cookie"] == "skypetoken_asm=tok"


GIPHY_ID = "l0MYt5jPR6QX5pnqM"
GIPHY = f"https://media2.giphy.com/media/v1.Y2lkPTc5MGI3NjEx/{GIPHY_ID}/giphy.gif"


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["o1.full.png", "o1.gif"], "o1.gif"),  # a pasted Teams GIF: the animated imgo copy
        (["o1.gif", "o1.full.png"], "o1.gif"),
        (["o1.full.png", "o1.png"], "o1.png"),  # no GIF: the original before the full view
        (["o1.png", "o1.full.png"], "o1.png"),
        (["o1.bmp", "o1.gif"], "o1.gif"),  # an original sorting before the GIF
        (["o1.full.png", "o1.part", "o1.gif.part"], "o1.full.png"),  # the full view alone, partials never
        (["o1.part"], None),
    ],
)
def test_existing_prefers_the_gif_then_originals_whatever_the_directory_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: list[str], expected: str | None
) -> None:
    """031-AC6: several files for one object id: `.gif` first, other originals next, `.full.*` last."""
    for name in names:
        (tmp_path / name).write_bytes(b"x")
    real = Path.glob
    for order in (lambda found: found, reversed):
        monkeypatch.setattr(Path, "glob", lambda self, pattern, o=order: iter(o(list(real(self, pattern)))))
        got = A.existing(tmp_path, "o1")
        assert (Path(got).name if got else None) == expected
    monkeypatch.setattr(Path, "glob", real)
    assert A.existing(tmp_path, "o1.full") == (
        str(tmp_path / "o1.full.png") if "o1.full.png" in names else None
    )


@pytest.mark.parametrize(
    ("src", "url", "stem"),
    [
        (f"{GIPHY}?cid=x&ct=g", GIPHY, f"giphy-{GIPHY_ID}"),
        (f"https://media.giphy.com/media/{GIPHY_ID}/giphy.gif", None, f"giphy-{GIPHY_ID}"),
        (f"https://media4.giphy.com/media/{GIPHY_ID}/200w.webp", None, f"giphy-{GIPHY_ID}"),
        (f"https://i.giphy.com/{GIPHY_ID}.gif", None, f"giphy-{GIPHY_ID}"),
        (f"https://i.giphy.com/media/{GIPHY_ID}/giphy.webp", None, f"giphy-{GIPHY_ID}"),
        (f"  {GIPHY}\n", GIPHY, f"giphy-{GIPHY_ID}"),  # padded: render and extract read it alike
        (f"https://media2.giphy.com/media/{GIPHY_ID}/200_d.gif", None, f"giphy-{GIPHY_ID}"),
    ],
)
def test_giphy_rebuilds_the_url_from_host_and_path(src: str, url: str | None, stem: str) -> None:
    """031-AC3: the URL fetched is `https://<host><path>`, never the page's string, named by Giphy's id."""
    assert A.giphy(src) == A.Giphy(url or src, stem)


@pytest.mark.parametrize(
    "src",
    [
        f"http://media2.giphy.com/media/{GIPHY_ID}/giphy.gif",
        f"https://media2.giphy.com:443/media/{GIPHY_ID}/giphy.gif",
        f"https://u:p@media2.giphy.com/media/{GIPHY_ID}/giphy.gif",
        f"https://media2.giphy.com./media/{GIPHY_ID}/giphy.gif",
        f"https://mediax.giphy.com/media/{GIPHY_ID}/giphy.gif",
        f"https://i.giphy.com.evil.example/{GIPHY_ID}.gif",
        f"https://evil.example/media/{GIPHY_ID}/giphy.gif",
        f"https://media2.giphy.com/media/{GIPHY_ID}/giphy.svg",
        f"https://media2.giphy.com/media/{GIPHY_ID}/giphy.gif/x",
        "https://media2.giphy.com/media/%2e%2e/giphy.gif",
        f"https://media2.giphy.com/media/{GIPHY_ID}/gi phy.gif",
        f"https://media2.giphy.com/media/{GIPHY_ID}/gi\tphy.gif",
        # Stills share the animated one's stem: archived first, they would be served in its place.
        f"https://media2.giphy.com/media/{GIPHY_ID}/giphy_s.gif",
        f"https://media2.giphy.com/media/v1.Y2lk/{GIPHY_ID}/480w_s.jpg",
        f"https://media2.giphy.com/media/{GIPHY_ID}/200w_s.gif",
        "https://[x/media/a/giphy.gif",
    ],
)
def test_giphy_refuses_anything_else(src: str) -> None:
    """031-AC5: https, the allowlisted hosts alone (no port, userinfo, trailing dot), an image path."""
    assert A.giphy(src) is None


async def test_giphy_image_keeps_only_images_and_sends_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """031-AC3: the web's Giphy fetch: no cookie or token, image types only, size cap, a refusal is None."""
    seen: list[_httpx.Request] = []
    answers = {
        "img": _httpx.Response(200, content=b"GIF89a", headers={"content-type": "image/gif"}),
        "html": _httpx.Response(200, content=b"<script>", headers={"content-type": "text/html"}),
        "svg": _httpx.Response(200, content=b"<svg/>", headers={"content-type": "image/svg+xml"}),
        "big": _httpx.Response(200, content=b"x" * 11, headers={"content-type": "image/gif"}),
        "gone": _httpx.Response(404),
        "moved": _httpx.Response(302, headers={"location": "https://evil.example/x.gif"}),
    }

    def handler(request: _httpx.Request) -> _httpx.Response:
        seen.append(request)
        return answers[request.url.path.split("/")[2]]

    monkeypatch.setattr(A, "_LIVE_MAX", 10)
    async with _httpx.AsyncClient(transport=_httpx.MockTransport(handler)) as client:
        assert await A.giphy_image(client, "https://media2.giphy.com/media/img/giphy.gif") == (
            b"GIF89a",
            "image/gif",
        )
        for name in ("html", "svg", "big", "gone", "moved"):
            assert await A.giphy_image(client, f"https://media2.giphy.com/media/{name}/giphy.gif") is None, (
                name
            )
    assert [r.url.host for r in seen] == ["media2.giphy.com"] * 6  # the redirect is not followed
    assert not any("cookie" in r.headers or "authorization" in r.headers for r in seen)


def _giphy_client(
    monkeypatch: pytest.MonkeyPatch, seen: list[_httpx.Request], answer: _httpx.Response
) -> _httpx.AsyncClient:
    """The archive's shared client; any client the module opens answers alike: nothing leaves the machine."""

    def handler(request: _httpx.Request) -> _httpx.Response:
        seen.append(request)
        return answer

    transport = _httpx.MockTransport(handler)
    real = _httpx.AsyncClient
    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: real(*a, **k, transport=transport))
    return real(transport=transport)


async def test_process_archives_a_giphy_image_once_without_token(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """031-AC2: a Giphy <img> is saved once as `giphy-<id>.<ext>`, fetched without the skype token."""
    seen: list[_httpx.Request] = []
    content = f'<img src="{GIPHY}?cid=x&amp;ct=g" alt="hi"><img src="{GIPHY}">'
    async with _giphy_client(
        monkeypatch, seen, _httpx.Response(200, content=b"GIF89a", headers={"content-type": "image/gif"})
    ) as c:
        for _ in range(2):  # a second pass finds the copy: no request
            notes = await A.process(
                content, "RichText/Html", "sk", settings.media_dir, download=True, client=c
            )
    dest = settings.media_dir / f"giphy-{GIPHY_ID}.gif"
    assert dest.read_bytes() == b"GIF89a"
    assert [str(r.url) for r in seen] == [GIPHY]
    assert "cookie" not in seen[0].headers and "authorization" not in seen[0].headers
    assert notes == [f"[image: {GIPHY} → {dest.resolve().as_uri()}]"]
    assert sorted(p.name for p in settings.media_dir.iterdir()) == [dest.name]


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        (_httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}), 415),
        (_httpx.Response(200, content=b"x" * 11, headers={"content-type": "image/gif"}), 413),
        (_httpx.Response(404), 404),
    ],
)
async def test_process_giphy_refusal_keeps_nothing_and_is_reported(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, answer: _httpx.Response, status: int
) -> None:
    """031-AC3: a non-image, an oversized body or a refusal leaves no file, and reaches on_fail."""
    monkeypatch.setattr(A, "_LIVE_MAX", 10)
    failed: list[tuple[str, int]] = []

    def on_fail(url: str, exc: Exception) -> None:
        failed.append((url, getattr(exc, "status", None) or exc.response.status_code))  # type: ignore[attr-defined]

    seen: list[_httpx.Request] = []
    async with _giphy_client(monkeypatch, seen, answer) as client:
        notes = await A.process(
            f'<img src="{GIPHY}">', "", "sk", settings.media_dir, True, client=client, on_fail=on_fail
        )
    assert notes == [f"[image: {GIPHY}]"]
    assert failed == [(GIPHY, status)]
    assert list(settings.media_dir.iterdir()) == []


async def test_process_giphy_honours_selection_and_download_flag(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """031-AC2: Giphy images are `images`: deselected, nothing; download off, a reference only."""
    content = f'<img src="{GIPHY}">'
    assert A.extract(content, "", frozenset({"files", "transcripts"})) == []
    assert A.extract(content, "") == [{"kind": "giphy", "url": GIPHY}]
    assert A.extract(f'<img src="https://evil.example/media/{GIPHY_ID}/giphy.gif">', "") == []
    assert A.extract(f'<img src=" {GIPHY}\n">', "") == [{"kind": "giphy", "url": GIPHY}]  # padded, as render
    seen: list[_httpx.Request] = []
    async with _giphy_client(monkeypatch, seen, _httpx.Response(500)) as client:
        assert await A.process(content, "", "sk", settings.media_dir, download=False, client=client) == [
            f"[image: {GIPHY}]"
        ]
    assert seen == []


async def test_giphy_cookies_never_ride_the_archives_shared_client(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """031-AC3: a cookie Giphy sets is not sent back on the next Giphy download (no tracking across chats)."""
    seen: list[_httpx.Request] = []
    answer = _httpx.Response(
        200,
        content=b"GIF89a",
        headers={"content-type": "image/gif", "set-cookie": "x=1; Domain=.giphy.com; Path=/"},
    )
    async with _giphy_client(monkeypatch, seen, answer) as shared:
        for oid in ("aaaaaaaa", "bbbbbbbb"):
            src = f"https://media1.giphy.com/media/{oid}/giphy.gif"
            await A.process(f'<img src="{src}">', "", "sk", settings.media_dir, True, client=shared)
    assert len(seen) == 2 and "cookie" not in seen[1].headers


async def test_live_image_builds_the_url_from_the_view() -> None:
    """031-AC6: the URL is rebuilt from host, id and view (imgo by default)."""
    seen: list[_httpx.Request] = []

    def handler(request: _httpx.Request) -> _httpx.Response:
        seen.append(request)
        return _httpx.Response(200, content=b"GIF89a", headers={"content-type": "image/gif"})

    host = "fr-prod.asyncgw.teams.microsoft.com"
    async with _httpx.AsyncClient(transport=_httpx.MockTransport(handler)) as client:
        assert await A.live_image(client, "tok", host, "o1", "imgpsh_mobile_save_anim") is not None
        assert await A.live_image(client, "tok", host, "o1") is not None
    assert [str(r.url) for r in seen] == [
        f"https://{host}/v1/objects/o1/views/imgpsh_mobile_save_anim",
        f"https://{host}/v1/objects/o1/views/imgo",
    ]
