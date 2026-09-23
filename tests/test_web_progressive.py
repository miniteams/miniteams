"""Progressive start (spec 004 phase 2): serve on the seed, fill from the listing, spin meanwhile."""

from typing import Any

import pytest

from miniteams import web as W
from miniteams.config import Settings
from miniteams.directory import Directory

ME = "8:orgid:me"


@pytest.fixture
def quiet_directory(directory: Directory, monkeypatch: pytest.MonkeyPatch) -> Directory:
    directory.me = ME

    async def fake_label(self: Directory, thread_id: str) -> str:
        return f"fetched:{thread_id}"

    async def fake_display(self: Directory, mri: str) -> str:
        return f"name:{mri}"

    monkeypatch.setattr(Directory, "label", fake_label)
    monkeypatch.setattr(Directory, "display", fake_display)
    return directory


def _seed(thread_id: str, when: str, **kw: Any) -> dict[str, Any]:
    return {
        "label": kw.get("label", f"Seeded {thread_id}"),
        "text": kw.get("text", "from the archive"),
        "sender": kw.get("sender", "Jean Martin"),
        "read_id": kw.get("read_id", ""),
        "read_at": kw.get("read_at", 0),
        "last_activity": when,
        "last_id": kw.get("last_id", "1"),
    }


def _conv(thread_id: str, when: str, **last: Any) -> dict[str, Any]:
    return {"id": thread_id, "lastMessage": {"composetime": when, **last}}


# --- depth ---


def test_resolve_limit_prefers_the_flag_including_zero() -> None:
    assert W.resolve_limit(None, seeded=True) == W._LIMIT_SEEDED
    assert W.resolve_limit(None, seeded=False) == W._LIMIT_BARE
    assert W.resolve_limit(0, seeded=True) == 0  # explicit "no limit" is not "unset"
    assert W.resolve_limit(7, seeded=False) == 7


# --- the first paint ---


def test_seeded_rows_are_the_newest_first_and_capped() -> None:
    seed = {
        "19:old@thread.v2": _seed("19:old@thread.v2", "2026-01-01T00:00:00Z"),
        "19:new@thread.v2": _seed("19:new@thread.v2", "2026-09-20T00:00:00Z"),
        "19:mid@thread.v2": _seed("19:mid@thread.v2", "2026-05-05T00:00:00Z"),
        "19:never@thread.v2": _seed("19:never@thread.v2", ""),  # no activity: not a row
    }
    rows = W.seeded_rows(seed, limit=2)
    assert list(rows) == ["19:new@thread.v2", "19:mid@thread.v2"]
    assert rows["19:new@thread.v2"]["text"] == "from the archive"
    assert W.seeded_rows(seed, limit=0).keys() == {
        "19:old@thread.v2",
        "19:new@thread.v2",
        "19:mid@thread.v2",
    }


def test_seeded_rows_carry_the_unread_state() -> None:
    seed = {
        "19:a@thread.v2": _seed("19:a@thread.v2", "2026-09-20T00:00:00Z", last_id="9", read_id="5"),
        "19:b@thread.v2": _seed("19:b@thread.v2", "2026-09-19T00:00:00Z", last_id="5", read_id="5"),
    }
    rows = W.seeded_rows(seed, limit=0)
    assert rows["19:a@thread.v2"]["unread"] is True
    assert rows["19:b@thread.v2"]["unread"] is False


def test_listed_keeps_in_scope_conversations_and_stops_at_the_cap(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    pages = [
        [
            _conv("19:a@thread.v2", "2026-09-20T10:00:00Z"),
            _conv("19:chan@thread.tacv2", "2026-09-20T09:00:00Z"),  # a channel is not widget material
            {"id": "19:noactivity@thread.v2"},  # never active: nothing to sort it by
            _conv("19:b@thread.v2", "2026-09-20T08:00:00Z"),
        ],
        [_conv("19:c@thread.v2", "2026-09-19T10:00:00Z")],
    ]
    monkeypatch.setattr(W, "fetch_conversations", lambda *a: iter(pages))
    assert [c["id"] for c in W._listed(settings, "sk", 0)] == [
        "19:a@thread.v2",
        "19:b@thread.v2",
        "19:c@thread.v2",
    ]
    assert [c["id"] for c in W._listed(settings, "sk", 2)] == ["19:a@thread.v2", "19:b@thread.v2"]


# --- the background fill ---


async def test_fill_paints_the_head_then_the_tail_and_stops_the_spinner(
    quiet_directory: Directory, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setattr(W, "_FIRST_CHUNK", 2)
    monkeypatch.setattr(W, "_MENTION_SCAN", 2)
    scanned: list[str] = []

    def fake_history(s: Any, token: str, thread_id: str, page_size: int, max_pages: int) -> list[Any]:
        scanned.append(thread_id)
        return []

    monkeypatch.setattr(W, "fetch_history", fake_history)
    convs = [_conv(f"19:c{i}@thread.v2", f"2026-09-{20 - i:02d}T10:00:00Z", id=str(i)) for i in range(5)]
    monkeypatch.setattr(W, "_listed", lambda *a: convs)
    board = W.Board({}, quiet_directory, busy=True)
    seen_sizes: list[int] = []
    monkeypatch.setattr(board, "broadcast", lambda: seen_sizes.append(len(board.rows)))

    await W.fill_from_listing(board, settings, "sk", quiet_directory, 0, ME, {}, {})

    assert seen_sizes == [2, 5, 5]  # head, tail, then the spinner going off
    assert board.busy is False
    assert len(board.rows) == 5
    # The tail continues at the head's rank, so the mention scan is not paid twice.
    assert set(scanned) == {"19:c0@thread.v2", "19:c1@thread.v2"}
    assert len(scanned) == 2


async def test_the_merged_table_never_outgrows_the_cap(
    quiet_directory: Directory, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    # An archive that lags knows other chats than the listing does; without a prune the table would
    # hold both sets and every broadcast would carry twice the rows.
    cap = 4
    seed = {
        f"19:old{i}@thread.v2": _seed(f"19:old{i}@thread.v2", f"2026-09-0{i}T10:00:00Z") for i in range(1, 6)
    }
    convs = [_conv(f"19:new{i}@thread.v2", f"2026-09-2{i}T10:00:00Z", id="1") for i in range(1, 5)]
    monkeypatch.setattr(W, "_listed", lambda *a: convs)
    board = W.Board(W.seeded_rows(seed, cap), quiet_directory, busy=True)
    assert len(board.rows) == cap

    await W.fill_from_listing(board, settings, "sk", quiet_directory, cap, "", {}, seed)

    assert len(board.rows) == cap
    assert all(tid.startswith("19:new") for tid in board.rows)


async def test_prune_spares_a_chat_the_stream_brought_in_during_the_walk(
    quiet_directory: Directory, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    convs = [_conv("19:listed@thread.v2", "2026-09-20T10:00:00Z", id="1")]
    monkeypatch.setattr(W, "_listed", lambda *a: convs)
    board = W.Board({}, quiet_directory, busy=True)
    # A message arriving mid-walk creates its row with a timestamp past the fill's start.
    board.rows["19:arrived@thread.v2"] = {
        "id": "19:arrived@thread.v2",
        "last_activity": "2099-01-01T00:00:00.000Z",
        "typing": [],
    }

    await W.fill_from_listing(board, settings, "sk", quiet_directory, 0, "", {}, {})

    assert set(board.rows) == {"19:listed@thread.v2", "19:arrived@thread.v2"}


async def test_a_failed_fill_keeps_the_seeded_table_and_clears_the_spinner(
    quiet_directory: Directory, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    def boom(*a: Any) -> list[Any]:
        raise RuntimeError("listing is down")

    monkeypatch.setattr(W, "_listed", boom)
    seeded = W.seeded_rows({"19:a@thread.v2": _seed("19:a@thread.v2", "2026-09-20T00:00:00Z")}, 0)
    board = W.Board(seeded, quiet_directory, busy=True)

    await W.fill_from_listing(board, settings, "sk", quiet_directory, 0, "", {}, {})

    assert board.busy is False  # a spinner that never stops is worse than no spinner
    assert list(board.rows) == ["19:a@thread.v2"]  # the archive's rows are still on screen


async def test_the_fill_never_moves_a_row_backwards(
    quiet_directory: Directory, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    # A message landing while the listing is being walked is newer than anything the walk saw.
    monkeypatch.setattr(W, "_listed", lambda *a: [_conv("19:a@thread.v2", "2026-09-20T10:00:00Z", id="1")])
    board = W.Board(
        W.seeded_rows({"19:a@thread.v2": _seed("19:a@thread.v2", "2026-09-20T10:00:00Z")}, 0),
        quiet_directory,
        busy=True,
    )
    board.rows["19:a@thread.v2"]["last_activity"] = "2026-09-23T23:59:00Z"  # the live stream
    board.rows["19:a@thread.v2"]["text"] = "just arrived"

    await W.fill_from_listing(board, settings, "sk", quiet_directory, 0, "", {}, {})

    assert board.rows["19:a@thread.v2"]["text"] == "just arrived"


def test_replace_keeps_typing_seen_and_muted(quiet_directory: Directory, tmp_path: Any) -> None:
    board = W.Board({}, quiet_directory, seen_path=tmp_path / "seen.json", muted_path=tmp_path / "muted.json")
    board._seen["19:a@thread.v2"] = "2026-09-20T10:00:00Z"
    board._muted["19:a@thread.v2"] = "2026-09-20T10:00:00Z"
    board.rows["19:a@thread.v2"] = {
        "id": "19:a@thread.v2",
        "last_activity": "2026-09-20T09:00:00Z",
        "typing": ["Jean Martin"],
    }
    fresh = W.seeded_rows({"19:a@thread.v2": _seed("19:a@thread.v2", "2026-09-20T10:00:00Z")}, 0)

    board.replace(fresh, busy=True)

    row = board.rows["19:a@thread.v2"]
    assert row["typing"] == ["Jean Martin"]  # only the page knows who is typing
    assert row["seen_at"] == "2026-09-20T10:00:00Z"
    assert row["muted"] is True
    assert board.busy is True


def test_the_payload_tells_the_page_whether_it_is_still_filling(quiet_directory: Directory) -> None:
    import json

    board = W.Board({}, quiet_directory, busy=True)
    assert json.loads(board.payload())["busy"] is True
    board.set_busy(False)
    assert json.loads(board.payload())["busy"] is False
