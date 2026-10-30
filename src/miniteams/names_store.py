"""Names store (spec 038): every name miniteams learned for a person, ranked, shared by its processes.

`<config_dir>/names.db`, state rather than cache: a departed person's name cannot be fetched again.
One row per bare MRI, written row by row (never a whole-file rewrite), so processes keep each other's names.
"""

import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

LIVE, ARCHIVED, DECLARED = 3, 2, 1  # profile lookup or fetched roster; the archive; a message's imdisplayname

_BUSY_TIMEOUT = 2.0  # seconds a write waits for another process's: it may run in the event loop
_WAL_TRIES = 20  # x 50 ms: two processes creating names.db at once

type Entry = tuple[str, str, int, str, int]  # mri, name, rank, source, seen_at (epoch ms)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS names (
    mri TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    rank INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    seen_at INTEGER NOT NULL DEFAULT 0,
    looked_up_at INTEGER
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# 038-AC2: never a lower rank; within a rank, the latest seen. A lookup's looked_up_at is left alone.
_UPSERT = """
INSERT INTO names (mri, name, rank, source, seen_at) VALUES (?, ?, ?, ?, ?)
ON CONFLICT (mri) DO UPDATE SET
    name = excluded.name, rank = excluded.rank, source = excluded.source, seen_at = excluded.seen_at
WHERE excluded.rank > names.rank OR (excluded.rank = names.rank AND excluded.seen_at >= names.seen_at)
"""

# A lookup that named nobody: noted beside the name, never over it.
_MISSED = """
INSERT INTO names (mri, looked_up_at) VALUES (?, ?)
ON CONFLICT (mri) DO UPDATE SET looked_up_at = excluded.looked_up_at
"""


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def iso_ms(value: object) -> int:
    """An ISO 8601 time as epoch ms; 0 when missing or unparsable."""
    try:
        return int(datetime.fromisoformat(str(value)).timestamp() * 1000)
    except ValueError, OverflowError:
        return 0


class Row(NamedTuple):
    name: str
    rank: int
    looked_up: bool  # a successful lookup found no name for it
    seen_at: int  # epoch ms the name was seen at


class NameStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))  # Security: owner-only before SQLite opens
        # One connection for the event loop and the executor threads, serialised by the lock.
        self._db = sqlite3.connect(path, timeout=_BUSY_TIMEOUT, isolation_level=None, check_same_thread=False)
        self._lock = threading.Lock()
        try:
            # Under the write lock: a deferred read-then-write fails at once, busy handler or not.
            self._db.executescript(f"BEGIN IMMEDIATE; {_SCHEMA} COMMIT;")
            self._wal()
            # WAL keeps NORMAL crash-safe; FULL would fsync every name write, from the event loop.
            self._db.execute("PRAGMA synchronous=NORMAL")
        except BaseException:
            self._db.close()
            raise

    def _wal(self) -> None:
        # The switch needs every other connection out, which the busy handler does not wait for.
        for attempt in range(_WAL_TRIES):
            try:
                if self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal":
                    return
            except sqlite3.OperationalError:
                if attempt == _WAL_TRIES - 1:
                    raise
            time.sleep(0.05)
        raise sqlite3.OperationalError("names.db stayed out of WAL mode")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def get(self, mris: Iterable[str]) -> dict[str, Row]:
        with self._lock:
            return self._get(set(mris))

    def _get(self, mris: set[str]) -> dict[str, Row]:
        if not mris:
            return {}
        marks = ",".join("?" * len(mris))
        query = f"SELECT mri, name, rank, looked_up_at, seen_at FROM names WHERE mri IN ({marks})"
        return {m: Row(n, r, at is not None, s) for m, n, r, at, s in self._db.execute(query, list(mris))}

    def put(self, entries: Iterable[Entry]) -> dict[str, Row]:
        """Upsert named entries in one transaction; the stored rows of their MRIs afterwards."""
        rows = [e for e in entries if e[0] and e[1]]
        if not rows:
            return {}
        with self._lock:
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                self._db.executemany(_UPSERT, rows)
            return self._get({e[0] for e in rows})

    def missed(self, mris: Iterable[str], at: int) -> None:
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.executemany(_MISSED, [(m, at) for m in mris])

    def imported(self, key: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM meta WHERE key = ?", (key,)).fetchone() is not None

    def import_once(self, key: str, entries: Iterable[Entry], missed: Iterable[str], at: int) -> bool:
        """Write an import unless `key` marks it done; one transaction, so concurrent starts import once."""
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if self._db.execute("SELECT 1 FROM meta WHERE key = ?", (key,)).fetchone():
                return False
            self._db.executemany(_UPSERT, [e for e in entries if e[0] and e[1]])
            self._db.executemany(_MISSED, [(m, at) for m in missed])
            self._db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, str(at)))
            return True
