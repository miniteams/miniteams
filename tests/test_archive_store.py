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


def test_denied_assets_roundtrip(tmp_path) -> None:
    store = ChatStore(tmp_path, "19:x@thread.v2")
    assert store.denied_urls() == set()
    store.mark_denied("https://api.asm.skype.com/v1/objects/gone", 403, "2026-07-24T00:00:00Z")
    store.mark_denied("https://api.asm.skype.com/v1/objects/gone", 403, "2026-07-25T00:00:00Z")  # idempotent
    assert store.denied_urls() == {"https://api.asm.skype.com/v1/objects/gone"}
    store.close()
    # Survives reopen (that's the whole point) — and old DBs get the table via IF NOT EXISTS.
    store2 = ChatStore(tmp_path, "19:x@thread.v2")
    assert store2.denied_urls() == {"https://api.asm.skype.com/v1/objects/gone"}
    store2.close()
