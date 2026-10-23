"""The script that regenerates src/miniteams/emojibase.py."""

import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))  # as when run: it imports teams_emojis.literal
_spec = importlib.util.spec_from_file_location("emojibase_names_script", _SCRIPTS / "emojibase_names.py")
assert _spec is not None and _spec.loader is not None
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def test_first_preset_wins_and_untypable_or_unknown_entries_are_dropped() -> None:
    data = [{"hexcode": "1F604", "emoji": "😄"}, {"hexcode": "1F642", "emoji": "🙂"}]
    presets = [
        {"1F604": ["smile", "Grin!"], "1F642": "slight_smile"},
        {"1F642": ["smile", "slightly_smiling_face"], "1F44D-1F3FB": "thumbsup_tone1"},
    ]
    assert gen.table(data, presets) == {"slight_smile": "🙂", "slightly_smiling_face": "🙂", "smile": "😄"}


def test_module_escapes_and_round_trips() -> None:
    names = {"+1": "👍", "family": "👨‍👩‍👧"}
    namespace: dict[str, object] = {}
    exec(compile(gen.module("https://x/", names), "emojibase.py", "exec"), namespace)  # noqa: S102
    assert namespace["EMOJIBASE"] == names
