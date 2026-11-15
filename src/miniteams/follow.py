"""Meeting follower (spec 044): call pushes start and stop one follower per call.

`evt 115` (I joined a call, any device) spawns `miniteams follow --call-file <push file>` as a detached
process; `evt 116` (the call ended for me) signals it. The follower holds `<config-dir>/follow/<call>.lock`
(flock) for its whole life: the kernel drops it on any exit, a crash included. Locks, markers and push
files live in the config dir, host-local and per user, so triggers with different data dirs share them.

`<call>.ended` lists the joins of the call that ended (mark_ended names its writers): a follower of such
a join, not yet holding its lock or launched by another trigger, stops once locked; a redelivered 115 of
it is skipped. A rejoin of the same call has another join time: neither is touched by it. A follower
ending on its own exits without waiting for evt 116, which only comes once it is gone; its lock is freed
once its hang-up and transcript close are done, and my rejoin's follower waits up to _LEAVE_S for it.

This module owns the layout of `<config-dir>/follow/` (locks, markers, push files) and of
`<data-dir>/follow/` (logs, transcripts). The follower's
main thread only waits for the stop signal; a thread of its own joins, transcribes and leaves the call
(`miniteams.follower`, the `transcript` extra).
"""

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import queue
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from .config import Settings, write_private
from .messages import delivery_json, unwrap_payload
from .presence import dm_ids, same_chat
from .stream import CallHook

log = structlog.get_logger()

EVT_JOINED = 115
EVT_ENDED = 116
TRANSCRIPT_MODULES = ("numpy", "onnxruntime", "faster_whisper", "transcribe_cpp", "av", "aioice", "pylibsrtp")
BUILD_HINT = "scripts/build-transcribe.sh"
EXTRA_HINT = "uv sync --inexact --extra transcript, then uv run --extra transcript miniteams follow"
_LOCK_TRIES, _LOCK_PAUSE = 10, 0.02  # ~200 ms before a follower concedes the lock
_SIGNALS = {signal.SIGTERM, signal.SIGINT}
_DONE = signal.SIGUSR1  # the follower thread ended on its own: wakes the main thread's sigwait
_LEAVE_S = 40.0  # the hang-up (token + DELETE, 7 s) beside the last lines (stt_close_timeout_s) + margin
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def missing_extra() -> list[str]:
    """Transcript modules not installed. find_spec, not import: the archiver hosting the trigger
    must not carry onnxruntime in memory."""
    return [m for m in TRANSCRIPT_MODULES if importlib.util.find_spec(m) is None]


def _build_command(settings: Settings, engine: str) -> str:
    """The build script, by absolute path in a source checkout. The cache dir is passed explicitly: the
    script reads the environment, not `.env`."""
    script = Path(__file__).resolve().parents[2] / BUILD_HINT
    where = shlex.quote(str(script)) if script.is_file() else f"{BUILD_HINT} (in the miniteams repository)"
    return f"MINITEAMS_CACHE_DIR={shlex.quote(str(settings.cache_dir))} {where} {engine}"


def stt_ready(settings: Settings) -> str | None:
    """None when the chosen engine can run, else what is missing and how to fix it (044-AC10). File
    checks only: nothing is loaded."""
    if missing := missing_extra():
        return f"transcript extra missing ({', '.join(missing)}): {EXTRA_HINT}"
    engine = settings.stt_engine
    build = f"run `{_build_command(settings, engine)}`"
    if engine == "parakeet" and not settings.stt_library.is_file():
        fix = (
            "point MINITEAMS_TRANSCRIBE_LIBRARY at an existing file" if settings.transcribe_library else build
        )
        return f"libtranscribe missing at {settings.stt_library}: {fix}"
    model = settings.stt_model
    if engine == "parakeet":
        ok, own, kind = model.is_file(), settings.parakeet_model, "an existing GGUF file"
    else:
        ok, own, kind = (
            (model / "model.bin").is_file(),
            settings.whisper_model,
            "a downloaded model directory",
        )
    if not ok:
        fix = f"point MINITEAMS_{engine.upper()}_MODEL at {kind}" if own else build
        return f"{engine} model missing at {model}: {fix}"
    return None


def as_dict(value: Any) -> dict[str, Any]:
    """`value` when a dict, else empty: a JSON field of a shape we cannot rely on."""
    return value if isinstance(value, dict) else {}


def decode_push(req: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
    """`{"evt": int, "cp" | "gp": payload}`, in any delivery encoding → (evt, payload); None when it is not
    that shape."""
    try:
        outer = delivery_json(req)
        evt = outer["evt"]
        payload = unwrap_payload(outer) if "cp" in outer or "gp" in outer else None
    except ValueError, KeyError, TypeError, OSError, EOFError:
        return None
    if not isinstance(evt, int) or not isinstance(payload, dict):
        return None
    return evt, payload


def is_call_id(value: Any) -> bool:
    """A UUID: a call id names files."""
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def call_id(payload: dict[str, Any]) -> str | None:
    """The correlationId, only when it is a call id."""
    value = payload.get("correlationId")
    return value if is_call_id(value) else None


def is_one_to_one(payload: dict[str, Any]) -> bool:
    """A 1:1 push names neither groupContext nor meetingInfo (seen live)."""
    return not payload.get("groupContext") and not payload.get("meetingInfo")


def call_thread(payload: dict[str, Any]) -> str:
    """The call's chat. A 1:1 push names none: its two participants give the 1:1 chat's id, sorted (Teams'
    usual spelling, not always its own: compare 1:1 ids with same_chat)."""
    thread = as_dict(payload.get("groupContext")).get("threadId")
    if isinstance(thread, str) and thread:
        return thread
    if not is_one_to_one(payload):
        return ""
    people = as_dict(payload.get("additionalData")).get("participantList")
    mris = [str(p.get("id")) for p in people if isinstance(p, dict)] if isinstance(people, list) else []
    ids = dm_ids(*mris) if len(mris) == 2 else ()
    return min(ids) if ids else ""


def call_kind(payload: dict[str, Any]) -> str:
    """An ad hoc group call's push names its chat's thread and messageId "0" (seen live); a channel
    meeting names its channel thread and the meeting's post."""
    if is_one_to_one(payload):
        return "1:1"
    message = as_dict(payload.get("groupContext")).get("messageId")
    meeting = call_thread(payload).startswith("19:meeting_") or str(message or "0") != "0"
    return "meeting" if meeting else "group"


# --- switch and skip list ---


def _switch_path(settings: Settings) -> Path:
    return settings.config_dir / "follow.json"


def load_switch(settings: Settings) -> tuple[bool, list[str]] | None:
    """(enabled, skip); missing file = on, nothing skipped; None (logged) when unreadable."""
    path = _switch_path(settings)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True, []
    except (OSError, ValueError) as exc:
        log.error("follow_config_unreadable", path=str(path), error=str(exc))
        return None
    config = as_dict(data)
    enabled, skip = config.get("enabled"), config.get("skip", [])
    if (
        not isinstance(enabled, bool)
        or not isinstance(skip, list)
        or not all(isinstance(t, str) for t in skip)
    ):
        log.error("follow_config_unreadable", path=str(path), error="expected {enabled: bool, skip: [str]}")
        return None
    return enabled, skip


def save_switch(settings: Settings, enabled: bool, skip: list[str]) -> None:
    write_private(_switch_path(settings), json.dumps({"enabled": enabled, "skip": skip}, indent=2) + "\n")


# --- the follow dirs: per-call lock, marker, push file (config dir); log, transcripts (data dir) ---


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:  # mkdir's mode leaves an existing dir as it was
        try:
            os.chmod(path, 0o700)
        except OSError as exc:  # another owner, a squashing or FUSE mount: privacy lost, not the feature
            log.warning("follow_dir_not_private", path=str(path), error=str(exc))
    return path


def follow_dir(data_dir: Path) -> Path:
    return _private_dir(data_dir / "follow")


def state_dir(settings: Settings) -> Path:
    """Host-local, per user: a lock in a data dir shared by two triggers (or hosts) is one only if both
    name that dir the same way."""
    return settings.config_dir / "follow"


def lock_path(settings: Settings, call: str) -> Path:
    return state_dir(settings) / f"{call}.lock"


def take_lock(path: Path, tries: int = _LOCK_TRIES) -> int | None:
    """An fd holding the exclusive flock on `path`, or None when another process holds it."""
    _private_dir(path.parent)
    # O_RDWR: on NFS flock becomes a POSIX lock, whose exclusive form needs a writable fd.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    for attempt in range(tries):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if attempt < tries - 1:
                time.sleep(_LOCK_PAUSE)  # a trigger's lock_held probe holds it for an instant
    os.close(fd)
    return None


def ended_path(settings: Settings, call: str) -> Path:
    return state_dir(settings) / f"{call}.ended"


def join_time(payload: dict[str, Any]) -> str:
    value = payload.get("userJoinTime")
    # One line of `<call>.ended` each: a newline would forge another join's entry.
    return value.strip() if isinstance(value, str) and value.isprintable() else ""


def ended_joins(settings: Settings, call: str) -> set[str]:
    try:
        return set(ended_path(settings, call).read_text(encoding="utf-8", errors="replace").splitlines())
    except FileNotFoundError:
        return set()


def mark_ended(settings: Settings, call: str, join: str) -> None:
    """Records `join` of `call` as ended, once: a join already listed is not added again. Writers: the
    trigger on evt 116, first, whatever runs; a follower ending on its own (not stopped), before its
    hang-up when it was in the call (that hang-up's 116 may be followed at once by my rejoin's 115, which
    must find this join ended), and run_call once it returned, whatever its outcome."""
    if not join:
        return  # nothing to match a follower or a redelivery against
    path = _private_dir(state_dir(settings)) / f"{call}.ended"
    if join in ended_joins(settings, call):
        return
    # O_APPEND: a concurrent 116 of another join adds its line, never replaces this one.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, f"{join}\n".encode())
    finally:
        os.close(fd)


def transcript_path(data_dir: Path, call: str, started: float) -> Path:
    """`<data-dir>/follow/<YYYY-MM>/<started>-<call>.ndjson`: names sort by start."""
    day = datetime.fromtimestamp(started, UTC)
    return data_dir / "follow" / f"{day:%Y-%m}" / f"{day:%Y%m%dT%H%M%SZ}-{call}.ndjson"


def transcript_files(data_dir: Path, call: str = "*") -> list[tuple[str, Path]]:
    """(call, transcript) of followed calls, of `call` or every one, oldest first. A rejoin of a call
    writes another file under the same call id."""
    paths = sorted((data_dir / "follow").glob(f"*/*-{call}.ndjson"), key=lambda p: p.name)
    named = [(p.stem.split("-", 1)[1], p) for p in paths]
    return [(c, p) for c, p in named if is_call_id(c)]


def lock_held(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        # Shared: two probes never see each other as a holder; a follower's take_lock retries past one.
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)  # closing drops a probe lock taken above
    return False


# --- trigger ---


def push_path(settings: Settings, payload: dict[str, Any]) -> Path:
    """One file per join: a rejoin's push never replaces the one its winding-down predecessor names."""
    digest = hashlib.sha256(join_time(payload).encode()).hexdigest()[:16]
    return state_dir(settings) / f"{call_id(payload)}-{digest}.push.json"


def follower_command(settings: Settings, data_dir: Path, push: Path) -> list[str]:
    """The trigger's own log level and capture dir go along. The push stays off argv (readable by any
    local user in /proc, and bounded by the kernel's per-argument limit)."""
    capture = ["--capture", str(settings.follow_capture)] if settings.follow_capture else []
    return [
        sys.executable,
        "-P",  # no cwd on sys.path: the data dir's parent is not code
        "-m",
        "miniteams",
        "--log-level",
        settings.log_level,
        "follow",
        "--data-dir",
        str(data_dir),
        *capture,
        "--call-file",
        str(push),
    ]


def _scope(cmd: list[str]) -> list[str]:
    """A transient scope of its own: systemd-oomd kills whole cgroups, the archiver's would take every
    follower with it. systemd-run execs the command, so the pid stays the follower's."""
    run = shutil.which("systemd-run")
    if run is None or not os.environ.get("XDG_RUNTIME_DIR"):  # no user manager to ask
        return []
    if any("$" in arg for arg in cmd):  # systemd-run expands ${VAR} and $$ in the command it runs
        return []
    return [run, "--user", "--scope", "--collect", "--quiet", "--"]


def _reap(proc: subprocess.Popen[bytes], call: str) -> None:
    """No zombie while the host lives; a follower (or systemd-run) failing shows in the trigger's log."""
    if (rc := proc.wait()) != 0:
        log.warning("follow_exited", call=call, pid=proc.pid, rc=rc)


def launch(settings: Settings, data_dir: Path, call: str, payload: dict[str, Any]) -> subprocess.Popen[bytes]:
    """Own session: a restart (or Ctrl-C) of the trigger's host leaves the follower running."""
    push = push_path(settings, payload)
    _private_dir(push.parent)
    write_private(push, json.dumps(payload))
    log_file = follow_dir(data_dir) / f"{call}.log"
    # The follower's config dir is the trigger's, set or not by env: one lock dir for both.
    env = {**os.environ, "MINITEAMS_CONFIG_DIR": str(settings.config_dir)}
    cmd = follower_command(settings, data_dir, push)
    with os.fdopen(os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab") as out:
        proc = subprocess.Popen(
            [*_scope(cmd), *cmd],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    threading.Thread(target=_reap, args=(proc, call), daemon=True).start()
    log.info("follow_launched", call=call, pid=proc.pid)
    return proc


def read_push(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _push_file(pid: int) -> Path | None:
    """The push file `pid` names when it is a follower, else None."""
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
        i = args.index("--call-file")
        path = args[i + 1]
    except OSError, UnicodeDecodeError, ValueError, IndexError:
        return None
    return Path(path) if "follow" in args[:i] else None


def _follower_push(pid: int, call: str) -> dict[str, Any] | None:
    """The push `pid` follows when it is our follower of `call`, else None (pid reused, other call)."""
    path = _push_file(pid)
    payload = read_push(path) if path is not None else None
    return payload if payload is not None and payload.get("correlationId") == call else None


def _follower_join(pid: int, call: str) -> str | None:
    """The join time `pid` follows when it is our follower of `call`, else None."""
    payload = _follower_push(pid, call)
    return None if payload is None else join_time(payload)


def _holder(path: Path) -> int:
    """The pid line of a lock file; OSError or ValueError when unreadable."""
    return int(path.read_text().strip())


def _winding_down(settings: Settings, call: str, join: str) -> bool:
    """The lock's holder follows another join of `call`, one already ended: it is leaving, not ours."""
    try:
        pid = _holder(lock_path(settings, call))
    except OSError, ValueError:
        return False
    running = _follower_join(pid, call)
    return bool(join) and running is not None and running != join and running in ended_joins(settings, call)


def _signal(call: str, pid: int, **why: Any) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
        log.info("follow_stop_sent", call=call, pid=pid, **why)


def stop_follower(settings: Settings, call: str, payload: dict[str, Any]) -> None:
    join = join_time(payload)
    # First, whatever runs: a follower between its launch and its pid line reads it once locked.
    try:
        mark_ended(settings, call, join)
    except OSError as exc:  # still signal a running follower
        log.warning("follow_ended_marker_failed", call=call, error=str(exc))
    path = lock_path(settings, call)
    if not lock_held(path):
        log.info("follow_not_running", call=call)
        return
    try:
        pid = _holder(path)
    except (OSError, ValueError) as exc:
        log.warning("follow_lock_unreadable", call=call, error=str(exc))
        return
    running = _follower_join(pid, call)
    if running is None:
        push = _push_file(pid)
        # Its push deleted, its lock not yet released: a follower of this call in its last instant.
        if push is not None and push.name.startswith(f"{call}-") and not push.exists():
            log.info("follow_winding_down", call=call, pid=pid)
        else:
            log.warning("follow_pid_mismatch", call=call, pid=pid)
        return
    if join and running != join:  # a late 116 of an earlier join: the follower of a rejoin stays
        log.info("follow_stop_other_join", call=call, pid=pid)
        return
    _signal(call, pid)


def stop_running(settings: Settings, thread: str | None = None) -> int:
    """SIGTERMs the running followers (`follow --disable`), only those of `thread`'s calls when given
    (`--skip`); how many were signalled. Each is checked as stop_follower does: never another process."""
    stopped = 0
    for path in sorted(state_dir(settings).glob("*.lock")):  # no dir yet: nothing
        call = path.stem
        if not is_call_id(call) or not lock_held(path):
            continue
        try:
            pid = _holder(path)
        except OSError, ValueError:
            continue  # between its lock and its pid line: the switch stops its next call
        payload = _follower_push(pid, call)
        if payload is None or (thread is not None and not same_chat(call_thread(payload), thread)):
            continue
        _signal(call, pid, reason="skip_list" if thread is not None else "disabled")
        stopped += 1
    return stopped


def handle(settings: Settings, data_dir: Path, req: dict[str, Any]) -> None:
    push = decode_push(req)
    if push is None:
        log.warning("call_push_unreadable")
        return
    evt, payload = push
    if evt not in (EVT_JOINED, EVT_ENDED):
        log.debug("call_push_ignored", evt=evt)
        return
    call = call_id(payload)
    if call is None:
        log.warning("call_push_bad_id", evt=evt)
        return
    if evt == EVT_ENDED:
        stop_follower(settings, call, payload)
        return
    thread = call_thread(payload)
    # Unreadable = off: following a call the user may have excluded is worse than missing one.
    enabled, skip = load_switch(settings) or (False, [])
    skipped = any(same_chat(thread, s) for s in skip)
    reason = "disabled" if not enabled else "skip_list" if skipped else None
    if reason is not None:
        log.info("follow_skipped", call=call, thread=thread, reason=reason)
        return
    # A self-join or redelivered push: the running follower holds the lock. Two racing triggers both
    # passing this check are settled by the follower, which exits when it cannot take the lock.
    join = join_time(payload)
    if lock_held(lock_path(settings, call)) and not _winding_down(settings, call, join):
        log.info("follow_already_running", call=call)
        return
    ended = ended_joins(settings, call)
    if join and join in ended:  # a redelivered 115 of the join a 116 already ended
        log.info("follow_skipped", call=call, thread=thread, reason="ended")
        return
    launch(settings, data_dir, call, payload)


def trigger(settings: Settings, data_dir: Path) -> CallHook:
    data_dir = data_dir.resolve()  # the follower runs with its own argv: no relative path
    # One daemon thread, fed in arrival order: NFS writes and forks never stall the socket's read loop,
    # a 116 is never handled before its 115, and a hung handle() never holds the process at exit.
    pushes: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()

    def work() -> None:
        while True:
            req = pushes.get()
            try:
                handle(settings, data_dir, req)
            except Exception as exc:  # noqa: BLE001 — a bad push must not stop the trigger
                log.warning(
                    "follow_trigger_failed", error=str(exc), error_type=type(exc).__name__, exc_info=True
                )

    threading.Thread(target=work, name="follow-trigger", daemon=True).start()

    async def on_call(req: dict[str, Any]) -> None:
        pushes.put(req)

    return on_call


# --- follower ---


def _attend(settings: Settings, data_dir: Path, payload: dict[str, Any], stop: threading.Event) -> str:
    """Joins the call and transcribes it until `stop` is set or the call ends; the outcome."""
    import asyncio

    from .follower import follow  # the transcript extra: never loaded by the trigger's hosts

    try:
        return asyncio.run(follow(settings, data_dir, payload, stop))
    # A stray CancelledError too: else the thread dies with no outcome and the process lingers to 116.
    except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 — the lock and the stop line outlive any failure
        log.error("follow_failed", error=str(exc), error_type=type(exc).__name__, exc_info=True)
        return "error"


def run_call(settings: Settings, data_dir: Path, payload: dict[str, Any] | None) -> int:
    call = call_id(payload) if payload is not None else None
    if payload is None or call is None:
        log.error("follow_bad_call")
        return 2
    # Blocked before any thread starts, so every thread inherits it and sigwait alone takes them.
    signal.pthread_sigmask(signal.SIG_BLOCK, {*_SIGNALS, _DONE})
    path, join = lock_path(settings, call), join_time(payload)
    fd = take_lock(path)
    if fd is None and _winding_down(settings, call, join):
        log.info("follow_waiting_for_previous", call=call)
        fd = take_lock(path, tries=int(_LEAVE_S / _LOCK_PAUSE))
    if fd is None:
        log.info("follow_already_running", call=call)
        return 0
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        # After the pid line: a 116 from now on either sees this pid or left the marker read here.
        if join and join in ended_joins(settings, call):
            log.info("follow_stop", call=call, reason="ended_before_start")
            return 0
        log.info("follow_start", call=call, thread=call_thread(payload), kind=call_kind(payload))
        stop_event, main = threading.Event(), threading.get_ident()
        outcome: list[str] = []

        def attend() -> None:
            outcome.append(_attend(settings, data_dir, payload, stop_event))
            if not stop_event.is_set():
                try:  # see mark_ended
                    mark_ended(settings, call, join)
                except OSError as exc:
                    log.warning("follow_ended_marker_failed", call=call, error=str(exc))
                signal.pthread_kill(main, _DONE)

        # Daemon: a follower hung past _LEAVE_S must not hold the process (and the lock) at exit.
        worker = threading.Thread(target=attend, name="follower", daemon=True)
        worker.start()
        sig = signal.sigwait({*_SIGNALS, _DONE})
        stop_event.set()
        worker.join(_LEAVE_S)
        if sig == _DONE:
            log.info("follow_stop", call=call, reason=outcome[0])
        else:
            name = signal.Signals(sig).name
            log.info(
                "follow_stop",
                call=call,
                reason="signal",
                signal=name,
                outcome=outcome[0] if outcome else None,
            )
    finally:
        # The holder's push goes with it. Not with an empty join time: a rejoin's push would share its name.
        if join:
            with contextlib.suppress(OSError):
                push_path(settings, payload).unlink(missing_ok=True)
        # ponytail: .lock, .ended and .log stay, small files per call (unlinking a flock file races a new
        # holder); a cleanup of old calls when they pile up.
        os.close(fd)
    return 0
