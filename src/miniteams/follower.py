"""One call's follower (spec 044): joins the call, transcribes it, leaves it.

Run by `follow --call`'s follower thread (miniteams.follow); imported only there: it needs the
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

from .call import TROUTER_MOVED, Call, JoinError
from .config import Settings
from .follow import call_id, call_kind, call_thread, join_time, mark_ended
from .stream import off_loop
from .stt.session import Session, Transcript, transcript_header

log = structlog.get_logger()

PENDING_MAX = 60 * 4 * 50  # frames held while the engines load: 60 s of 4 speakers
JOIN_RETRY_PAUSE_S = 5.0

Frame = tuple[int, npt.NDArray[np.int16], float]  # (msi, 20 ms of 16 kHz PCM, wall time)


async def follow(settings: Settings, data_dir: Path, payload: dict[str, Any], stop: threading.Event) -> str:
    """Joins the call, transcribes it until `stop` is set or the call ends, leaves; returns the outcome."""
    follower = Follower(settings, data_dir, payload)
    task = asyncio.ensure_future(follower.attend())
    try:
        # Whatever phase it is in (join, engines loading, transcribing), a stop is not held: the
        # follower's _LEAVE_S must leave room for the DELETE.
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
    transcribes every join; the frames heard before it started are in `pending`, None once fed."""

    def __init__(self, settings: Settings, data_dir: Path, payload: dict[str, Any]) -> None:
        self.settings, self.data_dir, self.payload = settings, data_dir, payload
        self.call_id, self.thread, self.kind = str(call_id(payload)), call_thread(payload), call_kind(payload)
        self.capture = settings.follow_capture / self.call_id if settings.follow_capture else None
        self.call = Call(settings, payload, self.on_audio, capture=self.capture)
        self.session: Session | None = None  # closed by the wind-down even if never started
        # Audio flows as soon as join() returns, the engines load after: a second or two held here.
        self.pending: list[Frame] | None = []
        self.dropped = 0

    def on_audio(self, msi: int, pcm: npt.NDArray[np.int16], t: float) -> None:
        if self.pending is None:
            assert self.session is not None  # pending goes None once the session is started
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
        for attempt in (1, 2):  # 044-AC14: one retry; a stop cancels this task, never retried
            try:
                await self.call.join()
                break
            except Exception as exc:  # noqa: BLE001 — any join failure is logged and written, not raised
                cause = _cause(exc)
                retry = attempt == 1 and _retryable(self.call, exc)
                log.warning(
                    "follow_join_failed", call=self.call_id, cause=cause, attempt=attempt, retry=retry
                )
                if not retry:
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
            await self.fresh()
        await self._transcribe()
        outcome = "call_ended"
        await self.call.ended.wait()
        while self.call.end_reason == TROUTER_MOVED:  # 044-AC13: re-attached, the transcript goes on
            log.warning("follow_reattach", call=self.call_id)
            if (failed := await self.reattach()) is not None:
                outcome = failed
                break
            await self.call.ended.wait()
        try:  # before the hang-up: see mark_ended
            await off_loop(mark_ended, self.data_dir, self.call_id, join_time(self.payload))
        except OSError as exc:
            log.warning("follow_ended_marker_failed", call=self.call_id, error=str(exc))
        return outcome

    async def _transcribe(self) -> None:
        """Starts the session, then feeds it the frames held meanwhile and the next ones."""
        self.session = Session(
            self.settings,
            self.data_dir,
            self.call_id,
            self.thread,
            self.resolve,
            kind=self.kind,
            header={"join": "ok"},
        )
        await self.session.start()
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
        # Straight on the controller the first join reached, when it got that far: no phase 1 (an ended
        # 1:1 answers 404; an ended meeting comes back with us alone, and the leave rule ends it).
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

    async def reattach(self) -> str | None:
        """A fresh join on the moved call, one more when the failure allows it. None when joined, else the
        follow's outcome: the call's own end (over, my last device gone) if it had one, else the move."""
        attempt = 1
        while True:
            await self.fresh()
            try:
                await self.call.join()
                return None
            except Exception as exc:  # noqa: BLE001 — fail safe: no follower sitting deaf as me
                again = attempt == 1 and _retryable(self.call, exc)
                log.warning(
                    "follow_reattach_failed",
                    call=self.call_id,
                    cause=_cause(exc),
                    attempt=attempt,
                    retry=again,
                )
                if not again:
                    return TROUTER_MOVED if self.call.retryable else self.call.end_reason
                attempt = 2

    async def wind_down(self) -> BaseException | None:
        """Hangs up and closes the transcript at once, each whatever the other does (both fit in _LEAVE_S);
        the first failure. close() also stops engines a cancelled start() left loading."""
        closing = [self.session.close()] if self.session is not None else []
        outcomes = await asyncio.gather(self.call.leave(), *closing, return_exceptions=True)
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
