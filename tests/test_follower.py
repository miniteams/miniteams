"""The follower: joins a call, transcribes it, leaves it (spec 044). Synthetic ids only."""

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from call_fakes import CALL, CONTROLLER, ME, OTHER, THREAD, one_to_one, payload, roster, until
from structlog.testing import capture_logs

from miniteams import call as C
from miniteams import follow as F
from miniteams import follower as FL
from miniteams.config import Settings
from miniteams.directory import Directory


class FakeSession:
    """Session's surface; refuses frames before begin(), as the real one would lose them."""

    instances: list[FakeSession] = []
    start_error: BaseException | None = None
    start_hangs = False
    release = asyncio.Event()  # ends a hanging load
    load_s = 0.02

    def __init__(
        self, settings: Settings, data_dir: Path, call: str, thread: str, resolve: Any, **kw: Any
    ) -> None:
        self.args, self.resolve, self.kw = (data_dir, call, thread), resolve, kw
        self.fed: list[tuple[int, int, float]] = []
        self.loaded = self.started = self.closed = False
        FakeSession.instances.append(self)

    async def load(self) -> None:
        # Audio arriving while the engines load: it must be held, never fed into this session yet.
        FakeCall.instances[-1].on_audio(202, np.zeros(320, np.int16), 0.5)
        await asyncio.sleep(FakeSession.load_s)
        if FakeSession.start_hangs:
            await FakeSession.release.wait()
        if FakeSession.start_error is not None:
            raise FakeSession.start_error
        self.loaded = True

    def begin(self) -> None:
        assert self.loaded and not self.started, "begun before its engines loaded, or twice"
        self.started = True

    def feed(self, msi: int, pcm: Any, t: float) -> None:
        assert self.started and not self.closed, "fed outside start()..close()"
        self.fed.append((msi, len(pcm), t))

    async def close(self) -> None:
        self.closed = True


class FakeCall:
    """Call's surface; audio starts right after join() returns, as the receiver task does."""

    instances: list[FakeCall] = []
    join_error: BaseException | None = None
    join_errors: list[BaseException] = []  # one per join, before join_error
    join_hangs = False
    join_slow_cancel = False  # the first cancel is held: the join winds down slowly, as a real one may
    directory: Any = None

    def __init__(
        self,
        settings: Settings,
        payload: dict[str, Any],
        on_audio: Any,
        capture: Path | None = None,
        directory: Any = None,
        controller: str = "",
    ) -> None:
        self.on_audio, self.capture, self.controller = on_audio, capture, controller
        self.roster = C.Roster(me=ME)
        self.directory, self.directory_given = directory or FakeCall.directory, directory
        self.ended = asyncio.Event()
        self.end_reason = ""
        self.retryable = True
        self.left = 0
        self.joined = False
        self.cancel_held = False
        FakeCall.instances.append(self)

    async def join(self) -> None:
        if FakeCall.join_errors:
            raise FakeCall.join_errors.pop(0)
        if FakeCall.join_error is not None:
            raise FakeCall.join_error
        if FakeCall.join_hangs:
            try:
                await self.ended.wait()  # as a real join, an abort fails it
                raise C.JoinError(self.end_reason, retry=self.retryable)
            except asyncio.CancelledError:
                if not FakeCall.join_slow_cancel:
                    raise
                self.cancel_held = True
                await asyncio.Event().wait()
        self.joined = True
        asyncio.get_running_loop().call_soon(self.on_audio, 201, np.zeros(320, np.int16), 1.0)

    def move(self, reason: str = C.TROUTER_MOVED) -> None:
        """What Call does when its Trouter socket reconnects on another surl (or its media goes idle)."""
        self.end_reason = reason
        self.ended.set()

    def abort(self, reason: str) -> None:
        self.end_reason, self.retryable = reason, False
        self.ended.set()

    async def leave(self) -> int:
        self.left += 1
        return 202


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch, directory: Directory) -> Iterator[None]:
    FakeSession.instances, FakeSession.start_error, FakeSession.start_hangs = [], None, False
    FakeSession.load_s, FakeSession.release = 0.02, asyncio.Event()
    FakeCall.instances, FakeCall.join_error, FakeCall.join_hangs = [], None, False
    FakeCall.join_slow_cancel, FakeCall.join_errors = False, []
    monkeypatch.setattr(FL, "JOIN_RETRY_PAUSE_S", 0.01)
    FakeCall.directory = directory
    monkeypatch.setattr(FL, "Session", FakeSession)
    monkeypatch.setattr(FL, "Call", FakeCall)
    yield


def session_up() -> bool:
    return bool(FakeSession.instances) and FakeSession.instances[0].started


async def test_follow_feeds_the_session_and_ends_with_the_call(
    settings: Settings, tmp_path: Path, fakes: None, directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC6: frames heard while the engines load are held and fed first, once started; names come from
    the roster, then the names store, then the MRI; the call's end ends it."""
    stop = threading.Event()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
    await until(lambda: session_up() and len(FakeSession.instances[0].fed) == 2, "held frames fed")
    call, session = FakeCall.instances[0], FakeSession.instances[0]
    assert session.args == (tmp_path, CALL, THREAD)
    assert session.kw == {"kind": "meeting", "header": {"join": "ok"}}
    # Both frames heard during start() are held, then fed once it returned.
    assert sorted(session.fed) == [(201, 320, 1.0), (202, 320, 0.5)] and call.capture is None
    call.on_audio(830, np.zeros(320, np.int16), 1.02)
    assert session.fed[-1] == (830, 320, 1.02)
    call.roster.update(
        roster(1, (OTHER, "Other Synthetic", [("main-audio", 830)]), (ME, "", [("main-audio", 5)]))
    )
    monkeypatch.setattr(directory, "known_name", lambda mri: "Me From Store" if mri == ME else "")
    assert session.resolve(830) == ("Other Synthetic", OTHER)
    assert session.resolve(5) == ("Me From Store", ME) and session.resolve(9) is None
    call.ended.set()
    assert await asyncio.wait_for(task, 5) == "call_ended"
    assert call.left == 1 and session.closed
    assert F.ended_joins(settings, CALL) == {payload()["userJoinTime"]}  # written before the hang-up


async def test_the_ended_marker_is_written_before_the_hang_up(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC4: evt 116 comes once the DELETE is done and my rejoin's 115 may follow at once: by then the
    join must read as ended, or the rejoin's push is taken for this follower and dropped."""
    seen: list[set[str]] = []

    async def leave(self: FakeCall) -> int:
        seen.append(F.ended_joins(settings, CALL))
        return 202

    monkeypatch.setattr(FakeCall, "leave", leave)
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), threading.Event()))
    await until(session_up, "session up")
    FakeCall.instances[0].ended.set()
    assert await asyncio.wait_for(task, 5) == "call_ended"
    assert seen == [{payload()["userJoinTime"]}]


async def test_a_marker_that_cannot_be_written_still_hangs_up(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    (settings.config_dir / "follow").write_text("not a directory")  # where the markers live
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), threading.Event()))
    await until(session_up, "session up")
    with capture_logs() as logs:
        FakeCall.instances[0].ended.set()
        assert await asyncio.wait_for(task, 5) == "call_ended"
    assert "follow_ended_marker_failed" in [e["event"] for e in logs] and FakeCall.instances[0].left == 1


async def test_a_trouter_move_re_attaches_and_the_transcript_goes_on(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """044-AC13: the moved call is hung up (one DELETE), a fresh one joins the same push, the same session
    keeps transcribing it with the speakers' names; a second move re-attaches again."""
    stop = threading.Event()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    first, session = FakeCall.instances[0], FakeSession.instances[0]
    first.roster.update(roster(1, (OTHER, "Other Synthetic", [("main-audio", 830)])))
    fed = len(session.fed)
    with capture_logs() as logs:
        first.move()
        await until(lambda: len(FakeCall.instances) == 2 and FakeCall.instances[1].joined, "re-attached")
    second = FakeCall.instances[1]
    assert first.left == 1 and second.left == 0 and FakeSession.instances == [session] and not session.closed
    assert "follow_reattach" in [e["event"] for e in logs]
    await until(lambda: len(session.fed) > fed, "the new call's audio in the same session")
    assert session.resolve(830) == ("Other Synthetic", OTHER)  # a line still in the engines keeps its name
    assert F.ended_joins(settings, CALL) == set()  # a re-attach never reads as the join's end
    second.move()
    await until(lambda: len(FakeCall.instances) == 3 and FakeCall.instances[2].joined, "re-attached again")
    assert F.ended_joins(settings, CALL) == set()
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    assert [c.left for c in FakeCall.instances] == [1, 1, 1] and session.closed


async def test_a_stop_during_the_re_attach_pause_joins_nothing(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-attach waits like the retry (a call that ended meanwhile gets its evt 116, the trigger's stop,
    before the direct phase 2 brings the ended conversation back as me): a stop then ends it cleanly,
    nothing joined."""
    stop = threading.Event()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    monkeypatch.setattr(FL, "JOIN_RETRY_PAUSE_S", 30.0)
    FakeCall.instances[0].move()
    await until(lambda: len(FakeCall.instances) == 2, "pausing")
    await asyncio.sleep(0.05)
    assert not FakeCall.instances[1].joined
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    first, second = FakeCall.instances
    assert (first.left, second.left, second.joined) == (1, 1, False) and FakeSession.instances[0].closed


@pytest.mark.parametrize("reason", C.REATTACH)
async def test_re_attaches_go_on_until_their_deadline_then_end_the_follow(
    settings: Settings, tmp_path: Path, fakes: None, reason: str
) -> None:
    """044-AC13: a network outage (the socket moved, the media silent) is re-attached again and again,
    each after the pause, for follow_reattach_deadline_s; then the follow ends, marked and hung up: no
    follower sitting deaf in the call as me."""
    settings.follow_reattach_deadline_s = 0.1
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), threading.Event()))
    await until(session_up, "session up")
    FakeCall.join_error = C.JoinError("sign-in: ConnectError")
    with capture_logs() as logs:
        FakeCall.instances[0].move(reason)
        assert await asyncio.wait_for(task, 5) == reason
    failed = [(e["attempt"], e["retry"]) for e in logs if e["event"] == "follow_reattach_failed"]
    assert len(failed) >= 3 and all(r for _, r in failed[:-1]) and failed[-1][1] is False
    assert [a for a, _ in failed] == list(range(1, len(failed) + 1))
    assert all(c.left == 1 for c in FakeCall.instances) and FakeSession.instances[0].closed
    assert F.ended_joins(settings, CALL) == {payload()["userJoinTime"]}


@pytest.mark.parametrize("reason", [C.MEDIA_IDLE, C.MEDIA_LOST])
async def test_media_gone_re_attaches_and_a_network_back_goes_on(
    settings: Settings, tmp_path: Path, fakes: None, reason: str
) -> None:
    """A minute-long outage: the media went silent; failing re-attaches until the network is back, then
    the same transcript goes on."""
    stop = threading.Event()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    FakeCall.join_errors = [C.JoinError("sign-in: ConnectError"), C.JoinError("Trouter: ConnectError")]
    FakeCall.instances[0].move(reason)
    await until(lambda: len(FakeCall.instances) == 4 and FakeCall.instances[3].joined, "back in the call")
    assert not task.done() and len(FakeSession.instances) == 1 and not FakeSession.instances[0].closed
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"


async def test_a_stalled_marker_does_not_hold_the_hang_up(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A data dir (or config dir) stalled on NFS: the marker waits MARK_TIMEOUT_S, then the follower hangs
    up all the same."""
    monkeypatch.setattr(FL, "MARK_TIMEOUT_S", 0.1)
    monkeypatch.setattr(FL, "mark_ended", lambda *_a: time.sleep(30))
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), threading.Event()))
    await until(session_up, "session up")
    with capture_logs() as logs:
        FakeCall.instances[0].ended.set()
        assert await asyncio.wait_for(task, 2) == "call_ended"
    assert FakeCall.instances[0].left == 1 and "follow_ended_marker_slow" in [e["event"] for e in logs]


async def test_a_re_attach_interrupted_once_gets_one_more(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """044-AC13: the re-attach's own join ended retryably (another move mid-join, a failed renegotiation):
    one more try, and the follow goes on."""
    stop = threading.Event()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    FakeCall.join_errors = [C.JoinError("renegotiation: ValueError")]
    FakeCall.instances[0].move()
    await until(lambda: len(FakeCall.instances) == 3 and FakeCall.instances[2].joined, "re-attached")
    assert not task.done() and not FakeSession.instances[0].closed
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"


@pytest.mark.parametrize(
    ("end", "outcome"),
    [(None, C.TROUTER_MOVED), (("last_device_left", False), "last_device_left")],
)
async def test_a_final_re_attach_failure_ends_at_once_with_its_real_cause(
    settings: Settings,
    tmp_path: Path,
    fakes: None,
    monkeypatch: pytest.MonkeyPatch,
    end: tuple[str, bool] | None,
    outcome: str,
) -> None:
    """044-AC13: a failure ruling out a new join (a gone call's 404, my last device gone during the join)
    gets no second re-attach; the outcome is the call's own end when it had one."""
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), threading.Event()))
    await until(session_up, "session up")
    original = FakeCall.join

    async def join(self: FakeCall) -> None:
        if self is FakeCall.instances[0]:
            await original(self)
            return
        if end is not None:
            self.end_reason, self.retryable = end
            self.ended.set()
            raise C.JoinError(end[0], retry=end[1])
        raise C.JoinError("phase2 answered 404", retry=False)

    monkeypatch.setattr(FakeCall, "join", join)
    FakeCall.instances[0].move()
    assert await asyncio.wait_for(task, 5) == outcome
    assert len(FakeCall.instances) == 2 and [c.left for c in FakeCall.instances] == [1, 1]


async def test_follow_stops_on_the_signal(settings: Settings, tmp_path: Path, fakes: None) -> None:
    """044-AC4: SIGTERM (the trigger's stop) → DELETE, then the transcript closes."""
    settings.follow_capture = tmp_path / "cap"
    stop = threading.Event()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    assert FakeCall.instances[0].left == 1 and FakeSession.instances[0].closed
    assert FakeCall.instances[0].capture == tmp_path / "cap" / CALL


async def test_follow_stopped_while_joining(settings: Settings, tmp_path: Path, fakes: None) -> None:
    FakeCall.join_hangs = True
    stop = threading.Event()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
    await until(lambda: bool(FakeCall.instances), "joining")
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    assert FakeCall.instances[0].left == 1 and not FakeSession.instances[0].started
    assert FakeSession.instances[0].closed  # its engines, loading meanwhile, stopped


async def test_follow_stopped_while_the_engines_load(settings: Settings, tmp_path: Path, fakes: None) -> None:
    """044-AC4: the stop is not held until the engines are up; it hangs up and closes the session."""
    FakeSession.start_hangs = True
    stop = threading.Event()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
    await until(lambda: bool(FakeSession.instances), "loading")
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    session = FakeSession.instances[0]
    assert FakeCall.instances[0].left == 1 and session.closed and session.fed == []


async def test_follow_cancelled_itself_leaves_no_body_behind(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """044-AC4: cancelling follow() mid-join stops its body before the hang-up; nothing is left pending."""
    FakeCall.join_hangs = True
    before = asyncio.all_tasks()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), threading.Event()))
    await until(lambda: bool(FakeCall.instances), "joining")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and FakeCall.instances[0].left == 1
    assert [t for t in asyncio.all_tasks() - before if not t.done()] == []


async def test_a_second_cancel_while_the_body_winds_down_still_hangs_up(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """044-AC4: follow() cancelled again while its body is still cancelling: the hang-up still happens."""
    FakeCall.join_hangs = FakeCall.join_slow_cancel = True
    before = asyncio.all_tasks()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), threading.Event()))
    await until(lambda: bool(FakeCall.instances), "joining")
    task.cancel()
    await until(lambda: FakeCall.instances[0].cancel_held, "body cancelling")
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    assert task.cancelled() and FakeCall.instances[0].left == 1
    assert [t for t in asyncio.all_tasks() - before if not t.done()] == []


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (C.JoinError("not admitted (lobby timeout)"), "not admitted (lobby timeout)"),
        (OSError("boom"), "OSError: boom"),
    ],
)
async def test_a_failed_join_lands_in_the_transcript_header(
    settings: Settings, tmp_path: Path, fakes: None, error: BaseException, cause: str
) -> None:
    """044-AC14: the cause is logged, retried once, then written to the header; nothing is transcribed."""
    FakeCall.join_error = error
    with capture_logs() as logs:
        assert await FL.follow(settings, tmp_path, payload(), threading.Event()) == "join_failed"
    failures = [(e["cause"], e["attempt"], e["retry"]) for e in logs if e["event"] == "follow_join_failed"]
    assert failures == [(cause, 1, True), (cause, 2, False)]
    (path,) = (tmp_path / "follow").rglob("*.ndjson")
    header = json.loads(path.read_text())
    assert header["join"] == "failed" and header["error"] == cause and header["call"] == CALL
    assert (header["thread"], header["kind"], header["engine"]) == (THREAD, "meeting", "parakeet")
    assert [c.left for c in FakeCall.instances] == [1, 1] and not FakeSession.instances[0].started
    assert FakeSession.instances[0].closed


async def test_a_1to1_transcript_names_its_chat(settings: Settings, tmp_path: Path, fakes: None) -> None:
    """044-AC9: a 1:1 push names no thread; the header names the 1:1 chat (sorted ids), so MCP lists it."""
    FakeCall.join_error = C.JoinError("refused")
    assert await FL.follow(settings, tmp_path, one_to_one(), threading.Event()) == "join_failed"
    (path,) = (tmp_path / "follow").rglob("*.ndjson")
    header = json.loads(path.read_text())
    oids = sorted(m.removeprefix("8:orgid:") for m in (ME, OTHER))
    assert (header["thread"], header["kind"]) == (f"19:{oids[0]}_{oids[1]}@unq.gbl.spaces", "1:1")


async def test_a_join_that_fails_once_is_retried_and_followed(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """044-AC14: the first try hung up, a fresh call joined after the pause; the transcript is the
    second call's; the wind-down leaves that one."""
    FakeCall.join_errors = [C.JoinError("ICE: ConnectionError")]
    stop = threading.Event()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    first, second = FakeCall.instances
    assert first.left == 1 and second.left == 0
    await until(lambda: (201, 320, 1.0) in FakeSession.instances[0].fed, "the second call's audio")
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    assert (first.left, second.left) == (1, 1) and not list((tmp_path / "follow").rglob("*.ndjson"))


@pytest.mark.parametrize(
    "reason",
    ["call/end: 5003 conversation has ended", "conversation/conversationEnd: 5010", "last_device_left"],
)
async def test_no_retry_after_the_call_itself_ended(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """044-AC14: the call ended (over, or my last device gone) after the acceptance, then the join failed
    on something retryable (ICE): the call's own end rules the retry out."""

    async def join(self: FakeCall) -> None:
        self.end_reason, self.retryable = reason, False
        self.ended.set()
        raise C.JoinError("ICE: ConnectionError")

    monkeypatch.setattr(FakeCall, "join", join)
    with capture_logs() as logs:
        assert await asyncio.wait_for(FL.follow(settings, tmp_path, payload(), threading.Event()), 5) == (
            "join_failed"
        )
    assert len(FakeCall.instances) == 1
    assert [e["retry"] for e in logs if e["event"] == "follow_join_failed"] == [False]


async def test_a_retry_and_a_re_attach_go_straight_to_the_reached_controller(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No phase 1 once a join reached the call's controller, and the names cache is kept (no new store
    per Call); a first try that never got that far leaves the retry free to run phase 1."""
    tries: list[str] = []
    original = FakeCall.join

    async def join(self: FakeCall) -> None:
        tries.append(self.controller)
        if len(tries) == 1:
            self.controller = CONTROLLER  # phase 1 answered, then the join failed
            raise C.JoinError("ICE: ConnectionError")
        await original(self)

    monkeypatch.setattr(FakeCall, "join", join)
    stop = threading.Event()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    FakeCall.instances[1].move()
    await until(lambda: len(FakeCall.instances) == 3 and FakeCall.instances[2].joined, "re-attached")
    assert tries == ["", CONTROLLER, CONTROLLER]
    first = FakeCall.instances[0]
    assert [c.directory_given for c in FakeCall.instances[1:]] == [first.directory, first.directory]
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"


async def test_a_retry_before_any_controller_runs_phase_1(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """A first try that failed before reaching the controller (phase 1 refused): the retry has none to go
    straight to, so it runs phase 1 itself, never the push's controller."""
    FakeCall.join_errors = [C.JoinError("phase1 answered 503")]
    stop = threading.Event()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    assert [c.controller for c in FakeCall.instances] == ["", ""]
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"


async def test_no_retry_when_the_call_is_gone(settings: Settings, tmp_path: Path, fakes: None) -> None:
    """044-AC14: a refusal proving the call is over (a 1:1's controller answering 404) is given up at once."""
    FakeCall.join_errors = [C.JoinError("phase2 answered 404", retry=False)]
    assert await asyncio.wait_for(FL.follow(settings, tmp_path, payload(), threading.Event()), 5) == (
        "join_failed"
    )
    assert len(FakeCall.instances) == 1 and FakeCall.instances[0].left == 1


async def test_a_stop_during_the_retry_pause_joins_nothing_more(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC14: never retried after a stop."""
    monkeypatch.setattr(FL, "JOIN_RETRY_PAUSE_S", 30.0)
    FakeCall.join_errors = [C.JoinError("not admitted (lobby timeout)")]
    stop = threading.Event()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
    await until(lambda: len(FakeCall.instances) == 2, "pausing")
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    first, second = FakeCall.instances
    assert first.left == 1 and second.left == 1  # the fresh one, never joined, still closed
    assert not FakeSession.instances[0].started


async def test_a_header_that_cannot_be_written_still_hangs_up(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    FakeCall.join_error = C.JoinError("refused")
    (tmp_path / "follow").write_text("not a directory")
    with pytest.raises(OSError):
        await FL.follow(settings, tmp_path, payload(), threading.Event())
    assert FakeCall.instances[0].left == 1


def _header(tmp_path: Path) -> dict[str, Any]:
    (path,) = (tmp_path / "follow").rglob("*.ndjson")
    lines = path.read_text().splitlines()
    assert len(lines) == 1  # nothing transcribed
    header: dict[str, Any] = json.loads(lines[0])
    return header


async def test_engines_failing_after_the_join_hang_up_and_say_why(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """044-AC10: joined before the engines failed: hung up at once, `join: failed` with their cause, never
    `join: ok` with no line."""
    FakeSession.start_error, FakeSession.load_s = RuntimeError("engine died"), 0.1
    assert await FL.follow(settings, tmp_path, payload(), threading.Event()) == "join_failed"
    assert FakeCall.instances[0].joined and FakeCall.instances[0].left == 1
    assert FakeSession.instances[0].closed and not FakeSession.instances[0].started
    header = _header(tmp_path)
    assert (header["join"], header["error"]) == ("failed", "engines: RuntimeError: engine died")


async def test_engines_failing_during_the_join_abort_it(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """The engines load while the join runs (a lobby can last minutes): their failure ends the join."""
    FakeCall.join_hangs = True
    FakeSession.start_error = RuntimeError("no model")
    assert await asyncio.wait_for(FL.follow(settings, tmp_path, payload(), threading.Event()), 5) == (
        "join_failed"
    )
    assert len(FakeCall.instances) == 1 and not FakeCall.instances[0].joined  # no retry
    assert _header(tmp_path)["error"] == "engines: RuntimeError: no model"


async def test_engines_failing_during_the_retry_pause_stop_the_retry(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FL, "JOIN_RETRY_PAUSE_S", 0.2)
    FakeCall.join_errors = [C.JoinError("ICE: ConnectionError")]
    FakeSession.start_error = RuntimeError("no model")
    assert await FL.follow(settings, tmp_path, payload(), threading.Event()) == "join_failed"
    assert [c.joined for c in FakeCall.instances] == [False, False]
    assert _header(tmp_path)["error"] == "engines: RuntimeError: no model"


async def test_engines_load_while_the_join_runs(settings: Settings, tmp_path: Path, fakes: None) -> None:
    """The first second of the call is not spent loading models: both start at once."""
    FakeCall.join_hangs = True
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), threading.Event()))
    await until(lambda: bool(FakeSession.instances) and FakeSession.instances[0].loaded, "engines loaded")
    assert not FakeCall.instances[0].joined
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_a_load_failure_after_the_wind_down_aborts_nothing(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """The wind-down stops engines still loading: their failure then is no abort of the left call."""
    FakeSession.start_hangs = True
    stop = threading.Event()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
    await until(lambda: FakeCall.instances and FakeCall.instances[0].joined, "joined")
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    follower = FL.Follower(settings, tmp_path, payload())
    follower.closing = True
    loading: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    loading.set_exception(RuntimeError("stopped"))
    follower.loading = loading
    follower._loaded(loading)
    assert not follower.call.ended.is_set()
    await follower.call.leave()


async def test_engines_failing_with_a_failing_leave_still_write_the_header(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: FakeCall) -> int:
        raise OSError("leave broke")

    monkeypatch.setattr(FakeCall, "leave", broken)
    FakeSession.start_error, FakeSession.load_s = RuntimeError("engine died"), 0.1
    with pytest.raises(OSError, match="leave broke"):
        await FL.follow(settings, tmp_path, payload(), threading.Event())
    assert FakeSession.instances[0].closed and _header(tmp_path)["error"].startswith("engines: ")


async def test_a_failing_leave_still_closes_the_session(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transcript's last lines are flushed whatever the hang-up did."""

    async def broken(self: FakeCall) -> int:
        raise RuntimeError("leave broke")

    monkeypatch.setattr(FakeCall, "leave", broken)
    stop = threading.Event()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    stop.set()
    with pytest.raises(RuntimeError):
        await task
    assert FakeSession.instances[0].closed


async def test_pending_audio_is_bounded_and_its_drops_counted(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FL, "PENDING_MAX", 1)
    stop = threading.Event()
    with capture_logs() as logs:
        task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
        await until(lambda: session_up() and bool(FakeSession.instances[0].fed), "session up")
    assert len(FakeSession.instances[0].fed) == 1
    full = [e for e in logs if e["event"] == "follow_pending_full"]
    assert [(e["dropped"], e["held"]) for e in full] == [(1, 1)]
    stop.set()
    await task


async def test_leave_and_close_run_at_once(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both must fit in the follower's leave budget: a slow hang-up does not delay the transcript's close."""
    order: list[str] = []

    async def slow_leave(self: FakeCall) -> int:
        order.append("leave")
        await asyncio.sleep(0.5)
        return 202

    async def slow_close(self: FakeSession) -> None:
        order.append("close")
        await asyncio.sleep(0.5)
        self.closed = True

    monkeypatch.setattr(FakeCall, "leave", slow_leave)
    monkeypatch.setattr(FakeSession, "close", slow_close)
    stop = threading.Event()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
    await until(session_up, "session up")
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    assert loop.time() - t0 < 0.5 + 0.2 + 0.2  # the longer of the two, one stop poll, slack; both: 1.0+
    assert sorted(order) == ["close", "leave"] and FakeSession.instances[0].closed


async def test_a_call_ending_while_the_engines_load_is_hung_up_at_once(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """044-AC4: joined, the engines still loading (minutes, for a cold model), my last device leaves: the
    follower hangs up after the grace, not once the engines are up."""
    FakeSession.start_hangs = True
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), threading.Event()))
    await until(lambda: bool(FakeCall.instances) and FakeCall.instances[0].joined, "joined")
    call = FakeCall.instances[0]
    call.end_reason, call.retryable = "last_device_left", False
    call.ended.set()
    assert await asyncio.wait_for(task, 1) == "join_failed"
    assert call.left == 1 and FakeSession.instances[0].closed and not FakeSession.instances[0].started
    assert _header(tmp_path)["error"] == "last_device_left"


async def test_a_move_while_the_engines_load_re_attaches_then_transcribes(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """044-AC13: the socket moves (or the media goes silent) before the engines are up: re-attached, and
    the transcript starts once they are, on the new join."""
    FakeSession.start_hangs = True
    stop = threading.Event()
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), stop))
    await until(lambda: bool(FakeCall.instances) and FakeCall.instances[0].joined, "joined")
    FakeCall.instances[0].move()
    await until(lambda: len(FakeCall.instances) == 2 and FakeCall.instances[1].joined, "re-attached")
    assert not task.done() and FakeCall.instances[0].left == 1 and not FakeSession.instances[0].started
    FakeSession.release.set()
    await until(session_up, "transcribing")
    await until(lambda: (201, 320, 1.0) in FakeSession.instances[0].fed, "the new join's audio")
    stop.set()
    assert await asyncio.wait_for(task, 5) == "stopped"
    assert [c.left for c in FakeCall.instances] == [1, 1]


async def test_a_move_then_my_last_device_gone_while_loading_hangs_up_at_once(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    FakeSession.start_hangs = True
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), threading.Event()))
    await until(lambda: bool(FakeCall.instances) and FakeCall.instances[0].joined, "joined")
    FakeCall.instances[0].move(C.MEDIA_IDLE)
    await until(lambda: len(FakeCall.instances) == 2 and FakeCall.instances[1].joined, "re-attached")
    second = FakeCall.instances[1]
    second.end_reason, second.retryable = "last_device_left", False
    second.ended.set()
    assert await asyncio.wait_for(task, 1) == "join_failed"
    assert [c.left for c in FakeCall.instances] == [1, 1] and FakeSession.instances[0].closed
    assert _header(tmp_path)["error"] == "last_device_left"


async def test_a_re_attach_failing_for_good_while_loading_ends_the_follow(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    FakeSession.start_hangs = True
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), threading.Event()))
    await until(lambda: bool(FakeCall.instances) and FakeCall.instances[0].joined, "joined")
    FakeCall.join_error = C.JoinError("phase2 answered 404", retry=False)
    FakeCall.instances[0].move()
    assert await asyncio.wait_for(task, 5) == "join_failed"
    assert _header(tmp_path)["error"] == C.TROUTER_MOVED and len(FakeCall.instances) == 2
