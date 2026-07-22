"""Archive storage (spec 001): `data/index.db` + one folder per chat with `messages.db`.

Messages are stored as the raw API JSON keyed by message id — `INSERT OR IGNORE` makes every
write reentrant, and the resume cursors (`oldest`/`newest`) are derived from the data itself,
so there is no separate sync state that can desync after a crash. One transaction per page:
an interrupted run loses at most the in-flight page.
"""

import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

# Only `/` (and NUL) are illegal in a Linux path component; `:`/`@` stay readable.
_UNSAFE_RE = re.compile(r"[/\x00]")


def chat_dir_name(thread_id: str) -> str:
    """Filesystem-safe folder name for a thread; the authoritative id lives in index.db."""
    return _UNSAFE_RE.sub("_", thread_id)


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    # WAL: readers don't block the writer, and a crash mid-transaction rolls back cleanly.
    db.execute("PRAGMA journal_mode=WAL")
    return db


class Index:
    """`data/index.db` — one row per known chat; enumeration metadata + backfill flag."""

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        self._db = _connect(data_dir / "index.db")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                dir TEXT NOT NULL UNIQUE,  -- two ids sanitizing to one dir must fail loudly
                label TEXT NOT NULL DEFAULT '',
                topic TEXT NOT NULL DEFAULT '',
                participants TEXT NOT NULL DEFAULT '[]',
                raw TEXT NOT NULL DEFAULT '{}',  -- full conversation object (lastMessage, version…)
                backfill_done INTEGER NOT NULL DEFAULT 0,
                last_fetch_at TEXT NOT NULL DEFAULT ''
            )"""
        )
        # Migrate an index.db created before `raw` existed.
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(chats)")}
        if "raw" not in cols:
            self._db.execute("ALTER TABLE chats ADD COLUMN raw TEXT NOT NULL DEFAULT '{}'")
        self._db.commit()

    def upsert_chat(
        self,
        thread_id: str,
        label: str = "",
        topic: str = "",
        participants: list[Any] | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        """Insert or refresh enumeration metadata; never touches backfill_done/last_fetch_at.

        participants/raw=None means "leave as stored" — enumeration doesn't know the roster,
        and a single-thread run has no conversation object to store.
        """
        with self._db:
            self._db.execute(
                "INSERT INTO chats (id, dir) VALUES (?, ?) ON CONFLICT(id) DO NOTHING",
                (thread_id, chat_dir_name(thread_id)),
            )
            sets, values = ["label = ?", "topic = ?"], [label, topic]
            if participants is not None:
                sets.append("participants = ?")
                values.append(json.dumps(participants, ensure_ascii=False))
            if raw is not None:
                sets.append("raw = ?")
                values.append(json.dumps(raw, ensure_ascii=False))
            self._db.execute(f"UPDATE chats SET {', '.join(sets)} WHERE id = ?", (*values, thread_id))

    def chats(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT id, dir, label, topic, participants, backfill_done, last_fetch_at, raw"
            " FROM chats ORDER BY id"
        ).fetchall()
        return [
            {
                "id": r[0],
                "dir": r[1],
                "label": r[2],
                "topic": r[3],
                "participants": json.loads(r[4]),
                "backfill_done": bool(r[5]),
                "last_fetch_at": r[6],
                "raw": json.loads(r[7]),
            }
            for r in rows
        ]

    def backfill_done(self, thread_id: str) -> bool:
        row = self._db.execute("SELECT backfill_done FROM chats WHERE id = ?", (thread_id,)).fetchone()
        return bool(row and row[0])

    def mark_backfill_done(self, thread_id: str) -> None:
        with self._db:
            self._db.execute("UPDATE chats SET backfill_done = 1 WHERE id = ?", (thread_id,))

    def touch(self, thread_id: str, when_iso: str) -> None:
        with self._db:
            self._db.execute("UPDATE chats SET last_fetch_at = ? WHERE id = ?", (when_iso, thread_id))

    def close(self) -> None:
        self._db.close()


class ChatStore:
    """`data/<chat>/messages.db` — every message of one conversation, raw JSON keyed by id."""

    def __init__(self, data_dir: Path, thread_id: str) -> None:
        self.thread_id = thread_id
        self.dir = data_dir / chat_dir_name(thread_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.media_dir = self.dir / "media"
        self._db = _connect(self.dir / "messages.db")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                composetime TEXT NOT NULL,
                raw TEXT NOT NULL
            )"""
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_messages_composetime ON messages (composetime)")
        self._db.commit()

    def insert_page(self, messages: list[dict[str, Any]]) -> int:
        """Store one API page atomically; returns how many were actually new."""
        rows = []
        for message in messages:
            msg_id = str(message.get("id") or "")
            if not msg_id:
                # No stable id → can't dedup; extremely rare (malformed control events).
                log.warning("message_without_id_skipped", thread=self.thread_id)
                continue
            rows.append(
                (msg_id, str(message.get("composetime") or ""), json.dumps(message, ensure_ascii=False))
            )
        # total_changes delta, not COUNT(*) before/after: COUNT is O(rows) and this runs per page.
        before = self._db.total_changes
        with self._db:
            self._db.executemany(
                "INSERT OR IGNORE INTO messages (id, composetime, raw) VALUES (?, ?, ?)", rows
            )
        return self._db.total_changes - before

    def oldest(self) -> str | None:
        """Backfill resume cursor: composetime of the oldest stored message."""
        row = self._db.execute("SELECT MIN(composetime) FROM messages").fetchone()
        return row[0] if row and row[0] else None

    def newest(self) -> str | None:
        """Top-up overlap bound: composetime of the newest stored message."""
        row = self._db.execute("SELECT MAX(composetime) FROM messages").fetchone()
        return row[0] if row and row[0] else None

    def iter_messages(self) -> Iterator[dict[str, Any]]:
        """Yield every stored message (raw JSON), oldest first."""
        cursor = self._db.execute("SELECT raw FROM messages ORDER BY composetime")
        for (raw,) in cursor:
            yield json.loads(raw)

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    def close(self) -> None:
        self._db.close()
