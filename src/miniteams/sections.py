"""Chat sections (spec 026): named, ordered groups of chats, saved as `sections.json`.

Every mutator returns False when the request is refused (malformed, unknown section or chat); a
valid request may still change nothing, which the caller detects by comparing `items`.
"""

import contextlib
import json
import os
import secrets
import shutil
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeIs

import structlog

from .config import write_private

log = structlog.get_logger()

NAME_MAX = 60  # characters, after whitespace is collapsed
MAX_SECTIONS = 100  # the page is trusted but the file must stay bounded
ORDERS = ("recent", "manual")

type Section = dict[str, Any]  # {"id": str, "name": str, "order": "recent" | "manual", "chats": [thread id]}
type Rows = Callable[[], Mapping[str, Mapping[str, Any]]]


def _joiner(char: str) -> bool:
    # Joiners and tag characters build emoji, flags, Indic and Persian words: kept, but never a name alone.
    return char in "\u200c\u200d" or 0xE0020 <= ord(char) <= 0xE007F


def _shown(char: str) -> bool:
    return not unicodedata.category(char).startswith("C") or _joiner(char)


def clean_name(value: Any) -> str | None:
    """The name as stored, or None when refused: plain text, one line, 1..NAME_MAX characters."""
    if not isinstance(value, str):
        return None
    # Controls, bidi overrides and zero-widths go first, so the spaces around them still collapse.
    name = " ".join("".join(c for c in value if _shown(c) or c.isspace()).split())
    return name if len(name) <= NAME_MAX and not all(map(_joiner, name)) else None


def _is_index(value: Any) -> TypeIs[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _target(index: int, size: int, by: Any, to: Any) -> int | None:
    """Where `by` (±1) or `to` (an index, clamped) moves item `index`; None for a malformed or no-op step."""
    if _is_index(to):
        return max(0, min(to, size - 1))
    if _is_index(by) and by in (-1, 1) and 0 <= index + by < size:
        return index + by
    return None


class Sections:
    def __init__(self, path: Path | None, rows: Rows) -> None:
        self.path = path
        self.rows = rows  # the board's rows: which chats exist, and their activity
        self.items: list[Section] = load(path)

    def save(self) -> None:
        if self.path is not None:
            write_private(self.path, json.dumps(self.items, ensure_ascii=False, indent=0))

    def _section(self, section_id: Any) -> Section | None:
        return next((s for s in self.items if s["id"] == section_id), None)

    def _known(self, chat: Any) -> bool:
        return isinstance(chat, str) and chat in self.rows()

    def _holder(self, chat_id: str) -> Section | None:
        return next((s for s in self.items if chat_id in s["chats"]), None)

    def create(self, name: Any, chat: Any = None) -> bool:
        """A new section, last before Other; `chat` is filed into it."""
        clean = clean_name(name)
        if clean is None or len(self.items) >= MAX_SECTIONS or (chat is not None and not self._known(chat)):
            return False
        section_id = secrets.token_urlsafe(6)
        self.items.append({"id": section_id, "name": clean, "order": "recent", "chats": []})
        return chat is None or self.file(chat, section_id)

    def rename(self, section_id: Any, name: Any) -> bool:
        section, clean = self._section(section_id), clean_name(name)
        if section is None or clean is None:
            return False
        section["name"] = clean
        return True

    def set_order(self, section_id: Any, order: Any) -> bool:
        section = self._section(section_id)
        if section is None or order not in ORDERS:
            return False
        if order == "manual" and section["order"] != "manual":
            # Manual starts from what the user sees: the chats by recency, absent ones last.
            rows = self.rows()
            section["chats"].sort(key=lambda c: str(rows.get(c, {}).get("last_activity") or ""), reverse=True)
        section["order"] = order
        return True

    def move(self, section_id: Any, by: Any = None, to: Any = None) -> bool:
        section = self._section(section_id)
        if section is None:
            return False
        index = self.items.index(section)
        target = _target(index, len(self.items), by, to)
        if target is None:
            return False
        self.items.insert(target, self.items.pop(index))
        return True

    def delete(self, section_id: Any) -> bool:
        """Its chats go back to no section (Other)."""
        section = self._section(section_id)
        if section is None:
            return False
        self.items.remove(section)
        return True

    def file(self, chat: Any, section_id: Any, at: Any = None) -> bool:
        """File `chat` into a section (None = no section) at `at`, top by default; one section per chat."""
        if not self._known(chat) or (at is not None and not _is_index(at)):
            return False
        section = None if section_id is None else self._section(section_id)
        if section_id is not None and section is None:
            return False
        holder = self._holder(chat)
        if holder is section and at is None:
            return True  # filed again where it already is: a Manual section keeps its place for it
        if holder is not None:
            holder["chats"].remove(chat)
        if section is not None:
            section["chats"].insert(max(0, min(at or 0, len(section["chats"]))), chat)
        return True

    def move_chat(self, chat: Any, by: Any = None, to: Any = None) -> bool:
        """Within its Manual section only: a Recent one orders by activity."""
        holder = self._holder(chat) if isinstance(chat, str) else None
        if holder is None or holder["order"] != "manual":
            return False
        chats = holder["chats"]
        index = chats.index(chat)
        target = _target(index, len(chats), by, to)
        if target is None:
            return False
        chats.insert(target, chats.pop(index))
        return True


def load(path: Path | None) -> list[Section]:
    """The saved sections; a missing file is none, an unreadable one is none and logged, never a crash."""
    if path is None:
        return []
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except OSError, ValueError, RecursionError:
        return _set_aside(path)
    if not isinstance(data, list):
        return _set_aside(path)
    sections: list[Section] = []
    for entry in data[:MAX_SECTIONS]:
        if (section := _parsed(entry, sections)) is None:
            log.warning("section_dropped", path=str(path))
        else:
            sections.append(section)
    if sections != data:  # the next save writes `sections`: what it would lose is kept aside
        log.warning("sections_set_aside", path=str(path))
        with contextlib.suppress(OSError):
            shutil.copy2(path, path.with_name(path.name + ".bad"))
    return sections


def _set_aside(path: Path) -> list[Section]:
    """No sections; the file moves to `.bad` so the next save does not overwrite what a hand edit broke."""
    log.warning("sections_unreadable", path=str(path))
    with contextlib.suppress(OSError):
        os.replace(path, path.with_name(path.name + ".bad"))
    return []


def _parsed(entry: Any, kept: list[Section]) -> Section | None:
    """One saved section, or None when malformed; a chat already filed earlier stays there."""
    if not isinstance(entry, dict):
        return None
    section_id, name = entry.get("id"), clean_name(entry.get("name"))
    order, chats = entry.get("order"), entry.get("chats")
    if not isinstance(section_id, str) or not section_id or any(s["id"] == section_id for s in kept):
        return None
    if name is None or order not in ORDERS or not isinstance(chats, list):
        return None
    filed = {c for s in kept for c in s["chats"]}
    own = [c for c in dict.fromkeys(chats) if isinstance(c, str) and c not in filed]
    return {"id": section_id, "name": name, "order": order, "chats": own}
