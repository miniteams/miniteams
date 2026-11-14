"""Meeting follower trigger (spec 044, phase 2). Synthetic ids only."""

import asyncio
import base64
import errno
import fcntl
import gzip
import json
import logging
import os
import select
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

from miniteams import archive_live as AL
from miniteams import cli
from miniteams import follow as F
from miniteams import stream as S
from miniteams.archive import StaticToken
from miniteams.config import Settings
from miniteams.directory import Directory
from miniteams.stream import TrouterClient

CALL = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
MEETING = "19:meeting_fakemeeting@thread.v2"


JOIN = "2026-01-01T00:00:00Z"


def _payload(call: str = CALL, thread: str = MEETING, join: str = JOIN) -> dict[str, Any]:
    return {
        "correlationId": call,
        "conversationController": "https://cc.example.invalid/x",
        "userJoinTime": join,
        "groupContext": {"threadId": thread, "messageId": "0"},
        "meetingInfo": {"organizerId": "fake-organizer", "tenantId": "fake-tenant"},
    }


def _push(evt: int, call: str = CALL, thread: str = MEETING, join: str = JOIN) -> dict[str, Any]:
    cp = base64.b64encode(gzip.compress(json.dumps(_payload(call, thread, join)).encode())).decode()
    return {
        "id": 9,
        "url": f"https://surl.invalid/x/{S.NGC_PATH}",
        "body": json.dumps({"evt": evt, "cp": cp}),
    }


def _until(cond: Callable[[], bool], what: str, timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError(f"{what} not met in {timeout} s")


@pytest.fixture(autouse=True)
def _info_logs() -> None:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO)
    )  # the skips log at info


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(F, "launch", lambda _d, call, _p: seen.append(call))
    return seen


@pytest.fixture
def procs() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for proc in started:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()


@pytest.fixture
def real_launches(procs: list[Any], monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Real launches, recorded so a failing test still kills them."""
    real_launch = F.launch
    monkeypatch.setattr(F, "launch", lambda *a: procs.append(real_launch(*a)))
    return procs


@pytest.fixture
def in_process(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """run_call in this process: no real signal mask or wait (they would reach pytest); records waits."""
    waits: list[str] = []

    def sigwait(_sigs: Any) -> int:
        waits.append("wait")
        return signal.SIGTERM

    monkeypatch.setattr(F.signal, "pthread_sigmask", lambda *_a: set())
    monkeypatch.setattr(F.signal, "sigwait", sigwait)
    return waits


_HOLDER = (
    "import os, sys, time\n"
    "from pathlib import Path\n"
    "from miniteams.follow import take_lock\n"
    "fd = take_lock(Path(sys.argv[1]))\n"
    "os.write(fd, f'{os.getpid()}\\n'.encode())\n"
    "print('ready', flush=True)\n"
    "time.sleep(60)\n"
)


def _holder(lock: Path, *argv: str) -> subprocess.Popen[bytes]:
    """A process holding `lock` with `argv` in its cmdline."""
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock), *argv], stdout=subprocess.PIPE)
    assert proc.stdout is not None and proc.stdout.readline() == b"ready\n"
    return proc


# --- decoding ---


def test_decode_keeps_evt_and_classifies_the_call() -> None:
    assert F.decode_push(_push(115)) == (115, _payload())
    assert F.decode_push({"body": json.dumps({"cp": "x"})}) is None  # no evt
    assert F.decode_push({"body": "not json"}) is None
    assert F.call_id({"correlationId": "../../etc/passwd"}) is None  # names a file: UUID only
    kinds = [F.call_kind(t) for t in (MEETING, "19:aa_bb@unq.gbl.spaces", "19:fakegroup@thread.v2")]
    assert kinds == ["meeting", "1:1", "group"]


# --- 044-AC3: one follower per call ---


def test_join_launches_and_other_codes_are_ignored(
    settings: Settings, tmp_path: Path, launches: list[str]
) -> None:
    """044-AC3"""
    for evt in (107, 110, 117, 124):
        F.handle(settings, tmp_path, _push(evt))
    assert launches == []
    F.handle(settings, tmp_path, _push(115))
    assert launches == [CALL]


def test_redelivery_and_self_join_start_no_second_follower(
    settings: Settings, tmp_path: Path, launches: list[str]
) -> None:
    """044-AC3: the follower holds the lock, so a 115 redelivered or caused by its own join skips."""
    fd = F.take_lock(F.lock_path(tmp_path, CALL))
    assert fd is not None
    try:
        F.handle(settings, tmp_path, _push(115))  # redelivered
        F.handle(settings, tmp_path, _push(115))  # the follower's own join, same correlationId
        F.handle(settings, tmp_path, _push(115, call=OTHER))  # another call is not blocked
    finally:
        os.close(fd)
    assert launches == [OTHER]


def test_two_triggers_racing_on_one_lock(tmp_path: Path) -> None:
    """044-AC3: the second taker loses, whichever trigger launched it; free again once released."""
    path = F.lock_path(tmp_path, CALL)
    first = F.take_lock(path)
    assert first is not None
    assert F.take_lock(path) is None and F.lock_held(path)
    os.close(first)
    assert not F.lock_held(path)


def test_two_followers_of_one_call_leave_one_running(tmp_path: Path, procs: list[Any]) -> None:
    """044-AC3: two real follower processes for one call: the second exits 0 on the held lock."""
    cmd = F.follower_command(tmp_path, _payload())
    procs.append(subprocess.Popen(cmd))
    _until(lambda: F.lock_held(F.lock_path(tmp_path, CALL)), "first follower holds the lock")
    second = subprocess.run(cmd, capture_output=True, timeout=30)
    assert second.returncode == 0 and b"follow_already_running" in second.stderr
    assert procs[0].poll() is None


def test_a_crashed_holder_releases_the_lock(tmp_path: Path, procs: list[Any]) -> None:
    """044-AC3: the kernel drops the flock when the holder dies, even by SIGKILL."""
    path = F.lock_path(tmp_path, CALL)
    procs.append(_holder(path))
    assert F.lock_held(path)
    procs[0].kill()
    procs[0].wait()
    assert not F.lock_held(path)


# --- 044-AC11: switch and skip list ---


def test_disabled_or_skipped_starts_nothing_and_logs_the_thread(
    settings: Settings, tmp_path: Path, launches: list[str]
) -> None:
    """044-AC11"""
    F.save_switch(settings, False, [])
    with capture_logs() as logs:
        F.handle(settings, tmp_path, _push(115))
    F.save_switch(settings, True, [MEETING])
    with capture_logs() as more:
        F.handle(settings, tmp_path, _push(115))
        F.handle(settings, tmp_path, _push(115, call=OTHER, thread="19:fakegroup@thread.v2"))
    skipped = [(e["thread"], e["reason"]) for e in logs + more if e["event"] == "follow_skipped"]
    assert skipped == [(MEETING, "disabled"), (MEETING, "skip_list")]
    assert launches == [OTHER]


def test_switch_file_states(settings: Settings, tmp_path: Path, launches: list[str]) -> None:
    """044-AC11: missing = on; unreadable or ill-typed = off for the trigger, never an exception."""
    assert F.load_switch(settings) == (True, [])
    path = settings.config_dir / "follow.json"
    path.parent.mkdir(parents=True)
    for bad in ("{not json", '{"enabled": "yes", "skip": []}', '{"enabled": true, "skip": [1]}', "[]"):
        path.write_text(bad)
        assert F.load_switch(settings) is None
        with capture_logs() as logs:
            F.handle(settings, tmp_path, _push(115))
        assert [e["reason"] for e in logs if e["event"] == "follow_skipped"] == ["disabled"]
    assert launches == []


@pytest.mark.parametrize("argv", [["--skip", MEETING], ["--unskip", MEETING], ["--enable"], ["--status"]])
def test_cli_never_overwrites_an_unreadable_switch(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """044-AC11"""
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "miniteams" / "follow.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{half written")
    with capture_logs() as logs:
        assert cli.main(["follow", *argv]) == 1
    assert path.read_text() == "{half written"
    assert [e["event"] for e in logs] == ["follow_config_unreadable"]


@pytest.mark.parametrize("flag", ["--skip", "--unskip"])
def test_cli_refuses_an_empty_thread(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["follow", flag, ""])
    assert exc.value.code == 2 and "must not be empty" in capsys.readouterr().err


def test_cli_manages_the_switch(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """044-AC11, 044-AC15: management needs neither Teams nor the transcript extra."""
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)
    monkeypatch.setattr(F, "missing_extra", lambda: ["numpy"])
    for argv in (["--disable"], ["--skip", MEETING], ["--skip", MEETING], ["--unskip", "19:x"], ["--status"]):
        assert cli.main(["follow", *argv]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"enabled": False, "skip": [MEETING]}
    assert cli.main(["follow", "--enable"]) == 0
    assert cli.main(["follow", "--unskip", MEETING]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"enabled": True, "skip": []}


# --- evt 116: stop ---


def test_end_signals_our_follower(settings: Settings, tmp_path: Path, procs: list[Any]) -> None:
    path = F.lock_path(tmp_path, CALL)
    procs.append(_holder(path, "follow", "--call", json.dumps(_payload())))
    F.handle(settings, tmp_path, _push(116))
    assert procs[0].wait(timeout=10) == -signal.SIGTERM


def test_end_never_signals_a_pid_that_is_not_our_follower(
    settings: Settings, tmp_path: Path, procs: list[Any]
) -> None:
    path = F.lock_path(tmp_path, CALL)
    procs.append(_holder(path, "follow", "--call", json.dumps(_payload(call=OTHER))))  # another call's
    procs.append(_holder(F.lock_path(tmp_path, OTHER), "not-a-follower"))
    F.handle(settings, tmp_path, _push(116))
    F.handle(settings, tmp_path, _push(116, call=OTHER))
    time.sleep(0.2)  # a SIGTERM sent would have landed by now
    assert procs[0].poll() is None and procs[1].poll() is None


def test_end_while_the_pid_line_is_empty_stops_the_follower_once_locked(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_process: list[str]
) -> None:
    """A follower between its lock and its pid line: 116 leaves the marker it reads next."""
    fd = F.take_lock(F.lock_path(tmp_path, CALL))  # locked, pid line not written yet
    assert fd is not None
    killed: list[int] = []
    monkeypatch.setattr(F.os, "kill", lambda pid, _sig: killed.append(pid))
    with capture_logs() as logs:
        F.handle(settings, tmp_path, _push(116))
    os.close(fd)
    assert killed == [] and [e["event"] for e in logs] == ["follow_lock_unreadable"]
    with capture_logs() as logs:
        assert F.run_call(tmp_path, json.dumps(_payload())) == 0  # the follower's next step
    assert in_process == [] and not F.lock_held(F.lock_path(tmp_path, CALL))
    assert [(e["event"], e.get("reason")) for e in logs] == [("follow_stop", "ended_before_start")]


def test_rejoin_after_end_starts_a_new_follower_and_a_late_redelivery_does_not(
    settings: Settings, tmp_path: Path, launches: list[str]
) -> None:
    """044-AC3"""
    F.handle(settings, tmp_path, _push(116))
    with capture_logs() as logs:
        F.handle(settings, tmp_path, _push(115))  # redelivered 115 of the join that ended
    assert launches == [] and [e["reason"] for e in logs if e["event"] == "follow_skipped"] == ["ended"]
    F.handle(settings, tmp_path, _push(115, join="2026-01-01T00:30:00Z"))  # I rejoin the same call
    assert launches == [CALL] and F.ended_joins(tmp_path, CALL) == {JOIN}


J2 = "2026-01-01T00:30:00Z"


def test_a_late_end_of_an_earlier_join_spares_the_rejoin(
    settings: Settings, tmp_path: Path, procs: list[Any], launches: list[str]
) -> None:
    """044-AC3: 115 J1, 116 J1, 115 J2, then 116 J1 again: the J2 follower stays until 116 J2."""
    F.handle(settings, tmp_path, _push(115))
    F.handle(settings, tmp_path, _push(116))
    F.handle(settings, tmp_path, _push(115, join=J2))
    assert launches == [CALL, CALL]
    procs.append(_holder(F.lock_path(tmp_path, CALL), "follow", "--call", json.dumps(_payload(join=J2))))
    with capture_logs() as logs:
        F.handle(settings, tmp_path, _push(116))  # late, J1
        F.handle(settings, tmp_path, _push(115, join=J2))  # redelivered
    time.sleep(0.2)  # a SIGTERM sent would have landed by now
    assert procs[0].poll() is None and launches == [CALL, CALL]
    assert [e["event"] for e in logs] == ["follow_stop_other_join", "follow_already_running"]
    F.handle(settings, tmp_path, _push(116, join=J2))
    assert procs[0].wait(timeout=10) == -signal.SIGTERM


def test_a_stale_end_neither_blocks_nor_stops_a_starting_rejoin(
    settings: Settings, tmp_path: Path, launches: list[str], in_process: list[str]
) -> None:
    """044-AC3: the J1 marker left by a late 116 J1 is ignored by everything about J2."""
    F.handle(settings, tmp_path, _push(116))
    F.handle(settings, tmp_path, _push(115, join=J2))
    F.handle(settings, tmp_path, _push(116))  # late, J1, before the J2 follower holds its lock
    F.handle(settings, tmp_path, _push(115, join=J2))  # redelivered while it is still starting
    assert launches == [CALL, CALL]  # the second launch loses the lock to the first
    with capture_logs() as logs:
        assert F.run_call(tmp_path, json.dumps(_payload(join=J2))) == 0
    assert [e["event"] for e in logs][0] == "follow_start" and in_process == ["wait"]
    F.handle(settings, tmp_path, _push(116, join=J2))
    with capture_logs() as logs:
        assert F.run_call(tmp_path, json.dumps(_payload(join=J2))) == 0  # a J2 follower starting late
        F.handle(settings, tmp_path, _push(115, join=J2))  # redelivered after its end
    assert [(e["event"], e.get("reason")) for e in logs] == [
        ("follow_stop", "ended_before_start"),
        ("follow_skipped", "ended"),
    ]
    assert in_process == ["wait"] and launches == [CALL, CALL]


def test_pushes_without_a_join_time(
    settings: Settings, tmp_path: Path, procs: list[Any], launches: list[str], in_process: list[str]
) -> None:
    """Unverified shape: a 116 without a join time stops the holder and records nothing; a 115 without
    one launches on a free lock; its follower ignores `.ended`."""
    procs.append(_holder(F.lock_path(tmp_path, CALL), "follow", "--call", json.dumps(_payload(join=J2))))
    F.handle(settings, tmp_path, _push(116, join=""))
    assert procs[0].wait(timeout=10) == -signal.SIGTERM
    assert not F.ended_path(tmp_path, CALL).exists()
    F.handle(settings, tmp_path, _push(116))  # J1 ended: one line
    F.handle(settings, tmp_path, _push(115, join=""))
    assert launches == [CALL] and F.ended_path(tmp_path, CALL).read_text() == f"{JOIN}\n"
    with capture_logs() as logs:
        assert F.run_call(tmp_path, json.dumps(_payload(join=""))) == 0
    assert [e["event"] for e in logs][0] == "follow_start" and in_process == ["wait"]


@pytest.mark.parametrize(
    "error", [PermissionError("not the owner"), OSError(errno.ENOTSUP, "no modes on this mount")]
)
def test_a_refused_chmod_costs_privacy_not_the_launch(
    tmp_path: Path, procs: list[Any], monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    data = tmp_path / "data"
    (data / "follow").mkdir(parents=True)
    os.chmod(data / "follow", 0o755)

    def refuse(*_a: Any) -> None:
        raise error

    monkeypatch.setattr(F.os, "chmod", refuse)
    with capture_logs() as logs:
        procs.append(F.launch(data, CALL, _payload(call=CALL)))
    assert "follow_dir_not_private" in [e["event"] for e in logs]
    assert (data / "follow" / f"{CALL}.log").exists()


def test_a_private_dir_is_not_chmodded_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "follow").mkdir(mode=0o700)
    calls: list[Any] = []
    monkeypatch.setattr(F.os, "chmod", lambda *a: calls.append(a))
    F._follow_dir(tmp_path)
    assert calls == []


def test_per_call_files_are_private(settings: Settings, tmp_path: Path, procs: list[Any]) -> None:
    data = tmp_path / "data"
    (data / "follow").mkdir(parents=True)
    os.chmod(data / "follow", 0o755)  # made by an older run, or by hand
    procs.append(F.launch(data, CALL, _payload(call=CALL)))
    F.handle(settings, data, _push(116, call=OTHER))
    modes = {
        f.name: stat.S_IMODE(f.stat().st_mode)
        for f in (data / "follow", data / "follow" / f"{CALL}.log", F.ended_path(data, OTHER))
    }
    assert modes == {"follow": 0o700, f"{CALL}.log": 0o600, f"{OTHER}.ended": 0o600}
    _until(lambda: F.lock_held(F.lock_path(data, CALL)), "follower up")
    assert stat.S_IMODE(F.lock_path(data, CALL).stat().st_mode) == 0o600
    F.handle(settings, data, _push(116))


class _Gate:
    """A handle() that blocks on its first push until released; records evt order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered, self.release, self.done = threading.Event(), threading.Event(), threading.Event()
        self.order: list[int] = []
        monkeypatch.setattr(F, "handle", self)

    def __call__(self, _s: Settings, _d: Path, req: dict[str, Any]) -> None:
        if not self.order and not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(10)
        self.order.append(json.loads(req["body"])["evt"])
        if len(self.order) == 2:
            self.done.set()


async def test_a_slow_push_does_not_hold_the_read_loop(
    settings: Settings, directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC17: a heartbeat behind a slow 115 is echoed while that 115 is still being handled."""
    gate = _Gate(monkeypatch)
    client = _client(settings, directory, on_call=F.trigger(settings, settings.config_dir))
    await asyncio.wait_for(client._handle_frame(f"3:::{json.dumps(_push(115))}"), 5)  # returns at once
    await asyncio.to_thread(gate.entered.wait, 10)
    await client._handle_frame("2::")
    assert "2::" in client._ws.sent and not gate.release.is_set() and gate.order == []
    gate.release.set()
    await client._handle_frame(f"3:::{json.dumps(_push(116))}")
    assert await asyncio.to_thread(gate.done.wait, 10)


async def test_pushes_are_handled_in_arrival_order(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC3: a 116 queued behind its slow 115 never overtakes it."""
    gate = _Gate(monkeypatch)
    hook = F.trigger(settings, tmp_path)
    await hook(_push(115))
    await asyncio.to_thread(gate.entered.wait, 10)
    await hook(_push(116))
    gate.release.set()
    assert await asyncio.to_thread(gate.done.wait, 10)
    assert gate.order == [115, 116]


def test_a_hung_push_does_not_hold_the_process_at_ctrl_c(tmp_path: Path) -> None:
    """044-AC17: the trigger's thread is a daemon: Ctrl-C exits even while handle() hangs."""
    script = (
        "import asyncio, sys, threading\nfrom pathlib import Path\n"
        "from miniteams import follow as F\nfrom miniteams.config import Settings\n"
        "entered = threading.Event()\n"
        "def hang(*_a):\n    entered.set()\n    threading.Event().wait()\n"
        "F.handle = hang\n"
        "async def main():\n"
        "    await F.trigger(Settings(tenant_id='t'), Path(sys.argv[1]))({})\n"
        "    await asyncio.to_thread(entered.wait)\n"
        "    print('ready', flush=True)\n"
        "    await asyncio.Event().wait()\n"
        "try:\n    asyncio.run(main())\nexcept KeyboardInterrupt:\n    pass\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE)
    try:
        assert proc.stdout is not None
        # Bounded: a trigger that awaits handle() never prints ready, and must fail, not hang.
        assert select.select([proc.stdout], [], [], 10)[0], "trigger did not return"
        assert proc.stdout.readline() == b"ready\n"
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=2) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        assert proc.stdout is not None
        proc.stdout.close()


def test_odd_join_times_and_a_corrupt_marker(tmp_path: Path) -> None:
    assert F.join_time({"userJoinTime": f"{J2}\n{JOIN}"}) == "" and F.join_time({"userJoinTime": 5}) == ""
    path = F.ended_path(tmp_path, CALL)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\n" + JOIN.encode() + b"\n")
    assert JOIN in F.ended_joins(tmp_path, CALL)


def test_a_probe_does_not_make_a_starting_follower_give_up(tmp_path: Path) -> None:
    """A trigger's lock_held probe holding the lock for an instant: take_lock waits it out."""
    path = F.lock_path(tmp_path, CALL)
    path.parent.mkdir(parents=True)
    probe = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(probe, fcntl.LOCK_SH)
    threading.Timer(0.05, os.close, (probe,)).start()
    fd = F.take_lock(path)
    assert fd is not None
    os.close(fd)
    other = F.take_lock(path)  # a probe does not block another probe, a real holder blocks a taker
    assert other is not None and F.lock_held(path) and F.take_lock(path) is None
    os.close(other)


def test_end_with_a_stale_lock_file_signals_nothing(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = F.lock_path(tmp_path, CALL)
    path.parent.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    path.write_text(f"{dead.pid}\n")  # left by a follower that died; nobody holds it
    killed: list[int] = []
    monkeypatch.setattr(F.os, "kill", lambda pid, _sig: killed.append(pid))
    with capture_logs() as logs:
        F.handle(settings, tmp_path, _push(116))
    assert killed == [] and [e["event"] for e in logs] == ["follow_not_running"]
    assert F.ended_path(tmp_path, CALL).read_text() == f"{JOIN}\n"


def test_unusable_pushes_are_logged_and_dropped(
    settings: Settings, tmp_path: Path, launches: list[str]
) -> None:
    bad_evt = {"body": json.dumps({**json.loads(_push(115)["body"]), "evt": "115"})}  # evt not an int
    with capture_logs() as logs:
        F.handle(settings, tmp_path, {"body": "{}"})
        F.handle(settings, tmp_path, bad_evt)
        F.handle(settings, tmp_path, _push(115, call="not-a-uuid"))
    assert [e["event"] for e in logs] == ["call_push_unreadable", "call_push_unreadable", "call_push_bad_id"]
    assert launches == [] and not (tmp_path / "follow").exists()


# --- behavioral guard: the real trigger and the real CLI stub ---


def test_join_then_end_runs_the_real_follower(
    settings: Settings, tmp_path: Path, real_launches: list[Any]
) -> None:
    """044-AC3: 115 launches `miniteams follow --call`, it locks and logs; 116 stops it and frees the lock."""
    procs = real_launches
    data = tmp_path / "data"
    F.handle(settings, data, _push(115))
    path, log = F.lock_path(data, CALL), data / "follow" / f"{CALL}.log"
    _until(lambda: log.exists() and "follow_start" in log.read_text(), "follow_start logged")
    assert F.lock_held(path)
    start = next(json.loads(line) for line in log.read_text().splitlines() if "follow_start" in line)
    assert (start["call"], start["thread"], start["kind"]) == (CALL, MEETING, "meeting")
    F.handle(settings, data, _push(115))  # self-join while it runs: no second follower
    F.handle(settings, data, _push(116))
    _until(lambda: "follow_stop" in log.read_text() and not F.lock_held(path), "follow_stop and lock freed")
    assert log.read_text().count("follow_start") == 1 and len(procs) == 1
    _until(lambda: procs[0].returncode == 0, "follower reaped by its host")  # the reaper waited, not us


def test_join_and_end_back_to_back_leave_nothing_running(
    settings: Settings, tmp_path: Path, real_launches: list[Any]
) -> None:
    """044-AC3: 116 lands before the follower holds its lock."""
    F.handle(settings, tmp_path, _push(115))
    F.handle(settings, tmp_path, _push(116))
    _until(lambda: real_launches[0].returncode is not None, "follower exited and reaped")
    assert real_launches[0].returncode == 0 and not F.lock_held(F.lock_path(tmp_path, CALL))
    log = (tmp_path / "follow" / f"{CALL}.log").read_text()
    assert "ended_before_start" in log and "follow_start" not in log


def test_a_follower_losing_the_race_is_reaped(tmp_path: Path, procs: list[Any]) -> None:
    """A long-running host leaves no zombie behind."""
    fd = F.take_lock(F.lock_path(tmp_path, CALL))
    try:
        proc = F.launch(tmp_path, CALL, _payload())
        procs.append(proc)
        _until(lambda: proc.returncode is not None, "reaped without our wait")
    finally:
        assert fd is not None
        os.close(fd)
    assert proc.returncode == 0


def test_run_call_in_process(tmp_path: Path, in_process: list[str]) -> None:
    """The stub's own lines, measured in this process (the subprocess runs cover the real signals)."""
    with capture_logs() as logs:
        assert F.run_call(tmp_path, "not json") == 2
        assert F.run_call(tmp_path, json.dumps({"correlationId": "x"})) == 2
        assert F.run_call(tmp_path, json.dumps(_payload())) == 0
        fd = F.take_lock(F.lock_path(tmp_path, CALL))
        assert F.run_call(tmp_path, json.dumps(_payload())) == 0
    assert fd is not None
    os.close(fd)
    events = [(e["event"], e.get("reason")) for e in logs]
    assert events == [
        ("follow_bad_call", None),
        ("follow_bad_call", None),
        ("follow_start", None),
        ("follow_stop", "signal"),
        ("follow_already_running", None),
    ]
    assert in_process == ["wait"] and F.lock_path(tmp_path, CALL).read_text().strip().isdigit()


# --- 044-AC13: registration on connect and re-register; hook isolation ---


class _Ws:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


def _client(settings: Settings, directory: Directory, **kw: Any) -> TrouterClient:
    c = TrouterClient(
        settings,
        {"access_token": "a"},
        "sk",
        {"surl": "https://surl.invalid/s/"},
        "sid",
        "epid-1",
        directory,
        **kw,
    )
    c._ws = _Ws()
    return c


class _Registrar:
    """Fake registrar: `ngc` / `messaging` = "ok", "fail" or "hang"; every body posted, in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.ngc, self.messaging = "ok", "ok"
        registrar = self

        class _Http:
            def __init__(self, *_a: Any, **_k: Any) -> None:
                pass

            async def __aenter__(self) -> _Http:
                return self

            async def __aexit__(self, *_: Any) -> bool:
                return False

            async def post(self, _url: str, headers: dict[str, str], json: Any) -> Any:
                registrar.bodies.append(json)
                ngc = json["clientDescription"]["appId"] == "NextGenCalling"
                mode = registrar.ngc if ngc else registrar.messaging
                if mode == "hang":
                    await asyncio.Event().wait()
                if mode == "fail":
                    raise OSError("registrar down")
                return type("R", (), {"status_code": 202, "raise_for_status": lambda self: None})()

        monkeypatch.setattr(S.httpx, "AsyncClient", _Http)

    @property
    def ngc_bodies(self) -> list[dict[str, Any]]:
        return [b for b in self.bodies if b["clientDescription"]["appId"] == "NextGenCalling"]


async def _noop(_req: dict[str, Any]) -> None:
    return None


async def _until_async(cond: Callable[[], bool], what: str) -> None:
    async with asyncio.timeout(5):
        while not cond():
            await asyncio.sleep(0.005)


@pytest.fixture
def no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    async def skip(self: TrouterClient) -> None:
        pass

    monkeypatch.setattr(TrouterClient, "_authenticate", skip)


async def test_ngc_is_registered_on_connect_and_on_every_reregister(
    settings: Settings, directory: Directory, monkeypatch: pytest.MonkeyPatch, no_auth: None
) -> None:
    """044-AC13"""
    reg = _Registrar(monkeypatch)
    client = _client(settings, directory, on_call=_noop)
    await client._on_connected()
    try:
        await _until_async(lambda: len(reg.ngc_bodies) == 1, "registered on connect")
        for n, reason in ((2, "message_loss"), (3, "ttl")):
            client._last_register -= S._REREGISTER_DEBOUNCE + 1
            await client._maybe_reregister(reason)
            await _until_async(lambda: len(reg.ngc_bodies) == n, reason)  # noqa: B023
    finally:
        for task in client._tasks:
            task.cancel()
    assert len(reg.bodies) == 6
    paths = {b["transports"]["TROUTER"][0]["path"] for b in reg.ngc_bodies}
    assert paths == {"https://surl.invalid/s/NGCallManagerWin"}
    ids = {b["registrationId"] for b in reg.ngc_bodies}
    assert len(ids) == 1 and "epid-1" not in ids  # one registration renewed, never the messaging one's id


async def test_without_the_hook_only_messaging_registers(
    settings: Settings, directory: Directory, monkeypatch: pytest.MonkeyPatch, no_auth: None
) -> None:
    reg = _Registrar(monkeypatch)
    client = _client(settings, directory)
    await client._on_connected()
    await asyncio.sleep(0.05)
    for task in client._tasks:
        task.cancel()
    assert [b["clientDescription"]["appId"] for b in reg.bodies] == [settings.registrar_app_id]


async def test_a_failed_ngc_registration_is_retried_with_backoff(
    settings: Settings, directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC13, 044-AC17: the socket stays; retries back off instead of hammering the registrar."""
    reg = _Registrar(monkeypatch)
    reg.ngc = "fail"
    monkeypatch.setattr(S, "_NGC_RETRY_MIN", 0.01)
    client = _client(settings, directory, on_call=_noop)
    await client._register()  # must not raise
    loop = asyncio.create_task(client._ngc_loop())
    try:
        await asyncio.sleep(0.35)  # backoff: tries at 0, .01, .03, .07, .15, .31; a flat retry: ~35
        tries = len(reg.ngc_bodies)
        assert 2 <= tries <= 8 and not client._ngc_ok
        reg.ngc = "ok"
        await _until_async(lambda: client._ngc_ok, "registered on a later retry")
        settled = len(reg.ngc_bodies)
        await asyncio.sleep(0.1)
        assert len(reg.ngc_bodies) == settled  # no more once registered
    finally:
        loop.cancel()


async def test_a_failed_messaging_reregister_still_renews_the_calling_one(
    settings: Settings, directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC13"""
    reg = _Registrar(monkeypatch)
    client = _client(settings, directory, on_call=_noop)
    loop = asyncio.create_task(client._ngc_loop())
    try:
        await _until_async(lambda: client._ngc_ok, "first registration")
        reg.messaging = "fail"
        with pytest.raises(OSError):
            await client._register()
        assert not client._ngc_ok
        await _until_async(lambda: client._ngc_ok and len(reg.ngc_bodies) == 2, "renewed anyway")
    finally:
        loop.cancel()


async def test_a_hanging_ngc_registrar_does_not_delay_the_keepalive(
    settings: Settings, directory: Directory, monkeypatch: pytest.MonkeyPatch, no_auth: None
) -> None:
    """044-AC13"""
    reg = _Registrar(monkeypatch)
    reg.ngc = "hang"
    monkeypatch.setattr(S, "_PING_INTERVAL", 0.01)
    client = _client(settings, directory, on_call=_noop)
    await asyncio.wait_for(client._on_connected(), 1)  # connect does not wait for the calling registration
    try:
        await _until_async(lambda: sum('"ping"' in f for f in client._ws.sent) >= 5, "pings keep flowing")
    finally:
        for task in client._tasks:
            task.cancel()
    assert len(reg.ngc_bodies) == 1  # still hanging


def test_ngc_registration_id_is_stable_per_endpoint(settings: Settings, directory: Directory) -> None:
    """044-AC13: reconnects renew one registration instead of piling new ones up."""
    a, b = _client(settings, directory), _client(settings, directory)
    assert a._ngc_id == b._ngc_id != "epid-1"
    other = TrouterClient(settings, {}, "sk", {}, "sid", "epid-2", directory)
    assert other._ngc_id != a._ngc_id


async def test_call_pushes_go_to_the_hook_only_and_its_failure_keeps_the_socket(
    settings: Settings, directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC13, 044-AC17: a trigger exception is logged; the frame is acked, the socket stays."""
    events: list[Any] = []

    async def on_event(obj: dict[str, Any]) -> None:
        events.append(obj)

    def boom(*_a: Any) -> None:
        raise RuntimeError("trigger bug")

    monkeypatch.setattr(F, "handle", boom)
    client = _client(settings, directory, on_event=on_event, on_call=F.trigger(settings, Path("data")))
    with capture_logs() as logs:
        await client._on_delivery(json.dumps(_push(115)))
        await _until_async(lambda: bool(logs), "the trigger's thread logged it")
    assert events == [] and client._ws.sent == ['3:::{"id":9,"status":200,"body":""}']
    assert [e["event"] for e in logs] == ["call_hook_failed"]


# --- 044-AC17: hosted by the archiver, detached from it ---


async def test_run_live_hosts_the_trigger_only_when_asked(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC17"""
    seen: list[Any] = []

    async def fake_forever(_s: Settings, **kw: Any) -> None:
        seen.append(kw["on_call"])

    monkeypatch.setattr(AL, "run_forever", fake_forever)
    await AL.run_live(settings, tmp_path / "data", token_provider=StaticToken("sk"))
    await AL.run_live(settings, tmp_path / "data", token_provider=StaticToken("sk"), follow=True)
    assert seen[0] is None and callable(seen[1])


def test_end_handled_by_another_trigger_instance(settings: Settings, tmp_path: Path) -> None:
    """044-AC17: the host that launched it is gone; a new trigger's 116 stops it, before or after its lock."""
    for call in (CALL, OTHER):
        data = tmp_path / call
        launcher = (
            "import json, sys\nfrom pathlib import Path\nfrom miniteams.follow import launch\n"
            "print(launch(Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])).pid)\n"
        )
        subprocess.run(
            [sys.executable, "-c", launcher, str(data), call, json.dumps(_payload(call=call))],
            capture_output=True,
            timeout=30,
            check=True,
        )
        log, hook = data / "follow" / f"{call}.log", F.trigger(settings, data)
        if call == OTHER:  # after its start: the pid + cmdline path
            _until(lambda: log.exists() and "follow_start" in log.read_text(), "follow_start")  # noqa: B023
        asyncio.run(hook(_push(116, call=call)))
        _until(lambda: "follow_stop" in log.read_text(), "follow_stop")  # noqa: B023
        _until(lambda: not F.lock_held(F.lock_path(data, call)), "lock freed")  # noqa: B023


def test_follower_survives_its_launcher(tmp_path: Path) -> None:
    """044-AC17: the launcher (the trigger's host) exits; the follower, in its own session, keeps running."""
    data = tmp_path / "data"
    launcher = (
        "import json, sys\nfrom pathlib import Path\nfrom miniteams.follow import launch\n"
        "print(launch(Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])).pid)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", launcher, str(data), CALL, json.dumps(_payload())],
        capture_output=True,
        timeout=30,
        check=True,
    )
    pid, path = int(out.stdout.splitlines()[-1]), F.lock_path(data, CALL)
    try:
        _until(lambda: F.lock_held(path), "detached follower holds the lock")
        assert os.getsid(pid) == pid != os.getsid(0)
        F.stop(data, CALL, _payload())
        _until(lambda: not F.lock_held(path), "follower stopped")
    finally:
        if F.lock_held(path):
            os.kill(pid, signal.SIGKILL)


# --- 044-AC15: the extra stays optional ---


def test_cli_and_archiver_never_import_the_transcript_modules() -> None:
    """044-AC15"""
    code = (
        "import sys, miniteams.cli, miniteams.archive_live, miniteams.follow\n"
        f"print([m for m in {F.TRANSCRIPT_MODULES!r} if m in sys.modules])\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True)
    assert out.stdout.strip() == "[]"


def test_missing_extra_names_the_absent_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """044-AC15"""
    monkeypatch.setattr(F.importlib.util, "find_spec", lambda m: None if m == "onnxruntime" else object())
    assert F.missing_extra() == ["onnxruntime"]


@pytest.mark.parametrize("outcome", [None, KeyboardInterrupt()])
def test_cli_starts_the_trigger_on_its_own_endpoint(
    outcome: BaseException | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC15: with the extra, `follow` hosts the trigger; exit 1 on a dead login, 0 on Ctrl-C."""
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)
    monkeypatch.setattr(F, "missing_extra", lambda: [])
    seen: dict[str, Any] = {}

    async def fake_forever(_s: Settings, **kw: Any) -> None:
        seen.update(kw)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(S, "run_forever", fake_forever)
    assert cli.main(["follow"]) == (0 if outcome else 1)
    assert seen["epid_name"] == "endpoint_id-follow" and callable(seen["on_call"])
    assert asyncio.run(seen["on_event"]({})) is None


def test_cli_call_runs_the_follower(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)
    got: list[Any] = []
    monkeypatch.setattr(F, "run_call", lambda d, push: got.append((d, push)) or 0)
    assert cli.main(["follow", "--data-dir", str(tmp_path), "--call", "{}"]) == 0
    assert got == [(tmp_path.resolve(), "{}")]


@pytest.mark.parametrize("argv", [["follow"], ["archive", "--live", "--follow"]])
def test_trigger_refuses_to_start_without_the_extra(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """044-AC15"""
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)
    monkeypatch.setattr(F, "missing_extra", lambda: ["onnxruntime"])
    monkeypatch.setattr(
        "miniteams.auth.token_source", lambda _s: type("T", (), {"acquire": lambda self: None})()
    )
    monkeypatch.setattr(S, "run_forever", lambda *_a, **_k: pytest.fail("connected without the extra"))
    with capture_logs() as logs:
        assert cli.main(argv) == 1
    missing = next(e for e in logs if e["event"] == "follow_extra_missing")
    assert missing["missing"] == ["onnxruntime"] and "uv sync --inexact --extra transcript" in missing["hint"]


def test_end_still_signals_when_the_marker_cannot_be_written(
    settings: Settings, tmp_path: Path, procs: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_a: Any) -> None:
        raise OSError("read-only data dir")

    monkeypatch.setattr(F, "_mark_ended", refuse)
    procs.append(_holder(F.lock_path(tmp_path, CALL), "follow", "--call", json.dumps(_payload())))
    with capture_logs() as logs:
        F.handle(settings, tmp_path, _push(116))
    assert procs[0].wait(timeout=10) == -signal.SIGTERM
    assert [e["event"] for e in logs] == ["follow_ended_marker_failed", "follow_stop_sent"]
