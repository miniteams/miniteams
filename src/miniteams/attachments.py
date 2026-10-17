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
import html
import re
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

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
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:  # malformed link: ref-only, the message's other attachments still go
        return False
    return bool(_OBJECT_HOST_RE.match(host))


def object_id(url: str) -> str:
    match = re.search(r"/objects/([^/]+)", url)
    return match.group(1) if match else "object"


def _safe_name(name: str) -> str:
    # Security: server-supplied filename — collapse any path components to a bare name.
    return Path(name).name or "file"


# Tenant hosts only: a bearer for `<host>` is minted and sent to exactly this host. No userinfo, port,
# uppercase, trailing dot or IDN form passes: the whole netloc is the host, for every URL parser.
_SP_HOST_RE = re.compile(r"^[a-z0-9-]+(-my)?\.sharepoint\.com$")


def _sp_host(url: str) -> str | None:
    """The SharePoint host of `url` a bearer may go to, None for any other URL.

    The only source of both the token host and the request host. Message HTML is written by any chat
    member: `https://evil.example&sol;@x.sharepoint.com/` must never get a SharePoint bearer."""
    # urlsplit drops whitespace and control characters that httpx rejects: the string checked must
    # be the string fetched.
    if url != url.strip() or not url.isprintable():
        return None
    try:
        parts = urlsplit(url)
    except ValueError:  # malformed link: ref-only, the message's other attachments still go
        return None
    return parts.netloc if parts.scheme == "https" and _SP_HOST_RE.match(parts.netloc) else None


def _is_video_url(url: str) -> bool:
    return "/:v:/" in url.lower() or bool(_VIDEO_EXT.search(url))


_TITLE_RE = re.compile(r"<Title>([^<]*)</Title>")
_ORIGNAME_RE = re.compile(r'<OriginalName v="([^"]*)"')
_DURATION_RE = re.compile(r'duration="([^"]*)"')


def recording_files(content: str, media_dir: Path) -> dict[str, Any]:
    """Manifest entry for one Media_CallRecording message: which of its video/transcript
    files exist on disk. Filenames alone can't pair them (sp-<sha1> vs file names) —
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
            # Fetched from the unescaped URI; older archives hashed the raw attribute.
            for stem in dict.fromkeys((_sp_stem(html.unescape(uri)), _sp_stem(uri))):
                transcripts += _present(f"{stem}.*")
        elif typ == "amsTranscript":
            transcripts += _present(f"{object_id(uri)}.transcript.*")
        elif typ == "amsVideo":
            videos += _present(f"{object_id(uri)}.video.*")
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


MEDIA_TYPES = ("images", "files", "transcripts", "videos")
DEFAULT_MEDIA = frozenset({"images", "files", "transcripts"})  # videos are GB-scale: opt-in


def media_type(kind: str, url: str, hint: str = "") -> str:
    """The `--media` type of one extracted item. `hint` is the `type` attribute of the tag it came
    from: a `URIObject` (`Picture.1` is a picture) or a recording `<item>` (`onedriveForBusinessVideo`),
    or `video` for a video `properties.files` entry."""
    if kind == "image" or (kind == "file" and hint.startswith("Picture.")):
        return "images"
    if kind in ("transcript", "sp_transcript"):
        return "transcripts"
    video_hint = hint in ("onedriveForBusinessVideo", "video")
    if kind == "video" or (kind == "sp_file" and (video_hint or _is_video_url(url))):
        return "videos"
    return "files"


def extract(
    content: str, msgtype: str, media: frozenset[str] = DEFAULT_MEDIA, files: list[Any] | None = None
) -> list[dict[str, str]]:
    """Pull attachment references of the selected `media` types out of message content and its decoded
    `properties.files` (spec 027: each entry but a pasted `reference` is an `sp_file`). No network.

    Videos (meeting recordings, shared video files) are GB-scale and left out of the default. The
    durable SharePoint copy rides the `sp_file` SharePoint path; the fast-expiring AMS copy (`amsVideo`)
    is emitted as a trailing `video` fallback: SharePoint blocks the file download of recordings
    owned by others ("block download" policy — streaming only), while the AMS object host still
    serves the bytes with the skype token."""
    items: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()  # a recording's :v: URL is both an <item> and the Play <a>
    # AMS fallbacks go LAST: process() walks items in order and must learn whether the
    # SharePoint copy resolved before deciding to pull the (redundant) AMS bytes.
    ams_videos: list[str] = []

    def _add(kind: str, url: str, hint: str = "", escaped: bool = True, fallback: str = "") -> None:
        if kind.startswith("sp_") and escaped:
            # Attribute values are HTML (`&amp;`): unescape once, so the URL stored, checked and
            # fetched is one string.
            url = html.unescape(url)
        if (kind, url) in seen:
            return
        # Seen even when deselected: the recording's Play <a> must not come back as a document.
        seen.add((kind, url))
        if media_type(kind, url, hint) in media:
            items.append({"kind": kind, "url": url} | ({"fallback": fallback} if fallback else {}))

    for tag in _IMG_RE.findall(content):
        attrs = _attrs(tag)
        if attrs.get("itemtype") == _AMSIMAGE and attrs.get("src"):
            _add("image", attrs["src"])
    for tag in _URIOBJ_RE.findall(content):
        attrs = _attrs(tag)
        url = attrs.get("uri") or attrs.get("url_thumbnail")
        if url:
            _add("file", url, attrs.get("type", ""))
    # Meeting-recording items. Two transcript sources: `amsTranscript` (fast-expiring AMS copy,
    # skype-token) and `onedriveForBusinessTranscript` (durable SharePoint copy, needs a
    # SharePoint bearer) — capture both, dedup on download.
    for tag in _ITEM_RE.findall(content):
        attrs = _attrs(tag)
        if attrs.get("type") == "amsTranscript" and attrs.get("uri"):
            _add("transcript", attrs["uri"])
        elif attrs.get("type") == "onedriveForBusinessTranscript" and attrs.get("uri"):
            _add("sp_transcript", attrs["uri"])
        elif attrs.get("type") == "onedriveForBusinessVideo" and attrs.get("uri"):
            # recording on OneDrive: same SharePoint path as docs
            _add("sp_file", attrs["uri"], "onedriveForBusinessVideo")
        elif attrs.get("type") == "amsVideo" and attrs.get("uri"):
            ams_videos.append(attrs["uri"])
    # SharePoint-hosted documents and videos shared into the chat — fetched from SharePoint. Folders
    # (:f:), sites and OneNote (:u:/:o:) are never single files.
    for tag in _A_RE.findall(content):
        attrs = _attrs(tag)
        href = attrs.get("href") or ""
        if "sharepoint.com" not in href:
            continue
        if _is_video_url(href) or attrs.get("itemtype") == _FILE_LINK or _SP_DOC_PREFIX.search(href):
            _add("sp_file", href)
    for entry in files or ():
        # A `reference` is a pasted body link: archived above when it is a document (027-AC6).
        if not isinstance(entry, dict) or entry.get("state") == "reference":
            continue
        info = entry.get("fileInfo")
        # The sharing link first: a file in someone else's OneDrive is often reachable only through it.
        found = (info.get("shareUrl") if isinstance(info, dict) else None, entry.get("objectUrl"))
        urls = list(dict.fromkeys(u for u in found if isinstance(u, str) and _sp_host(u)))
        # The body may link the same file already: one download (027-AC3).
        if urls and not any(("sp_file", u) in seen for u in urls):
            video = any(_is_video_url(u) for u in (*urls, str(entry.get("fileName") or "")))
            fallback = urls[1] if len(urls) > 1 else ""
            _add("sp_file", urls[0], "video" if video else "", escaped=False, fallback=fallback)
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


def existing(media_dir: Path, stem: str) -> str | None:
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
    stem = f"{object_id(url)}{suffix}"
    async with _serialized(str(media_dir / stem)):
        cached = existing(media_dir, stem)
        if cached:
            return cached  # skip re-download (archive re-runs, same object across messages)
        # Content-type is in the response headers (available before the body), so name the file
        # after a HEAD-cheap streamed GET. Write to a temp path first, rename once the ext is known.
        tmp = media_dir / f"{stem}.part"
        headers = await _stream_to(client, url, token, tmp)
        ext = _CTYPE_EXT.get((headers.get("content-type") or "").split(";")[0], ".img")
        dest = media_dir / f"{object_id(url)}{suffix}{ext}"
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
# Transcripts are text: a transcript URI any chat member wrote may point at a GB-scale file.
_SP_TRANSCRIPT_MAX = 50 * 1024 * 1024


def _sp_stem(url: str) -> str:
    return f"sp-{hashlib.sha1(url.split('?')[0].encode()).hexdigest()[:16]}.transcript"


class _AllSuppressed(Exception):
    """Every rendition is waiting out its backoff — nominal steady state, not a failure."""


class _Refused(Exception):
    """A body never kept: over the cap (413, denied for good) or of the wrong type or encoding (415,
    backed off like a 404). `status` is the HTTP code that names the reason."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status


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
    stem = _sp_stem(url)
    # Older archives hashed the URI as written in the HTML: an entity before `?` gave another stem.
    legacy = _sp_stem(html.escape(url.split("?")[0], quote=False))
    media_dir.mkdir(parents=True, exist_ok=True)
    # identity: httpx inflates a whole compressed chunk at once, so a gzip bomb would blow past
    # the cap in memory before it is counted. Uncompressed, wire bytes are the bytes counted.
    headers = {"Authorization": f"Bearer {bearer}", "Accept": "*/*", "Accept-Encoding": "identity"}
    got: list[Path] = []
    failures: list[tuple[str, Exception]] = []
    for fmt, ext, ctype in _SP_RENDITIONS:
        dest = media_dir / f"{stem}{ext}"
        async with _serialized(str(dest)):
            await _sp_rendition(
                client,
                url,
                headers,
                fmt,
                ext,
                ctype,
                dest,
                media_dir / f"{legacy}{ext}",
                got,
                failures,
                skip_url,
            )
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
    legacy: Path,
    got: list[Path],
    failures: list[tuple[str, Exception]],
    skip_url: Callable[[str], bool] | None,
) -> None:
    for kept in (dest, legacy):
        if kept.exists():
            got.append(kept)
            return
    # Per-rendition cache key: one rendition permanently gone must never suppress the other.
    if skip_url and skip_url(f"{url}#{ext}"):
        return
    tmp = dest.with_name(f"{dest.name}.part")
    try:
        async with astream_with_retry(
            client,
            url,
            params={"format": fmt} if fmt else {},
            headers=headers,
            follow_redirects=True,
        ) as resp:
            if (resp.headers.get("content-type") or "").split(";")[0] != ctype:
                # Server ignored `format`: those bytes under this extension would mislabel them.
                raise _Refused(415, f"unexpected content-type {resp.headers.get('content-type')!r}")
            if resp.headers.get("content-encoding", "identity").lower() != "identity":
                # Accept-Encoding ignored: httpx would inflate each chunk before it is counted.
                raise _Refused(415, f"unexpected content-encoding {resp.headers.get('content-encoding')!r}")
            length = resp.headers.get("content-length") or ""
            if length.isdigit() and int(length) > _SP_TRANSCRIPT_MAX:
                raise _Refused(413, "transcript over the size cap")
            size = 0
            with tmp.open("wb") as fh:
                async for chunk in resp.aiter_bytes(65536):
                    size += len(chunk)
                    if size > _SP_TRANSCRIPT_MAX:
                        raise _Refused(413, "transcript over the size cap")
                    fh.write(chunk)
        tmp.rename(dest)
        # Named speaker turns for every participant: keep them owner-only, like the token cache.
        dest.chmod(0o600)
    except Exception as exc:  # noqa: BLE001 — one rendition may 404 while the other serves
        tmp.unlink(missing_ok=True)  # never leave partial bytes a later skip would trust
        failures.append((ext, exc))
        return
    got.append(dest)


def _sp_web(path: str) -> str:
    """Site path (`/sites/x`, `/teams/x`, `/personal/x`) of a SharePoint URL path, `""` for the root
    site. Share-link prefixes (`/:x:/r/`, `/:x:/g/`, `/:x:/s/<site>/`, `/:x:/p/<user>/`) are dropped first."""
    path = re.sub(r"^/:\w:/s/", "/sites/", path)
    path = re.sub(r"^/:\w:/p/", "/personal/", path)
    path = re.sub(r"^/:\w:/[rg]/", "/", path)
    seg = path.split("/")
    return "/".join(seg[:3]) if len(seg) > 2 and seg[1] in ("sites", "teams", "personal") else ""


async def _sp_file_source(client: httpx.AsyncClient, url: str, hdr: dict[str, str]) -> tuple[str, str] | None:
    """(file name, content URL) of a SharePoint document or share link, None for a non-file.

    SharePoint REST on the file's own host, never Graph: `GetFileByUrl` takes direct paths and share
    links alike but only resolves them on the file's own site. A miss falls back to SharePoint's own
    `/_api/v2.0/shares` API on the same host, which resolves the share links `GetFileByUrl` cannot.
    The fallback's error is the one raised, so the denied-asset bookkeeping sees its status."""
    sp_host = _sp_host(url)
    if sp_host is None:
        raise ValueError("not a SharePoint tenant URL")  # process() gates first; never send the bearer
    host = f"https://{sp_host}"
    parts = urlsplit(url)
    at = quote("'" + url.replace("'", "''") + "'", safe="")
    rest = f"{host}{_sp_web(parts.path)}/_api/web/GetFileByUrl(@u)"
    try:
        meta = await aget_with_retry(
            client, f"{rest}?@u={at}", headers={**hdr, "Accept": "application/json;odata=nometadata"}
        )
        return str(meta.json().get("Name") or "file"), f"{rest}/$value?@u={at}"
    except httpx.HTTPStatusError as exc:
        log.debug("sp_rest_file_miss", url=url, status=exc.response.status_code)
    # Share id: unpadded base64url of the sharing URL, prefixed "u!".
    item = (
        f"{host}/_api/v2.0/shares/u!{base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')}/driveItem"
    )
    info = (await aget_with_retry(client, item, headers=hdr)).json()
    if "file" not in info:  # folder / notebook / list item — not a single downloadable file
        return None
    return str(info.get("name") or "file"), f"{item}/content"


async def _download_sp_file(client: httpx.AsyncClient, url: str, bearer: str, media_dir: Path) -> str | None:
    """Fetch a SharePoint/OneDrive shared document with a SharePoint bearer for its host.

    Returns the local path, or None when the share resolves to a non-file (folder/site/page).
    Named by the file's real name; skip-if-exists on re-runs."""
    hdr = {"Authorization": f"Bearer {bearer}"}
    source = await _sp_file_source(client, url, hdr)
    if source is None:
        return None
    name, content_url = source
    name = _safe_name(name)
    dest = media_dir / name
    async with _serialized(str(dest)):
        if dest.exists():
            return str(dest)  # skip re-download
        media_dir.mkdir(parents=True, exist_ok=True)
        tmp = media_dir / f"{name}.part"
        try:
            # Streamed, not buffered: shared items include meeting recordings (GBs) — a whole-body
            # `.content` read would pin RSS by the full file size (see _stream_to).
            async with astream_with_retry(client, content_url, headers=hdr, follow_redirects=True) as resp:
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


def _item_urls(item: dict[str, str]) -> list[str]:
    return [u for u in (item["url"], item.get("fallback")) if u]


async def _sp_file_or_fallback(
    client: httpx.AsyncClient,
    item: dict[str, str],
    media_dir: Path,
    download: bool,
    sp_token: Callable[[str], str | None] | None,
    skip_url: Callable[[str], bool] | None,
) -> tuple[str | None, list[tuple[str, Exception]]]:
    """(local path, []) of an `sp_file` item, trying its `fallback` URL when the first is denied, fails or is
    not a file; (None, each URL's error) when none gives a file."""
    failures: list[tuple[str, Exception]] = []
    for url in _item_urls(item):
        if url != item["url"] and skip_url and skip_url(url):
            continue  # the first URL was vetted by the caller
        sp_host = _sp_host(url)
        bearer = sp_token(sp_host) if (download and sp_token and sp_host) else None
        if not bearer:
            continue
        try:
            fpath = await _download_sp_file(client, url, bearer, media_dir)
        except Exception as exc:  # noqa: BLE001 — best-effort; the fallback, else ref-only
            log.debug("sp_file_failed", url=url, error=str(exc))
            failures.append((url, exc))
            continue
        if fpath:
            return fpath, []
    return None, failures


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
    name = _safe_name(str(info.get("original_filename") or object_id(uri)))
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
    skip_url: Callable[[str], bool] | None = None,
    on_fail: Callable[[str, Exception], None] | None = None,
    media: frozenset[str] = DEFAULT_MEDIA,
    files: list[Any] | None = None,
) -> list[str]:
    """Return human annotations for the print line, downloading bytes when enabled.

    Pass `client` to reuse a shared connection pool (archive downloads thousands of attachments
    concurrently); when omitted a private client is opened for the call (live-stream path).
    `sp_token(host)` supplies a SharePoint bearer for the file's host, for durable OneDrive
    transcripts and SharePoint-hosted shared documents. Without it those items are refs only.
    `skip_url(url)` vetoes a download (ref-only note, no network) — the archive's denied-asset
    cache; `on_fail(url, exc)` observes each failed download so callers can feed that cache.
    Only the `media` types are extracted: a deselected item is neither fetched nor annotated. `files` is the
    message's decoded `properties.files`."""
    items = extract(content, msgtype, media, files)
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
            # Per-URL keys: a body link denied under the sharing link must not mute the entry's objectUrl.
            if skip_url and all(skip_url(u) for u in _item_urls(item)):
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
                sp_host = _sp_host(url)
                bearer = sp_token(sp_host) if (download and sp_token and sp_host) else None
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
                fpath, failures = await _sp_file_or_fallback(
                    client, item, media_dir, download, sp_token, skip_url
                )
                if fpath:
                    if _is_video_url(url):
                        # ponytail: message-global flag — one SP video success mutes every AMS
                        # fallback in the message; pair by recording id if multi-recording
                        # messages ever appear (today's corpus: one recording per message).
                        sp_video_ok = True
                    notes.append(f"[file: {url} → {Path(fpath).resolve().as_uri()}]")
                else:
                    # Only when no URL gave the file: one that served must not be denied for the other.
                    for failed, error in failures:
                        if on_fail:
                            on_fail(failed, error)
                    notes.append(f"[file(sp): {url}]")  # no bearer, failed, or folder/site/page
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
