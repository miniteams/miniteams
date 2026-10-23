"""Built-in shortcode table (spec 012): Teams' names, then emojibase's."""

import re
from pathlib import Path

from miniteams import emojibase
from miniteams.emoji_table import SHORTCODES


def test_table_has_the_common_names_and_aliases() -> None:
    """012-AC1 (table part): `fire` → 🔥, `+1` and `thumbsup` are the same 👍."""
    assert SHORTCODES["fire"] == "🔥"
    assert SHORTCODES["+1"] == SHORTCODES["thumbsup"] == "👍"
    assert SHORTCODES["-1"] == SHORTCODES["thumbsdown"] == "👎"
    assert SHORTCODES["white_check_mark"] == "✅"
    assert SHORTCODES["hankey"] == SHORTCODES["poop"] == "💩"  # GitHub's name, emojibase's
    assert SHORTCODES["saluting_face"] == SHORTCODES["salute"] == "🫡"


def test_teams_wins_a_clash_with_emojibase() -> None:
    assert SHORTCODES["smile"] == "🙂"  # emojibase's `smile` is 😄
    assert SHORTCODES["ok"] == "👌"  # emojibase's `ok` is 🆗
    assert SHORTCODES["party"] == "🥳"  # emojibase's `party` is 🎉
    assert SHORTCODES["grinning"] == "😀"  # emojibase-only names stay


def test_teams_names_complete_too() -> None:
    """Teams' own names expand, hex-keyed ids by their name part."""
    assert SHORTCODES["zippermouthface"] == "🤐"
    assert SHORTCODES["servicedog"] == "🐕‍🦺"
    assert SHORTCODES["face_massage"] == "💆‍♀️"  # a real name, not a hex id
    assert not any(re.match(r"\d[0-9a-f]{3,5}[-_]", name) for name in SHORTCODES)


def test_names_are_typable_and_the_source_is_sorted_without_duplicates() -> None:
    # The page opens the popup on `[a-z0-9_+-]`: a name outside that set could never be completed.
    assert all(re.fullmatch(r"[a-z0-9_+-]+", name) for name in SHORTCODES)
    assert all(SHORTCODES.values())
    # A dict literal silently keeps the last of two equal keys: count them in the source instead.
    keys = re.findall(r'^    "([^"]+)":', Path(emojibase.__file__).read_text(encoding="utf-8"), re.M)
    assert keys == sorted(set(keys))
    assert len(keys) == len(emojibase.EMOJIBASE)
    # Skin-tone variants would crowd the completion; the bare modifiers (`tone1`) are emojis of their own.
    assert not any(re.search(".[\U0001f3fb-\U0001f3ff]", char) for char in emojibase.EMOJIBASE.values())
