# Copied from grill-room `stt/tests/test_pipeline.py` @ ae133de (spec 044, AC16), trimmed with the code it
# tests: the room API, heartbeat, prompt and metric tests are gone; lines go to a sink.
# Copyright grill-room authors. Licensed under the Apache License, Version 2.0:
# http://www.apache.org/licenses/LICENSE-2.0
import asyncio
import logging
import time

import numpy as np
import pytest
import structlog
import structlog.testing

from miniteams.stt.line import Line
from miniteams.stt.pipeline import Pipeline, Sink, Utterance
from miniteams.stt.vad import Audio


@pytest.fixture(autouse=True)
def _info_logs() -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))


def utterance(msi: int = 7) -> Utterance:
    return Utterance(msi, np.ones(16000, np.float32), time.time())


def sink(lines: list[str]) -> Sink:
    return lambda _u, line: lines.append(line.text)


async def test_line_done_logs_the_gap_and_the_forced_cut() -> None:
    """018-AC9: line_done says the pause before the line and whether its cut was forced."""
    p = Pipeline([lambda _a, _p: Line("ok", "en")], sink([]), size=1)
    u = Utterance(7, np.ones(16000, np.float32), time.time(), gap_s=0.0, forced=True)
    with structlog.testing.capture_logs() as logs:
        await p.process(u)
        await p.process(utterance())
    done = [(e["gap_s"], e["forced"]) for e in logs if e["event"] == "line_done"]
    assert done == [(0.0, True), (None, False)]


async def test_queue_is_bounded() -> None:
    """001-AC7: overlapping speech queues up to a bound instead of growing without limit; a drop is
    logged."""
    lines: list[str] = []
    p = Pipeline([lambda _a, _p: Line("hello", "en")], sink(lines), size=2)
    assert p.submit(utterance()) and p.submit(utterance())
    with structlog.testing.capture_logs() as logs:
        assert not p.submit(utterance(msi=8))
    assert [(e["event"], e["msi"], e["queue"]) for e in logs] == [("utterance_dropped", 8, 2)]
    task = asyncio.create_task(p.run())
    await asyncio.wait_for(p.idle(), 5)
    task.cancel()
    assert lines == ["hello", "hello"]


async def test_empty_transcripts_are_not_written_and_audio_is_released() -> None:
    seen: list[Audio] = []

    def transcribe(a: Audio, _p: str) -> Line:
        seen.append(a)
        return Line("", "fr")

    def written(*_a: object) -> None:
        raise AssertionError("wrote an empty line")

    u = utterance()
    await Pipeline([transcribe], written, size=1).process(u)
    assert len(seen[0]) == 16000
    # 001-AC9: the samples are dropped as soon as the line is done, not kept with the utterance.
    assert len(u.audio) == 0


async def test_a_failing_utterance_does_not_stop_the_queue() -> None:
    stored: list[str] = []
    texts = iter(["first", "boom", "third"])

    def transcribe(_a: Audio, _p: str) -> Line:
        t = next(texts)
        if t == "boom":
            raise RuntimeError("model error")
        return Line(t, "en")

    p = Pipeline([transcribe], sink(stored), size=4)
    for _ in range(3):
        p.submit(utterance())
    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(p.run())
        await asyncio.wait_for(p.idle(), 5)
        task.cancel()
    assert stored == ["first", "third"]
    assert [e["event"] for e in logs if e["log_level"] == "error"] == ["utterance_failed"]


async def test_implausible_lines_are_logged_not_written() -> None:
    """001-AC8: babble the model made of a TTS glitch (spike S5) never reaches the transcript."""
    stored: list[str] = []
    babble = Line("三二八四八四八四八二八四八三二八四八八", "zh", 0.3, -0.5, 0.2, 3.1)
    texts = iter([babble, Line("Oui.", "en", 0.28, -0.43, 0.26, 0.33)])
    p = Pipeline([lambda _a, _p: next(texts)], sink(stored), size=4)
    with structlog.testing.capture_logs() as logs:
        await p.process(utterance())
        await p.process(utterance())
    assert stored == ["Oui."]
    assert [e["reason"] for e in logs if e["event"] == "line_implausible"] == ["repetition"]
