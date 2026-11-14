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
from test_stream import _client

from miniteams import archive_live as AL
from miniteams import cli
from miniteams import follow as F
from miniteams import stream as S
from miniteams.archive import StaticToken
from miniteams.config import Settings
from miniteams.directory import Directory

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
def _stt_runtime(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """044-AC10: `follow` and `follow --call` start only with a library and a model; empty stand-ins, by
    env so the followers these tests spawn see them too."""
    root = tmp_path_factory.mktemp("stt")
    for name, var in (("lib.so", "MINITEAMS_TRANSCRIBE_LIBRARY"), ("model.gguf", "MINITEAMS_PARAKEET_MODEL")):
        (root / name).touch()
        monkeypatch.setenv(var, str(root / name))
    # The followers' engines load while they join: a stand-in binding they load at once.
    (root / "transcribe_cpp.py").write_text(_FAKE_BINDING)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(root), os.environ.get("PYTHONPATH")])))


_FAKE_BINDING = """
class _Session:
    def run(self, audio):
        return type("R", (), {"text": ""})()


class Model:
    def __init__(self, path, backend):
        pass

    def session(self, n_threads):
        return _Session()


def set_log_callback(fn):
    pass
"""


@pytest.fixture(autouse=True)
def _offline_followers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The followers these tests spawn try to sign in: a dead proxy keeps them off the internet."""
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_user_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Followers these tests spawn run as plain children: no transient scope on the host's systemd."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)


@pytest.fixture(autouse=True)
def _info_logs() -> None:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO)
    )  # the skips log at info


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(F, "launch", lambda _s, _d, call, _p: seen.append(call))
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
    """run_call in this process: no real signal mask, wait or wake-up (they would reach pytest), no real
    join, a SIGTERM stop; records waits."""
    waits: list[str] = []
    monkeypatch.setattr(F, "_attend", lambda _s, _d, _p, stop: "attended" if stop.wait(5) else "late")

    def sigwait(_sigs: Any) -> int:
        waits.append("wait")
        return signal.SIGTERM

    monkeypatch.setattr(F.signal, "pthread_sigmask", lambda *_a: set())
    monkeypatch.setattr(F.signal, "sigwait", sigwait)
    monkeypatch.setattr(F.signal, "pthread_kill", lambda *_a: None)
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


def _argv(settings: Settings, payload: dict[str, Any]) -> list[str]:
    """A follower's cmdline tail, its push file written as the trigger does."""
    path = F.push_path(settings, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return ["follow", "--call-file", str(path)]


def _env(settings: Settings) -> dict[str, str]:
    """A follower run by hand shares the trigger's config dir, as launch() makes it."""
    return {**os.environ, "MINITEAMS_CONFIG_DIR": str(settings.config_dir)}


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
    pushes = (_payload(), _one_to_one(), _payload(thread="19:fakegroup@thread.v2"), _one_to_one([]))
    assert [F.call_kind(p) for p in pushes] == ["meeting", "1:1", "group", "1:1"]


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("0", "group"),
        (None, "group"),
        ("", "group"),
        ("1731600000000", "meeting"),
        (1731600000000, "meeting"),
    ],
)
def test_a_channel_meeting_is_a_meeting(message: Any, kind: str) -> None:
    """A channel meeting names its channel thread and the meeting's post; an ad hoc group call names its
    chat and messageId "0" (seen live), the caller as organizer."""
    push = _payload(thread="19:fakechannel@thread.tacv2")
    push["groupContext"]["messageId"] = message
    assert F.call_kind(push) == kind
    group = _payload(thread="19:fakegroup@thread.v2")  # live shape: meetingInfo too, messageId "0"
    assert F.call_kind(group) == "group" and F.call_kind(_payload()) == "meeting"


def test_decode_reads_every_delivery_encoding() -> None:
    """A transport-gzipped body, or a `gp` (base64 JSON) payload, decodes too."""
    plain = _push(115)
    gz = base64.b64encode(gzip.compress(plain["body"].encode())).decode()
    zipped = {**plain, "headers": {"X-Microsoft-Skype-Content-Encoding": "gzip"}, "body": gz}
    assert F.decode_push(zipped) == (115, _payload())
    gp = base64.b64encode(json.dumps(_payload()).encode()).decode()
    assert F.decode_push({"body": json.dumps({"evt": 116, "gp": gp})}) == (116, _payload())
    assert F.decode_push({"body": json.dumps({"evt": 116})}) is None  # no payload
    assert F.decode_push({"body": json.dumps([115])}) is None


def test_044_a_blank_thread_with_meeting_info_is_no_1to1() -> None:
    """One predicate: neither groupContext nor meetingInfo. A blank thread next to meetingInfo is not it."""
    blank = {**_one_to_one(), "groupContext": {"threadId": "", "messageId": "0"}}
    blank["meetingInfo"] = _payload()["meetingInfo"]
    assert not F.is_one_to_one(blank) and F.call_kind(blank) == "group" and F.call_thread(blank) == ""
    assert F.is_one_to_one({**_one_to_one(), "groupContext": None, "meetingInfo": None})


ME = "8:orgid:00000000-0000-4000-8000-0000000000b0"
PEER = "8:orgid:00000000-0000-4000-8000-0000000000a0"
DM_SORTED = "19:00000000-0000-4000-8000-0000000000a0_00000000-0000-4000-8000-0000000000b0@unq.gbl.spaces"
DM_UNSORTED = "19:00000000-0000-4000-8000-0000000000b0_00000000-0000-4000-8000-0000000000a0@unq.gbl.spaces"


def _one_to_one(people: list[Any] | None = None) -> dict[str, Any]:
    """A 1:1 push (seen live): no groupContext nor meetingInfo, the two people in additionalData."""
    return {
        "correlationId": CALL,
        "conversationController": "https://cc.example.invalid/x",
        "userJoinTime": JOIN,
        "additionalData": {"participantList": [{"id": ME}, {"id": PEER}] if people is None else people},
    }


def test_044_a_1to1_push_names_its_chat() -> None:
    """A 1:1 push has no thread: its two participants give the chat id, sorted, whatever their order."""
    assert F.call_thread(_one_to_one()) == DM_SORTED
    assert F.call_thread(_one_to_one([{"id": PEER}, {"id": ME}])) == DM_SORTED
    assert F.call_kind(_one_to_one()) == "1:1"
    for people in ([{"id": ME}], [{"id": ME}, {"id": PEER}, {"id": OTHER}], [{"id": ME}, {"id": ME}]):
        assert F.call_thread(_one_to_one(people)) == ""  # not two distinct people
    assert F.call_thread({**_one_to_one(), "additionalData": None}) == ""
    assert F.call_thread(_one_to_one([{"id": "28:bot"}, {"id": PEER}])) == ""  # no org user: no 1:1 id


@pytest.mark.parametrize("listed", [DM_SORTED, DM_UNSORTED])
def test_044_a_1to1_follows_the_skip_list_in_either_spelling(
    settings: Settings, tmp_path: Path, launches: list[str], listed: str
) -> None:
    """044-AC11 for a 1:1: the skip list names the chat, either spelling of its id."""
    F.save_switch(settings, True, [listed])
    cp = base64.b64encode(gzip.compress(json.dumps(_one_to_one()).encode())).decode()
    with capture_logs() as logs:
        F.handle(settings, tmp_path, {"body": json.dumps({"evt": 115, "cp": cp})})
    assert [(e["thread"], e["reason"]) for e in logs if e["event"] == "follow_skipped"] == [
        (DM_SORTED, "skip_list")
    ]
    assert launches == []


def test_044_the_hook_reads_no_archive_index(settings: Settings, tmp_path: Path, launches: list[str]) -> None:
    """The trigger shares the archive's data dir, often on NFS: a read-only open of its WAL index would
    leave -wal/-shm files behind."""
    from miniteams.archive_store import Index

    index = Index(tmp_path)
    index.upsert_chat(DM_UNSORTED, label="dm", raw={})
    index.close()
    cp = base64.b64encode(gzip.compress(json.dumps(_one_to_one()).encode())).decode()
    F.handle(settings, tmp_path, {"body": json.dumps({"evt": 115, "cp": cp})})
    assert launches == [CALL]
    assert sorted(p.name for p in tmp_path.glob("index.db*")) == ["index.db"]


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
    fd = F.take_lock(F.lock_path(settings, CALL))
    assert fd is not None
    try:
        F.handle(settings, tmp_path, _push(115))  # redelivered
        F.handle(settings, tmp_path, _push(115))  # the follower's own join, same correlationId
        F.handle(settings, tmp_path, _push(115, call=OTHER))  # another call is not blocked
    finally:
        os.close(fd)
    assert launches == [OTHER]


def test_two_triggers_racing_on_one_lock(settings: Settings, tmp_path: Path) -> None:
    """044-AC3: the second taker loses, whichever trigger launched it; free again once released."""
    path = F.lock_path(settings, CALL)
    first = F.take_lock(path)
    assert first is not None
    assert F.take_lock(path) is None and F.lock_held(path)
    os.close(first)
    assert not F.lock_held(path)


@pytest.mark.transcript
def test_two_followers_of_one_call_leave_one_running(
    settings: Settings, tmp_path: Path, procs: list[Any]
) -> None:
    """044-AC3: two real follower processes for one call: the second exits 0 on the held lock."""
    cmd = F.follower_command(settings, tmp_path, Path(_argv(settings, _payload())[-1]))
    procs.append(subprocess.Popen(cmd, env=_env(settings)))
    _until(lambda: F.lock_held(F.lock_path(settings, CALL)), "first follower holds the lock")
    second = subprocess.run(cmd, capture_output=True, timeout=30, env=_env(settings))
    assert second.returncode == 0 and b"follow_already_running" in second.stderr
    assert procs[0].poll() is None


def test_a_crashed_holder_releases_the_lock(settings: Settings, tmp_path: Path, procs: list[Any]) -> None:
    """044-AC3: the kernel drops the flock when the holder dies, even by SIGKILL."""
    path = F.lock_path(settings, CALL)
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


def test_cli_skip_and_unskip_take_a_1to1_in_either_spelling(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC11: the switch's skip list matches as the trigger does (presence.same_chat)."""
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)
    for argv in (["--skip", DM_SORTED], ["--skip", DM_UNSORTED], ["--skip", MEETING]):
        assert cli.main(["follow", *argv]) == 0
    skip = json.loads(capsys.readouterr().out.splitlines()[-1])["skip"]
    assert skip == [DM_SORTED, MEETING]  # the other spelling added nothing
    assert cli.main(["follow", "--unskip", DM_UNSORTED.upper()]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["skip"] == [MEETING]
    F.save_switch(Settings(), True, [DM_SORTED, DM_UNSORTED, MEETING])  # a hand-edited file holding both
    assert cli.main(["follow", "--unskip", DM_SORTED]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["skip"] == [MEETING]


# --- evt 116: stop ---


SENTINEL = "00000000-0000-4000-8000-0000000000ff"


def _sentinel_stopped(settings: Settings, tmp_path: Path, procs: list[Any]) -> None:
    """A 116 to a third follower, handled after the ones under test, until it dies: a SIGTERM they sent
    went out earlier and had longer to land."""
    procs.append(_holder(F.lock_path(settings, SENTINEL), *_argv(settings, _payload(call=SENTINEL))))
    F.handle(settings, tmp_path, _push(116, call=SENTINEL))
    assert procs[-1].wait(timeout=10) == -signal.SIGTERM


def test_end_signals_our_follower(settings: Settings, tmp_path: Path, procs: list[Any]) -> None:
    path = F.lock_path(settings, CALL)
    procs.append(_holder(path, *_argv(settings, _payload())))
    F.handle(settings, tmp_path, _push(116))
    assert procs[0].wait(timeout=10) == -signal.SIGTERM


def test_end_never_signals_a_pid_that_is_not_our_follower(
    settings: Settings, tmp_path: Path, procs: list[Any]
) -> None:
    path = F.lock_path(settings, CALL)
    procs.append(_holder(path, *_argv(settings, _payload(call=OTHER))))  # another call's
    procs.append(_holder(F.lock_path(settings, OTHER), "not-a-follower"))
    F.handle(settings, tmp_path, _push(116))
    F.handle(settings, tmp_path, _push(116, call=OTHER))
    _sentinel_stopped(settings, tmp_path, procs)
    assert procs[0].poll() is None and procs[1].poll() is None


def test_end_while_the_pid_line_is_empty_stops_the_follower_once_locked(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_process: list[str]
) -> None:
    """A follower between its lock and its pid line: 116 leaves the marker it reads next."""
    fd = F.take_lock(F.lock_path(settings, CALL))  # locked, pid line not written yet
    assert fd is not None
    killed: list[int] = []
    monkeypatch.setattr(F.os, "kill", lambda pid, _sig: killed.append(pid))
    with capture_logs() as logs:
        F.handle(settings, tmp_path, _push(116))
    os.close(fd)
    assert killed == [] and [e["event"] for e in logs] == ["follow_lock_unreadable"]
    with capture_logs() as logs:
        assert F.run_call(settings, tmp_path, _payload()) == 0  # the follower's next step
    assert in_process == [] and not F.lock_held(F.lock_path(settings, CALL))
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
    assert launches == [CALL] and F.ended_joins(settings, CALL) == {JOIN}


J2 = "2026-01-01T00:30:00Z"


def test_a_late_end_of_an_earlier_join_spares_the_rejoin(
    settings: Settings, tmp_path: Path, procs: list[Any], launches: list[str]
) -> None:
    """044-AC3: 115 J1, 116 J1, 115 J2, then 116 J1 again: the J2 follower stays until 116 J2."""
    F.handle(settings, tmp_path, _push(115))
    F.handle(settings, tmp_path, _push(116))
    F.handle(settings, tmp_path, _push(115, join=J2))
    assert launches == [CALL, CALL]
    procs.append(_holder(F.lock_path(settings, CALL), *_argv(settings, _payload(join=J2))))
    with capture_logs() as logs:
        F.handle(settings, tmp_path, _push(116))  # late, J1
        F.handle(settings, tmp_path, _push(115, join=J2))  # redelivered
    _sentinel_stopped(settings, tmp_path, procs)
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
        assert F.run_call(settings, tmp_path, _payload(join=J2)) == 0
    assert [e["event"] for e in logs][0] == "follow_start" and in_process == ["wait"]
    F.handle(settings, tmp_path, _push(116, join=J2))
    with capture_logs() as logs:
        # a J2 follower starting late
        assert F.run_call(settings, tmp_path, _payload(join=J2)) == 0
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
    procs.append(_holder(F.lock_path(settings, CALL), *_argv(settings, _payload(join=J2))))
    F.handle(settings, tmp_path, _push(116, join=""))
    assert procs[0].wait(timeout=10) == -signal.SIGTERM
    assert not F.ended_path(settings, CALL).exists()
    F.handle(settings, tmp_path, _push(116))  # J1 ended: one line
    F.handle(settings, tmp_path, _push(115, join=""))
    assert launches == [CALL] and F.ended_path(settings, CALL).read_text() == f"{JOIN}\n"
    with capture_logs() as logs:
        assert F.run_call(settings, tmp_path, _payload(join="")) == 0
    assert [e["event"] for e in logs][0] == "follow_start" and in_process == ["wait"]


@pytest.mark.parametrize(
    "error", [PermissionError("not the owner"), OSError(errno.ENOTSUP, "no modes on this mount")]
)
def test_a_refused_chmod_costs_privacy_not_the_launch(
    settings: Settings, tmp_path: Path, procs: list[Any], monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    data = tmp_path / "data"
    (data / "follow").mkdir(parents=True)
    os.chmod(data / "follow", 0o755)

    def refuse(*_a: Any) -> None:
        raise error

    monkeypatch.setattr(F.os, "chmod", refuse)
    with capture_logs() as logs:
        procs.append(F.launch(settings, data, CALL, _payload(call=CALL)))
    assert "follow_dir_not_private" in [e["event"] for e in logs]
    assert (data / "follow" / f"{CALL}.log").exists()


def test_a_private_dir_is_not_chmodded_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "follow").mkdir(mode=0o700)
    calls: list[Any] = []
    monkeypatch.setattr(F.os, "chmod", lambda *a: calls.append(a))
    F.follow_dir(tmp_path)
    assert calls == []


@pytest.mark.transcript
def test_per_call_files_are_private(settings: Settings, tmp_path: Path, procs: list[Any]) -> None:
    data = tmp_path / "data"
    (data / "follow").mkdir(parents=True)
    os.chmod(data / "follow", 0o755)  # made by an older run, or by hand
    procs.append(F.launch(settings, data, CALL, _payload(call=CALL)))
    F.handle(settings, data, _push(116, call=OTHER))
    modes = {
        f.name: stat.S_IMODE(f.stat().st_mode)
        for f in (data / "follow", data / "follow" / f"{CALL}.log", F.ended_path(settings, OTHER))
    }
    assert modes == {"follow": 0o700, f"{CALL}.log": 0o600, f"{OTHER}.ended": 0o600}
    _until(lambda: F.lock_held(F.lock_path(settings, CALL)), "follower up")
    assert stat.S_IMODE(F.lock_path(settings, CALL).stat().st_mode) == 0o600
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


def test_odd_join_times_and_a_corrupt_marker(settings: Settings, tmp_path: Path) -> None:
    assert F.join_time({"userJoinTime": f"{J2}\n{JOIN}"}) == "" and F.join_time({"userJoinTime": 5}) == ""
    path = F.ended_path(settings, CALL)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\n" + JOIN.encode() + b"\n")
    assert JOIN in F.ended_joins(settings, CALL)


def test_a_join_is_marked_ended_once(settings: Settings, tmp_path: Path) -> None:
    """The follower and the trigger's 116 may both mark one join: one line, other joins still added."""
    for join in (JOIN, JOIN, J2, JOIN):
        F.mark_ended(settings, CALL, join)
    assert F.ended_path(settings, CALL).read_text() == f"{JOIN}\n{J2}\n"


def test_a_probe_does_not_make_a_starting_follower_give_up(settings: Settings, tmp_path: Path) -> None:
    """A trigger's lock_held probe holding the lock for an instant: take_lock waits it out."""
    path = F.lock_path(settings, CALL)
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
    path = F.lock_path(settings, CALL)
    path.parent.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    path.write_text(f"{dead.pid}\n")  # left by a follower that died; nobody holds it
    killed: list[int] = []
    monkeypatch.setattr(F.os, "kill", lambda pid, _sig: killed.append(pid))
    with capture_logs() as logs:
        F.handle(settings, tmp_path, _push(116))
    assert killed == [] and [e["event"] for e in logs] == ["follow_not_running"]
    assert F.ended_path(settings, CALL).read_text() == f"{JOIN}\n"


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


@pytest.mark.transcript
def test_join_then_end_runs_the_real_follower(
    settings: Settings, tmp_path: Path, real_launches: list[Any]
) -> None:
    """044-AC3: 115 launches `miniteams follow --call`, it locks and logs; 116 stops it and frees the lock."""
    procs = real_launches
    data = tmp_path / "data"
    F.handle(settings, data, _push(115))
    path, log = F.lock_path(settings, CALL), data / "follow" / f"{CALL}.log"
    _until(lambda: log.exists() and "follow_start" in log.read_text(), "follow_start logged")
    assert F.lock_held(path)
    start = next(json.loads(line) for line in log.read_text().splitlines() if "follow_start" in line)
    assert (start["call"], start["thread"], start["kind"]) == (CALL, MEETING, "meeting")
    F.handle(settings, data, _push(115))  # self-join while it runs: no second follower
    F.handle(settings, data, _push(116))
    _until(lambda: "follow_stop" in log.read_text() and not F.lock_held(path), "follow_stop and lock freed")
    assert log.read_text().count("follow_start") == 1 and len(procs) == 1
    _until(lambda: procs[0].returncode == 0, "follower reaped by its host")  # the reaper waited, not us


@pytest.mark.transcript
def test_join_and_end_back_to_back_leave_nothing_running(
    settings: Settings, tmp_path: Path, real_launches: list[Any]
) -> None:
    """044-AC3: 116 lands before the follower holds its lock."""
    F.handle(settings, tmp_path, _push(115))
    F.handle(settings, tmp_path, _push(116))
    _until(lambda: real_launches[0].returncode is not None, "follower exited and reaped")
    assert real_launches[0].returncode == 0 and not F.lock_held(F.lock_path(settings, CALL))
    log = (tmp_path / "follow" / f"{CALL}.log").read_text()
    assert "ended_before_start" in log and "follow_start" not in log


@pytest.mark.transcript
def test_a_follower_losing_the_race_is_reaped(settings: Settings, tmp_path: Path, procs: list[Any]) -> None:
    """A long-running host leaves no zombie behind."""
    fd = F.take_lock(F.lock_path(settings, CALL))
    try:
        proc = F.launch(settings, tmp_path, CALL, _payload())
        procs.append(proc)
        _until(lambda: proc.returncode is not None, "reaped without our wait")
    finally:
        assert fd is not None
        os.close(fd)
    assert proc.returncode == 0


def test_run_call_in_process(settings: Settings, tmp_path: Path, in_process: list[str]) -> None:
    """The stub's own lines, measured in this process (the subprocess runs cover the real signals)."""
    with capture_logs() as logs:
        assert F.run_call(settings, tmp_path, None) == 2
        assert F.run_call(settings, tmp_path, {"correlationId": "x"}) == 2
        assert F.run_call(settings, tmp_path, _payload()) == 0
        fd = F.take_lock(F.lock_path(settings, CALL))
        assert F.run_call(settings, tmp_path, _payload()) == 0
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
    assert in_process == ["wait"] and F.lock_path(settings, CALL).read_text().strip().isdigit()


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


@pytest.mark.transcript
def test_end_handled_by_another_trigger_instance(settings: Settings, tmp_path: Path) -> None:
    """044-AC17: the host that launched it is gone; a new trigger's 116 stops it, before or after its lock."""
    for call in (CALL, OTHER):
        data = tmp_path / call
        launcher = (
            "import json, sys\nfrom pathlib import Path\nfrom miniteams.config import Settings\n"
            "from miniteams.follow import launch\n"
            "print(launch(Settings(), Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])).pid)\n"
        )
        subprocess.run(
            [sys.executable, "-c", launcher, str(data), call, json.dumps(_payload(call=call))],
            capture_output=True,
            timeout=30,
            check=True,
            env=_env(settings),
        )
        log, hook = data / "follow" / f"{call}.log", F.trigger(settings, data)
        if call == OTHER:  # after its start: the pid + cmdline path
            _until(lambda: log.exists() and "follow_start" in log.read_text(), "follow_start")  # noqa: B023
        asyncio.run(hook(_push(116, call=call)))
        _until(lambda: "follow_stop" in log.read_text(), "follow_stop")  # noqa: B023
        _until(lambda: not F.lock_held(F.lock_path(settings, call)), "lock freed")  # noqa: B023


@pytest.mark.transcript
def test_follower_survives_its_launcher(tmp_path: Path) -> None:
    """044-AC17: the launcher (the trigger's host) exits; the follower, in its own session, keeps running."""
    data, settings = tmp_path / "data", Settings()  # the launcher's own, from the same environment
    launcher = (
        "import json, sys\nfrom pathlib import Path\nfrom miniteams.config import Settings\n"
        "from miniteams.follow import launch\n"
        "print(launch(Settings(), Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])).pid)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", launcher, str(data), CALL, json.dumps(_payload())],
        capture_output=True,
        timeout=30,
        check=True,
    )
    pid, path = int(out.stdout.splitlines()[-1]), F.lock_path(settings, CALL)
    try:
        _until(lambda: F.lock_held(path), "detached follower holds the lock")
        assert os.getsid(pid) == pid != os.getsid(0)
        F.stop_follower(settings, CALL, _payload())
        _until(lambda: not F.lock_held(path), "follower stopped")
    finally:
        if F.lock_held(path):
            os.kill(pid, signal.SIGKILL)


# --- 044-AC15: the extra stays optional ---


def test_only_the_followers_modules_import_the_transcript_modules() -> None:
    """044-AC15: every module but the follower's own (and the widget's, its own extra) imports without
    loading one."""
    code = (
        "import pkgutil, sys, miniteams\n"
        "own = {'miniteams.call', 'miniteams.follower', 'miniteams.widget_qt'}\n"
        "names = [m.name for m in pkgutil.walk_packages(miniteams.__path__, 'miniteams.')]\n"
        "for name in names:\n"
        "    if name not in own and name.split('.')[:2] != ['miniteams', 'stt']:\n"
        "        __import__(name)\n"
        "print(len(names), [m for m in sys.argv[1:] if m in sys.modules])\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, *F.TRANSCRIPT_MODULES],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    count, loaded = out.stdout.split(" ", 1)
    assert int(count) > 40 and loaded.strip() == "[]"


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


@pytest.mark.transcript
def test_cli_call_runs_the_follower(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)
    got: list[Any] = []
    monkeypatch.setattr(F, "run_call", lambda _s, d, push: got.append((d, push)) or 0)
    push = tmp_path / "push.json"
    push.write_text(json.dumps(_payload()))
    assert cli.main(["follow", "--data-dir", str(tmp_path), "--call-file", str(push)]) == 0
    push.write_text("not json")
    assert cli.main(["follow", "--data-dir", str(tmp_path), "--call-file", str(push)]) == 0
    missing = str(tmp_path / "gone.json")
    assert cli.main(["follow", "--data-dir", str(tmp_path), "--call-file", missing]) == 0
    # A trigger started before --call-file still passes the push itself.
    assert cli.main(["follow", "--data-dir", str(tmp_path), "--call", json.dumps(_payload())]) == 0
    assert cli.main(["follow", "--data-dir", str(tmp_path), "--call", "[]"]) == 0
    assert cli.main(["follow", "--data-dir", str(tmp_path), "--call", "{bad"]) == 0
    assert got == [(tmp_path.resolve(), p) for p in (_payload(), None, None, _payload(), None, None)]


@pytest.mark.parametrize("argv", [["follow"], ["archive", "--live", "--follow"]])
def test_trigger_refuses_to_start_without_the_extra(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """044-AC10, AC15, AC17: `follow` refuses; the archiver logs the fix and archives without the trigger."""
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)
    monkeypatch.setattr(F, "missing_extra", lambda: ["onnxruntime"])
    monkeypatch.setattr(
        "miniteams.auth.token_source", lambda _s: type("T", (), {"acquire": lambda self: None})()
    )
    monkeypatch.setattr(S, "run_forever", lambda *_a, **_k: pytest.fail("connected without the extra"))
    archived: list[Any] = []

    async def run_live(*_a: Any, **kw: Any) -> None:
        archived.append(kw["follow"])

    monkeypatch.setattr(AL, "run_live", run_live)
    with capture_logs() as logs:
        assert cli.main(argv) == 1  # the archiver's run_live returns on a dead login: exit 1 as ever
    missing = next(e for e in logs if e["event"] == "follow_not_ready")
    assert "(onnxruntime)" in missing["fix"] and "uv sync --inexact --extra transcript" in missing["fix"]
    assert archived == ([False] if argv[0] == "archive" else [])


def test_end_still_signals_when_the_marker_cannot_be_written(
    settings: Settings, tmp_path: Path, procs: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_a: Any) -> None:
        raise OSError("read-only data dir")

    monkeypatch.setattr(F, "mark_ended", refuse)
    procs.append(_holder(F.lock_path(settings, CALL), *_argv(settings, _payload())))
    with capture_logs() as logs:
        F.handle(settings, tmp_path, _push(116))
    assert procs[0].wait(timeout=10) == -signal.SIGTERM
    assert [e["event"] for e in logs] == ["follow_ended_marker_failed", "follow_stop_sent"]


# --- phase 3: the follower thread ---


@pytest.mark.parametrize("outcome", ["call_ended", "join_failed", "error"])
def test_a_follower_ending_on_its_own_marks_its_join_and_exits_without_waiting_for_116(
    settings: Settings, tmp_path: Path, in_process: list[str], monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """044-AC4, AC5, AC14: no wait for a SIGTERM (evt 116 only comes once the follower is gone): it marks
    its join ended, so a redelivered 115 of it is skipped, and wakes its own wait."""
    kicked = threading.Event()
    monkeypatch.setattr(F, "_attend", lambda *_a: outcome)
    monkeypatch.setattr(F.signal, "pthread_kill", lambda _tid, sig: kicked.set() if sig == F._DONE else None)
    monkeypatch.setattr(F.signal, "sigwait", lambda _s: F._DONE if kicked.wait(5) else signal.SIGTERM)
    t0 = time.monotonic()
    with capture_logs() as logs:
        assert F.run_call(settings, tmp_path, _payload()) == 0
    assert time.monotonic() - t0 < 2
    assert [(e["event"], e.get("reason")) for e in logs] == [
        ("follow_start", None),
        ("follow_stop", outcome),
    ]
    assert F.ended_path(settings, CALL).read_text() == f"{JOIN}\n"
    assert not F.lock_held(F.lock_path(settings, CALL))


def test_a_join_the_follower_already_marked_is_not_marked_twice(
    settings: Settings, tmp_path: Path, in_process: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """follow() marks an ended call itself, before its hang-up; run_call adds no second line."""
    kicked = threading.Event()

    def attend(_s: Settings, data: Path, payload: dict[str, Any], _stop: threading.Event) -> str:
        F.mark_ended(settings, CALL, F.join_time(payload))
        return "call_ended"

    monkeypatch.setattr(F, "_attend", attend)
    monkeypatch.setattr(F.signal, "pthread_kill", lambda _tid, sig: kicked.set() if sig == F._DONE else None)
    monkeypatch.setattr(F.signal, "sigwait", lambda _s: F._DONE if kicked.wait(5) else signal.SIGTERM)
    assert F.run_call(settings, tmp_path, _payload()) == 0
    assert F.ended_path(settings, CALL).read_text() == f"{JOIN}\n"


def test_a_follower_whose_marker_fails_still_exits(
    settings: Settings, tmp_path: Path, in_process: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    kicked = threading.Event()
    monkeypatch.setattr(F, "_attend", lambda *_a: "call_ended")
    monkeypatch.setattr(F.signal, "pthread_kill", lambda _tid, sig: kicked.set() if sig == F._DONE else None)
    monkeypatch.setattr(F.signal, "sigwait", lambda _s: F._DONE if kicked.wait(5) else signal.SIGTERM)

    def broken(*_a: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(F, "mark_ended", broken)
    with capture_logs() as logs:
        assert F.run_call(settings, tmp_path, _payload()) == 0
    assert [e["event"] for e in logs] == ["follow_start", "follow_ended_marker_failed", "follow_stop"]


@pytest.mark.transcript
def test_a_rejoin_while_the_previous_follower_winds_down_gets_its_own(
    settings: Settings, tmp_path: Path, real_launches: list[Any]
) -> None:
    """044-AC4: the J1 follower left on its own (its join marked ended) and still hangs up; my rejoin's 115
    (J2) launches a follower that waits for the lock instead of being taken for J1's; a redelivered 115 J1
    and a late 116 J1 touch neither."""
    data = tmp_path / "data"
    lock = F.lock_path(settings, CALL)
    holder = _holder(lock, *_argv(settings, _payload()))  # J1, still winding down
    real_launches.append(holder)
    with capture_logs() as logs:
        F.handle(settings, data, _push(115))  # J1 redelivered while it runs: dropped
    assert [e["event"] for e in logs] == ["follow_already_running"]
    F.mark_ended(settings, CALL, JOIN)  # what the J1 follower does before its DELETE
    F.handle(settings, data, _push(115, join=J2))
    log = data / "follow" / f"{CALL}.log"
    _until(lambda: log.exists() and "follow_waiting_for_previous" in log.read_text(), "J2 waiting")
    assert "follow_start" not in log.read_text() and len(real_launches) == 2
    F.handle(settings, data, _push(115))  # J1 redelivered once ended: skipped
    holder.kill()
    holder.wait()
    _until(lambda: "follow_start" in log.read_text(), "J2 started once J1 let go")
    _until(lambda: _follower_pid(lock) == real_launches[1].pid, "J2 holds the lock")
    with capture_logs() as logs:
        F.handle(settings, data, _push(116))  # late 116 J1
    assert [e["event"] for e in logs] == ["follow_stop_other_join"]
    F.handle(settings, data, _push(116, join=J2))
    _until(lambda: real_launches[1].returncode is not None, "J2 stopped by its own 116")
    assert len(real_launches) == 2


def _follower_pid(lock: Path) -> int | None:
    try:
        return int(lock.read_text().strip())
    except OSError, ValueError:
        return None


def test_a_rejoin_while_the_previous_follower_still_follows_is_dropped(
    settings: Settings, tmp_path: Path, launches: list[str], procs: list[Any]
) -> None:
    """The J1 follower has not ended: whatever J2 is, the call has its follower (a stale holder never
    makes a second one join)."""
    procs.append(_holder(F.lock_path(settings, CALL), *_argv(settings, _payload())))
    F.handle(settings, tmp_path, _push(115, join=J2))
    assert launches == []
    F.mark_ended(settings, CALL, J2)  # another join's marker
    F.handle(settings, tmp_path, _push(115, join="2026-01-01T01:00:00Z"))
    assert launches == []


def test_a_rejoin_whose_previous_follower_never_lets_go_gives_up(
    settings: Settings,
    tmp_path: Path,
    procs: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    in_process: list[str],
) -> None:
    procs.append(_holder(F.lock_path(settings, CALL), *_argv(settings, _payload())))
    F.mark_ended(settings, CALL, JOIN)
    monkeypatch.setattr(F, "_LEAVE_S", 0.1)
    with capture_logs() as logs:
        assert F.run_call(settings, tmp_path, _payload(join=J2)) == 0
    assert [e["event"] for e in logs] == ["follow_waiting_for_previous", "follow_already_running"]
    assert in_process == []


@pytest.mark.transcript
def test_attend_runs_the_follower_and_survives_its_failure(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from miniteams import follower as FL

    seen: list[Any] = []

    async def follow(settings: Settings, data: Path, payload: dict[str, Any], stop: threading.Event) -> str:
        seen.append((settings.tenant_id, data, payload["correlationId"], stop.is_set()))
        return "call_ended"

    monkeypatch.setattr(FL, "follow", follow)
    assert F._attend(settings, tmp_path, _payload(), threading.Event()) == "call_ended"
    assert seen == [("tenant-123", tmp_path, CALL, False)]

    async def broken(*_a: Any) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(FL, "follow", broken)
    with capture_logs() as logs:
        assert F._attend(settings, tmp_path, _payload(), threading.Event()) == "error"
    assert [(e["event"], e["error"]) for e in logs] == [("follow_failed", "boom")]

    async def stray_cancel(*_a: Any) -> str:
        raise asyncio.CancelledError  # e.g. a cancelled inner future nobody asked to cancel

    monkeypatch.setattr(FL, "follow", stray_cancel)
    with capture_logs() as logs:
        assert F._attend(settings, tmp_path, _payload(), threading.Event()) == "error"
    assert [e["event"] for e in logs] == ["follow_failed"]


@pytest.mark.transcript
def test_a_follower_that_cannot_join_retries_writes_why_and_exits(
    settings: Settings, tmp_path: Path, real_launches: list[Any]
) -> None:
    """044-AC14: the real `follow --call`, signed out: two tries, the cause in the transcript header, the
    lock freed once it wound down, and the join marked ended, so a redelivered 115 joins nothing more."""
    data = tmp_path / "data"
    F.handle(settings, data, _push(115))
    log = data / "follow" / f"{CALL}.log"
    _until(lambda: real_launches[0].returncode is not None, "exited on its own", timeout=30)
    assert real_launches[0].returncode == 0 and not F.lock_held(F.lock_path(settings, CALL))
    attempts = [json.loads(x)["attempt"] for x in log.read_text().splitlines() if "follow_join_failed" in x]
    assert attempts == [1, 2]
    (path,) = (data / "follow").rglob("*.ndjson")
    header = json.loads(path.read_text().splitlines()[0])
    assert header["join"] == "failed" and header["error"].startswith("sign-in: ")
    assert F.ended_joins(settings, CALL) == {JOIN}
    with capture_logs() as logs:
        F.handle(settings, data, _push(115))
    assert [(e["event"], e.get("reason")) for e in logs] == [("follow_skipped", "ended")]


@pytest.mark.transcript
def test_cli_capture_and_log_level_reach_the_followers_on_their_argv(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """044-AC12: --capture is opt-in; the detached followers get it, and the trigger's log level, on argv."""
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)
    monkeypatch.delenv("MINITEAMS_FOLLOW_CAPTURE", raising=False)
    monkeypatch.chdir(tmp_path)  # the relative `cap` resolves here, never in the repository
    got: list[Settings] = []
    monkeypatch.setattr(F, "run_call", lambda s, _d, _push: got.append(s) or 0)
    push = tmp_path / "push.json"
    assert cli.main(["follow", "--data-dir", str(tmp_path), "--call-file", str(push)]) == 0
    assert got[0].follow_capture is None and "--capture" not in F.follower_command(got[0], tmp_path, push)
    assert cli.main(["--log-level", "DEBUG", "follow", "--capture", "cap", "--call-file", str(push)]) == 0
    assert got[1].follow_capture == tmp_path / "cap"
    cmd = F.follower_command(got[1], tmp_path, push)
    assert cli.main(cmd[cmd.index("--log-level") :]) == 0  # the follower's own CLI parses it
    assert (got[2].follow_capture, got[2].log_level) == (tmp_path / "cap", "DEBUG")
    assert "MINITEAMS_FOLLOW_CAPTURE" not in os.environ


def test_a_signalled_stop_logs_the_follower_outcome(
    settings: Settings, tmp_path: Path, in_process: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop line says what the follower thread made of the signal (it hung up: "stopped")."""
    monkeypatch.setattr(F, "_attend", lambda _s, _d, _p, stop: "stopped" if stop.wait(5) else "late")
    with capture_logs() as logs:
        assert F.run_call(settings, tmp_path, _payload()) == 0
    (stop,) = [e for e in logs if e["event"] == "follow_stop"]
    assert (stop["reason"], stop["signal"], stop["outcome"]) == ("signal", "SIGTERM", "stopped")
    assert not F.ended_path(settings, CALL).exists()  # the 116 that sent it wrote the marker


# --- swarm-review step 2: host-local state, argv, scopes, the switch on running followers ---


@pytest.mark.transcript
def test_two_triggers_with_different_data_dirs_start_one_follower(
    settings: Settings, tmp_path: Path, real_launches: list[Any]
) -> None:
    """044-AC3: the lock lives in the config dir: an archiver and a `follow` naming the data dir apart
    (a symlink, an NFS path) share it, so each sees the other's follower as running."""
    one, two = tmp_path / "one", tmp_path / "two"
    F.handle(settings, one, _push(115))
    log_one = one / "follow" / f"{CALL}.log"
    _until(lambda: log_one.exists() and "follow_start" in log_one.read_text(), "first follower up")
    with capture_logs() as logs:
        F.handle(settings, two, _push(115))
    assert [e["event"] for e in logs] == ["follow_already_running"] and len(real_launches) == 1
    F.launch(settings, two, CALL, _payload())  # two triggers racing past the check (recorded)
    _until(lambda: real_launches[1].returncode is not None, "second follower exited")
    assert "follow_already_running" in (two / "follow" / f"{CALL}.log").read_text()
    assert not (one / "follow" / f"{CALL}.lock").exists() and F.lock_held(F.lock_path(settings, CALL))
    F.handle(settings, two, _push(116))  # the other trigger's 116 stops it
    _until(lambda: real_launches[0].returncode is not None, "first follower stopped")


@pytest.mark.transcript
def test_the_push_stays_off_argv_and_a_big_one_still_launches(
    settings: Settings, tmp_path: Path, real_launches: list[Any]
) -> None:
    """Local users read /proc/<pid>/cmdline, and one argument past 128 KiB fails exec (E2BIG): the push
    goes in a 0600 file; the trigger's 116 still finds and stops the follower through it."""
    push = _payload()
    push["additionalData"] = {"participantList": [{"id": f"8:orgid:{n:036d}"} for n in range(4000)]}
    assert len(json.dumps(push)) > 200 * 1024
    F.handle(settings, tmp_path, {"body": json.dumps({"evt": 115, "cp": _cp(push)})})
    _until(lambda: F.lock_held(F.lock_path(settings, CALL)), "follower up")
    proc = real_launches[0]
    argv = Path(f"/proc/{proc.pid}/cmdline").read_bytes()
    assert b"participantList" not in argv and b"orgid" not in argv and b"fakemeeting" not in argv
    path = F.push_path(settings, push)
    assert json.loads(path.read_text()) == push and f"--call-file\0{path}\0".encode() in argv
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    F.handle(settings, tmp_path, {"body": json.dumps({"evt": 116, "cp": _cp(push)})})
    assert proc.wait(timeout=10) == 0
    assert not path.exists()  # the follower took its push with it


def _cp(payload: dict[str, Any]) -> str:
    return base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()


def test_a_rejoin_gets_its_own_push_file(settings: Settings) -> None:
    """The winding-down follower of J1 keeps naming its own push while J2's follower starts."""
    assert F.push_path(settings, _payload()) != F.push_path(settings, _payload(join=J2))
    assert F.push_path(settings, _payload()).parent == F.state_dir(settings)


_FAKE_SYSTEMD_RUN = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$@" > "$SCOPE_LOG"
while [[ "$1" != "--" ]]; do shift; done
shift
exec "$@"
"""


@pytest.mark.transcript
def test_a_follower_runs_in_its_own_scope_when_systemd_run_exists(
    settings: Settings, tmp_path: Path, real_launches: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """systemd-oomd kills whole cgroups: each follower gets a transient scope of its own. systemd-run
    execs the follower, so the pid the trigger reaps, the lock's pid line and the /proc check agree."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "systemd-run"
    fake.write_text(_FAKE_SYSTEMD_RUN)
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("SCOPE_LOG", str(tmp_path / "scope.args"))
    data = tmp_path / "data"
    F.handle(settings, data, _push(115))
    lock = F.lock_path(settings, CALL)
    _until(lambda: _follower_pid(lock) is not None, "follower up")
    proc = real_launches[0]
    args = (tmp_path / "scope.args").read_text().splitlines()
    assert args[: args.index("--")] == ["--user", "--scope", "--collect", "--quiet"]
    assert args[args.index("--") + 1] == sys.executable and _follower_pid(lock) == proc.pid
    F.handle(settings, data, _push(116))
    assert proc.wait(timeout=10) == 0


def test_without_systemd_run_or_a_user_manager_the_follower_is_a_plain_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = [sys.executable, "-m", "miniteams", "follow", "--data-dir", "/srv/data"]
    monkeypatch.setenv("PATH", str(tmp_path))  # no systemd-run there
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert F._scope(cmd) == []
    fake = tmp_path / "systemd-run"
    fake.write_text(_FAKE_SYSTEMD_RUN)
    fake.chmod(0o755)
    assert F._scope(cmd)[1:] == ["--user", "--scope", "--collect", "--quiet", "--"]
    # systemd-run would expand these in the follower's own arguments: such a follower runs as a plain child.
    for odd in ("/srv/$HOME/data", "/srv/${USER}", "/srv/$$"):
        assert F._scope([*cmd[:-1], odd]) == []
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert F._scope(cmd) == []


def test_a_follower_exiting_in_error_is_logged_by_its_host(
    settings: Settings, tmp_path: Path, real_launches: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A systemd-run that cannot reach the user manager starts no follower: the trigger's log says so."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "systemd-run"
    fake.write_text("#!/usr/bin/env bash\necho 'Failed to connect to bus' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    with capture_logs() as logs:
        F.handle(settings, tmp_path / "data", _push(115))
        _until(lambda: any(e["event"] == "follow_exited" for e in logs), "exit logged")
    (exited,) = [e for e in logs if e["event"] == "follow_exited"]
    assert (exited["call"], exited["pid"], exited["rc"], exited["log_level"]) == (
        CALL,
        real_launches[0].pid,
        1,
        "warning",
    )
    assert "Failed to connect to bus" in (tmp_path / "data" / "follow" / f"{CALL}.log").read_text()


@pytest.mark.transcript
def test_a_follower_exiting_cleanly_logs_nothing_more(
    settings: Settings, tmp_path: Path, procs: list[Any]
) -> None:
    fd = F.take_lock(F.lock_path(settings, CALL))  # the follower loses the race and exits 0
    try:
        with capture_logs() as logs:
            procs.append(F.launch(settings, tmp_path, CALL, _payload()))
            _until(lambda: procs[0].returncode is not None, "reaped")
    finally:
        assert fd is not None
        os.close(fd)
    assert procs[0].returncode == 0 and "follow_exited" not in [e["event"] for e in logs]


@pytest.mark.parametrize(("join", "kept"), [(JOIN, False), ("", True)])
def test_the_lock_holder_removes_its_push_file(
    settings: Settings, tmp_path: Path, in_process: list[str], join: str, kept: bool
) -> None:
    """The push holds participant data: gone with its follower; not with an empty join time, whose
    file name a rejoin's push would share; never by a follower that lost the lock."""
    payload = _payload(join=join)
    path = Path(_argv(settings, payload)[-1])
    fd = F.take_lock(F.lock_path(settings, CALL))
    assert fd is not None
    try:
        assert F.run_call(settings, tmp_path, payload) == 0  # lost the lock
    finally:
        os.close(fd)
    assert path.exists()
    assert F.run_call(settings, tmp_path, payload) == 0  # held it, stopped
    assert path.exists() is kept


def test_disable_and_skip_stop_the_running_followers(
    procs: list[Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The switch also reaches followers already in a call: --skip THREAD stops those of its calls,
    --disable every one; a lock holder that is not our follower is never signalled."""
    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)
    settings = Settings()  # what cli.main builds, from the same environment
    group = "19:fakegroup@thread.v2"
    meeting = _holder(F.lock_path(settings, CALL), *_argv(settings, _payload()))
    other = _holder(F.lock_path(settings, OTHER), *_argv(settings, _payload(call=OTHER, thread=group)))
    third = "00000000-0000-4000-8000-000000000003"
    stranger = _holder(F.lock_path(settings, third), "not-a-follower")
    procs += [meeting, other, stranger]
    with capture_logs() as logs:
        assert cli.main(["follow", "--skip", MEETING]) == 0
    assert meeting.wait(timeout=10) == -signal.SIGTERM
    time.sleep(0.2)  # a SIGTERM sent would have landed by now
    assert other.poll() is None and stranger.poll() is None
    assert [(e["call"], e["reason"]) for e in logs if e["event"] == "follow_stop_sent"] == [
        (CALL, "skip_list")
    ]
    assert cli.main(["follow", "--disable"]) == 0
    assert other.wait(timeout=10) == -signal.SIGTERM
    time.sleep(0.2)
    assert stranger.poll() is None
    assert cli.main(["follow", "--status"]) == 0 and cli.main(["follow", "--enable"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"enabled": True, "skip": [MEETING]}


def test_stop_running_skips_what_it_cannot_check(settings: Settings, procs: list[Any]) -> None:
    """No follow state yet, a holder between its lock and its pid line, a stray file: nothing signalled."""
    assert F.stop_running(settings) == 0
    fd = F.take_lock(F.lock_path(settings, CALL))  # locked, no pid line yet
    assert fd is not None
    try:
        (F.state_dir(settings) / "notes.lock").write_text("1\n")
        F.lock_path(settings, OTHER).write_text("1\n")  # a lock nobody holds
        assert F.stop_running(settings) == 0
    finally:
        os.close(fd)
