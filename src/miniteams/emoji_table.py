"""`:shortcode:` → character for the composer: Teams' names (`servicedog`), then emojibase's."""

import re

from .emojibase import EMOJIBASE
from .teams_emojis import TEAMS_EMOJIS

# Teams' hex-keyed ids (`1f910_zippermouthface`, `1f415_200d_1f9ba_servicedog`) are typed by their name part.
_HEX_ID = re.compile(r"[0-9][0-9a-f]{3,5}(?:[-_][0-9a-f]{4,6})*_")

SHORTCODES: dict[str, str] = {
    **EMOJIBASE,
    # Teams wins a clash (`smile` is 🙂, as Teams shows it); the first of two equal Teams names wins.
    **{_HEX_ID.sub("", name, count=1): char for name, char in reversed(TEAMS_EMOJIS.items())},
}
