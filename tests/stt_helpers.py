"""Shared by the speech-to-text tests (spec 044): the Piper fixtures and the real runtime.

`pcm` and `read` follow grill-room's `stt/tests/test_plausibility.py` @ 682b106 `read()`, Apache-2.0.
"""

import os
import wave
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest

from miniteams.config import Settings, _default_cache_dir
from miniteams.follow import stt_ready
from miniteams.stt.vad import Audio

FIXTURES = Path(__file__).with_name("stt_fixtures")
FAKE_ENGINE = str(Path(__file__).with_name("stt_fake_engine.py"))
# Read at import, before the autouse fixture points XDG_CACHE_HOME at a temp dir: where
# scripts/build-transcribe.sh put the library and models.
RUNTIME = Path(os.environ.get("MINITEAMS_CACHE_DIR") or _default_cache_dir())


def pcm(name: str) -> npt.NDArray[np.int16]:
    """A fixture's 16 kHz int16 samples."""
    with wave.open(str(FIXTURES / name)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2")


def read(name: str) -> Audio:
    audio: Audio = pcm(name).astype(np.float32) / np.float32(32768)
    return audio


def real_settings(engine: str) -> Settings:
    """Settings on the built runtime; skips when it is absent (fails under CI: never silently)."""
    s = Settings(tenant_id="t", stt_engine=engine)
    s.cache_dir = RUNTIME
    if fix := stt_ready(s):
        if os.getenv("CI"):
            pytest.fail(fix)
        pytest.skip(fix)
    return s
