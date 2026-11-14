"""The follower: joins a call, transcribes it, leaves it (spec 044). Synthetic ids only."""

import asyncio
import json
import threading
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
    """Session's surface; refuses frames before start() ends, as the real one would lose them."""

    instances: list[FakeSession] = []
    start_error: BaseException | None = None
    start_hangs = False

    def __init__(
        self, settings: Settings, data_dir: Path, call: str, thread: str, resolve: Any, **kw: Any
    ) -> None:
        self.args, self.resolve, self.kw = (data_dir, call, thread), resolve, kw
        self.fed: list[tuple[int, int, float]] = []
        self.started = self.closed = False
        FakeSession.instances.append(self)

    async def start(self) -> None:
        # Audio arriving while the engines load: it must be held, never fed into this session yet.
        FakeCall.instances[-1].on_audio(202, np.zeros(320, np.int16), 0.5)
        await asyncio.sleep(0.02)
        if FakeSession.start_hangs:
            await asyncio.Event().wait()
        if FakeSession.start_error is not None:
            raise FakeSession.start_error
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
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not FakeCall.join_slow_cancel:
                    raise
                self.cancel_held = True
                await asyncio.Event().wait()
        self.joined = True
        asyncio.get_running_loop().call_soon(self.on_audio, 201, np.zeros(320, np.int16), 1.0)

    def move(self) -> None:
        """What Call does when its Trouter socket reconnects on another surl."""
        self.end_reason = C.TROUTER_MOVED
        self.ended.set()

    async def leave(self) -> int:
        self.left += 1
        return 202


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch, directory: Directory) -> Iterator[None]:
    FakeSession.instances, FakeSession.start_error, FakeSession.start_hangs = [], None, False
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
    assert F.ended_joins(tmp_path, CALL) == {payload()["userJoinTime"]}  # written before the hang-up


async def test_the_ended_marker_is_written_before_the_hang_up(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC4: evt 116 comes once the DELETE is done and my rejoin's 115 may follow at once: by then the
    join must read as ended, or the rejoin's push is taken for this follower and dropped."""
    seen: list[set[str]] = []

    async def leave(self: FakeCall) -> int:
        seen.append(F.ended_joins(tmp_path, CALL))
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
    (tmp_path / "follow").write_text("not a directory")
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
    assert F.ended_joins(tmp_path, CALL) == set()  # a re-attach never reads as the join's end
    second.move()
    await until(lambda: len(FakeCall.instances) == 3 and FakeCall.instances[2].joined, "re-attached again")
    assert F.ended_joins(tmp_path, CALL) == set()
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


async def test_a_re_attach_failing_twice_ends_the_follow(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    """044-AC13: fail safe, no follower sitting deaf in the call as me: a retryable failure gets one more
    re-attach, a second ends it, marked and hung up."""
    task = asyncio.ensure_future(FL.follow(settings, tmp_path, payload(), threading.Event()))
    await until(session_up, "session up")
    FakeCall.join_errors = [C.JoinError("phase2 answered 500"), C.JoinError(C.TROUTER_MOVED)]
    with capture_logs() as logs:
        FakeCall.instances[0].move()
        assert await asyncio.wait_for(task, 5) == "trouter_moved"
    failed = [(e["cause"], e["attempt"], e["retry"]) for e in logs if e["event"] == "follow_reattach_failed"]
    assert failed == [("phase2 answered 500", 1, True), (C.TROUTER_MOVED, 2, False)]
    assert [c.left for c in FakeCall.instances] == [1, 1, 1] and FakeSession.instances[0].closed
    assert F.ended_joins(tmp_path, CALL) == {payload()["userJoinTime"]}


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
    assert FakeCall.instances[0].left == 1 and FakeSession.instances == []


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
    assert [c.left for c in FakeCall.instances] == [1, 1] and FakeSession.instances == []


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
    assert FakeSession.instances == []


async def test_a_header_that_cannot_be_written_still_hangs_up(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    FakeCall.join_error = C.JoinError("refused")
    (tmp_path / "follow").write_text("not a directory")
    with pytest.raises(OSError):
        await FL.follow(settings, tmp_path, payload(), threading.Event())
    assert FakeCall.instances[0].left == 1


async def test_engines_that_fail_to_load_still_hang_up(
    settings: Settings, tmp_path: Path, fakes: None
) -> None:
    FakeSession.start_error = RuntimeError("engine died")
    with pytest.raises(RuntimeError):
        await FL.follow(settings, tmp_path, payload(), threading.Event())
    assert FakeCall.instances[0].left == 1 and FakeSession.instances[0].closed


async def test_a_start_failure_outranks_a_failing_leave(
    settings: Settings, tmp_path: Path, fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engines' failure is the cause worth reporting, not the hang-up's that followed it."""

    async def broken(self: FakeCall) -> int:
        raise OSError("leave broke")

    monkeypatch.setattr(FakeCall, "leave", broken)
    FakeSession.start_error = RuntimeError("engine died")
    with pytest.raises(RuntimeError, match="engine died"):
        await FL.follow(settings, tmp_path, payload(), threading.Event())
    assert FakeSession.instances[0].closed


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
