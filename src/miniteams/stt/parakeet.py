# Copied from grill-room `stt/grill_stt/parakeet.py` @ 89744d1 (spec 044, AC16).
# Copyright grill-room authors. Licensed under the Apache License, Version 2.0:
# http://www.apache.org/licenses/LICENSE-2.0
"""parakeet-ultra through transcribe.cpp: Q8_0, greedy, no language argument (spec 007, spike S10d)."""

import zlib

import structlog

from .line import Line
from .vad import Audio

log = structlog.get_logger()
# transcribe.cpp's levels: 1 info, 2 warn, 3 error, 4 debug, 5 continuation.
WARN, ERROR = 2, 3


def native_log(level: int, message: str) -> None:
    """transcribe.cpp's own lines, as JSON: left to stderr they would break the one-object-per-line log."""
    text = message.strip()
    if text:
        (log.warning if level in (WARN, ERROR) else log.debug)("transcribe_cpp", level=level, message=text)


def compression_ratio(text: str) -> float:
    """Whisper's repetition measure, on the text: a looped phrase compresses well."""
    raw = text.encode()
    return len(raw) / len(zlib.compress(raw)) if raw else 0.0


class ParakeetTranscriber:
    def __init__(self, model: str, cpu_threads: int) -> None:
        # Importing the binding dlopens libtranscribe: only this engine needs it.
        import transcribe_cpp

        transcribe_cpp.set_log_callback(native_log)
        self._model = transcribe_cpp.Model(model, backend="cpu")
        self._session = self._model.session(n_threads=cpu_threads)

    def __call__(self, audio: Audio, prompt: str = "") -> Line:
        # The prompt is ignored: Parakeet has no vocabulary input in transcribe.cpp (spec 007, AC14).
        text = self._session.run(memoryview(audio)).text.strip()
        return Line(text, "", compression_ratio=compression_ratio(text))
