"""Archive follows the live stream (spec 005): subscribe, buffer, catch up, drain, follow.

Events are buffered, not written, while a catch-up pass runs: top-up stops at the first page
overlapping `newest()`, so a live write landing before the pass reaches its chat would lift
`newest()` over the gap and the gap would never be fetched. Buffering also keeps a single writer:
the pass, or the live applier, never both.
"""

import asyncio
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import structlog

from .archive import TokenProvider, _now_iso, run_archive
from .archive_store import ChatStore, Index, merge_dicts, message_version
from .chats import is_meeting, is_private
from .config import Settings
from .messages import thread_of
from .stream import run_forever

log = structlog.get_logger()

_BUFFER_MAX = 10_000  # buffered entries (cards weigh KBs); past it only thread ids are kept
_LOSS_WINDOW = 300.0  # seconds: chats written live this recently are forced on a message_loss
_RETRY_MAX = 300.0  # seconds: cap of the backoff between failed catch-up passes


async def _in_thread[T](fn: Callable[[], T]) -> T:
    """Run `fn` in a daemon thread: `asyncio.to_thread` would make Ctrl-C wait for a whole pass."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def _settle(setter: Callable[[Any], None], value: Any) -> None:
        if not future.done():  # cancelled on shutdown
            setter(value)

    def _work() -> None:
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001 — handed to the awaiting coroutine
            loop.call_soon_threadsafe(_settle, future.set_exception, exc)
        else:
            loop.call_soon_threadsafe(_settle, future.set_result, result)

    threading.Thread(target=_work, name="archive-catchup", daemon=True).start()
    return await future


class LiveArchive:
    """Stream hooks (`on_event`, `on_gap`) plus the catch-up loop they drive."""

    def __init__(
        self,
        settings: Settings,
        data_dir: Path,
        provider: TokenProvider,
        *,
        include_all: bool = False,
        download_media: bool = True,
        download_avatars: bool = True,
        download_videos: bool = False,
    ) -> None:
        self.settings, self.data_dir, self.provider = settings, data_dir, provider
        self.include_all = include_all
        self.download_media, self.download_avatars = download_media, download_avatars
        self.download_videos = download_videos
        self.index = Index(data_dir)
        self.stopped = asyncio.Event()  # set when the refresh token is dead
        self._buffering = False
        self._buffer: dict[tuple[str, ...], tuple[str, str, dict[str, Any]]] = {}
        self._force: set[str] = set()  # chats the next pass must top up despite the fast-skip
        self._again = False  # a gap arrived after the running pass started
        self._recent: OrderedDict[str, float] = OrderedDict()  # thread → last live write
        self._catchup: asyncio.Task[None] | None = None
        self._dropped = 0

    def in_scope(self, thread_id: str) -> bool:
        return bool(thread_id) and (self.include_all or is_private(thread_id) or is_meeting(thread_id))

    # --- stream hooks ---

    async def on_gap(self, reason: str) -> None:
        log.info("live_gap", reason=reason, buffering=self._buffering)
        if reason == "message_loss":
            # The loss predates the signal: chats written since may hold a message past a hole.
            cutoff = time.monotonic() - _LOSS_WINDOW
            self._force.update(t for t, at in self._recent.items() if at >= cutoff)
        self._buffering = True
        if self._catchup is None or self._catchup.done():
            self._catchup = asyncio.create_task(self._catch_up())
        else:
            self._again = True

    async def on_event(self, obj: dict[str, Any]) -> None:
        entry = self._entry(obj)
        if entry is None:
            return
        if self._buffering:
            self._buffer_entry(entry)
        elif self._write(entry) is None:
            self._buffer_entry(entry)  # kept for the drain after the pass
            await self.on_gap("write_failed")

    # --- buffering ---

    def _entry(self, obj: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
        """(kind, thread, resource) for what the archive keeps, None for the rest."""
        resource = obj.get("resource") or {}
        kind = str(obj.get("resourceType") or "")
        if kind == "ConversationUpdate":
            thread_id = str(resource.get("id") or "")
        elif kind in ("NewMessage", "MessageUpdate"):
            # No id: horizon ticks and other ephemeral activity, not messages.
            if not resource.get("id") or str(resource.get("messagetype") or "").startswith("Control/"):
                return None
            thread_id = thread_of(resource)
        else:
            return None
        return (kind, thread_id, resource) if self.in_scope(thread_id) else None

    def _buffer_entry(self, entry: tuple[str, str, dict[str, Any]]) -> None:
        kind, thread_id, resource = entry
        # Messages collapse only on exact redelivery: each distinct version must reach
        # `message_versions`. A conversation keeps one merged entry.
        key: tuple[str, ...] = (
            (thread_id,)
            if kind == "ConversationUpdate"
            else (thread_id, str(resource["id"]), str(message_version(resource)))
        )
        held = self._buffer.get(key)
        if held is not None:
            if kind == "ConversationUpdate":
                self._buffer[key] = (kind, thread_id, merge_dicts(held[2], resource))
            return
        if len(self._buffer) >= _BUFFER_MAX:
            self._dropped += 1
            if thread_id not in self._force:
                self._force.add(thread_id)
                self._again = True
            return
        self._buffer[key] = entry

    # --- writes ---

    def _write(self, entry: tuple[str, str, dict[str, Any]]) -> str | None:
        """`_apply`, or None when it failed (lock held by another writer, disk): nothing landed."""
        try:
            return self._apply(*entry)
        except Exception as exc:  # noqa: BLE001 — the entry is retried, never dropped
            log.error("live_write_failed", thread=entry[1], kind=entry[0], error=str(exc))
            return None

    def _apply(self, kind: str, thread_id: str, resource: dict[str, Any]) -> str:
        if kind == "ConversationUpdate":
            self.index.merge_raw(thread_id, resource)
            return "merged"
        self.index.ensure_chat(thread_id)
        store = ChatStore(self.data_dir, thread_id)
        try:
            outcome = store.apply_message(resource, _now_iso())
        finally:
            store.close()
        if outcome == "new":
            self.index.merge_raw(thread_id, {"lastMessage": resource})
        if outcome != "stale":
            self._recent[thread_id] = time.monotonic()
            self._recent.move_to_end(thread_id)
            cutoff = time.monotonic() - _LOSS_WINDOW
            while self._recent and next(iter(self._recent.values())) < cutoff:
                self._recent.popitem(last=False)
            log.info("live_event_stored", thread=thread_id, kind=kind, outcome=outcome)
        return outcome

    # --- catch-up ---

    def _pass(self, force: frozenset[str]) -> bool:
        return asyncio.run(
            run_archive(
                self.settings,
                self.data_dir,
                token_provider=self.provider,
                include_all=self.include_all,
                download_media=self.download_media,
                download_avatars=self.download_avatars,
                download_videos=self.download_videos,
                force=force,
            )
        )

    async def _catch_up(self) -> None:
        failures = write_failures = 0
        while True:
            self._again = False
            force, self._force = frozenset(self._force), set()
            log.info("live_catchup_start", forced=len(force), buffered=len(self._buffer))
            try:
                expired = await _in_thread(partial(self._pass, force))
            except Exception as exc:  # noqa: BLE001 — transient (network, 5xx): the pass is reentrant
                failures += 1
                self._force |= force
                delay = min(2.0**failures, _RETRY_MAX)
                log.warning("live_catchup_failed", attempt=failures, delay=delay, error=str(exc))
                await asyncio.sleep(delay)
                continue
            failures = 0
            if expired:
                self.stopped.set()
                return
            if self._again:
                continue
            drained = await self._drain()
            if drained:
                return
            if drained is None:
                # Not reset by a good pass: a lasting write failure must not loop passes every 2 s.
                write_failures += 1
                await asyncio.sleep(min(2.0**write_failures, _RETRY_MAX))

    async def _drain(self) -> bool | None:
        """Apply the buffer: True when empty, False when a gap wants another pass first, None when
        a write failed (the entry stays at the head of the buffer)."""
        counts: dict[str, int] = {}
        while self._buffer:
            if self._again:
                return False
            key = next(iter(self._buffer))
            outcome = self._write(self._buffer[key])
            if outcome is None:
                return None
            del self._buffer[key]
            counts[outcome] = counts.get(outcome, 0) + 1
            await asyncio.sleep(0)  # a long buffer must not starve the socket's heartbeat echo
        self._buffering = False
        log.info("live_drained", dropped=self._dropped, **counts)
        self._dropped = 0
        return True

    def close(self) -> None:
        self.index.close()


async def run_live(
    settings: Settings,
    data_dir: Path,
    *,
    token_provider: TokenProvider,
    include_all: bool = False,
    download_media: bool = True,
    download_avatars: bool = True,
    download_videos: bool = False,
) -> bool:
    """Returns True when it stopped on a dead refresh token (re-login needed)."""
    live = LiveArchive(
        settings,
        data_dir.resolve(),
        token_provider,
        include_all=include_all,
        download_media=download_media,
        download_avatars=download_avatars,
        download_videos=download_videos,
    )
    stream = asyncio.create_task(
        run_forever(settings, on_event=live.on_event, on_gap=live.on_gap, epid_name="endpoint_id-archive")
    )
    stop = asyncio.create_task(live.stopped.wait())
    try:
        await asyncio.wait({stream, stop}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (stream, stop, live._catchup):
            if task is not None:
                task.cancel()
        live.close()
    # run_forever only returns on a dead refresh token (or a closed stdout, not a case here).
    return True
