"""Meeting follower trigger (spec 044, phase 2): call pushes start and stop one follower per call.

`evt 115` (I joined a call, any device) spawns `miniteams follow --call <payload>` as a detached
process; `evt 116` (the call ended for me) signals it. The follower holds `<data-dir>/follow/<call>.lock`
(flock) for its whole life: the kernel drops it on any exit, a crash included.

`evt 116` also appends the ended join's `userJoinTime` to `<call>.ended`: a follower of that join not
yet holding its lock, or launched by another trigger, reads it once locked and stops; a redelivered
115 of that join is skipped. A rejoin of the same call has another join time: neither is touched by it.
"""

import contextlib
import fcntl
import importlib.util
import json
import os
import queue
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import structlog

from .config import Settings, write_private
from .messages import _gunzip_b64
from .stream import CallHook

log = structlog.get_logger()

EVT_JOINED = 115
EVT_ENDED = 116
TRANSCRIPT_MODULES = ("numpy", "onnxruntime", "faster_whisper")
EXTRA_HINT = "uv sync --inexact --extra transcript, then uv run --extra transcript miniteams follow"
_LOCK_TRIES, _LOCK_PAUSE = 10, 0.02  # ~200 ms before a follower concedes the lock
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def missing_extra() -> list[str]:
    """Transcript modules not installed. find_spec, not import: the archiver hosting the trigger
    must not carry onnxruntime in memory."""
    return [m for m in TRANSCRIPT_MODULES if importlib.util.find_spec(m) is None]


def decode_push(req: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
    """`{"evt": int, "cp": base64 gzip JSON}` → (evt, payload); None when it is not that shape."""
    try:
        outer = json.loads(req.get("body") or "")
        evt, cp = outer["evt"], outer["cp"]
        payload = json.loads(_gunzip_b64(cp))
    except ValueError, KeyError, TypeError, OSError, EOFError:
        return None
    if not isinstance(evt, int) or not isinstance(payload, dict):
        return None
    return evt, payload


def call_id(payload: dict[str, Any]) -> str | None:
    """The correlationId, only when it is a UUID: it names a file."""
    value = payload.get("correlationId")
    return value if isinstance(value, str) and _UUID.fullmatch(value) else None


def thread_of(payload: dict[str, Any]) -> str:
    group = payload.get("groupContext")
    thread = group.get("threadId") if isinstance(group, dict) else None
    return thread if isinstance(thread, str) else ""


def call_kind(thread: str) -> str:
    if thread.startswith("19:meeting_"):
        return "meeting"
    if thread.endswith("@unq.gbl.spaces"):
        return "1:1"
    return "group"


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
    enabled = data.get("enabled") if isinstance(data, dict) else None
    skip = data.get("skip", []) if isinstance(data, dict) else None
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


# --- per-call lock ---


def _follow_dir(data_dir: Path) -> Path:
    path = data_dir / "follow"
    path.mkdir(parents=True, exist_ok=True)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:  # mkdir's mode leaves an existing dir as it was
        try:
            os.chmod(path, 0o700)
        except OSError as exc:  # another owner, a squashing or FUSE mount: privacy lost, not the feature
            log.warning("follow_dir_not_private", path=str(path), error=str(exc))
    return path


def lock_path(data_dir: Path, call: str) -> Path:
    return data_dir / "follow" / f"{call}.lock"


def take_lock(path: Path) -> int | None:
    """An fd holding the exclusive flock on `path`, or None when another process holds it."""
    _follow_dir(path.parent.parent)
    # O_RDWR: on NFS flock becomes a POSIX lock, whose exclusive form needs a writable fd.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    for attempt in range(_LOCK_TRIES):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if attempt < _LOCK_TRIES - 1:
                time.sleep(_LOCK_PAUSE)  # a trigger's lock_held probe holds it for an instant
    os.close(fd)
    return None


def ended_path(data_dir: Path, call: str) -> Path:
    return data_dir / "follow" / f"{call}.ended"


def join_time(payload: dict[str, Any]) -> str:
    value = payload.get("userJoinTime")
    # One line of `<call>.ended` each: a newline would forge another join's entry.
    return value.strip() if isinstance(value, str) and value.isprintable() else ""


def ended_joins(data_dir: Path, call: str) -> set[str]:
    try:
        return set(ended_path(data_dir, call).read_text(encoding="utf-8", errors="replace").splitlines())
    except FileNotFoundError:
        return set()


def _mark_ended(data_dir: Path, call: str, join: str) -> None:
    if not join:
        return  # nothing to match a follower or a redelivery against
    path = _follow_dir(data_dir) / f"{call}.ended"
    # O_APPEND: a concurrent 116 of another join adds its line, never replaces this one.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, f"{join}\n".encode())
    finally:
        os.close(fd)


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


def follower_command(data_dir: Path, payload: dict[str, Any]) -> list[str]:
    return [
        sys.executable,
        "-P",  # no cwd on sys.path: the data dir's parent is not code
        "-m",
        "miniteams",
        "follow",
        "--data-dir",
        str(data_dir),
        "--call",
        json.dumps(payload),
    ]


def launch(data_dir: Path, call: str, payload: dict[str, Any]) -> subprocess.Popen[bytes]:
    """Own session: a restart (or Ctrl-C) of the trigger's host leaves the follower running."""
    log_file = _follow_dir(data_dir) / f"{call}.log"
    with os.fdopen(os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab") as out:
        proc = subprocess.Popen(
            follower_command(data_dir, payload),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    threading.Thread(target=proc.wait, daemon=True).start()  # reaps it: no zombie while the host lives
    log.info("follow_launched", call=call, pid=proc.pid)
    return proc


def _follower_join(pid: int, call: str) -> str | None:
    """The join time `pid` follows when it is our follower of `call`, else None (pid reused, other call)."""
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
        i = args.index("--call")
        payload = json.loads(args[i + 1])
    except OSError, UnicodeDecodeError, ValueError, IndexError:
        return None
    if "follow" not in args[:i] or not isinstance(payload, dict) or payload.get("correlationId") != call:
        return None
    return join_time(payload)


def stop(data_dir: Path, call: str, payload: dict[str, Any]) -> None:
    join = join_time(payload)
    # First, whatever runs: a follower between its launch and its pid line reads it once locked.
    try:
        _mark_ended(data_dir, call, join)
    except OSError as exc:  # still signal a running follower
        log.warning("follow_ended_marker_failed", call=call, error=str(exc))
    path = lock_path(data_dir, call)
    if not lock_held(path):
        log.info("follow_not_running", call=call)
        return
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError) as exc:
        log.warning("follow_lock_unreadable", call=call, error=str(exc))
        return
    running = _follower_join(pid, call)
    if running is None:
        log.warning("follow_pid_mismatch", call=call, pid=pid)
        return
    if join and running != join:  # a late 116 of an earlier join: the follower of a rejoin stays
        log.info("follow_stop_other_join", call=call, pid=pid)
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
        log.info("follow_stop_sent", call=call, pid=pid)


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
        stop(data_dir, call, payload)
        return
    thread = thread_of(payload)
    # Unreadable = off: following a call the user may have excluded is worse than missing one.
    enabled, skip = load_switch(settings) or (False, [])
    reason = "disabled" if not enabled else "skip_list" if thread in skip else None
    if reason is not None:
        log.info("follow_skipped", call=call, thread=thread, reason=reason)
        return
    # A self-join or redelivered push: the running follower holds the lock. Two racing triggers both
    # passing this check are settled by the follower, which exits when it cannot take the lock.
    if lock_held(lock_path(data_dir, call)):
        # ponytail: a rejoin's 115 reordered before the 116 of the join still running is dropped here;
        # the fix is the follower owning its own end (Phase 3 leave detection, 116 only as a hint).
        log.info("follow_already_running", call=call)
        return
    join, ended = join_time(payload), ended_joins(data_dir, call)
    if join and join in ended:  # a redelivered 115 of the join a 116 already ended
        log.info("follow_skipped", call=call, thread=thread, reason="ended")
        return
    launch(data_dir, call, payload)


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
                log.warning("call_hook_failed", error=str(exc), error_type=type(exc).__name__, exc_info=True)

    threading.Thread(target=work, name="follow-trigger", daemon=True).start()

    async def on_call(req: dict[str, Any]) -> None:
        pushes.put(req)

    return on_call


# --- follower (stub until phases 3 and 4) ---


def run_call(data_dir: Path, push_json: str) -> int:
    try:
        payload = json.loads(push_json)
    except ValueError:
        payload = None
    call = call_id(payload) if isinstance(payload, dict) else None
    if payload is None or call is None:
        log.error("follow_bad_call")
        return 2
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    fd = take_lock(lock_path(data_dir, call))
    if fd is None:
        log.info("follow_already_running", call=call)
        return 0
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        # After the pid line: a 116 from now on either sees this pid or left the marker read here.
        join = join_time(payload)
        if join and join in ended_joins(data_dir, call):
            log.info("follow_stop", call=call, reason="ended_before_start")
            return 0
        thread = thread_of(payload)
        log.info("follow_start", call=call, thread=thread, kind=call_kind(thread))
        sig = signal.sigwait({signal.SIGTERM, signal.SIGINT})
        log.info("follow_stop", call=call, reason="signal", signal=signal.Signals(sig).name)
    finally:
        # ponytail: .lock, .ended and .log stay, three small files per call (unlinking a flock file races
        # a new holder); a cleanup of old calls when they pile up.
        os.close(fd)
    return 0
