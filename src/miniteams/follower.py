"""One call's follower (spec 044): joins the call, transcribes it, leaves it.

Run by `follow --call-file`'s follower thread (miniteams.follow); imported only there: it needs the
`transcript` extra.
"""

import asyncio
import threading
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import structlog

from .call import REATTACH, Call, JoinError
from .config import Settings
from .follow import call_id, call_kind, call_thread, join_time, mark_ended
from .stream import off_loop
from .stt.session import Session, Transcript, transcript_header

log = structlog.get_logger()

PENDING_MAX = 60 * 4 * 50  # frames held while the engines load: 60 s of 4 speakers
JOIN_RETRY_PAUSE_S = 5.0
MARK_TIMEOUT_S = 1.0  # a stalled config dir (NFS home) must not hold the hang-up

Frame = tuple[int, npt.NDArray[np.int16], float]  # (msi, 20 ms of 16 kHz PCM, wall time)


async def follow(settings: Settings, data_dir: Path, payload: dict[str, Any], stop: threading.Event) -> str:
    """Joins the call, transcribes it until `stop` is set or the call ends, leaves; returns the outcome."""
    follower = Follower(settings, data_dir, payload)
    task = asyncio.ensure_future(follower.attend())
    try:
        # A stop is never held, whatever the step: the follower's _LEAVE_S must leave room for the DELETE.
        if await _until(stop, task):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            outcome = "stopped"
        else:
            outcome = task.result()
    except BaseException:
        try:
            # follow() itself cancelled: the body must not outlive the hang-up.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:  # a second cancel during that wait must not cost the hang-up
            await follower.wind_down()  # the failure that got here is the one to report
        raise
    if (error := await follower.wind_down()) is not None:
        raise error
    return outcome


class Follower:
    """`call` is the current join of the call: fresh() replaces it for a retry or a re-attach. `session`
    transcribes every join; its engines load while the first join runs, and the frames heard before it
    begins are in `pending`, None once fed."""

    def __init__(self, settings: Settings, data_dir: Path, payload: dict[str, Any]) -> None:
        self.settings, self.data_dir, self.payload = settings, data_dir, payload
        self.call_id, self.thread, self.kind = str(call_id(payload)), call_thread(payload), call_kind(payload)
        self.capture = settings.follow_capture / self.call_id if settings.follow_capture else None
        self.call = Call(settings, payload, self.on_audio, capture=self.capture)
        self.session = Session(
            settings, data_dir, self.call_id, self.thread, self.resolve, kind=self.kind, header={"join": "ok"}
        )
        self.loading: asyncio.Future[None] | None = None
        self.pending: list[Frame] | None = []
        self.dropped = 0
        self.closing = False

    def on_audio(self, msi: int, pcm: npt.NDArray[np.int16], t: float) -> None:
        if self.pending is None:
            self.session.feed(msi, pcm, t)
        elif len(self.pending) < PENDING_MAX:
            self.pending.append((msi, pcm, t))
        else:
            self.dropped += 1

    def resolve(self, msi: int) -> tuple[str, str] | None:
        name, mri = self.call.roster.names.get(msi, ("", ""))
        if not mri:
            return None
        return name or self.call.directory.known_name(mri), mri

    async def attend(self) -> str:
        self.loading = asyncio.ensure_future(self.session.load())
        self.loading.add_done_callback(self._loaded)
        if (cause := await self._join()) is not None:
            started = time.time()
            header = transcript_header(
                self.call_id,
                self.thread,
                self.kind,
                started,
                self.settings.stt_engine,
                join="failed",
                error=cause,
            )
            Transcript(self.data_dir, self.call_id, started, header).close()
            return "join_failed"
        self._begin()
        outcome = "call_ended"
        await self.call.ended.wait()
        while self.call.end_reason in REATTACH:  # 044-AC13: re-attached, the transcript goes on
            log.warning("follow_reattach", call=self.call_id, reason=self.call.end_reason)
            if (failed := await self.reattach(self.call.end_reason)) is not None:
                outcome = failed
                break
            await self.call.ended.wait()
        try:  # before the hang-up: see mark_ended
            mark = off_loop(mark_ended, self.settings, self.call_id, join_time(self.payload))
            await asyncio.wait_for(mark, MARK_TIMEOUT_S)
        except TimeoutError:
            log.warning("follow_ended_marker_slow", call=self.call_id, timeout_s=MARK_TIMEOUT_S)
        except OSError as exc:
            log.warning("follow_ended_marker_failed", call=self.call_id, error=str(exc))
        return outcome

    def _engines_failed(self) -> str | None:
        """The engines' load failure, as the transcript header names it; None while loading or loaded."""
        if self.loading is None or not self.loading.done() or self.loading.cancelled():
            return None
        exc = self.loading.exception()
        return None if exc is None else f"engines: {_cause(exc)}"

    def _loaded(self, _loading: asyncio.Future[None]) -> None:
        """Engines that cannot load end the join in progress: a call it cannot transcribe is not joined."""
        if (cause := self._engines_failed()) is not None and not self.closing:
            self.call.abort(cause)

    async def _join(self) -> str | None:
        """None once joined with the engines loaded, else the cause for the transcript header."""
        assert self.loading is not None
        attempt = 1
        while True:  # 044-AC14: one retry; a stop cancels this task, never retried
            if (cause := self._engines_failed()) is not None:
                return cause
            try:
                await self.call.join()
            except Exception as exc:  # noqa: BLE001 — any join failure is logged and written, not raised
                if (cause := self._engines_failed()) is not None:
                    return cause
                cause = _cause(exc)
                retry = attempt == 1 and _retryable(self.call, exc)
                log.warning(
                    "follow_join_failed", call=self.call_id, cause=cause, attempt=attempt, retry=retry
                )
                if not retry:
                    return cause
                attempt = 2
                await self.fresh()
                continue
            return await self._until_loaded()

    async def _until_loaded(self) -> str | None:
        """Joined before the engines are up: its audio is held meanwhile. None once they are, else the
        cause. A call ending meanwhile is hung up now, not once loaded (a load may take minutes; the
        wind-down stops it), unless a fresh join may recover it (044-AC13): then it re-attaches."""
        assert self.loading is not None
        while True:
            ended: asyncio.Future[Any] = asyncio.ensure_future(self.call.ended.wait())
            waits: set[asyncio.Future[Any]] = {self.loading, ended}
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                ended.cancel()
            if self.loading.done():
                return self._engines_failed()
            if self.call.end_reason not in REATTACH:
                return self.call.end_reason
            log.warning("follow_reattach", call=self.call_id, reason=self.call.end_reason)
            if (failed := await self.reattach(self.call.end_reason)) is not None:
                return failed

    def _begin(self) -> None:
        """Opens the transcript, feeds it the frames held meanwhile, then the next ones."""
        self.session.begin()
        assert self.pending is not None  # fed once only
        if self.dropped:
            log.warning(
                "follow_pending_full", call=self.call_id, dropped=self.dropped, held=len(self.pending)
            )
        for frame in self.pending:
            self.session.feed(*frame)
        self.pending = None

    async def fresh(self) -> None:
        """Hangs up the call (its leg and sockets, before the next one opens its own), replaces it with a
        new one on the same call (phase 2 on its controller, phase 1 first only if no join reached it),
        pauses."""
        old = self.call
        await old.leave()
        # The controller the first join reached, if any: phase 2 straight to it, no phase 1.
        self.call = Call(
            self.settings,
            self.payload,
            self.on_audio,
            directory=old.directory,
            capture=self.capture,
            controller=old.controller,
        )
        self.call.roster.names.update(
            old.roster.names
        )  # the same call: a line in the engines keeps its speaker
        # A call that ended meanwhile gets its evt 116 (the trigger's stop) in this pause.
        await asyncio.sleep(JOIN_RETRY_PAUSE_S)

    async def reattach(self, reason: str) -> str | None:
        """Fresh joins on the call, until one holds, a failure rules the next out, or
        follow_reattach_deadline_s passes (a network outage). None when joined, else the follow's
        outcome: the call's own end (over, my last device gone) if it had one, else `reason`."""
        deadline = time.monotonic() + self.settings.follow_reattach_deadline_s
        attempt = 1
        while True:
            await self.fresh()
            try:
                await self.call.join()
                return None
            except Exception as exc:  # noqa: BLE001 — fail safe: no follower sitting deaf as me
                again = _retryable(self.call, exc) and time.monotonic() < deadline
                log.warning(
                    "follow_reattach_failed",
                    call=self.call_id,
                    cause=_cause(exc),
                    attempt=attempt,
                    retry=again,
                )
                if not again:
                    return reason if self.call.retryable else self.call.end_reason
                attempt += 1

    async def wind_down(self) -> BaseException | None:
        """Hangs up and closes the transcript at once, each whatever the other does (both fit in _LEAVE_S);
        the first failure. close() also stops engines still loading."""
        self.closing = True
        outcomes = await asyncio.gather(self.call.leave(), self.session.close(), return_exceptions=True)
        if self.loading is not None:  # its engines are stopped: what is left of the load goes too
            self.loading.cancel()
            await asyncio.gather(self.loading, return_exceptions=True)
        return next((o for o in outcomes if isinstance(o, BaseException)), None)


async def _until(stop: threading.Event, *aws: Awaitable[Any]) -> bool:
    """Waits until one of `aws` is done or `stop` is set (by the process's signal thread); True if stopped."""
    tasks = [asyncio.ensure_future(a) for a in aws]
    while not stop.is_set():
        done, _ = await asyncio.wait(tasks, timeout=0.2, return_when=asyncio.FIRST_COMPLETED)
        if done:
            return False
    return True


def _retryable(call: Call, exc: BaseException) -> bool:
    """No new join when the failure, or the call's own first end (over, my device gone), rules it out."""
    return call.retryable and not (isinstance(exc, JoinError) and not exc.retry)


def _cause(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, JoinError) else f"{type(exc).__name__}: {exc}"
