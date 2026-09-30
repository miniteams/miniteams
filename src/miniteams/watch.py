"""`miniteams watch` (spec 009): one JSON line per live event that matches declared criteria.

Built for Claude Code's Monitor tool, which turns each stdout line into an event in the session,
and for any pipe. Three service lines (`gap`, `suppressed`, `stopped`) keep silence from ever
standing for success.
"""

import asyncio
import json
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from ._io import emit, force_blocking_stdout
from .auth import AuthExpired, token_source
from .config import Settings
from .directory import Directory
from .dump import iter_history_pages
from .messages import thread_of
from .skype import exchange_skype_token
from .stream import run_forever
from .web import _is_me, self_mri
from .web import _plain as plain_text

log = structlog.get_logger()

_TEXT_MAX = 1000
_RATE_MAX = 10  # matches per minute before the rest is held back (Monitor cuts a noisy source)
_RATE_WINDOW = 60.0
_SEEN_MAX = 10_000  # message ids already printed, so a live repeat of a caught-up one stays silent
_SENT_FILE = "mcp-sent.json"  # client ids the MCP server sent; see `note_sent`
_SENT_MAX = 1000
_PAGE_SIZE = 100
_CATCHUP_PAGES = 50  # history pages a catch-up walks when `--after` is not found
_MESSAGE_TYPES = ("RichText/Html", "Text")  # what a person writes; calls, typing, roster: not news


@dataclass(frozen=True)
class Watch:
    event: str = "message"  # message | reaction
    thread: str = ""
    sender: str = "anyone"  # me | others | anyone
    after: str = ""
    reaction: str = ""
    once: bool = False
    note: str = ""

    def validate(self) -> None:
        """Refuse a firehose and the flag pairs that make no sense together."""
        if self.event not in ("message", "reaction"):
            raise ValueError("--event must be message or reaction")
        if self.sender not in ("me", "others", "anyone"):
            raise ValueError("--from must be me, others or anyone")
        if not self.thread and self.sender != "me":
            raise ValueError("a watch needs --thread or --from me")
        if self.after and not self.thread:
            raise ValueError("--after needs --thread")
        if self.reaction and self.event != "reaction":
            raise ValueError("--reaction needs --event reaction")


def _newer(message_id: str, after: str) -> bool:
    """Message ids are epoch milliseconds as text; compare as numbers when both are."""
    if message_id.isdigit() and after.isdigit():
        return int(message_id) > int(after)
    return message_id > after


def _wanted_sender(watch: Watch, mri: str, me: str) -> bool:
    mine = _is_me(mri, me)
    return watch.sender == "anyone" or (watch.sender == "me") == mine


def match(
    watch: Watch, obj: dict[str, Any], me: str, started_ms: int, sent_ids: set[str]
) -> dict[str, Any] | None:
    """The part of a stream event that matches `watch`, or None. Pure: no clock, no network.

    Returns `{"resource", "reaction", "reactor"}`; the caller turns it into a line."""
    resource = obj.get("resource") or {}
    kind = str(obj.get("resourceType") or "")
    if watch.thread and thread_of(resource) != watch.thread:
        return None
    message_id = str(resource.get("id") or "")
    if watch.after and not _newer(message_id, watch.after):
        return None
    if watch.event == "message":
        if kind != "NewMessage" or str(resource.get("messagetype") or "") not in _MESSAGE_TYPES:
            return None  # edits and deletes are MessageUpdate; typing and calls are not messages
        if str(resource.get("clientmessageid") or "") in sent_ids:
            return None  # what the MCP server sent must not wake its own agent
        if not _wanted_sender(watch, str(resource.get("from") or ""), me):
            return None
        return {"resource": resource, "reaction": "", "reactor": ""}
    if kind != "MessageUpdate":
        return None
    emotions = (resource.get("properties") or {}).get("emotions")
    for emotion in emotions or []:
        key = str(emotion.get("key") or "")
        if watch.reaction and key != watch.reaction:
            continue
        for user in emotion.get("users") or []:
            mri = str(user.get("mri") or "")
            # The first update seen for a message lists every reaction it already had: only a
            # reaction newer than the watch is news.
            if int(user.get("time") or 0) <= started_ms:
                continue
            if _wanted_sender(watch, mri, me):
                return {"resource": resource, "reaction": key, "reactor": mri}
    return None


def load_sent_ids(settings: Settings) -> set[str]:
    """Client ids the MCP server sent. A missing or unreadable file excludes nothing."""
    try:
        data = json.loads((settings.cache_dir / _SENT_FILE).read_text())
    except OSError, ValueError:
        return set()
    return {str(x) for x in data} if isinstance(data, list) else set()


def note_sent(settings: Settings, client_message_id: str) -> None:
    """Remember a client id the MCP server sent, in a bounded file `watch` reads. Best-effort."""
    path = settings.cache_dir / _SENT_FILE
    ids = [x for x in sorted(load_sent_ids(settings)) if x != client_message_id][-(_SENT_MAX - 1) :]
    ids.append(client_message_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ids))
    except OSError as exc:
        log.warning("sent_file_write_failed", path=str(path), error=str(exc))


class Watcher:
    """Turns matches into lines: labels, rate limit, once, the ids already printed."""

    def __init__(self, settings: Settings, watch: Watch, me: str, started_ms: int) -> None:
        self.settings, self.watch, self.me, self.started_ms = settings, watch, me, started_ms
        self.directory = Directory(settings)
        self.directory.me = me
        self.done = asyncio.Event()  # set once `--once` has printed its line
        self._printed: OrderedDict[str, None] = OrderedDict()
        self._recent: deque[float] = deque()
        self._held = 0
        self._connects = 0

    def _sent_ids(self) -> set[str]:
        return load_sent_ids(self.settings) if self.watch.sender != "others" else set()

    async def line(self, hit: dict[str, Any]) -> dict[str, Any]:
        resource = hit["resource"]
        thread = thread_of(resource)
        sender_mri = hit["reactor"] or str(resource.get("from") or "")
        if hit["reactor"]:
            sender = await self.directory.display(sender_mri)
        else:
            sender = str(resource.get("imdisplayname") or "") or await self.directory.display(sender_mri)
        return {
            "event": self.watch.event,
            "thread": thread,
            "chat": await self.directory.label(thread),
            "sender": sender,
            "message_id": str(resource.get("id") or ""),
            "time": str(resource.get("composetime") or resource.get("originalarrivaltime") or ""),
            "text": plain_text(str(resource.get("content") or ""))[:_TEXT_MAX],
            "reaction": hit["reaction"],
            "note": self.watch.note,
        }

    def _allowed(self, now: float) -> bool:
        while self._recent and now - self._recent[0] > _RATE_WINDOW:
            self._recent.popleft()
        if len(self._recent) >= _RATE_MAX:
            self._held += 1
            return False
        self._recent.append(now)
        return True

    async def _flush_held(self) -> None:
        if self._held:
            await emit(
                json.dumps({"event": "suppressed", "held_back": self._held, "note": self.watch.note}) + "\n"
            )
            self._held = 0

    async def handle(self, obj: dict[str, Any], *, live: bool = True) -> bool:
        """Print the line for `obj` if it matches; True when a line went out."""
        if self.done.is_set():
            return False
        hit = match(self.watch, obj, self.me, self.started_ms, self._sent_ids())
        if hit is None:
            return False
        message_id = str(hit["resource"].get("id") or "")
        key = f"{message_id}:{hit['reaction']}:{hit['reactor']}"
        if key in self._printed:
            return False  # a live repeat of a caught-up message, or a redelivery
        if live and not self._allowed(time.monotonic()):
            return False
        await self._flush_held()
        self._printed[key] = None
        while len(self._printed) > _SEEN_MAX:
            self._printed.popitem(last=False)
        await emit(json.dumps(await self.line(hit), ensure_ascii=False) + "\n")
        if self.watch.once:
            self.done.set()
        return True

    async def on_event(self, obj: dict[str, Any]) -> None:
        await self.handle(obj)

    async def on_gap(self, reason: str) -> None:
        if reason == "connected":
            self._connects += 1
            if self._connects == 1:
                return  # the first connection is the start, not a gap
        await emit(json.dumps({"event": "gap", "reason": reason, "note": self.watch.note}) + "\n")


async def catch_up(watcher: Watcher, skype_token: str) -> None:
    """Print the matching messages already in the chat after `--after`, oldest first."""
    watch = watcher.watch
    collected: list[dict[str, Any]] = []
    pages = 0
    for page in iter_history_pages(watcher.settings, skype_token, watch.thread, _PAGE_SIZE, 0):
        pages += 1
        collected += [m for m in page if _newer(str(m.get("id") or ""), watch.after)]
        if not _newer(str(page[-1].get("id") or ""), watch.after) or pages >= _CATCHUP_PAGES:
            break
    for message in sorted(collected, key=lambda m: str(m.get("composetime") or "")):
        await watcher.handle({"resourceType": "NewMessage", "resource": message}, live=False)
        if watcher.done.is_set():
            return


async def run_watch(settings: Settings, watch: Watch) -> int:
    """Silent sign-in, optional catch-up, then the stream until `--once` fires or the credential
    dies. Exit 0 on a match or a closed stdout, 1 on a dead credential."""
    force_blocking_stdout()
    tokens = token_source(settings)
    try:
        aad = tokens.refresh()
    except AuthExpired as exc:
        await emit(
            json.dumps({"event": "stopped", "reason": f"auth_expired: {exc}", "note": watch.note}) + "\n"
        )
        return 1
    started_ms = int(time.time() * 1000)
    watcher = Watcher(settings, watch, self_mri(str(aad["access_token"])), started_ms)
    skype = exchange_skype_token(settings, aad["access_token"])
    watcher.directory.set_token(skype["skype_token"], str(aad.get("id_token") or aad["access_token"]))
    if watch.after:
        await catch_up(watcher, skype["skype_token"])
        if watcher.done.is_set():
            return 0
    # A fresh endpoint id per process: the registrar keeps one socket per id, and two watches on
    # one id would steal each other's events. Never persisted, so nothing to recycle.
    stream = asyncio.create_task(
        run_forever(
            settings,
            on_event=watcher.on_event,
            on_gap=watcher.on_gap,
            directory=watcher.directory,
            epid=str(uuid.uuid4()),
            prompt=False,
        )
    )
    done = asyncio.create_task(watcher.done.wait())
    try:
        await asyncio.wait({stream, done}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        done.cancel()
        if not stream.done():
            stream.cancel()
    await watcher._flush_held()
    if watcher.done.is_set():
        return 0
    if _stdout_closed():
        return 0  # the reader went away: nothing to say and nobody to say it to
    # The stream only ends on a dead refresh token, a closed stdout, or a crash: all of them end
    # the watch, and a silent end would read as "nothing happened".
    error = stream.exception() if stream.done() else None
    reason = f"crash: {error}" if error else "auth_expired"
    await emit(json.dumps({"event": "stopped", "reason": reason, "note": watch.note}) + "\n")
    return 1


def _stdout_closed() -> bool:
    import sys

    try:
        sys.stdout.flush()
    except OSError, ValueError:
        return True
    return sys.stdout.closed


def sent_file(settings: Settings) -> Path:
    return settings.cache_dir / _SENT_FILE
