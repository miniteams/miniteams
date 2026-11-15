# Copied from grill-room `stt/grill_stt/line.py` @ 89744d1 (spec 044, AC16).
# Copyright grill-room authors. Licensed under the Apache License, Version 2.0:
# http://www.apache.org/licenses/LICENSE-2.0
"""One transcript and the confidence the pipeline judges it on."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Line:
    text: str
    language: str
    # Whisper's confidence: in the language, and over the kept segments the lowest mean token
    # log-probability, the highest chance of no speech and the highest gzip ratio (repetition). The
    # pipeline judges plausibility on them.
    language_probability: float = 1.0
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    compression_ratio: float = 1.0
