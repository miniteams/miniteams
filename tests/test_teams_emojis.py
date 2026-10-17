"""Teams' emoticon catalog: the vendored table and the script that regenerates it."""

import ast
import importlib.util
import re
from pathlib import Path

import pytest

from miniteams.teams_emojis import TEAMS_EMOJIS

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "teams_emojis.py"
_spec = importlib.util.spec_from_file_location("teams_emojis_script", _SCRIPT)
assert _spec is not None and _spec.loader is not None
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def test_table_holds_only_characters() -> None:
    assert len(TEAMS_EMOJIS) > 1500
    assert all(re.fullmatch(r"[A-Za-z0-9_]{1,64}", key) for key in TEAMS_EMOJIS)
    assert not [key for key, char in TEAMS_EMOJIS.items() if char.isascii()]


@pytest.mark.parametrize(
    "text", ["🤘", "❤️\u200d🔥", "\U0001facd", 'a"b', "a\\b", "\u200f", "🏴\U000e0067\U000e007f"]
)
def test_literal_reads_back_as_the_same_string(text: str) -> None:
    assert ast.literal_eval(gen.literal(text)) == text
    assert gen.literal(text).isprintable()


def test_generator_drops_text_only_emoticons_and_keeps_the_first_of_a_repeat() -> None:
    metadata = {
        "categories": [
            {"emoticons": [{"id": "rock", "unicode": "🤘"}, {"id": "support", "unicode": "(support)"}]},
            {"emoticons": [{"id": "rock", "unicode": "🎸"}, {"id": "oreo", "unicode": "(oreo)"}]},
        ]
    }
    assert gen.table(metadata) == {"rock": "🤘"}


def test_generated_module_keeps_hostile_header_values_in_comments() -> None:
    """The URL and Last-Modified come from outside: they must not reach the module as code."""
    hostile = 'x"""\nimport os\nos.system("boom")\n"""'
    source = gen.module("https://h/x.json\nimport os", hostile, {"rock": "🤘"}, {"🤘": "rock"}, ["rock"])
    tree = ast.parse(source)
    assert [type(node) for node in tree.body] == [ast.Expr, ast.AnnAssign, ast.AnnAssign, ast.AnnAssign]
    assert "\nimport" not in source
    space: dict[str, object] = {}
    exec(compile(tree, "teams_emojis", "exec"), space)
    assert (space["TEAMS_EMOJIS"], space["TEAMS_DIVERSE"]) == ({"rock": "🤘"}, frozenset({"rock"}))


def test_plain_names_skip_themed_variants() -> None:
    """A character Teams names several times goes as its own `<hex>_` id, else the entry its Unicode
    name describes; only variants: no name at all."""

    def emo(name: str, char: str, description: str) -> dict[str, str]:
        return {"id": name, "unicode": char, "description": description}

    metadata = {
        "categories": [
            {
                "emoticons": [
                    emo("bartlett", "⚽", "Man playing football"),
                    emo("soccerball", "⚽", "Soccer ball"),
                    emo("saddog", "🙁", "Sad dog"),
                    emo("shake", "🙁", "Shake"),
                    emo("rock", "🤘", "Rock"),
                    emo("signofthehorns", "🤘", "Sign of the horns"),
                    emo("1f918_signofthehorns", "🤘", "Horns"),
                    emo("recycle", "♻️", "Recycle"),
                    emo("windturbine", "♻", "Wind turbine"),
                    emo("launch", "🚀", "Launch"),
                    emo("foxhug", "🦊", "Fox hug"),  # its only name: not listed
                ]
            }
        ]
    }
    assert gen.plain(metadata) == {"⚽": "soccerball", "🤘": "1f918_signofthehorns", "♻": "recycle"}


def test_diverse_lists_the_ids_teams_tones() -> None:
    emoticons = [
        {"id": "bartlett", "unicode": "⚽", "diverse": True},
        {"id": "launch", "unicode": "🚀", "diverse": False},
        {"id": "oreo", "unicode": "(oreo)", "diverse": True},
        {"id": "rock", "unicode": "🤘"},
    ]
    assert gen.diverse({"categories": [{"emoticons": emoticons}]}) == ["bartlett"]
