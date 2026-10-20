"""Outbound inline images: sniffing, limits, HTML layout, AMS upload sequence (httpx mocked)."""

import os
import struct
import zlib
from typing import Any

import pytest

from miniteams import cli, images, send
from miniteams.config import Settings

OID = "0-weu-d1-0123456789abcdef0123456789abcdef"


def png(width: int = 3, height: int = 2) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


def jpeg(width: int, height: int, pre: bytes = b"", app1: bytes = b"") -> bytes:
    """`app1` is an APP1 payload; `pre` goes raw right before the frame header."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    exif = b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 if app1 else b""
    dht = b"\xff\xc4" + struct.pack(">H", 3) + b"\x00"  # C4 sits in the SOF range but holds no size
    sof2 = b"\xff\xc2" + struct.pack(">HBHH", 11, 8, height, width) + b"\x01\x01\x11\x00"
    return b"\xff\xd8" + app0 + exif + dht + pre + sof2 + b"\xff\xd9"


def exif(orientation: int, order: str = "<", kind: int = 3, before: int = 0) -> bytes:
    """APP1 Exif payload with IFD0 holding `before` unrelated tags, then Orientation."""
    tags = [(0x010F, 2, 1, 0)] * before + [(0x0112, kind, 1, orientation)]
    ifd = struct.pack(f"{order}H", len(tags)) + b"".join(
        struct.pack(f"{order}HHIHH", tag, k, n, v, 0) for tag, k, n, v in tags
    )
    head = (b"II" if order == "<" else b"MM") + struct.pack(f"{order}HI", 42, 8)
    return b"Exif\x00\x00" + head + ifd + b"\x00\x00\x00\x00"


def webp(chunk: bytes, payload: bytes) -> bytes:
    body = b"WEBP" + chunk + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (png(3, 2), images.Image("png", 3, 2)),
        (b"GIF89a" + struct.pack("<HH", 7, 5) + b"\x00" * 20, images.Image("gif", 7, 5)),
        (b"GIF87a" + struct.pack("<HH", 1, 1) + b"\x00" * 20, images.Image("gif", 1, 1)),
        (jpeg(640, 480), images.Image("jpeg", 640, 480)),
        (
            webp(b"VP8X", b"\x10\x00\x00\x00" + (99).to_bytes(3, "little") + (49).to_bytes(3, "little")),
            images.Image("webp", 100, 50),
        ),
        (webp(b"VP8L", b"\x2f" + ((19) | (9 << 14)).to_bytes(4, "little")), images.Image("webp", 20, 10)),
        (
            webp(b"VP8 ", b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", 30, 40)),
            images.Image("webp", 30, 40),
        ),
    ],
)
def test_sniff_reads_format_and_natural_size_from_the_bytes(data: bytes, expected: images.Image) -> None:
    # 012-AC24: the format comes from the content, all four formats, natural size from the header.
    assert images.sniff(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        b"BM" + b"\x00" * 40,  # BMP
        b"%PDF-1.7",
        b"\x89PNG\r\n\x1a\n",  # truncated PNG
        png()[:20],  # truncated inside IHDR
        png()[:23],
        b"\xff\xd8\xff\xe0\x00\x10JFIF",  # JPEG without a frame header
        b"RIFF\x00\x00\x00\x00WEBPVP8Z" + b"\x00" * 20,
        b"",
    ],
)
def test_sniff_refuses_anything_else(data: bytes) -> None:
    # 012-AC24: a filename or MIME label never gets a non-image through; only magic bytes count.
    with pytest.raises(ValueError):
        images.sniff(data)


@pytest.mark.parametrize("bad", [0, -1, 100_001, 1.5, True, "10", None])
def test_check_dimensions_wants_integers_in_range(bad: object) -> None:
    # 012-AC24: integer dimensions 1-100000, a bool or float from JSON is not one.
    with pytest.raises(ValueError):
        images.check_dimensions(bad, 10)
    with pytest.raises(ValueError):
        images.check_dimensions(10, bad)
    assert images.check_dimensions(1, 100_000) == (1, 100_000)


def test_sniff_refuses_out_of_range_header_sizes() -> None:
    # 012-AC24: a header claiming 0 or > 100000 px is refused too.
    with pytest.raises(ValueError):
        images.sniff(png(0, 2))
    with pytest.raises(ValueError):
        images.sniff(png(100_001, 1))
    with pytest.raises(ValueError):
        images.sniff(jpeg(5, 0))


def test_check_batch_limits(monkeypatch) -> None:
    # 012-AC24: count, per-image and total byte limits, checked before any upload.
    one = png()
    assert len(images.check_batch([one] * images.MAX_IMAGES)) == images.MAX_IMAGES
    with pytest.raises(ValueError, match="at most"):
        images.check_batch([one] * (images.MAX_IMAGES + 1))
    with pytest.raises(ValueError, match="no image"):
        images.check_batch([])
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", len(one) - 1)
    with pytest.raises(ValueError, match="an image is over"):
        images.check_batch([one])
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", len(one))
    monkeypatch.setattr(images, "MAX_TOTAL_BYTES", len(one) * 2 - 1)
    with pytest.raises(ValueError, match="in total"):
        images.check_batch([one, one])


def test_images_html_matches_the_native_shape_in_order() -> None:
    # 012-AC26: one paragraph, images in order, each the native client's AMSImage <img>.
    other = "0-weu-d1-ffffffffffffffffffffffffffffffff"
    out = images.images_html(
        "fr-prod.asyncgw.teams.microsoft.com",
        [(OID, images.Image("png", 3, 2)), (other, images.Image("jpeg", 640, 480))],
    )
    assert out == (
        f'<p><img alt="image" height="2" id="{OID}" itemid="{OID}" itemscope="png" '
        'itemtype="http://schema.skype.com/AMSImage" '
        f'src="https://fr-prod.asyncgw.teams.microsoft.com/v1/objects/{OID}/views/imgo" width="3">'
        f'<img alt="image" height="480" id="{other}" itemid="{other}" itemscope="jpeg" '
        'itemtype="http://schema.skype.com/AMSImage" '
        f'src="https://fr-prod.asyncgw.teams.microsoft.com/v1/objects/{other}/views/imgo" width="640"></p>'
    )


@pytest.mark.parametrize("bad", ['x" onerror="y', "a/b", "", "a" * 129])
def test_images_html_refuses_odd_object_ids(bad: str) -> None:
    # 012-AC24: a remote-supplied id never reaches an attribute or a URL unchecked.
    with pytest.raises(ValueError):
        images.images_html("h", [(bad, images.Image("png", 1, 1))])


class _Resp:
    def __init__(self, status: int = 201, data: Any = None, content: bytes = b"") -> None:
        self.status_code = status
        self.is_error = status >= 400
        self.is_success = 200 <= status < 300
        self._data = {} if data is None else data
        self.content = content
        self.reason_phrase = "x"
        self.request = None

    def json(self) -> Any:
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


@pytest.fixture
def wire(monkeypatch) -> list[tuple[str, str, dict[str, Any]]]:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    ids = iter([OID, "0-weu-d1-ffffffffffffffffffffffffffffffff"])

    def post(url: str, **kw: Any) -> _Resp:
        calls.append(("POST", url, kw))
        if url.endswith("/v1/objects/"):
            return _Resp(201, {"id": next(ids)}, b"{}")
        return _Resp(201, {}, b"{}")

    def put(url: str, **kw: Any) -> _Resp:
        calls.append(("PUT", url, kw))
        return _Resp(201, content=b"not json")

    monkeypatch.setattr(send._CLIENT, "post", post)
    monkeypatch.setattr(send._CLIENT, "put", put)
    return calls


def test_send_images_request_sequence_and_bodies(settings: Settings, wire) -> None:
    # 012-AC26: create + upload per image, in order, then one message: text first, then the images.
    host = settings.asyncgw_host
    jpg = jpeg(640, 480)
    result = send.send_images(
        settings, "sk", "8:orgid:peer", "hi <b>", [("a.png", png()), ("b.jpg", jpg)], "Me"
    )
    assert [(m, u.split("/v1/")[-1]) for m, u, _ in wire] == [
        ("POST", "objects/"),
        ("PUT", f"objects/{OID}/content/imgpsh"),
        ("POST", "objects/"),
        ("PUT", "objects/0-weu-d1-ffffffffffffffffffffffffffffffff/content/imgpsh"),
        ("POST", "users/ME/conversations/8%3Aorgid%3Apeer/messages"),
    ]
    assert all(u.startswith(f"https://{host}/v1/objects/") for _, u, _ in wire[:4])
    create, upload = wire[0][2], wire[1][2]
    assert create["json"] == {
        "type": "pish/image",
        "permissions": {"8:orgid:peer": ["read"]},
        "filename": "a.png",
        "sharingMode": "Inline",
    }
    assert create["headers"]["Authorization"] == "skype_token sk"
    assert upload["headers"]["Authorization"] == "skype_token sk"
    assert upload["headers"]["Content-Type"] == "application/octet-stream"
    assert upload["content"] == png() and wire[3][2]["content"] == jpg
    message = wire[4][2]["json"]
    assert message["content"].startswith("<p>hi &lt;b&gt;</p><p><img ")
    assert message["content"].count('itemtype="http://schema.skype.com/AMSImage"') == 2
    assert message["content"].index(OID) < message["content"].index("0-weu-d1-ffff")
    assert message["messagetype"] == "RichText/Html"
    assert result["status"] == 201


def test_send_images_without_text_has_no_empty_paragraph(settings: Settings, wire) -> None:
    send.send_images(settings, "sk", "t", "", [("a.png", png())], "Me")
    assert wire[-1][2]["json"]["content"].startswith("<p><img ")


def test_send_images_validates_all_before_uploading(settings: Settings, wire) -> None:
    # 012-AC24: a bad image in the batch sends nothing at all.
    with pytest.raises(ValueError):
        send.send_images(settings, "sk", "t", "x", [("a.png", png()), ("b.png", b"GIF8")], "Me")
    assert wire == []


def test_upload_refuses_a_missing_or_odd_object_id(settings: Settings, monkeypatch) -> None:
    # 012-AC24: the server's id goes into a URL and HTML only once it has the expected shape.
    monkeypatch.setattr(send._CLIENT, "post", lambda url, **kw: _Resp(201, {"id": "../x"}, b"{}"))
    with pytest.raises(RuntimeError, match="object id"):
        send.upload_image(settings, "sk", "t", png(), "a.png")


def test_upload_failure_raises(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(send._CLIENT, "post", lambda url, **kw: _Resp(201, {"id": OID}, b"{}"))
    monkeypatch.setattr(send._CLIENT, "put", lambda url, **kw: _Resp(403, {}, b""))
    with pytest.raises(Exception, match="image upload failed"):
        send.upload_image(settings, "sk", "t", png(), "a.png")


def test_send_message_client_message_id_passthrough(settings: Settings, monkeypatch) -> None:
    # 012-AC26 (phase 5a): the caller may fix the clientmessageid (manual retry reuses it).
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(send._CLIENT, "post", lambda url, **kw: seen.append(kw["json"]) or _Resp(201))
    assert (
        send.send_message(settings, "sk", "t", "x", "Me", client_message_id="42")["clientmessageid"] == "42"
    )
    assert seen[-1]["clientmessageid"] == "42"
    default = send.send_message(settings, "sk", "t", "x", "Me")["clientmessageid"]
    assert default.isdigit() and default != "42"


def _parse(monkeypatch, argv: list[str]) -> Any:
    seen: list[Any] = []
    monkeypatch.setattr(cli, "cmd_send", lambda s, a: seen.append(a) or 0)
    assert cli.main(argv) == 0
    monkeypatch.undo()
    return seen[0]


def test_cli_send_images(settings: Settings, monkeypatch, tmp_path) -> None:
    # 012-AC26: `miniteams send TEXT --thread T --image a --image b`.
    a, b = tmp_path / "a.png", tmp_path / "b.jpg"
    a.write_bytes(png())
    b.write_bytes(jpeg(4, 4))
    args = _parse(monkeypatch, ["send", "hello", "--thread", "T", "--image", str(a), "--image", str(b)])
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: ({}, "sk"))
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        "miniteams.send.send_images",
        lambda s, tok, thread, text, imgs, name, **kw: seen.update(thread=thread, text=text, imgs=imgs),
    )
    assert cli.cmd_send(settings, args) == 0
    # 029-AC10: the text is rendered as the composer does; the posted body is the same `<p>` as before.
    assert seen == {"thread": "T", "text": "<p>hello</p>", "imgs": [("a.png", png()), ("b.jpg", jpeg(4, 4))]}


def test_cli_send_refuses_a_non_image_before_auth(settings: Settings, monkeypatch, tmp_path) -> None:
    # 012-AC24: a file named .png that is not one is refused, and no login is attempted.
    fake = tmp_path / "x.png"
    fake.write_bytes(b"<html>")
    args = _parse(monkeypatch, ["send", "--image", str(fake)])
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: pytest.fail("auth before validation"))
    assert cli.cmd_send(settings, args) == 2


@pytest.mark.parametrize(
    ("pre", "case"),
    [
        (b"\xff\xff", "fill bytes"),
        (b"\xff\xd0", "RST0"),
        (b"\xff\x01", "TEM"),
        (b"\xff\xd3\xff", "RST3+fill"),
    ],
)
def test_jpeg_walk_skips_fill_and_standalone_markers(pre: bytes, case: str) -> None:
    # 012-AC24: real encoders emit FF fill bytes and length-less markers before the frame header.
    assert images.sniff(jpeg(640, 480, pre=pre)) == images.Image("jpeg", 640, 480), case


@pytest.mark.parametrize(
    ("app1", "expected"),
    [
        (exif(1), (640, 480)),
        (exif(6), (480, 640)),
        (exif(8), (480, 640)),
        (exif(6, order=">"), (480, 640)),
        (exif(5, before=2), (480, 640)),
        (exif(3), (640, 480)),
    ],
)
def test_jpeg_exif_orientation_swaps_quarter_turns(app1: bytes, expected: tuple[int, int]) -> None:
    # 012-AC24: the size is the displayed one, as the browser measures it (orientations 5-8 swap).
    assert images.sniff(jpeg(640, 480, app1=app1)) == images.Image("jpeg", *expected)


@pytest.mark.parametrize(
    "app1",
    [
        b"Exif\x00\x00",  # no TIFF header
        b"Exif\x00\x00XX" + b"\x00" * 10,  # unknown byte order
        b"Exif\x00\x00II*\x00" + struct.pack("<I", 4000),  # IFD offset past the end
        exif(6)[:-12],  # entry cut short
        b"Exif\x00\x00II*\x00\x08\x00\x00\x00" + struct.pack("<H", 60000),  # count far over the buffer
        exif(6, kind=4),  # LONG instead of SHORT
        exif(9),  # out of range
        b"XMP\x00" + b"\x00" * 30,  # an APP1 that is not Exif
    ],
)
def test_jpeg_malformed_exif_is_ignored(app1: bytes) -> None:
    # 012-AC24: a broken EXIF block never crashes nor rotates; the frame size stands.
    assert images.sniff(jpeg(640, 480, app1=app1)) == images.Image("jpeg", 640, 480)


@pytest.mark.parametrize(
    "data",
    [
        webp(b"VP8L", b"\x2e" + ((19) | (9 << 14)).to_bytes(4, "little")),  # bad VP8L signature
        webp(b"VP8 ", b"\x00\x00\x00\x9d\x01\x2b" + struct.pack("<HH", 30, 40)),  # bad start code
        webp(b"VP8 ", b"\x00\x00\x00\x9e\x01\x2a" + struct.pack("<HH", 30, 40)),
    ],
)
def test_webp_bad_signatures_are_refused(data: bytes) -> None:
    with pytest.raises(ValueError):
        images.sniff(data)


def test_webp_vp8_ignores_the_scale_bits() -> None:
    # The top two bits of each VP8 dimension are an upscale hint, not part of the size.
    data = webp(b"VP8 ", b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", 30 | 0xC000, 40 | 0x4000))
    assert images.sniff(data) == images.Image("webp", 30, 40)


def test_send_images_passes_the_client_message_id(settings: Settings, wire) -> None:
    # 012-AC26 (phase 5a): a manual retry of an image send reuses its clientmessageid.
    result = send.send_images(settings, "sk", "t", "x", [("a.png", png())], "Me", client_message_id="77")
    assert wire[-1][2]["json"]["clientmessageid"] == "77"
    assert result["clientmessageid"] == "77"


@pytest.mark.parametrize("extra", [0, 400])
def test_send_images_checks_the_length_before_uploading(settings: Settings, wire, extra: int) -> None:
    # 012-AC24: text that fits alone but not with the image block is refused before any upload.
    room = send.MAX_HTML_BYTES - len(images.images_html(settings.asyncgw_host, [(OID, images.sniff(png()))]))
    with pytest.raises(ValueError, match="too long"):
        send.send_images(settings, "sk", "t", "a" * (room - 7 + extra), [("a.png", png())], "Me")
    assert wire == []


def test_send_images_failed_second_upload_sends_nothing(settings: Settings, monkeypatch) -> None:
    # 012-AC24: a failed upload stops the send; no message goes out with missing images.
    calls: list[str] = []
    ids = iter([OID, "0-weu-d1-ffffffffffffffffffffffffffffffff"])

    def post(url: str, **kw: Any) -> _Resp:
        calls.append(f"POST {url.split('/v1/')[-1]}")
        return _Resp(201, {"id": next(ids)}, b"{}")

    def put(url: str, **kw: Any) -> _Resp:
        calls.append("PUT")
        return _Resp(201) if calls.count("PUT") == 1 else _Resp(403)

    monkeypatch.setattr(send._CLIENT, "post", post)
    monkeypatch.setattr(send._CLIENT, "put", put)
    with pytest.raises(Exception, match="image upload failed: HTTP 403"):
        send.send_images(settings, "sk", "t", "x", [("a.png", png()), ("b.png", png())], "Me")
    assert calls == ["POST objects/", "PUT", "POST objects/", "PUT"]
    assert not any("messages" in call for call in calls)


@pytest.mark.parametrize("status", [302, 304, 101])
def test_upload_create_wants_a_2xx(settings: Settings, monkeypatch, status: int) -> None:
    # 012-AC24: a redirect is not an object, even with an id-shaped body.
    monkeypatch.setattr(send._CLIENT, "post", lambda url, **kw: _Resp(status, {"id": OID}, b"{}"))
    monkeypatch.setattr(send._CLIENT, "put", lambda url, **kw: pytest.fail("uploaded after a non-2xx"))
    with pytest.raises(Exception, match="image create failed"):
        send.upload_image(settings, "sk", "t", png(), "a.png")


@pytest.mark.parametrize("status", [302, 304])
def test_upload_put_wants_a_2xx(settings: Settings, monkeypatch, status: int) -> None:
    monkeypatch.setattr(send._CLIENT, "post", lambda url, **kw: _Resp(201, {"id": OID}, b"{}"))
    monkeypatch.setattr(send._CLIENT, "put", lambda url, **kw: _Resp(status))
    with pytest.raises(Exception, match="image upload failed"):
        send.upload_image(settings, "sk", "t", png(), "a.png")


@pytest.mark.parametrize("body", [[OID], OID, None, ValueError("not json"), {"id": 5}])
def test_upload_create_body_must_be_an_object_with_an_id(settings: Settings, monkeypatch, body: Any) -> None:
    monkeypatch.setattr(send._CLIENT, "post", lambda url, **kw: _Resp(201, body, b"x"))
    with pytest.raises(RuntimeError, match="object id"):
        send.upload_image(settings, "sk", "t", png(), "a.png")


def test_cli_send_refuses_an_oversized_file(settings: Settings, monkeypatch, tmp_path) -> None:
    # 012-AC24: the per-image cap is checked on the file size, before the bytes are loaded.
    big = tmp_path / "big.png"
    big.write_bytes(png() + b"\x00" * 64)
    args = _parse(monkeypatch, ["send", "--image", str(big)])
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", len(png()))
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: pytest.fail("auth before validation"))
    assert cli.cmd_send(settings, args) == 2


def test_jpeg_stray_byte_after_a_segment_stops_the_walk() -> None:
    # 012-AC24: a segment not followed by a marker is a broken file, never a size read off garbage.
    data = jpeg(640, 480, pre=b"\x00")
    with pytest.raises(ValueError, match="frame size"):
        images.sniff(data)


def _no_auth(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: pytest.fail("auth before validation"))


def _bad_image_rc(settings: Settings, monkeypatch, path: str) -> int:
    args = _parse(monkeypatch, ["send", "x", "--image", str(path)])
    _no_auth(monkeypatch)
    return cli.cmd_send(settings, args)


def test_cli_refuses_a_fifo_without_blocking(settings: Settings, monkeypatch, tmp_path) -> None:
    # 012-AC24: a FIFO would block the open forever; refused from its type (test timeout = hang).
    fifo = tmp_path / "pipe.png"
    os.mkfifo(fifo)
    assert _bad_image_rc(settings, monkeypatch, str(fifo)) == 2


@pytest.mark.skipif(not os.path.exists("/dev/zero"), reason="no /dev/zero")
def test_cli_refuses_a_character_device(settings: Settings, monkeypatch) -> None:
    # 012-AC24: /dev/zero reports size 0 and never ends; refused before any read.
    assert _bad_image_rc(settings, monkeypatch, "/dev/zero") == 2


def test_cli_refuses_a_missing_path_and_a_directory(settings: Settings, monkeypatch, tmp_path) -> None:
    # 012-AC24: a bad path is a clear rc 2 (send_bad_file), not a crash through command_failed.
    assert _bad_image_rc(settings, monkeypatch, str(tmp_path / "nope.png")) == 2
    assert _bad_image_rc(settings, monkeypatch, str(tmp_path)) == 2


def test_read_image_reads_at_most_one_byte_over_the_cap(monkeypatch, tmp_path) -> None:
    # 012-AC24: a file that grew past its stat size is still cut at limit + 1 and refused.
    grown = tmp_path / "grown.png"
    grown.write_bytes(png() + b"\x00" * 4096)
    reads: list[int] = []
    real_fdopen = os.fdopen

    class Spy:
        def __init__(self, fh: Any) -> None:
            self.fh = fh

        def __enter__(self) -> Spy:
            return self

        def __exit__(self, *exc: object) -> None:
            self.fh.close()

        def fileno(self) -> int:
            return int(self.fh.fileno())

        def read(self, n: int = -1) -> bytes:
            reads.append(n)
            return bytes(self.fh.read(n))

    monkeypatch.setattr(cli.os, "fdopen", lambda fd, mode: Spy(real_fdopen(fd, mode)))
    with pytest.raises(ValueError, match="over"):
        cli._read_image(grown, 100)
    assert reads == [101]
    assert cli._read_image(grown, len(grown.read_bytes())) == grown.read_bytes()


def test_read_image_rechecks_the_opened_fd(monkeypatch, tmp_path) -> None:
    # 012-AC24: a path swapped for a non-regular file between the stat and the open is refused.
    ok = tmp_path / "a.png"
    ok.write_bytes(png())
    real_fstat = os.fstat
    monkeypatch.setattr(
        cli.os, "fstat", lambda fd: os.stat_result((0o010644, *real_fstat(fd)[1:]))
    )  # S_IFIFO
    with pytest.raises(ValueError, match="not a regular file"):
        cli._read_image(ok, 1 << 20)


@pytest.mark.parametrize("kind", ["fifo", "dir"])
def test_read_image_refuses_non_regular_before_opening(monkeypatch, tmp_path, kind: str) -> None:
    # 012-AC24: the type check comes before the open, so a FIFO's open never gets a chance to block.
    target = tmp_path / "x.png"
    os.mkfifo(target) if kind == "fifo" else target.mkdir()
    monkeypatch.setattr(cli.os, "open", lambda *a: pytest.fail("opened a non-regular file"))
    with pytest.raises(ValueError, match="not a regular file"):
        cli._read_image(target, 1 << 20)


def test_read_image_opens_non_blocking(monkeypatch, tmp_path) -> None:
    # 012-AC24: a FIFO swapped in after the stat cannot hang the open.
    ok = tmp_path / "a.png"
    ok.write_bytes(png())
    flags: list[int] = []
    real_open = os.open
    monkeypatch.setattr(cli.os, "open", lambda path, flag: flags.append(flag) or real_open(path, flag))
    assert cli._read_image(ok, 1 << 20) == png()
    assert flags and flags[0] & os.O_NONBLOCK
