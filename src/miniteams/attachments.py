"""Inbound attachment extraction + download (purple-teams teams_messages.c / teams_contacts.c).

Images and files are referenced inside the message `content` as `<img>` / `<URIObject>` tags
whose URL is used **verbatim** (the host is not rebuilt). Bytes are fetched from the AMS object
host with a `skypetoken_asm` **cookie** — not a Bearer/X-Skypetoken header — and only from the
whitelisted `api.asm.skype.com` / `*.asyncgw.teams.microsoft.com` hosts. Files need two hops:
`GET <uri>/views/original/status` for metadata, then GET its `view_location` for the bytes.
"""

import base64
import hashlib
import re
from collections.abc import Callable
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
# Call-recording messages nest the transcript/video as <item type="amsTranscript" uri="…">.
_ITEM_RE = re.compile(r"<item\b[^>]*>", re.IGNORECASE)
# Shared files show up as <a itemtype=".../HyperLink/Files" href="…sharepoint.com/…">name</a>.
_A_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
_FILE_LINK = "http://schema.skype.com/HyperLink/Files"
# SharePoint "open in <app>" path prefixes: :x: Excel, :w: Word, :p: PowerPoint, :b: PDF/other doc.
# Downloadable single documents — EXCLUDES :v: (video, huge), :f: (folder), :u:/:o: (site/OneNote).
_SP_DOC_PREFIX = re.compile(r"sharepoint\.com/:[xwpb]:/", re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
_CTYPE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "text/vtt": ".vtt",
    "application/json": ".json",
    "text/plain": ".txt",
}
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
    # Meeting-recording transcript (the video item is intentionally left out — huge, and the
    # SharePoint "Play" link in the same message already preserves the recording). Two sources:
    # `amsTranscript` (fast-expiring AMS copy, skype-token) and `onedriveForBusinessTranscript`
    # (durable SharePoint copy, needs a SharePoint bearer) — capture both, dedup on download.
    for tag in _ITEM_RE.findall(content):
        attrs = _attrs(tag)
        if attrs.get("type") == "amsTranscript" and attrs.get("uri"):
            items.append({"kind": "transcript", "url": attrs["uri"]})
        elif attrs.get("type") == "onedriveForBusinessTranscript" and attrs.get("uri"):
            items.append({"kind": "sp_transcript", "url": attrs["uri"]})
    # SharePoint-hosted documents shared into the chat (xls/ppt/pdf/doc) — fetched via Graph.
    # Videos (:v:) and folders (:f:) are deliberately left out.
    for tag in _A_RE.findall(content):
        attrs = _attrs(tag)
        href = attrs.get("href") or ""
        if "sharepoint.com" not in href:
            continue
        if attrs.get("itemtype") == _FILE_LINK or _SP_DOC_PREFIX.search(href):
            items.append({"kind": "sp_file", "url": href})
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


async def _download_sp_transcript(client: httpx.AsyncClient, url: str, bearer: str, media_dir: Path) -> str:
    """Fetch a meeting transcript from SharePoint/OneDrive with a SharePoint bearer.

    Named by a hash of the URL (no stable object id like AMS); skip-if-exists on re-runs."""
    stem = f"sp-{hashlib.sha1(url.split('?')[0].encode()).hexdigest()[:16]}.transcript"
    cached = _existing(media_dir, stem)
    if cached:
        return cached
    media_dir.mkdir(parents=True, exist_ok=True)
    resp = await client.get(
        url, headers={"Authorization": f"Bearer {bearer}", "Accept": "*/*"}, follow_redirects=True
    )
    resp.raise_for_status()
    ext = _CTYPE_EXT.get((resp.headers.get("content-type") or "").split(";")[0], ".vtt")
    dest = media_dir / f"{stem}{ext}"
    tmp = media_dir / f"{stem}.part"
    tmp.write_bytes(resp.content)  # transcripts are small text
    tmp.rename(dest)
    return str(dest)


_GRAPH = "https://graph.microsoft.com/v1.0"


async def _download_sp_file(
    client: httpx.AsyncClient, url: str, graph_bearer: str, media_dir: Path
) -> str | None:
    """Fetch a SharePoint/OneDrive shared document via the Graph /shares API.

    Returns the local path, or None when the share resolves to a non-file (folder/site/page).
    Named by the driveItem's real filename; skip-if-exists on re-runs."""
    # Graph share id: unpadded base64url of the sharing URL, prefixed "u!".
    share = "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    hdr = {"Authorization": f"Bearer {graph_bearer}"}
    meta = await client.get(f"{_GRAPH}/shares/{share}/driveItem", headers=hdr)
    meta.raise_for_status()
    info = meta.json()
    if "file" not in info:  # folder / notebook / list item — not a single downloadable file
        return None
    name = _safe_name(str(info.get("name") or "file"))
    dest = media_dir / name
    if dest.exists():
        return str(dest)  # skip re-download
    media_dir.mkdir(parents=True, exist_ok=True)
    resp = await client.get(f"{_GRAPH}/shares/{share}/driveItem/content", headers=hdr, follow_redirects=True)
    resp.raise_for_status()
    tmp = media_dir / f"{name}.part"
    tmp.write_bytes(resp.content)
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
    sp_token: Callable[[str], str | None] | None = None,
    graph_token: Callable[[], str | None] | None = None,
) -> list[str]:
    """Return human annotations for the print line, downloading bytes when enabled.

    Pass `client` to reuse a shared connection pool (archive downloads thousands of attachments
    concurrently); when omitted a private client is opened for the call (live-stream path).
    `sp_token(host)` supplies a SharePoint bearer for durable OneDrive transcripts; `graph_token()`
    a Graph bearer for SharePoint-hosted shared documents. Without them those items are refs only."""
    items = extract(content, msgtype)
    notes: list[str] = []
    _remote = {"sp_transcript", "sp_file"}
    do_fetch = download and any(_downloadable(i["url"]) or i["kind"] in _remote for i in items)
    if do_fetch:
        media_dir.mkdir(parents=True, exist_ok=True)
        media_dir.chmod(0o700)

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60.0)
    try:
        for item in items:
            url, kind = item["url"], item["kind"]
            if kind == "sp_transcript":
                bearer = sp_token(urlsplit(url).hostname or "") if (download and sp_token) else None
                if not bearer:
                    notes.append(f"[transcript(sp): {url}]")
                    continue
                try:
                    path = await _download_sp_transcript(client, url, bearer, media_dir)
                    notes.append(f"[transcript: {url} → {Path(path).resolve().as_uri()}]")
                except Exception as exc:  # noqa: BLE001 — best-effort; ref-only on failure
                    log.debug("sp_transcript_failed", url=url, error=str(exc))
                    notes.append(f"[transcript(sp): {url}]")
                continue
            if kind == "sp_file":
                gtok = graph_token() if (download and graph_token) else None
                if not gtok:
                    notes.append(f"[file(sp): {url}]")
                    continue
                try:
                    fpath = await _download_sp_file(client, url, gtok, media_dir)
                    if fpath:
                        notes.append(f"[file: {url} → {Path(fpath).resolve().as_uri()}]")
                    else:
                        notes.append(f"[file(sp): {url}]")  # folder/site/page — not a file
                except Exception as exc:  # noqa: BLE001 — best-effort; ref-only on failure
                    log.debug("sp_file_failed", url=url, error=str(exc))
                    notes.append(f"[file(sp): {url}]")
                continue
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
                elif kind == "transcript":
                    # Direct /views/transcript GET, named by content-type (vtt/json/txt).
                    path = await _fetch_image(client, token, url, media_dir, suffix=".transcript")
                    notes.append(f"[transcript: {url} → {Path(path).resolve().as_uri()}]")
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
