# Copied from grill-room `stt/grill_stt/pipeline.py` @ ae133de (spec 044, AC16), trimmed: no room API,
# heartbeat or Prometheus metrics; lines go to a sink (the transcript writer).
# Copyright grill-room authors. Licensed under the Apache License, Version 2.0:
# http://www.apache.org/licenses/LICENSE-2.0
"""The bounded queue between the audio tracks and the model, and the sink it hands lines to."""

import asyncio
import re
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import structlog

from .engines import STOP_GRACE_S, EngineDied, ProcessEngine, Transcribe
from .line import Line
from .vad import SR, Audio

log = structlog.get_logger()

# Queue wait that logs a warning.
SLOW_WAIT_S = 10.0

# The script each language is written in, by the first word of its letters' Unicode names; any
# language not listed is written in Latin letters.
SCRIPTS = {
    "zh": {"CJK"},
    "yue": {"CJK"},
    "ja": {"CJK", "HIRAGANA", "KATAKANA"},
    "ko": {"HANGUL"},
    "ru": {"CYRILLIC"},
    "uk": {"CYRILLIC"},
    "be": {"CYRILLIC"},
    "bg": {"CYRILLIC"},
    "mk": {"CYRILLIC"},
    "sr": {"CYRILLIC", "LATIN"},
    "kk": {"CYRILLIC"},
    "mn": {"CYRILLIC"},
    "tt": {"CYRILLIC"},
    "ba": {"CYRILLIC"},
    "tg": {"CYRILLIC"},
    "el": {"GREEK"},
    "he": {"HEBREW"},
    "yi": {"HEBREW"},
    "ar": {"ARABIC"},
    "fa": {"ARABIC"},
    "ur": {"ARABIC"},
    "ps": {"ARABIC"},
    "sd": {"ARABIC"},
    "hi": {"DEVANAGARI"},
    "mr": {"DEVANAGARI"},
    "ne": {"DEVANAGARI"},
    "sa": {"DEVANAGARI"},
    "bn": {"BENGALI"},
    "as": {"BENGALI"},
    "pa": {"GURMUKHI"},
    "gu": {"GUJARATI"},
    "ta": {"TAMIL"},
    "te": {"TELUGU"},
    "kn": {"KANNADA"},
    "ml": {"MALAYALAM"},
    "si": {"SINHALA"},
    "th": {"THAI"},
    "lo": {"LAO"},
    "my": {"MYANMAR"},
    "km": {"KHMER"},
    "bo": {"TIBETAN"},
    "ka": {"GEORGIAN"},
    "hy": {"ARMENIAN"},
    "am": {"ETHIOPIC"},
}


@dataclass(frozen=True)
class Plausibility:
    """Thresholds past which a transcript is dropped rather than posted (spike S5, problem 6)."""

    # Whisper's own failure thresholds: mean token log-probability, and gzip ratio (repetition).
    min_avg_logprob: float = -1.0
    max_compression_ratio: float = 2.4
    # Whisper keeps a likely-silent segment it decoded confidently: only an unsure language too
    # makes it noise.
    max_no_speech_prob: float = 0.6
    # An unsure language on a long utterance is babble: real speech that long is detected at 0.8 and
    # up (S5). A short "oui" often comes out as English at 0.3, so short utterances are spared.
    min_language_probability: float = 0.5
    short_utterance_s: float = 2.0
    # Share of a line's letters that must be in the detected language's script.
    min_script_share: float = 0.9


def script_share(text: str, language: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 1.0
    want = SCRIPTS.get(language, {"LATIN"})
    return sum(unicodedata.name(c, "?").split(" ")[0] in want for c in letters) / len(letters)


def implausible(line: Line, seconds: float, p: Plausibility) -> str | None:
    """The first check a transcript of seconds of audio fails, or None when it is fit to post."""
    if line.avg_logprob < p.min_avg_logprob:
        return "avg_logprob"
    if line.compression_ratio > p.max_compression_ratio:
        return "repetition"
    unsure = line.language_probability < p.min_language_probability
    if line.no_speech_prob > p.max_no_speech_prob and unsure:
        return "no_speech"
    if unsure and seconds >= p.short_utterance_s:
        return "language_probability"
    if script_share(line.text, line.language) < p.min_script_share:
        return "script"
    return None


def implausible_parakeet(line: Line, _seconds: float, p: Plausibility) -> str | None:
    """As implausible, without the checks on Whisper's confidence and language (spec 007, AC3)."""
    if line.compression_ratio > p.max_compression_ratio:
        return "repetition"
    # French and English both use Latin letters: anything else means the engine went astray.
    if script_share(line.text, "") < p.min_script_share:
        return "script"
    return None


# Words frequent in one language and absent from the other's: "a", "on", "en" or "pour" would not do.
FRENCH = frozenset(
    "le la les un une des du et est sont que qui pas ne je tu il elle nous vous ils elles ce cette ces dans "
    "avec sur au aux mais ou donc oui non très aussi bien fait faire être avoir mon ma mes ton ta son sa "
    "leur notre votre quoi comme alors parce puis voilà accord".split()
)
ENGLISH = frozenset(
    "the and is are was were of to that it this for with not you we they be have has do does but or so "
    "yes no very what which there their our your my his her will would can could should just about "
    "because then okay".split()
)


def guess_language(text: str) -> str:
    """fr, en or unknown, by function words; it feeds logs only (spec 007, AC4)."""
    words = re.findall(r"[^\W\d_]+", text.lower())
    fr = sum(w in FRENCH for w in words)
    en = sum(w in ENGLISH for w in words)
    if fr == en:
        return "unknown"
    return "fr" if fr > en else "en"


@dataclass
class Utterance:
    # The speaker's track: the media source id (MSI) of the call's audio.
    msi: int
    audio: Audio
    # Wall clock (epoch s) when the speaker stopped; the VAD sees it min_silence_ms later.
    ended: float
    # Wall clock when the utterance began, the VAD's 200 ms lead-in included.
    started: float = 0.0
    # When the VAD cut it, on the pipeline's clock: the start of its queue wait.
    queued: float = field(default=0.0, compare=False)
    # The pause before it on its track, and whether the VAD forced its cut (vad.Cut).
    gap_s: float | None = None
    forced: bool = False
    # The speaker as resolved when the VAD cut the utterance.
    name: str = ""
    mri: str = ""

    @property
    def speaker(self) -> int:
        return self.msi


Sink = Callable[[Utterance, Line], None]


class Pipeline:
    """One bounded queue drained by one task per engine process (spec 007, AC6-AC8).

    An utterance waits while its speaker's previous one is in an engine, so a speaker's lines reach
    the sink in speech order; other speakers' utterances pass it.
    """

    def __init__(
        self,
        engines: Sequence[Transcribe],
        sink: Sink,
        size: int,
        rules: Plausibility | None = None,
        engine: str = "whisper",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._engines = list(engines)
        self._sink = sink
        self._size = size
        self._engine = engine
        self._judge = implausible_parakeet if engine == "parakeet" else implausible
        self._clock = clock
        self._rules = rules or Plausibility()
        self._pending: deque[Utterance] = deque()
        self._busy: set[int] = set()
        # Set on every submit and every finished utterance: idle engine tasks look again.
        self._wake = asyncio.Event()

    def submit(self, u: Utterance) -> bool:
        if len(self._pending) >= self._size:
            log.warning("utterance_dropped", msi=u.msi, queue=len(self._pending))
            return False
        u.queued = self._clock()
        self._pending.append(u)
        self._wake.set()
        return True

    async def start(self) -> None:
        """Loads every engine process at once; if one fails, stops them all once each load is done."""
        loads = await asyncio.gather(
            *(asyncio.to_thread(e.start) for e in self._engines if isinstance(e, ProcessEngine)),
            return_exceptions=True,
        )
        if errors := [e for e in loads if isinstance(e, BaseException)]:
            self.stop()
            raise errors[0]

    def load(self) -> tuple[int, int]:
        """Utterances queued, and speakers in an engine."""
        return len(self._pending), len(self._busy)

    def stop(self, grace_s: float = STOP_GRACE_S) -> None:
        for e in self._engines:
            if isinstance(e, ProcessEngine):
                e.stop(grace_s)

    async def idle(self) -> None:
        """Returns once nothing is queued or in an engine."""
        while self._pending or self._busy:
            self._wake.clear()
            await self._wake.wait()

    async def _take(self) -> Utterance:
        while True:
            for i, u in enumerate(self._pending):
                if u.speaker not in self._busy:
                    del self._pending[i]
                    self._busy.add(u.speaker)
                    return u
            self._wake.clear()
            await self._wake.wait()

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tg:
            for e in self._engines:
                tg.create_task(self._serve(e))

    async def _serve(self, engine: Transcribe) -> None:
        while True:
            u = await self._take()
            wait = self._clock() - u.queued
            if wait > SLOW_WAIT_S:
                log.warning("queue_wait_slow", msi=u.msi, wait_s=round(wait, 1))
            try:
                if isinstance(engine, ProcessEngine) and not engine.alive():
                    # Died while idle: restart it before it costs this utterance.
                    log.error("engine_died", pid=engine.pid, idle=True)
                    await self._restart(engine)
                await self.process(u, engine)
            except EngineDied as e:
                log.error("engine_died", msi=u.msi, error=str(e))
                await self._restart(engine)
            except Exception:
                # One utterance the model chokes on must not stop transcription for the call.
                log.exception("utterance_failed", msi=u.msi)
            finally:
                self._busy.discard(u.speaker)
                self._wake.set()

    async def _restart(self, engine: Transcribe) -> None:
        if not isinstance(engine, ProcessEngine):
            return
        delay = 1.0
        while True:
            try:
                await asyncio.to_thread(engine.start)
                log.info("engine_restarted", pid=engine.pid)
                return
            except Exception as e:  # a model that fails to load again: back off, the other engine serves
                log.error("engine_restart_failed", error=str(e), retry_s=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    async def process(self, u: Utterance, engine: Transcribe | None = None) -> None:
        engine = engine or self._engines[0]
        line = await asyncio.to_thread(engine, u.audio, "")
        seconds = round(len(u.audio) / SR, 2)
        u.audio = u.audio[:0]  # the samples are no longer needed: drop them now, not at the next utterance
        if not line.text.strip():
            log.info("utterance_empty", msi=u.msi, seconds=seconds, detected=line.language)
            return
        reason = self._judge(line, seconds, self._rules)
        if reason:
            log.info(
                "line_implausible",
                msi=u.msi,
                reason=reason,
                seconds=seconds,
                detected=line.language,
                language_probability=round(line.language_probability, 2),
                avg_logprob=round(line.avg_logprob, 2),
                no_speech_prob=round(line.no_speech_prob, 2),
                compression_ratio=round(line.compression_ratio, 2),
                chars=len(line.text),
            )
            return
        self._sink(u, line)
        # The text is personal data: logs carry its size and language, never the words.
        log.info(
            "line_done",
            msi=u.msi,
            seconds=seconds,
            language=guess_language(line.text),
            # Whisper's own label; parakeet gives none.
            detected=line.language,
            chars=len(line.text),
            latency_s=round(self._clock() - u.ended, 2),
            gap_s=u.gap_s,
            forced=u.forced,
        )
