"""Recipient read receipts for the chat page (spec 011 amendment): who read up to which message.

A member's horizon is the id of the newest message they read, the first `consumptionhorizon` field.
Live updates come as `ThreadActivity/MemberConsumptionHorizonUpdate` on the web's Trouter socket; a
`GET /v1/threads/<id>/consumptionhorizons` snapshot reconciles each followed chat on open and after
every reconnect or gap. Horizons only move forward. A member without one is unknown, never unread;
our own horizon is never shown. The presence cache file keeps them so a reopened chat paints at once.
"""

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from .directory import Directory
from .http import aget_with_retry
from .presence import Cache

log = structlog.get_logger()

EVENT = "ThreadActivity/MemberConsumptionHorizonUpdate"


def horizon_id(value: Any) -> int:
    """`"<msgId>;<readAtMs>;<clientMsgId>"` → msgId; 0 when there is none to trust."""
    first = value.split(";", 1)[0] if isinstance(value, str) else ""
    # 18 digits at most: a longer one overflows the cache's SQLite int64 (or int()'s digit limit).
    return int(first) if first.isascii() and first.isdigit() and len(first) <= 18 else 0


def _member(mri: Any) -> str:
    return mri.lower() if isinstance(mri, str) and ":" in mri else ""


def parse_event(resource: dict[str, Any]) -> tuple[str, int] | None:
    """A live update's (member, horizon); None for anything else or malformed."""
    if resource.get("messagetype") != EVENT:
        return None
    try:
        content = json.loads(str(resource.get("content") or ""))
    except ValueError:
        return None
    if not isinstance(content, dict):
        return None
    member, at = _member(content.get("user")), horizon_id(content.get("consumptionhorizon"))
    return (member, at) if member and at else None


def parse_snapshot(body: Any) -> dict[str, int]:
    """`consumptionhorizons: [{id, consumptionhorizon}]` → member → horizon; absent = unknown."""
    items = body.get("consumptionhorizons") if isinstance(body, dict) else None
    out: dict[str, int] = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and (member := _member(item.get("id"))):
            if at := horizon_id(item.get("consumptionhorizon")):
                out[member] = at
    return out


class Receipts:
    """Horizons of the followed chats; every change goes to their pages as one full `receipts` frame."""

    def __init__(
        self,
        directory: Directory,
        me: str,
        send: Callable[[str, dict[str, Any]], None],
        cache: Cache | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.directory = directory
        self.me = me.lower()
        self.send = send  # (thread, frame) → the chat's followers
        self.cache = cache
        self.http = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        self.shown: dict[str, dict[str, int]] = {}  # followed thread → member → horizon
        self._tasks: set[asyncio.Task[None]] = set()
        self._asking: set[str] = set()  # threads whose snapshot GET is in flight
        self._again: set[str] = set()  # asked during that GET: its answer may predate the ask

    def frame(self, thread_id: str) -> dict[str, Any]:
        name = self.directory.name_for
        found = self.shown.get(thread_id, {})
        return {"receipts": {m: {"id": str(at), "name": name(m)} for m, at in sorted(found.items())}}

    def merge(self, thread_id: str, found: dict[str, int]) -> bool:
        """Keep the newer horizon of each member, ours left out; True when a followed chat changed."""
        found = {m: at for m, at in found.items() if m != self.me}
        if found and self.cache is not None:
            try:
                self.cache.save_horizons(thread_id, found, time.time())
            except sqlite3.Error as exc:
                log.warning("receipts_cache_write_failed", error=str(exc))
        shown = self.shown.get(thread_id)
        if shown is None:
            return False
        newer = {m: at for m, at in found.items() if at > shown.get(m, 0)}
        shown.update(newer)
        return bool(newer)

    def on_event(self, thread_id: str, resource: dict[str, Any]) -> None:
        parsed = parse_event(resource)
        if parsed is None or not self.merge(thread_id, dict([parsed])):
            return
        self.send(thread_id, self.frame(thread_id))
        member = parsed[0]
        # Unnamed: the next snapshot, which names members, may be a reconnect away.
        if not self.directory.known_name(member):
            self._track(self._name(thread_id, member))

    def open(self, thread_id: str) -> None:
        """A page follows the chat: the cached horizons at once, then the native snapshot."""
        if not self.me:
            return  # our own receipt could not be left out
        cached: dict[str, int] = {}
        if self.cache is not None:
            try:
                cached = self.cache.horizons(thread_id)
            except sqlite3.Error as exc:
                log.warning("receipts_cache_read_failed", error=str(exc))
        shown = self.shown.setdefault(thread_id, {})
        for m, at in cached.items():
            if m != self.me and at > shown.get(m, 0):
                shown[m] = at
        self.send(thread_id, self.frame(thread_id))
        self._spawn(thread_id)

    def retain(self, followed: set[str]) -> None:
        for thread_id in self.shown.keys() - followed:
            del self.shown[thread_id]

    def resync(self) -> None:
        """Trouter reconnected or lost events: whatever was missed, the snapshot has it."""
        for thread_id in list(self.shown):
            self._spawn(thread_id)

    def _spawn(self, thread_id: str) -> None:
        if thread_id in self._asking:
            self._again.add(thread_id)  # one more GET when this one ends: a burst stays at two
            return
        self._asking.add(thread_id)
        self._track(self.snapshot(thread_id))

    def _track(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _name(self, thread_id: str, member: str) -> None:
        await self.directory.resolve([member])
        if thread_id in self.shown and self.directory.known_name(member):
            self.send(thread_id, self.frame(thread_id))

    async def snapshot(self, thread_id: str) -> None:
        url = f"https://{self.directory.settings.contacts_host}/v1/threads/{quote(thread_id, safe='')}"
        headers = {
            "X-Skypetoken": self.directory.skype_token,
            "Authentication": f"skypetoken={self.directory.skype_token}",
            "Accept": "application/json; ver=1.0;",
            "User-Agent": self.directory.settings.user_agent,
        }
        try:
            resp = await aget_with_retry(self.http, url + "/consumptionhorizons", headers=headers)
            found = parse_snapshot(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            # The cached and live horizons stay; a missing one stays unknown.
            log.warning("receipts_snapshot_failed", error=str(exc), error_type=type(exc).__name__)
            return
        finally:
            self._asking.discard(thread_id)
            if thread_id in self._again:
                self._again.discard(thread_id)
                if thread_id in self.shown:
                    self._spawn(thread_id)
        await self.directory.resolve([m for m in found if m != self.me])
        self.merge(thread_id, found)
        if thread_id in self.shown:
            self.send(thread_id, self.frame(thread_id))  # unchanged horizons too: names may have resolved

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self.http.aclose()
