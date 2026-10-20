"""The chat page's socket verbs (spec 011, 012): history, search, send, message actions, completion.

Each verb has a pure parser (wire dict → typed request or the refusal shown to the page) and a
`ChatVerbs` method on the typed request; `handlers()` wires both to the socket with the per-socket
busy slots. The board (`web.Board`) owns the rows and the followed chats; it lends `scope`, the
chats a socket may act on. Archive reads go through `io`: one archive pool per kind group (media
apart), with a deadline.
"""

import asyncio
import contextlib
import functools
import hashlib
import html
import json
import mimetypes
import re
import sqlite3
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal, TypeIs
from urllib.parse import quote, unquote

import httpx
import structlog
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from .archive_store import chat_label, find_chat_dir, messages_by_id, read_page, saved_files, search_chat
from .attachments import LIVE_HOST_RE, existing, live_image
from .chats import chat_kind
from .compose import MAX_TEXT, compose_html, reply_quote
from .decompose import Editable, decompose
from .directory import Directory, is_app
from .directory import humans as _humans
from .dump import epoch_seconds, fetch_history
from .emoji_table import SHORTCODES
from .emojis import EmojiCache
from .files import check_files, my_onedrive, post_files, upload_files
from .http import Downloads, failure
from .images import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    MAX_TOTAL_BYTES,
    OBJECT_ID_RE,
    check_batch,
    check_dimensions,
)
from .messages import (
    TEXT_TYPES,
    activity_line,
    fold,
    is_activity,
    is_deleted,
    is_edited,
    is_me,
    json_list,
    mention_kind,
    message_text,
    parse_activity,
    reaction_emoji,
    reaction_key,
    snippet,
)
from .render import STEM_RE, cards_html, file_links, file_urls, files_html, media_url, sanitize
from .send import (
    attach_images,
    before_images,
    delete_message,
    edit_message,
    message_html,
    react,
    send_message,
    too_long,
)

log = structlog.get_logger()

type Side = Literal["before", "after", "around"]  # which side of `at` a history page reads
type Action = Literal["react", "edit", "delete"]
type Scope = Callable[[ServerConnection | None, str], bool]  # may this socket (None: any) act on the chat?
type Latest = Callable[[str], str]  # the chat's newest message id the board saw, "" = none
type IoKind = Literal["history", "search", "index", "media", "label", "dir", "relay"]
type Handler = Callable[[ServerConnection, dict[str, Any]], Awaitable[None]]

_CHAT_PAGE = 100  # history messages per page of the chat view
_CHAT_PAGE_MAX = 200  # the archive query is cheap, the page's DOM is not
_SEARCH_HITS = 50  # search hits per answer
_MEDIA_MAX = 50 * 1024 * 1024  # ponytail: whole-body responses; stream if videos ever get linked
_LIVE_FAILED_TTL = 60.0  # seconds a failed live image is not asked again: the page retries it
_LIVE_DEADLINE = 8.0  # slot wait + fetch, under serve()'s 10 s open_timeout: a later answer is never shown
_LIVE_FAILED_MAX = 512
_LIVE_CONCURRENCY = 4
_CURSOR_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z?", re.ASCII)  # a composetime, nothing after
MSG_ID_RE = re.compile(r"[0-9]{1,20}")  # Teams message ids are ms-epoch digits
_QUOTE_MAX = 10  # quotes in one reply
ACTIONS: tuple[Action, ...] = ("react", "edit", "delete")  # page verbs on one message
SIDES: tuple[Side, ...] = ("before", "after", "around")  # `history` keys, one at most per verb
ANY_VALUE = frozenset({"preview"})  # verb keys matched on presence, not on a thread id string value
_COMPLETE_ITEMS = 12  # completion list length
_COMPLETE_Q_MAX = 64
_ROSTER_TTL = 600.0  # seconds a roster is trusted: members join and leave without a board event
_MEMBERS_SHOWN = 50  # the members popover's rows (spec 022): a big meeting's roster stays out of the frame
_ARCHIVE_WORKERS = 4
_THREAD_PREFIX = "archive"
_MEDIA_READS = 2  # media pool threads: concurrent media reads
# Reads queued per pool before io() refuses at once: a hung mount would otherwise queue every call
# (deadline-cancelled ones included) forever. Ceiling: 64 work items (~4 KB each) per pool.
_BACKLOG = 64
# Seconds an archive read may take before its caller stops waiting (the thread itself cannot be killed).
_DEADLINES: dict[IoKind, float] = {
    "history": 10.0,
    "search": 10.0,
    "index": 10.0,  # the whole index re-read for a name
    "media": 8.0,  # under serve()'s 10 s open_timeout, so /media still answers 503
    "label": 3.0,
    "dir": 3.0,
    "relay": 5.0,
}
_ACTIVITY = "_activity"  # record key: a system line's (msgtype, Activity), until named
_LOOKUP_PAGE = 20  # API messages read ending at an older message the latest page lacks
_FORMATS = ("inline", "full")
_FRAMES_WAIT = 30.0  # seconds a send waits for its next attachment frame: a slow link keeps going
_ECHO_WAIT = 15.0  # seconds an unanswered send waits for its own echo before saying it may have gone out
_UPLOAD_DRAFTS = 16  # drafts whose uploaded objects are kept for a retry (each a few ids)
# Raised once the request may have reached Teams: a blind resend could post it twice (no dedup, spec 012 5a).
_UNSURE_ERRORS = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)
UNSURE = "no answer from Teams: the message may have gone out"
CHANGED = "the message changed since it was opened: reopen it to edit"
# The page may not load the asyncgw URL (it needs the skype token): previews point at /emoji/<id>.
_ASYNCGW_SRC_RE = re.compile(
    r'src="https://[^./"]+\.asyncgw\.teams\.microsoft\.com/v1/objects/([\w-]+)/views/imgt2_anim"', re.ASCII
)


type Frames = tuple[list[bytes], list[BinaryIO]]  # a send's images, then its files spooled, in order


def stored(exc: OSError) -> str:
    return f"could not store the file: {exc.strerror or exc}"


class _Incoming:
    """A send's binary frames as they arrive: each image in one frame, then each file in chunks, written
    to an unnamed temp file under `spool` (gone once closed, or with the process, crash included)."""

    def __init__(
        self, sizes: tuple[int, ...], files: tuple[tuple[str, int], ...], spool: Path | None
    ) -> None:
        self.sizes, self.files, self.blobs = sizes, files, list[bytes]()
        self.future: asyncio.Future[Frames] = asyncio.get_running_loop().create_future()
        self.spools: list[BinaryIO] = []
        self.at = 0  # the file the next chunk belongs to
        self.seen = asyncio.get_running_loop().time()  # the last frame taken: the wait is an idle one
        if files:
            if spool is None:
                self.future.set_exception(ValueError("not connected"))
                return
            # ponytail: no per-process cap on spooled bytes: 10 x 100 MiB per socket, sockets unbounded;
            # a global budget if the cache disk ever fills
            try:
                spool.mkdir(mode=0o700, parents=True, exist_ok=True)
                for _ in files:
                    self.spools.append(tempfile.TemporaryFile(dir=spool))
            except OSError as exc:
                self.close()
                self.future.set_exception(ValueError(stored(exc)))
                return
        self._advance()

    def take(self, data: bytes) -> str | None:
        """One frame; the refusal when it is not the one expected."""
        if len(self.blobs) < len(self.sizes):
            if len(data) != self.sizes[len(self.blobs)]:
                return "image size mismatch"
            self.blobs.append(data)
        else:
            fh = self.spools[self.at]
            # The cap holds while streaming: a chunk never goes past the size the send announced.
            if not 0 < len(data) <= self.files[self.at][1] - fh.tell():
                return "file size mismatch"
            # ponytail: written in the loop, a page's 1 MiB frame at a time; to_thread if it stalls
            fh.write(data)
        self.seen = asyncio.get_running_loop().time()
        self._advance()
        return None

    def _advance(self) -> None:
        while self.at < len(self.files) and self.spools[self.at].tell() == self.files[self.at][1]:
            self.at += 1
        if len(self.blobs) == len(self.sizes) and self.at == len(self.files) and not self.future.done():
            self.future.set_result((self.blobs, self.spools))

    def close(self) -> None:
        for fh in self.spools:
            fh.close()


class ArchiveUnavailable(Exception):
    """An archive read outlived its deadline (a hung mount; its thread stays), or was refused at once:
    `_BACKLOG` reads queued, or the pool closed. The caller moves on."""


_pools: dict[str, ThreadPoolExecutor] = {}  # by thread name prefix
_closed = False
_UNAVAILABLE_EVERY = 30.0  # seconds between two `archive_unavailable` warnings
_unavailable_warned = -_UNAVAILABLE_EVERY


def archive_pool(kind: IoKind) -> ThreadPoolExecutor:
    """The process's archive pool for `kind`, made on first use; ArchiveUnavailable once closed."""
    if _closed:
        raise ArchiveUnavailable("archive pool closed")
    media = kind == "media"
    # Archive reads can hang on NFS: they must not starve the default pool the Teams calls use, nor
    # (50 MiB media bodies) the other archive reads.
    prefix = f"{_THREAD_PREFIX}-media" if media else _THREAD_PREFIX
    if prefix not in _pools:
        workers = _MEDIA_READS if media else _ARCHIVE_WORKERS
        _pools[prefix] = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix)
    return _pools[prefix]


def close_archive_pool() -> None:
    global _closed
    _closed = True
    for pool in _pools.values():
        # A thread stuck on a dead mount must not hold the shutdown.
        pool.shutdown(wait=False, cancel_futures=True)
    _pools.clear()


def stuck_archive_threads(grace: float = 1.0) -> int:
    """Archive threads (media ones too) alive `grace` seconds from now: reads hung on a dead mount."""
    until = time.monotonic() + grace
    threads = [t for t in threading.enumerate() if t.name.startswith(_THREAD_PREFIX)]
    for thread in threads:
        thread.join(max(0.0, until - time.monotonic()))
    return sum(thread.is_alive() for thread in threads)


def _warn_unavailable(kind: IoKind) -> None:
    """One warning per _UNAVAILABLE_EVERY: a dead mount fails every read, the log must not follow suit."""
    global _unavailable_warned
    now = time.monotonic()
    if now - _unavailable_warned >= _UNAVAILABLE_EVERY:
        _unavailable_warned = now
        log.warning("archive_unavailable", kind=kind)


async def io[**P, R](kind: IoKind, fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Blocking archive or NFS work on the kind's archive pool; ArchiveUnavailable past its deadline,
    or at once when `_BACKLOG` reads are queued or the pool is closed.

    A read still queued when the deadline passes is cancelled; a running one keeps its thread."""
    pool = archive_pool(kind)
    if pool._work_queue.qsize() >= _BACKLOG:
        _warn_unavailable(kind)
        raise ArchiveUnavailable(f"archive unavailable ({kind})")
    job = pool.submit(fn, *args, **kwargs)
    try:
        async with asyncio.timeout(_DEADLINES[kind]) as deadline:
            return await asyncio.wrap_future(job)
    except TimeoutError:
        if deadline.expired():
            _warn_unavailable(kind)
            raise ArchiveUnavailable(f"archive unavailable ({kind})") from None
        raise


def archive_dir(data_dir: Path | None, thread_id: str) -> Path | None:
    """The chat's archive folder; None when the archive does not hold it or cannot be read. Blocking."""
    if data_dir is None:
        return None
    try:
        return find_chat_dir(data_dir, thread_id)
    except (OSError, sqlite3.Error) as exc:
        log.info("archive_chat_lookup_failed", thread=thread_id, error=str(exc))
        return None


def index_label(data_dir: Path | None, thread_id: str) -> str:
    """The archive index's name for the chat, "" when it has none. Blocking."""
    if data_dir is None:
        return ""
    try:
        return chat_label(data_dir, thread_id)
    except (OSError, sqlite3.Error) as exc:
        log.info("archive_label_lookup_failed", thread=thread_id, error=str(exc))
        return ""


# --- records ---


def _record_type(msgtype: str) -> str:
    if msgtype in TEXT_TYPES:
        return "text"
    if msgtype == "Event/Call":
        return "call"
    return "system" if is_activity(msgtype) else "other"


def _servable(media: Path, name: str) -> Path | None:
    """The file `name` of a chat's `media` folder that `/media` serves, else None. Blocking (NFS)."""
    try:
        media = media.resolve(strict=True)
        target = (media / name).resolve(strict=True)
        # Resolved: `..`, encoded slashes and symlinks out of the folder or onto a partial download
        # all fail this one check.
        if target.parent != media or target.name.endswith(".part") or not target.is_file():
            return None
        if target.stat().st_size > _MEDIA_MAX:
            log.info("media_too_large", folder=str(media), file=name)
            return None
    except OSError, ValueError:  # ValueError: an encoded NUL in the name
        return None
    return target


def local_file(thread_id: str, media_dir: Path | None, saved: dict[str, str], urls: list[str]) -> str | None:
    """`/media/<thread>/<name>` of the file the archive saved for one of `urls` (`saved`: url → name) when
    `/media` serves it (027-AC5, AC7); never by name alone; past the cap or absent, None: the hotlink."""
    for url in urls:
        name = saved.get(url)
        if media_dir is not None and name and _servable(media_dir, name) is not None:
            return f"/media/{quote(thread_id, safe='')}/{quote(name, safe='')}"
    return None


def page_files(media_dir: Path | None, messages: list[dict[str, Any]]) -> dict[str, str]:
    """url → archived file name for the shared files of a page: one read-only query (NFS). Blocking."""
    urls = [
        url
        for message in messages
        if isinstance(props := message.get("properties"), dict)
        for url in file_urls(props.get("files"))
    ]
    if media_dir is None or not urls:
        return {}
    try:
        return saved_files(media_dir.parent, urls)
    except (OSError, sqlite3.Error) as exc:
        log.info("archive_files_lookup_failed", folder=str(media_dir.parent), error=str(exc))
        return {}


def chat_record(
    message: dict[str, Any],
    thread_id: str,
    me: str = "",
    media_dir: Path | None = None,
    custom: frozenset[str] = frozenset(),
    name_for: Callable[[str], str] | None = None,
    images: dict[str, str] | None = None,
    saved: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One message as the chat page shows it (spec 011): sanitised body, reactions, mention kind.
    `custom`: the custom emoji object ids `/emoji/<id>` serves; `images`: their reaction key → object id;
    `saved`: `page_files`."""
    props = message.get("properties")
    props = props if isinstance(props, dict) else {}
    message = {**message, "properties": props}  # mention_kind reads it too
    msgtype = str(message.get("messagetype") or "")
    content = str(message.get("content") or "")
    kind = _record_type(msgtype)
    mri = str(message.get("from") or "").rsplit("/", 1)[-1]
    deleted = is_deleted(props)
    body = ""
    activity = None if deleted or kind != "system" else parse_activity(msgtype, content)
    if activity:
        body = html.escape(activity_line(msgtype, activity, me, name_for))
    elif not deleted:
        named: set[str] = set()  # 027-AC6: the reference entries the body already links
        if msgtype == "RichText/Html":
            mentions = {
                str(m.get("itemid")): str(m.get("mri") or "")
                for m in json_list(props.get("mentions"))
                if isinstance(m, dict)
            }
            names = file_links(props.get("files"))
            body = sanitize(content, thread_id, media_dir, me, mentions, custom, names, named)
        elif msgtype == "Text":
            body = html.escape(content)
        if msgtype == "RichText/Media_Card" or props.get("cards"):
            # Appended after sanitize(), never through it: the card markup is its own allowlist.
            cards = cards_html(message, media_url=lambda u: media_url(thread_id, media_dir, u))
            body += cards
            kind = "text" if cards and kind == "other" else kind
        local = functools.partial(local_file, thread_id, media_dir, saved or {})
        files = "" if kind in ("system", "call") else files_html(props.get("files"), named, local)
        if files:  # 027-AC1: after the text, replacing the 📎 marker
            body += files
            kind = "text" if kind == "other" else kind
        body = body or html.escape(snippet(msgtype, content, props))  # file/card posts, markers
    reactions = []
    for emotion in json_list(props.get("emotions")):
        if not isinstance(emotion, dict):
            continue
        users = emotion.get("users")
        users = (
            [str(u.get("mri") or "") for u in users if isinstance(u, dict)] if isinstance(users, list) else []
        )
        if users:
            key = str(emotion.get("key") or "")
            # The emoji cache knows a custom key's image id; else its tail mostly is one.
            oid = (images or {}).get(key) or key.partition(";")[2]
            reactions.append(
                {
                    "key": key,
                    "emoji": reaction_emoji(key),
                    "count": len(users),
                    "mine": any(is_me(u, me) for u in users),
                    "people": [name_for(u) if name_for else u for u in users],
                }
                | ({"src": f"/emoji/{oid}"} if oid in custom else {})
            )
    return {
        "id": str(message.get("id") or ""),
        "at": str(message.get("composetime") or message.get("originalarrivaltime") or ""),
        "from": mri,
        "name": str(message.get("imdisplayname") or ""),
        "me": is_me(mri, me),
        "type": kind,
        "editable": msgtype in TEXT_TYPES,
        "html": body,
        "edited": is_edited(message),
        "deleted": deleted,
        "edittime": str(props.get("edittime") or ""),  # spec 019: the editor tells an edit made elsewhere
        "reactions": reactions,
        "mentions": mention_kind(message, me),
        "cmid": str(message.get("clientmessageid") or ""),  # the page swaps its queued bubble for this echo
    } | ({_ACTIVITY: (msgtype, activity)} if activity else {})


async def name_activity(records: list[dict[str, Any]], directory: Directory | None, me: str) -> None:
    """Resolve the people of the records' system lines the directory lacks, then reword the lines.
    Pops `_ACTIVITY`: it never goes on the wire."""
    pending = [(record, record.pop(_ACTIVITY)) for record in records if _ACTIVITY in record]
    if not pending or directory is None:
        return
    # resolve() skips cached MRIs (negatives too): no request once the names are known.
    people = [m for _, (_, a) in pending for m in (a.initiator, *a.targets) if m not in a.names]
    await directory.resolve([m for m in people if not is_me(m, me)])
    for record, (msgtype, activity) in pending:
        record["html"] = html.escape(activity_line(msgtype, activity, me, directory.name_for))


# --- wire parsers: the page's dict → a typed request, or the refusal it is answered ---


@dataclass(frozen=True, slots=True)
class SendRequest:
    thread: str
    text: str
    quote: tuple[str, ...] = ()
    mentions: tuple[dict[str, Any], ...] = ()  # {name, mri, everyone?}, shapes checked
    full: bool = False
    images: tuple[int, ...] = ()  # byte sizes of the binary frames that follow, in order
    cmid: str = ""  # the page's clientmessageid, kept across the retries of one draft
    check: bool = False  # a retry after an unsure send: post only if the chat lacks `cmid`
    files: tuple[tuple[str, int], ...] = ()  # (name, size), their chunks after the images, in order


@dataclass(frozen=True, slots=True)
class ActRequest:
    action: Action
    thread: str
    msg: str
    key: str = ""
    remove: bool = False
    text: str = ""
    mentions: tuple[dict[str, Any], ...] = ()  # edit: picked, like a send's
    keep_quotes: tuple[str, ...] | None = None  # edit: Reply blocks kept, by quoted id; None: an old page
    keep_images: tuple[str, ...] = ()  # edit: trailing images kept, by AMS object id


@dataclass(frozen=True, slots=True)
class HistoryRequest:
    thread: str
    side: Side = "before"
    at: str = ""  # "" = the latest page
    size: int = _CHAT_PAGE


@dataclass(frozen=True, slots=True)
class SearchRequest:
    thread: str
    query: str


@dataclass(frozen=True, slots=True)
class CompleteRequest:
    kind: Literal["emoji", "member"]
    q: str
    thread: str = ""


@dataclass(frozen=True, slots=True)
class PreviewRequest:
    text: str
    full: bool


def _text_error(text: Any) -> str | None:
    """Why `text` cannot be posted as a message, None when it can."""
    if not isinstance(text, str) or not text.strip():
        return "empty message"
    if too_long(text):
        return "message too long"
    return None


def _mention_shape(mention: Any) -> bool:
    return (
        isinstance(mention, dict)
        and isinstance(mention.get("name"), str)
        and isinstance(mention.get("mri"), str)
        and isinstance(mention.get("everyone", False), bool)
    )


def _format(value: Any) -> bool | None:
    """`format` → full mode?; None when it is neither format."""
    value = "inline" if value is None else value
    return value == "full" if value in _FORMATS else None


def _image_sizes(images: Any) -> tuple[int, ...] | str:
    """`images` ([{w, h, size}], browser-measured) → the frame sizes; the bytes are checked again."""
    if not isinstance(images, list) or not all(isinstance(i, dict) for i in images):
        return "invalid images"
    if len(images) > MAX_IMAGES:
        return f"at most {MAX_IMAGES} images per message"
    try:
        for image in images:
            check_dimensions(image.get("w"), image.get("h"))
    except ValueError as exc:
        return str(exc)
    sizes = [image.get("size") for image in images]
    if not all(type(size) is int and 0 < size for size in sizes):
        return "invalid images"
    if any(size > MAX_IMAGE_BYTES for size in sizes):
        return f"an image is over {MAX_IMAGE_BYTES >> 20} MiB"
    if sum(sizes) > MAX_TOTAL_BYTES:
        return f"images are over {MAX_TOTAL_BYTES >> 20} MiB in total"
    return tuple(sizes)


def _file_list(files: Any) -> tuple[tuple[str, int], ...] | str:
    """`files` ([{name, size}]) → (name, size) pairs within the limits; the bytes are counted again."""
    if (
        not isinstance(files, list)
        or not all(
            isinstance(f, dict) and isinstance(f.get("name"), str) and type(f.get("size")) is int
            for f in files
        )
        or any(f["size"] < 0 for f in files)
    ):
        return "invalid files"
    found = tuple((str(f["name"]), int(f["size"])) for f in files)
    try:
        check_files(list(found))
    except ValueError as exc:
        return str(exc)
    return found


def parse_send(verb: dict[str, Any]) -> SendRequest | str:
    thread, text = verb.get("send"), verb.get("text")
    if not isinstance(thread, str):
        return "unknown chat"
    images = _image_sizes([] if verb.get("images") is None else verb["images"])
    if isinstance(images, str):
        return images
    files = _file_list([] if verb.get("files") is None else verb["files"])
    if isinstance(files, str):
        return files
    # Images or files alone are a message: the text may then be empty.
    alone = (images or files) and isinstance(text, str) and not text.strip()
    if not alone and (error := _text_error(text)):
        return error
    cmid, check = verb.get("cmid", ""), verb.get("check", False)
    if not (cmid == "" or isinstance(cmid, str) and MSG_ID_RE.fullmatch(cmid)) or not isinstance(check, bool):
        return "invalid message id"
    if check and not cmid:
        return "invalid message id"
    quote = [] if verb.get("quote") is None else verb["quote"]
    if not isinstance(quote, list):
        return "invalid quote"
    if len(quote) > _QUOTE_MAX:
        return "too many quotes"
    if not all(isinstance(q, str) and MSG_ID_RE.fullmatch(q) for q in quote) or len(set(quote)) < len(quote):
        return "invalid quote"
    mentions = [] if verb.get("mentions") is None else verb["mentions"]
    if not isinstance(mentions, list) or not all(_mention_shape(m) for m in mentions):
        return "invalid mention"
    if (full := _format(verb.get("format"))) is None:
        return "invalid format"
    return SendRequest(thread, str(text), tuple(quote), tuple(mentions), full, images, cmid, check, files)


def parse_act(action: Action, verb: dict[str, Any]) -> ActRequest | str:
    thread, msg = verb.get(action), verb.get("msg")
    if not isinstance(thread, str):
        return "unknown chat"
    if not isinstance(msg, str) or not MSG_ID_RE.fullmatch(msg):
        return "invalid message"
    key, remove, text = verb.get("key"), verb.get("remove", False), verb.get("text")
    if action == "react":
        if not (isinstance(key, str) and (sent := reaction_key(key)) and isinstance(remove, bool)):
            return "invalid reaction"
        return ActRequest(action, thread, msg, key=sent, remove=remove)
    if action == "edit":
        if not any(k in verb for k in ("mentions", "keep_quotes", "keep_images")):
            if error := _text_error(text):
                return error
            return ActRequest(action, thread, msg, text=str(text))
        quotes, images = verb.get("keep_quotes"), verb.get("keep_images")
        if not _keys(quotes, MSG_ID_RE):
            return "invalid kept quote"
        if not _keys(images, OBJECT_ID_RE):
            return "invalid kept image"
        # Kept images alone are a message, as in a send.
        if not (images and isinstance(text, str) and not text.strip()) and (error := _text_error(text)):
            return error
        mentions = [] if verb.get("mentions") is None else verb["mentions"]
        if not isinstance(mentions, list) or not all(_mention_shape(m) for m in mentions):
            return "invalid mention"
        return ActRequest(
            action,
            thread,
            msg,
            text=str(text),
            mentions=tuple(mentions),
            keep_quotes=tuple(quotes),
            keep_images=tuple(images),
        )
    return ActRequest(action, thread, msg)


def _keys(value: Any, pattern: re.Pattern[str]) -> TypeIs[list[str]]:
    return (
        isinstance(value, list)
        and all(isinstance(key, str) and pattern.fullmatch(key) for key in value)
        and len(set(value)) == len(value)
    )


def parse_editable(verb: dict[str, Any]) -> tuple[str, str] | str:
    """`editable` → (thread, message id)."""
    thread, msg = verb.get("editable"), verb.get("msg")
    if not isinstance(thread, str):
        return "unknown chat"
    if not isinstance(msg, str) or not MSG_ID_RE.fullmatch(msg):
        return "invalid message"
    return thread, msg


def parse_history(verb: dict[str, Any]) -> HistoryRequest | str:
    thread, limit = verb.get("history"), verb.get("limit")
    if not isinstance(thread, str):
        return "unknown chat"
    given: list[tuple[Side, str]] = []
    for key in SIDES:
        value = verb.get(key)
        if value in (None, ""):  # "" = latest, like null
            continue
        if not (isinstance(value, str) and _CURSOR_RE.fullmatch(value)):
            return f"invalid {key}"
        given.append((key, value))
    if len(given) > 1:
        return "one cursor at most"
    side, at = given[0] if given else ("before", "")
    size = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else _CHAT_PAGE
    return HistoryRequest(thread, side, at, min(size, _CHAT_PAGE_MAX))


def parse_search(verb: dict[str, Any]) -> SearchRequest | str:
    thread, query = verb.get("search"), verb.get("q")
    if not isinstance(thread, str):
        return "unknown chat"
    if not isinstance(query, str) or not query.strip():
        return "empty query"
    return SearchRequest(thread, query.strip())


def parse_complete(verb: dict[str, Any]) -> CompleteRequest | str:
    kind, q, thread = verb.get("complete"), verb.get("q"), verb.get("thread")
    if not isinstance(q, str) or len(q) > _COMPLETE_Q_MAX or (kind == "emoji" and not q):
        return "invalid query"
    if kind == "emoji":
        return CompleteRequest("emoji", q)
    if kind != "member":
        return "invalid kind"
    if not isinstance(thread, str):
        return "unknown chat"
    return CompleteRequest("member", q, thread)


def parse_preview(verb: dict[str, Any]) -> PreviewRequest | str:
    text, full = verb.get("preview"), _format(verb.get("format"))
    if not isinstance(text, str) or full is None:
        return "invalid preview"
    if len(text) > MAX_TEXT:  # per keystroke: refused before any rendering work
        return "message too long"
    return PreviewRequest(text, full)


# --- answer heads: what each answer echoes of its verb, busy or not ---


def _q(verb: dict[str, Any]) -> str | None:
    q = verb.get("q")
    return q if isinstance(q, str) else None


def _head_send(verb: dict[str, Any]) -> dict[str, Any]:
    return {"sent": verb.get("send")}


def _head_act(action: Action, verb: dict[str, Any]) -> dict[str, Any]:
    msg = verb.get("msg")
    return {action: verb.get(action), "msg": msg if isinstance(msg, str) else None}


def _head_editable(verb: dict[str, Any]) -> dict[str, Any]:
    msg = verb.get("msg")
    return {"editable": verb.get("editable"), "msg": msg if isinstance(msg, str) else None}


def _head_history(verb: dict[str, Any]) -> dict[str, Any]:
    """The thread and the side asked for, `before: null` for the latest page."""
    key = next((k for k in SIDES if verb.get(k) not in (None, "")), "before")
    value = verb.get(key)
    return {"history": verb.get("history"), key: value if isinstance(value, str) and value else None}


def _head_search(verb: dict[str, Any]) -> dict[str, Any]:
    return {"search": verb.get("search"), "q": _q(verb)}


def _head_complete(verb: dict[str, Any]) -> dict[str, Any]:
    return {"complete": verb.get("complete"), "q": _q(verb)}


def _member_items(humans: dict[str, str], thread_id: str, me: str, q: str) -> list[dict[str, Any]]:
    """Humans but me whose full, first or last name starts with `q` (accents folded); everyone first
    on groups."""
    prefix = fold(q)
    items: list[dict[str, Any]] = []
    # As the Teams client: every chat but a 1:1 has everyone, whatever its member count.
    if chat_kind(thread_id) != "private" and "everyone".startswith(prefix):
        items.append({"name": "everyone", "mri": thread_id, "everyone": True})
    for mri, name in humans.items():
        folded = fold(name)
        if name and not is_me(mri, me) and any(p.startswith(prefix) for p in (folded, *folded.split())):
            items.append({"name": name, "mri": mri})
    return items[:_COMPLETE_ITEMS]


def _people(members: list[dict[str, Any]] | None, me: str) -> dict[str, Any]:
    """Spec 022: the roster's people, me first then by name, the first _MEMBERS_SHOWN, their count and
    the apps' (the label counts both); `people: None` without a roster."""
    if members is None:
        return {"people": None}
    humans = sorted(_humans(members).items(), key=lambda h: (not is_me(h[0], me), fold(h[1]), h[0]))
    people = [{"name": name, "mri": mri, "me": is_me(mri, me)} for mri, name in humans]
    apps = sum(1 for m in members if is_app(str(m["mri"])))
    return {"people": people[:_MEMBERS_SHOWN], "members_total": len(people), "apps": apps}


async def reply(ws: ServerConnection, frame: dict[str, Any]) -> None:
    # A page closed while its answer was computed is not a handler failure.
    with contextlib.suppress(ConnectionClosed):
        await ws.send(json.dumps(frame, ensure_ascii=False))


async def answer(ws: ServerConnection, head: dict[str, Any], work: Awaitable[dict[str, Any]]) -> None:
    """Reply what `work` answers; whatever it raises, the page still gets `head` with an error."""
    try:
        frame = await work
    except Exception as exc:  # noqa: BLE001 — the page waits for an answer whatever happens
        log.warning("verb_failed", verb=next(iter(head), ""), error_type=type(exc).__name__, exc_info=True)
        frame = {**head, "error": "failed"}
    await reply(ws, frame)


def _resolved(
    thread_id: str, members: list[dict[str, Any]], mentions: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Resolve full/first names from the roster; refuse nonmembers and unrelated labels."""
    humans = _humans(members)
    out: list[dict[str, Any]] = []
    for m in mentions:
        if m.get("everyone"):
            name = m["name"]
            ok = m["mri"] == thread_id and chat_kind(thread_id) != "private" and fold(name) == "everyone"
        else:
            name = humans.get(m["mri"], "")
            first = (name.split() or [""])[0]
            ok = bool(first) and fold(m["name"]) in (fold(name), fold(first))
            if ok and fold(m["name"]) != fold(name):
                name = first
        if not ok:
            return None
        out.append({**m, "label": name})
    return out


def _pick_key(mention: dict[str, Any]) -> tuple[str, str, bool]:
    return str(mention.get("mri")), str(mention.get("name")), bool(mention.get("everyone"))


class ChatVerbs:
    """The chat page's verbs on one board: the archive first, the chat service for what it lacks."""

    def __init__(
        self,
        directory: Directory | None,
        emojis: EmojiCache,
        scope: Scope,
        me: str = "",
        me_name: str = "",
        data_dir: Path | None = None,
        latest: Latest = lambda _: "",
    ) -> None:
        self.directory = directory
        self.emojis = emojis
        self.scope = scope
        self.latest = latest
        self.me = me
        self.me_name = me_name
        self.data_dir = data_dir
        self._replies: set[asyncio.Task[None]] = set()  # the loop keeps only weak refs to tasks
        self._history_busy: set[ServerConnection] = set()  # sockets with a history answer pending
        self._action_busy: set[ServerConnection] = set()  # sockets acting or reading a quote
        self._editable_busy: set[ServerConnection] = set()  # sockets opening an editor
        self._search_busy: set[ServerConnection] = set()  # sockets with a search pending
        # socket → the send waiting for its image and file frames
        self._incoming: dict[ServerConnection, _Incoming] = {}
        # (thread, cmid) → digest → object ids: a retry of the draft reuses what it already uploaded
        self._uploads: OrderedDict[tuple[str, str], dict[str, list[str]]] = OrderedDict()
        # (thread, cmid) being sent → its outcome: a retry must not race its own first try
        self._inflight: dict[tuple[str, str], asyncio.Future[str | None]] = {}
        # (thread, cmid) → set once its message comes back on the stream
        self._echoes: dict[tuple[str, str], asyncio.Event] = {}
        self._live: Downloads[tuple[bytes, str]] = Downloads(_LIVE_FAILED_TTL, _LIVE_FAILED_MAX)
        self._live_gate = asyncio.Semaphore(_LIVE_CONCURRENCY)  # a history page can hold dozens

    # --- socket wiring ---

    def handlers(self) -> dict[str, Handler]:
        """Verb key → socket handler, each with its busy gating; ANY_VALUE lists the keys not carrying
        a thread id."""
        acts: dict[str, Handler] = {action: functools.partial(self._on_act, action) for action in ACTIONS}
        return {
            "send": self._on_send,
            "history": self._on_history,
            "search": self._on_search,
            "complete": self._on_complete,
            "preview": self._on_preview,
            "editable": self._on_editable,
            **acts,
        }

    def _background(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._replies.add(task)
        task.add_done_callback(self._replies.discard)

    async def _one_at_a_time(
        self,
        ws: ServerConnection,
        head: dict[str, Any],
        work: Callable[[], Awaitable[dict[str, Any]]],
        slots: set[ServerConnection],
    ) -> dict[str, Any]:
        """One history page, message action, or search per socket: another while it
        runs answers `busy`."""
        if ws in slots:
            return {**head, "error": "busy"}
        slots.add(ws)
        try:
            return await work()
        finally:
            slots.discard(ws)

    async def _on_send(self, ws: ServerConnection, verb: dict[str, Any]) -> None:
        # A send can take the whole HTTP timeout: the page's other verbs must not queue behind it.
        request = parse_send(verb)
        frames = (
            self._expect(ws, request.images, request.files)
            if isinstance(request, SendRequest) and (request.images or request.files)
            else None
        )
        self._background(answer(ws, _head_send(verb), self.send_answer(ws, verb, frames)))

    async def _on_act(self, action: Action, ws: ServerConnection, verb: dict[str, Any]) -> None:
        # An HTTP round trip, like send: the page's other verbs must not queue behind it.
        acted = functools.partial(self.act_answer, ws, action, verb)
        head = _head_act(action, verb)
        self._background(answer(ws, head, self._one_at_a_time(ws, head, acted, self._action_busy)))

    async def _on_editable(self, ws: ServerConnection, verb: dict[str, Any]) -> None:
        # Reads the server's copy (maybe an API round trip); its own slot: a pending save never blocks it.
        opened = functools.partial(self.editable_answer, ws, verb)
        head = _head_editable(verb)
        self._background(answer(ws, head, self._one_at_a_time(ws, head, opened, self._editable_busy)))

    async def _on_history(self, ws: ServerConnection, verb: dict[str, Any]) -> None:
        # The API fallback is a network round trip: the page's other verbs must not wait.
        paged = functools.partial(self.history_answer, verb)
        head = _head_history(verb)
        self._background(answer(ws, head, self._one_at_a_time(ws, head, paged, self._history_busy)))

    async def _on_search(self, ws: ServerConnection, verb: dict[str, Any]) -> None:
        # Reads the whole chat in a worker thread: one search per socket.
        found = functools.partial(self.search_answer, verb)
        head = _head_search(verb)
        self._background(answer(ws, head, self._one_at_a_time(ws, head, found, self._search_busy)))

    async def _on_complete(self, ws: ServerConnection, verb: dict[str, Any]) -> None:
        # A roster fetch is a network round trip: the page's typing must not wait on it.
        self._background(answer(ws, _head_complete(verb), self.complete_answer(ws, verb)))

    async def _on_preview(self, ws: ServerConnection, verb: dict[str, Any]) -> None:
        await reply(ws, self.preview_answer(verb))

    # --- answers: parse, run, head ---

    async def send_answer(
        self,
        ws: ServerConnection | None,
        verb: dict[str, Any],
        frames: Awaitable[Frames] | None = None,
    ) -> dict[str, Any]:
        # Only the sending page learns the outcome; every page sees the echoed message.
        request = parse_send(verb)
        error = request if isinstance(request, str) else None
        blobs: list[bytes] = []
        spools: list[BinaryIO] = []
        try:
            if isinstance(request, SendRequest) and (request.images or request.files):
                try:
                    assert frames is not None  # _on_send pairs every attachment send with its frames
                    blobs, spools = await self._arrived(ws, frames)
                except TimeoutError:
                    if ws is not None:
                        self._drop_frames(ws, frames)  # partial blobs freed; a late frame finds nothing
                    error = "files did not arrive" if request.files else "images did not arrive"
                except ValueError as exc:
                    error = str(exc)
            if isinstance(request, SendRequest) and error is None:
                for fh in spools:
                    fh.seek(0)
                docs = [(name, fh, size) for (name, size), fh in zip(request.files, spools, strict=True)]
                error = await self.send(request, ws, blobs, docs)
        finally:
            for fh in spools:
                fh.close()
        if not error:
            return {**_head_send(verb), "ok": True}
        return {**_head_send(verb), "error": error, **({"unsure": True} if error == UNSURE else {})}

    # --- attachment frames: binary socket messages following their `send`, in order ---

    async def _arrived(self, ws: ServerConnection | None, frames: Awaitable[Frames]) -> Frames:
        """`frames` once all arrived; TimeoutError after _FRAMES_WAIT without a new one."""
        loop, start = asyncio.get_running_loop(), asyncio.get_running_loop().time()
        waiting = asyncio.ensure_future(frames)
        while True:
            pending = self._incoming.get(ws) if ws is not None else None
            seen = pending.seen if pending is not None and pending.future is waiting else start
            left = seen + _FRAMES_WAIT - loop.time()
            if left <= 0:
                waiting.cancel()
                raise TimeoutError
            with contextlib.suppress(TimeoutError):
                return await asyncio.wait_for(asyncio.shield(waiting), left)

    def _expect(
        self, ws: ServerConnection, sizes: tuple[int, ...], files: tuple[tuple[str, int], ...] = ()
    ) -> asyncio.Future[Frames]:
        """The frames `sizes` and `files` announce, as they arrive on `ws`; an earlier send still short of
        its own fails, so a frame never lands in the wrong message."""
        earlier = self._incoming.get(ws)
        self._fail_frames(ws, "attachments missing" if earlier and earlier.files else "images missing")
        spool = self.directory.settings.cache_dir / "uploads" if self.directory is not None else None
        pending = _Incoming(sizes, files, spool)
        if not pending.future.done():
            self._incoming[ws] = pending
        return pending.future

    def frame(self, ws: ServerConnection, data: bytes) -> None:
        """One binary message of `ws`: the next image or file chunk of its pending send."""
        pending = self._incoming.get(ws)
        if pending is None:
            log.debug("ws_frame_ignored", size=len(data))
            return
        if pending.future.done():  # its send gave up waiting (cancelled): the frame is late, not the next one
            del self._incoming[ws]
            pending.close()
            log.debug("ws_frame_ignored", size=len(data))
            return
        try:
            error = pending.take(data)
        except OSError as exc:  # a full disk fails this send, never the socket
            error = stored(exc)
        if error:
            self._fail_frames(ws, error)
        elif pending.future.done():
            del self._incoming[ws]  # its spools are the send's now

    def _fail_frames(self, ws: ServerConnection, reason: str) -> None:
        pending = self._incoming.pop(ws, None)
        if pending is not None:
            pending.close()
            if not pending.future.done():
                pending.future.set_exception(ValueError(reason))

    def _drop_frames(self, ws: ServerConnection, future: object) -> None:
        if (pending := self._incoming.get(ws)) is not None and pending.future is future:
            del self._incoming[ws]
            pending.close()

    def forget(self, ws: ServerConnection) -> None:
        """A closed socket: its send waiting for frames is dropped, nothing uploaded."""
        pending = self._incoming.pop(ws, None)
        if pending is not None:
            pending.close()
            pending.future.cancel()

    def echoed(self, thread_id: str, cmid: str) -> None:
        """A message came back on the live stream: a send unsure of its outcome learns it went out."""
        if event := self._echoes.get((thread_id, cmid)):
            event.set()

    async def act_answer(
        self, ws: ServerConnection | None, action: Action, verb: dict[str, Any]
    ) -> dict[str, Any]:
        request = parse_act(action, verb)
        error = request if isinstance(request, str) else await self.act(request, ws)
        return {**_head_act(action, verb), **({"error": error} if error else {"ok": True})}

    async def editable_answer(self, ws: ServerConnection | None, verb: dict[str, Any]) -> dict[str, Any]:
        request = parse_editable(verb)
        found = request if isinstance(request, str) else await self.editable(*request, ws=ws)
        return {**_head_editable(verb), **({"error": found} if isinstance(found, str) else found)}

    async def history_answer(self, verb: dict[str, Any]) -> dict[str, Any]:
        """The answer echoes its side (`before`, `after` or `around`) so the page can match it to its
        request."""
        head = _head_history(verb)
        request = parse_history(verb)
        if isinstance(request, str):
            return {**head, "error": request}
        try:
            page = await self.history(request)
            await name_activity(page.get("messages") or [], self.directory, self.me)
            return {**head, **page}
        except ArchiveUnavailable:
            return {**head, "error": "archive unavailable"}

    async def search_answer(self, verb: dict[str, Any]) -> dict[str, Any]:
        request = parse_search(verb)
        if isinstance(request, str):
            return {**_head_search(verb), "error": request}
        return {**_head_search(verb), **await self.search(request)}

    async def complete_answer(self, ws: ServerConnection | None, verb: dict[str, Any]) -> dict[str, Any]:
        request = parse_complete(verb)
        if isinstance(request, str):
            return {**_head_complete(verb), "error": request}
        return {**_head_complete(verb), **await self.complete(request, ws)}

    def preview_answer(self, verb: dict[str, Any]) -> dict[str, Any]:
        request = parse_preview(verb)
        if isinstance(request, str):
            return {"preview": "", "error": request}
        return self.preview(request)

    # --- verbs ---

    async def send(
        self,
        request: SendRequest,
        ws: ServerConnection | None = None,
        blobs: list[bytes] | None = None,
        docs: list[tuple[str, BinaryIO, int]] | None = None,
    ) -> str | None:
        """Post the text to the chat as me, after one Reply block per quoted id, with the mentions and
        markdown (spec 012), then `blobs` (the images, in order), with `docs` ((name, file, size)) as
        file cards (spec 025). Returns the refusal/failure reason (UNSURE when Teams may have it anyway),
        None once Teams accepted it.

        The row is left alone: the Trouter echo of the sent message updates it like any other."""
        thread_id, cmid = request.thread, request.cmid
        if not self.scope(ws, thread_id):
            return "unknown chat"
        if self.directory is None:
            return "not connected"
        key = (thread_id, cmid)
        running = self._inflight.get(key) if cmid else None
        if running is not None:
            if not request.check:
                return "still sending: try again in a moment"
            # A page resending after a reconnect: the first try's outcome is this one's, never a second post.
            # Shielded: this waiter cancelled must not cancel the outcome the first try still owes.
            return await asyncio.shield(running)
        outcome: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        if cmid:
            self._inflight[key] = (
                outcome  # before any await: a retry must not slip in during the roster fetch
            )
        error: str | None = UNSURE  # cancelled mid-send: it may have gone out
        try:
            error = await self._send(request, blobs or [], docs or [], ws)
        finally:
            if self._inflight.get(key) is outcome:
                del self._inflight[key]
            outcome.set_result(error)
        if error is None:
            self._uploads.pop(key, None)
        return error

    async def _send(
        self,
        request: SendRequest,
        blobs: list[bytes],
        docs: list[tuple[str, BinaryIO, int]],
        ws: ServerConnection | None = None,
    ) -> str | None:
        assert self.directory is not None
        thread_id, cmid = request.thread, request.cmid
        mentions = list(request.mentions)
        if mentions:
            members = await self._members(thread_id)
            if members is None:
                return "roster unavailable"
            resolved = _resolved(thread_id, members, mentions)
            if resolved is None:
                return "unknown mention"
            mentions = resolved
        settings = self.directory.settings
        names = self.emojis.names
        try:
            content, props = compose_html(
                request.text, mentions, names, full=request.full, asyncgw_host=settings.asyncgw_host
            )
        except ValueError as exc:
            return str(exc)
        text = request.text
        try:
            if request.check and await self._already_sent(thread_id, cmid):
                return None
            extra: dict[str, Any] = {}
            quoted = ""
            if request.quote:
                # Quotes cost a lookup: a message action's slot, for the lookup only (not the upload).
                if ws is not None and ws in self._action_busy:
                    return "busy"
                quote = list(request.quote)
                if ws is not None:
                    self._action_busy.add(ws)
                try:
                    copies = await self._server_copies(thread_id, quote)
                finally:
                    if ws is not None:
                        self._action_busy.discard(ws)
                if any(q not in copies for q in quote):
                    return "unknown quoted message"
                if any(
                    not copies[q].get("from") or copies[q].get("messagetype") not in TEXT_TYPES for q in quote
                ):
                    return "cannot quote this message"
                quoted = "".join(self._reply_quote(copies[q]) for q in quote)
            if blobs:
                body = before_images(content)
                files = [
                    (f"image.{image.kind}", blob)
                    for image, blob in zip(check_batch(blobs), blobs, strict=True)
                ]
                content = await asyncio.to_thread(
                    attach_images,
                    settings,
                    self.directory.skype_token,
                    thread_id,
                    quoted + body,
                    files,
                    self._draft_uploads(thread_id, cmid),
                )
            else:
                content = quoted + content
                if too_long(content, is_html=True):
                    return "message too long"
            # Plain text keeps today's call: send_message escapes it itself, to the same bytes.
            if quoted or blobs or props or content != message_html(text, False):
                text, extra["is_html"] = content, True
            if props:
                extra["properties"] = props
            if cmid:
                extra["client_message_id"] = cmid
            post: Callable[..., Any] = send_message
            if docs:
                # Last before the post: a refusal above must not cost an upload.
                try:
                    drive = await asyncio.to_thread(my_onedrive, settings)
                    entries, uploaded = await asyncio.to_thread(
                        upload_files, settings, self.directory.skype_token, drive, self.me, thread_id, docs
                    )
                except Exception as exc:  # noqa: BLE001 — nothing was posted: a plain failure, never unsure
                    log.warning("files_failed", thread=thread_id, error_type=type(exc).__name__)
                    return f"files not sent: {failure(exc)}"
                post = functools.partial(post_files, drive=drive, entries=entries, uploaded=uploaded)
            return await self._post(thread_id, cmid, text, extra, post)
        except Exception as exc:  # noqa: BLE001 — a failed send must not drop the socket
            error = failure(exc)
            log.warning("send_failed", thread=thread_id, error=error, error_type=type(exc).__name__)
            return error

    async def _post(
        self,
        thread_id: str,
        cmid: str,
        text: str,
        extra: dict[str, Any],
        post: Callable[..., Any],
    ) -> str | None:
        """`post` (send_message, or post_files with its cards); a request lost after it may have reached
        Teams waits for the message's own echo, and is never sent again from here."""
        assert self.directory is not None
        key = (thread_id, cmid)
        echo = self._echoes[key] = asyncio.Event()
        try:
            await asyncio.to_thread(
                post,
                self.directory.settings,
                self.directory.skype_token,
                thread_id,
                text,
                self.me_name,
                **extra,
            )
        except _UNSURE_ERRORS as exc:
            if cmid:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(echo.wait(), _ECHO_WAIT)
            log.warning("send_unsure", thread=thread_id, echoed=echo.is_set(), error_type=type(exc).__name__)
            return None if echo.is_set() else UNSURE
        finally:
            if self._echoes.get(key) is echo:
                del self._echoes[key]
        return None

    async def _already_sent(self, thread_id: str, cmid: str) -> bool:
        """Whether the chat's latest messages hold my `cmid` (the API, read-only: the archive may lag)."""
        assert self.directory is not None
        settings, token = self.directory.settings, self.directory.skype_token
        try:
            page = await asyncio.to_thread(fetch_history, settings, token, thread_id, _CHAT_PAGE, 1)
        except Exception as exc:
            raise RuntimeError(f"could not check whether it went out: {failure(exc)}") from exc
        found = any(
            str(m.get("clientmessageid") or "") == cmid and is_me(str(m.get("from") or ""), self.me)
            for m in page
        )
        log.info("send_checked", thread=thread_id, clientmessageid=cmid, found=found)
        return found

    def _draft_uploads(self, thread_id: str, cmid: str) -> dict[str, list[str]]:
        """The objects a previous try of this draft uploaded (digest → ids); none without a cmid."""
        if not cmid:
            return {}
        key = (thread_id, cmid)
        uploads = self._uploads.setdefault(key, {})
        self._uploads.move_to_end(key)
        while len(self._uploads) > _UPLOAD_DRAFTS:
            self._uploads.popitem(last=False)
        return uploads

    async def act(self, request: ActRequest, ws: ServerConnection | None = None) -> str | None:
        """`react`, `edit`, `delete` on one message of a chat `send` may post to."""
        thread_id, msg_id, action = request.thread, request.msg, request.action
        if not self.scope(ws, thread_id):
            return "unknown chat"
        if self.directory is None:
            return "not connected"
        settings, token = self.directory.settings, self.directory.skype_token
        try:
            # A reaction claims no authorship: Teams itself refuses one on an unknown message.
            copy = None if action == "react" else await self._own_copy(thread_id, msg_id, action == "edit")
            if isinstance(copy, str):
                return copy
            if action == "react":
                await asyncio.to_thread(
                    react, settings, token, thread_id, msg_id, request.key, remove=request.remove
                )
            elif action == "edit":
                assert copy is not None
                # Whatever the page: a message compose cannot write back is not edited from here.
                form = decompose(copy, self.emojis.names)
                if isinstance(form, str):
                    return f"cannot edit here: {form}"
                if request.keep_quotes is None:  # an old page: its text as typed, as before
                    await asyncio.to_thread(edit_message, settings, token, thread_id, msg_id, request.text)
                    return None
                edited = await self._edited(request, form)
                if isinstance(edited, str):
                    return edited
                content, props = edited
                await asyncio.to_thread(
                    edit_message, settings, token, thread_id, msg_id, content, is_html=True, properties=props
                )
            else:
                await asyncio.to_thread(delete_message, settings, token, thread_id, msg_id)
        except Exception as exc:  # noqa: BLE001 — a failed action must not drop the socket
            reason = failure(exc)
            log.warning(
                "message_action_failed",
                verb=action,
                thread=thread_id,
                message_id=msg_id,
                error=reason,
                error_type=type(exc).__name__,
            )
            return reason
        return None

    async def _own_copy(self, thread_id: str, msg_id: str, editing: bool) -> dict[str, Any] | str:
        """The server's copy of my live message `msg_id` (text, when `editing`), or the refusal."""
        copy = (await self._server_copies(thread_id, [msg_id])).get(msg_id)
        if copy is None:
            return "unknown message"
        # The server's copy decides: the page could name anyone's message.
        if not is_me(str(copy.get("from") or ""), self.me):
            return "not your message"
        props = copy.get("properties")
        if is_deleted(props if isinstance(props, dict) else {}):
            return "message deleted"
        if editing and copy.get("messagetype") not in TEXT_TYPES:
            return "cannot edit this message"
        return copy

    async def _edited(self, request: ActRequest, form: Editable) -> tuple[str, dict[str, Any] | None] | str:
        """The edited message rebuilt from the server's copy (spec 019): the kept Reply blocks, the text
        composed as a send would be, the kept images. Mentions already in the copy skip the roster."""
        assert self.directory is not None and request.keep_quotes is not None
        # Named by key, not position: an edit made elsewhere since opening must not swap what is kept.
        quoted, pictured = {q.key for q in form.quotes}, {image.key for image in form.images}
        if not quoted.issuperset(request.keep_quotes) or not pictured.issuperset(request.keep_images):
            return CHANGED
        # The copy's own pick, never the page's: a kept mention cannot bring a label of its choosing.
        kept = {_pick_key(p): p for p in form.picks}
        new = [m for m in request.mentions if _pick_key(m) not in kept]
        resolved: list[dict[str, Any]] = []
        if new:
            members = await self._members(request.thread)
            if members is None:
                return "roster unavailable"
            if (checked := _resolved(request.thread, members, new)) is None:
                return "unknown mention"
            resolved = checked
        fresh = iter(resolved)
        mentions = [kept.get(_pick_key(m)) or next(fresh) for m in request.mentions]
        settings = self.directory.settings
        try:
            content, props = compose_html(
                request.text, mentions, self.emojis.names, full=True, asyncgw_host=settings.asyncgw_host
            )
        except ValueError as exc:
            return str(exc)
        if images := "".join(image.html for image in form.images if image.key in request.keep_images):
            content = before_images(content) + f"<p>{images}</p>"
        content = "".join(q.html for q in form.quotes if q.key in request.keep_quotes) + content
        if too_long(content, is_html=True):
            return "message too long"
        return content, props

    async def editable(
        self, thread_id: str, msg_id: str, ws: ServerConnection | None = None
    ) -> dict[str, Any] | str:
        """My message as the editor opens it (spec 019): its markdown, picked mentions, reply quotes and
        trailing images, or `blocked` with what keeps it from being edited here; the refusal otherwise."""
        if not self.scope(ws, thread_id):
            return "unknown chat"
        if self.directory is None:
            return "not connected"
        try:
            copy = await self._own_copy(thread_id, msg_id, editing=True)
        except Exception as exc:  # noqa: BLE001 — a failed lookup must not drop the socket
            reason = failure(exc)
            log.warning("edit_open_failed", thread=thread_id, message_id=msg_id, error=reason)
            return reason
        if isinstance(copy, str):
            return copy
        form = decompose(copy, self.emojis.names)
        if isinstance(form, str):
            return {"blocked": form}
        srcs = [image.src for image in form.images]
        if srcs:
            with contextlib.suppress(ArchiveUnavailable):  # a hung archive: the AMS URLs
                srcs = await io("media", self._image_srcs, thread_id, form)
        props = copy.get("properties")
        props = props if isinstance(props, dict) else {}
        return {
            "ok": True,
            "text": form.text,
            "picks": form.picks,
            "quotes": [{"key": q.key, "name": q.name, "line": q.line} for q in form.quotes],
            "images": [
                {"key": image.key, "src": src, "alt": image.alt}
                for image, src in zip(form.images, srcs, strict=True)
            ],
            # The edit signal (`version` moves on reactions too): compared with the records' own.
            "edittime": str(props.get("edittime") or ""),
        }

    def _image_srcs(self, thread_id: str, form: Editable) -> list[str]:
        """Each kept image's `media_url`, else its AMS URL. Blocking: archive pool."""
        chat_dir = archive_dir(self.data_dir, thread_id)
        media = chat_dir / "media" if chat_dir is not None else None
        return [media_url(thread_id, media, image.src) or image.src for image in form.images]

    async def complete(self, request: CompleteRequest, ws: ServerConnection | None = None) -> dict[str, Any]:
        """Up to _COMPLETE_ITEMS emojis, or members of a chat, matching the prefix."""
        if request.kind == "emoji":
            return {"items": self._emoji_items(request.q.casefold())}
        if not self.scope(ws, request.thread):
            return {"error": "unknown chat"}
        members = await self._members(request.thread)
        if members is None:
            return {"error": "chat members unavailable"}
        return {"items": _member_items(_humans(members), request.thread, self.me, request.q)}

    def preview(self, request: PreviewRequest) -> dict[str, Any]:
        """The HTML `send` would post, mentions left as typed; custom emojis point at `/emoji/<id>`, so
        the asyncgw host does not matter here."""
        try:
            content, _ = compose_html(request.text, [], self.emojis.names, full=request.full)
        except ValueError as exc:
            return {"preview": "", "error": str(exc)}
        return {"preview": _ASYNCGW_SRC_RE.sub(r'src="/emoji/\1"', content)}

    async def history(self, request: HistoryRequest) -> dict[str, Any]:
        """A page of messages, archive first, else one API page."""
        thread_id, side, at, size = request.thread, request.side, request.at, request.size
        chat_dir = await io("dir", archive_dir, self.data_dir, thread_id)
        if chat_dir is None and not self.scope(None, thread_id):
            return {"error": "unknown chat"}
        resume = behind = ""
        try:
            if chat_dir is None:
                raise FileNotFoundError(thread_id)
            records, older, newer, resume = await io(
                "history", self._archive_records, chat_dir, thread_id, (side, at), size
            )
            source = "archive"
        except (OSError, sqlite3.Error) as exc:
            if not isinstance(exc, FileNotFoundError):
                log.warning(
                    "history_failed",
                    thread=thread_id,
                    source="archive",
                    error=failure(exc),
                    error_type=type(exc).__name__,
                )
            if side != "before":
                return {"error": "not available without archive"}
            if self.directory is None:
                return {"error": "not connected"}
            media = chat_dir / "media" if chat_dir else None
            try:
                kept = await asyncio.to_thread(self._api_page, thread_id, at, size)
            except Exception as api_exc:  # noqa: BLE001 — a failed page must not drop the socket
                reason = failure(api_exc)
                log.warning(
                    "history_failed",
                    thread=thread_id,
                    source="api",
                    error=reason,
                    error_type=type(api_exc).__name__,
                )
                return {"error": reason}
            records, older = await io("history", self.records, kept, thread_id, media), bool(kept)
            source, newer = "api", False
        if chat_dir and source == "archive" and not newer and (side != "before" or not at):
            # The archive's tail: the archiver may lag the stream the board row follows.
            after = at if side == "after" else ""
            records, behind = await self._catch_up(thread_id, records, after, chat_dir / "media")
        page = {"messages": records, "older": older, "newer": newer, "source": source}
        # Unreadable windows read past without a message: the page asks on from there.
        return page | ({"cursor": resume} if resume else {}) | ({"behind": behind} if behind else {})

    async def _catch_up(
        self, thread_id: str, records: list[dict[str, Any]], after: str, media: Path
    ) -> tuple[list[dict[str, Any]], str]:
        """The archive's tail plus the API's newer messages when the board saw one the archive lacks;
        the API unreachable, the tail alone and the id it lags behind (the page asks again)."""
        last = self.latest(thread_id)
        newest = max((int(r["id"]) for r in records if r["id"].isdigit()), default=0)
        if not last.isdigit() or int(last) <= newest or self.directory is None:
            return records, ""
        try:
            # ponytail: one API page; an archive lagging more than that leaves a hole above its tail
            kept = await asyncio.to_thread(self._api_page, thread_id, "", _CHAT_PAGE)
        except Exception as exc:  # noqa: BLE001 — offline: the archive's page, flagged
            log.warning(
                "history_catch_up_failed", thread=thread_id, error=failure(exc), error_type=type(exc).__name__
            )
            return records, last
        shown = {r["id"] for r in records}
        floor = max([after, *(r["at"] for r in records)])
        fresh = [
            m for m in kept if str(m.get("id") or "") not in shown and str(m.get("composetime") or "") > floor
        ]
        return records + await io("history", self.records, fresh, thread_id, media), ""

    async def search(self, request: SearchRequest) -> dict[str, Any]:
        """The thread's archived messages matching the query, newest first."""
        thread_id = request.thread
        try:
            chat_dir = await io("dir", archive_dir, self.data_dir, thread_id)
            if chat_dir is None or not await io("dir", (chat_dir / "messages.db").is_file):
                return {"error": "no archive for this chat"}
            found, _ = await io("search", search_chat, chat_dir, request.query, _SEARCH_HITS)
        except ArchiveUnavailable:
            return {"error": "archive unavailable"}
        hits = [
            {
                "id": str(m.get("id") or ""),
                "at": str(m.get("composetime") or ""),
                "name": str(m.get("imdisplayname") or ""),
                "text": message_text(m),
            }
            for m in found
        ]
        return {"hits": hits}

    # --- archive and API reads ---

    async def media(self, path: str, host: str = "") -> tuple[bytes, str] | None:
        """`media_file` on the media pool, else, with the `host` `media_url` gave, the object fetched from
        Teams; ArchiveUnavailable past the pool's deadline."""
        found = await io("media", self.media_file, path)
        thread_id, _, oid = (unquote(part) for part in path.removeprefix("/media/").partition("/"))
        token = self.directory.skype_token if self.directory is not None else ""
        if found is not None or not token or not LIVE_HOST_RE.fullmatch(host) or not STEM_RE.fullmatch(oid):
            return found
        # ponytail: scope(None, …) sees no follows: a followed chat off the board and not archived gets text.
        if not self.scope(None, thread_id) and await io("dir", archive_dir, self.data_dir, thread_id) is None:
            return None
        # A refusal under an expired token must not outlive its refresh.
        key = f"{hashlib.sha256(token.encode()).hexdigest()[:16]}/{host}/{oid}"
        return await self._live.get(key, functools.partial(self._live_image, token, host, oid))

    async def _live_image(self, token: str, host: str, oid: str) -> tuple[bytes, str] | None:
        try:
            async with asyncio.timeout(_LIVE_DEADLINE), self._live_gate, httpx.AsyncClient() as client:
                return await live_image(client, token, host, oid)
        except TimeoutError:
            log.info("live_image_failed", host=host, object_id=oid, error="timeout")
            return None

    def media_file(self, path: str) -> tuple[bytes, str] | None:
        """`/media/<thread>/<file>`: (body, content type) of an archived asset, None for anything
        else. Blocking (index lookup, NFS read): run it in a worker thread."""
        parts = path.removeprefix("/media/").split("/")
        if len(parts) != 2:
            return None
        thread_id, name = (unquote(part) for part in parts)
        chat_dir = archive_dir(self.data_dir, thread_id)
        if chat_dir is None:
            return None
        media = chat_dir / "media"
        target = _servable(media, name)
        if target is None and STEM_RE.fullmatch(name):  # a `media_url` written before the archiver had it
            with contextlib.suppress(OSError):
                found = existing(media, name)
                target = _servable(media, Path(found).name) if found else None
        if target is None:
            return None
        try:
            body = target.read_bytes()
        except OSError:
            return None
        return body, mimetypes.guess_type(target.name)[0] or "application/octet-stream"

    async def _server_copies(self, thread_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Messages `ids` of the thread as the server holds them, by id: the archive, then the API for
        those it lacks."""
        try:
            found = await io("history", self._archived_copies, thread_id, ids)
        except ArchiveUnavailable:
            found = {}  # a hung archive: the API holds the same copies
        missing = [msg_id for msg_id in ids if msg_id not in found]
        if missing and self.directory is not None:
            found |= await asyncio.to_thread(self._api_copies, thread_id, missing)
        return found

    def _archived_copies(self, thread_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        chat_dir = archive_dir(self.data_dir, thread_id)
        if chat_dir is None:
            return {}
        try:
            return messages_by_id(chat_dir, ids)
        except (OSError, sqlite3.Error) as exc:
            if not isinstance(exc, FileNotFoundError):
                log.warning("archive_message_lookup_failed", thread=thread_id, error=str(exc))
            return {}

    def _api_copies(self, thread_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        """The latest API page (sent after the archiver's last write), then the page ending at each id
        still missing (older than that page, on a chat without archive). Blocking: worker thread."""
        assert self.directory is not None
        settings, token = self.directory.settings, self.directory.skype_token
        found: dict[str, dict[str, Any]] = {}

        def keep(page: list[dict[str, Any]]) -> None:
            for message in page:
                if (msg_id := str(message.get("id") or "")) in ids:
                    found.setdefault(msg_id, message)

        keep(fetch_history(settings, token, thread_id, _CHAT_PAGE_MAX, 1))
        for msg_id in ids:
            if msg_id not in found:
                # Ids are ms epochs; endTime is second-granular and exclusive: the next second.
                keep(fetch_history(settings, token, thread_id, _LOOKUP_PAGE, 1, int(msg_id) // 1000 + 1))
        return found

    def _reply_quote(self, message: dict[str, Any]) -> str:
        mri = str(message.get("from") or "").rsplit("/", 1)[-1]
        name = str(message.get("imdisplayname") or "") or (
            self.directory.name_for(mri) if self.directory else mri
        )
        return reply_quote(message, name)

    def records(
        self, messages: list[dict[str, Any]], thread_id: str, media: Path | None
    ) -> list[dict[str, Any]]:
        """Records of a page, on the archive pool: sanitising globs the media folder (NFS)."""
        records = []
        custom, images = self.emojis.custom(), self.emojis.images()
        saved = page_files(media, messages)
        for message in messages:
            try:
                records.append(
                    chat_record(
                        message,
                        thread_id,
                        self.me,
                        media,
                        custom,
                        self.directory.name_for if self.directory else None,
                        images,
                        saved,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — one malformed message must not lose the page
                log.warning("chat_record_skipped", thread=thread_id, error_type=type(exc).__name__)
        return records

    def _archive_records(
        self, chat_dir: Path, thread_id: str, side: tuple[str, str], size: int
    ) -> tuple[list[dict[str, Any]], bool, bool, str]:
        page = read_page(chat_dir, size, **dict([side]))
        return self.records(page.messages, thread_id, chat_dir / "media"), page.older, page.newer, page.cursor

    def _api_page(self, thread_id: str, before: str, size: int) -> list[dict[str, Any]]:
        """Up to `size` messages before `before` from one API page. Blocking: worker thread.

        A short page is not the end (the API under-fills windows): only an empty one is."""
        assert self.directory is not None
        # endTime is second-granular: ask up to the next second, then cut at `before` exactly.
        end = epoch_seconds(before) + 1 if before else None
        page = fetch_history(self.directory.settings, self.directory.skype_token, thread_id, size, 1, end)
        return [m for m in page if not before or str(m.get("composetime") or "") < before][-size:]

    # --- composer completion (spec 012) ---

    async def _members(self, thread_id: str) -> list[dict[str, Any]] | None:
        """The thread's roster, at most _ROSTER_TTL old; None when it cannot be had."""
        if self.directory is None:
            return None
        info = await self.directory.thread(thread_id, max_age=_ROSTER_TTL)
        return None if info is None else list(info["members"])

    async def roster_now(self, thread_id: str) -> dict[str, Any] | None:
        """Spec 022: the roster's people from the cache (a stale copy refetched behind it); None when
        nothing is cached: the fetch must not hold the socket loop."""
        if self.directory is None:
            return {"people": None}
        if not self.directory.known(thread_id):
            return None
        return _people(await self._members(thread_id), self.me)

    def roster_later(self, ws: ServerConnection, thread_id: str, following: Callable[[], bool]) -> None:
        """Spec 022: fetch the roster off the socket loop, then push it while `following` still holds."""

        async def push() -> None:
            frame = _people(await self._members(thread_id), self.me)
            if following():  # the page moved on: its next follow asks again
                await reply(ws, {"members": thread_id, **frame})

        self._background(push())

    def _emoji_items(self, prefix: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = [
            {"name": name, "char": char} for name, char in SHORTCODES.items() if name.startswith(prefix)
        ]
        # A custom name the built-in table shadows would send the built-in character.
        keys = self.emojis.keys
        found += [
            {
                "name": name,
                "custom": oid,
                "src": f"/emoji/{oid}",
                **({"key": keys[name]} if name in keys else {}),
            }
            for name, oid in self.emojis.names.items()
            if name.casefold().startswith(prefix) and name not in SHORTCODES
        ]
        return sorted(found, key=lambda it: str(it["name"]).casefold())[:_COMPLETE_ITEMS]
