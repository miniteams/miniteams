"""Archive storage layer: dedup, cursors, reopen-resume, dir sanitization (spec 001 phase 1)."""

import json
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


# --- live writes (spec 005) ---

NOW = "2026-09-23T10:00:00Z"


def _versions(store: ChatStore) -> list[tuple[str, str]]:
    return store._db.execute("SELECT id, version FROM message_versions ORDER BY version").fetchall()


def _raw(store: ChatStore, mid: str) -> dict[str, Any]:
    (raw,) = store._db.execute("SELECT raw FROM messages WHERE id = ?", (mid,)).fetchone()
    return dict(json.loads(raw))


def test_apply_message_inserts_then_replaces_newer_version(tmp_path: Path) -> None:
    store = ChatStore(tmp_path, THREAD)
    assert store.apply_message(_msg("1", "2026-09-23T09:00:00Z", version="100"), NOW) == "new"
    edited = _msg("1", "2026-09-23T09:00:00Z", version="200", properties={"edittime": "200"})
    edited["content"] = "edited"
    assert store.apply_message(edited, NOW) == "updated"
    raw = _raw(store, "1")
    assert raw["content"] == "edited" and raw["composetime"] == "2026-09-23T09:00:00Z"
    assert _versions(store) == [("1", "100")]
    assert store.count() == 1


def test_apply_message_ignores_same_or_older_version(tmp_path: Path) -> None:
    store = ChatStore(tmp_path, THREAD)
    store.apply_message(_msg("1", "2026-09-23T09:00:00Z", version="200"), NOW)
    assert store.apply_message({"id": "1", "version": "200", "content": "x"}, NOW) == "stale"
    assert store.apply_message({"id": "1", "version": "150", "content": "x"}, NOW) == "stale"
    assert store.apply_message({"id": "1", "content": "no version"}, NOW) == "stale"
    assert _raw(store, "1")["content"] == "msg 1"
    assert _versions(store) == [("1", "150")]  # older is history; same or unversioned is noise


def test_apply_message_delete_keeps_original_content(tmp_path: Path) -> None:
    store = ChatStore(tmp_path, THREAD)
    store.apply_message(_msg("1", "2026-09-23T09:00:00Z", version="100", properties={"a": 1}), NOW)
    store.apply_message({"id": "1", "version": "300", "content": "", "properties": {"deletetime": "3"}}, NOW)
    raw = _raw(store, "1")
    assert raw["content"] == "" and raw["properties"] == {"deletetime": "3"}
    (old,) = store._db.execute("SELECT raw FROM message_versions WHERE id = '1'").fetchone()
    assert json.loads(old)["content"] == "msg 1"


def test_apply_message_over_history_row_without_version(tmp_path: Path) -> None:
    store = ChatStore(tmp_path, THREAD)
    store.insert_page([_msg("1", "2026-09-23T09:00:00Z")])  # pass-stored, no version field
    assert store.apply_message({"id": "1", "version": "5", "content": "edited"}, NOW) == "updated"
    assert store.apply_message({"composetime": NOW}, NOW) == "stale"  # no id


def test_message_versions_survive_reopen(tmp_path: Path) -> None:
    store = ChatStore(tmp_path, THREAD)
    store.apply_message(_msg("1", NOW, version="1"), NOW)
    store.apply_message({"id": "1", "version": "2"}, NOW)
    store.close()
    assert _versions(ChatStore(tmp_path, THREAD)) == [("1", "1")]


def test_merge_raw_moves_horizon_and_keeps_other_keys(tmp_path: Path) -> None:
    index = Index(tmp_path)
    conv = {
        "id": THREAD,
        "version": 1,
        "properties": {"consumptionhorizon": "1;1;1", "addedBy": "x"},
        "lastMessage": {"id": "1", "composetime": "2026-09-23T09:00:00Z"},
    }
    index.upsert_chat(THREAD, label="Alice", raw=conv)
    index.merge_raw(THREAD, {"properties": {"consumptionhorizon": "2;2;2"}})
    (chat,) = index.chats()
    assert chat["raw"]["properties"] == {"consumptionhorizon": "2;2;2", "addedBy": "x"}
    assert chat["raw"]["version"] == 1 and chat["label"] == "Alice"
    assert chat["raw"]["lastMessage"]["id"] == "1"


def test_merge_raw_last_message_only_when_newer(tmp_path: Path) -> None:
    index = Index(tmp_path)
    index.upsert_chat(THREAD, raw={"lastMessage": {"id": "2", "composetime": "2026-09-23T09:00:00Z"}})
    index.merge_raw(THREAD, {"lastMessage": {"id": "1", "composetime": "2026-09-23T08:00:00Z"}})
    assert index.chats()[0]["raw"]["lastMessage"]["id"] == "2"
    index.merge_raw(THREAD, {"lastMessage": {"id": "3", "composetime": "2026-09-23T10:00:00Z"}})
    assert index.chats()[0]["raw"]["lastMessage"] == {"id": "3", "composetime": "2026-09-23T10:00:00Z"}


def test_merge_raw_creates_unknown_chat(tmp_path: Path) -> None:
    index = Index(tmp_path)
    index.merge_raw("19:new@thread.v2", {"lastMessage": {"id": "1", "composetime": NOW}})
    index.ensure_chat("19:new@thread.v2")  # idempotent
    (chat,) = index.chats()
    assert chat["id"] == "19:new@thread.v2" and chat["dir"] == "19:new@thread.v2"
    assert chat["raw"]["lastMessage"]["id"] == "1" and not chat["backfill_done"]


def test_apply_message_keeps_a_late_older_version(tmp_path: Path) -> None:
    store = ChatStore(tmp_path, THREAD)
    store.apply_message(_msg("1", NOW, version="200", content="edited"), NOW)
    assert store.apply_message(_msg("1", NOW, version="100"), NOW) == "stale"
    assert _raw(store, "1")["content"] == "edited"
    assert _versions(store) == [("1", "100")]


def test_apply_message_full_update_drops_a_removed_reaction(tmp_path: Path) -> None:
    store = ChatStore(tmp_path, THREAD)
    liked = {"emotions": [{"key": "like", "users": [{"mri": "8:x"}]}]}
    store.apply_message(_msg("1", NOW, version="1", properties=liked), NOW)
    store.apply_message(_msg("1", NOW, version="2", properties={}), NOW)  # reaction removed
    assert _raw(store, "1")["properties"] == {}


def test_apply_message_partial_update_merges(tmp_path: Path) -> None:
    store = ChatStore(tmp_path, THREAD)
    store.apply_message(_msg("1", NOW, version="1", properties={"a": 1}), NOW)
    store.apply_message({"id": "1", "version": "2", "properties": {"b": 2}}, NOW)  # no content
    raw = _raw(store, "1")
    assert raw["content"] == "msg 1" and raw["properties"] == {"a": 1, "b": 2}
