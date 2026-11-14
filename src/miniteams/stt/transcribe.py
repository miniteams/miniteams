# Copied from grill-room `stt/grill_stt/transcribe.py` @ 89744d1 (spec 044, AC16).
# Copyright grill-room authors. Licensed under the Apache License, Version 2.0:
# http://www.apache.org/licenses/LICENSE-2.0
"""faster-whisper on CPU: small int8, language detected per utterance (spike S1-bis)."""

from dataclasses import replace

import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.audio import pad_or_trim
from faster_whisper.tokenizer import Tokenizer

from .line import Line
from .vad import SR, Audio

__all__ = ["Line", "Transcriber"]


class Transcriber:
    def __init__(self, model: str, cpu_threads: int, temperature: float | list[float] = 0.0) -> None:
        self._model = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=cpu_threads)
        # With a language given, transcribe() encodes nothing until its segments are read: this only
        # builds the decoding options, reused for every utterance.
        _, info = self._model.transcribe(
            np.zeros(SR, np.float32),
            language="en",
            beam_size=1,
            temperature=temperature,
            without_timestamps=True,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        self._options = info.transcription_options

    def __call__(self, audio: Audio, prompt: str = "") -> Line:
        # The room's glossary goes in initial_prompt, never hotwords: they did worse in spike S10.
        m = self._model
        options = replace(self._options, initial_prompt=prompt or None)
        features = m.feature_extractor(audio)
        # One encoder pass serves language detection and decoding. transcribe(language=None) runs a
        # second one and drops the first (faster-whisper 1.2.1, spike S1-bis): half the CPU per line.
        first = features[:, : min(features.shape[-1] - 1, m.feature_extractor.nb_max_frames)]
        encoded = m.encode(pad_or_trim(first))
        # Each utterance is detected on its own, so FR and EN mix freely (D16). An English-only model
        # has no language head to ask.
        language, probability = "en", 1.0
        if m.model.is_multilingual:
            (token, probability), *_ = m.model.detect_language(encoded)[0]
            language = token[2:-2]
        tokenizer = Tokenizer(m.hf_tokenizer, m.model.is_multilingual, task="transcribe", language=language)
        segments = list(m.generate_segments(features, tokenizer, options, False, encoded))
        # Whisper's own silence rule: a segment that is likely no speech and decoded with low
        # confidence is a hallucination ("merci d'avoir regardé cette vidéo").
        kept = [s for s in segments if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)]
        text = " ".join(t for t in (s.text.strip() for s in kept) if t)
        return Line(
            text,
            language,
            probability,
            min((s.avg_logprob for s in kept), default=0.0),
            max((s.no_speech_prob for s in kept), default=0.0),
            max((s.compression_ratio for s in kept), default=1.0),
        )
