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


def test_themed_ids_are_the_variants_of_a_shared_character() -> None:
    """042-AC1: an id whose character another id also shows, bar the one sent for it, gets Teams' art."""
    from miniteams.messages import THEMED, themed_art

    assert len(THEMED) == 102
    assert {"rock", "laughcat", "coolkoala", "sadrobot", "hungover", "yes"} <= THEMED
    assert not {"1f918_signofthehorns", "like", "sad", "heart", "smile"} & THEMED
    assert all(re.fullmatch(r"[\w-]{1,64}", i, re.ASCII) for i in THEMED)  # safe as a file name
    # 042-AC6: a toned themed id is its base only when another id shows its toned character too.
    keys = ("gran-tone3", "dropthemic-tone3", "rock-tone3", "yes-tone3", "crossedfingers-tone3", "gran-tone9")
    assert [themed_art(k) for k in keys] == ["gran", "dropthemic", "rock", None, None, None]


def test_no_two_ids_share_a_chip_face() -> None:
    """042-AC1, 042-AC6: every Teams id and toned key shows art or a character no other id shows."""
    from miniteams.messages import _TONE_RE, reaction_emoji, themed_art
    from miniteams.teams_emojis import TEAMS_DIVERSE

    faces: dict[str, set[str]] = {}
    for key in [*TEAMS_EMOJIS, *(f"{n}-tone{t}" for n in TEAMS_DIVERSE for t in "12345")]:
        art = themed_art(key)
        face = f"art:{art}" if art else reaction_emoji(key).replace("\ufe0f", "")
        faces.setdefault(face, set()).add(_TONE_RE.sub("", key))
    assert {face: ids for face, ids in faces.items() if len(ids) > 1} == {}
