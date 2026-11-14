"""The per-call transcription session and its NDJSON transcript (spec 044, phase 4)."""

import asyncio
import json
import os
import stat
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest
import structlog
from stt_helpers import pcm

from miniteams import cli
from miniteams import follow as F
from miniteams.config import Settings
from miniteams.stt import session as S
from miniteams.stt.line import Line
from miniteams.stt.vad import SR, Audio, Silero
from miniteams.stt.vad import Segmenter as Seg

CALL = "00000000-0000-4000-8000-000000000001"
THREAD = "19:meeting_synthetic@thread.v2"
T0 = datetime(2026, 11, 14, 10, 0, tzinfo=UTC).timestamp()
FRAME = SR // 50  # 20 ms, as the call's media delivers it
NAMES = {1: ("Speaker One", "8:orgid:00000000-0000-4000-8000-0000000000a1"), 2: ("Speaker Two", "")}


def loudness(windows: Audio) -> npt.NDArray[np.float32]:
    """The VAD stand-in of test_stt_vad: loud windows are speech, zeros silence."""
    return np.where(np.abs(windows).mean(axis=1) > 0.3, 0.9, 0.0).astype(np.float32)


def by_level(audio: Audio, _prompt: str) -> Line:
    """Names the tone that was spoken: each test speaker talks at its own level."""
    return Line(f"level {np.abs(audio).max():.1f}", "")


def tone(seconds: float, level: float) -> npt.NDArray[np.int16]:
    return np.full(int(seconds * SR), int(level * 32767), np.int16)


def silence(seconds: float) -> npt.NDArray[np.int16]:
    return np.zeros(int(seconds * SR), np.int16)


def frames(pcm16k: npt.NDArray[np.int16], t0: float) -> list[tuple[float, npt.NDArray[np.int16]]]:
    return [(t0 + i / SR, pcm16k[i : i + FRAME]) for i in range(0, len(pcm16k), FRAME)]


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def parse(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


@pytest.fixture
def now() -> list[float]:
    return [T0]


@pytest.fixture
def make(settings: Settings, tmp_path: Path, now: list[float]) -> Callable[..., S.Session]:
    def build(
        resolve: S.Resolve = NAMES.get,
        engine: Callable[[Audio, str], Line] = by_level,
        vad: Callable[[], Any] = lambda: loudness,
    ) -> S.Session:
        data = tmp_path / "data"
        # One fake clock for both: a frame arrives at its own t_wall, no lag (see the lag tests).
        clock = lambda: now[0]  # noqa: E731
        return S.Session(
            settings, data, CALL, THREAD, resolve, engines=[engine], vad=vad, clock=clock, monotonic=clock
        )

    return build


async def idle(session: S.Session) -> None:
    await asyncio.wait_for(session._pipeline.idle(), 10)


async def test_header_line_shape_modes_and_month_path(
    make: Callable[..., S.Session], tmp_path: Path, now: list[float]
) -> None:
    """044-AC8: <data-dir>/follow/<YYYY-MM>/<started>-<call>.ndjson, dir 0700 and file 0600; a header with
    call, thread, kind, started, engine; one object per utterance with t, end, speaker, mri, text, engine."""
    s = make()
    await s.start()
    for t, f in frames(np.concatenate([silence(0.5), tone(1.0, 0.5), silence(1.0)]), T0):
        now[0] = t
        s.feed(1, f, t)
    await s.close()
    path = tmp_path / "data" / "follow" / "2026-11" / f"20261114T100000Z-{CALL}.ndjson"
    assert s.transcript is not None and s.transcript.path == path
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    header, line = lines(path)
    assert header == {
        "call": CALL,
        "thread": THREAD,
        "kind": "meeting",
        "started": "2026-11-14T10:00:00.000Z",
        "engine": "parakeet",
    }
    assert set(line) == {"t", "end", "speaker", "mri", "text", "engine"}
    assert (line["speaker"], line["mri"], line["text"], line["engine"]) == (
        "Speaker One",
        NAMES[1][1],
        "level 0.5",
        "parakeet",
    )
    # Speech from 0.5 s to 1.5 s: start includes the 192 ms lead-in; the VAD decides per 32 ms window.
    assert line["t"].endswith("Z") and abs(parse(line["t"]) - (T0 + 0.5 - 0.192)) < 0.07
    assert abs(parse(line["end"]) - (T0 + 1.5)) < 0.07


async def test_another_process_reads_each_line_as_it_lands(
    make: Callable[..., S.Session], now: list[float]
) -> None:
    """044-AC8: no buffering: a reader opening the file mid-session sees the header and every finished
    line, then the next one once written."""
    s = make()
    await s.start()
    assert s.transcript is not None
    path = s.transcript.path
    assert len(lines(path)) == 1  # the header, before any speech
    for n in (1, 2):
        for t, f in frames(np.concatenate([tone(1.0, 0.5), silence(1.0)]), T0 + 2 * (n - 1)):
            now[0] = t
            s.feed(1, f, t)
        await idle(s)
        with open(path, encoding="utf-8") as reader:  # a fresh descriptor, as another process
            assert len(reader.read().splitlines()) == 1 + n
    await s.close()


async def test_only_text_is_written(make: Callable[..., S.Session], tmp_path: Path, now: list[float]) -> None:
    """044-AC12: after a call, the data dir holds the transcript and nothing else."""
    s = make()
    await s.start()
    for t, f in frames(np.concatenate([tone(1.0, 0.5), silence(1.0), tone(2.0, 0.5)]), T0):
        now[0] = t
        s.feed(1, f, t)
    await s.close()
    files = [p for p in (tmp_path / "data").rglob("*") if p.is_file()]
    assert s.transcript is not None and files == [s.transcript.path]
    assert len(lines(files[0])) == 3


async def test_overlapping_speakers_each_get_their_lines(
    make: Callable[..., S.Session], now: list[float]
) -> None:
    """044-AC6: speakers talking over each other, a fourth MSI appearing mid-call: each line names its
    own speaker from the resolver; an MSI it does not know falls back to its MSI, a name-less one to its
    MRI, a lookup that raises to the MSI."""
    mri3 = "8:orgid:00000000-0000-4000-8000-0000000000a3"

    def resolve(msi: int) -> tuple[str, str] | None:
        if msi == 4:
            raise KeyError(msi)
        return {**NAMES, 3: ("", mri3)}.get(msi)

    s = make(resolve=resolve)
    await s.start()
    tracks = {
        1: frames(np.concatenate([tone(2.0, 0.4), silence(1.2)]), T0),
        2: frames(np.concatenate([silence(0.5), tone(2.0, 0.6), silence(1.2)]), T0),
        3: frames(np.concatenate([silence(1.0), tone(1.0, 0.8), silence(1.2)]), T0 + 1.0),
        4: frames(np.concatenate([tone(1.0, 0.9), silence(1.2)]), T0 + 1.5),
        5: frames(np.concatenate([tone(1.0, 0.7), silence(1.2)]), T0 + 1.5),
    }
    # Interleaved on the wall clock, as the media delivers concurrent speakers.
    for t, msi, f in sorted((t, msi, f) for msi, fs in tracks.items() for t, f in fs):
        now[0] = t
        s.feed(msi, f, t)
    await s.close()
    assert s.transcript is not None
    got = sorted((x["speaker"], x["mri"], x["text"]) for x in lines(s.transcript.path)[1:])
    assert got == [
        (mri3, mri3, "level 0.8"),
        ("Speaker One", NAMES[1][1], "level 0.4"),
        ("Speaker Two", "", "level 0.6"),
        ("msi:4", "", "level 0.9"),
        ("msi:5", "", "level 0.7"),
    ]


async def test_a_track_that_goes_quiet_is_cut_without_another_frame(
    make: Callable[..., S.Session], now: list[float]
) -> None:
    """044-AC7: the media stops sending a speaker who stopped; the session feeds silence on the wall
    clock, so the line lands min_silence_ms later, not at the next word. The quiet track is dropped and
    a later frame of that MSI opens a new one."""
    s = make()
    await s.start()
    end = T0
    for t, f in frames(tone(1.0, 0.5), T0):
        now[0] = t
        s.feed(1, f, t)
        end = t + len(f) / SR
    s.tick(end + 0.5)
    await idle(s)
    assert s.transcript is not None and len(lines(s.transcript.path)) == 1
    s.tick(end + 1.0)
    await idle(s)
    (line,) = lines(s.transcript.path)[1:]
    assert abs(parse(line["end"]) - end) < 0.05
    s.tick(end + 1.3)
    assert 1 not in s._tracks
    # A gap inside feed: the next frame, 3 s later, first cuts what came before it.
    for t, f in frames(np.concatenate([tone(1.0, 0.6), silence(0.3)]), end + 5):
        s.feed(2, f, t)
    for t, f in frames(tone(1.0, 0.7), end + 8.3):
        s.feed(2, f, t)
    await idle(s)
    assert [x["text"] for x in lines(s.transcript.path)[1:]] == ["level 0.5", "level 0.6"]
    await s.close()
    assert [x["text"] for x in lines(s.transcript.path)[1:]][-1] == "level 0.7"


async def test_the_ticker_runs_on_its_own(
    make: Callable[..., S.Session], now: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(S, "TICK_S", 0.01)
    s = make()
    await s.start()
    for t, f in frames(tone(1.0, 0.5), T0):
        s.feed(1, f, t)
    now[0] = T0 + 3
    for _ in range(500):
        if s._tracks == {}:
            break
        await asyncio.sleep(0.01)
    assert s._tracks == {}
    await s.close()


async def test_quiet_tracks_reuse_their_vad_model(make: Callable[..., S.Session]) -> None:
    """A long call's speakers come and go: a dropped track's Silero is reset and reused, not reloaded."""
    built: list[Silero] = []

    def vad() -> Silero:
        built.append(Silero())
        return built[-1]

    s = make(vad=vad)
    await s.start()
    s.feed(1, silence(0.1), T0)
    s.tick(T0 + 3)
    s.feed(2, silence(0.1), T0 + 4)
    s.feed(3, silence(0.1), T0 + 4)
    assert len(built) == 2 and s._tracks[2].probs is built[0]
    await s.close()


def test_vad_settings_default_to_the_segmenter_ones(settings: Settings) -> None:
    """config cannot import stt.vad (numpy): its copies of the defaults must not drift."""
    assert (settings.vad_min_silence_ms, settings.vad_max_speech_s, settings.vad_cut_search_s) == (
        Seg.min_silence_ms,
        Seg.max_speech_s,
        Seg.cut_search_s,
    )
    with pytest.raises(ValueError, match="vad_cut_search_s"):
        Settings(tenant_id="t", vad_cut_search_s=15)


def test_default_session_runs_the_configured_engine_processes(settings: Settings, tmp_path: Path) -> None:
    """044-AC10: the chosen engine's model and library reach `python -P -m miniteams.stt.engines`."""
    settings.stt_engine_processes = 3
    settings.log_level = "DEBUG"  # the engine children log at the follower's level
    s = S.Session(settings, tmp_path, CALL, THREAD, NAMES.get)
    engines = s._pipeline._engines
    assert len(engines) == 3
    e = engines[0]
    assert isinstance(e, S.ProcessEngine)
    assert e._argv[1:4] == ["-P", "-m", "miniteams.stt.engines"]
    assert e._argv[4:] == ["parakeet", str(settings.stt_model), "2"]
    assert e._env == {"TRANSCRIBE_LIBRARY": str(settings.stt_library), "MINITEAMS_LOG_LEVEL": "DEBUG"}


async def test_behaviour_guard_piper_voices_through_the_session(
    make: Callable[..., S.Session], now: list[float]
) -> None:
    """044-AC6, AC8: three Piper voices on three MSIs, overlapping, fed as the follower will (20 ms frames,
    wall-clock times) through the real Silero VAD; each clip comes back as one line on its speaker, in
    its time span."""
    clips = {1: ("siwis-jargon.wav", 0.0), 2: ("joe-jargon.wav", 3.0), 3: ("cori-okay.wav", 5.0)}

    def by_length(audio: Audio, _prompt: str) -> Line:
        return Line(f"{len(audio) / SR:.0f}s", "")

    s = make(resolve=lambda m: (f"Voice {m}", ""), engine=by_length, vad=Silero)
    await s.start()
    feeds = []
    for msi, (name, at) in clips.items():
        feeds += [(t, msi, f) for t, f in frames(pcm(name), T0 + at)]
    for t, msi, f in sorted(feeds, key=lambda x: (x[0], x[1])):
        now[0] = t
        s.feed(msi, f, t)
    now[0] += 2
    s.tick(now[0])
    await s.close()
    assert s.transcript is not None
    got = lines(s.transcript.path)[1:]
    assert sorted(x["speaker"] for x in got) == ["Voice 1", "Voice 2", "Voice 3"]
    for x in got:
        name, at = clips[int(x["speaker"][-1])]
        start, end = T0 + at, T0 + at + len(pcm(name)) / SR
        assert start - 0.3 < parse(x["t"]) < parse(x["end"]) <= end + 0.1, (x, start, end)


# --- 044-AC10: follow refuses to start without its runtime ---


def runtime(tmp_path: Path, engine: str, *, lib: bool = True, model: bool = True) -> Settings:
    s = Settings(tenant_id="t", stt_engine=engine)
    s.cache_dir = tmp_path / "cache"
    if lib:
        s.stt_library.parent.mkdir(parents=True)
        s.stt_library.touch()
    if model:
        target = s.stt_model if engine == "parakeet" else s.stt_model / "model.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    return s


@pytest.mark.parametrize("engine", ["parakeet", "whisper"])
def test_stt_ready_names_what_is_missing_and_the_fix(
    engine: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC10"""
    assert F.stt_ready(runtime(tmp_path / "ok", engine)) is None
    s = runtime(tmp_path / "nomodel", engine, model=False)
    assert F.stt_ready(s) == f"{engine} model missing at {s.stt_model}: run `{build(s, engine)}`"
    s = runtime(tmp_path / "nolib", engine, lib=False)
    if engine == "parakeet":
        assert F.stt_ready(s) == f"libtranscribe missing at {s.stt_library}: run `{build(s, engine)}`"
    else:
        assert F.stt_ready(s) is None  # whisper needs no libtranscribe
    monkeypatch.setattr(F, "missing_extra", lambda: ["transcribe_cpp"])
    assert F.stt_ready(runtime(tmp_path / "noextra", engine)) == (
        "transcript extra missing (transcribe_cpp): "
        "uv sync --inexact --extra transcript, then uv run --extra transcript miniteams follow"
    )


def build(s: Settings, engine: str) -> str:
    """The command the hint gives: the repo's script by absolute path, the settings' cache dir."""
    script = Path(F.__file__).resolve().parents[2] / "scripts" / "build-transcribe.sh"
    assert script.is_file()
    return f"MINITEAMS_CACHE_DIR={s.cache_dir} {script} {engine}"


def test_stt_ready_explicit_paths_say_to_fix_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC10: a library or model the user pointed elsewhere is not rebuilt by the script: the hint names
    the setting; a cache dir with a space is quoted; outside a source checkout the script is named."""
    missing = tmp_path / "nowhere"
    s = Settings(tenant_id="t", transcribe_library=missing / "lib.so")
    fix = "point MINITEAMS_TRANSCRIBE_LIBRARY at an existing file"
    assert F.stt_ready(s) == f"libtranscribe missing at {missing / 'lib.so'}: {fix}"
    s = runtime(tmp_path / "p", "parakeet")
    s.parakeet_model = missing / "p.gguf"
    fix = "point MINITEAMS_PARAKEET_MODEL at an existing GGUF file"
    assert F.stt_ready(s) == f"parakeet model missing at {missing / 'p.gguf'}: {fix}"
    s = Settings(tenant_id="t", stt_engine="whisper", whisper_model=missing)
    assert F.stt_ready(s) == (
        f"whisper model missing at {missing}: point MINITEAMS_WHISPER_MODEL at a downloaded model directory"
    )
    s = Settings(tenant_id="t", stt_engine="whisper")
    s.cache_dir = tmp_path / "my cache"
    assert f"MINITEAMS_CACHE_DIR='{tmp_path}/my cache' " in (F.stt_ready(s) or "")
    monkeypatch.setattr(F, "__file__", str(tmp_path / "site-packages" / "miniteams" / "follow.py"))
    assert (F.stt_ready(s) or "").endswith(
        " scripts/build-transcribe.sh (in the miniteams repository) whisper`"
    )


def test_settings_paths_override_the_cache_defaults(tmp_path: Path) -> None:
    s = Settings(
        tenant_id="t",
        transcribe_library=tmp_path / "lib.so",
        parakeet_model=tmp_path / "p.gguf",
        whisper_model=tmp_path / "w",
    )
    assert (s.stt_library, s.stt_model) == (tmp_path / "lib.so", tmp_path / "p.gguf")
    s.stt_engine = "whisper"
    assert s.stt_model == tmp_path / "w"


@pytest.mark.parametrize(
    "argv", [["follow"], ["archive", "--live", "--follow"], ["follow", "--call", json.dumps({"x": 1})]]
)
def test_follow_refuses_to_start_without_the_model(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """044-AC10: the trigger and a follower both stop before Teams or the call, with the fix command."""
    from structlog.testing import capture_logs

    from miniteams import stream

    monkeypatch.setattr(cli, "setup_logging", lambda _level: None)
    # An earlier test's real setup_logging may have cached cli.log past capture_logs.
    monkeypatch.setattr(cli, "log", structlog.get_logger())
    monkeypatch.setattr(F, "missing_extra", list)
    monkeypatch.setattr(
        "miniteams.auth.token_source", lambda _s: type("T", (), {"acquire": lambda self: None})()
    )
    monkeypatch.setattr(stream, "run_forever", lambda *_a, **_k: pytest.fail("connected without a model"))
    monkeypatch.setattr(F, "run_call", lambda *_a: pytest.fail("joined a call it cannot transcribe"))
    with capture_logs() as logs:
        assert cli.main(argv) == 1
    (event,) = [e for e in logs if e["event"] == "follow_stt_missing"]
    assert event["engine"] == "parakeet"
    assert event["fix"].startswith("libtranscribe missing at ")
    assert event["fix"].endswith("/scripts/build-transcribe.sh parakeet`")
    assert os.environ["XDG_CACHE_HOME"] in event["fix"]  # the settings' own cache dir


def test_whisper_is_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    """044-AC10"""
    monkeypatch.setenv("MINITEAMS_STT_ENGINE", "whisper")
    assert Settings(tenant_id="t").stt_engine == "whisper"


# --- failure paths and clocks ---


class Recorder:
    """The loudness VAD, recording each window it was given: all-zero windows are fed silence."""

    def __init__(self) -> None:
        self.zero: list[bool] = []

    def __call__(self, windows: Audio) -> npt.NDArray[np.float32]:
        self.zero += [not w.any() for w in windows]
        return loudness(windows)


@pytest.mark.parametrize("lag", ["0.02", "0.15", "0.3", "jitter"])
async def test_late_frames_never_get_silence_inside_speech(
    lag: str, settings: Settings, tmp_path: Path
) -> None:
    """Frames reach the session after their t_wall (network, decode, a busy loop), the ticker running
    every 100 ms meanwhile: no silence is spliced into the speech, t and end stay within one VAD window
    of the truth, and once the speaker stops the line is cut within stt_quiet_s + the VAD's 800 ms."""
    rng = np.random.default_rng(7)
    rec = Recorder()
    mono = [0.0]
    cut_at: list[float] = []
    s = S.Session(
        settings, tmp_path, CALL, THREAD, NAMES.get, engines=[by_level], vad=lambda: rec,
        clock=lambda: T0 + 100, monotonic=lambda: mono[0],
    )  # fmt: skip
    real_submit = s._pipeline.submit
    s._pipeline.submit = lambda u: cut_at.append(mono[0]) or real_submit(u)  # type: ignore[method-assign]
    await s.start()
    speech = (T0 + 1.0, T0 + 4.0)
    arrival = 0.0
    next_tick = 0.0
    for t, f in frames(tone(3.0, 0.5), speech[0]):
        delay = float(rng.uniform(0.05, 0.35)) if lag == "jitter" else float(lag)
        arrival = max(arrival, t - T0 + delay)  # in order, as the media delivers them
        while next_tick <= arrival:
            mono[0] = next_tick
            s.tick(next_tick)
            next_tick += S.TICK_S
        mono[0] = arrival
        s.feed(1, f, t)
    last = arrival
    while next_tick <= last + 3:
        mono[0] = next_tick
        s.tick(next_tick)
        next_tick += S.TICK_S
    await idle(s)
    await s.close()
    assert s.transcript is not None
    (line,) = lines(s.transcript.path)[1:]
    window = 512 / SR
    assert abs(parse(line["t"]) - speech[0]) <= window  # the speech opens the track: no lead-in
    assert abs(parse(line["end"]) - speech[1]) <= window
    voiced = [i for i, z in enumerate(rec.zero) if not z]
    assert not any(rec.zero[voiced[0] : voiced[-1]]), "silence fed inside the speech"
    assert cut_at and cut_at[0] - last <= settings.stt_quiet_s + 0.8 + S.TICK_S + window


async def test_close_is_bounded_when_an_engine_never_comes_back(settings: Settings, tmp_path: Path) -> None:
    """044-AC5: an engine that died and fails every restart cannot hold close() past stt_close_timeout_s;
    the engines are stopped and the transcript closed all the same."""
    from structlog.testing import capture_logs
    from stt_fake_engine import DIE
    from stt_helpers import FAKE_ENGINE

    from miniteams.stt.engines import EngineDied, ProcessEngine
    from miniteams.stt.pipeline import Utterance

    settings.stt_close_timeout_s = 0.5
    e = ProcessEngine([sys.executable, FAKE_ENGINE], 30)

    def never(*_a: Any) -> None:
        raise EngineDied("model gone")

    s = S.Session(settings, tmp_path, CALL, THREAD, NAMES.get, engines=[e])
    await asyncio.wait_for(s.start(), 30)
    e.start = never  # type: ignore[method-assign]
    audio = np.zeros(16000, np.float32)
    audio[0] = DIE
    s._pipeline.submit(Utterance(1, audio, T0))
    s._pipeline.submit(Utterance(1, np.zeros(16000, np.float32), T0))
    await asyncio.sleep(0.3)  # the engine dies on the first, the restart loop begins
    began = time.monotonic()
    with capture_logs() as logs:
        await asyncio.wait_for(s.close(), 5)
    assert time.monotonic() - began < 1.5
    (timeout,) = [x for x in logs if x["event"] == "stt_close_timeout"]
    assert (timeout["pending"], timeout["busy"]) == (1, 1)
    assert not e.alive()
    assert s.transcript is not None
    with pytest.raises(OSError):
        os.fstat(s.transcript._fd)


async def test_a_failed_engine_load_stops_the_others_and_closes_the_transcript(
    settings: Settings, tmp_path: Path
) -> None:
    from stt_helpers import FAKE_ENGINE

    from miniteams.stt.engines import EngineDied, ProcessEngine

    good = ProcessEngine([sys.executable, FAKE_ENGINE], 30)
    bad = ProcessEngine([sys.executable, "-c", "import time, sys; time.sleep(0.3); sys.exit(3)"], 30)
    s = S.Session(settings, tmp_path, CALL, THREAD, NAMES.get, engines=[bad, good])
    with pytest.raises(EngineDied):
        await asyncio.wait_for(s.start(), 30)
    assert good.pid is None and not good.alive() and bad.pid is None
    assert s.transcript is not None
    with pytest.raises(OSError):
        os.fstat(s.transcript._fd)
    # A caller closing anyway, twice: the descriptor numbers others now hold are left alone.
    other = os.open(os.devnull, os.O_RDONLY)
    try:
        await s.close()
        await s.close()
        os.fstat(other)
    finally:
        os.close(other)


async def test_a_failing_tick_is_logged_and_the_ticker_goes_on(
    make: Callable[..., S.Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from structlog.testing import capture_logs

    monkeypatch.setattr(S, "TICK_S", 0.01)
    s = make()
    calls: list[float] = []

    def tick(now: float) -> None:
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("bad tick")

    s.tick = tick  # type: ignore[method-assign]
    with capture_logs() as logs:
        await s.start()
        for _ in range(500):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.01)
        await s.close()
    assert len(calls) >= 3
    assert [x["event"] for x in logs if x["log_level"] == "error"] == ["stt_tick_failed"]


async def test_feed_after_close_is_ignored(make: Callable[..., S.Session]) -> None:
    """A late frame from the media, after the call's session closed, opens no track and writes nothing."""
    s = make()
    await s.start()
    await s.close()
    s.feed(1, tone(1.0, 0.5), T0)
    assert s._tracks == {}


@pytest.mark.parametrize("search", [0.0, 1.0])
async def test_a_forced_cut_is_timed_from_the_audio_it_kept(
    search: float, make: Callable[..., S.Session], settings: Settings, now: list[float]
) -> None:
    """A cut at the cap that lands in a short pause: the hard cut (no search) keeps up to pad_ms of that
    pause and its end is the speech's; the searched one cuts on the pause's last window, so its end is
    the end of the audio the engine heard, inside the pause. Either way t is the speech's start."""
    settings.vad_max_speech_s, settings.vad_cut_search_s = 2.0, search
    s = make()
    await s.start()
    audio = np.concatenate([tone(1.75, 0.5), silence(0.4), tone(1.0, 0.7), silence(1.0)])
    for t, f in frames(audio, T0):
        now[0] = t
        s.feed(1, f, t)
    await s.close()
    assert s.transcript is not None
    first = lines(s.transcript.path)[1]
    window = 512 / SR
    assert first["text"] == "level 0.5"
    assert abs(parse(first["t"]) - T0) <= window  # the speech opens the track: no lead-in
    if search:
        assert T0 + 1.75 - window <= parse(first["end"]) <= T0 + 2.0 + window
    else:
        assert abs(parse(first["end"]) - (T0 + 1.75)) <= window


async def test_an_engine_child_exits_on_its_own_when_stopped() -> None:
    """An idle engine reads EOF and exits 0: no kill, nothing left for the resource tracker to warn of."""
    from stt_helpers import FAKE_ENGINE

    from miniteams.stt.engines import ProcessEngine

    e = ProcessEngine([sys.executable, FAKE_ENGINE], 30)
    await asyncio.to_thread(e.start, 30)
    proc = e._proc
    assert proc is not None
    await asyncio.to_thread(e.stop)
    assert proc.returncode == 0


@pytest.mark.parametrize("min_silence_ms", [800, 1500])
async def test_a_packet_gap_shorter_than_the_pause_setting_does_not_cut(
    min_silence_ms: int, make: Callable[..., S.Session], settings: Settings, now: list[float]
) -> None:
    """vad_min_silence_ms is honoured whatever its value: a speaker silent (no packets) for less than it
    stays one line, for more than it gets two. The silence fed covers the setting, not a fixed 1 s."""
    settings.vad_min_silence_ms = min_silence_ms
    pause = min_silence_ms / 1000
    for gap, want in ((pause - 0.3, 1), (pause + 0.4, 2)):
        s = make()
        await s.start()
        t = T0
        for t, f in frames(tone(1.0, 0.5), T0):
            now[0] = t
            s.feed(1, f, t)
        restart = t + FRAME / SR + gap
        while now[0] < restart:  # the ticker meanwhile, every 100 ms
            now[0] += S.TICK_S
            s.tick(now[0])
        for t, f in frames(tone(1.0, 0.5), restart):
            now[0] = t
            s.feed(1, f, t)
        await s.close()
        assert s.transcript is not None
        assert len(lines(s.transcript.path)) - 1 == want, (min_silence_ms, gap)


async def test_the_speaker_is_resolved_when_the_vad_cuts(
    make: Callable[..., S.Session], now: list[float]
) -> None:
    """044-AC6: the roster may drop a speaker who left before the engine answers: the line keeps the name
    known when their speech was cut."""
    roster = dict(NAMES)
    release = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow(audio: Audio, prompt: str) -> Line:
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(10)
        return by_level(audio, prompt)

    s = make(resolve=roster.get, engine=slow)
    await s.start()
    for t, f in frames(np.concatenate([tone(1.0, 0.5), silence(1.0)]), T0):
        now[0] = t
        s.feed(1, f, t)
    roster.clear()  # the speaker left the call
    release.set()
    await s.close()
    assert s.transcript is not None
    (line,) = lines(s.transcript.path)[1:]
    assert (line["speaker"], line["mri"]) == NAMES[1]


@pytest.mark.parametrize("frame_s", [1.0, 3.2])
async def test_long_frames_land_where_short_ones_do(
    frame_s: float, make: Callable[..., S.Session], now: list[float]
) -> None:
    """t and end do not depend on how much audio one feed() carries: 1 s or the whole call at once land
    within one VAD window of 20 ms frames."""
    audio = np.concatenate([silence(0.3), tone(1.0, 0.5), silence(1.0), tone(0.6, 0.7), silence(0.3)])

    async def run(step: int) -> list[tuple[float, float]]:
        s = make()
        await s.start()
        for i in range(0, len(audio), step):
            t = T0 + i / SR
            now[0] = t
            s.feed(1, audio[i : i + step], t)
        await s.close()
        assert s.transcript is not None
        return [(parse(x["t"]), parse(x["end"])) for x in lines(s.transcript.path)[1:]]

    short = await run(FRAME)
    long = await run(int(frame_s * SR))
    assert len(short) == len(long) == 2
    for (t0, e0), (t1, e1) in zip(short, long, strict=True):
        assert abs(t1 - t0) <= 512 / SR and abs(e1 - e0) <= 512 / SR
    assert abs(short[0][1] - (T0 + 1.3)) <= 512 / SR
