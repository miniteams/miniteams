"""Chat sections (spec 026), server side: the store, its file and the board verbs."""

import asyncio
import errno
import json
import logging
from pathlib import Path
from typing import Any

import pytest
import structlog.testing
from conftest import board_socket
from test_web import _free_port

from miniteams import web as W
from miniteams.config import Settings
from miniteams.sections import MAX_SECTIONS, NAME_MAX, Sections, clean_name, load

A, B, C = "19:a@thread.v2", "19:b@thread.v2", "19:c@thread.v2"


def _rows(*ids: str) -> dict[str, dict[str, Any]]:
    """Rows whose activity follows the argument order: the first is the oldest."""
    return {tid: {"id": tid, "last_activity": f"2026-10-17T10:00:0{i}Z"} for i, tid in enumerate(ids)}


def _store(path: Path | None = None, rows: dict[str, dict[str, Any]] | None = None) -> Sections:
    table = _rows(A, B, C) if rows is None else rows
    return Sections(path, lambda: table)


def _names(store: Sections) -> list[str]:
    return [s["name"] for s in store.items]


def _chats(store: Sections) -> list[list[str]]:
    return [s["chats"] for s in store.items]


# --- names and creation ---


def test_name_is_one_line_bounded_plain_text() -> None:
    """026-AC11: non-empty after strip, bounded, kept verbatim as text (markup is the page's to escape)."""
    assert clean_name("  Work\n\tstuff  ") == "Work stuff"
    assert clean_name("<b>x</b> & y") == "<b>x</b> & y"
    assert clean_name("x" * NAME_MAX) == "x" * NAME_MAX
    # Format and control characters (bidi overrides, zero-width, NUL) never reach the page.
    assert clean_name("\u202eabc") == "abc"
    assert clean_name("a\x00b\u200bc") == "abc"
    assert clean_name("a \u200b b") == "a b"  # stripped first, then the spaces collapse
    assert clean_name("a\u200b \u2066 \u200bb") == "a b"
    for stripped in ("\u202a\u202b\u202c\u202d", "\u2066\u2067\u2068\u2069", "\u200e\u200f", "\u2060\ufeff"):
        assert clean_name(f"a{stripped}b") == "ab"
    # Joiners and tag characters are part of the text: emoji families, subdivision flags, Indic, Persian.
    for kept in (
        "\U0001f468\u200d\U0001f469\u200d\U0001f467",
        "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
        "\u0915\u094d\u200d\u0937",
        "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",
    ):
        assert clean_name(kept) == kept
    for bad in (
        "",
        "   ",
        "\n",
        "x" * (NAME_MAX + 1),
        None,
        3,
        ["a"],
        "\u200b",
        "\x00\x07",
        " \u202e ",
        "\u200d",
        "\u200c",
        "\U000e0067",
        " \u200d\u200c ",
    ):
        assert clean_name(bad) is None


def test_create_files_the_chat_on_top_and_refuses_bad_input() -> None:
    """026-AC1, 026-AC7, 026-AC11: New section… files the chat; a bad name or unknown chat is refused."""
    store = _store()
    assert store.create("Team", A) is True
    assert [(s["name"], s["order"], s["chats"]) for s in store.items] == [("Team", "recent", [A])]
    assert store.create("Later") is True and store.items[1]["chats"] == []  # last, before Other
    assert store.items[0]["id"] != store.items[1]["id"]
    for name, chat in (("", None), ("ok", "19:unknown@thread.v2"), ("ok", ["x"]), ("ok", 5), (7, None)):
        assert store.create(name, chat) is False
    assert len(store.items) == 2


def test_create_is_bounded() -> None:
    """026-AC11: the file stays bounded whatever the page sends."""
    store = _store()
    for i in range(MAX_SECTIONS):
        assert store.create(f"s{i}")
    assert store.create("one more") is False


# --- managing sections ---


def test_rename_and_unknown_section_refused() -> None:
    """026-AC7, 026-AC11."""
    store = _store()
    store.create("Old")
    sid = store.items[0]["id"]
    assert store.rename(sid, "  New ") is True and store.items[0]["name"] == "New"
    assert store.rename(sid, "") is False and store.items[0]["name"] == "New"
    assert store.rename("nope", "X") is False
    assert store.rename(["unhashable"], "X") is False


def test_move_section_up_down_and_to_an_index() -> None:
    """026-AC7: move up/down; `to` for phase 2's drag, clamped; Other is not stored so never moves."""
    store = _store()
    for name in ("one", "two", "three"):
        store.create(name)
    one, three = store.items[0]["id"], store.items[2]["id"]
    assert store.move(one, by=1) and _names(store) == ["two", "one", "three"]
    assert store.move(one, by=-1) and _names(store) == ["one", "two", "three"]
    assert store.move(one, by=-1) is False  # already first
    assert store.move(three, by=1) is False  # already last
    assert store.move(three, to=0) and _names(store) == ["three", "one", "two"]
    assert store.move(three, to=99) and _names(store) == ["one", "two", "three"]
    assert store.move(three, to=-5) and _names(store) == ["three", "one", "two"]
    assert store.move(three, to=10**30) and _names(store) == ["one", "two", "three"]
    for by, to in ((2, None), (True, None), (1.0, None), (None, "1"), (None, True), (None, None)):
        assert store.move(one, by, to) is False
    assert store.move("nope", by=1) is False


def test_delete_sends_its_chats_to_other() -> None:
    """026-AC7: deleting a section unfiles its chats; another section is untouched."""
    store = _store()
    store.create("one", A)
    store.create("two", B)
    one = store.items[0]["id"]
    assert store.file(C, one) is True
    assert store.delete(one) is True
    assert [s["name"] for s in store.items] == ["two"] and _chats(store) == [[B]]
    assert store.file(A, store.items[0]["id"]) is True  # free to file again
    assert store.delete(one) is False


# --- filing ---


def test_a_chat_is_in_one_section_at_most() -> None:
    """026-AC7: filing moves the chat out of its old section; None unfiles it."""
    store = _store()
    store.create("one", A)
    store.create("two")
    one, two = (s["id"] for s in store.items)
    assert store.file(A, two) is True and _chats(store) == [[], [A]]
    assert store.file(A, None) is True and _chats(store) == [[], []]
    assert store.file(A, None) is True and _chats(store) == [[], []]  # already in none: nothing changes
    assert store.file(A, one) is True and _chats(store) == [[A], []]


def test_file_refuses_unknown_chat_unknown_section_and_bad_position() -> None:
    """026-AC11."""
    store = _store()
    store.create("one")
    sid = store.items[0]["id"]
    assert store.file("19:unknown@thread.v2", sid) is False
    assert store.file(5, sid) is False
    assert store.file(A, "nope") is False
    for at in ("0", 1.5, True):
        assert store.file(A, sid, at) is False
    assert _chats(store) == [[]]


def test_filed_chat_lands_on_top_or_at_the_given_position() -> None:
    """026-AC12: a newly filed chat goes on top; `at` places it (phase 2's drop), clamped."""
    store = _store()
    store.create("one")
    sid = store.items[0]["id"]
    store.set_order(sid, "manual")
    store.file(A, sid)
    store.file(B, sid)
    assert _chats(store) == [[B, A]]
    assert store.file(C, sid, at=1) and _chats(store) == [[B, C, A]]
    assert store.file(C, sid) and _chats(store) == [[B, C, A]]  # filed again: keeps its place
    assert store.file(C, sid, at=99) and _chats(store) == [[B, A, C]]
    assert store.file(C, sid, at=-3) and _chats(store) == [[C, B, A]]
    assert store.file(A, sid, at=-1) and _chats(store) == [[A, C, B]]  # -1 is first, not "before last"
    # A JSON big int is a valid int: clamped, never an OverflowError half-way through a move.
    assert store.file(A, sid, at=10**30) and _chats(store) == [[C, B, A]]
    assert store.file(A, sid, at=-(10**30)) and _chats(store) == [[A, C, B]]


# --- manual order ---


def test_order_switch_freezes_recency_and_is_validated() -> None:
    """026-AC12: Manual starts from the order the user sees (newest first, absent chats last)."""
    rows = _rows(A, B, C)
    store = _store(rows=rows)
    store.create("one")
    sid = store.items[0]["id"]
    for chat in (B, C, A):
        store.file(chat, sid, at=99)
    rows.pop(B)  # off the board for now: kept, sorted last
    assert store.set_order(sid, "manual") is True
    assert store.items[0]["order"] == "manual" and _chats(store) == [[C, A, B]]
    assert store.set_order(sid, "recent") is True and store.items[0]["order"] == "recent"
    for bad in ("Manual", "", None, 1):
        assert store.set_order(sid, bad) is False
    assert store.set_order("nope", "manual") is False


def test_move_chat_within_a_manual_section_only() -> None:
    """026-AC12: Alt+↑/↓ (by) and drag (to) inside a Manual section; a Recent one refuses."""
    store = _store()
    store.create("one")
    sid = store.items[0]["id"]
    for chat in (C, B, A):
        store.file(chat, sid)
    assert _chats(store) == [[A, B, C]]
    assert store.move_chat(A, by=1) is False  # still Recent
    store.set_order(sid, "manual")
    assert _chats(store) == [[C, B, A]]  # frozen by recency
    assert store.move_chat(C, by=1) and _chats(store) == [[B, C, A]]
    assert store.move_chat(C, by=-1) and _chats(store) == [[C, B, A]]
    assert store.move_chat(C, by=-1) is False
    assert store.move_chat(C, to=2) and _chats(store) == [[B, A, C]]
    assert store.move_chat(B, to=10**30) and _chats(store) == [[A, C, B]]
    for chat, by, to in ((C, 2, None), (C, None, "0"), ("19:unfiled@thread.v2", 1, None), (5, 1, None)):
        assert store.move_chat(chat, by, to) is False


# --- the file ---


def test_round_trip_keeps_chats_absent_from_the_board(tmp_path: Path) -> None:
    """026-AC10: a restart reloads the file; a chat off the (bounded) board keeps its section."""
    path = tmp_path / "sections.json"
    store = _store(path)
    store.create("one", A)
    store.create("two", B)
    store.set_order(store.items[1]["id"], "manual")
    store.save()
    assert path.stat().st_mode & 0o777 == 0o600
    again = _store(path, rows=_rows(B))  # A has left the board
    assert again.items == store.items


def test_missing_file_is_no_section_and_silent(tmp_path: Path) -> None:
    """026-AC10: a first run has no file."""
    with structlog.testing.capture_logs() as logs:
        assert load(tmp_path / "sections.json") == [] and load(None) == []
    assert logs == []


@pytest.mark.parametrize("text", ["{not json", '{"id": "x"}', "null", "\udcff"])
def test_corrupt_file_is_no_section_and_logged(tmp_path: Path, text: str) -> None:
    """A corrupt sections.json must not stop the board."""
    path = tmp_path / "sections.json"
    path.write_bytes(text.encode("utf-8", "surrogateescape"))
    with structlog.testing.capture_logs() as logs:
        assert load(path) == []
    assert [entry["event"] for entry in logs] == ["sections_unreadable"]
    # Set aside, not overwritten by the next save: a hand edit's typo stays recoverable.
    assert not path.exists()
    assert (tmp_path / "sections.json.bad").read_bytes() == text.encode("utf-8", "surrogateescape")


def test_too_deep_a_file_is_set_aside(tmp_path: Path) -> None:
    """json.loads recurses: a deeply nested file must be unreadable, not a crash."""
    path = tmp_path / "sections.json"
    path.write_text("[" * 200_000 + "]" * 200_000)
    assert load(path) == []
    assert (tmp_path / "sections.json.bad").exists()


def test_malformed_entries_are_dropped_one_by_one(tmp_path: Path) -> None:
    """A bad entry costs that entry only; a chat saved twice stays in its first section."""
    path = tmp_path / "sections.json"
    good = {"id": "s1", "name": "One", "order": "manual", "chats": [A, B, A, 5]}
    path.write_text(
        json.dumps(
            [
                good,
                {"id": "s1", "name": "Dup id", "order": "recent", "chats": []},
                {"id": "s2", "name": "", "order": "recent", "chats": []},
                {"id": "s3", "name": "Bad order", "order": "sideways", "chats": []},
                {"id": "s4", "name": "Bad chats", "order": "recent", "chats": "x"},
                {"id": 5, "name": "Bad id", "order": "recent", "chats": []},
                "not a section",
                {"id": "s5", "name": "Two", "order": "recent", "chats": [B, C]},
            ]
        )
    )
    with structlog.testing.capture_logs() as logs:
        sections = load(path)
    assert sections == [
        {"id": "s1", "name": "One", "order": "manual", "chats": [A, B]},
        {"id": "s5", "name": "Two", "order": "recent", "chats": [C]},
    ]
    assert [entry["event"] for entry in logs] == ["section_dropped"] * 6 + ["sections_set_aside"]
    assert (tmp_path / "sections.json.bad").read_text() == path.read_text()  # the next save overwrites it


def _entry(sid: str, name: str = "One", chats: list[Any] | None = None) -> dict[str, Any]:
    return {"id": sid, "name": name, "order": "recent", "chats": chats or []}


@pytest.mark.parametrize(
    ("data", "loaded"),
    [
        ([_entry("s1"), "not a section"], 1),
        ([_entry("s1"), {"id": "s2", "name": "x", "order": "sideways", "chats": []}], 1),
        ([_entry("s1", chats=[A, 5, A]), _entry("s2", chats=[A, B])], 2),  # bad and repeated chats
        ([_entry("s1", "  One\u200b ")], 1),  # a name that cleaning changes
        ([_entry(f"s{i}") for i in range(MAX_SECTIONS + 5)], MAX_SECTIONS),
    ],
)
def test_a_file_load_changes_keeps_a_copy_aside(tmp_path: Path, data: list[Any], loaded: int) -> None:
    """Whatever load does not keep as written survives the next save as sections.json.bad."""
    path = tmp_path / "sections.json"
    path.write_text(json.dumps(data))
    with structlog.testing.capture_logs() as logs:
        assert len(load(path)) == loaded
    assert "sections_set_aside" in [entry["event"] for entry in logs]
    assert (tmp_path / "sections.json.bad").read_text() == path.read_text()


def test_a_clean_file_is_not_copied(tmp_path: Path) -> None:
    path = tmp_path / "sections.json"
    path.write_text(json.dumps([_entry("s1", chats=[A]), _entry("s2", "Two", [B])]))
    assert len(load(path)) == 2 and not (tmp_path / "sections.json.bad").exists()


# --- board verbs ---


async def test_verbs_reach_every_open_view_and_survive_a_restart(settings: Settings) -> None:
    """026-AC10: a change from one view shows in the other without a reload, and outlives the server."""
    path = settings.config_dir / "sections.json"
    board = W.Board(_rows(A, B, C), sections_path=path)
    port = _free_port()
    task = asyncio.create_task(W.serve_board(board, settings, f"127.0.0.1:{port}"))
    try:
        await asyncio.sleep(0.2)
        async with (
            board_socket(f"127.0.0.1:{port}", board.secret) as one,
            board_socket(f"127.0.0.1:{port}", board.secret) as two,
        ):
            assert json.loads(await one.recv())["sections"] == []
            await two.recv()

            async def both(verb: dict[str, Any]) -> list[dict[str, Any]]:
                await one.send(json.dumps(verb))
                frame = json.loads(await asyncio.wait_for(two.recv(), 2))
                assert json.loads(await asyncio.wait_for(one.recv(), 2)) == frame
                assert frame["rows"][0]["id"] == C  # the row shape rides on unchanged
                return list(frame["sections"])

            [made] = await both({"section_new": "Team", "chat": A})
            sid = made["id"]
            assert made == {"id": sid, "name": "Team", "order": "recent", "chats": [A]}
            await both({"section_new": "Later"})
            assert [s["name"] for s in await both({"section_move": sid, "by": 1})] == ["Later", "Team"]
            assert (await both({"section_rename": sid, "name": "Crew"}))[1]["name"] == "Crew"
            assert (await both({"section_order": sid, "order": "manual"}))[1]["order"] == "manual"
            assert (await both({"section_file": B, "section": sid}))[1]["chats"] == [B, A]
            assert (await both({"section_chat_move": B, "by": 1}))[1]["chats"] == [A, B]
            assert (await both({"section_file": B, "section": None}))[1]["chats"] == [A]
            later = (await both({"section_move": sid, "to": 0}))[1]["id"]
            assert [s["name"] for s in await both({"section_delete": later})] == ["Crew"]
            # Refused, malformed or no-op: no frame, and the socket stays up for the next verb.
            for verb in (
                {"section_new": ""},
                {"section_new": "x", "chat": "19:unknown@thread.v2"},
                {"section_rename": "nope", "name": "x"},
                {"section_order": sid, "order": 1},
                {"section_move": sid, "by": 1},  # alone: nowhere to go
                {"section_file": A, "section": "nope"},
                {"section_file": 5, "section": sid},
                {"section_chat_move": A, "by": "up"},
                {"section_delete": ["x"]},
            ):
                await one.send(json.dumps(verb))
            await one.send(json.dumps({"section_rename": sid, "name": "Last"}))
            assert json.loads(await asyncio.wait_for(two.recv(), 2))["sections"][0]["name"] == "Last"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    saved = json.loads(path.read_text())
    assert saved == [{"id": sid, "name": "Last", "order": "manual", "chats": [A]}]
    restarted = W.Board(_rows(B), sections_path=path)
    assert json.loads(restarted.payload())["sections"] == saved


async def test_refused_verb_is_logged_and_not_saved(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """026-AC11: a refusal leaves the file untouched; a valid no-op neither saves nor broadcasts."""
    path = settings.config_dir / "sections.json"
    board = W.Board(_rows(A), sections_path=path)
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    with structlog.testing.capture_logs() as logs:
        await board._handlers["section_new"](None, {"section_new": " "})  # type: ignore[arg-type]
    assert [(e["event"], e["verb"], e["log_level"]) for e in logs] == [
        ("section_verb_refused", "section_new", "debug")  # like ws_client_message_ignored
    ]
    assert not path.exists()
    await board._handlers["section_new"](None, {"section_new": "One"})  # type: ignore[arg-type]
    stamp = path.stat().st_mtime_ns
    sent: list[Any] = []
    monkeypatch.setattr(W, "broadcast", lambda *a: sent.append(a))
    await board._handlers["section_file"](None, {"section_file": A, "section": None})  # type: ignore[arg-type]
    sid = board.sections.items[0]["id"]
    await board._handlers["section_rename"](None, {"section_rename": sid, "name": "One"})  # type: ignore[arg-type]
    assert path.stat().st_mtime_ns == stamp and sent == []


async def test_a_change_failing_half_way_is_rolled_back(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verb that raises after mutating (a bug, an unforeseen input) must leave the sections as they were."""
    path = settings.config_dir / "sections.json"
    board = W.Board(_rows(A, B), sections_path=path)
    await board._dispatch(None, "section_new", {"section_new": "One", "chat": A})  # type: ignore[arg-type]
    kept = json.loads(json.dumps(board.sections.items))

    def broken(self: Sections, *_: Any) -> bool:
        self.items[0]["chats"].clear()
        raise RuntimeError("half-way")

    for name in ("file", "move_chat"):
        with monkeypatch.context() as patch:
            patch.setattr(Sections, name, broken)
            verb = (
                {"section_file": B, "section": kept[0]["id"]} if name == "file" else {"section_chat_move": A}
            )
            await board._dispatch(None, next(iter(verb)), verb)  # type: ignore[arg-type]
        assert board.sections.items == kept
    assert json.loads(path.read_text()) == kept


@pytest.mark.parametrize(
    "verb",
    [
        lambda sid: {"section_rename": sid, "name": "Two"},
        lambda sid: {"section_file": A, "section": sid},
        lambda sid: {"section_delete": sid},
    ],
)
async def test_failed_save_rolls_back_and_a_retry_saves(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, verb: Any
) -> None:
    """026-AC10: a full disk must not leave a change shown nowhere and saved nowhere; retrying works."""
    path = settings.config_dir / "sections.json"
    board = W.Board(_rows(A), sections_path=path)
    await board._dispatch(None, "section_new", {"section_new": "One"})  # type: ignore[arg-type]
    saved, kept = path.read_text(), json.loads(json.dumps(board.sections.items))
    sent: list[Any] = []
    monkeypatch.setattr(W, "broadcast", lambda *a: sent.append(a))

    def full(*_: Any) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("miniteams.sections.write_private", full)
    frame = verb(kept[0]["id"])
    await board._dispatch(None, next(iter(frame)), frame)  # type: ignore[arg-type]
    assert board.sections.items == kept and path.read_text() == saved and sent == []
    monkeypatch.undo()
    monkeypatch.setattr(W, "broadcast", lambda *a: sent.append(a))
    await board._dispatch(None, next(iter(frame)), frame)  # type: ignore[arg-type]
    assert board.sections.items != kept and json.loads(path.read_text()) == board.sections.items
    assert len(sent) == 1
