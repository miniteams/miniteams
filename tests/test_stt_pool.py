# Copied from grill-room `stt/tests/test_pool.py` @ 89744d1 (spec 044, AC16), trimmed with the code it
# tests: no metrics or health; lines go to a sink.
# Copyright grill-room authors. Licensed under the Apache License, Version 2.0:
# http://www.apache.org/licenses/LICENSE-2.0
"""The engine pool: parallel speakers, per-speaker order, dead engines, slow waits."""

import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import structlog
from structlog.testing import capture_logs
from stt_fake_engine import DIE, HANG
from stt_helpers import FAKE_ENGINE

from miniteams.stt.engines import EngineDied, ProcessEngine, child_env, serve
from miniteams.stt.line import Line
from miniteams.stt.pipeline import Pipeline, Sink, Utterance
from miniteams.stt.vad import Audio


def room(posted: list[tuple[int, str]]) -> Sink:
    return lambda u, line: posted.append((u.msi, line.text))


def utt(pid: int, n: float, marker: float = 0.0) -> Utterance:
    audio = np.zeros(16000, np.float32)
    audio[0], audio[1] = marker, n
    return Utterance(pid, audio, time.time())


class Gate:
    """An in-process engine whose calls block until released, recording who was inside when."""

    def __init__(self) -> None:
        self.entered = threading.Semaphore(0)
        self.release = threading.Semaphore(0)
        self.inside: list[int] = []
        self.started: list[int] = []
        self.overlap = False
        self._lock = threading.Lock()

    def __call__(self, audio: Audio, _prompt: str) -> Line:
        pid = int(audio[2])
        with self._lock:
            self.overlap |= pid in self.inside
            self.inside.append(pid)
            self.started.append(pid)
        self.entered.release()
        assert self.release.acquire(timeout=10), "never released"
        with self._lock:
            self.inside.remove(pid)
        return Line(f"line {float(audio[1]):g}", "en")


def gated(pid: int, n: float) -> Utterance:
    u = utt(pid, n)
    u.audio[2] = pid
    return u


async def entered(gate: Gate, n: int = 1) -> None:
    for _ in range(n):
        assert await asyncio.to_thread(gate.entered.acquire, True, 10), "no engine took an utterance"


async def test_two_speakers_run_in_parallel() -> None:
    """007-AC6: each engine takes the next utterance when free: two speakers, two engines at once."""
    posted: list[tuple[int, str]] = []
    gate = Gate()
    p = Pipeline([gate, gate], room(posted), size=8)
    task = asyncio.create_task(p.run())
    p.submit(gated(1, 1))
    p.submit(gated(2, 2))
    await entered(gate, 2)
    assert sorted(gate.inside) == [1, 2]
    gate.release.release(2)
    await asyncio.wait_for(p.idle(), 10)
    task.cancel()
    assert sorted(posted) == [(1, "line 1"), (2, "line 2")]


async def test_one_speaker_posts_in_speech_order() -> None:
    """007-AC7: a speaker's next utterance waits for their previous one; another speaker passes it."""
    posted: list[tuple[int, str]] = []
    gate = Gate()
    p = Pipeline([gate, gate], room(posted), size=8)
    task = asyncio.create_task(p.run())
    p.submit(gated(1, 1))
    await entered(gate)
    p.submit(gated(1, 2))
    p.submit(gated(2, 3))
    # The free engine skips speaker 1's second line for speaker 2's.
    await entered(gate)
    assert gate.started == [1, 2]
    gate.release.release(2)
    await entered(gate)
    assert gate.started == [1, 2, 1]
    gate.release.release()
    await asyncio.wait_for(p.idle(), 10)
    task.cancel()
    assert not gate.overlap
    assert [text for pid, text in posted if pid == 1] == ["line 1", "line 2"]


def fake_engines(n: int, timeout_s: float = 30) -> list[ProcessEngine]:
    return [ProcessEngine([sys.executable, FAKE_ENGINE], timeout_s) for _ in range(n)]


async def test_a_dead_engine_is_restarted_and_the_other_carries_on() -> None:
    """007-AC6: an engine process that dies costs its utterance (an error) and is restarted; the
    other engine finishes its own utterance meanwhile."""
    posted: list[tuple[int, str]] = []
    engines = fake_engines(2)
    p = Pipeline(engines, room(posted), size=8)
    await asyncio.wait_for(p.start(), 30)
    pids = {e.pid for e in engines}
    task = asyncio.create_task(p.run())
    try:
        p.submit(utt(1, 1, DIE))
        p.submit(utt(2, 2))
        await asyncio.wait_for(p.idle(), 30)
        assert posted == [(2, "line 2")]
        # The restart happens before the engine takes anything else: the next line proves it.
        p.submit(utt(1, 3))
        p.submit(utt(2, 4))
        await asyncio.wait_for(p.idle(), 30)
        assert sorted(posted[1:]) == [(1, "line 3"), (2, "line 4")]
        assert all(e.alive() for e in engines)
        assert len({e.pid for e in engines} - pids) == 1
        # One dies while idle: it is restarted before its next utterance, which is not lost.
        engines[0]._proc.kill()  # type: ignore[union-attr]
        await asyncio.to_thread(engines[0]._proc.wait)  # type: ignore[union-attr]
        for n in (5, 6, 7):
            p.submit(utt(n, n))
        await asyncio.wait_for(p.idle(), 30)
        assert sorted(posted[3:]) == [(5, "line 5"), (6, "line 6"), (7, "line 7")]
        assert all(e.alive() for e in engines)
    finally:
        task.cancel()
        p.stop()


async def test_a_hung_engine_is_killed_at_its_deadline() -> None:
    """007-AC6: an engine that does not answer within APP_ENGINE_TIMEOUT_S is killed, then restarted."""
    (e,) = fake_engines(1, timeout_s=0.5)
    await asyncio.to_thread(e.start, 30)
    try:
        with pytest.raises(EngineDied, match="no answer"):
            await asyncio.to_thread(e, utt(1, 1, HANG).audio, "")
        assert not e.alive()
        with pytest.raises(EngineDied, match="not started"):
            e(utt(1, 1).audio, "")
        await asyncio.to_thread(e.start, 30)
        assert (await asyncio.to_thread(e, utt(1, 5).audio, "")).text == "line 5"
    finally:
        e.stop()


async def test_an_engine_that_dies_loading_leaves_nothing_behind() -> None:
    """A child that exits before ready is reaped and its pipes closed; its env reaches it."""
    probe = "import os, sys; sys.exit(0 if os.environ.get('TRANSCRIBE_LIBRARY') == '/x.so' else 7)"
    for code in ("import sys; sys.exit(3)", probe):
        e = ProcessEngine([sys.executable, "-c", code], 30, {"TRANSCRIBE_LIBRARY": "/x.so"})
        with pytest.raises(EngineDied):
            await asyncio.to_thread(e.start, 30)
        assert e.pid is None and not e.alive()
    assert child_env()["OTEL_SDK_DISABLED"] == "true" and child_env()["HF_HUB_OFFLINE"] == "1"


async def test_a_slow_queue_wait_is_warned() -> None:
    """007-AC19: the wait from the VAD cut to an engine, with a warning past 10 s."""
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    now = [0.0]
    posted: list[tuple[int, str]] = []
    gate = Gate()
    p = Pipeline([gate], room(posted), size=8, clock=lambda: now[0])
    p.submit(gated(1, 1))
    p.submit(gated(2, 2))
    with capture_logs() as logs:
        task = asyncio.create_task(p.run())
        await entered(gate)
        now[0] = 12.5
        gate.release.release()
        await entered(gate)
        gate.release.release()
        await asyncio.wait_for(p.idle(), 10)
        task.cancel()
    slow = [e for e in logs if e["event"] == "queue_wait_slow"]
    assert [(e["msi"], e["wait_s"], e["log_level"]) for e in slow] == [(2, 12.5, "warning")]


# --- spec 044: the engine protocol's failure paths, in-process ---

FAILING = """import sys
from miniteams.stt.engines import serve
answers = iter([ValueError("bad audio"), "not a line"])
def transcribe(_a, _p):
    a = next(answers)
    if isinstance(a, Exception):
        raise a
    return a
serve(transcribe, int(sys.argv[1]), int(sys.argv[2]))
"""


def test_serve_answers_errors_and_returns_on_eof() -> None:
    """The child's loop, run in a thread: ready, a line, an error for an utterance the model chokes
    on, and a clean return once the parent closes its end."""
    from multiprocessing.connection import Connection

    child_reads, parent_writes = os.pipe()
    parent_reads, child_writes = os.pipe()
    to, back = Connection(parent_writes, readable=False), Connection(parent_reads, writable=False)

    def transcribe(audio: Audio, _p: str) -> Line:
        if audio[0]:
            raise ValueError("bad audio")
        return Line("ok", "en")

    t = threading.Thread(target=serve, args=(transcribe, child_reads, child_writes))
    t.start()
    assert back.recv() == ("ready", os.getpid())
    to.send((np.zeros(4, np.float32), ""))
    assert back.recv() == ("ok", Line("ok", "en"))
    to.send((np.ones(4, np.float32), ""))
    assert back.recv() == ("error", "ValueError: bad audio")
    to.close()
    t.join(10)
    assert not t.is_alive()
    back.close()


async def test_a_model_error_keeps_the_engine_and_a_bad_answer_kills_it() -> None:
    e = ProcessEngine([sys.executable, "-c", FAILING], 30)
    await asyncio.to_thread(e.start, 30)
    try:
        with pytest.raises(RuntimeError, match="ValueError: bad audio"):
            e(np.zeros(4, np.float32))
        assert e.alive()
        with pytest.raises(EngineDied, match="unexpected answer str"):
            e(np.zeros(4, np.float32))
        e._proc.kill()  # type: ignore[union-attr]
        e._proc.wait()  # type: ignore[union-attr]
        with pytest.raises(EngineDied):  # its pipe is gone: send or receive fails
            for _ in range(100):
                e(np.zeros(1 << 16, np.float32))
    finally:
        e.stop()


def test_an_engine_that_cannot_spawn_leaves_nothing_behind(tmp_path: Path) -> None:
    e = ProcessEngine([str(tmp_path / "missing")], 30)
    with pytest.raises(FileNotFoundError):
        e.start(30)
    assert e.pid is None and e._to is None and e._from is None


async def test_a_restart_that_fails_backs_off_then_retries() -> None:
    """An engine whose model fails to load again is retried with a back-off; an in-process engine
    that dies is not restarted."""
    (e,) = fake_engines(1)
    real_start = e.start
    calls: list[int] = []

    def flaky(load_timeout_s: float = 600) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise EngineDied("load failed")
        real_start(load_timeout_s)

    e.start = flaky  # type: ignore[method-assign]
    p = Pipeline([e], room([]), size=1)
    try:
        with capture_logs() as logs:
            await asyncio.wait_for(p._restart(e), 30)
        assert len(calls) == 2 and e.alive()
        assert [x["event"] for x in logs if x["log_level"] == "error"] == ["engine_restart_failed"]
    finally:
        p.stop()

    def dies(_a: Audio, _p: str) -> Line:
        raise EngineDied("in-process")

    q = Pipeline([dies], room([]), size=1)
    q.submit(utt(1, 1))
    task = asyncio.create_task(q.run())
    await asyncio.wait_for(q.idle(), 10)
    task.cancel()


# --- spec 044: stop() from another thread ---


async def test_stop_during_a_hung_utterance_ends_it_with_engine_died() -> None:
    """A call's close stops the engines while a worker thread waits on a hung one: the worker gets
    EngineDied, never an AttributeError on a pipe pulled from under it; no child is left, each pipe is
    closed once, and the engine does not start again."""
    (e,) = fake_engines(1, timeout_s=30)
    await asyncio.to_thread(e.start, 30)
    proc = e._proc
    assert proc is not None
    closes: list[int] = []
    for conn in (e._to, e._from):
        assert conn is not None
        real = conn.close
        conn.close = lambda real=real, conn=conn: (closes.append(id(conn)), real())[1]  # type: ignore[method-assign]
    worker = asyncio.create_task(asyncio.to_thread(e, utt(1, 1, HANG).audio, ""))
    await asyncio.sleep(0.3)  # the child holds the utterance
    began = time.monotonic()
    await asyncio.to_thread(e.stop)
    with pytest.raises(EngineDied):
        await asyncio.wait_for(worker, 10)
    assert time.monotonic() - began < 2
    assert proc.poll() is not None and not e.alive() and e.pid is None
    assert sorted(closes) == sorted(set(closes)) and len(closes) == 2
    with pytest.raises(EngineDied, match="stopped"):
        e.start(30)
    with pytest.raises(EngineDied, match="not started"):
        e(utt(1, 2).audio, "")


async def test_stop_during_a_load_leaves_no_child() -> None:
    """A restart loading a model when the call closes: the load ends in EngineDied, nothing runs after."""
    slow = "import sys, time\nfrom miniteams.stt.engines import serve\ntime.sleep(30)\n"
    e = ProcessEngine([sys.executable, "-c", slow], 30)
    load = asyncio.create_task(asyncio.to_thread(e.start, 60))
    for _ in range(500):
        if e.pid is not None:
            break
        await asyncio.sleep(0.01)
    proc = e._proc
    assert proc is not None
    await asyncio.to_thread(e.stop)
    with pytest.raises(EngineDied):
        await asyncio.wait_for(load, 10)
    assert proc.poll() is not None and e.pid is None


def test_a_stop_racing_a_spawn_kills_the_new_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop() that ran while start() was spawning (it saw no child yet) still ends that child."""
    import subprocess

    (e,) = fake_engines(1)
    real_popen = subprocess.Popen
    spawned: list[subprocess.Popen[bytes]] = []

    def popen(*a: object, **k: object) -> subprocess.Popen[bytes]:
        e._stopped = True  # stop() between the check and the spawn
        spawned.append(real_popen(*a, **k))  # type: ignore[call-overload]
        return spawned[-1]

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(EngineDied):
        e.start(30)
    assert spawned[0].poll() is not None and e.pid is None
