"""Outbound inline images (spec 012 phase 5): content sniffing, limits, message HTML. Pure, stdlib.

The format comes from the magic bytes, never from a MIME label or a filename.
"""

import html
import re
import struct
from dataclasses import dataclass
from typing import TypeIs

MAX_IMAGES = 8
MAX_IMAGE_BYTES = 10 << 20
MAX_TOTAL_BYTES = 40 << 20
MAX_SIDE = 100_000
MAX_OBJECT_ID = 128
# Interpolated into URLs and HTML attributes: AMS ids look like `0-weu-d1-<32 hex>`.
OBJECT_ID_RE = re.compile(rf"[A-Za-z0-9_-]{{1,{MAX_OBJECT_ID}}}")

_AMS_IMAGE = "http://schema.skype.com/AMSImage"
# JPEG start-of-frame markers; C4 (DHT), C8 (JPG) and CC (DAC) share the range but carry no size.
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


@dataclass(frozen=True, slots=True)
class Image:
    kind: str  # png | jpeg | gif | webp
    width: int
    height: int


def _side(value: object) -> TypeIs[int]:
    return type(value) is int and 1 <= value <= MAX_SIDE


def check_dimensions(width: object, height: object) -> tuple[int, int]:
    """Integers 1–MAX_SIDE; a bool or a float is refused (browser-measured sizes arrive as JSON)."""
    if _side(width) and _side(height):
        return width, height
    raise ValueError(f"image dimensions must be integers 1-{MAX_SIDE}")


def _exif_orientation(segment: bytes) -> int:
    """EXIF Orientation (1-8) from an APP1 payload; 1 when absent or malformed."""
    if not segment.startswith(b"Exif\x00\x00"):
        return 1
    tiff = segment[6:]
    order = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if order is None:
        return 1
    try:
        (ifd,) = struct.unpack_from(f"{order}I", tiff, 4)
        (count,) = struct.unpack_from(f"{order}H", tiff, ifd)
        for entry in range(ifd + 2, ifd + 2 + 12 * count, 12):  # bounded: a short buffer raises
            tag, kind, _, value = struct.unpack_from(f"{order}HHIH", tiff, entry)
            if tag == 0x0112:
                return value if kind == 3 and 1 <= value <= 8 else 1
    except struct.error:
        pass
    return 1


def _jpeg_size(data: bytes) -> tuple[int, int]:
    i, orientation = 2, 1
    while i + 9 <= len(data):
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in _JPEG_SOF:
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            # Orientations 5-8 turn the picture a quarter: the displayed (browser-natural) size swaps.
            return (height, width) if orientation >= 5 else (width, height)
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # standalone markers: no length field
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if marker == 0xE1 and orientation == 1:
            orientation = _exif_orientation(data[i + 4 : i + 2 + length])
        i += 2 + length
    raise ValueError("JPEG without a readable frame size")


def _webp_size(data: bytes) -> tuple[int, int]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    raise ValueError("WebP without a readable frame size")


def sniff(data: bytes) -> Image:
    """Format and natural size from the header; ValueError for anything else."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR" and len(data) >= 24:
        kind, (width, height) = "png", struct.unpack(">II", data[16:24])
    elif data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        kind, (width, height) = "gif", struct.unpack("<HH", data[6:10])
    elif data.startswith(b"\xff\xd8\xff"):
        kind, (width, height) = "jpeg", _jpeg_size(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind, (width, height) = "webp", _webp_size(data)
    else:
        raise ValueError("unsupported image: PNG, JPEG, GIF or WebP only")
    return Image(kind, *check_dimensions(width, height))


def check_batch(blobs: list[bytes]) -> list[Image]:
    """Every limit checked before anything is uploaded."""
    if not blobs:
        raise ValueError("no image")
    if len(blobs) > MAX_IMAGES:
        raise ValueError(f"at most {MAX_IMAGES} images per message")
    if any(len(blob) > MAX_IMAGE_BYTES for blob in blobs):
        raise ValueError(f"an image is over {MAX_IMAGE_BYTES >> 20} MiB")
    if sum(map(len, blobs)) > MAX_TOTAL_BYTES:
        raise ValueError(f"images are over {MAX_TOTAL_BYTES >> 20} MiB in total")
    return [sniff(blob) for blob in blobs]


def images_html(host: str, uploaded: list[tuple[str, Image]]) -> str:
    """One paragraph holding the images in order, in the native client's `<img>` shape."""
    tags = []
    for object_id, image in uploaded:
        if not OBJECT_ID_RE.fullmatch(object_id):
            raise ValueError("unexpected AMS object id")
        src = html.escape(f"https://{host}/v1/objects/{object_id}/views/imgo")
        # Bare id: the chat service stores it as `x_<id>` itself (seen live 2026-10-13).
        tags.append(
            f'<img alt="image" height="{image.height}" id="{object_id}" itemid="{object_id}" '
            f'itemscope="{image.kind}" itemtype="{_AMS_IMAGE}" src="{src}" width="{image.width}">'
        )
    return f"<p>{''.join(tags)}</p>"
