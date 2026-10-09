"""`miniteams web`: one local page listing conversations newest-activity-first (spec 002).

One process, one port: `websockets.serve` answers `GET /` with the bundled page and upgrades
`/ws`, over which the server pushes the whole row list (small: `--limit` rows) on connect and
on every change. No REST layer — the only client verb (`seen`) rides the same socket.

Bind is `127.0.0.X:PORT` with X and PORT drawn once and persisted (`web.json`) so the URL
survives restarts; the whole 127/8 is loopback on Linux, no interface setup needed.
"""

import asyncio
import base64
import contextlib
import functools
import hashlib
import html
import ipaddress
import json
import mimetypes
import random
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from datetime import UTC, datetime
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx
import structlog
from websockets.asyncio.server import ServerConnection, broadcast, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.typing import Origin

from .archive_store import find_chat_dir, read_chats, read_messages, read_page
from .chats import fetch_conversations, is_meeting, is_private, last_activity
from .config import Settings
from .directory import Directory
from .dump import _epoch_seconds, fetch_history
from .messages import _TAG_RE, EMOJI, thread_of
from .render import cards_html, media_url, sanitize
from .send import delete_message, edit_message, mark_read, message_html, react, send_message
from .stream import run_forever

log = structlog.get_logger()

_YOU = "You"  # own messages, as Teams' chat list shows them
_SNIPPET_LEN = 1000  # the row tooltip shows it whole; CSS truncates the visible line
_EMOJI_ALT_RE = re.compile(r'<emoji\b[^>]*\balt="([^"]*)"[^>]*>')
# A forward is a blockquote too, but its content IS the message — only a reply quotes another.
_REPLY_QUOTE_RE = re.compile(r"\A\s*<blockquote\b(?![^>]*Forward)[^>]*>.*?</blockquote>", re.DOTALL)
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
_SEND_MAX_BYTES = 28 * 1024  # Teams' per-message limit
_TYPING_TTL = 10.0  # seconds; Teams does not always send ClearTyping
_CHAT_PAGE = 100  # history messages per page of the chat view
_SEARCH_HITS = 50  # search hits per answer
_CHAT_PAGE_MAX = 200  # the archive query is cheap, the page's DOM is not
_MEDIA_MAX = 50 * 1024 * 1024  # ponytail: whole-body responses; stream if videos ever get linked
_CURSOR_RE = re.compile(
    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z?", re.ASCII
)  # a composetime, nothing after it
_MSG_ID_RE = re.compile(r"[0-9]{1,20}")  # Teams message ids are ms-epoch digits
_QUOTE_MAX = 10  # quotes in one reply
_PREVIEW_LEN = 100  # characters of the quoted message a Reply block carries
_ACTIONS = ("react", "edit", "delete")  # page verbs on one message
_TEXT_TYPES = ("RichText/Html", "Text")  # what a person wrote: quotable, editable
_SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.DOTALL)
# Thread activity that changes the label (topic, member count): drop the cached thread info.
# Someone reading a chat is not activity: bumping the row on it would defeat "seen".
_SILENT_TYPES = {"ThreadActivity/MemberConsumptionHorizonUpdate"}
_RELABEL_TYPES = {"ThreadActivity/TopicUpdate", "ThreadActivity/AddMember", "ThreadActivity/DeleteMember"}
_BIND_ATTEMPTS = 3  # redraws when the persisted port turns out taken
# Non-text last messages: a short marker instead of the raw HTML/card payload.
_TYPE_MARKERS = {
    "RichText/Media_GenericFile": "📎 file",
    "RichText/UriObject": "🖼 image",
    "RichText/Media_Card": "🃏 card",
    "RichText/Media_CallRecording": "🎥 recording",
    "RichText/Media_LocalRecording": "🎥 recording",
    "ThreadActivity/AddMember": "👥 member added",
    "ThreadActivity/DeleteMember": "👥 member removed",
    "ThreadActivity/TopicUpdate": "✎ renamed",
    "ThreadActivity/MemberConsumptionHorizonUpdate": "👁 read marker",
    "Event/Call": "📞 call started",
}


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


def self_mri(access_token: str) -> str:
    """`8:orgid:<oid>` from the AAD access token's claims; "" when they cannot be read.

    Our own token, already trusted for the API calls it authorises — the payload is decoded
    without signature verification only to learn who we are (the silent refresh returns no
    id_token)."""
    try:
        payload = access_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        oid = str(claims.get("oid") or "")
    except IndexError, ValueError, AttributeError:
        return ""
    return f"8:orgid:{oid}" if oid else ""


def mention_kind(resource: dict[str, Any], me: str) -> str | None:
    """`"me"` when the message @mentions `me` by name, `"all"` for an @everyone, else None.

    `properties.mentions` is a JSON string (or list) of entries; `bot` and `share-contact`
    entries are not mentions of a person. A message from `me` never counts."""
    if not me or str(resource.get("from") or "").endswith(me):
        return None
    kind = None
    for entry in _json_list((resource.get("properties") or {}).get("mentions")):
        if not isinstance(entry, dict):
            continue
        mtype = str(entry.get("mentionType") or "")
        if mtype == "person" and entry.get("mri") == me:
            return "me"
        if mtype == "everyone":
            kind = "all"  # the entry's mri is the thread itself; a `me` entry still wins
    return kind


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


def _json_list(value: Any) -> list[Any]:
    """`properties.files` / `.cards` arrive as JSON *strings* (`'[]'` is truthy) or as lists."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _plain(content: str) -> str:
    """HTML body as one line of text.

    Emoji are tags whose `alt` holds the character; tags then become spaces, not nothing:
    `<at>Bob</at>dis` would otherwise read "Bobdis".
    """
    return " ".join(html.unescape(_TAG_RE.sub(" ", _EMOJI_ALT_RE.sub(r"\1", content))).split())


def snippet(msgtype: str, content: str, props: dict[str, Any] | None = None) -> str:
    """One-line preview of a message body, marker for non-text types, capped at _SNIPPET_LEN."""
    props = props or {}
    if msgtype in ("RichText/Html", "Text"):
        text = content
        if msgtype == "RichText/Html":
            # A reply repeats the quoted message before its own body; preview what was written,
            # unless the reply is nothing but the quote.
            body = _REPLY_QUOTE_RE.sub("", content, count=1)
            text = _plain(body) or _plain(content)
        text = " ".join(text.split())
        if not text:
            # A file/card post has an empty body and its payload in properties; the list stub of
            # a deleted message has neither; a body that strips to nothing is an image tag.
            names = [
                str(f.get("fileName") or "file")
                for f in _json_list(props.get("files"))
                if isinstance(f, dict)
            ]
            if names:
                text = "📎 " + ", ".join(names)
            elif _json_list(props.get("cards")):
                text = "🃏 card"
            elif not content.strip():
                text = "🗑 deleted"
            else:
                text = "🖼 image" if "<img" in content else "📎 attachment"
    elif msgtype == "Event/Call" and "<ended/>" in content:
        text = "📞 call ended"
    else:
        text = (_TYPE_MARKERS.get(msgtype) or f"[{msgtype.rsplit('/', 1)[-1]}]") if msgtype else ""
    text = " ".join(text.split())
    return text if len(text) <= _SNIPPET_LEN else text[: _SNIPPET_LEN - 1] + "…"


def _record_type(msgtype: str) -> str:
    if msgtype in ("RichText/Html", "Text"):
        return "text"
    if msgtype == "Event/Call":
        return "call"
    return "system" if _is_system(msgtype) else "other"


def chat_record(
    message: dict[str, Any], thread_id: str, me: str = "", media_dir: Path | None = None
) -> dict[str, Any]:
    """One message as the chat page shows it (spec 011): sanitised body, reactions, mention kind."""
    props = message.get("properties")
    props = props if isinstance(props, dict) else {}
    message = {**message, "properties": props}  # mention_kind reads it too
    msgtype = str(message.get("messagetype") or "")
    content = str(message.get("content") or "")
    kind = _record_type(msgtype)
    mri = str(message.get("from") or "").rsplit("/", 1)[-1]
    deleted = _deleted(props)
    body = ""
    if not deleted:
        if msgtype == "RichText/Html":
            mentions = {
                str(m.get("itemid")): str(m.get("mri") or "")
                for m in _json_list(props.get("mentions"))
                if isinstance(m, dict)
            }
            body = sanitize(content, thread_id, media_dir, me, mentions)
        elif msgtype == "Text":
            body = html.escape(content)
        if msgtype == "RichText/Media_Card" or props.get("cards"):
            # Appended after sanitize(), never through it: the card markup is its own allowlist.
            cards = cards_html(message, media_url=lambda u: media_url(thread_id, media_dir, u))
            body += cards
            kind = "text" if cards and kind == "other" else kind
        body = body or html.escape(snippet(msgtype, content, props))  # file/card posts, markers
    reactions = []
    for emotion in _json_list(props.get("emotions")):
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
                    "mine": any(_is_me(u, me) for u in users),
                }
            )
    return {
        "id": str(message.get("id") or ""),
        "at": str(message.get("composetime") or message.get("originalarrivaltime") or ""),
        "from": mri,
        "name": str(message.get("imdisplayname") or ""),
        "me": _is_me(mri, me),
        "type": kind,
        "editable": msgtype in _TEXT_TYPES,
        "html": body,
        "edited": bool(message.get("skypeeditedid") or props.get("edittime")),
        "deleted": deleted,
        "reactions": reactions,
        "mentions": mention_kind(message, me),
    }


def reply_quote(message: dict[str, Any], name: str) -> str:
    """Teams' Reply block quoting `message`, in the shape the archive holds (spec 011)."""
    msg_id = html.escape(str(message.get("id") or ""))
    mri = html.escape(str(message.get("from") or "").rsplit("/", 1)[-1])
    props = message.get("properties")
    msgtype, content = str(message.get("messagetype") or ""), str(message.get("content") or "")
    preview = snippet(msgtype, content, props if isinstance(props, dict) else None)[:_PREVIEW_LEN]
    return (
        f'<blockquote itemscope itemtype="http://schema.skype.com/Reply" itemid="{msg_id}">'
        f'<strong itemprop="mri" itemid="{mri}">{html.escape(name)}</strong>'
        f'<span itemprop="time" itemid="{msg_id}"></span>'
        f'<p itemprop="preview">{html.escape(preview)}</p></blockquote>'
    )


_APP_MRI = "28:"  # bots and app installs sit in the roster like people do


def _seed_label(chat: dict[str, Any], me: str) -> str:
    """The name the widget would have fetched: topic, else the roster without me.

    The archive stores every participant, me included; `Directory.label` leaves me out, so reusing
    the stored label verbatim would name every 1:1 after its two members.
    """
    members = [m for m in chat["participants"] if isinstance(m, dict) and m.get("name")]
    if chat["topic"]:
        return f"{chat['topic']} · {len(chat['participants'])}p"

    def is_app(member: dict[str, Any]) -> bool:
        return str(member.get("mri", "")).startswith(_APP_MRI)

    others = [str(m["name"]) for m in members if m.get("mri") != me and not is_app(m)]
    apps = [str(m["name"]) for m in members if is_app(m)]
    return ", ".join(others or apps or [str(m["name"]) for m in members])


def _index_stamp(data_dir: Path | None) -> tuple[int, int, int]:
    """Identity of the index file's content; (0, 0, 0) when there is none to read."""
    if data_dir is None:
        return (0, 0, 0)
    try:
        st = (data_dir / "index.db").stat()
    except OSError:
        return (0, 0, 0)
    return (st.st_ino, st.st_mtime_ns, st.st_size)


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
                if _is_me(str(last.get("from") or ""), me)
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
    sender = "" if _is_system(msgtype) else await sender_of(last, directory)
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


def _is_system(msgtype: str) -> bool:
    """Thread activity (member changes, read markers…) is emitted by the thread, not a person."""
    return msgtype.startswith("ThreadActivity/")


def _is_me(mri: str, me: str) -> bool:
    """`from` is a contact URL ending in the MRI; a call event names the bare MRI."""
    return bool(me) and mri.rsplit("/", 1)[-1] == me


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
        return _YOU if _is_me(mri, directory.me) else await directory.display(mri)
    mri = str(resource.get("from") or "")
    name = resource.get("imdisplayname")
    if directory is None:
        return str(name or mri)
    directory.note_name(mri, name)
    if _is_me(mri, directory.me):
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
        log.debug("history_fetch_failed", thread=thread_id, error=str(exc))
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
    if not _is_system(msgtype):
        row["sender"] = await sender_of(message, directory)


# --- bind address ---


def parse_bind(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"expected ADDR:PORT, got {value!r}")
    # The page has no auth: anything but loopback would publish the chat list to the network.
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError(f"refusing non-loopback bind {host!r}")
    return host, int(port)


def _draw_bind() -> tuple[str, int]:
    # 127.0.0.1 is what every other local daemon binds; a random sibling avoids collisions and
    # keeps the widget off the well-known address (user request).
    return f"127.0.0.{random.randint(2, 254)}", random.randint(32768, 60999)


def load_bind(settings: Settings, override: str | None = None) -> tuple[str, int]:
    """`--bind` wins; else the persisted draw; else draw now and persist."""
    if override:
        return parse_bind(override)
    path = settings.config_dir / "web.json"
    try:
        data = json.loads(path.read_text())
        return str(data["host"]), int(data["port"])
    except OSError, ValueError, KeyError, TypeError:
        pass
    host, port = _draw_bind()
    save_bind(settings, host, port)
    return host, port


def save_bind(settings: Settings, host: str, port: int) -> None:
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    (settings.config_dir / "web.json").write_text(json.dumps({"host": host, "port": port}))


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
        self.clients: set[ServerConnection] = set()
        self.busy = busy  # the page shows a spinner until the background fill says otherwise
        self.seed = seed or {}
        self.data_dir = data_dir
        self._seed_read = time.monotonic()
        self._seed_stamp = _index_stamp(data_dir)
        self._replies: set[asyncio.Task[None]] = set()  # the loop keeps only weak refs to tasks
        self._typing_timers: dict[tuple[str, str], asyncio.TimerHandle] = {}
        self._follows: dict[ServerConnection, tuple[str, Path | None]] = {}  # socket → (thread, media dir)
        self._history_busy: set[ServerConnection] = set()  # sockets with a history answer pending
        self._action_busy: set[ServerConnection] = set()  # sockets with a message action pending
        self._search_busy: set[ServerConnection] = set()  # sockets with a search pending
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
        name = str(self.seed.get(thread_id, {}).get("label") or "")
        if name or self.data_dir is None:
            return name
        stamp = _index_stamp(self.data_dir)
        if stamp == self._seed_stamp or time.monotonic() - self._seed_read < _SEED_REREAD:
            return ""  # same file, or too soon: re-reading it would buy nothing
        # Claimed before the await, not after: a burst of unnamed chats (a reconnect replaying what
        # it dropped) would otherwise start one 12 MiB read each, all of them reading the same file.
        self._seed_read, self._seed_stamp = time.monotonic(), stamp
        self.seed = await asyncio.to_thread(seed_from_archive, self.data_dir, self.me)
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
            log.warning("mark_read_failed", thread=thread_id, error=str(exc))
            return False
        return True

    async def send(
        self, thread_id: str, text: Any, ws: ServerConnection | None = None, quote: Any = None
    ) -> str | None:
        """Page verb `send`: post `text` to the row's chat (or the chat `ws` follows) as me, after
        one Reply block per `quote` id. Returns the refusal/failure reason, None once Teams accepted it.

        The row is left alone: the Trouter echo of the sent message updates it like any other."""
        if not self._may_post(ws, thread_id):
            return "unknown chat"
        if error := _text_error(text):
            return error
        quote = [] if quote is None else quote
        if not isinstance(quote, list):
            return "invalid quote"
        if len(quote) > _QUOTE_MAX:
            return "too many quotes"
        ids_ok = all(isinstance(q, str) and _MSG_ID_RE.fullmatch(q) for q in quote)
        if not ids_ok or len(set(quote)) < len(quote):
            return "invalid quote"
        if self.directory is None:
            return "not connected"
        try:
            extra: dict[str, Any] = {}
            if quote:
                copies = await asyncio.to_thread(self._server_copies, thread_id, quote)
                if any(q not in copies for q in quote):
                    return "unknown quoted message"
                if any(
                    not copies[q].get("from") or copies[q].get("messagetype") not in _TEXT_TYPES
                    for q in quote
                ):
                    return "cannot quote this message"
                text = "".join(self._reply_quote(copies[q]) for q in quote) + message_html(text, False)
                if len(text.encode()) > _SEND_MAX_BYTES:
                    return "message too long"
                extra["is_html"] = True
            await asyncio.to_thread(
                send_message,
                self.directory.settings,
                self.directory.skype_token,
                thread_id,
                text,
                self.me_name,
                **extra,
            )
        except Exception as exc:  # noqa: BLE001 — a failed send must not drop the socket
            reason = _failure(exc)
            log.warning("send_failed", thread=thread_id, error=reason, error_type=type(exc).__name__)
            return reason
        return None

    # --- chat view (spec 011) ---

    async def act(self, ws: ServerConnection | None, verb: str, request: dict[str, Any]) -> dict[str, Any]:
        """Page verbs `react`, `edit`, `delete` on one message of a chat `send` may post to."""
        thread_id, msg_id = request[verb], request.get("msg")
        error = await self._act(ws, verb, request, thread_id, msg_id)
        head = {verb: thread_id, "msg": msg_id if isinstance(msg_id, str) else None}
        return {**head, **({"error": error} if error else {"ok": True})}

    async def _act(
        self, ws: ServerConnection | None, verb: str, request: dict[str, Any], thread_id: str, msg_id: Any
    ) -> str | None:
        if not isinstance(msg_id, str) or not _MSG_ID_RE.fullmatch(msg_id):
            return "invalid message"
        if not self._may_post(ws, thread_id):
            return "unknown chat"
        key, remove, text = request.get("key"), request.get("remove", False), request.get("text")
        if verb == "react" and not (isinstance(key, str) and key in EMOJI and isinstance(remove, bool)):
            return "invalid reaction"
        if verb == "edit" and (error := _text_error(text)):
            return error
        if self.directory is None:
            return "not connected"
        settings, token = self.directory.settings, self.directory.skype_token
        try:
            copy = (await asyncio.to_thread(self._server_copies, thread_id, [msg_id])).get(msg_id)
            if copy is None:
                return "unknown message"
            # The server's copy decides: the page could name anyone's message.
            if verb != "react" and not _is_me(str(copy.get("from") or ""), self.me):
                return "not your message"
            props = copy.get("properties")
            if verb != "react" and _deleted(props if isinstance(props, dict) else {}):
                return "message deleted"
            if verb == "edit" and copy.get("messagetype") not in _TEXT_TYPES:
                return "cannot edit this message"
            if verb == "react":
                await asyncio.to_thread(react, settings, token, thread_id, msg_id, str(key), remove=remove)
            elif verb == "edit":
                await asyncio.to_thread(edit_message, settings, token, thread_id, msg_id, str(text))
            else:
                await asyncio.to_thread(delete_message, settings, token, thread_id, msg_id)
        except Exception as exc:  # noqa: BLE001 — a failed action must not drop the socket
            reason = _failure(exc)
            log.warning(
                "message_action_failed",
                verb=verb,
                thread=thread_id,
                message_id=msg_id,
                error=reason,
                error_type=type(exc).__name__,
            )
            return reason
        return None

    def _may_post(self, ws: ServerConnection | None, thread_id: str) -> bool:
        # Off the board, only a followed chat of the widget's scope: never a channel.
        return thread_id in self.rows or (in_scope(thread_id) and self._following(ws, thread_id))

    def _server_copies(self, thread_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Messages `ids` of the thread as the server holds them, by id: the archive, then the latest
        API page for those it lacks (sent after the archiver's last write). Blocking: worker thread."""
        found: dict[str, dict[str, Any]] = {}
        chat_dir = self._archive_dir(thread_id)
        if chat_dir is not None:
            try:
                found = read_messages(chat_dir, ids)
            except (OSError, sqlite3.Error) as exc:
                if not isinstance(exc, FileNotFoundError):
                    log.warning("archive_message_lookup_failed", thread=thread_id, error=str(exc))
        if len(found) < len(ids) and self.directory is not None:
            page = fetch_history(
                self.directory.settings, self.directory.skype_token, thread_id, _CHAT_PAGE_MAX, 1
            )
            for message in page:
                msg_id = str(message.get("id") or "")
                if msg_id in ids:
                    found.setdefault(msg_id, message)
        return found

    def _reply_quote(self, message: dict[str, Any]) -> str:
        mri = str(message.get("from") or "").rsplit("/", 1)[-1]
        name = str(message.get("imdisplayname") or "") or (
            self.directory.name_for(mri) if self.directory else mri
        )
        return reply_quote(message, name)

    def _following(self, ws: ServerConnection | None, thread_id: str) -> bool:
        return ws is not None and self._follows.get(ws, ("", None))[0] == thread_id

    def media_file(self, path: str) -> tuple[bytes, str] | None:
        """`/media/<thread>/<file>`: (body, content type) of an archived asset, None for anything
        else. Blocking (index lookup, NFS read): run it in a worker thread."""
        parts = path.removeprefix("/media/").split("/")
        if len(parts) != 2:
            return None
        thread_id, name = (unquote(part) for part in parts)
        chat_dir = self._archive_dir(thread_id)
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

    def _archive_dir(self, thread_id: str) -> Path | None:
        """The chat's archive folder; None when the archive does not hold it or cannot be read."""
        if self.data_dir is None:
            return None
        try:
            return find_chat_dir(self.data_dir, thread_id)
        except (OSError, sqlite3.Error) as exc:
            log.info("archive_chat_lookup_failed", thread=thread_id, error=str(exc))
            return None

    def _records(
        self, messages: list[dict[str, Any]], thread_id: str, media: Path | None
    ) -> list[dict[str, Any]]:
        """Records of a page, in a worker thread: sanitising globs the media folder (NFS)."""
        records = []
        for message in messages:
            try:
                records.append(chat_record(message, thread_id, self.me, media))
            except Exception as exc:  # noqa: BLE001 — one malformed message must not lose the page
                log.warning("chat_record_skipped", thread=thread_id, error_type=type(exc).__name__)
        return records

    def _archive_records(
        self, chat_dir: Path, thread_id: str, cursor: tuple[str, str], size: int
    ) -> tuple[list[dict[str, Any]], bool, bool]:
        messages, older, newer = read_page(chat_dir, size, **dict([cursor]))
        return self._records(messages, thread_id, chat_dir / "media"), older, newer

    def _api_records(
        self, thread_id: str, before: str, size: int, media: Path | None
    ) -> tuple[list[dict[str, Any]], bool]:
        assert self.directory is not None
        # endTime is second-granular: ask up to the next second, then cut at `before` exactly.
        end = _epoch_seconds(before) + 1 if before else None
        page = fetch_history(self.directory.settings, self.directory.skype_token, thread_id, size, 1, end)
        kept = [m for m in page if not before or str(m.get("composetime") or "") < before]
        # A short page is not the end (the API under-fills windows): only an empty one is.
        return self._records(kept[-size:], thread_id, media), bool(kept)

    async def history(
        self, thread_id: str, before: Any = None, limit: Any = None, *, after: Any = None, around: Any = None
    ) -> dict[str, Any]:
        """Page verb `history`: a page of messages, archive first, else one API page.

        The answer echoes its cursor (`before`, `after` or `around`) so the page can match it to its
        request."""
        cursors = {"before": before, "after": after, "around": around}
        reply = _history_head(thread_id, cursors)
        try:
            return {**reply, **await self._history(thread_id, cursors, limit)}
        except Exception as exc:  # noqa: BLE001 — the page waits for an answer whatever happens
            log.warning("history_failed", thread=thread_id, error_type=type(exc).__name__)
            return {**reply, "error": "history unavailable"}

    async def _history(self, thread_id: str, cursors: dict[str, Any], limit: Any) -> dict[str, Any]:
        given = {k: v for k, v in cursors.items() if v not in (None, "")}  # "" = latest, like null
        for key, value in given.items():
            if not (isinstance(value, str) and _CURSOR_RE.fullmatch(value)):
                return {"error": f"invalid {key}"}
        if len(given) > 1:
            return {"error": "one cursor at most"}
        key, at = next(iter(given.items()), ("before", ""))
        valid = isinstance(limit, int) and not isinstance(limit, bool) and limit > 0
        size = min(limit if valid else _CHAT_PAGE, _CHAT_PAGE_MAX)
        chat_dir = await asyncio.to_thread(self._archive_dir, thread_id)
        if chat_dir is None and thread_id not in self.rows:
            return {"error": "unknown chat"}
        try:
            if chat_dir is None:
                raise FileNotFoundError(thread_id)
            records, older, newer = await asyncio.to_thread(
                self._archive_records, chat_dir, thread_id, (key, at), size
            )
            source = "archive"
        except (OSError, sqlite3.Error) as exc:
            if not isinstance(exc, FileNotFoundError):
                log.warning("archive_history_failed", thread=thread_id, error=str(exc))
            if key != "before":
                return {"error": "not available without archive"}
            if self.directory is None:
                return {"error": "not connected"}
            media = chat_dir / "media" if chat_dir else None
            try:
                records, older = await asyncio.to_thread(self._api_records, thread_id, at, size, media)
            except Exception as api_exc:  # noqa: BLE001 — a failed page must not drop the socket
                reason = _failure(api_exc)
                log.warning("history_api_failed", thread=thread_id, error=reason)
                return {"error": reason}
            source, newer = "api", False
        return {"messages": records, "older": older, "newer": newer, "source": source}

    async def _history_reply(self, ws: ServerConnection, verb: dict[str, Any]) -> None:
        try:
            await self._answer(
                ws,
                self.history(
                    verb["history"],
                    verb.get("before"),
                    verb.get("limit"),
                    after=verb.get("after"),
                    around=verb.get("around"),
                ),
            )
        finally:
            self._history_busy.discard(ws)

    async def search(self, thread_id: str, query: Any) -> dict[str, Any]:
        """Page verb `search`: the thread's archived messages matching `query`, newest first."""
        head = {"search": thread_id, "q": query if isinstance(query, str) else None}
        if not isinstance(query, str) or not query.strip():
            return {**head, "error": "empty query"}
        chat_dir = await asyncio.to_thread(self._archive_dir, thread_id)
        if (
            self.data_dir is None
            or chat_dir is None
            or not await asyncio.to_thread((chat_dir / "messages.db").is_file)
        ):
            return {**head, "error": "no archive for this chat"}
        # Imported here: mcp imports this module.
        from .mcp import Archive

        try:
            found = await asyncio.to_thread(
                Archive(self.data_dir).search_messages, query.strip(), "", "", thread_id, _SEARCH_HITS
            )
        except Exception as exc:  # noqa: BLE001 — the page waits for an answer whatever happens
            log.warning("search_failed", thread=thread_id, error_type=type(exc).__name__)
            return {**head, "error": "search unavailable"}
        hits = [
            {"id": h["id"], "at": h["time"], "name": h["sender"], "text": h["text"]}
            for h in found["messages"]
        ]
        return {**head, "hits": hits}

    async def follow(self, ws: ServerConnection, thread_id: str) -> dict[str, Any]:
        """Page verb `follow`: this socket receives the thread's live messages from now on."""
        chat_dir = await asyncio.to_thread(self._archive_dir, thread_id)
        if chat_dir is None and thread_id not in self.rows:
            return {"follow": thread_id, "error": "unknown chat"}
        self._follows[ws] = (thread_id, chat_dir / "media" if chat_dir else None)
        row = self.rows.get(thread_id)
        label = str(row.get("label") or "") if row else await self.archived_label(thread_id)
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
            if _is_me(str(resource.get("from") or "").rsplit("/", 1)[-1], self.me):
                return
            name = await sender_of(resource, self.directory)
            if msgtype == "Control/Typing":
                self._typing_arm(thread_id, name)
                self._relay(thread_id, {"typing": name, "on": True})
            else:
                self._typing_stop(thread_id, name)  # relays the clear when this name was typing
            return
        if resource_type not in ("NewMessage", "MessageUpdate"):
            return
        if msgtype.startswith("Control/") or msgtype in _SILENT_TYPES:
            return
        record = await asyncio.to_thread(chat_record, resource, thread_id, self.me, media)
        if "content" not in resource:
            del record["html"]  # a partial update (reaction): the page keeps the body it has
        if resource_type == "NewMessage" and not _is_system(msgtype):
            self._typing_stop(thread_id, await sender_of(resource, self.directory))  # their message ends it
        self._relay(thread_id, {"msg" if resource_type == "NewMessage" else "edit": record})

    def _relay(self, thread_id: str, frame: dict[str, Any]) -> None:
        followers = [ws for ws, (tid, _) in self._follows.items() if tid == thread_id]
        if followers:
            # Named: a page switching chats may still receive frames of the one it left.
            broadcast(followers, json.dumps({**frame, "thread": thread_id}, ensure_ascii=False))

    def _background(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._replies.add(task)
        task.add_done_callback(self._replies.discard)

    async def _answer(self, ws: ServerConnection, work: Awaitable[dict[str, Any]]) -> None:
        answer = await work
        # A page closed while its answer was computed is not a handler failure.
        with contextlib.suppress(ConnectionClosed):
            await ws.send(json.dumps(answer, ensure_ascii=False))

    async def _sent(self, ws: ServerConnection, thread_id: str, text: Any, quote: Any) -> dict[str, Any]:
        # Only the sending page learns the outcome; every page sees the echoed message.
        error = await self.send(thread_id, text, ws, quote)
        return {"sent": thread_id, **({"error": error} if error else {"ok": True})}

    async def _one_at_a_time(
        self,
        ws: ServerConnection,
        head: dict[str, Any],
        work: Callable[[], Awaitable[dict[str, Any]]],
        slots: set[ServerConnection] | None = None,
    ) -> dict[str, Any]:
        """One message action (or quoted send, or search) per socket: another while it runs answers
        `busy`."""
        slots = self._action_busy if slots is None else slots
        if ws in slots:
            return {**head, "error": "busy"}
        slots.add(ws)
        try:
            return await work()
        finally:
            slots.discard(ws)

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
        await self._board_event(thread_id, resource_type, resource, msgtype)
        if self._follows:  # after the board: a relay failure must not cost the row its update
            try:
                await self._forward(thread_id, resource_type, resource, msgtype)
            except Exception as exc:  # noqa: BLE001 — a malformed event must not stop the stream
                log.warning("follow_relay_failed", thread=thread_id, error_type=type(exc).__name__)

    async def _board_event(
        self, thread_id: str, resource_type: Any, resource: dict[str, Any], msgtype: str
    ) -> None:
        if not in_scope(thread_id):
            return
        if resource_type == "ConversationUpdate":
            self._conversation_update(thread_id, resource)
        elif resource_type == "NewMessage":
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
        sender = "" if _is_system(msgtype) else await sender_of(resource, self.directory)
        # A bare-id label is a failed lookup: retry it (Directory spaces transient retries 60s apart).
        if self.directory is not None and (msgtype in _RELABEL_TYPES or row["label"] == thread_id):
            if msgtype in _RELABEL_TYPES:
                self.directory.forget(thread_id)
            fresh = await self.directory.label(thread_id)
            if fresh != thread_id:  # lookup failed (removed from the chat, 429): keep the old name
                row["label"] = fresh
            elif row["label"] == thread_id:
                row["label"] = await self.archived_label(thread_id) or row["label"]
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
        self._typing_stop(thread_id, sender)  # their message is the end of their typing
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
        edited = bool(resource.get("skypeeditedid") or props.get("edittime"))
        deleted = bool(props.get("deletetime"))
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
                    candidate = (int(u.get("time") or 0), EMOJI.get(key, f":{key}:"), mri)
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
            self._typing_stop(thread_id, name)
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

    def _typing_stop(self, thread_id: str, name: str) -> None:
        timer = self._typing_timers.pop((thread_id, name), None)
        if timer:
            timer.cancel()
            if name != _YOU:  # my own typing on another device is not news to the page
                self._relay(thread_id, {"typing": name, "on": False})
        row = self.rows.get(thread_id)
        if row and name in row["typing"]:
            row["typing"].remove(name)

    def _typing_expired(self, thread_id: str, name: str) -> None:
        self._typing_stop(thread_id, name)
        if thread_id in self.rows:  # a followed chat off the board has nothing to repaint
            self.broadcast()

    # --- pages ---

    def payload(self) -> str:
        ordered = sorted(self.rows.values(), key=lambda r: r["last_activity"], reverse=True)
        # `page` lets an open tab notice a newer widget.html (server restart, edit) and reload.
        for row in ordered:
            row["link"] = deep_link(row["id"], row.get("last_id", ""))
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
        web: bool = False,
        ws: ServerConnection | None = None,
        msg_id: str = "",
        chat: bool = False,
    ) -> bool:
        """Page verb `open`: run the opener (or, for `web`, the browser) on the deep link of the row
        (or of the chat `ws` follows), at `msg_id` when given, else at the row's last message.
        `chat` runs the browser on our own `/chat/<thread>` page instead.

        Opening is not reading: the row keeps its seen/unread state until the user says so."""
        row = self.rows.get(thread_id)
        argv = self.browser if web or chat else self.opener
        if (row is None and not self._following(ws, thread_id)) or argv is None:
            return False
        if chat:
            if not self.base_url:
                return False
            url = f"{self.base_url}/chat/{quote(thread_id, safe='')}"
        else:
            target = msg_id or str((row or {}).get("last_id") or "")
            url = deep_link(thread_id, target, "https" if web else self.open_scheme)
        try:
            # argv exec, no shell: the only variable part is a URL built from ids we already hold.
            proc = await asyncio.create_subprocess_exec(
                *argv,
                url,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            log.warning("opener_failed", opener=argv[0], error=str(exc))
            return False
        asyncio.get_running_loop().create_task(proc.wait())  # reap; xdg-open returns at once
        log.info("opened", thread=thread_id, web=web, chat=chat)
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

    async def handle(self, ws: ServerConnection) -> None:
        # A chat page (`/ws?chat`) only needs its own chat: the board's rows reach it only once it
        # asks (`board`, split layout).
        board_page = ws.request is None or urlsplit(ws.request.path).query != "chat"
        if board_page:
            self.clients.add(ws)
        try:
            if board_page:
                await ws.send(self.payload())
            async for raw in ws:
                try:
                    verb = json.loads(raw)
                except json.JSONDecodeError, TypeError:
                    verb = None
                at = verb.get("at") if isinstance(verb, dict) else None
                at = at if isinstance(at, str) else None
                if isinstance(verb, dict) and isinstance(verb.get("seen"), str):
                    self.mark_seen(verb["seen"], at)
                elif isinstance(verb, dict) and isinstance(verb.get("unseen"), str):
                    self.mark_unseen(verb["unseen"])
                elif isinstance(verb, dict) and isinstance(verb.get("read"), str):
                    await self.mark_read(verb["read"])
                elif isinstance(verb, dict) and isinstance(verb.get("mute"), str):
                    self.set_muted(verb["mute"], True)
                elif isinstance(verb, dict) and isinstance(verb.get("unmute"), str):
                    self.set_muted(verb["unmute"], False)
                elif isinstance(verb, dict) and isinstance(verb.get("send"), str):
                    # A send can take the whole HTTP timeout: the page's other verbs must not queue behind it.
                    sent = functools.partial(
                        self._sent, ws, verb["send"], verb.get("text"), verb.get("quote")
                    )
                    # Quotes cost a lookup: they count as a message action.
                    quoted = verb.get("quote")
                    work = self._one_at_a_time(ws, {"sent": verb["send"]}, sent) if quoted else sent()
                    self._background(self._answer(ws, work))
                elif isinstance(verb, dict) and isinstance(verb.get("history"), str):
                    if ws in self._history_busy:  # one page in flight per socket
                        busy = {**_history_head(verb["history"], verb), "error": "busy"}
                        with contextlib.suppress(ConnectionClosed):
                            await ws.send(json.dumps(busy, ensure_ascii=False))
                    else:
                        # The API fallback is a network round trip: the page's other verbs must not wait.
                        self._history_busy.add(ws)
                        self._background(self._history_reply(ws, verb))
                elif isinstance(verb, dict) and isinstance(verb.get("search"), str):
                    # Reads the whole chat in a worker thread: one search per socket.
                    head = {"search": verb["search"], "q": verb.get("q")}
                    found = functools.partial(self.search, verb["search"], verb.get("q"))
                    self._background(
                        self._answer(ws, self._one_at_a_time(ws, head, found, self._search_busy))
                    )
                elif isinstance(verb, dict) and verb.get("board") is True:
                    # A split chat page shows the list too: from now on it gets the board's rows,
                    # exactly what the widget gets.
                    if ws not in self.clients:
                        self.clients.add(ws)
                        with contextlib.suppress(ConnectionClosed):
                            await ws.send(self.payload())
                elif isinstance(verb, dict) and isinstance(verb.get("follow"), str):
                    await self._answer(ws, self.follow(ws, verb["follow"]))
                elif isinstance(verb, dict) and isinstance(verb.get("open"), str):
                    chat = verb.get("chat") is True
                    msg = verb.get("msg")
                    msg = msg if not chat and isinstance(msg, str) and _MSG_ID_RE.fullmatch(msg) else ""
                    await self.open_row(
                        verb["open"], web=verb.get("web") is True, ws=ws, msg_id=msg, chat=chat
                    )
                elif isinstance(verb, dict) and (
                    action := next((k for k in _ACTIONS if isinstance(verb.get(k), str)), "")
                ):
                    # An HTTP round trip, like send: the page's other verbs must not queue behind it.
                    msg = verb.get("msg")
                    head = {action: verb[action], "msg": msg if isinstance(msg, str) else None}
                    acted = functools.partial(self.act, ws, action, verb)
                    self._background(self._answer(ws, self._one_at_a_time(ws, head, acted)))
                else:
                    log.debug("ws_client_message_ignored", data=str(raw)[:100])
        finally:
            self.clients.discard(ws)
            self._follows.pop(ws, None)
            self._action_busy.discard(ws)
            self._search_busy.discard(ws)


def _history_head(thread_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """The answer's head: the thread and the cursor asked for, `before: null` for the latest page."""
    key = next((k for k in ("before", "after", "around") if request.get(k) not in (None, "")), "before")
    value = request.get(key)
    return {"history": thread_id, key: value if isinstance(value, str) and value else None}


def _deleted(props: dict[str, Any]) -> bool:
    return bool(props.get("deletetime") or props.get("hardDeleteTime"))


def _text_error(text: Any) -> str | None:
    """Why `text` cannot be posted as a message, None when it can."""
    if not isinstance(text, str) or not text.strip():
        return "empty message"
    if len(message_html(text, False).encode()) > _SEND_MAX_BYTES:
        return "message too long"
    return None


def _failure(exc: Exception) -> str:
    """Reason a failed Teams call shows on the page."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Teams answered {exc.response.status_code}"
    if isinstance(exc, httpx.TransportError):
        return "network error"
    return str(exc)[:200]


def _save_marks(path: Path | None, marks: dict[str, str]) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(marks, ensure_ascii=False, indent=0))


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


def page_version(name: str = "widget.html") -> str:
    try:
        return str(_page_path(name).stat().st_mtime_ns)
    except OSError:
        return ""


def _page(name: str = "widget.html") -> str:
    return _page_path(name).read_text(encoding="utf-8")


@functools.lru_cache(maxsize=4)
def _chat_csp(page: str) -> str:
    """CSP for chat.html: only its own script (by hash) runs, so an inline handler in a message cannot.

    Server HTML reaches that page's DOM: no foreign script, image or socket even if the sanitiser slips.
    """
    found = _SCRIPT_RE.search(page)
    digest = hashlib.sha256(found.group(1).encode() if found else b"").digest()
    return (
        f"default-src 'none'; script-src 'sha256-{base64.b64encode(digest).decode()}'; "
        "style-src 'unsafe-inline'; img-src 'self'; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    )


def _respond(ws: ServerConnection, body: bytes, headers: dict[str, str]) -> Response:
    resp = ws.respond(HTTPStatus.OK, "")
    resp.body = body
    for name, value in {**headers, "Content-Length": str(len(body))}.items():
        if name in resp.headers:  # respond() stamps text/plain; Headers is a multidict
            del resp.headers[name]
        resp.headers[name] = value
    return resp


async def _process_request(
    board: Board, bound: str, ws: ServerConnection, request: Request
) -> Response | None:
    path = request.path.split("?", 1)[0]  # the page's view params (?theme=…) ride the query string
    if path == "/ws":
        return None  # proceed with the websocket upgrade
    if path == "/":
        return _respond(ws, _page().encode(), {"Content-Type": "text/html; charset=utf-8"})
    # DNS rebinding: a foreign name resolving to our address must not read a chat or its files.
    own_host = request.headers.get("Host") == bound
    if own_host and path.startswith("/chat/"):
        thread_id = unquote(path.removeprefix("/chat/"))
        if thread_id in board.rows or await asyncio.to_thread(board._archive_dir, thread_id) is not None:
            page = _page("chat.html")
            headers = {"Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": _chat_csp(page)}
            return _respond(ws, page.encode(), headers)
    if own_host and path.startswith("/media/"):
        found = await asyncio.to_thread(board.media_file, path)
        if found is not None:
            body, ctype = found
            # An archived HTML or SVG opened directly must not run as this origin (it could open /ws).
            safe = {
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
                "Cross-Origin-Resource-Policy": "same-origin",
            }
            return _respond(
                ws, body, {"Content-Type": ctype, "Cache-Control": "private, max-age=3600"} | safe
            )
    return ws.respond(HTTPStatus.NOT_FOUND, "not found\n")


async def serve_board(board: Board, settings: Settings, bind: str | None) -> None:
    host, port = load_bind(settings, bind)
    for attempt in range(_BIND_ATTEMPTS):
        try:
            # Only the page we serve may open the socket — any other site's Origin gets 403,
            # else every open tab could read the chat list (cross-site websocket hijacking).
            origins = [Origin(f"http://{host}:{port}")]
            routes = functools.partial(_process_request, board, f"{host}:{port}")
            board.base_url = f"http://{host}:{port}"
            async with serve(board.handle, host, port, origins=origins, process_request=routes):
                log.info("web_listening", url=f"http://{host}:{port}/")
                await board.watch_page()  # until cancelled
        except OSError as exc:
            if bind or attempt == _BIND_ATTEMPTS - 1:
                raise
            # Persisted port got taken by something else since the draw — redraw, keep going.
            log.warning("bind_failed", host=host, port=port, error=str(exc))
            host, port = _draw_bind()
            save_bind(settings, host, port)


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
        log.error("web_bootstrap_failed", error=str(exc), error_type=type(exc).__name__)
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
    directory = Directory(settings)
    directory.set_token(skype_token, bearer)
    directory.me = me
    seen_path = settings.config_dir / "seen.json"
    seen = _load_seen(seen_path)
    seed = await asyncio.to_thread(seed_from_archive, data_dir, me) if data_dir else {}
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
    )
    log.info("web_seeded", rows=len(board.rows), seeded=len(seed), limit=cap, me=bool(me))
    # The stream only returns when auth is dead: a page that silently stops updating is worse
    # than an exit, so the server goes down with it and the user re-runs.
    server = asyncio.create_task(serve_board(board, settings, bind))
    # The page is up before the listing is walked: it fills in, it does not wait.
    fill = asyncio.create_task(
        fill_from_listing(board, settings, skype_token, directory, cap, me, seen, seed)
    )
    # Own endpoint id: sharing `stream`'s would make whichever registered last the only receiver.
    stream = asyncio.create_task(
        run_forever(settings, on_event=board.on_event, directory=directory, epid_name="endpoint_id-web")
    )
    done, pending = await asyncio.wait({server, stream}, return_when=asyncio.FIRST_COMPLETED)
    fill.cancel()  # the page is going away; a half-finished walk has nobody to tell
    for task in pending:
        task.cancel()
    for task in done:
        task.result()  # re-raise a server failure (e.g. bind) instead of exiting 0
    raise RuntimeError("live stream ended (auth expired?) — re-run")
