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
_VIEW_RE = re.compile(r"/views/[^/?#]+")
_FULL_VIEW = "imgpsh_fullsize"  # full-resolution view (vs the bounded `imgo` Teams references)


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


async def _stream_to(client: httpx.AsyncClient, url: str, token: str, dest: Path) -> httpx.Headers:
    # Stream to disk in chunks: a whole-body `.content` buffer spikes RSS by the full file size,
    # and CPython rarely returns freed arenas to the OS — so one fat attachment pins RSS for the
    # life of the (forever-running) stream. Chunked writes cap peak RAM at one chunk.
    try:
        async with client.stream(
            "GET", url, headers={"Accept": "*/*"}, cookies={"skypetoken_asm": token}
        ) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes(65536):
                    fh.write(chunk)
            return resp.headers
    except BaseException:
        # A truncated file is worse than none: it looks complete to any later
        # skip-if-exists check. Never leave partial bytes behind.
        dest.unlink(missing_ok=True)
        raise


def _existing(media_dir: Path, stem: str) -> str | None:
    """First already-downloaded file for this object id (any extension), else None."""
    for match in media_dir.glob(f"{stem}.*"):
        if not match.name.endswith(".part"):
            return str(match)
    return None


async def _fetch_image(client: httpx.AsyncClient, token: str, url: str, media_dir: Path, suffix: str) -> str:
    stem = f"{_object_id(url)}{suffix}"
    cached = _existing(media_dir, stem)
    if cached:
        return cached  # skip re-download (archive re-runs, same object across messages)
    # Content-type is in the response headers (available before the body), so name the file after
    # a HEAD-cheap streamed GET. Write to a temp path first, then rename once the ext is known.
    tmp = media_dir / f"{stem}.part"
    headers = await _stream_to(client, url, token, tmp)
    ext = _CTYPE_EXT.get((headers.get("content-type") or "").split(";")[0], ".img")
    dest = media_dir / f"{_object_id(url)}{suffix}{ext}"
    tmp.rename(dest)
    return str(dest)


async def _download_image(
    client: httpx.AsyncClient, token: str, src: str, media_dir: Path
) -> tuple[str, str | None]:
    """Fetch the optimized view (as referenced) and the full-resolution view; return (optim, full)."""
    optim = await _fetch_image(client, token, src, media_dir, suffix="")
    full_url = _VIEW_RE.sub(f"/views/{_FULL_VIEW}", src)
    if full_url == src:
        return optim, None
    try:
        full = await _fetch_image(client, token, full_url, media_dir, suffix=".full")
    except Exception as exc:  # noqa: BLE001 — full view may 404; the optimized one still stands
        log.debug("full_image_failed", url=full_url, error=str(exc))
        return optim, None
    return optim, full


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
    dest = media_dir / name
    if dest.exists():
        return str(dest), name, size  # already downloaded — skip
    tmp = media_dir / f"{name}.part"
    await _stream_to(client, view, token, tmp)  # chunked: files can be arbitrarily large
    tmp.rename(dest)  # atomic: dest either absent or complete
    return str(dest), name, size


async def process(
    content: str,
    msgtype: str,
    token: str,
    media_dir: Path,
    download: bool,
    client: httpx.AsyncClient | None = None,
) -> list[str]:
    """Return human annotations for the print line, downloading bytes when enabled.

    Pass `client` to reuse a shared connection pool (archive downloads thousands of attachments
    concurrently); when omitted a private client is opened for the call (live-stream path)."""
    items = extract(content, msgtype)
    notes: list[str] = []
    do_fetch = download and any(_downloadable(i["url"]) for i in items)
    if do_fetch:
        media_dir.mkdir(parents=True, exist_ok=True)
        media_dir.chmod(0o700)

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60.0)
    try:
        for item in items:
            url, kind = item["url"], item["kind"]
            if not (download and _downloadable(url)):
                notes.append(f"[{kind}: {url}]")
                continue
            try:
                if kind == "image":
                    optim_path, full_path = await _download_image(client, token, url, media_dir)
                    # Show the local file:// (full-res when available) next to the original URL.
                    # resolve(): as_uri() rejects relative paths (a relative media_dir would 500).
                    primary = Path(full_path or optim_path).resolve().as_uri()
                    extra = f" (optim {Path(optim_path).resolve().as_uri()})" if full_path else ""
                    notes.append(f"[image: {url} → {primary}{extra}]")
                else:
                    fpath, name, size = await _download_file(client, token, url, media_dir)
                    sz = f" ({size}B)" if size else ""
                    notes.append(f"[file: {name}{sz} → {fpath}]" if fpath else f"[file: {name}{sz} {url}]")
            except Exception as exc:  # noqa: BLE001 — a failed download must not drop the message
                log.debug("attachment_download_failed", kind=kind, url=url, error=str(exc))
                notes.append(f"[{kind}: {url}]")
    finally:
        if owns_client:
            await client.aclose()

    if msgtype == "RichText/Media_Card":
        notes.append("[card]")
    return notes
