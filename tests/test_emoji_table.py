"""Built-in shortcode table (spec 012)."""

import re
from pathlib import Path

from miniteams import emoji_table
from miniteams.emoji_table import SHORTCODES


def test_table_has_the_common_names_and_aliases() -> None:
    """012-AC1 (table part): `fire` → 🔥, `+1` and `thumbsup` are the same 👍."""
    assert SHORTCODES["fire"] == "🔥"
    assert SHORTCODES["+1"] == SHORTCODES["thumbsup"] == "👍"
    assert SHORTCODES["-1"] == SHORTCODES["thumbsdown"] == "👎"
    assert SHORTCODES["check"] == SHORTCODES["white_check_mark"] == "✅"
    assert 300 <= len(SHORTCODES) <= 500


def test_names_are_typable_and_the_source_is_sorted_without_duplicates() -> None:
    # The page opens the popup on `[a-z0-9_+-]`: a name outside that set could never be completed.
    assert all(re.fullmatch(r"[a-z0-9_+-]+", name) for name in SHORTCODES)
    assert all(SHORTCODES.values())
    # A dict literal silently keeps the last of two equal keys: count them in the source instead.
    keys = re.findall(r'^    "([^"]+)":', Path(emoji_table.__file__).read_text(encoding="utf-8"), re.M)
    assert keys == sorted(set(keys))
    assert len(keys) == len(SHORTCODES)
