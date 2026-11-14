# Copied from grill-room `stt/tests/test_plausibility.py` @ 682b106 (spec 044, AC16), trimmed: its two
# model tests moved to test_stt_real.py.
# Copyright grill-room authors. Licensed under the Apache License, Version 2.0:
# http://www.apache.org/licenses/LICENSE-2.0

import pytest

from miniteams.stt.line import Line
from miniteams.stt.pipeline import Plausibility, implausible

RULES = Plausibility()
BABBLE = "三二八四八四八四八二八四八三二八四八八"


def line(text: str, language: str = "fr", **kw: float) -> Line:
    """A confident transcript unless kw says otherwise."""
    base = {
        "language_probability": 0.95,
        "avg_logprob": -0.2,
        "no_speech_prob": 0.05,
        "compression_ratio": 1.0,
    }
    return Line(text, language, **(base | kw))


@pytest.mark.parametrize(
    ("case", "seconds", "reason"),
    [
        # Babble the S5 lab run posted: Chinese digits for French speech, and its repro's variant.
        (
            line("三二八四八四八四八二八四八三二八四八八", "zh", language_probability=0.3),
            6.0,
            "language_probability",
        ),
        (line("三二八四八四八四八二八四八三二八四八八", "zh", compression_ratio=2.9), 1.5, "repetition"),
        (line("打啦!死!打啦!", "fr"), 1.5, "script"),
        (line("Sa-hi-ba-su-ba-su-ba-su-ba", "en", compression_ratio=11.2), 6.3, "repetition"),
        (line("Die, Pozo.", "en", avg_logprob=-1.86), 1.2, "avg_logprob"),
        (line("Merci d'avoir regardé.", no_speech_prob=0.8, language_probability=0.4), 1.0, "no_speech"),
        (line("Bonjour à tous", "zh"), 2.0, "script"),
        (line("Привет всем", "en"), 2.0, "script"),
        (line("On passe en revue le plan", "fr", language_probability=0.4), 2.0, "language_probability"),
    ],
)
def test_implausible_lines_are_named(case: Line, seconds: float, reason: str) -> None:
    """001-AC8: a line in a script, language or shape the speech cannot have is not posted."""
    assert implausible(case, seconds, RULES) == reason


@pytest.mark.parametrize(
    ("case", "seconds"),
    [
        # Short answers: Whisper often hears a French "oui" as English at 0.3 (siwis-oui.wav).
        (line("Oui.", "en", language_probability=0.28, avg_logprob=-0.43, no_speech_prob=0.26), 0.8),
        (line("OK", "fr", language_probability=0.4), 1.0),
        (line("Yes.", "en", language_probability=0.45), 1.9),
        (line("Ça marche, d'accord : c'est noté à 100 %.", "fr"), 3.0),
        (line("Okay, let me switch to English.", "en"), 3.0),
        (line("打啦", "zh"), 1.0),
        (line("それは良いです", "ja"), 2.0),
        (line("Привет всем", "ru"), 2.0),
        (line("1, 2, 3", "fr"), 1.0),
        # Whisper kept this segment: likely silent, but decoded confidently in a sure language.
        (line("Bon, on y va.", "fr", no_speech_prob=0.7), 1.5),
    ],
)
def test_plausible_lines_pass(case: Line, seconds: float) -> None:
    """001-AC7: short valid answers and any language in its own script are posted."""
    assert implausible(case, seconds, RULES) is None
