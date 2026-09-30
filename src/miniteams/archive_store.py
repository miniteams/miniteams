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


def message_version(message: dict[str, Any]) -> int:
    """Message version (ms epoch string); 0 when absent so any real version beats it."""
    try:
        return int(message.get("version") or 0)
    except TypeError, ValueError:
        return 0


def merge_dicts(old: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Shallow merge, one level deeper for dicts: live updates can be partial (`properties`)."""
    merged = dict(old)
    for key, value in patch.items():
        prev = merged.get(key)
        merged[key] = {**prev, **value} if isinstance(prev, dict) and isinstance(value, dict) else value
    return merged


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    # WAL: readers don't block the writer, and a crash mid-transaction rolls back cleanly.
    db.execute("PRAGMA journal_mode=WAL")
    return db


def read_chats(data_dir: Path) -> Iterator[dict[str, Any]]:
    """Every indexed chat, read-only, one at a time.

    Not `Index.chats()`: that constructor creates the directory and the table, and a reader must
    never bring an archive into existence — nor block the writer. Raises OSError when there is no
    index and sqlite3.Error when it cannot be read; both mean "no archive" to the caller.

    Streamed, not a list: `raw` holds the whole conversation object, so materialising the table
    costs several times the file (67 MiB peak for a 12 MiB index) for rows the caller drops at once.
    """
    path = data_dir / "index.db"
    if not path.is_file():
        raise FileNotFoundError(path)
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for r in db.execute(
            "SELECT id, label, topic, participants, raw, backfill_done, history_denied_at, last_fetch_at, dir"
            " FROM chats"
        ):
            yield {
                "id": r[0],
                "label": r[1],
                "topic": r[2],
                "participants": json.loads(r[3] or "[]"),
                "raw": json.loads(r[4] or "{}"),
                "backfill_done": bool(r[5]),
                "history_denied": bool(r[6]),
                "synced_at": r[7] or "",
                "dir": r[8] or "",
            }
    finally:
        db.close()


def read_meta(data_dir: Path) -> dict[str, str]:
    """The archiver's `meta` rows, read-only; `{}` for an index without the table.

    Same rule as `read_chats`: a reader never creates the archive. Raises OSError when there is no
    index and sqlite3.Error when it cannot be read.
    """
    path = data_dir / "index.db"
    if not path.is_file():
        raise FileNotFoundError(path)
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "meta" not in tables:
            return {}
        return {str(k): str(v) for k, v in db.execute("SELECT key, value FROM meta")}
    finally:
        db.close()


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
                last_fetch_at TEXT NOT NULL DEFAULT '',
                history_denied_at TEXT NOT NULL DEFAULT '',  -- 403 on history; skip until forced
                synced_version INTEGER NOT NULL DEFAULT 0  -- conv version at the last complete pass
            )"""
        )
        # Migrate an index.db created before these columns existed.
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(chats)")}
        if "raw" not in cols:
            self._db.execute("ALTER TABLE chats ADD COLUMN raw TEXT NOT NULL DEFAULT '{}'")
        if "history_denied_at" not in cols:
            self._db.execute("ALTER TABLE chats ADD COLUMN history_denied_at TEXT NOT NULL DEFAULT ''")
        if "synced_version" not in cols:
            self._db.execute("ALTER TABLE chats ADD COLUMN synced_version INTEGER NOT NULL DEFAULT 0")
        # Archiver liveness for readers (spec 008): archiver_seen, archiver_state, sync_started_at.
        self._db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._db.commit()

    def set_meta(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def get_meta(self, key: str) -> str:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else ""

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

    def ensure_chat(self, thread_id: str) -> None:
        """Row for a chat first seen live; the next pass fills label and roster."""
        with self._db:
            self._db.execute(
                "INSERT INTO chats (id, dir) VALUES (?, ?) ON CONFLICT(id) DO NOTHING",
                (thread_id, chat_dir_name(thread_id)),
            )

    def merge_raw(self, thread_id: str, patch: dict[str, Any]) -> None:
        """Fold a live update into the stored conversation object.

        `lastMessage` is replaced, never merged (it is another message), and only by a newer one:
        events can arrive after the pass stored a fresher conversation object.
        """
        self.ensure_chat(thread_id)
        with self._db:
            (stored,) = self._db.execute("SELECT raw FROM chats WHERE id = ?", (thread_id,)).fetchone()
            raw = json.loads(stored)
            patch = dict(patch)
            last = patch.pop("lastMessage", None)
            raw = merge_dicts(raw, patch)
            if isinstance(last, dict) and str(last.get("composetime") or "") >= str(
                (raw.get("lastMessage") or {}).get("composetime") or ""
            ):
                raw["lastMessage"] = last
            self._db.execute(
                "UPDATE chats SET raw = ? WHERE id = ?", (json.dumps(raw, ensure_ascii=False), thread_id)
            )

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

    def synced_version(self, thread_id: str) -> int:
        row = self._db.execute("SELECT synced_version FROM chats WHERE id = ?", (thread_id,)).fetchone()
        return int(row[0]) if row else 0

    def mark_synced(self, thread_id: str, version: int) -> None:
        with self._db:
            self._db.execute("UPDATE chats SET synced_version = ? WHERE id = ?", (version, thread_id))

    def history_denied_ids(self) -> set[str]:
        return {r[0] for r in self._db.execute("SELECT id FROM chats WHERE history_denied_at != ''")}

    def mark_history_denied(self, thread_id: str, when_iso: str) -> None:
        with self._db:
            self._db.execute("UPDATE chats SET history_denied_at = ? WHERE id = ?", (when_iso, thread_id))

    def clear_history_denied(self, thread_id: str) -> None:
        with self._db:
            self._db.execute("UPDATE chats SET history_denied_at = '' WHERE id = ?", (thread_id,))

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
                raw TEXT NOT NULL,
                pending INTEGER NOT NULL DEFAULT 0  -- not yet covered by a complete top-up walk
            )"""
        )
        if "pending" not in {r[1] for r in self._db.execute("PRAGMA table_info(messages)")}:
            # Rows written live before this column are indistinguishable: `archive --recheck-since`.
            self._db.execute("ALTER TABLE messages ADD COLUMN pending INTEGER NOT NULL DEFAULT 0")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_messages_composetime ON messages (composetime)")
        # Negative cache for assets the archive gave up on; `--retry-assets` forces them all.
        # 403 (deleted object, lost share permission) is permanent — `retry_after` stays ''.
        # 404 backs off instead of being permanent: a transcript can still be generating when the
        # recording message lands, and a permanent verdict would lose it for good.
        # `url` holds a cache KEY, not always a bare URL: assets fetched in several renditions key
        # as `<url>#<ext>`, so one dead rendition never suppresses the one that still serves.
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS denied_assets (
                url TEXT PRIMARY KEY,
                status INTEGER NOT NULL,
                at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 1,
                retry_after TEXT NOT NULL DEFAULT ''
            )"""
        )
        # Versions a live update replaced (edits, reactions, deletes): `messages.raw` holds the latest.
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS message_versions (
                id TEXT NOT NULL,
                version TEXT NOT NULL,
                raw TEXT NOT NULL,
                replaced_at TEXT NOT NULL,
                PRIMARY KEY (id, version)
            )"""
        )
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(denied_assets)")}
        for name, decl in (
            ("attempts", "INTEGER NOT NULL DEFAULT 1"),
            ("retry_after", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in cols:
                # Pre-existing rows are all 403s: the '' default is exactly their semantics.
                self._db.execute(f"ALTER TABLE denied_assets ADD COLUMN {name} {decl}")
        self._db.commit()

    def insert_page(self, messages: list[dict[str, Any]], *, pending: bool = False) -> int:
        """Store one API page atomically; returns how many were actually new."""
        rows = []
        for message in messages:
            msg_id = str(message.get("id") or "")
            if not msg_id:
                # No stable id → can't dedup; extremely rare (malformed control events).
                log.warning("message_without_id_skipped", thread=self.thread_id)
                continue
            rows.append(
                (
                    msg_id,
                    str(message.get("composetime") or ""),
                    json.dumps(message, ensure_ascii=False),
                    int(pending),
                )
            )
        # total_changes delta, not COUNT(*) before/after: COUNT is O(rows) and this runs per page.
        before = self._db.total_changes
        with self._db:
            self._db.executemany(
                "INSERT OR IGNORE INTO messages (id, composetime, raw, pending) VALUES (?, ?, ?, ?)", rows
            )
        return self._db.total_changes - before

    def apply_message(self, message: dict[str, Any], now_iso: str) -> str:
        """Store one live message: `new`, `updated` (newer version, old raw kept) or `stale`."""
        msg_id = str(message.get("id") or "")
        if not msg_id:
            log.warning("message_without_id_skipped", thread=self.thread_id)
            return "stale"
        with self._db:
            row = self._db.execute("SELECT composetime, raw FROM messages WHERE id = ?", (msg_id,)).fetchone()
            if row is None:
                self._db.execute(
                    # Pending: a live message says nothing of the ones before it (see `newest`).
                    "INSERT INTO messages (id, composetime, raw, pending) VALUES (?, ?, ?, 1)",
                    (msg_id, str(message.get("composetime") or ""), json.dumps(message, ensure_ascii=False)),
                )
                return "new"
            old = json.loads(row[1])
            if message_version(message) <= message_version(old):
                if 0 < message_version(message) < message_version(old):
                    # Arrived late (buffer order, redelivery): history, not current.
                    self._db.execute(
                        "INSERT OR IGNORE INTO message_versions (id, version, raw, replaced_at)"
                        " VALUES (?, ?, ?, ?)",
                        (
                            msg_id,
                            str(message.get("version") or ""),
                            json.dumps(message, ensure_ascii=False),
                            now_iso,
                        ),
                    )
                return "stale"
            self._db.execute(
                "INSERT OR IGNORE INTO message_versions (id, version, raw, replaced_at) VALUES (?, ?, ?, ?)",
                (msg_id, str(old.get("version") or ""), row[1], now_iso),
            )
            # Live updates carry the whole message: replacing drops a removed reaction, merging
            # would keep it. A resource without `content` would be partial, so merge that one.
            merged = dict(message) if "content" in message else merge_dicts(old, message)
            self._db.execute(
                "UPDATE messages SET composetime = ?, raw = ? WHERE id = ?",
                (
                    str(merged.get("composetime") or row[0]),
                    json.dumps(merged, ensure_ascii=False),
                    msg_id,
                ),
            )
            return "updated"

    def oldest(self) -> str | None:
        """Backfill resume cursor: composetime of the oldest stored message."""
        row = self._db.execute("SELECT MIN(composetime) FROM messages").fetchone()
        return row[0] if row and row[0] else None

    def newest(self) -> str | None:
        """Top-up overlap bound: composetime of the newest message a complete walk reached. A live
        write or a half-done top-up can hold a newer one past a hole the next top-up must cross."""
        row = self._db.execute("SELECT MAX(composetime) FROM messages WHERE pending = 0").fetchone()
        return row[0] if row and row[0] else None

    def confirm(self, upto: str) -> None:
        """A top-up walked down to the bound without a hole: everything up to `upto` is covered."""
        with self._db:
            self._db.execute(
                "UPDATE messages SET pending = 0 WHERE pending = 1 AND composetime <= ?", (upto,)
            )

    def iter_messages(self) -> Iterator[dict[str, Any]]:
        """Yield every stored message (raw JSON), oldest first."""
        cursor = self._db.execute("SELECT raw FROM messages ORDER BY composetime")
        for (raw,) in cursor:
            yield json.loads(raw)

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    def denied_urls(self, now_iso: str) -> set[str]:
        """Cache keys still suppressed at `now_iso`: permanent ones, plus backoffs not yet due."""
        return {
            r[0]
            for r in self._db.execute(
                "SELECT url FROM denied_assets WHERE retry_after = '' OR retry_after > ?", (now_iso,)
            )
        }

    def denied_attempts(self, key: str) -> int:
        row = self._db.execute("SELECT attempts FROM denied_assets WHERE url = ?", (key,)).fetchone()
        return int(row[0]) if row else 0

    def mark_denied(
        self, key: str, status: int, when_iso: str, attempts: int = 1, retry_after: str = ""
    ) -> None:
        """Record a give-up. `retry_after` empty ⇒ permanent (403); an ISO stamp ⇒ retry past it."""
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO denied_assets (url, status, at, attempts, retry_after) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, status, when_iso, attempts, retry_after),
            )

    def close(self) -> None:
        self._db.close()
