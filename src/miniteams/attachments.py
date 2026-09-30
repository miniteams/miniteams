"""Inbound attachment extraction + download (purple-teams teams_messages.c / teams_contacts.c).

Images and files are referenced inside the message `content` as `<img>` / `<URIObject>` tags
whose URL is used **verbatim** (the host is not rebuilt). Bytes are fetched from the AMS object
host with a `skypetoken_asm` **cookie** — not a Bearer/X-Skypetoken header — and only from the
whitelisted `api.asm.skype.com` / `*.asyncgw.teams.microsoft.com` hosts. Files need two hops:
`GET <uri>/views/original/status` for metadata, then GET its `view_location` for the bytes.
"""

import asyncio
import base64
import hashlib
import re
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog

from miniteams.http import aget_with_retry, astream_with_retry

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
# Videos are excluded even when tagged as a shared file (HyperLink/Files can point at an .mp4).
_VIDEO_EXT = re.compile(r"\.(mp4|mov|avi|mkv|webm|wmv|m4v)(\?|$)", re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
_CTYPE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "text/vtt": ".vtt",
    "application/json": ".json",
    "text/plain": ".txt",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
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


def _is_video_url(url: str) -> bool:
    return "/:v:/" in url.lower() or bool(_VIDEO_EXT.search(url))


_TITLE_RE = re.compile(r"<Title>([^<]*)</Title>")
_ORIGNAME_RE = re.compile(r'<OriginalName v="([^"]*)"')
_DURATION_RE = re.compile(r'duration="([^"]*)"')


def recording_files(content: str, media_dir: Path) -> dict[str, Any]:
    """Manifest entry for one Media_CallRecording message: which of its video/transcript
    files exist on disk. Filenames alone can't pair them (sp-<sha1> vs driveItem names) —
    this derives every candidate name from the message and keeps the ones present."""
    videos: list[str] = []
    transcripts: list[str] = []

    def _present(pattern: str) -> list[str]:
        return sorted(p.name for p in media_dir.glob(pattern) if not p.name.endswith(".part"))

    for tag in _ITEM_RE.findall(content):
        attrs = _attrs(tag)
        uri, typ = attrs.get("uri"), attrs.get("type")
        if not uri:
            continue
        if typ == "onedriveForBusinessTranscript":
            stem = f"sp-{hashlib.sha1(uri.split('?')[0].encode()).hexdigest()[:16]}.transcript"
            transcripts += _present(f"{stem}.*")
        elif typ == "amsTranscript":
            transcripts += _present(f"{_object_id(uri)}.transcript.*")
        elif typ == "amsVideo":
            videos += _present(f"{_object_id(uri)}.video.*")
    orig = _ORIGNAME_RE.search(content)
    if orig:
        name = _safe_name(orig.group(1))
        if (media_dir / name).exists():
            videos.insert(0, name)  # SharePoint copy first: durable, human-named
    title = _TITLE_RE.search(content)
    duration = _DURATION_RE.search(content)
    return {
        "title": title.group(1) if title else "",
        "duration": duration.group(1) if duration else "",
        "videos": videos,
        "transcripts": transcripts,
    }


def extract(content: str, msgtype: str, videos: bool = False) -> list[dict[str, str]]:
    """Pull attachment references out of message content. No network.

    `videos=True` opts in to meeting recordings and shared video files (GB-scale — excluded
    by default). The durable SharePoint copy rides the `sp_file` Graph path; the fast-expiring
    AMS copy (`amsVideo`) is emitted as a trailing `video` fallback: SharePoint blocks the
    file download of recordings owned by others ("block download" policy — streaming only),
    while the AMS object host still serves the bytes with the skype token."""
    items: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()  # a recording's :v: URL is both an <item> and the Play <a>
    # AMS fallbacks go LAST: process() walks items in order and must learn whether the
    # SharePoint copy resolved before deciding to pull the (redundant) AMS bytes.
    ams_videos: list[str] = []

    def _add(kind: str, url: str) -> None:
        if (kind, url) not in seen:
            seen.add((kind, url))
            items.append({"kind": kind, "url": url})

    for tag in _IMG_RE.findall(content):
        attrs = _attrs(tag)
        if attrs.get("itemtype") == _AMSIMAGE and attrs.get("src"):
            _add("image", attrs["src"])
    for tag in _URIOBJ_RE.findall(content):
        attrs = _attrs(tag)
        url = attrs.get("uri") or attrs.get("url_thumbnail")
        if url:
            _add("file", url)
    # Meeting-recording items. Two transcript sources: `amsTranscript` (fast-expiring AMS copy,
    # skype-token) and `onedriveForBusinessTranscript` (durable SharePoint copy, needs a
    # SharePoint bearer) — capture both, dedup on download.
    for tag in _ITEM_RE.findall(content):
        attrs = _attrs(tag)
        if attrs.get("type") == "amsTranscript" and attrs.get("uri"):
            _add("transcript", attrs["uri"])
        elif attrs.get("type") == "onedriveForBusinessTranscript" and attrs.get("uri"):
            _add("sp_transcript", attrs["uri"])
        elif videos and attrs.get("type") == "onedriveForBusinessVideo" and attrs.get("uri"):
            _add("sp_file", attrs["uri"])  # recording on OneDrive: same Graph /shares path as docs
        elif videos and attrs.get("type") == "amsVideo" and attrs.get("uri"):
            ams_videos.append(attrs["uri"])
    # SharePoint-hosted documents shared into the chat (xls/ppt/pdf/doc) — fetched via Graph.
    # Videos (:v:) and folders (:f:) are left out unless `videos` opts in.
    for tag in _A_RE.findall(content):
        attrs = _attrs(tag)
        href = attrs.get("href") or ""
        if "sharepoint.com" not in href:
            continue
        is_video = _is_video_url(href)
        if is_video and not videos:
            continue  # excluded by default (huge)
        if is_video or attrs.get("itemtype") == _FILE_LINK or _SP_DOC_PREFIX.search(href):
            _add("sp_file", href)
    for url in ams_videos:
        _add("video", url)
    return items


async def _stream_to(client: httpx.AsyncClient, url: str, token: str, dest: Path) -> httpx.Headers:
    # Stream to disk in chunks: a whole-body `.content` buffer spikes RSS by the full file size,
    # and CPython rarely returns freed arenas to the OS — so one fat attachment pins RSS for the
    # life of the (forever-running) stream. Chunked writes cap peak RAM at one chunk.
    try:
        async with astream_with_retry(
            client, url, headers={"Accept": "*/*"}, cookies={"skypetoken_asm": token}
        ) as resp:
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


# Per-destination serialization: the same asset referenced by several messages (duplicate
# recording posts, re-shared files) is downloaded concurrently by the media pass, and every
# helper writes the same deterministic `.part` path — two writers truncate/mutate each other's
# bytes and a late failure can leave a corrupt dest that skip-if-exists trusts forever. One
# lock per dest key: the winner downloads, waiters re-check skip-if-exists and return the file.
_dl_locks: dict[str, asyncio.Lock] = {}
_dl_refs: dict[str, int] = {}


@asynccontextmanager
async def _serialized(key: str) -> AsyncGenerator[None]:
    # Refcounted so entries are dropped as soon as no task holds or awaits them (bounded dict
    # in a forever-running process). Single event loop: the counter updates are atomic.
    _dl_refs[key] = _dl_refs.get(key, 0) + 1
    lock = _dl_locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            yield
    finally:
        remaining = _dl_refs[key] - 1
        if remaining:
            _dl_refs[key] = remaining
        else:
            del _dl_refs[key], _dl_locks[key]


async def fetch_image(client: httpx.AsyncClient, token: str, url: str, media_dir: Path, suffix: str) -> str:
    stem = f"{_object_id(url)}{suffix}"
    async with _serialized(str(media_dir / stem)):
        cached = _existing(media_dir, stem)
        if cached:
            return cached  # skip re-download (archive re-runs, same object across messages)
        # Content-type is in the response headers (available before the body), so name the file
        # after a HEAD-cheap streamed GET. Write to a temp path first, rename once the ext is known.
        tmp = media_dir / f"{stem}.part"
        headers = await _stream_to(client, url, token, tmp)
        ext = _CTYPE_EXT.get((headers.get("content-type") or "").split(";")[0], ".img")
        dest = media_dir / f"{_object_id(url)}{suffix}{ext}"
        tmp.rename(dest)
        if suffix == ".video":
            # info, not debug — videos are rare and huge; one line per FRESH download only
            # (the cached early-return above never reaches here, so loop re-runs stay quiet).
            log.info("video_downloaded", source="ams", path=str(dest), bytes=dest.stat().st_size)
        return str(dest)


# Both renditions of the same SharePoint transcript, neither derivable from the other: the JSON
# carries the speakers (`speakerDisplayName`/`speakerId`), the default VTT splits each turn into
# sub-cues with their own timings (~2.5x the entry count). Order matters — the JSON is the primary,
# returned for the message annotation.
_SP_RENDITIONS = (("json", ".json", "application/json"), ("", ".vtt", "text/vtt"))


class _AllSuppressed(Exception):
    """Every rendition is waiting out its backoff — nominal steady state, not a failure."""


async def _download_sp_transcript(
    client: httpx.AsyncClient,
    url: str,
    bearer: str,
    media_dir: Path,
    on_fail: Callable[[str, Exception], None] | None = None,
    skip_url: Callable[[str], bool] | None = None,
) -> str:
    """Fetch a meeting transcript from SharePoint/OneDrive with a SharePoint bearer.

    Named by a hash of the URL (no stable object id like AMS); skip-if-exists per rendition, so an
    archive holding only the older untagged VTT picks up the JSON on the next run without losing it
    (for a since-deleted recording that VTT is the only copy left). Raises only when *both* fail —
    a rendition that fails on its own is still reported through `on_fail`, or a permanently denied
    one is silently re-requested on every archive pass forever."""
    stem = f"sp-{hashlib.sha1(url.split('?')[0].encode()).hexdigest()[:16]}.transcript"
    media_dir.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {bearer}", "Accept": "*/*"}
    got: list[Path] = []
    failures: list[tuple[str, Exception]] = []
    for fmt, ext, ctype in _SP_RENDITIONS:
        dest = media_dir / f"{stem}{ext}"
        async with _serialized(str(dest)):
            await _sp_rendition(client, url, headers, fmt, ext, ctype, dest, got, failures, skip_url)
    # Report before raising: a total failure must still record BOTH renditions under their own key,
    # or the retry policy never sees them and every pass re-requests a dead URL.
    for ext, failure in failures:
        log.debug("sp_transcript_rendition_failed", url=url, rendition=ext, error=str(failure))
        if on_fail:
            on_fail(f"{url}#{ext}", failure)
    if not got:
        if not failures:
            raise _AllSuppressed(url)
        raise failures[0][1]  # the original error, so the caller can read its HTTP status
    return str(got[0])


async def _sp_rendition(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    fmt: str,
    ext: str,
    ctype: str,
    dest: Path,
    got: list[Path],
    failures: list[tuple[str, Exception]],
    skip_url: Callable[[str], bool] | None,
) -> None:
    if dest.exists():
        got.append(dest)
        return
    # Per-rendition cache key: one rendition permanently gone must never suppress the other.
    if skip_url and skip_url(f"{url}#{ext}"):
        return
    tmp = dest.with_name(f"{dest.name}.part")
    try:
        resp = await aget_with_retry(
            client,
            url,
            params={"format": fmt} if fmt else {},
            headers=headers,
            follow_redirects=True,
        )
        if (resp.headers.get("content-type") or "").split(";")[0] != ctype:
            # Server ignored `format`: those bytes under this extension would mislabel them.
            raise RuntimeError(f"unexpected content-type {resp.headers.get('content-type')!r}")
        tmp.write_bytes(resp.content)  # transcripts are small text
        tmp.rename(dest)
        # Named speaker turns for every participant: keep them owner-only, like the token cache.
        dest.chmod(0o600)
    except Exception as exc:  # noqa: BLE001 — one rendition may 404 while the other serves
        tmp.unlink(missing_ok=True)  # never leave partial bytes a later skip would trust
        failures.append((ext, exc))
        return
    got.append(dest)


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
    meta = await aget_with_retry(client, f"{_GRAPH}/shares/{share}/driveItem", headers=hdr)
    info = meta.json()
    if "file" not in info:  # folder / notebook / list item — not a single downloadable file
        return None
    name = _safe_name(str(info.get("name") or "file"))
    dest = media_dir / name
    async with _serialized(str(dest)):
        if dest.exists():
            return str(dest)  # skip re-download
        media_dir.mkdir(parents=True, exist_ok=True)
        tmp = media_dir / f"{name}.part"
        try:
            # Streamed, not buffered: shared items include meeting recordings (GBs) — a whole-body
            # `.content` read would pin RSS by the full file size (see _stream_to).
            async with astream_with_retry(
                client, f"{_GRAPH}/shares/{share}/driveItem/content", headers=hdr, follow_redirects=True
            ) as resp:
                with tmp.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(65536):
                        fh.write(chunk)
            tmp.rename(dest)
        except BaseException:
            tmp.unlink(missing_ok=True)  # truncated file would look complete to a later skip-if-exists
            raise
        if _is_video_url(url):
            # One line per fresh video download (cached early-return above stays quiet).
            log.info("video_downloaded", source="sharepoint", path=str(dest), bytes=dest.stat().st_size)
        return str(dest)


async def _download_image(
    client: httpx.AsyncClient,
    token: str,
    src: str,
    media_dir: Path,
    skip_url: Callable[[str], bool] | None = None,
    on_fail: Callable[[str, Exception], None] | None = None,
) -> tuple[str, str | None]:
    """Fetch the optimized view (as referenced) and the full-resolution view; return (optim, full)."""
    optim = await fetch_image(client, token, src, media_dir, suffix="")
    full_url = _VIEW_RE.sub(f"/views/{_FULL_VIEW}", src)
    # The full view has its own URL: a denied one must be skipped/recorded on that URL, or a
    # message whose optimized view is fine would re-poll a 403 full view on every run.
    if full_url == src or (skip_url and skip_url(full_url)):
        return optim, None
    try:
        full = await fetch_image(client, token, full_url, media_dir, suffix=".full")
    except Exception as exc:  # noqa: BLE001 — full view may 404; the optimized one still stands
        log.debug("full_image_failed", url=full_url, error=str(exc))
        if on_fail:
            on_fail(full_url, exc)
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
    async with _serialized(str(dest)):
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
    skip_url: Callable[[str], bool] | None = None,
    on_fail: Callable[[str, Exception], None] | None = None,
    videos: bool = False,
) -> list[str]:
    """Return human annotations for the print line, downloading bytes when enabled.

    Pass `client` to reuse a shared connection pool (archive downloads thousands of attachments
    concurrently); when omitted a private client is opened for the call (live-stream path).
    `sp_token(host)` supplies a SharePoint bearer for durable OneDrive transcripts; `graph_token()`
    a Graph bearer for SharePoint-hosted shared documents. Without them those items are refs only.
    `skip_url(url)` vetoes a download (ref-only note, no network) — the archive's denied-asset
    cache; `on_fail(url, exc)` observes each failed download so callers can feed that cache."""
    items = extract(content, msgtype, videos=videos)
    notes: list[str] = []
    _remote = {"sp_transcript", "sp_file"}
    _ref_label = {"sp_transcript": "transcript(sp)", "sp_file": "file(sp)"}
    do_fetch = download and any(_downloadable(i["url"]) or i["kind"] in _remote for i in items)
    if do_fetch:
        media_dir.mkdir(parents=True, exist_ok=True)
        media_dir.chmod(0o700)

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60.0)
    sp_video_ok = False  # durable copy secured → the trailing AMS `video` fallback is redundant
    try:
        for item in items:
            url, kind = item["url"], item["kind"]
            if skip_url and skip_url(url):
                notes.append(f"[{_ref_label.get(kind, kind)}: {url}]")
                continue
            if kind == "video":
                # AMS recording copy: fallback only — SharePoint "block download" recordings
                # (owned by others) stream-only there, but the AMS object host still serves them.
                if sp_video_ok or not (download and _downloadable(url)):
                    notes.append(f"[video: {url}]")
                    continue
                try:
                    path = await fetch_image(client, token, url, media_dir, suffix=".video")
                    notes.append(f"[video: {url} → {Path(path).resolve().as_uri()}]")
                except Exception as exc:  # noqa: BLE001 — best-effort; ref-only on failure
                    log.debug("ams_video_failed", url=url, error=str(exc))
                    if on_fail:
                        on_fail(url, exc)
                    notes.append(f"[video: {url}]")
                continue
            if kind == "sp_transcript":
                bearer = sp_token(urlsplit(url).hostname or "") if (download and sp_token) else None
                if not bearer:
                    notes.append(f"[transcript(sp): {url}]")
                    continue
                try:
                    path = await _download_sp_transcript(client, url, bearer, media_dir, on_fail, skip_url)
                    notes.append(f"[transcript: {url} → {Path(path).resolve().as_uri()}]")
                except _AllSuppressed:
                    notes.append(f"[transcript(sp): {url}]")  # backing off: nominal, not a failure
                except Exception as exc:  # noqa: BLE001 — best-effort; ref-only on failure
                    # No on_fail here: the helper already reported each rendition under its own key.
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
                        if _is_video_url(url):
                            # ponytail: message-global flag — one SP video success mutes every AMS
                            # fallback in the message; pair by recording id if multi-recording
                            # messages ever appear (today's corpus: one recording per message).
                            sp_video_ok = True
                        notes.append(f"[file: {url} → {Path(fpath).resolve().as_uri()}]")
                    else:
                        notes.append(f"[file(sp): {url}]")  # folder/site/page — not a file
                except Exception as exc:  # noqa: BLE001 — best-effort; ref-only on failure
                    log.debug("sp_file_failed", url=url, error=str(exc))
                    if on_fail:
                        on_fail(url, exc)
                    notes.append(f"[file(sp): {url}]")
                continue
            if not (download and _downloadable(url)):
                notes.append(f"[{kind}: {url}]")
                continue
            try:
                if kind == "image":
                    optim_path, full_path = await _download_image(
                        client, token, url, media_dir, skip_url=skip_url, on_fail=on_fail
                    )
                    # Show the local file:// (full-res when available) next to the original URL.
                    # resolve(): as_uri() rejects relative paths (a relative media_dir would 500).
                    primary = Path(full_path or optim_path).resolve().as_uri()
                    extra = f" (optim {Path(optim_path).resolve().as_uri()})" if full_path else ""
                    notes.append(f"[image: {url} → {primary}{extra}]")
                elif kind == "transcript":
                    # Direct /views/transcript GET, named by content-type (vtt/json/txt).
                    path = await fetch_image(client, token, url, media_dir, suffix=".transcript")
                    notes.append(f"[transcript: {url} → {Path(path).resolve().as_uri()}]")
                else:
                    fpath, name, size = await _download_file(client, token, url, media_dir)
                    sz = f" ({size}B)" if size else ""
                    notes.append(f"[file: {name}{sz} → {fpath}]" if fpath else f"[file: {name}{sz} {url}]")
            except Exception as exc:  # noqa: BLE001 — a failed download must not drop the message
                log.debug("attachment_download_failed", kind=kind, url=url, error=str(exc))
                if on_fail:
                    on_fail(url, exc)
                notes.append(f"[{kind}: {url}]")
    finally:
        if owns_client:
            await client.aclose()

    if msgtype == "RichText/Media_Card":
        notes.append("[card]")
    return notes
