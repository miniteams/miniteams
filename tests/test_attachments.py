"""Attachment reference extraction + host gating (no network)."""

from miniteams.attachments import _downloadable, _object_id, _safe_name, extract

IMG = '<img itemtype="http://schema.skype.com/AMSImage" src="https://api.asm.skype.com/v1/objects/abc/views/imgo">'


def test_extract_inline_image() -> None:
    assert extract(IMG, "RichText/Html") == [
        {"kind": "image", "url": "https://api.asm.skype.com/v1/objects/abc/views/imgo"}
    ]


def test_extract_ignores_non_ams_image() -> None:
    assert extract('<img src="https://x/y.png">', "RichText/Html") == []


def test_extract_uriobject_file() -> None:
    content = '<URIObject uri="https://eu.asyncgw.teams.microsoft.com/v1/objects/f1">f</URIObject>'
    items = extract(content, "RichText/Media_GenericFile")
    assert items == [{"kind": "file", "url": "https://eu.asyncgw.teams.microsoft.com/v1/objects/f1"}]


def test_downloadable_host_whitelist() -> None:
    assert _downloadable("https://api.asm.skype.com/v1/objects/x")
    assert _downloadable("https://eu-prod.asyncgw.teams.microsoft.com/v1/objects/x")
    assert not _downloadable("https://evil.example.com/v1/objects/x")
    assert not _downloadable("not-a-url")


def test_safe_name_strips_path_traversal() -> None:
    assert _safe_name("../../etc/passwd") == "passwd"
    assert _safe_name("") == "file"


def test_object_id_extraction() -> None:
    assert _object_id("https://h/v1/objects/abc-123/views/imgo") == "abc-123"
    assert _object_id("https://h/no-objects-here") == "object"
