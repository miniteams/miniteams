# Copied from grill-room `stt/tests/test_parakeet.py` @ 89744d1 (spec 044, AC16), trimmed with the code it
# tests: no room prompt, metrics or engine info; the engine setting is miniteams Settings.stt_engine.
# Copyright grill-room authors. Licensed under the Apache License, Version 2.0:
# http://www.apache.org/licenses/LICENSE-2.0
import time

import numpy as np
import pytest
from pydantic import ValidationError

from miniteams.config import Settings
from miniteams.stt.line import Line
from miniteams.stt.parakeet import compression_ratio
from miniteams.stt.pipeline import Pipeline, Plausibility, Utterance, guess_language, implausible_parakeet
from miniteams.stt.vad import Audio

RULES = Plausibility()


def parakeet_line(text: str) -> Line:
    """A transcript as ParakeetTranscriber returns it: no language, no confidence."""
    return Line(text, "", compression_ratio=compression_ratio(text))


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        # 007-AC3: a looped phrase, and one token repeated.
        ("on parle de la carte, " * 12, "repetition"),
        ("oui " * 40, "repetition"),
        # 007-AC3: a transcript in another script means the engine went astray.
        ("Привет всем, как дела сегодня", "script"),
        ("我们今天讨论一下部署的问题", "script"),
        ("Bonjour, привет всем как дела", "script"),
    ],
)
def test_parakeet_drops_repetition_and_other_scripts(text: str, reason: str) -> None:
    assert implausible_parakeet(parakeet_line(text), 3.0, RULES) == reason


@pytest.mark.parametrize(
    "text",
    [
        "Oui.",
        "OK",
        "Ça marche, d'accord : c'est noté à 100 %.",
        "On déploie le chart Helm avec Argo CD, puis on regarde les pods dans Kubernetes.",
        "Okay, let me switch to English.",
        "1, 2, 3",
        # Whisper's confidence checks do not apply: a Line with none of them still passes.
        "Les métriques de latence sont dans Grafana.",
    ],
)
def test_parakeet_posts_french_and_english(text: str) -> None:
    """007-AC3: no language probability, no language script: only repetition and Latin letters."""
    assert implausible_parakeet(parakeet_line(text), 0.8, RULES) is None
    assert implausible_parakeet(Line(text, "", 0.1, -3.0, 0.99, 1.0), 6.0, RULES) is None


def test_compression_ratio_matches_whisper() -> None:
    assert compression_ratio("") == 0.0
    assert compression_ratio("Bonjour à tous.") < 1.5
    assert compression_ratio("la la " * 50) > 2.4


@pytest.mark.parametrize(
    ("text", "guess"),
    [
        ("Oui.", "fr"),
        ("D'accord, c'est noté.", "fr"),
        # French dense in English terms is still French.
        ("On déploie le chart Helm avec Argo CD et on check les pods dans le cluster.", "fr"),
        ("Le build est cassé, le pipeline CI ne passe pas sur la merge request.", "fr"),
        ("Yes.", "en"),
        ("Okay, let me switch to English for the rest of the call.", "en"),
        ("We deploy the chart with Argo CD and check the pods.", "en"),
        ("OK", "unknown"),
        ("Kubernetes, Helm, Argo CD", "unknown"),
        ("", "unknown"),
        ("1, 2, 3", "unknown"),
    ],
)
def test_language_guess(text: str, guess: str) -> None:
    """007-AC4: fr, en or unknown from function words, English terms in French included."""
    assert guess_language(text) == guess


async def test_parakeet_pipeline_drops_empty_and_other_scripts() -> None:
    """007-AC3, AC14: whitespace is empty, a Cyrillic line is dropped, the others reach the sink, and the
    engine is never given a prompt."""
    stored: list[str] = []
    prompts: list[str] = []
    texts = iter(
        ["   ", "Привет всем, как дела", "On check les pods dans le cluster.", "Let me check the pods."]
    )

    def transcribe(_a: Audio, prompt: str) -> Line:
        prompts.append(prompt)
        return parakeet_line(next(texts))

    p = Pipeline([transcribe], lambda _u, line: stored.append(line.text), 4, engine="parakeet")
    for _ in range(4):
        await p.process(Utterance(7, np.ones(16000, np.float32), time.time()))
    assert stored == ["On check les pods dans le cluster.", "Let me check the pods."]
    assert prompts == [""] * 4


@pytest.mark.parametrize("value", ["sherpa", "Parakeet", ""])
def test_unknown_engine_is_refused_naming_the_allowed_values(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """007-AC1, 044-AC10: any other engine stops at start, naming parakeet and whisper."""
    monkeypatch.setenv("MINITEAMS_STT_ENGINE", value)
    with pytest.raises(ValidationError, match="'parakeet' or 'whisper'"):
        Settings(tenant_id="t")
