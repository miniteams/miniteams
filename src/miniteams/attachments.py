"""Inbound attachment extraction + download (purple-teams teams_messages.c / teams_contacts.c).

Images and files are referenced inside the message `content` as `<img>` / `<URIObject>` tags
whose URL is used **verbatim** (the host is not rebuilt). Bytes are fetched from the AMS object
host with a `skypetoken_asm` **cookie** — not a Bearer/X-Skypetoken header — and only from the
whitelisted `api.asm.skype.com` / `*.asyncgw.teams.microsoft.com` hosts. Files need two hops:
`GET <uri>/views/original/status` for metadata, then GET its `view_location` for the bytes.
"""

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog

log = structlog.get_logger()

_AMSIMAGE = "http://schema.skype.com/AMSImage"
# Cookie is valid (and only attached) for these object hosts (teams_contacts.c:373).
_OBJECT_HOST_RE = re.compile(r"^(api\.asm\.skype\.com|[^.]+\.asyncgw\.teams\.microsoft\.com)$")
_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_URIOBJ_RE = re.compile(r"<URIObject\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
_CTYPE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


def _attrs(tag: str) -> dict[str, str]:
    return {k.lower(): v for k, v in _ATTR_RE.findall(tag)}


def _downloadable(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return bool(_OBJECT_HOST_RE.match(host))


def _object_id(url: str) -> str:
    match = re.search(r"/objects/([^/]+)", url)
    return match.group(1) if match else "object"


def _safe_name(name: str) -> str:
    # Security: server-supplied filename — collapse any path components to a bare name.
    return Path(name).name or "file"


def extract(content: str, msgtype: str) -> list[dict[str, str]]:
    """Pull attachment references out of message content. No network."""
    items: list[dict[str, str]] = []
    for tag in _IMG_RE.findall(content):
        attrs = _attrs(tag)
        if attrs.get("itemtype") == _AMSIMAGE and attrs.get("src"):
            items.append({"kind": "image", "url": attrs["src"]})
    for tag in _URIOBJ_RE.findall(content):
        attrs = _attrs(tag)
        url = attrs.get("uri") or attrs.get("url_thumbnail")
        if url:
            items.append({"kind": "file", "url": url})
    return items


async def _download_image(client: httpx.AsyncClient, token: str, src: str, media_dir: Path) -> str:
    resp = await client.get(src, headers={"Accept": "image/*"}, cookies={"skypetoken_asm": token})
    resp.raise_for_status()
    ext = _CTYPE_EXT.get((resp.headers.get("content-type") or "").split(";")[0], ".img")
    dest = media_dir / f"{_object_id(src)}{ext}"
    dest.write_bytes(resp.content)
    return str(dest)


async def _download_file(
    client: httpx.AsyncClient, token: str, uri: str, media_dir: Path
) -> tuple[str | None, str, Any]:
    status_url = uri if uri.endswith("/status") else f"{uri.rstrip('/')}/views/original/status"
    cookies = {"skypetoken_asm": token}
    meta = await client.get(status_url, headers={"Accept": "*/*"}, cookies=cookies)
    meta.raise_for_status()
    info = meta.json()
    name = _safe_name(str(info.get("original_filename") or _object_id(uri)))
    size = info.get("content_full_length")
    view = info.get("view_location")
    if info.get("content_state") != "ready" or not view:
        return None, name, size  # not ready yet — surface the ref only
    blob = await client.get(view, headers={"Accept": "*/*"}, cookies=cookies)
    blob.raise_for_status()
    dest = media_dir / name
    dest.write_bytes(blob.content)
    return str(dest), name, size


async def process(content: str, msgtype: str, token: str, media_dir: Path, download: bool) -> list[str]:
    """Return human annotations for the print line, downloading bytes when enabled."""
    items = extract(content, msgtype)
    notes: list[str] = []
    do_fetch = download and any(_downloadable(i["url"]) for i in items)
    if do_fetch:
        media_dir.mkdir(parents=True, exist_ok=True)
        media_dir.chmod(0o700)

    async with httpx.AsyncClient(timeout=60.0) as client:
        for item in items:
            url, kind = item["url"], item["kind"]
            if not (download and _downloadable(url)):
                notes.append(f"[{kind}: {url}]")
                continue
            try:
                if kind == "image":
                    img_path = await _download_image(client, token, url, media_dir)
                    notes.append(f"[image → {img_path}]")
                else:
                    fpath, name, size = await _download_file(client, token, url, media_dir)
                    sz = f" ({size}B)" if size else ""
                    notes.append(f"[file: {name}{sz} → {fpath}]" if fpath else f"[file: {name}{sz} {url}]")
            except Exception as exc:  # noqa: BLE001 — a failed download must not drop the message
                log.debug("attachment_download_failed", kind=kind, url=url, error=str(exc))
                notes.append(f"[{kind}: {url}]")

    if msgtype == "RichText/Media_Card":
        notes.append("[card]")
    return notes
