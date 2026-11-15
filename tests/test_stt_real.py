"""Real speech-to-text engines (spec 044): skipped until scripts/build-transcribe.sh has run.

The first two tests are grill-room's `stt/tests/test_plausibility.py` @ 682b106 model tests (Apache-2.0),
moved here so only this module runs under the raised memory cap (conftest._stt_memory).
"""

import asyncio
import json
import os
import statistics
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from stt_helpers import FIXTURES, pcm, read, real_settings

from miniteams.stt.line import Line
from miniteams.stt.pipeline import Plausibility, guess_language, implausible, implausible_parakeet
from miniteams.stt.session import Session
from miniteams.stt.vad import SR, Audio, Segmenter, Silero

pytestmark = pytest.mark.stt
RULES = Plausibility()


@pytest.fixture(scope="module", params=["whisper", "parakeet"])
def transcriber(request: pytest.FixtureRequest) -> Callable[[Audio], Line]:
    """Each engine in-process, from the built runtime (spec 007, AC2 and AC15)."""
    s = real_settings(request.param)
    if request.param == "parakeet":
        # The binding finds the library through it when first imported.
        os.environ.setdefault("TRANSCRIBE_LIBRARY", str(s.stt_library))
        from miniteams.stt.parakeet import ParakeetTranscriber

        return ParakeetTranscriber(str(s.stt_model), cpu_threads=4)
    from miniteams.stt.transcribe import Transcriber

    return Transcriber(str(s.stt_model), cpu_threads=4)


def cut_one(name: str) -> Audio:
    seg = Segmenter(Silero())
    audio = read(name)
    cut: list[Audio] = []
    for i in range(0, len(audio), 160):
        cut += seg.feed(audio[i : i + 160])
    (u,) = cut + seg.flush()
    return u


def test_synthetic_voices_post_answers_and_drop_babble(transcriber: Callable[[Audio], Line]) -> None:
    """001-AC8, 007-AC3: one-word answers are posted with their words; Whisper drops the S5 TTS glitch."""
    parakeet = type(transcriber).__name__ == "ParakeetTranscriber"
    judge = implausible_parakeet if parakeet else implausible
    files = json.loads((FIXTURES / "voices.json").read_text())["files"]
    got = {}
    for f in files:
        u = cut_one(f["file"])
        ln = transcriber(u)
        posted = bool(ln.text.strip()) and judge(ln, len(u) / SR, RULES) is None
        got[f["file"]] = posted
        if posted and "heard" in f:
            assert f["heard"] in ln.text.lower(), (f["file"], ln.text)
    # Parakeet has no confidence to drop TTS glitches with: they post (voices.json, post_parakeet).
    key = "post_parakeet" if parakeet else "post"
    assert got == {f["file"]: f.get(key, f["post"]) for f in files}


def test_french_dense_in_english_terms_is_not_an_english_leak(transcriber: Callable[[Audio], Line]) -> None:
    """007-AC5: a French sentence full of English tech terms is guessed fr, its English twin en."""
    for f in json.loads((FIXTURES / "voices.json").read_text())["leak"]:
        ln = transcriber(cut_one(f["file"]))
        assert guess_language(ln.text) == f["lang"], (f["file"], ln.text)


CALL = "00000000-0000-4000-8000-000000000002"
FRAME = SR // 50
# Three Piper voices on three MSIs, the second and third starting while the first still talks.
REPLAY = {
    1: ("siwis-jargon.wav", 0.0, "kubernetes"),
    2: ("joe-jargon.wav", 2.0, "helm"),
    3: ("cori-okay.wav", 6.0, "okay"),
}


@pytest.mark.parametrize("engine", ["parakeet", "whisper"])
async def test_replay_puts_each_phrase_on_its_speaker(engine: str, tmp_path: Path) -> None:
    """044-AC6, AC7 (offline), AC8, Phase 4 DoD: a synthetic multi-speaker call through the real engine
    processes, fed in real time as 20 ms frames with wall-clock times; each expected phrase lands on its
    speaker's line. Prints the per-utterance latency, end of speech to line written."""
    settings = real_settings(engine)
    names = {msi: (f"Voice {msi}", f"8:orgid:00000000-0000-4000-8000-00000000000{msi}") for msi in REPLAY}
    session = Session(settings, tmp_path, CALL, "19:synthetic@thread.v2", names.get, kind="group")
    await asyncio.wait_for(session.start(), 120)
    assert session.transcript is not None
    path = session.transcript.path
    seen: dict[int, float] = {}

    async def watch() -> None:
        while True:
            n = len(path.read_bytes().splitlines())
            for i in range(1, n):
                seen.setdefault(i, time.time())
            await asyncio.sleep(0.01)

    watcher = asyncio.create_task(watch())
    clips = {msi: (pcm(name), at) for msi, (name, at, _) in REPLAY.items()}
    length = max(at + len(a) / SR for a, at in clips.values())
    t0 = time.time()
    for step in range(int(length * 50) + 1):
        offset = step / 50
        t_wall = t0 + offset
        for msi, (audio, at) in clips.items():
            i = round((offset - at) * SR)
            if 0 <= i < len(audio):
                session.feed(msi, audio[i : i + FRAME], t_wall)
        await asyncio.sleep(max(0.0, t_wall + 1 / 50 - time.time()))
    # The tracks went quiet: the ticker cuts them, no close needed for the lines.
    deadline = time.time() + 30
    while len(seen) < len(REPLAY) and time.time() < deadline:
        await asyncio.sleep(0.05)
    await session.close()
    watcher.cancel()
    got = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()[1:]]
    for msi, (name, _at, phrase) in REPLAY.items():
        mine = [x for x in got if x["speaker"] == names[msi][0]]
        assert mine and all(x["mri"] == names[msi][1] for x in mine), (msi, got)
        assert phrase in " ".join(x["text"] for x in mine).lower(), (msi, name, mine)
    ends = [datetime.fromisoformat(x["end"]).timestamp() for x in got]
    latency = [seen[i + 1] - end for i, end in enumerate(ends) if i + 1 in seen]
    print(
        f"\n{engine}: {len(got)} lines, latency s: median {statistics.median(latency):.2f}, "
        f"max {max(latency):.2f}, each {[round(v, 2) for v in latency]}"
    )
