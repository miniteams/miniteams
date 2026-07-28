"""Archive storage layer: dedup, cursors, reopen-resume, dir sanitization (spec 001 phase 1)."""

from pathlib import Path
from typing import Any

from miniteams.archive_store import ChatStore, Index, chat_dir_name

THREAD = "19:user1_user2@unq.gbl.spaces"


def _msg(mid: str, when: str, **extra: Any) -> dict[str, Any]:
    return {"id": mid, "composetime": when, "content": f"msg {mid}", **extra}


def test_chat_dir_name_keeps_readable_chars_replaces_slash() -> None:
    assert chat_dir_name(THREAD) == THREAD  # ':' and '@' are legal path chars
    assert chat_dir_name("19:a/b@thread.v2") == "19:a_b@thread.v2"


def test_insert_page_dedups_on_double_insert(tmp_path: Path) -> None:
    store = ChatStore(tmp_path, THREAD)
    page = [_msg("2", "2026-07-02T10:00:00Z"), _msg("1", "2026-07-01T10:00:00Z")]
    assert store.insert_page(page) == 2
    assert store.insert_page(page) == 0  # reentrant: same page again is a no-op
    assert store.count() == 2


def test_cursors_and_reopen_resume(tmp_path: Path) -> None:
    store = ChatStore(tmp_path, THREAD)
    assert store.oldest() is None and store.newest() is None  # empty store
    store.insert_page([_msg("2", "2026-07-02T10:00:00Z"), _msg("1", "2026-07-01T10:00:00Z")])
    store.close()

    reopened = ChatStore(tmp_path, THREAD)  # crash/restart: cursors derive from the data
    assert reopened.oldest() == "2026-07-01T10:00:00Z"
    assert reopened.newest() == "2026-07-02T10:00:00Z"


def test_insert_page_skips_message_without_id(tmp_path: Path) -> None:
    store = ChatStore(tmp_path, THREAD)
    assert store.insert_page([{"composetime": "2026-07-01T10:00:00Z"}]) == 0
    assert store.count() == 0


def test_index_upsert_preserves_backfill_state(tmp_path: Path) -> None:
    index = Index(tmp_path)
    index.upsert_chat(THREAD, label="Alice", participants=[{"mri": "8:orgid:x", "name": "Alice"}])
    index.mark_backfill_done(THREAD)
    index.touch(THREAD, "2026-07-22T10:00:00Z")

    index.upsert_chat(THREAD, label="Alice Renamed")  # re-enumeration must not reset state
    (chat,) = index.chats()
    assert chat["label"] == "Alice Renamed"
    assert chat["backfill_done"] is True
    assert chat["last_fetch_at"] == "2026-07-22T10:00:00Z"
    assert chat["participants"] == [{"mri": "8:orgid:x", "name": "Alice"}]
    assert index.backfill_done(THREAD)
    assert not index.backfill_done("19:unknown@thread.v2")


def test_index_survives_reopen(tmp_path: Path) -> None:
    index = Index(tmp_path)
    index.upsert_chat(THREAD, label="x")
    index.close()
    assert Index(tmp_path).chats()[0]["id"] == THREAD


NOW = "2026-07-26T00:00:00Z"
GONE = "https://api.asm.skype.com/v1/objects/gone"


def test_denied_assets_roundtrip(tmp_path) -> None:
    store = ChatStore(tmp_path, "19:x@thread.v2")
    assert store.denied_urls(NOW) == set()
    store.mark_denied(GONE, 403, "2026-07-24T00:00:00Z")
    store.mark_denied(GONE, 403, "2026-07-25T00:00:00Z")  # idempotent
    assert store.denied_urls(NOW) == {GONE}
    store.close()
    # Survives reopen (that's the whole point) — and old DBs get the table via IF NOT EXISTS.
    store2 = ChatStore(tmp_path, "19:x@thread.v2")
    assert store2.denied_urls(NOW) == {GONE}
    store2.close()


def test_retry_after_is_comparable_to_now_iso() -> None:
    """denied_urls compares these as SQL strings: with two formats, punctuation decides, not time."""
    from datetime import datetime

    from miniteams.archive import _now_iso, _retry_after

    fmt = "%Y-%m-%dT%H:%M:%SZ"
    now, due = _now_iso(), _retry_after(1)
    # Both must parse under the one format, or lexicographic ordering stops tracking chronology.
    datetime.strptime(now, fmt)
    datetime.strptime(due, fmt)
    assert due > now  # a fresh backoff is in the future under plain string comparison
    assert _retry_after(1) < _retry_after(5)  # the ladder orders correctly as strings too


def test_denied_backoff_expires_but_403_never_does(tmp_path) -> None:
    """A 404 must come back once its delay elapses; a 403 must not — that is the whole split."""
    store = ChatStore(tmp_path, "19:x@thread.v2")
    store.mark_denied(f"{GONE}#.json", 404, NOW, attempts=2, retry_after="2026-07-26T06:00:00Z")
    store.mark_denied(f"{GONE}#.vtt", 403, NOW)  # permanent: retry_after stays ''
    assert store.denied_urls("2026-07-26T05:00:00Z") == {f"{GONE}#.json", f"{GONE}#.vtt"}
    assert store.denied_urls("2026-07-26T07:00:00Z") == {f"{GONE}#.vtt"}  # backoff elapsed
    assert store.denied_attempts(f"{GONE}#.json") == 2
    assert store.denied_attempts("never-seen") == 0
    store.close()


def test_denied_assets_migrates_pre_backoff_schema(tmp_path) -> None:
    """Rows written before the backoff columns existed are 403s: they must stay permanent."""
    import sqlite3

    # Let ChatStore create the dir/db (its path derivation is the thing under test's neighbour),
    # then roll the table back to the pre-backoff shape and reopen.
    ChatStore(tmp_path, "19:x@thread.v2").close()
    con = sqlite3.connect(next(tmp_path.glob("*/messages.db")))
    con.execute("DROP TABLE denied_assets")
    con.execute(
        "CREATE TABLE denied_assets (url TEXT PRIMARY KEY, status INTEGER NOT NULL, at TEXT NOT NULL)"
    )
    con.execute("INSERT INTO denied_assets VALUES (?, 403, ?)", (GONE, NOW))
    con.commit()
    con.close()

    store = ChatStore(tmp_path, "19:x@thread.v2")
    assert store.denied_urls("2099-01-01T00:00:00Z") == {GONE}  # still suppressed far in the future
    store.close()
