"""`miniteams web`: one local page listing conversations newest-activity-first (spec 002).

One process, one port: `websockets.serve` answers `GET /` with the bundled page and upgrades
`/ws`, over which the server pushes the whole row list (small: `--limit` rows) on connect and
on every change. No REST layer: the page verbs ride the same socket.

Routes: `/` the board page, `/chat/<thread>` a chat page, `/media/<thread>/<file>` and `/emoji/<id>`
archived and custom-emoji images, `/ws` the socket (`/ws?chat` for a chat page).

Bind is `127.0.0.X:PORT` with X and PORT drawn once and persisted (`web.json`) so the URL
survives restarts; the whole 127/8 is loopback on Linux, no interface setup needed. Pages are static
shells; every route carrying data needs the `mt` cookie, the persisted `web-secret`. A browser gets it by
loading a page with `#<token>`: the page trades the single-use token at `/auth`; `web.json` and the
launch file (`open.html`) hold the current one. The secret never goes through argv, the terminal or
the history.
"""

import asyncio
import base64
import contextlib
import errno
import functools
import hashlib
import hmac
import html
import ipaddress
import itertools
import json
import os
import random
import re
import secrets
import sqlite3
import stat
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, quote, unquote, urlsplit

import structlog
from websockets.asyncio.server import ServerConnection, broadcast, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.typing import Origin

from .archive_store import read_chats
from .auth import token_source
from .chat_verbs import (
    ANY_VALUE,
    MSG_ID_RE,
    ArchiveUnavailable,
    ChatVerbs,
    Handler,
    answer,
    archive_dir,
    chat_record,
    close_archive_pool,
    index_label,
    io,
    stuck_archive_threads,
)
from .chats import fetch_conversations, is_meeting, is_private, last_activity
from .config import Settings, write_private
from .directory import Directory, roster_label
from .dump import fetch_history
from .emojis import EmojiCache
from .http import failure
from .images import MAX_IMAGE_BYTES
from .messages import (
    is_activity,
    is_deleted,
    is_edited,
    is_me,
    mention_kind,
    reaction_emoji,
    snippet,
    thread_of,
)
from .presence import Presence, dm_peer, open_cache, person
from .receipts import EVENT as RECEIPT_EVENT
from .receipts import Receipts
from .send import mark_read
from .stream import run_forever

log = structlog.get_logger()

_YOU = "You"  # own messages, as Teams' chat list shows them
# Ad-hoc calls name who started them; scheduled meetings carry an empty list.
_CALL_STARTER_RE = re.compile(
    r'<partlist[^>]*\btype\s*=\s*"started"[^>]*>.*?<part\s+identity="([^"]+)"', re.S
)
_LABEL_CONCURRENCY = 8
_HISTORY_PAGE = 20  # history fetched per row at bootstrap: stub resolution + mention scan
_MENTION_SCAN = 50  # newest rows whose history is scanned for mentions at bootstrap
_FIRST_CHUNK = 50  # rows the background fill paints before walking the rest of the listing
_SEED_REREAD = 30.0  # seconds before the archive index is worth re-reading for a name
# Default table depth: seeded rows cost no API call, a fetched one costs two.
_LIMIT_SEEDED = 400
_LIMIT_BARE = 250
# Snippets that mean "the listing did not say": the archive may know better.
_STUB_SNIPPETS = ("🗑 deleted", "📎 attachment")
_PAGE_POLL = 2.0  # seconds between widget.html mtime checks (dev reload)
_TYPING_TTL = 10.0  # seconds; Teams does not always send ClearTyping
_SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.DOTALL)
# Thread activity that changes the label (topic, member count): drop the cached thread info.
# Someone reading a chat is not activity: bumping the row on it would defeat "seen".
_SILENT_TYPES = {RECEIPT_EVENT}
_RELABEL_TYPES = {"ThreadActivity/TopicUpdate", "ThreadActivity/AddMember", "ThreadActivity/DeleteMember"}
MAX_FRAME = MAX_IMAGE_BYTES + (1 << 20)  # one pasted image per binary frame, plus room for a long JSON verb
_BIND_ATTEMPTS = 3  # redraws when the persisted port turns out taken
_RELAY_QUEUE = 256  # live events waiting for their followers; past it one is dropped, the stream never waits
_RELAY_MAX = 10.0  # seconds one relay may take (name lookups included) before the next one goes
_MEDIA_BACKOFF = 30.0  # seconds relayed records skip the media folder after a read outlived its deadline
_COOKIE_FILE = "web-secret"
_SECRET_RE = re.compile(r"[\w-]{32,}", re.ASCII)
_COOKIE = "mt"
_COOKIE_AGE = 365 * 24 * 3600
_TOKEN_HEADER = "X-Miniteams-Token"
_PRESENCE_PEERS = 50  # 1:1 rows whose peer's presence is followed, newest first (spec 015)
_PRESENCE_AUTHORS = 50  # authors one chat page may ask presence for
_PRESENCE_MAX = 100  # people followed in all, in one post: upstream chunks at 100, the spike tested 50

type OpenTarget = Literal["app", "web", "chat", "split"]


def read_up_to(props: dict[str, Any] | None) -> str:
    """Id of the newest message Teams considers read (`consumptionhorizon` = "<id>;<ts>;<x>")."""
    horizon = str((props or {}).get("consumptionhorizon") or "")
    return horizon.split(";", 1)[0]


def is_unread(last_id: str, read_id: str) -> bool:
    """Message ids are ms-epoch strings: newer than the horizon ⇒ unread (on every device)."""
    if not last_id or not read_id:
        return False  # nothing known: do not shout
    if last_id.isdigit() and read_id.isdigit():
        return int(last_id) > int(read_id)
    return last_id > read_id


def read_at_ms(props: dict[str, Any] | None) -> int:
    """Time Teams last considered the chat read (2nd `consumptionhorizon` field, ms epoch).

    Compared against a mention's *time* rather than its message id: an edit that adds a mention
    keeps the original id, so an id comparison would call a late-added mention already read."""
    horizon = str((props or {}).get("consumptionhorizon") or "")
    parts = horizon.split(";")
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0


def _ms(iso: str | None) -> int:
    """ISO-8601 Z timestamp → ms epoch; 0 when missing or unparsable (never raises)."""
    if not iso:
        return 0
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def mention_time(resource: dict[str, Any]) -> tuple[str, bool]:
    """(ISO time the mention became visible, edited?): `edittime` (ms) when edited, else compose."""
    edit = str((resource.get("properties") or {}).get("edittime") or "")
    if edit.isdigit():
        return _iso(int(edit)), True
    return str(resource.get("composetime") or resource.get("originalarrivaltime") or ""), False


def deep_link(thread_id: str, msg_id: str, scheme: str = "https") -> str:
    """Teams link opening the chat at a message; `msteams` targets the desktop client.

    The client (teams-for-linux) falls back to a full navigation to the link's host, which a
    redirecting host aborts silently — so the msteams form uses the host the client itself loads.
    """
    host = "teams.cloud.microsoft" if scheme == "msteams" else "teams.microsoft.com"
    ctx = quote('{"contextType":"chat"}', safe="")
    thread = quote(thread_id, safe="")
    if not msg_id:  # no message to land on (activity known only from the thread version)
        return f"{scheme}://{host}/l/chat/{thread}/conversations?context={ctx}"
    return f"{scheme}://{host}/l/message/{thread}/{msg_id}?context={ctx}"


def in_scope(thread_id: str) -> bool:
    """V1 widget scope = private 1:1/group + meeting chats."""
    return is_private(thread_id) or is_meeting(thread_id)


def _seed_label(chat: dict[str, Any], me: str) -> str:
    """The name the widget would have fetched: topic, else the roster without me.

    The archive stores every participant, me included; `Directory.label` leaves me out, so reusing
    the stored label verbatim would name every 1:1 after its two members.
    """
    members = [m for m in chat["participants"] if isinstance(m, dict)]
    return roster_label(str(chat["topic"] or ""), members, me)


_NO_INDEX: tuple[int, ...] = (0, 0, 0, 0, 0)


def _index_stamp(data_dir: Path) -> tuple[int, ...]:
    """Identity of the index's content; `_NO_INDEX` when there is none to read."""
    try:
        st = (data_dir / "index.db").stat()
    except OSError:
        return _NO_INDEX
    try:
        # WAL mode: a live archiver's commits land in the -wal file, index.db only moves at checkpoints.
        wal = (data_dir / "index.db-wal").stat()
    except OSError:
        return (st.st_ino, st.st_mtime_ns, st.st_size, 0, 0)
    # A reader opening the index creates an empty -wal: that is no new content.
    return (st.st_ino, st.st_mtime_ns, st.st_size, wal.st_mtime_ns if wal.st_size else 0, wal.st_size)


def _reseed(
    data_dir: Path, me: str, known: tuple[int, ...] | None
) -> tuple[tuple[int, ...], dict[str, dict[str, Any]] | None]:
    """(index stamp, its seed), the seed None when the index is still the `known` one. Blocking."""
    stamp = _index_stamp(data_dir)  # before the read: a rewrite in between is caught by the next one
    return stamp, None if stamp == known else seed_from_archive(data_dir, me)


def seed_from_archive(data_dir: Path, me: str = "") -> dict[str, dict[str, Any]]:
    """Per-thread row material from `index.db`: what the per-row API fetches would have bought.

    An unusable archive is not an error — the caller falls back to fetching everything.
    """
    seeded: dict[str, dict[str, Any]] = {}
    try:
        # Inside the try: `read_chats` streams, so a missing or unreadable index raises on the
        # first row, not on the call.
        for chat in read_chats(data_dir):
            thread_id = str(chat["id"])
            if not in_scope(thread_id):
                continue
            conv = chat["raw"] if isinstance(chat["raw"], dict) else {}
            last = conv.get("lastMessage") or {}
            seeded[thread_id] = {
                "label": _seed_label(chat, me),
                "text": snippet(
                    str(last.get("messagetype") or ""), str(last.get("content") or ""), last.get("properties")
                ),
                "sender": _YOU
                if is_me(str(last.get("from") or ""), me)
                else str(last.get("imdisplayname") or ""),
                "read_id": read_up_to(conv.get("properties")),
                "read_at": read_at_ms(conv.get("properties")),
                "last_activity": last_activity(conv),
                "last_id": str(last.get("id") or ""),
            }
    except (OSError, sqlite3.Error) as exc:
        event = "archive_index_absent" if isinstance(exc, FileNotFoundError) else "archive_index_unusable"
        log.info(event, data_dir=str(data_dir), error=str(exc))
        return {}
    log.info("archive_index_read", chats=len(seeded), data_dir=str(data_dir))
    return seeded


async def row_from_conversation(
    conv: dict[str, Any], directory: Directory, label: str = ""
) -> dict[str, Any]:
    thread_id = str(conv.get("id") or "")
    last = conv.get("lastMessage") or {}
    msgtype = str(last.get("messagetype") or "")
    sender = "" if is_activity(msgtype) else await sender_of(last, directory)
    label = label or await directory.label(thread_id)
    if label == thread_id:  # thread lookup failed (rate limit…): the listing carries the topic
        label = str((conv.get("threadProperties") or {}).get("topic") or thread_id)
    last_id = str(last.get("id") or "")
    read_id = read_up_to(conv.get("properties"))
    return {
        "id": thread_id,
        "label": label,
        "last_activity": last_activity(conv),
        "last_id": last_id,
        "read_id": read_id,
        "read_at": read_at_ms(conv.get("properties")),
        "unread": is_unread(last_id, read_id),
        "mention": None,
        "sender": sender,
        "text": snippet(msgtype, str(last.get("content") or ""), last.get("properties")),
        "seen_at": None,
        "typing": [],
    }


async def sender_of(resource: dict[str, Any], directory: Directory | None) -> str:
    if str(resource.get("messagetype") or "") == "Event/Call":
        # `from` is the meeting organizer, not who joined or hung up: only an ad-hoc call's
        # "started" part names a person; a scheduled meeting's event has nobody to credit.
        started = _CALL_STARTER_RE.search(str(resource.get("content") or ""))
        if not started:
            return ""
        mri = started.group(1)
        if directory is None:
            return mri
        return _YOU if is_me(mri, directory.me) else await directory.display(mri)
    mri = str(resource.get("from") or "")
    name = resource.get("imdisplayname")
    if directory is None:
        return str(name or mri)
    directory.note_name(mri, name)
    if is_me(mri, directory.me):
        return _YOU
    return str(name) if name else (await directory.display(mri) if mri else "")


def seeded_rows(seed: dict[str, dict[str, Any]], limit: int) -> dict[str, dict[str, Any]]:
    """The newest `limit` archived chats as display rows — the first paint, before any API call.

    The listing has not been walked yet, so this ordering is the archive's: a chat that moved while
    the widget was down sorts late until the real listing lands and the table is replaced.
    """
    newest = sorted(
        ((tid, row) for tid, row in seed.items() if row["last_activity"]),
        key=lambda kv: str(kv[1]["last_activity"]),
        reverse=True,
    )
    return {
        thread_id: {
            "id": thread_id,
            "label": row["label"] or thread_id,
            "last_activity": row["last_activity"],
            "last_id": row["last_id"],
            "read_id": row["read_id"],
            "read_at": row["read_at"],
            "unread": is_unread(row["last_id"], row["read_id"]),
            "mention": None,
            "sender": row["sender"],
            "text": row["text"],
            "seen_at": None,
            "typing": [],
        }
        for thread_id, row in (newest[:limit] if limit else newest)
    }


async def bootstrap(
    pages: Iterable[list[dict[str, Any]]],
    directory: Directory,
    limit: int,
    me: str = "",
    seen: dict[str, str] | None = None,
    seed: dict[str, dict[str, Any]] | None = None,
    rank_offset: int = 0,
) -> dict[str, dict[str, Any]]:
    """In-scope conversations from the newest-first listing, capped at `limit` (0 = walk all).

    `seed` carries what the archive already knows (see `seed_from_archive`): the listing wins on
    every field it provides, the seed only spares the per-row fetches that would fill the gaps.
    """
    picked: list[dict[str, Any]] = []
    for page in pages:
        for conv in page:
            thread_id = str(conv.get("id") or "")
            if thread_id and in_scope(thread_id) and last_activity(conv):
                picked.append(conv)
            if limit and len(picked) >= limit:
                break
        if limit and len(picked) >= limit:
            break
    # Labels cost one thread fetch each; sequential that is ~80s for 50 rows. Bounded fan-out
    # keeps it a few seconds without hammering the chat service.
    gate = asyncio.Semaphore(_LABEL_CONCURRENCY)

    async def build(conv: dict[str, Any], rank: int) -> dict[str, Any]:
        async with gate:
            seeded = (seed or {}).get(str(conv.get("id") or "")) or {}
            row = await row_from_conversation(conv, directory, label=str(seeded.get("label") or ""))
            row["seen_at"] = (seen or {}).get(row["id"])
            # A listing message without `imdisplayname` costs a directory lookup; the archived copy
            # of the same message usually carries the name.
            row["sender"] = row["sender"] or str(seeded.get("sender") or "")
            if not row["read_id"] and seeded.get("read_id"):
                # The listing dropped the horizon (it happens on quiet chats); the archive kept it.
                row["read_id"] = str(seeded["read_id"])
                row["read_at"] = max(int(row["read_at"] or 0), int(seeded.get("read_at") or 0))
                row["unread"] = is_unread(row["last_id"], row["read_id"])
            stub = row["text"] in _STUB_SNIPPETS
            if stub and seeded.get("text") and seeded["text"] not in _STUB_SNIPPETS:
                # The listing's lastMessage has no `properties`, so a file or card post reads as a
                # deleted one; the archived copy has them. Trust it and skip the history page.
                row["text"] = str(seeded["text"])
                stub = False
            # Mentions need history, and history is the expensive part: scan the head of the list
            # only — an older chat surfaces its mention when it next moves.
            scan = bool(me) and rank < _MENTION_SCAN
            recent = await _recent(row["id"], directory) if (stub or scan or not row["last_id"]) else []
            if stub:
                await _resolve_stub(row, recent, directory)
            if not row["last_id"] and recent:
                # A listing without lastMessage (activity known from the thread version only): the
                # newest history message gives the row a target, so its deep link lands on a
                # message. The chat-only link form makes the desktop client reload (it drops calls).
                row["last_id"] = str(recent[-1].get("id") or "")
            if scan:
                await _scan_mentions(row, recent, directory, me)
            return row

    rows = await asyncio.gather(*(build(conv, rank) for rank, conv in enumerate(picked, rank_offset)))
    return {row["id"]: row for row in rows}


async def _recent(thread_id: str, directory: Directory) -> list[dict[str, Any]]:
    """Last history page of a thread, chronological; [] on failure (bootstrap must survive)."""
    try:
        # pageSize=1 skips the newest message (window artefact); ask for a page and pick by id.
        return await asyncio.to_thread(
            fetch_history, directory.settings, directory.skype_token, thread_id, _HISTORY_PAGE, 1
        )
    except Exception as exc:  # noqa: BLE001 — cosmetic enrichment: keep the row as listed
        log.debug("history_fetch_failed", thread=thread_id, error=failure(exc))
        return []


def _mention_cleared(row: dict[str, Any], at: str) -> bool:
    """Seen after the mention, or read in Teams after it: nothing left to highlight."""
    at_ms = _ms(at)
    return _ms(row.get("seen_at")) >= at_ms or int(row.get("read_at") or 0) >= at_ms


def _mention_outranked(current: dict[str, Any] | None, kind: str) -> bool:
    """An uncleared direct mention is kept over a newer @everyone."""
    return current is not None and current.get("kind") == "me" and kind == "all"


async def _mention_of(resource: dict[str, Any], kind: str, directory: Directory | None) -> dict[str, Any]:
    at, edited = mention_time(resource)
    msgtype = str(resource.get("messagetype") or "")
    return {
        "kind": kind,
        "by": await sender_of(resource, directory),
        "text": snippet(msgtype, str(resource.get("content") or ""), resource.get("properties")),
        "at": at,
        "edited": edited,
        "msg_id": str(resource.get("id") or ""),
    }


async def _scan_mentions(
    row: dict[str, Any], recent: list[dict[str, Any]], directory: Directory, me: str
) -> None:
    """Newest still-visible mention in the page (a direct one over @everyone), else None."""
    best: dict[str, Any] | None = None
    for message in recent:
        kind = mention_kind(message, me)
        if kind is None:
            continue
        at, _ = mention_time(message)
        if _mention_cleared(row, at) or _mention_outranked(best, kind):
            continue
        # History is in compose order, but a mention added by edit is dated by its edit: compare
        # by `at`, and let a direct mention replace an @everyone whatever its date.
        if best is None or best["kind"] != kind or _ms(at) >= _ms(best["at"]):
            best = await _mention_of(message, kind, directory)
    row["mention"] = best


async def _resolve_stub(row: dict[str, Any], recent: list[dict[str, Any]], directory: Directory) -> None:
    """The listing's lastMessage drops `properties`, so a file/card post looks exactly like a
    deleted message. The history page tells them apart."""
    message = next((m for m in recent if str(m.get("id") or "") == row["last_id"]), None)
    if message is None:
        return
    msgtype = str(message.get("messagetype") or "")
    row["text"] = snippet(msgtype, str(message.get("content") or ""), message.get("properties"))
    if not is_activity(msgtype):
        row["sender"] = await sender_of(message, directory)


# --- bind address ---


def parse_bind(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    host = host.removeprefix("[").removesuffix("]")  # [::1]:PORT, the URL form
    if not host or not port.isdigit():
        raise ValueError(f"expected ADDR:PORT, got {value!r}")
    address = ipaddress.ip_address(host)
    # The cookie travels in clear over plain HTTP: anything but loopback would hand it to the network.
    if not address.is_loopback:
        raise ValueError(f"refusing non-loopback bind {host!r}")
    # Canonical: the Host and Origin checks compare text, and a browser sends the compressed form.
    return str(address), int(port)


def netloc(host: str, port: int) -> str:
    """`host:port` as a URL and a Host header carry it: an IPv6 address goes in brackets."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def load_secret(settings: Settings) -> str:
    """The pages' cookie value, from `web-secret`; drawn and written owner-only when missing, or when
    the file could have been read or swapped by someone else (a symlink, group/other bits)."""
    path = settings.config_dir / _COOKIE_FILE
    try:
        secret = (_read_owner_only(path) or "").strip()
    except OSError as exc:
        raise RuntimeError(
            f"cannot read {path}: {exc.strerror or exc}; fix its permissions or delete it"
        ) from None
    if _SECRET_RE.fullmatch(secret):
        return secret
    secret = secrets.token_urlsafe(32)
    try:
        write_private(path, secret)
    except OSError as exc:
        raise RuntimeError(f"cannot write {path}: {exc.strerror or exc}") from None
    return secret


def _read_owner_only(path: Path) -> str | None:
    """`path`'s text; None when absent, a symlink, not a regular file of ours, or open to group/other."""
    try:
        # O_NOFOLLOW: a planted symlink fails with ELOOP; O_NONBLOCK: a planted FIFO must not hang the start.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ELOOP):
            return None
        raise
    with open(fd, encoding="utf-8", errors="replace") as fh:
        st = os.fstat(fd)
        ours = stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077
        return fh.read() if ours else None


def _draw_port() -> int:
    return random.randint(32768, 60999)


def _draw_bind() -> tuple[str, int]:
    # 127.0.0.1 is what every other local daemon binds; a random sibling avoids collisions and
    # keeps the widget off the well-known address (user request).
    return f"127.0.0.{random.randint(2, 254)}", _draw_port()


def _persisted_bind(settings: Settings) -> tuple[str, int] | None:
    try:
        data = json.loads((settings.config_dir / "web.json").read_text())
        return str(data["host"]), int(data["port"])
    except OSError, ValueError, KeyError, TypeError:
        return None


def load_bind(settings: Settings, override: str | None = None) -> tuple[str, int]:
    """`--bind` wins; else the persisted draw; else a new draw (serve_board persists it once bound)."""
    if override:
        return parse_bind(override)
    return _persisted_bind(settings) or _draw_bind()


def save_bind(settings: Settings, host: str, port: int, token: str) -> None:
    """`web.json`: the bind, and the launch token a browser trades for the cookie."""
    write_private(settings.config_dir / "web.json", json.dumps({"host": host, "port": port, "token": token}))


async def _reap(proc: asyncio.subprocess.Process, opener: str) -> None:
    """Wait for an opener; a failing one is logged with the end of its stderr (else it fails silently)."""
    tail = b""
    if proc.stderr is not None:
        # Drained as it comes: a browser started here may live for hours and must never block on a full pipe.
        while chunk := await proc.stderr.read(4096):
            tail = (tail + chunk)[-2000:]
    if rc := await proc.wait():
        log.warning("opener_exit", opener=opener, rc=rc, stderr=tail.decode(errors="replace").strip())


def launch_page(host: str, port: int, token: str) -> str:
    url = html.escape(f"http://{netloc(host, port)}/#{token}")
    return f'<!doctype html><meta http-equiv="refresh" content="0;url={url}"><a href="{url}">miniteams</a>\n'


def publish(settings: Settings, host: str, port: int, guarded: bool, token: str) -> bool:
    """Write `token` into web.json and the launch file; False, writing nothing, when `guarded` (a --bind
    run) and web.json names another server, whose launch token lives there."""
    if guarded and _persisted_bind(settings) not in (None, (host, port)):
        return False
    save_bind(settings, host, port, token)
    try:
        write_private(settings.launch_file, launch_page(host, port, token))
    except OSError as exc:  # web.json holds the token: rotation must go on
        log.warning("launch_file_failed", path=str(settings.launch_file), error=str(exc))
    return True


# --- server ---


class Board:
    """Row table + connected pages. Every mutation ends in a broadcast of the full sorted list."""

    def __init__(
        self,
        rows: dict[str, dict[str, Any]],
        directory: Directory | None = None,
        reactions: bool = False,
        typing_ttl: float = _TYPING_TTL,
        seen_path: Path | None = None,
        muted_path: Path | None = None,
        opener: list[str] | None = None,
        open_scheme: str = "msteams",
        browser: list[str] | None = None,
        me: str = "",
        busy: bool = False,
        seed: dict[str, dict[str, Any]] | None = None,
        data_dir: Path | None = None,
        seed_stamp: tuple[int, ...] = _NO_INDEX,
    ) -> None:
        self.rows = rows
        self.directory = directory
        self.me = me  # own MRI: what a mention has to name
        self.me_name = directory.name_for(me) if directory and me else ""  # the page highlights it
        self.reactions = reactions
        self.typing_ttl = typing_ttl
        self.seen_path = seen_path
        self.muted_path = muted_path
        self.opener = opener  # argv the deep link is appended to; None = the page follows its link
        self.open_scheme = open_scheme
        self.browser = browser  # argv for the https link on Ctrl/middle click; None = page's own browser
        self.base_url = ""  # http://host:port, set once bound: `chat` opens point back at us
        self.secret = ""  # the `mt` cookie every route needs: serve_board loads it, routes read it here
        self.token = secrets.token_urlsafe(24)  # single use: `/auth` trades it for the cookie
        self.publish: Callable[[str], bool] | None = None  # a new token into web.json, set by serve_board
        self.clients: set[ServerConnection] = set()
        self.busy = busy  # the page shows a spinner until the background fill says otherwise
        self.seed = seed or {}
        self.data_dir = data_dir
        self._seed_read = time.monotonic()
        self._seed_stamp = seed_stamp  # `seed`'s index stamp, read with it: a stat here would block the loop
        self._seed_lock = asyncio.Lock()
        self._typing_timers: dict[tuple[str, str], asyncio.TimerHandle] = {}
        self._follows: dict[ServerConnection, tuple[str, Path | None]] = {}  # socket → (thread, media dir)
        self._relays: asyncio.Queue[tuple[str, Callable[[], Awaitable[None]]]] = asyncio.Queue(_RELAY_QUEUE)
        self._relay_task: asyncio.Task[None] | None = None  # started by the first relay, ended by close()
        self._dropped: dict[str, int] = {}  # thread → relays dropped since the queue filled (one stall)
        self._media_down_until = 0.0  # monotonic; relayed records skip the media folder until then
        self.presence: Presence | None = None  # set by run(); None = no dots
        self.receipts: Receipts | None = None  # set by run(); None = no read receipts
        self._authors: dict[ServerConnection, list[str]] = {}  # chat socket → authors it shows
        self.emojis = EmojiCache(directory)
        self.chat = ChatVerbs(directory, self.emojis, self._scope, me, self.me_name, data_dir)
        board_first: dict[str, Handler] = {
            "seen": self._on_seen,
            "unseen": self._on_unseen,
            "read": self._on_read,
            "mute": self._on_mute,
            "unmute": self._on_unmute,
        }
        board_last: dict[str, Handler] = {
            "board": self._on_board,
            "follow": self._on_follow,
            "open": self._on_open,
            "authors": self._on_authors,
        }
        # Dispatch order: a verb carrying several keys is handled as the first one listed.
        self._handlers: dict[str, Handler] = {**board_first, **self.chat.handlers(), **board_last}
        # Keys whose value is not a thread id string: how each one is recognised.
        self._accepts: dict[str, Callable[[Any], bool]] = {
            "board": lambda value: value is True,
            "authors": lambda value: isinstance(value, list),
        }
        self._accepts |= dict.fromkeys(ANY_VALUE, lambda value: True)
        self._seen: dict[str, str] = _load_seen(seen_path)
        self._muted: dict[str, str] = _load_seen(muted_path)  # thread → when muted (same shape)
        for row in rows.values():
            row["seen_at"] = self._seen.get(row["id"])
            row["muted"] = row["id"] in self._muted
            row.setdefault("mention", None)

    async def archived_label(self, thread_id: str) -> str:
        """The name the archive has for a chat, re-reading the index when it has moved on.

        The live lookup is what names a new chat; this is the fallback when it fails (a 429, or a
        chat we were removed from), and the archive may have learned the name after we started.
        """
        name = self._seeded_label(thread_id)
        if name or self.data_dir is None:
            return name
        # One at a time: a burst of unnamed chats (a reconnect replaying what it dropped) would
        # otherwise start one 12 MiB read each, all of them reading the same file.
        async with self._seed_lock:
            if time.monotonic() - self._seed_read < _SEED_REREAD:
                return ""  # too soon: even the stat is an NFS call, and it runs per unnamed event
            self._seed_read = time.monotonic()
            try:
                stamp, seed = await io("index", _reseed, self.data_dir, self.me, self._seed_stamp)
            except ArchiveUnavailable:
                return ""
            if seed is not None:
                self.seed, self._seed_stamp = seed, stamp
        return self._seeded_label(thread_id)

    def _seeded_label(self, thread_id: str) -> str:
        return str(self.seed.get(thread_id, {}).get("label") or "")

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.broadcast()

    def replace(self, rows: dict[str, dict[str, Any]], busy: bool = False) -> None:
        """Swap the table for the freshly bootstrapped one, keeping what only the live page knows.

        A message landing during the fill created or moved a row: its `typing` and anything newer
        than what the fill saw must survive, or the page would visibly go backwards.
        """
        for thread_id, row in rows.items():
            live = self.rows.get(thread_id)
            if live and str(live.get("last_activity") or "") > str(row.get("last_activity") or ""):
                continue  # the stream already knows better than the listing did
            row["typing"] = live["typing"] if live else []
            row["seen_at"] = self._seen.get(thread_id)
            row["muted"] = thread_id in self._muted
            row.setdefault("mention", None)
            self.rows[thread_id] = row
        self.busy = busy
        self.broadcast()

    def prune(self, keep: set[str], newer_than: str) -> int:
        """Drop the seeded rows the live listing did not return; returns how many went.

        Without this the table holds both sets — the archive's newest `--limit` chats *and* the
        listing's — so a widget whose archive lags doubles its row count and its payload. A row the
        stream touched since the walk started is not the listing's to disown.
        """
        gone = [
            thread_id
            for thread_id, row in self.rows.items()
            if thread_id not in keep and str(row.get("last_activity") or "") <= newer_than
        ]
        for thread_id in gone:
            del self.rows[thread_id]
        return len(gone)

    # --- seen marker (page verb) ---

    def mark_seen(self, thread_id: str, at: str | None = None) -> bool:
        """Stamp `at` (the activity the page showed when clicked) as seen; hidden until newer lands.

        A message that landed between render and click keeps the row visible: the stamp never
        exceeds what the user actually looked at.
        """
        row = self.rows.get(thread_id)
        if row is None or not row.get("last_activity"):
            return False
        # `at` comes from the page: an unparsable value must not become the stamp (ms 0 sorts first).
        stamp = min([at, row["last_activity"]], key=_ms) if at and _ms(at) else row["last_activity"]
        mention = row.get("mention")
        if mention:
            # A mention added by editing an old message is newer than the last activity: the
            # stamp must reach it, else the row would never leave the mention state.
            stamp = max([stamp, mention["at"]], key=_ms)
            row["mention"] = None
        self._seen[thread_id] = row["seen_at"] = stamp
        self._save_seen()
        self.broadcast()
        return True

    async def mark_read(self, thread_id: str) -> bool:
        """Page verb `read`: tell Teams the chat is read up to the row's last message.

        Nothing is changed locally: Teams answers with a ConversationUpdate that moves the
        read marker (and clears a mention) the same way a read on any other device does."""
        row = self.rows.get(thread_id)
        if row is None or not row.get("last_id") or self.directory is None:
            return False
        try:
            await asyncio.to_thread(
                mark_read, self.directory.settings, self.directory.skype_token, thread_id, row["last_id"]
            )
        except Exception as exc:  # noqa: BLE001 — a failed read marker must not drop the socket
            log.warning("mark_read_failed", thread=thread_id, error=failure(exc))
            return False
        return True

    # --- followed chats (spec 011) ---

    def _scope(self, ws: ServerConnection | None, thread_id: str) -> bool:
        # Off the board, only a followed chat of the widget's scope: never a channel.
        return thread_id in self.rows or (in_scope(thread_id) and self._following(ws, thread_id))

    def _following(self, ws: ServerConnection | None, thread_id: str) -> bool:
        return ws is not None and self._follows.get(ws, ("", None))[0] == thread_id

    async def follow(self, ws: ServerConnection, thread_id: str) -> dict[str, Any]:
        """Page verb `follow`: this socket receives the thread's live messages from now on."""
        try:
            chat_dir = await io("dir", archive_dir, self.data_dir, thread_id)
        except ArchiveUnavailable:
            return {"follow": thread_id, "error": "archive unavailable"}
        if chat_dir is None and thread_id not in self.rows:
            return {"follow": thread_id, "error": "unknown chat"}
        self._follows[ws] = (thread_id, chat_dir / "media" if chat_dir else None)
        if self.receipts is not None:
            self.receipts.retain({tid for tid, _ in self._follows.values()})
            self.receipts.open(thread_id)
        row = self.rows.get(thread_id)
        label = str(row.get("label") or "") if row else await self.archived_label(thread_id)
        if not label and chat_dir is not None:  # a channel, or a chat the seed lacks: the index names it
            with contextlib.suppress(ArchiveUnavailable):  # the page shows the id rather than nothing
                label = await io("label", index_label, self.data_dir, thread_id)
        # `opener` tells the page whether Open in Teams goes through the server or its own new tab.
        return {
            "follow": thread_id,
            "ok": True,
            "label": label or thread_id,
            "opener": self.opener is not None,
            "page": page_version("chat.html"),  # an open tab reloads when chat.html changed
            "read_only": not in_scope(thread_id),  # `send` refuses channels: no composer for them
        }

    async def _forward(
        self, thread_id: str, resource_type: Any, resource: dict[str, Any], msgtype: str
    ) -> None:
        """Relay a followed thread's message, edit or typing to the sockets following it."""
        medias = [media for tid, media in self._follows.values() if tid == thread_id]
        if not medias:
            return
        media = medias[0]
        if resource_type == "NewMessage" and msgtype in ("Control/Typing", "Control/ClearTyping"):
            if is_me(str(resource.get("from") or "").rsplit("/", 1)[-1], self.me):
                return
            name = await sender_of(resource, self.directory)
            if msgtype == "Control/Typing":
                self._typing_arm(thread_id, name)
                self._relay_now(thread_id, {"typing": name, "on": True})
            else:
                self._typing_stop(thread_id, name, self._relay_now)
            return
        if resource_type not in ("NewMessage", "MessageUpdate"):
            return
        if msgtype.startswith("Control/") or msgtype in _SILENT_TYPES:
            return
        record = await self._record(resource, thread_id, media)
        if "content" not in resource:
            del record["html"]  # a partial update (reaction): the page keeps the body it has
        if resource_type == "NewMessage" and not is_activity(msgtype):
            name = await sender_of(resource, self.directory)
            self._typing_stop(thread_id, name, self._relay_now)  # their message ends it
        self._relay_now(thread_id, {"msg" if resource_type == "NewMessage" else "edit": record})

    async def _record(self, resource: dict[str, Any], thread_id: str, media: Path | None) -> dict[str, Any]:
        """The relayed record; past a media read's deadline, records skip the folder for a while."""
        custom = self.emojis.custom()
        name_for = self.directory.name_for if self.directory else None
        if media is not None and time.monotonic() >= self._media_down_until:
            try:
                return await io("relay", chat_record, resource, thread_id, self.me, media, custom, name_for)
            except ArchiveUnavailable:
                self._media_down_until = time.monotonic() + _MEDIA_BACKOFF
                log.warning("follow_media_unavailable", thread=thread_id, retry_in=_MEDIA_BACKOFF)
        # Without a media folder sanitising reads no file: cheap enough for the loop.
        return chat_record(resource, thread_id, self.me, None, custom, name_for)

    def _followers(self, thread_id: str) -> list[ServerConnection]:
        return [ws for ws, (tid, _) in self._follows.items() if tid == thread_id]

    def _relay_now(self, thread_id: str, frame: dict[str, Any]) -> None:
        """Frame to the thread's followers; only the relay task calls it, so frames keep their order."""
        # Named: a page switching chats may still receive frames of the one it left.
        broadcast(self._followers(thread_id), json.dumps({**frame, "thread": thread_id}, ensure_ascii=False))

    def _relay_queued(self, thread_id: str, frame: dict[str, Any]) -> None:
        """From the board or a timer: behind the queued relays, a typing clear must not overtake them."""
        if self._followers(thread_id):
            self._enqueue_relay(thread_id, functools.partial(self._relay_async, thread_id, frame))

    def take_token(self, offered: str) -> bool:
        """The launch token is good once: a use re-mints it into web.json and the launch file."""
        if not _same(offered, self.token):
            return False
        token = secrets.token_urlsafe(24)
        try:
            if self.publish is not None and not self.publish(token):
                print(f"{self.base_url}/#{token}", file=sys.stderr)  # not in web.json: its only way in
        except OSError as exc:
            log.warning("web_json_failed", error=str(exc))
            return True  # web.json still holds this one: keep it valid, never strand the user
        self.token = token
        return True

    def mark_unseen(self, thread_id: str) -> bool:
        """Undo `seen`: the row shows again until the next click."""
        row = self.rows.get(thread_id)
        if row is None or thread_id not in self._seen:
            return False
        del self._seen[thread_id]
        row["seen_at"] = None
        self._save_seen()
        self.broadcast()
        return True

    def _save_seen(self) -> None:
        _save_marks(self.seen_path, self._seen)

    # --- mute (page verb) ---

    def set_muted(self, thread_id: str, muted: bool) -> bool:
        """Muted: hidden whatever lands on the chat, until unmuted; a mention still surfaces."""
        row = self.rows.get(thread_id)
        if row is None or (thread_id in self._muted) == muted:
            return False
        if muted:
            self._muted[thread_id] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            del self._muted[thread_id]
        row["muted"] = muted
        _save_marks(self.muted_path, self._muted)
        self.broadcast()
        return True

    # --- live events (stream hook) ---

    async def on_event(self, obj: dict[str, Any]) -> None:
        resource = obj.get("resource") or {}
        resource_type = obj.get("resourceType")
        # A ConversationUpdate is keyed by the thread itself (no conversationLink).
        thread_id = (
            str(resource.get("id") or "") if resource_type == "ConversationUpdate" else thread_of(resource)
        )
        msgtype = str(resource.get("messagetype") or "")
        log.debug("web_event", thread=thread_id, resource_type=resource_type, messagetype=msgtype)
        if msgtype == RECEIPT_EVENT and self.receipts is not None:
            self.receipts.on_event(thread_id, resource)
        await self._board_event(thread_id, resource_type, resource, msgtype)
        if any(tid == thread_id for tid, _ in self._follows.values()):
            # Queued, not awaited: a record globs the (NFS) media folder, the shared stream must not wait.
            forward = functools.partial(self._forward, thread_id, resource_type, resource, msgtype)
            self._enqueue_relay(thread_id, forward)

    def _enqueue_relay(self, thread_id: str, work: Callable[[], Awaitable[None]]) -> None:
        if self._relay_task is None:
            self._relay_task = asyncio.create_task(self._relay_forever())
        try:
            self._relays.put_nowait((thread_id, work))
        except asyncio.QueueFull:
            if not self._dropped:
                log.warning("follow_relay_stalled", thread=thread_id)
            if thread_id not in self._dropped:
                # Once per stall: the page knows its view has a hole (a message, a typing clear).
                self._gap(thread_id)
            self._dropped[thread_id] = self._dropped.get(thread_id, 0) + 1

    def _receipts_frame(self, thread_id: str, frame: dict[str, Any]) -> None:
        # Not queued: each frame is the full set, and a stalled relay must not hold it back.
        broadcast(self._followers(thread_id), json.dumps({**frame, "thread": thread_id}, ensure_ascii=False))

    async def on_gap(self, reason: str) -> None:
        """Trouter reconnected or dropped events: the followed chats' receipts are snapshotted again."""
        if self.receipts is not None:
            self.receipts.resync()

    def _gap(self, thread_id: str) -> None:
        broadcast(self._followers(thread_id), json.dumps({"gap": thread_id}, ensure_ascii=False))

    async def _relay_forever(self) -> None:
        """One relay at a time, in arrival order."""
        while True:
            thread_id, work = await self._relays.get()
            try:
                await asyncio.wait_for(work(), _RELAY_MAX)
            except Exception as exc:  # noqa: BLE001 — a malformed event must not end the relays
                log.warning("follow_relay_failed", thread=thread_id, error_type=type(exc).__name__)
            if self._dropped and self._relays.empty():
                dropped, threads = sum(self._dropped.values()), len(self._dropped)
                log.warning("follow_relay_resumed", dropped=dropped, threads=threads)
                # Again once drained: the first gap may have been answered while the hole still grew.
                for gapped in self._dropped:
                    self._gap(gapped)
                self._dropped.clear()

    async def _relay_async(self, thread_id: str, frame: dict[str, Any]) -> None:
        self._relay_now(thread_id, frame)

    def close(self) -> None:
        """Stop the relays (run() owns the archive pool)."""
        if self._relay_task is not None:
            self._relay_task.cancel()
            self._relay_task = None

    async def _board_event(
        self, thread_id: str, resource_type: Any, resource: dict[str, Any], msgtype: str
    ) -> None:
        if not in_scope(thread_id):
            return
        if resource_type == "ConversationUpdate":
            self._conversation_update(thread_id, resource)
        elif resource_type == "NewMessage":
            self.chat.echoed(thread_id, str(resource.get("clientmessageid") or ""))
            if msgtype in ("Control/Typing", "Control/ClearTyping"):
                await self._typing(thread_id, resource, started=msgtype == "Control/Typing")
            elif not msgtype.startswith("Control/") and msgtype not in _SILENT_TYPES:
                await self._new_message(thread_id, resource, msgtype)
        elif resource_type == "MessageUpdate":
            await self._message_update(thread_id, resource, msgtype)

    def _conversation_update(self, thread_id: str, resource: dict[str, Any]) -> None:
        """Read marker moved (this or another device): Teams' own read state, not our `seen`."""
        row = self.rows.get(thread_id)
        read_id = read_up_to(resource.get("properties"))
        if row is None or not read_id:
            return
        row["read_id"] = read_id
        row["read_at"] = max(int(row.get("read_at") or 0), read_at_ms(resource.get("properties")))
        row["unread"] = is_unread(row.get("last_id", ""), read_id)
        mention = row.get("mention")
        if mention and _mention_cleared(row, mention["at"]):
            row["mention"] = None
        self.broadcast()

    async def _new_message(self, thread_id: str, resource: dict[str, Any], msgtype: str) -> None:
        row = self.rows.get(thread_id)
        if row is None:
            label = await self.directory.label(thread_id) if self.directory else thread_id
            row = self.rows[thread_id] = {
                "id": thread_id,
                "label": label,
                "seen_at": self._seen.get(thread_id),
                "typing": [],
                "read_id": "",
                "read_at": 0,
                "mention": None,
                "muted": thread_id in self._muted,
            }
        sender = "" if is_activity(msgtype) else await sender_of(resource, self.directory)
        # A bare-id label is a failed lookup: retry it (Directory spaces transient retries 60s apart).
        if self.directory is not None and (msgtype in _RELABEL_TYPES or row["label"] == thread_id):
            if msgtype in _RELABEL_TYPES:
                self.directory.forget(thread_id)
            fresh = await self.directory.label(thread_id)
            if fresh != thread_id:  # lookup failed (removed from the chat, 429): keep the old name
                row["label"] = fresh
            elif row["label"] == thread_id:
                # The seed only: an index re-read may wait on a hung mount, the stream must not.
                row["label"] = self._seeded_label(thread_id) or thread_id
        last_id = str(resource.get("id") or "")
        row.update(
            last_activity=str(resource.get("composetime") or resource.get("originalarrivaltime") or ""),
            last_id=last_id,
            # Own messages come back as unread until Teams moves the horizon (a ConversationUpdate
            # follows within a second) — honest, and it needs no notion of "me".
            unread=is_unread(last_id, row.get("read_id", "")),
            sender=sender,
            text=snippet(msgtype, str(resource.get("content") or ""), resource.get("properties")),
        )
        await self._note_mention(row, resource)
        self._typing_stop(thread_id, sender, self._relay_queued)  # their message is the end of their typing
        self.broadcast()

    async def _message_update(self, thread_id: str, resource: dict[str, Any], msgtype: str) -> None:
        row = self.rows.get(thread_id)
        props = resource.get("properties") or {}
        if props.get("emotions") is not None:
            if self.reactions and row is not None:
                await self._reaction(row, resource, props["emotions"])
            return
        if row is None:
            return
        msg_id = str(resource.get("id") or "")
        edited, deleted = is_edited(resource), is_deleted(props)
        changed = False
        if deleted:
            # The mentioning message is gone: nothing left to point at.
            if (row.get("mention") or {}).get("msg_id") == msg_id:
                row["mention"] = None
                changed = True
        elif edited:
            # Any message of the thread, not only the last: an edit can add a mention late.
            changed = await self._note_mention(row, resource)
        if row.get("last_id") == msg_id and (deleted or edited):
            row["text"] = (
                "🗑 deleted" if deleted else snippet(msgtype, str(resource.get("content") or ""), props)
            )
            changed = True
        if changed:
            self.broadcast()

    async def _note_mention(self, row: dict[str, Any], resource: dict[str, Any]) -> bool:
        """Fold a message's mention state into the row; True when the row changed."""
        kind = mention_kind(resource, self.me)
        current = row.get("mention")
        msg_id = str(resource.get("id") or "")
        if kind is None:
            if current and current.get("msg_id") == msg_id:  # an edit removed the mention
                row["mention"] = None
                return True
            return False
        at, _ = mention_time(resource)
        if _mention_cleared(row, at) or _mention_outranked(current, kind):
            return False
        row["mention"] = await _mention_of(resource, kind, self.directory)
        return True

    async def _reaction(self, row: dict[str, Any], resource: dict[str, Any], emotions: list[Any]) -> None:
        """Newest added reaction becomes the row's last event and bumps it (opt-in, `--reactions`)."""
        msg_id = str(resource.get("id") or resource.get("clientmessageid") or "")
        latest: tuple[int, str, str] | None = None  # (time_ms, emoji, mri)
        for emotion in emotions:
            key = emotion.get("key", "?")
            users = emotion.get("users") or []
            added, _ = (
                self.directory.reaction_diff(msg_id, key, [str(u.get("mri", "")) for u in users])
                if self.directory
                else ({str(u.get("mri", "")) for u in users}, set())
            )
            for u in users:
                mri = str(u.get("mri", ""))
                if mri in added:
                    candidate = (int(u.get("time") or 0), reaction_emoji(key), mri)
                    latest = max(latest, candidate) if latest else candidate
        if latest is None:
            return
        time_ms, emoji, mri = latest
        who = await self.directory.display(mri) if self.directory else mri
        when = datetime.fromtimestamp(time_ms / 1000, tz=UTC) if time_ms else datetime.now(UTC)
        row.update(
            last_activity=when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            sender="",
            text=f"{emoji} {who} reacted",
        )
        self.broadcast()

    async def _typing(self, thread_id: str, resource: dict[str, Any], started: bool) -> None:
        row = self.rows.get(thread_id)
        if row is None:
            return  # a thread we never listed: nothing to annotate
        name = await sender_of(resource, self.directory)
        if not started:
            self._typing_stop(thread_id, name, self._relay_queued)
            self.broadcast()
            return
        if name not in row["typing"]:
            row["typing"].append(name)
        self._typing_arm(thread_id, name)
        self.broadcast()

    def _typing_arm(self, thread_id: str, name: str) -> None:
        """(Re)start the expiry: Teams does not always send ClearTyping."""
        key = (thread_id, name)
        if key in self._typing_timers:
            self._typing_timers[key].cancel()
        loop = asyncio.get_running_loop()
        self._typing_timers[key] = loop.call_later(self.typing_ttl, self._typing_expired, thread_id, name)

    def _typing_stop(self, thread_id: str, name: str, relay: Callable[[str, dict[str, Any]], None]) -> None:
        """Clear `name`'s typing; `relay` tells a follower's page: `_relay_now` from the relay task."""
        timer = self._typing_timers.pop((thread_id, name), None)
        if timer:
            timer.cancel()
            if name != _YOU:  # my own typing on another device is not news to the page
                relay(thread_id, {"typing": name, "on": False})
        row = self.rows.get(thread_id)
        if row and name in row["typing"]:
            row["typing"].remove(name)

    def _typing_expired(self, thread_id: str, name: str) -> None:
        self._typing_stop(thread_id, name, self._relay_queued)
        if thread_id in self.rows:  # a followed chat off the board has nothing to repaint
            self.broadcast()

    # --- pages ---

    def payload(self) -> str:
        ordered = sorted(self.rows.values(), key=lambda r: r["last_activity"], reverse=True)
        # `page` lets an open tab notice a newer widget.html (server restart, edit) and reload.
        for row in ordered:
            row["link"] = deep_link(row["id"], row.get("last_id", ""))
            row["peer"] = dm_peer(row["id"], self.me)  # whose presence dot the row shows
        return json.dumps(
            {
                "rows": ordered,
                "page": page_version(),
                "opener": self.opener is not None,
                "browser": self.browser is not None,
                "me": self.me_name,
                "busy": self.busy,
            },
            ensure_ascii=False,
        )

    async def open_row(
        self,
        thread_id: str,
        target: OpenTarget = "app",
        ws: ServerConnection | None = None,
        msg_id: str = "",
    ) -> bool:
        """Page verb `open`: run the opener (`app`) or the browser (`web`) on the deep link of the row
        (or of the chat `ws` follows), at `msg_id` when given, else at the row's last message; `chat`
        and `split` run the browser on our own `/chat/<thread>` page instead, `split` in split mode.

        Opening is not reading: the row keeps its seen/unread state until the user says so."""
        row = self.rows.get(thread_id)
        argv = self.opener if target == "app" else self.browser
        if (row is None and not self._following(ws, thread_id)) or argv is None:
            return False
        if target in ("chat", "split"):
            if not self.base_url:
                return False
            # argv is world-readable: only the single-use token, never the secret; split rides in the
            # URL (--browser has none of the widget's settings).
            split = "?chatnav=split" if target == "split" else ""
            url = f"{self.base_url}/chat/{quote(thread_id, safe='')}{split}#{self.token}"
        else:
            last = msg_id or str((row or {}).get("last_id") or "")
            url = deep_link(thread_id, last, "https" if target == "web" else self.open_scheme)
        try:
            # argv exec, no shell: the only variable part is a URL built from ids we already hold.
            proc = await asyncio.create_subprocess_exec(
                *argv,
                url,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            log.warning("opener_failed", opener=argv[0], error=str(exc))
            return False
        asyncio.get_running_loop().create_task(_reap(proc, argv[0]))
        log.info("opened", thread=thread_id, target=target)
        return True

    async def watch_page(self, interval: float = _PAGE_POLL) -> None:
        """Broadcast when widget.html changes so open tabs reload without waiting for an event."""
        current = page_version()
        while True:
            await asyncio.sleep(interval)
            version = page_version()
            if version != current:
                current = version
                self.broadcast()

    def broadcast(self) -> None:
        # Library helper: never awaits a slow page; a client with a full buffer just misses one
        # frame (the next push carries the full list anyway).
        broadcast(self.clients, self.payload())
        self._follow_presence()

    # --- presence (spec 015) ---

    def _follow_presence(self) -> None:
        """Peers of the newest 1:1 rows, plus the authors every chat page shows."""
        if self.presence is None:
            return
        newest = sorted(self.rows.values(), key=lambda r: str(r.get("last_activity") or ""), reverse=True)
        peers = (p for r in newest if (p := dm_peer(r["id"], self.me)))
        authors = (m for shown in self._authors.values() for m in shown)
        followed = dict.fromkeys([*itertools.islice(peers, _PRESENCE_PEERS), *authors])
        self.presence.want(itertools.islice(followed, _PRESENCE_MAX))

    def presence_frame(self) -> str:
        return json.dumps(self.presence.frame() if self.presence else {"presence": {}})

    def broadcast_presence(self) -> None:
        """Every page, board or chat: the same person's dots change everywhere at once."""
        broadcast(self.clients | self._authors.keys(), self.presence_frame())

    async def handle(self, ws: ServerConnection) -> None:
        # A chat page (`/ws?chat`) only needs its own chat: the board's rows reach it only once it
        # asks (`board`, split layout).
        query = parse_qs(urlsplit(ws.request.path).query, keep_blank_values=True) if ws.request else {}
        board_page = "chat" not in query
        if board_page:
            self.clients.add(ws)
        try:
            if board_page:
                await ws.send(self.payload())
                if self.presence is not None:
                    await ws.send(self.presence_frame())
            async for raw in ws:
                if isinstance(raw, bytes):
                    self.chat.frame(ws, raw)
                    continue
                try:
                    verb = json.loads(raw)
                except json.JSONDecodeError, TypeError:
                    verb = None
                key = _verb_key(self._handlers, self._accepts, verb) if isinstance(verb, dict) else None
                if key is None:
                    log.debug("ws_client_message_ignored", data=str(raw)[:100])
                    continue
                await self._dispatch(ws, key, verb)
        finally:
            self.clients.discard(ws)
            self._follows.pop(ws, None)
            if self.receipts is not None:
                self.receipts.retain({tid for tid, _ in self._follows.values()})
            self.chat.forget(ws)
            if self._authors.pop(ws, None) is not None:
                self._follow_presence()

    async def _dispatch(self, ws: ServerConnection, key: str, verb: dict[str, Any]) -> None:
        try:
            await self._handlers[key](ws, verb)
        except Exception as exc:  # noqa: BLE001 — a failing verb must not close the page's socket
            # Answering verbs go through `answer`, which replies; this guards the synchronous ones.
            log.warning("verb_failed", verb=key, error_type=type(exc).__name__, exc_info=True)

    # --- board verbs: one handler per wire key ---

    async def _on_seen(self, ws: ServerConnection, verb: dict[str, Any]) -> None:
        at = verb.get("at")
        self.mark_seen(verb["seen"], at if isinstance(at, str) else None)

    async def _on_unseen(self, ws: ServerConnection, verb: dict[str, Any]) -> None:
        self.mark_unseen(verb["unseen"])

    async def _on_read(self, ws: ServerConnection, verb: dict[str, Any]) -> None:
        await self.mark_read(verb["read"])

    async def _on_mute(self, ws: ServerConnection, verb: dict[str, Any]) -> None:
        self.set_muted(verb["mute"], True)

    async def _on_unmute(self, ws: ServerConnection, verb: dict[str, Any]) -> None:
        self.set_muted(verb["unmute"], False)

    async def _on_board(self, ws: ServerConnection, verb: dict[str, Any]) -> None:
        # A split chat page shows the list too: from now on it gets the board's rows, exactly what
        # the widget gets.
        if ws not in self.clients:
            self.clients.add(ws)
            with contextlib.suppress(ConnectionClosed):
                await ws.send(self.payload())
                if self.presence is not None:
                    await ws.send(self.presence_frame())

    async def _on_follow(self, ws: ServerConnection, verb: dict[str, Any]) -> None:
        await answer(ws, {"follow": verb["follow"]}, self.follow(ws, verb["follow"]))

    async def _on_open(self, ws: ServerConnection, verb: dict[str, Any]) -> None:
        target, msg = parse_open(verb)
        await self.open_row(verb["open"], target, ws, msg)

    async def _on_authors(self, ws: ServerConnection, verb: dict[str, Any]) -> None:
        # A chat page's message authors, newest first: page data, so only org MRIs, and a bounded few.
        if self.presence is None:
            return
        shown = list(dict.fromkeys(p for m in verb["authors"] if (p := person(m))))[:_PRESENCE_AUTHORS]
        self._authors[ws] = shown
        self._follow_presence()
        with contextlib.suppress(ConnectionClosed):
            await ws.send(self.presence_frame())


def parse_open(verb: dict[str, Any]) -> tuple[OpenTarget, str]:
    """`open` verb → (where to open, message to land on); `chat` wins over `web`."""
    if verb.get("chat") is True:
        return ("split" if verb.get("chatnav") == "split" else "chat"), ""
    msg = verb.get("msg")
    msg = msg if isinstance(msg, str) and MSG_ID_RE.fullmatch(msg) else ""
    return ("web" if verb.get("web") is True else "app"), msg


def _verb_key(
    handlers: dict[str, Handler], accepts: dict[str, Callable[[Any], bool]], verb: dict[str, Any]
) -> str | None:
    """The first handled key the verb carries: with a thread id string, or a value its `accepts`
    check takes."""
    for key in handlers:
        if key in verb and accepts.get(key, _is_str)(verb[key]):
            return key
    return None


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _save_marks(path: Path | None, marks: dict[str, str]) -> None:
    if path is not None:
        write_private(path, json.dumps(marks, ensure_ascii=False, indent=0))


def _load_seen(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except OSError, ValueError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _page_path(name: str = "widget.html") -> Path:
    return Path(str(files("miniteams").joinpath(name)))


_UI = ("ui.css", "ui.js")  # shared by both pages, inlined at their `/* ui.css */` and `/* ui.js */`


def _version(name: str) -> str:
    return "-".join(str(_page_path(part).stat().st_mtime_ns) for part in (name, *_UI))


def page_version(name: str = "widget.html") -> str:
    try:
        return _version(name)
    except OSError:
        return ""


@functools.lru_cache(maxsize=4)
def _assembled(name: str, version: str) -> str:
    """`name` with the shared files inlined; `version` (the mtimes) keys the cache, so an edit shows."""
    page = _page_path(name).read_text(encoding="utf-8")
    for part in _UI:
        mark = f"/* {part} */"
        if page.count(mark) != 1:
            raise ValueError(f"{name}: needs exactly one {mark}")
        page = page.replace(mark, _page_path(part).read_text(encoding="utf-8"))
    return page


def _page(name: str = "widget.html") -> str:
    return _assembled(name, _version(name))


@functools.lru_cache(maxsize=4)
def _page_csp(page: str) -> str:
    """CSP for a served page: only its own script (by hash) runs, so an inline handler cannot.

    Server HTML reaches the pages' DOM (messages, rows): no foreign script, image or socket even if
    the sanitiser slips.
    """
    found = _SCRIPT_RE.search(page)
    digest = hashlib.sha256(found.group(1).encode() if found else b"").digest()
    # blob: the pasted images' local thumbnails (object URLs), never a remote image.
    return (
        f"default-src 'none'; script-src 'sha256-{base64.b64encode(digest).decode()}'; "
        "style-src 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    )


# Chat contents: no framing, no sniffing, no cross-origin embedding, no disk cache.
_PAGE_HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def _respond(ws: ServerConnection, body: bytes, headers: dict[str, str]) -> Response:
    resp = ws.respond(HTTPStatus.OK, "")
    resp.body = body
    for name, value in {**headers, "Content-Length": str(len(body))}.items():
        _put(resp, name, value)
    return resp


def _put(resp: Response, name: str, value: str) -> None:
    if name in resp.headers:  # respond() stamps text/plain; Headers is a multidict
        del resp.headers[name]
    resp.headers[name] = value


def _same(offered: str, expected: str) -> bool:
    # websockets decodes header bytes >= 0x80 with surrogateescape: a plain encode() would raise.
    return hmac.compare_digest(offered.encode(errors="surrogateescape"), expected.encode())


def _authorized(request: Request, secret: str) -> bool:
    """The request carries the `mt` cookie (compared in constant time)."""
    for header in request.headers.get_all("Cookie"):
        for pair in header.split(";"):
            name, _, value = pair.strip().partition("=")
            if name == _COOKIE and _same(value, secret):
                return True
    return False


def _set_cookie(resp: Response, secret: str) -> None:
    _put(resp, "Set-Cookie", f"{_COOKIE}={secret}; HttpOnly; SameSite=Strict; Path=/; Max-Age={_COOKIE_AGE}")
    _put(resp, "Cache-Control", "no-store")  # the response carries the secret


_FILE_HEADERS = {
    # An archived HTML or SVG opened directly must not run as this origin (it could open /ws).
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox",
    "Cross-Origin-Resource-Policy": "same-origin",
}


async def _process_request(
    board: Board, bound: str, ws: ServerConnection, request: Request
) -> Response | None:
    url = urlsplit(request.path)
    path = url.path  # the page's view params (?theme=…) ride the query string
    if path == "/ws":
        # Any local process can forge an Origin: only a browser holding the cookie gets through.
        if not _authorized(request, board.secret):
            return ws.respond(HTTPStatus.FORBIDDEN, "forbidden\n")
        return None  # proceed with the websocket upgrade; the Origin check runs next
    # DNS rebinding: a foreign name resolving to our address must not read a page, a chat or its files.
    if request.headers.get("Host") != bound:
        return ws.respond(HTTPStatus.NOT_FOUND, "not found\n")
    if path == "/auth":
        # A header, not the URL: no history, and only a preflight could carry it cross-site — never
        # answered, websockets serves GET alone.
        offered = request.headers.get_all(_TOKEN_HEADER)
        if len(offered) != 1 or not board.take_token(offered[0]):
            return ws.respond(HTTPStatus.FORBIDDEN, "forbidden\n")
        resp = ws.respond(HTTPStatus.NO_CONTENT, "")
        _set_cookie(resp, board.secret)
        return resp
    page_route = path == "/" or path.startswith("/chat/")
    authorized = _authorized(request, board.secret)
    if not (authorized or page_route):
        return ws.respond(HTTPStatus.FORBIDDEN, "forbidden\n")
    try:
        resp = await _route(board, ws, path, authorized)
    except ArchiveUnavailable:
        return ws.respond(HTTPStatus.SERVICE_UNAVAILABLE, "archive unavailable\n")
    if authorized and page_route and resp.status_code == HTTPStatus.OK:
        _set_cookie(resp, board.secret)  # a year from the last use, not from the launch
    return resp


async def _route(board: Board, ws: ServerConnection, path: str, authorized: bool) -> Response:
    if path == "/":
        page = _page("widget.html")
        return _respond(ws, page.encode(), _PAGE_HEADERS | {"Content-Security-Policy": _page_csp(page)})
    if path.startswith("/chat/"):
        thread_id = unquote(path.removeprefix("/chat/"))
        # Without the cookie the shell, unchecked: whether a chat exists is data.
        known = not authorized or thread_id in board.rows
        if known or await io("dir", archive_dir, board.data_dir, thread_id) is not None:
            page = _page("chat.html")
            return _respond(ws, page.encode(), _PAGE_HEADERS | {"Content-Security-Policy": _page_csp(page)})
    if path.startswith("/media/"):
        found = await board.chat.media(path)
        if found is not None:
            body, ctype = found
            headers = {"Content-Type": ctype, "Cache-Control": "private, max-age=3600"}
            return _respond(ws, body, headers | _FILE_HEADERS)
    if path.startswith("/emoji/"):
        found = await board.emojis.image(path.removeprefix("/emoji/"))
        if found is not None:
            body, ctype = found
            headers = {"Content-Type": ctype, "Cache-Control": "private, max-age=86400"}
            return _respond(ws, body, headers | _FILE_HEADERS)
    return ws.respond(HTTPStatus.NOT_FOUND, "not found\n")


async def serve_board(board: Board, settings: Settings, bind: str | None) -> None:
    for name in ("widget.html", "chat.html"):
        _page(name)  # a missing ui.js / ui.css or marker fails here, not on the first page load
    board.secret = load_secret(settings)  # never logged: it is every route's only credential
    host, port = load_bind(settings, bind)
    for attempt in range(_BIND_ATTEMPTS):
        bound = netloc(host, port)
        board.base_url = f"http://{bound}"
        try:
            # Only the page we serve may open the socket — any other site's Origin gets 403,
            # else every open tab could read the chat list (cross-site websocket hijacking).
            server = await serve(
                board.handle,
                host,
                port,
                origins=[Origin(board.base_url)],
                max_size=MAX_FRAME,
                process_request=functools.partial(_process_request, board, bound),
            )
            break
        except OSError as exc:
            if bind or attempt == _BIND_ATTEMPTS - 1:
                raise
            # Persisted port got taken by something else since the draw. Same host: the cookie is
            # scoped to it, a new address would sign every open page out.
            log.warning("bind_failed", host=host, port=port, error=str(exc))
            port = _draw_port()
    async with server:
        log.info("web_listening", url=f"{board.base_url}/", launch=f"file://{settings.launch_file}")
        # Once bound: a run failing on the port keeps the live one's web.json.
        board.publish = functools.partial(publish, settings, host, port, bool(bind))
        if not board.publish(board.token):
            print(f"{board.base_url}/#{board.token}", file=sys.stderr)  # this run's only way in
        await board.watch_page()  # until cancelled


def resolve_limit(limit: int | None, seeded: bool) -> int:
    """How deep the table goes. `--limit` wins, including `0` (no limit).

    Seeded rows cost no API call, so the default goes deeper when an archive is there; without one
    every row is fetched twice over and the same depth would mean minutes of blank page.
    """
    return limit if limit is not None else (_LIMIT_SEEDED if seeded else _LIMIT_BARE)


def _listed(settings: Settings, skype_token: str, limit: int) -> list[dict[str, Any]]:
    """In-scope conversations from the live listing, newest first, capped — one blocking walk."""
    picked: list[dict[str, Any]] = []
    for page in fetch_conversations(settings, skype_token):
        for conv in page:
            thread_id = str(conv.get("id") or "")
            if thread_id and in_scope(thread_id) and last_activity(conv):
                picked.append(conv)
                if limit and len(picked) >= limit:
                    return picked
    return picked


async def fill_from_listing(
    board: Board,
    settings: Settings,
    skype_token: str,
    directory: Directory,
    limit: int,
    me: str,
    seen: dict[str, str],
    seed: dict[str, dict[str, Any]],
) -> None:
    """Replace the seeded table with the live listing, head first; the page spins until it ends.

    Two passes so a widget with no archive shows something quickly: the head carries the mention
    scan, the tail continues at its rank so nothing is scanned twice.
    """
    started = _iso(int(datetime.now(UTC).timestamp() * 1000))
    try:
        convs = await asyncio.to_thread(_listed, settings, skype_token, limit)
        head = min(_FIRST_CHUNK, limit or _FIRST_CHUNK)
        board.replace(await bootstrap([convs[:head]], directory, 0, me, seen, seed), busy=True)
        if len(convs) > head:
            rest = await bootstrap([convs[head:]], directory, 0, me, seen, seed, rank_offset=head)
            board.replace(rest, busy=True)
        dropped = board.prune({str(conv.get("id") or "") for conv in convs}, started)
        log.info("web_bootstrap", rows=len(board.rows), listed=len(convs), seeded=len(seed), dropped=dropped)
    except Exception as exc:  # noqa: BLE001 — a failed fill leaves the seeded table, not a dead page
        log.error("web_bootstrap_failed", error=failure(exc), error_type=type(exc).__name__)
    finally:
        board.set_busy(False)  # a spinner that never stops is worse than no spinner


async def run(
    settings: Settings,
    skype_token: str,
    bearer: str,
    limit: int | None,
    bind: str | None,
    reactions: bool = False,
    opener: str = "xdg-open",
    open_scheme: str = "msteams",
    browser: str = "",
    me: str = "",
    data_dir: Path | None = None,
) -> None:
    # Ctrl-C while an archive read hangs on a dead mount must still exit: see `_leave_if_stuck`.
    try:
        directory = Directory(settings)
        directory.set_token(skype_token, bearer)
        directory.me = me
        seen_path = settings.config_dir / "seen.json"
        seen = _load_seen(seen_path)
        seed: dict[str, dict[str, Any]] = {}
        stamp = _NO_INDEX
        if data_dir is not None:
            try:
                stamp, read = await io("index", _reseed, data_dir, me, None)
                seed = read or {}
            except ArchiveUnavailable:
                log.warning("archive_index_timeout", data_dir=str(data_dir))  # the listing fills the table
        cap = resolve_limit(limit, seeded=bool(seed))
        board = Board(
            seeded_rows(seed, cap),
            directory,
            reactions=reactions,
            seen_path=seen_path,
            muted_path=settings.config_dir / "muted.json",
            opener=None if opener in ("", "none") else opener.split(),
            open_scheme=open_scheme,
            browser=None if browser in ("", "none") else browser.split(),
            me=me,
            busy=True,
            seed=seed,
            data_dir=data_dir,
            seed_stamp=stamp,
        )
        log.info("web_seeded", rows=len(board.rows), seeded=len(seed), limit=cap, me=bool(me))
        cache = open_cache(settings)
        presence = board.presence = Presence(
            settings,
            # Lazy: building the MSAL app discovers its authority online.
            lambda: token_source(settings).presence_token(),
            board.broadcast_presence,
            cache,
        )
        receipts = board.receipts = Receipts(directory, me, board._receipts_frame, cache)
        renewals = asyncio.create_task(presence.run())
        board._follow_presence()  # the seeded rows' peers, before any event moves a row
        # The stream waits out a dead login (see run_forever) and returns only on failure: a page that
        # silently stops updating is worse than an exit, so the server goes down with it.
        server = asyncio.create_task(serve_board(board, settings, bind))
        # The page is up before the listing is walked: it fills in, it does not wait.
        fill = asyncio.create_task(
            fill_from_listing(board, settings, skype_token, directory, cap, me, seen, seed)
        )
        # Own endpoint id: sharing `stream`'s would make whichever registered last the only receiver.
        stream = asyncio.create_task(
            run_forever(
                settings,
                on_event=board.on_event,
                directory=directory,
                epid_name="endpoint_id-web",
                wait_for_login=True,  # a `miniteams login` elsewhere brings the widget back
                on_gap=board.on_gap,
                on_session=presence.on_session,
                on_presence=presence.on_presence,
            )
        )
        emojis = asyncio.create_task(board.emojis.refresh())
        try:
            done, pending = await asyncio.wait({server, stream}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()  # re-raise a server failure (e.g. bind) instead of exiting 0
            raise RuntimeError("live stream ended — re-run")
        finally:
            fill.cancel()  # the page is going away; a half-finished walk has nobody to tell
            emojis.cancel()
            renewals.cancel()
            board.close()
            await receipts.close()  # before the presence close: that one closes the shared cache
            with contextlib.suppress(Exception):
                await asyncio.wait_for(presence.close(), 5)  # unsubscribe; a hung post must not hold the exit
    finally:
        _leave_if_stuck()


def _leave_if_stuck() -> None:
    """Close the archive pools; exit at once when archive threads outlive the run (see `stuck_exit_code`)."""
    exc = sys.exception()
    close_archive_pool()
    try:
        stuck = stuck_archive_threads()
    except KeyboardInterrupt:  # a second Ctrl-C during the grace join must not skip the exit
        stuck = stuck_archive_threads(0)
    code = stuck_exit_code(exc, stuck)
    if code is not None:
        error = failure(exc) if isinstance(exc, Exception) else None
        log.error("archive_threads_stuck", threads=stuck, exit_code=code, error=error)
        for out in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):
                out.flush()
        os._exit(code)  # the interpreter's exit joins the pool's threads: a dead mount would hold it


def stuck_exit_code(exc: BaseException | None, stuck: int) -> int | None:
    """The code to leave with at once when `stuck` archive threads outlive the run, as `main` would
    have exited after `exc`; None when there are none."""
    if not stuck:
        return None
    return 0 if exc is None or isinstance(exc, KeyboardInterrupt | asyncio.CancelledError) else 1
