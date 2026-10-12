"""The chat page's socket verbs (spec 011, 012): history, search, send, message actions, completion.

Each verb has a pure parser (wire dict → typed request or the refusal shown to the page) and a
`ChatVerbs` method on the typed request; `handlers()` wires both to the socket with the per-socket
busy slots. The board (`web.Board`) owns the rows and the followed chats; it lends `scope`, the
chats a socket may act on. Archive reads go through `io`: the archive pool, with a deadline.
"""

import asyncio
import contextlib
import functools
import html
import json
import mimetypes
import re
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote

import structlog
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from .archive_store import chat_label, find_chat_dir, messages_by_id, read_page, search_chat
from .chats import chat_kind
from .compose import MAX_TEXT, compose_html, reply_quote
from .directory import Directory, is_app
from .dump import epoch_seconds, fetch_history
from .emoji_table import SHORTCODES
from .emojis import EmojiCache
from .http import failure
from .messages import (
    EMOJI,
    TEXT_TYPES,
    fold,
    is_activity,
    is_deleted,
    is_edited,
    is_me,
    json_list,
    mention_kind,
    message_text,
    snippet,
)
from .render import cards_html, media_url, sanitize
from .send import delete_message, edit_message, message_html, react, send_message, too_long

log = structlog.get_logger()

type Side = Literal["before", "after", "around"]  # which side of `at` a history page reads
type Action = Literal["react", "edit", "delete"]
type Scope = Callable[[ServerConnection | None, str], bool]  # may this socket (None: any) act on the chat?
type IoKind = Literal["history", "search", "index", "media", "label", "dir", "relay"]
type Handler = Callable[[ServerConnection, dict[str, Any]], Awaitable[None]]

_CHAT_PAGE = 100  # history messages per page of the chat view
_CHAT_PAGE_MAX = 200  # the archive query is cheap, the page's DOM is not
_SEARCH_HITS = 50  # search hits per answer
_MEDIA_MAX = 50 * 1024 * 1024  # ponytail: whole-body responses; stream if videos ever get linked
_CURSOR_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z?", re.ASCII)  # a composetime, nothing after
MSG_ID_RE = re.compile(r"[0-9]{1,20}")  # Teams message ids are ms-epoch digits
_QUOTE_MAX = 10  # quotes in one reply
ACTIONS: tuple[Action, ...] = ("react", "edit", "delete")  # page verbs on one message
SIDES: tuple[Side, ...] = ("before", "after", "around")  # `history` keys, one at most per verb
ANY_VALUE = frozenset({"preview"})  # verb keys matched on presence, not on a thread id string value
_COMPLETE_ITEMS = 12  # completion list length
_COMPLETE_Q_MAX = 64
_ROSTER_TTL = 600.0  # seconds a roster is trusted: members join and leave without a board event
_ARCHIVE_WORKERS = 4
_THREAD_PREFIX = "archive"
_MEDIA_READS = 2  # concurrent media reads: 50 MiB bodies must leave pool threads for the rest
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
_LOOKUP_PAGE = 20  # API messages read ending at an older message the latest page lacks
_FORMATS = ("inline", "full")
# The page may not load the asyncgw URL (it needs the skype token): previews point at /emoji/<id>.
_ASYNCGW_SRC_RE = re.compile(
    r'src="https://[^./"]+\.asyncgw\.teams\.microsoft\.com/v1/objects/([\w-]+)/views/imgt2_anim"', re.ASCII
)


class ArchiveUnavailable(Exception):
    """An archive read outlived its deadline: a hung mount. Its thread stays; the caller moves on."""


_pool: ThreadPoolExecutor | None = None
_closed = False
_busy = 0  # archive threads running a read, hung ones included
_busy_lock = threading.Lock()
_UNAVAILABLE_EVERY = 30.0  # seconds between two `archive_unavailable` warnings
_unavailable_warned = -_UNAVAILABLE_EVERY


def archive_pool() -> ThreadPoolExecutor:
    """The process's archive pool, made on first use; ArchiveUnavailable once closed."""
    global _pool
    if _closed:
        raise ArchiveUnavailable("archive pool closed")
    if _pool is None:
        # Archive reads can hang on NFS: they must not starve the default pool the Teams calls use.
        _pool = ThreadPoolExecutor(max_workers=_ARCHIVE_WORKERS, thread_name_prefix=_THREAD_PREFIX)
    return _pool


def close_archive_pool() -> None:
    global _pool, _closed
    _closed = True
    if _pool is not None:
        # A thread stuck on a dead mount must not hold the shutdown.
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None


def stuck_archive_threads(grace: float = 1.0) -> int:
    """Archive threads still alive `grace` seconds from now: reads hung on a dead mount."""
    until = time.monotonic() + grace
    threads = [t for t in threading.enumerate() if t.name.startswith(_THREAD_PREFIX)]
    for thread in threads:
        thread.join(max(0.0, until - time.monotonic()))
    return sum(thread.is_alive() for thread in threads)


def _counted[R](work: Callable[[], R]) -> R:
    global _busy
    with _busy_lock:
        _busy += 1
    try:
        return work()
    finally:
        with _busy_lock:
            _busy -= 1


def _warn_unavailable(kind: IoKind) -> None:
    """One warning per _UNAVAILABLE_EVERY: a dead mount fails every read, the log must not follow suit."""
    global _unavailable_warned
    now = time.monotonic()
    if now - _unavailable_warned >= _UNAVAILABLE_EVERY:
        _unavailable_warned = now
        log.warning("archive_unavailable", kind=kind, busy_workers=_busy)


async def io[**P, R](kind: IoKind, fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Blocking archive or NFS work on the archive pool; ArchiveUnavailable past the kind's deadline."""
    return await run_io(kind, functools.partial(fn, *args, **kwargs))


async def run_io[R](kind: IoKind, work: Callable[[], R], finished: Callable[[], None] | None = None) -> R:
    """`io` for a prepared call; `finished` runs on the loop once its thread is really done (or the
    call was cancelled before it started), however long after the deadline that is.

    A read still queued when the deadline passes is cancelled; a running one keeps its thread."""
    loop = asyncio.get_running_loop()
    try:
        job = archive_pool().submit(_counted, work)
    except BaseException:
        if finished is not None:
            finished()  # no thread to wait for
        raise
    if finished is not None:

        def done(_: object) -> None:
            with contextlib.suppress(RuntimeError):  # the loop closed while a hung read went on
                loop.call_soon_threadsafe(finished)

        job.add_done_callback(done)
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


def chat_record(
    message: dict[str, Any],
    thread_id: str,
    me: str = "",
    media_dir: Path | None = None,
    custom: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """One message as the chat page shows it (spec 011): sanitised body, reactions, mention kind.
    `custom`: the custom emoji object ids `/emoji/<id>` serves."""
    props = message.get("properties")
    props = props if isinstance(props, dict) else {}
    message = {**message, "properties": props}  # mention_kind reads it too
    msgtype = str(message.get("messagetype") or "")
    content = str(message.get("content") or "")
    kind = _record_type(msgtype)
    mri = str(message.get("from") or "").rsplit("/", 1)[-1]
    deleted = is_deleted(props)
    body = ""
    if not deleted:
        if msgtype == "RichText/Html":
            mentions = {
                str(m.get("itemid")): str(m.get("mri") or "")
                for m in json_list(props.get("mentions"))
                if isinstance(m, dict)
            }
            body = sanitize(content, thread_id, media_dir, me, mentions, custom)
        elif msgtype == "Text":
            body = html.escape(content)
        if msgtype == "RichText/Media_Card" or props.get("cards"):
            # Appended after sanitize(), never through it: the card markup is its own allowlist.
            cards = cards_html(message, media_url=lambda u: media_url(thread_id, media_dir, u))
            body += cards
            kind = "text" if cards and kind == "other" else kind
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
            reactions.append(
                {
                    "key": key,
                    "emoji": EMOJI.get(key, f":{key}:"),
                    "count": len(users),
                    "mine": any(is_me(u, me) for u in users),
                }
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
        "reactions": reactions,
        "mentions": mention_kind(message, me),
    }


# --- wire parsers: the page's dict → a typed request, or the refusal it is answered ---


@dataclass(frozen=True, slots=True)
class SendRequest:
    thread: str
    text: str
    quote: tuple[str, ...] = ()
    mentions: tuple[dict[str, Any], ...] = ()  # {name, mri, everyone?}, shapes checked
    full: bool = False


@dataclass(frozen=True, slots=True)
class ActRequest:
    action: Action
    thread: str
    msg: str
    key: str = ""
    remove: bool = False
    text: str = ""


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


def parse_send(verb: dict[str, Any]) -> SendRequest | str:
    thread, text = verb.get("send"), verb.get("text")
    if not isinstance(thread, str):
        return "unknown chat"
    if error := _text_error(text):
        return error
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
    return SendRequest(thread, str(text), tuple(quote), tuple(mentions), full)


def parse_act(action: Action, verb: dict[str, Any]) -> ActRequest | str:
    thread, msg = verb.get(action), verb.get("msg")
    if not isinstance(thread, str):
        return "unknown chat"
    if not isinstance(msg, str) or not MSG_ID_RE.fullmatch(msg):
        return "invalid message"
    key, remove, text = verb.get("key"), verb.get("remove", False), verb.get("text")
    if action == "react":
        if not (isinstance(key, str) and key in EMOJI and isinstance(remove, bool)):
            return "invalid reaction"
        return ActRequest(action, thread, msg, key=key, remove=remove)
    if action == "edit":
        if error := _text_error(text):
            return error
        return ActRequest(action, thread, msg, text=str(text))
    return ActRequest(action, thread, msg)


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


def _head_history(verb: dict[str, Any]) -> dict[str, Any]:
    """The thread and the side asked for, `before: null` for the latest page."""
    key = next((k for k in SIDES if verb.get(k) not in (None, "")), "before")
    value = verb.get(key)
    return {"history": verb.get("history"), key: value if isinstance(value, str) and value else None}


def _head_search(verb: dict[str, Any]) -> dict[str, Any]:
    return {"search": verb.get("search"), "q": _q(verb)}


def _head_complete(verb: dict[str, Any]) -> dict[str, Any]:
    return {"complete": verb.get("complete"), "q": _q(verb)}


def _humans(members: list[dict[str, Any]]) -> dict[str, str]:
    """Roster → {mri: display name} of its people; bots and apps left out."""
    return {str(m["mri"]): str(m["name"] or "") for m in members if not is_app(str(m["mri"]))}


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
    """The mentions with the roster's name as `label`; None when one is not a human of the thread
    (or everyone of a group), or its typed name is not that member's."""
    humans = _humans(members)
    out: list[dict[str, Any]] = []
    for m in mentions:
        if m.get("everyone"):
            name = m["name"]
            ok = m["mri"] == thread_id and chat_kind(thread_id) != "private" and fold(name) == "everyone"
        else:
            name = humans.get(m["mri"], "")
            ok = bool(name) and fold(name) == fold(m["name"])
        if not ok:
            return None
        out.append({**m, "label": name})
    return out


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
    ) -> None:
        self.directory = directory
        self.emojis = emojis
        self.scope = scope
        self.me = me
        self.me_name = me_name
        self.data_dir = data_dir
        self._replies: set[asyncio.Task[None]] = set()  # the loop keeps only weak refs to tasks
        self._history_busy: set[ServerConnection] = set()  # sockets with a history answer pending
        self._action_busy: set[ServerConnection] = set()  # sockets with a message action pending
        self._search_busy: set[ServerConnection] = set()  # sockets with a search pending
        self._media_reads = 0  # media reads whose thread is still running, past their deadline too

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
        """One history page, message action (or quoted send), or search per socket: another while it
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
        sent = functools.partial(self.send_answer, ws, verb)
        # Quotes cost a lookup: they count as a message action.
        head = _head_send(verb)
        if verb.get("quote"):
            self._background(answer(ws, head, self._one_at_a_time(ws, head, sent, self._action_busy)))
        else:
            self._background(answer(ws, head, sent()))

    async def _on_act(self, action: Action, ws: ServerConnection, verb: dict[str, Any]) -> None:
        # An HTTP round trip, like send: the page's other verbs must not queue behind it.
        acted = functools.partial(self.act_answer, ws, action, verb)
        head = _head_act(action, verb)
        self._background(answer(ws, head, self._one_at_a_time(ws, head, acted, self._action_busy)))

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

    async def send_answer(self, ws: ServerConnection | None, verb: dict[str, Any]) -> dict[str, Any]:
        # Only the sending page learns the outcome; every page sees the echoed message.
        request = parse_send(verb)
        error = request if isinstance(request, str) else await self.send(request, ws)
        return {**_head_send(verb), **({"error": error} if error else {"ok": True})}

    async def act_answer(
        self, ws: ServerConnection | None, action: Action, verb: dict[str, Any]
    ) -> dict[str, Any]:
        request = parse_act(action, verb)
        error = request if isinstance(request, str) else await self.act(request, ws)
        return {**_head_act(action, verb), **({"error": error} if error else {"ok": True})}

    async def history_answer(self, verb: dict[str, Any]) -> dict[str, Any]:
        """The answer echoes its side (`before`, `after` or `around`) so the page can match it to its
        request."""
        head = _head_history(verb)
        request = parse_history(verb)
        if isinstance(request, str):
            return {**head, "error": request}
        try:
            return {**head, **await self.history(request)}
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

    async def send(self, request: SendRequest, ws: ServerConnection | None = None) -> str | None:
        """Post the text to the chat as me, after one Reply block per quoted id, with the mentions and
        markdown (spec 012). Returns the refusal/failure reason, None once Teams accepted it.

        The row is left alone: the Trouter echo of the sent message updates it like any other."""
        thread_id = request.thread
        if not self.scope(ws, thread_id):
            return "unknown chat"
        if self.directory is None:
            return "not connected"
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
            extra: dict[str, Any] = {}
            if request.quote:
                quote = list(request.quote)
                copies = await self._server_copies(thread_id, quote)
                if any(q not in copies for q in quote):
                    return "unknown quoted message"
                if any(
                    not copies[q].get("from") or copies[q].get("messagetype") not in TEXT_TYPES for q in quote
                ):
                    return "cannot quote this message"
                content = "".join(self._reply_quote(copies[q]) for q in quote) + content
            if too_long(content, is_html=True):
                return "message too long"
            # Plain text keeps today's call: send_message escapes it itself, to the same bytes.
            if request.quote or props or content != message_html(text, False):
                text, extra["is_html"] = content, True
            if props:
                extra["properties"] = props
            await asyncio.to_thread(
                send_message, settings, self.directory.skype_token, thread_id, text, self.me_name, **extra
            )
        except Exception as exc:  # noqa: BLE001 — a failed send must not drop the socket
            reason = failure(exc)
            log.warning("send_failed", thread=thread_id, error=reason, error_type=type(exc).__name__)
            return reason
        return None

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
            if action != "react":
                copy = (await self._server_copies(thread_id, [msg_id])).get(msg_id)
                if copy is None:
                    return "unknown message"
                # The server's copy decides: the page could name anyone's message.
                if not is_me(str(copy.get("from") or ""), self.me):
                    return "not your message"
                props = copy.get("properties")
                if is_deleted(props if isinstance(props, dict) else {}):
                    return "message deleted"
                if action == "edit" and copy.get("messagetype") not in TEXT_TYPES:
                    return "cannot edit this message"
            if action == "react":
                await asyncio.to_thread(
                    react, settings, token, thread_id, msg_id, request.key, remove=request.remove
                )
            elif action == "edit":
                await asyncio.to_thread(edit_message, settings, token, thread_id, msg_id, request.text)
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
        resume = ""
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
        page = {"messages": records, "older": older, "newer": newer, "source": source}
        # Unreadable windows read past without a message: the page asks on from there.
        return page | ({"cursor": resume} if resume else {})

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

    async def media(self, path: str) -> tuple[bytes, str] | None:
        """`media_file` on the archive pool, _MEDIA_READS threads at most; ArchiveUnavailable past its
        deadline, or at once when every slot is taken (waiting would only queue more hung reads)."""
        if self._media_reads >= _MEDIA_READS:
            _warn_unavailable("media")
            raise ArchiveUnavailable("archive unavailable (media busy)")
        self._media_reads += 1
        return await run_io("media", functools.partial(self.media_file, path), self._media_done)

    def _media_done(self) -> None:
        self._media_reads -= 1

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
        try:
            media = (chat_dir / "media").resolve(strict=True)
            target = (media / name).resolve(strict=True)
            # Resolved: `..`, encoded slashes and symlinks out of the folder or onto a partial download
            # all fail this one check.
            if target.parent != media or target.name.endswith(".part") or not target.is_file():
                return None
            if target.stat().st_size > _MEDIA_MAX:
                log.info("media_too_large", thread=thread_id, file=name)
                return None
            body = target.read_bytes()
        except OSError, ValueError:  # ValueError: an encoded NUL in the name
            return None
        return body, mimetypes.guess_type(name)[0] or "application/octet-stream"

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
        custom = self.emojis.custom()
        for message in messages:
            try:
                records.append(chat_record(message, thread_id, self.me, media, custom))
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

    def _emoji_items(self, prefix: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = [
            {"name": name, "char": char} for name, char in SHORTCODES.items() if name.startswith(prefix)
        ]
        # A custom name the built-in table shadows would send the built-in character.
        found += [
            {"name": name, "custom": oid, "src": f"/emoji/{oid}"}
            for name, oid in self.emojis.names.items()
            if name.casefold().startswith(prefix) and name not in SHORTCODES
        ]
        return sorted(found, key=lambda it: str(it["name"]).casefold())[:_COMPLETE_ITEMS]
