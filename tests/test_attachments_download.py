"""Attachment download paths with a fake async httpx client."""

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


class _AsyncClient:
    def __init__(self, handler: Any) -> None:
        self._handler = handler

    async def __aenter__(self) -> _AsyncClient:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def get(self, url: str, headers: Any = None, cookies: Any = None) -> _Resp:
        return self._handler(url)


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
