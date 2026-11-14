# Copied from grill-room `stt/tests/test_vad.py` @ ae133de (spec 044, AC16), modified: the two
# Silero tests on the FLEURS recording run on the Piper fixtures instead.
# Copyright grill-room authors. Licensed under the Apache License, Version 2.0:
# http://www.apache.org/licenses/LICENSE-2.0
import numpy as np
import numpy.typing as npt
import pytest
from stt_helpers import read

from miniteams.stt.vad import SR, WINDOW, Audio, Cut, Probs, Segmenter, Silero


def loudness(windows: Audio) -> npt.NDArray[np.float32]:
    """A stand-in model: loud windows are speech, quiet ones are between thresholds, zeros silence."""
    level = np.abs(windows).mean(axis=1)
    return np.where(level > 0.3, 0.9, np.where(level > 0.05, 0.4, 0.0)).astype(np.float32)


def tone(seconds: float, level: float = 0.5) -> Audio:
    return np.full(int(seconds * SR), level, np.float32)


def silence(seconds: float) -> Audio:
    return np.zeros(int(seconds * SR), np.float32)


def scripted(probs: list[float]) -> tuple[Probs, Audio]:
    """A model that gives each window its scripted probability: window i holds the value i."""
    table = np.asarray(probs, np.float32)
    audio = np.repeat(np.arange(len(probs), dtype=np.float32), WINDOW)
    return lambda w: table[w[:, 0].astype(int)], audio


def cuts(seg: Segmenter, audio: Audio, frame: int = 160) -> list[Cut]:
    out: list[Cut] = []
    for i in range(0, len(audio), frame):
        out += seg.feed_timed(audio[i : i + frame])
    return out + seg.flush_timed()


def window_ids(c: Cut) -> list[int]:
    return [int(v) for v in c.audio[::WINDOW]]


def feed(seg: Segmenter, audio: Audio, frame: int = 160) -> list[Audio]:
    """Feeds audio in LiveKit-sized frames, like a track does."""
    out: list[Audio] = []
    for i in range(0, len(audio), frame):
        out += seg.feed(audio[i : i + frame])
    return out


def test_cuts_on_a_pause() -> None:
    seg = Segmenter(loudness)
    out = feed(seg, np.concatenate([silence(1), tone(2), silence(1), tone(1), silence(0.9)]))
    assert len(out) == 2
    # Each utterance keeps pad_ms of lead-in and of trailing silence around its speech.
    assert abs(len(out[0]) / SR - 2.4) < 0.07
    assert abs(len(out[1]) / SR - 1.4) < 0.07


def test_frame_size_does_not_change_the_cuts() -> None:
    audio = np.concatenate([silence(0.5), tone(1.3), silence(0.9), tone(0.9), silence(0.9)])
    frames = (100, 160, 480, 512, 4000)
    shapes = {frame: [len(u) for u in feed(Segmenter(loudness), audio, frame)] for frame in frames}
    assert len(set(map(tuple, shapes.values()))) == 1, shapes


@pytest.mark.parametrize("pause", [0.3, 0.6])
def test_a_short_pause_does_not_cut(pause: float) -> None:
    """001-AC7: a pause between two sentences of one turn keeps them in one utterance. At 500 ms
    the S5 script's 18 lines became 32 utterances, each paying a full model pass."""
    out = feed(Segmenter(loudness), np.concatenate([tone(1), silence(pause), tone(1), silence(0.9)]))
    assert len(out) == 1


def test_between_thresholds_is_not_silence() -> None:
    # 0.4 is under threshold but over neg_threshold: a soft syllable must not end the utterance.
    out = feed(Segmenter(loudness), np.concatenate([tone(1), tone(1, 0.1), tone(1), silence(0.9)]))
    assert len(out) == 1
    assert len(out[0]) / SR > 3


def test_drops_blips_shorter_than_min_speech() -> None:
    assert feed(Segmenter(loudness), np.concatenate([tone(0.2), silence(1)])) == []
    assert len(feed(Segmenter(loudness), np.concatenate([tone(0.6), silence(1)]))) == 1


def test_cuts_long_speech_at_max() -> None:
    out = feed(Segmenter(loudness, max_speech_s=2), np.concatenate([tone(5), silence(0.9)]))
    assert [round(len(u) / SR) for u in out] == [2, 2, 1]


def test_flush_emits_speech_in_progress() -> None:
    seg = Segmenter(loudness)
    assert feed(seg, np.concatenate([silence(0.5), tone(1)])) == []
    out = seg.flush()
    assert len(out) == 1
    assert seg.flush() == []


CLIPS = ("siwis-jargon.wav", "joe-jargon.wav", "cori-okay.wav")


def speech() -> tuple[Audio, list[tuple[float, float]]]:
    """Three Piper clips, 1.5 s of silence after each, with each clip's (start, end) in seconds."""
    parts: list[Audio] = []
    spans: list[tuple[float, float]] = []
    at = 0.0
    for name in CLIPS:
        clip = read(name)
        parts += [clip, silence(1.5)]
        spans.append((at, at + len(clip) / SR))
        at += len(clip) / SR + 1.5
    return np.concatenate(parts), spans


def test_silero_cuts_the_fixture_after_each_sentence() -> None:
    """001-AC7: the real model, state carried across 10 ms frames, ends each utterance on its pause."""
    audio, spans = speech()
    seg = Segmenter(Silero())
    cuts: list[float] = []
    for i in range(0, len(audio), 160):
        cuts += [(i + 160) / SR for _ in seg.feed(audio[i : i + 160])]
    assert len(cuts) == len(spans)
    for cut, (start, end) in zip(cuts, spans, strict=True):
        # After the clip's speech, before the next clip starts.
        assert start + 0.5 < cut < end + 1.5, (cut, start, end)


def test_each_cut_says_how_long_ago_its_speech_ended() -> None:
    """A pause cut waited min_silence_ms after the speech; a forced cut at max_speech_s waited none,
    and a flush says how long the track was silent: the line's latency counts from there."""
    out = cuts(Segmenter(loudness), np.concatenate([tone(1), silence(0.9), tone(16)]))
    assert [round(c.silent_s, 2) for c in out] == [0.8, 0.0, 0.0]
    seg = Segmenter(loudness)
    seg.feed_timed(np.concatenate([tone(1), silence(0.3)]))
    (tail,) = seg.flush_timed()
    assert abs(tail.silent_s - 0.3) < 0.07  # two 32 ms windows


S, N = 0.9, 0.1


def test_gap_is_the_pause_between_two_utterances() -> None:
    """018-AC1: from the end of one utterance's speech to the start of the next's, to the window;
    none before the first."""
    probs, audio = scripted([S] * 20 + [N] * 30 + [S] * 20 + [N] * 30)
    out = cuts(Segmenter(probs), audio)
    assert [(c.gap_s, c.forced) for c in out] == [(None, False), (30 * WINDOW / SR, False)]
    out = cuts(Segmenter(loudness), np.concatenate([tone(1), silence(1.5), tone(1), silence(0.9)]))
    gap = out[1].gap_s
    assert gap is not None and abs(gap - 1.5) <= WINDOW / SR


def test_gap_is_zero_after_a_forced_cut() -> None:
    """018-AC1: a forced cut leaves the speaker mid-sentence: the next utterance follows with no gap."""
    for search in (0.0, 1.0):
        out = cuts(
            Segmenter(loudness, max_speech_s=2, cut_search_s=search), np.concatenate([tone(5), silence(0.9)])
        )
        assert [(c.gap_s, c.forced) for c in out] == [(None, True), (0.0, True), (0.0, False)], search


@pytest.mark.parametrize("search", [0.0, 3.0])
def test_a_long_pause_after_a_forced_cut_is_a_real_gap(search: float) -> None:
    """018-AC1: a cap reached in the speaker's pause is no continuation: the gap after it counts from
    the forced utterance's last voiced window, for the hard cut and for the quiet one."""
    out = cuts(Segmenter(loudness, cut_search_s=search), np.concatenate([tone(14.7), silence(10), tone(1)]))
    assert [c.forced for c in out] == [True, False]
    gap = out[1].gap_s
    assert gap is not None and abs(gap - 10) <= 2 * WINDOW / SR


def test_a_dropped_tail_after_a_forced_cut_leaves_the_real_gap() -> None:
    """018-AC1: the quiet cut leaves 5 voiced windows behind, too few to post; the next line's gap
    counts from the forced utterance's last voiced window, not 0."""
    probs, audio = scripted([S] * 45 + [0.0] + [S] * 5 + [N] * 111 + [S] * 20 + [N] * 30)
    out = cuts(Segmenter(probs, max_speech_s=2, cut_search_s=1), audio)
    assert [(window_ids(c)[0], c.forced, c.gap_s) for c in out] == [
        (0, True, None),
        (162 - 5, False, (162 - 45) * WINDOW / SR),
    ]


def test_the_rest_of_a_quiet_cut_keeps_its_trailing_silence() -> None:
    """018-AC3: the rest after the cut window already holds 10 silent windows: the pause cut that
    follows comes 15 windows later, not 25, as it would have without the forced cut."""
    probs, audio = scripted([S] * 36 + [0.0] + [S] * 15 + [N] * 50)
    seg = Segmenter(probs, max_speech_s=2, cut_search_s=1)
    at: list[tuple[int, Cut]] = []
    for i in range(0, len(audio), WINDOW):
        at += [(i // WINDOW, c) for c in seg.feed_timed(audio[i : i + WINDOW])]
    assert [(i, window_ids(c)[0], len(window_ids(c)), c.silent_s) for i, c in at] == [
        (61, 0, 37, 25 * WINDOW / SR),
        (76, 37, 15 + 6, 0.8),
    ]


def test_a_flush_forgets_the_audio_before_it() -> None:
    """018-AC1: after a flush (track end, mute) the next utterance holds only audio fed after it: no
    pre-roll and no partial window from before."""
    probs, audio = scripted([N] * 10 + [S] * 20 + [N] * 30)
    seg = Segmenter(probs)
    assert seg.feed_timed(audio[: 9 * WINDOW + 100]) == [] and seg.flush_timed() == []
    assert [window_ids(c) for c in cuts(seg, audio[10 * WINDOW :])] == [list(range(10, 36))]


def test_forget_gap_leaves_the_next_utterance_without_one() -> None:
    """018-AC1: after a mute the pause is unknown: no gap, even for the utterance in progress, whose
    audio stays whole."""
    probs, audio = scripted([S] * 20 + [N] * 30 + [S] * 20 + [N] * 30)
    seg = Segmenter(probs)
    first = seg.feed_timed(audio[: 60 * WINDOW])
    seg.forget_gap()
    second = seg.feed_timed(audio[60 * WINDOW :])
    assert [(window_ids(c), c.gap_s) for c in first + second] == [
        (list(range(26)), None),
        (list(range(45, 76)), None),
    ]


def test_no_gap_after_the_track_ends() -> None:
    """018-AC1: the first utterance after a resume has no gap; the worker flushes, or starts afresh."""
    seg = Segmenter(loudness)
    assert len(cuts(seg, np.concatenate([tone(1), silence(0.2)]))) == 1
    (c,) = cuts(seg, np.concatenate([silence(0.5), tone(1), silence(0.9)]))
    assert c.gap_s is None


# A 2 s cap (62 windows) and a 1 s search (31): the cut at the cap looks at windows 31 to 61.
DIPS = [S] * 20 + [N] + [S] * 24 + [0.2] + [S] * 4 + [0.3] + [S] * 30 + [N] * 40


def test_the_forced_cut_lands_on_the_quietest_window_of_the_search() -> None:
    """018-AC3: the cut at the cap lands on the lowest probability of the last cut_search_s, not on
    the deeper dip before it; the windows after it open the next utterance, none lost or repeated."""
    probs, audio = scripted(DIPS)
    out = cuts(Segmenter(probs, max_speech_s=2, cut_search_s=1), audio)
    assert [(c.gap_s, c.forced) for c in out] == [(None, True), (0.0, False)]
    first, second = (window_ids(c) for c in out)
    assert first == list(range(46))
    assert out[0].silent_s == (61 - 45) * WINDOW / SR
    # The second keeps pad_ms of its trailing silence, as any pause cut.
    assert first + second == list(range(81 + 6))


def test_a_float32_probability_at_a_threshold_is_on_it() -> None:
    """The model's 0.35 is not below neg_threshold 0.35: the device port compares the same way."""
    probs, audio = scripted([N] * 4 + [0.5] * 14 + [0.35] * 30 + [0.3499] * 25)
    assert [window_ids(c) for c in cuts(Segmenter(probs), audio)] == [list(range(54))]


def test_no_cut_search_is_the_hard_cut() -> None:
    """018-AC3: cut_search_s 0 cuts at the cap, as before 018."""
    probs, audio = scripted(DIPS)
    out = cuts(Segmenter(probs, max_speech_s=2, cut_search_s=0), audio)
    assert [window_ids(c) for c in out] == [list(range(62)), list(range(62, 81 + 6))]
    assert [c.silent_s for c in out] == [0.0, 0.8]


def test_a_reset_silero_judges_as_a_new_one() -> None:
    """010-AC15: a track resumed after a device flag starts from no memory of the audio before it."""
    audio = read("siwis-jargon.wav")
    start = int(1.0 * SR)
    windows = audio[start : start + 20 * WINDOW].reshape(20, WINDOW)
    assert windows[-1, -64:].any()
    heard = Silero()
    heard(windows)
    fresh = Silero()(windows)
    assert not np.allclose(heard(windows), fresh)
    heard(windows)
    heard.reset()
    # The 64-sample context barely moves the output: check every piece of state is gone.
    assert not (heard._h.any() or heard._c.any() or heard._context.any())
    np.testing.assert_allclose(heard(windows), fresh)


# --- spec 044: where a cut sits on the timeline (Cut.trail_s, Cut.end_ago_s) ---

W = WINDOW / SR


def cuts_at(seg: Segmenter, audio: Audio, frame: int = 160) -> list[tuple[int, Cut]]:
    """Each cut with the number of samples fed when it came out."""
    out: list[tuple[int, Cut]] = []
    for i in range(0, len(audio), frame):
        out += [(min(i + frame, len(audio)), c) for c in seg.feed_timed(audio[i : i + frame])]
    return out


def test_a_pause_cut_keeps_its_pad_as_trail() -> None:
    """The pause cut keeps pad_ms (6 windows) of its pause; its audio ends 25 windows of silence before
    the samples fed, the held-back ones included."""
    probs, audio = scripted([S] * 20 + [N] * 40)
    ((fed, c),) = cuts_at(Segmenter(probs), audio, frame=100)
    assert c.trail_s == 6 * W
    assert len(c.audio) == 26 * WINDOW
    # 20 voiced + 6 kept; the 25th silent window cut it, then fed samples not yet windowed.
    assert c.end_ago_s == ((25 - 6) * WINDOW + fed - 45 * WINDOW) / SR


def test_a_searched_forced_cut_keeps_the_pause_it_landed_in() -> None:
    """018-AC3's quiet cut on the last of equal minima: the 4 silent windows before it stay with it."""
    probs, audio = scripted([S] * 50 + [0.0] * 5 + [S] * 30 + [N] * 30)
    (fed, first), *_ = cuts_at(Segmenter(probs, max_speech_s=2, cut_search_s=1), audio, frame=WINDOW)
    assert first.forced and len(first.audio) == 55 * WINDOW
    assert first.trail_s == 5 * W
    assert first.end_ago_s == (fed - 55 * WINDOW) / SR


def test_a_hard_forced_cut_keeps_up_to_pad_of_a_pause_at_the_cap() -> None:
    probs, audio = scripted([S] * 58 + [N] * 4 + [S] * 10 + [N] * 30)
    (fed, first), *_ = cuts_at(Segmenter(probs, max_speech_s=2, cut_search_s=0), audio, frame=WINDOW)
    assert first.forced and first.trail_s == 4 * W and first.end_ago_s == (fed - 62 * WINDOW) / SR


def test_a_flush_keeps_the_silence_heard_so_far() -> None:
    probs, audio = scripted([S] * 20 + [N] * 3)
    seg = Segmenter(probs)
    assert seg.feed_timed(np.concatenate([audio, np.full(100, 22, np.float32)])) == []
    (c,) = seg.flush_timed()
    assert c.trail_s == 3 * W and c.end_ago_s == 100 / SR


def placed(seg: Segmenter, audio: Audio, frame: int) -> list[tuple[float, float, bool]]:
    """Each cut's audio end on the fed timeline (s), its trail, whether forced, flush included."""
    out = [(fed / SR - c.end_ago_s, c.trail_s, c.forced) for fed, c in cuts_at(seg, audio, frame)]
    return out + [(len(audio) / SR - c.end_ago_s, c.trail_s, c.forced) for c in seg.flush_timed()]


def test_a_cut_is_placed_alike_whatever_the_frame_size() -> None:
    """A batch of several windows judged at once: the windows after the cut in that batch count as
    heard after it, so a pause cut and a forced cut in one chunk land where 10 ms frames put them."""
    probs, audio = scripted([N] * 5 + [S] * 20 + [N] * 30 + [S] * 70 + [N] * 30 + [S] * 15 + [N] * 4)
    seg = lambda: Segmenter(probs, max_speech_s=2, cut_search_s=1)  # noqa: E731
    small = placed(seg(), audio, 160)
    assert [f for _, _, f in small] == [False, True, False, False]
    for frame in (3 * WINDOW, len(audio)):
        got = placed(seg(), audio, frame)
        assert len(got) == len(small)
        for (end, trail, forced), (end0, trail0, forced0) in zip(got, small, strict=True):
            assert (trail, forced) == (trail0, forced0) and abs(end - end0) < 1e-9, frame
