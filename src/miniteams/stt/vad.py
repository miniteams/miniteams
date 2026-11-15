# Copied from grill-room `stt/grill_stt/vad.py` @ ae133de (spec 044, AC16), modified: Cut carries trail_s and
# end_ago_s, set in Segmenter._utterance, so a cut is placed on the timeline without its private state.
# Copyright grill-room authors. Licensed under the Apache License, Version 2.0:
# http://www.apache.org/licenses/LICENSE-2.0
"""Streaming voice activity detection: cuts a live 16 kHz track into utterances on pauses."""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import numpy.typing as npt

SR = 16000
WINDOW = 512  # samples per Silero decision, 32 ms
CONTEXT = 64  # samples of the previous window Silero v6 expects in front of each window

Audio = npt.NDArray[np.float32]
Probs = Callable[[Audio], npt.NDArray[np.float32]]


class Silero:
    """faster-whisper's Silero v6 ONNX model, fed window by window with its state carried over.

    faster-whisper only runs it on a whole file and resets the state on every call, which would
    restart the model on each 10 ms LiveKit frame.
    """

    def __init__(self) -> None:
        import onnxruntime
        from faster_whisper.utils import get_assets_path

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self._session = onnxruntime.InferenceSession(
            str(Path(get_assets_path()) / "silero_vad_v6.onnx"),
            providers=["CPUExecutionProvider"],
            sess_options=opts,
        )
        self.reset()

    def reset(self) -> None:
        """Forgets the audio heard so far, for a track resumed after a gap."""
        self._h = np.zeros((1, 1, 128), np.float32)
        self._c = np.zeros((1, 1, 128), np.float32)
        self._context = np.zeros(CONTEXT, np.float32)

    def __call__(self, windows: Audio) -> npt.NDArray[np.float32]:
        """Speech probability of each row of windows, shape (n, WINDOW)."""
        prev = np.vstack([self._context, windows[:-1, -CONTEXT:]])
        batch = np.hstack([prev, windows]).astype(np.float32)
        out, self._h, self._c = self._session.run(None, {"input": batch, "h": self._h, "c": self._c})
        self._context = windows[-1, -CONTEXT:].copy()
        return np.asarray(out, np.float32).reshape(-1)


@dataclass(frozen=True, eq=False)
class Cut:
    """One utterance, with what the room needs about its timing."""

    audio: Audio
    # Seconds since the cut audio ended, as heard: a line's latency counts from there.
    silent_s: float
    # 018-AC1: seconds from the previous utterance's last voiced window to this one's first; 0 after a
    # forced cut the speaker talked through, None for the first utterance since the track (re)started.
    gap_s: float | None
    # Cut at max_speech_s, the speaker still talking.
    forced: bool
    # Non-speech kept at the end of audio, after its last voiced window: the pause cut's pad, or the
    # pause a quiet forced cut landed in.
    trail_s: float = 0.0
    # Audio heard after the end of audio, samples not yet judged included: audio ended that long before
    # the end of what was fed.
    end_ago_s: float = 0.0


@dataclass
class Segmenter:
    """Accumulates speech and emits it as one utterance once the speaker pauses.

    Thresholds follow faster-whisper's VadOptions. min_silence_ms was 500 (S1-bis) until spike S5:
    that cut 18 script lines into 32 utterances, each paying a full encoder pass; 700 and up keep a
    line whole there, and 800 leaves a margin for slower talkers.
    """

    probs: Probs
    threshold: float = 0.5
    neg_threshold: float = 0.35
    min_silence_ms: int = 800
    min_speech_ms: int = 400
    max_speech_s: float = 15.0
    # 018-AC3: a forced cut lands on the quietest window this far back, not mid-word; 0 cuts hard.
    cut_search_s: float = 3.0
    pad_ms: int = 200
    _pending: Audio = field(default_factory=lambda: np.zeros(0, np.float32))
    _preroll: deque[tuple[Audio, np.float32]] = field(default_factory=deque)
    _speech: list[Audio] = field(default_factory=list)
    # The speech probability of each window of _speech, and the track window index of _speech[0].
    _probs: list[np.float32] = field(default_factory=list)
    _start: int = 0
    _seen: int = 0
    _silent: int = 0
    # The window after the last utterance's last voiced one.
    _prev_end: int | None = None
    # Samples fed after the window being judged: the rest of the batch, then the partial window.
    _ahead: int = 0

    def _windows(self, ms: float) -> int:
        return max(1, round(ms * SR / 1000 / WINDOW))

    def forget_gap(self) -> None:
        """The next utterance, even the one in progress, gets no gap: the pause before it is unknown."""
        self._prev_end = None

    def feed(self, samples: Audio) -> list[Audio]:
        """Adds samples; returns the utterances that ended in them."""
        return [c.audio for c in self.feed_timed(samples)]

    def flush(self) -> list[Audio]:
        """Emits the speech in progress, when the track ends."""
        return [c.audio for c in self.flush_timed()]

    def feed_timed(self, samples: Audio) -> list[Cut]:
        """As feed, each utterance with its timing (Cut)."""
        buf = np.concatenate([self._pending, samples])
        n = len(buf) // WINDOW
        self._pending = buf[n * WINDOW :]
        if n == 0:
            return []
        windows = buf[: n * WINDOW].reshape(n, WINDOW)
        out: list[Cut] = []
        pad = self._windows(self.pad_ms)
        # The probabilities stay float32: 0.35 from the model must compare as the model's 0.35.
        for k, (w, p) in enumerate(zip(windows, self.probs(windows), strict=True)):
            self._seen += 1
            self._ahead = (n - k - 1) * WINDOW + len(self._pending)
            if not self._speech:
                self._preroll.append((w, p))
                if len(self._preroll) > pad:
                    self._preroll.popleft()
                if p >= self.threshold:
                    self._speech = [x for x, _ in self._preroll]
                    self._probs = [q for _, q in self._preroll]
                    self._start = self._seen - len(self._preroll)
                    self._silent = 0
                    self._preroll.clear()
                continue
            self._speech.append(w)
            self._probs.append(p)
            if p >= self.threshold:
                self._silent = 0
            elif p < self.neg_threshold:
                self._silent += 1
            if self._silent >= self._windows(self.min_silence_ms):
                out += self._emit()
            elif len(self._speech) >= self._windows(self.max_speech_s * 1000):
                out += self._cut()
        return out

    def flush_timed(self) -> list[Cut]:
        """As flush; the track ends, so the next utterance has no gap before it, nor any audio from
        before the flush."""
        self._ahead = len(self._pending)
        out = self._emit() if self._speech else []
        self._prev_end = None
        self._preroll.clear()
        self._pending = self._pending[:0]
        return out

    def _emit(self, forced: bool = False) -> list[Cut]:
        # Keep pad_ms of the trailing silence: Whisper clips the last word without it.
        keep = len(self._speech) - max(0, self._silent - self._windows(self.pad_ms))
        out = self._utterance(keep, self._silent * WINDOW / SR, forced)
        self._speech, self._probs, self._silent = [], [], 0
        return out

    def _cut(self) -> list[Cut]:
        """The forced cut at max_speech_s, on the quietest window of the last cut_search_s (018-AC3).
        The windows after it open the next utterance: no audio is dropped or repeated."""
        n = len(self._probs)
        k = round(self.cut_search_s * SR / WINDOW)
        if k == 0:
            return self._emit(forced=True)
        # The latest of equal minima: steady speech keeps the hard cut.
        i = min(range(max(0, n - k), n), key=lambda j: (self._probs[j], -j))
        out = self._utterance(i + 1, (n - i - 1) * WINDOW / SR, True)
        self._speech, self._probs = self._speech[i + 1 :], self._probs[i + 1 :]
        self._start += i + 1
        self._silent = 0
        for p in self._probs:
            if p >= self.threshold:
                self._silent = 0
            elif p < self.neg_threshold:
                self._silent += 1
        return out

    def _utterance(self, keep: int, silent_s: float, forced: bool) -> list[Cut]:
        """The first keep windows of the speech as a Cut, unless too little of it is voiced."""
        voiced = [j for j, p in enumerate(self._probs[:keep]) if p >= self.threshold]
        if len(voiced) < self._windows(self.min_speech_ms):
            return []  # a click or a cough: Whisper turns these into invented sentences (S1-bis)
        first = self._start + voiced[0]
        if self._prev_end is None:
            gap: float | None = None
        elif first - self._prev_end < self._windows(self.min_silence_ms):
            # 018-AC1: only a forced cut leaves so short a pause: the speaker went on talking through it.
            gap = 0.0
        else:
            gap = (first - self._prev_end) * WINDOW / SR
        self._prev_end = self._start + voiced[-1] + 1
        trail = (keep - 1 - voiced[-1]) * WINDOW / SR
        after = ((self._seen - self._start - keep) * WINDOW + self._ahead) / SR
        return [Cut(np.concatenate(self._speech[:keep]), silent_s, gap, forced, trail, after)]
