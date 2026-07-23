"""Attachment download paths with a fake async httpx client."""

from pathlib import Path
from typing import Any

from miniteams import attachments as A
from miniteams.config import Settings


class _Resp:
    def __init__(self, content: bytes = b"", headers: dict | None = None, data: Any = None) -> None:
        self.content = content
        self.headers = headers or {}
        self._data = data

    def raise_for_status(self) -> None:
        pass

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
    files = list(settings.media_dir.glob("sp-*.transcript.vtt"))
    assert len(files) == 1 and files[0].read_bytes() == b"WEBVTT\n\nsp"


async def test_process_sp_transcript_ref_only_without_token(settings: Settings) -> None:
    notes = await A.process(REC_SP, "RichText/Media_CallRecording", "sk", settings.media_dir, download=True)
    assert notes == [f"[transcript(sp): {SP}]"]  # no sp_token → ref only, no crash
