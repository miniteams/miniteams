# Copied from grill-room `stt/grill_stt/engines.py` @ 89744d1 (spec 044, AC16), modified: ProcessEngine
# takes an env; stop() is final, safe from another thread, and lets an idle child exit on EOF; the child
# runs as `python -P -m miniteams.stt.engines` with miniteams logging; child_env() sets HF offline instead
# of filtering grill's secrets.
# Copyright grill-room authors. Licensed under the Apache License, Version 2.0:
# http://www.apache.org/licenses/LICENSE-2.0
"""Engine processes: each loads one model and transcribes one utterance at a time (spec 007, AC6).

A process, not a thread: a native crash or a hang takes down one engine, which is restarted, and
the other keeps transcribing. The child is a plain `python -P -m miniteams.stt.engines`, so it imports
only its engine's runtime, never the follower's call stack.
"""

import os
import subprocess
import sys
import threading
from collections.abc import Callable
from multiprocessing.connection import Connection

from .line import Line
from .vad import Audio

Transcribe = Callable[[Audio, str], Line]
STOP_GRACE_S = 2.0


class EngineDied(Exception):
    """The engine process exited, broke its pipe or overran its deadline: restart it."""


class ProcessEngine:
    """The parent's handle on one engine process. Calls block: run them in a thread, one at a time.

    stop() may come from another thread while a call or a load runs: the I/O lock keeps the pipes to
    whoever uses them, and stop() kills the child first so that user wakes on EOF and releases them.
    """

    def __init__(self, argv: list[str], timeout_s: float, env: dict[str, str] | None = None) -> None:
        # argv runs the child; the two pipe fds are appended (see serve). env adds to child_env().
        self._argv = argv
        self._env = env or {}
        self._timeout = timeout_s
        self._proc: subprocess.Popen[bytes] | None = None
        self._to: Connection | None = None
        self._from: Connection | None = None
        self._io = threading.Lock()  # the pipes and the child, for a call, a load or a shutdown
        self._state = threading.Lock()  # _proc and _stopped, read by stop() without the I/O lock
        self._stopped = False

    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc else None

    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def start(self, load_timeout_s: float = 600) -> None:
        """Starts the process and waits until its model is loaded; EngineDied once stop() was called."""
        with self._io:
            if self._stopped:
                raise EngineDied("stopped")
            self._shutdown(STOP_GRACE_S)
            child_reads, parent_writes = os.pipe()
            parent_reads, child_writes = os.pipe()
            self._to = Connection(parent_writes, readable=False)
            self._from = Connection(parent_reads, writable=False)
            try:
                fds = (child_reads, child_writes)
                env = child_env() | self._env
                proc = subprocess.Popen([*self._argv, *map(str, fds)], pass_fds=fds, env=env)
                with self._state:
                    self._proc = proc
                    if self._stopped:  # stop() ran meanwhile and saw no child: this one must not live
                        proc.kill()
            except BaseException:
                self._shutdown(0)
                raise
            finally:
                # The child's ends, once: a second close could hit an fd another thread reopened.
                os.close(child_reads)
                os.close(child_writes)
            try:
                self._receive(load_timeout_s)
            except BaseException:
                self._shutdown(0)  # a child that died loading leaves neither a zombie nor open pipes
                raise

    def __call__(self, audio: Audio, prompt: str = "") -> Line:
        with self._io:
            to = self._to
            if to is None:
                raise EngineDied("not started")
            try:
                to.send((audio, prompt))
            except OSError as e:
                raise EngineDied(f"send: {e}") from e
            line = self._receive(self._timeout)
        if not isinstance(line, Line):
            raise EngineDied(f"unexpected answer {type(line).__name__}")
        return line

    def _receive(self, timeout: float) -> object:
        """Under the I/O lock."""
        conn = self._from
        assert conn is not None  # set with _to, under the same lock
        try:
            if not conn.poll(timeout):
                self._shutdown(0)  # hung: no point waiting for it to read EOF
                raise EngineDied(f"no answer within {timeout:g} s")
            kind, value = conn.recv()
        except (EOFError, OSError) as e:
            raise EngineDied(f"receive: {e!r}") from e
        if kind == "error":
            # The model raised on this utterance; the process is fine.
            raise RuntimeError(value)
        return value

    def stop(self, grace_s: float = STOP_GRACE_S) -> None:
        """Ends the engine for good: start() refuses from now on."""
        with self._state:
            self._stopped = True
            proc = self._proc
        if self._io.acquire(blocking=False):  # idle: the child exits on EOF within grace_s
            try:
                self._shutdown(grace_s)
            finally:
                self._io.release()
            return
        # A call or a load holds the pipes: killing the child wakes it on EOF, then it lets go.
        if proc is not None:
            proc.kill()
        with self._io:
            self._shutdown(0)

    def _shutdown(self, grace_s: float) -> None:
        """Under the I/O lock: closes the pipes once, then reaps the child."""
        for conn in (self._to, self._from):
            if conn is not None:
                conn.close()
        self._to = self._from = None
        with self._state:
            proc, self._proc = self._proc, None
        if proc is not None:
            # An idle child returns from serve() on EOF and exits cleanly; a busy or hung one is killed.
            try:
                proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def child_env() -> dict[str, str]:
    """The follower's environment. OTel off: under opentelemetry-instrument the child would start its
    own SDK and exporter for no span. HF offline: the models are local; a download would be a bug."""
    return os.environ | {"OTEL_SDK_DISABLED": "true", "HF_HUB_OFFLINE": "1"}


def serve(transcribe: Transcribe, read_fd: int, write_fd: int) -> None:
    """The child's loop: ready once loaded, then one answer per utterance until the parent goes."""
    inbox = Connection(read_fd, writable=False)
    outbox = Connection(write_fd, readable=False)
    outbox.send(("ready", os.getpid()))
    while True:
        try:
            audio, prompt = inbox.recv()
        except EOFError:
            return
        try:
            outbox.send(("ok", transcribe(audio, prompt)))
        except Exception as e:  # one utterance the model chokes on is an error, not a dead engine
            outbox.send(("error", f"{type(e).__name__}: {e}"))


def argv(engine: str, model: str, cpu_threads: int) -> list[str]:
    # -P: no cwd on sys.path, the follower's cwd is not code.
    return [sys.executable, "-P", "-m", "miniteams.stt.engines", engine, model, str(cpu_threads)]


def main() -> None:  # pragma: no cover - runs only as the engine child, loading a real model
    from ..logging import setup_logging

    engine, model, threads, read_fd, write_fd = sys.argv[1:]
    setup_logging(os.getenv("MINITEAMS_LOG_LEVEL", "INFO"))
    transcribe: Transcribe
    if engine == "parakeet":
        from .parakeet import ParakeetTranscriber

        transcribe = ParakeetTranscriber(model, int(threads))
    else:
        from .transcribe import Transcriber

        transcribe = Transcriber(model, int(threads))
    serve(transcribe, int(read_fd), int(write_fd))


if __name__ == "__main__":
    main()
