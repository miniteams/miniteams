"""One call's transcription: per-speaker VAD tracks into the engine pipeline, lines into the transcript.

The follower feeds 16 kHz PCM per speaker MSI with the wall-clock time of each frame. Two clocks:
t_wall places audio on the call's timeline (line timestamps, holes between frames); the monotonic time
a frame arrives says whether a speaker went quiet. A track that receives nothing for stt_quiet_s gets
silence fed on its behalf, so its last utterance is cut without waiting for its next word; delivery
lag and jitter below that never put silence inside speech. Only text is written (044-AC12): the audio
lives in memory until its line is done.
"""

import asyncio
import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import structlog

from ..config import Settings
from ..follow import follow_dir, transcript_path
from .engines import STOP_GRACE_S, ProcessEngine, Transcribe, argv
from .line import Line
from .pipeline import Pipeline, Utterance
from .vad import SR, WINDOW, Cut, Probs, Segmenter, Silero

log = structlog.get_logger()

# A hole this long between a frame's t_wall and the end of the previous one is a pause on the call's
# timeline (lost packets, a muted speaker): it is fed as silence. Shorter ones are jitter.
FRAME_SLACK_S = 0.1
# Silence fed into a quiet track: vad_min_silence_ms plus this, enough for the VAD to cut on whole
# windows. Then the track is flushed and dropped.
GAP_MARGIN_S = 0.2
TICK_S = 0.1

# msi → (display name, mri); None when the MSI is unknown.
Resolve = Callable[[int], tuple[str, str] | None]


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def transcript_header(
    call: str, thread: str, kind: str, started: float, engine: str, **extra: Any
) -> dict[str, Any]:
    """The transcript's first line (044-AC8), plus what the follower adds (its join outcome)."""
    return {
        "call": call,
        "thread": thread,
        "kind": kind,
        "started": iso(started),
        "engine": engine,
        **extra,
    }


class Transcript:
    """transcript_path's file: a header, then one line per utterance, each in one write() so another
    process reads whole lines as soon as they land (044-AC8)."""

    def __init__(self, data_dir: Path, call: str, started: float, header: dict[str, Any]) -> None:
        follow_dir(data_dir)
        self.path = transcript_path(data_dir, call, started)
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._torn = False  # the last write may have left part of a line: the next one starts afresh
        self.write(header)

    def write(self, obj: dict[str, Any]) -> None:
        data = memoryview(
            (("\n" if self._torn else "") + json.dumps(obj, ensure_ascii=False) + "\n").encode()
        )
        self._torn = True
        while data:  # a full disk or a file size limit writes part of it, then raises
            data = data[os.write(self._fd, data) :]
        self._torn = False

    def close(self) -> None:
        # Once only: a second os.close could close an fd number another file got meanwhile.
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


@dataclass
class _Track:
    seg: Segmenter
    probs: Probs
    until: float  # call timeline (wall clock) at the end of the last sample fed; never moves back
    heard: float  # monotonic time the last real frame arrived
    filled: float = 0.0  # silence fed since the last real frame


class Session:
    """start(), then feed() from the event loop for each speaker's frames, then close()."""

    def __init__(
        self,
        settings: Settings,
        data_dir: Path,
        call: str,
        thread: str,
        resolve: Resolve,
        *,
        kind: str,
        engines: Sequence[Transcribe] | None = None,
        vad: Callable[[], Probs] = Silero,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        header: dict[str, Any] | None = None,
    ) -> None:
        self._settings = settings
        self._extra = header or {}  # added to the transcript header, e.g. the join outcome
        self._data_dir = data_dir
        self._call = call
        self._thread = thread
        self._kind = kind
        self._resolve = resolve
        self._engine = settings.stt_engine
        self._vad = vad
        self._clock = clock
        self._monotonic = monotonic
        self._gap_fill = settings.vad_min_silence_ms / 1000 + GAP_MARGIN_S
        self._closed = False
        if engines is None:
            # The binding reads TRANSCRIBE_LIBRARY when imported, in the child.
            env = {"TRANSCRIBE_LIBRARY": str(settings.stt_library), "MINITEAMS_LOG_LEVEL": settings.log_level}
            cmd = argv(self._engine, str(settings.stt_model), settings.stt_cpu_threads)
            engines = [
                ProcessEngine(cmd, settings.stt_engine_timeout_s, env)
                for _ in range(settings.stt_engine_processes)
            ]
        self._pipeline = Pipeline(
            engines, self._write, settings.stt_queue_size, engine=self._engine, clock=clock
        )
        self._tracks: dict[int, _Track] = {}
        # VAD models of dropped tracks, reset: a call's speakers come and go, its models are reused.
        self._spare: list[Probs] = []
        self._tasks: list[asyncio.Task[None]] = []
        self.transcript: Transcript | None = None

    async def start(self) -> None:
        await self.load()
        self.begin()

    async def load(self) -> None:
        """Loads the engines; no transcript yet (the follower runs this while it joins)."""
        await self._pipeline.start()

    def begin(self) -> None:
        """Opens the transcript and starts transcribing; frames are fed from now on."""
        started = self._clock()
        header = transcript_header(self._call, self._thread, self._kind, started, self._engine, **self._extra)
        self.transcript = Transcript(self._data_dir, self._call, started, header)
        self._tasks = [asyncio.create_task(self._pipeline.run()), asyncio.create_task(self._ticker())]
        log.info("stt_started", call=self._call, engine=self._engine, path=str(self.transcript.path))

    def feed(self, msi: int, pcm16k: npt.NDArray[np.int16], t_wall: float) -> None:
        """One frame of `msi`'s track; t_wall is the wall clock (epoch s) of its first sample."""
        if self._closed:
            log.debug("stt_feed_after_close", msi=msi)
            return
        audio = pcm16k.astype(np.float32) / np.float32(32768)
        track = self._tracks.get(msi)
        if track is not None and t_wall - track.until > FRAME_SLACK_S:
            self._silence(msi, track, track.until + min(t_wall - track.until, self._gap_fill))
            track = self._tracks.get(msi)
        if track is None:
            probs = self._spare.pop() if self._spare else self._vad()
            s = self._settings
            seg = Segmenter(
                probs,
                min_silence_ms=s.vad_min_silence_ms,
                max_speech_s=s.vad_max_speech_s,
                cut_search_s=s.vad_cut_search_s,
            )
            track = self._tracks[msi] = _Track(seg, probs, t_wall, 0.0)
        track.heard = self._monotonic()
        track.filled = 0.0
        # A frame placed before the end of what was fed (lag, or silence fed meanwhile) follows it.
        track.until = max(t_wall, track.until) + len(audio) / SR
        self._submit(msi, track, track.seg.feed_timed(audio), track.until)

    def tick(self, now: float) -> None:
        """Feeds silence to the tracks that received nothing for stt_quiet_s (now: monotonic), as much
        silence as time went by since their last frame."""
        for msi, track in list(self._tracks.items()):
            quiet = now - track.heard
            if quiet >= self._settings.stt_quiet_s:
                self._silence(msi, track, track.until + min(quiet, self._gap_fill) - track.filled)

    def _silence(self, msi: int, track: _Track, upto: float) -> None:
        # Window by window: a cut's end is placed within one window of the real pause.
        zeros = np.zeros(WINDOW, np.float32)
        while track.until < upto and track.filled < self._gap_fill:
            track.until += WINDOW / SR
            track.filled += WINDOW / SR
            self._submit(msi, track, track.seg.feed_timed(zeros), track.until)
        if track.filled >= self._gap_fill:
            self._drop(msi, track)

    def _drop(self, msi: int, track: _Track) -> None:
        self._submit(msi, track, track.seg.flush_timed(), track.until)
        del self._tracks[msi]
        if isinstance(track.probs, Silero):
            track.probs.reset()
            self._spare.append(track.probs)

    def _submit(self, msi: int, track: _Track, cuts: list[Cut], end: float) -> None:
        """end: the call timeline at the end of what was fed to the track's VAD."""
        for c in cuts:
            audio_end = end - c.end_ago_s
            name, mri = self._speaker(msi)  # now: the roster may forget a speaker before the engine answers
            self._pipeline.submit(
                Utterance(
                    msi,
                    c.audio,
                    ended=audio_end - c.trail_s,
                    started=audio_end - len(c.audio) / SR,
                    gap_s=c.gap_s,
                    forced=c.forced,
                    name=name,
                    mri=mri,
                )
            )

    def _speaker(self, msi: int) -> tuple[str, str]:
        try:
            name, mri = self._resolve(msi) or ("", "")
        except Exception:  # a broken lookup must not cost the line
            log.exception("speaker_resolve_failed", msi=msi)
            name, mri = "", ""
        return name or mri or f"msi:{msi}", mri

    def _write(self, u: Utterance, line: Line) -> None:
        assert self.transcript is not None
        self.transcript.write(
            {
                "t": iso(u.started),
                "end": iso(u.ended),
                "speaker": u.name,
                "mri": u.mri,
                "text": line.text,
                "engine": self._engine,
            }
        )

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                self.tick(self._monotonic())
            except Exception:  # one bad tick must not leave quiet speakers uncut for the rest of the call
                log.exception("stt_tick_failed", call=self._call)

    async def close(self) -> None:
        """Cuts every track's speech in progress, waits up to stt_close_timeout_s for its lines, stops
        the engines."""
        self._closed = True
        for msi, track in list(self._tracks.items()):
            self._drop(msi, track)
        grace = STOP_GRACE_S
        try:
            async with asyncio.timeout(self._settings.stt_close_timeout_s):
                await self._pipeline.idle()
        except TimeoutError:
            pending, busy = self._pipeline.load()
            log.warning("stt_close_timeout", call=self._call, pending=pending, busy=busy)
            grace = 0.0  # the engines are busy or hung: waiting for them to read EOF is time lost
        finally:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._pipeline.stop(grace)
            if self.transcript is not None:
                self.transcript.close()
            log.info("stt_closed", call=self._call)
