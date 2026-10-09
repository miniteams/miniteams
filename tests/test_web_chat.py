"""Chat view server side (spec 011 phase 1): `history`, `follow`, message records, live relay."""

import asyncio
import base64
import contextlib
import hashlib
import json
import socket
import sqlite3
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import websockets

from miniteams import web as W
from miniteams.archive_store import ChatStore, Index, find_chat_dir
from miniteams.config import Settings
from miniteams.directory import Directory

T = "19:aaaa@thread.v2"  # archived, not on the board
B = "19:bbbb@thread.v2"  # on the board, not archived
C = "19:cccc@thread.tacv2"  # archived channel: outside the board's scope
NOPE = "19:zzzz@thread.v2"  # neither
ME = "8:orgid:00000000-0000-0000-0000-00000000000a"
ANN = "8:orgid:00000000-0000-0000-0000-00000000000b"
OBJ = "0-weu-d1-0123456789abcdef"


def _at(i: int) -> str:
    return f"2026-09-15T10:{i // 60:02d}:{i % 60:02d}.000Z"


def _m(msg_id: str, when: str, content: str = "", **extra: Any) -> dict[str, Any]:
    return {
        "id": msg_id,
        "composetime": when,
        "messagetype": "RichText/Html",
        "content": content or f"<p>m{msg_id}</p>",
        "from": f"https://h/v1/users/ME/contacts/{ANN}",
        "imdisplayname": "Ann Lee",
        **extra,
    }


def _archive(data_dir: Path, thread_id: str, messages: list[dict[str, Any]] | None) -> Path:
    index = Index(data_dir)
    index.upsert_chat(thread_id, label="Chat")
    index.close()
    if messages is None:
        return data_dir
    store = ChatStore(data_dir, thread_id)
    store.insert_page(messages)
    store.media_dir.mkdir(exist_ok=True)
    store.close()
    return data_dir


@pytest.fixture
def quiet_directory(directory: Directory, monkeypatch: pytest.MonkeyPatch) -> Directory:
    async def fake_display(self: Directory, mri: str) -> str:
        return f"name:{mri}"

    monkeypatch.setattr(Directory, "display", fake_display)
    directory.me = ME
    return directory


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    _archive(path, T, [_m(str(i), _at(i)) for i in range(1, 6)])
    _archive(path, C, [])
    return path


@pytest.fixture
def board(quiet_directory: Directory, data_dir: Path) -> W.Board:
    rows: dict[str, dict[str, Any]] = {
        B: {"id": B, "label": "B", "last_activity": _at(1), "last_id": "1", "seen_at": None, "typing": []}
    }
    return W.Board(rows, quiet_directory, me=ME, data_dir=data_dir)


@pytest.fixture
def no_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(W, "fetch_history", lambda *a: pytest.fail("no API call expected"))
    monkeypatch.setattr(W, "send_message", lambda *a: pytest.fail("no API call expected"))


def _ids(answer: dict[str, Any]) -> list[str]:
    return [m["id"] for m in answer["messages"]]


# --- history: archive (011-AC4) ---


async def test_history_latest_page_oldest_first(board: W.Board, no_api: None) -> None:
    """011-AC4: the last `limit` messages, oldest first, `older` while earlier ones exist."""
    answer = await board.history(T, limit=2)
    assert answer["history"] == T
    assert (_ids(answer), answer["older"], answer["newer"], answer["source"]) == (
        ["4", "5"],
        True,
        False,
        "archive",
    )
    assert set(answer) == {"history", "before", "messages", "older", "newer", "source"}
    assert answer["before"] is None
    before = await board.history(T, before=answer["messages"][0]["at"], limit=2)
    assert (_ids(before), before["older"]) == (["2", "3"], True)
    first = await board.history(T, before=before["messages"][0]["at"], limit=2)
    assert (_ids(first), first["older"]) == (["1"], False)


async def test_history_before_pages_back_without_overlap_or_gap(
    tmp_path: Path, quiet_directory: Directory
) -> None:
    """011-AC4: walking `before` from the latest page returns every message once, `older` false at the start.

    Messages 3, 4 and 5 share one time, and the page boundary falls inside them."""
    times = [_at(1), _at(2), _at(3), _at(3), _at(3), _at(4), _at(5)]
    data = _archive(tmp_path / "d", T, [_m(str(i), t) for i, t in enumerate(times, 1)])
    board = W.Board({}, quiet_directory, me=ME, data_dir=data)
    pages: list[list[str]] = []
    answer = await board.history(T, limit=3)
    while True:
        pages.insert(0, _ids(answer))
        if not answer["older"]:
            break
        answer = await board.history(T, before=answer["messages"][0]["at"], limit=3)
    # 5, 6, 7 fill the page; 3 and 4 share 5's time, so they join it rather than be skipped.
    assert pages == [["1", "2"], ["3", "4", "5", "6", "7"]]
    assert (await board.history(T, before=_at(1)))["messages"] == []


async def test_history_limit_default_and_cap(tmp_path: Path, quiet_directory: Directory) -> None:
    """011-AC4: default page 100, capped at 200, junk limits fall back to the default."""
    data = _archive(tmp_path / "d", T, [_m(str(i), _at(i)) for i in range(250)])
    board = W.Board({}, quiet_directory, data_dir=data)
    assert len((await board.history(T))["messages"]) == 100
    assert len((await board.history(T, limit=500))["messages"]) == 200
    for junk in (0, -3, "50", True, 2.5, None):
        assert len((await board.history(T, limit=junk))["messages"]) == 100


async def test_history_of_an_empty_archived_chat(
    tmp_path: Path, quiet_directory: Directory, no_api: None
) -> None:
    """011-AC4: an archived chat without messages is an empty page, not an error."""
    board = W.Board({}, quiet_directory, data_dir=_archive(tmp_path / "d", T, []))
    assert await board.history(T) == {
        "history": T,
        "before": None,
        "messages": [],
        "older": False,
        "newer": False,
        "source": "archive",
    }


async def test_history_refuses_a_non_text_cursor(board: W.Board, no_api: None) -> None:
    """011-AC4"""
    assert await board.history(T, before=123) == {"history": T, "before": None, "error": "invalid before"}


async def test_history_rewrites_archived_images(board: W.Board, data_dir: Path) -> None:
    """011-AC4 + 011-AC6: images in a history page point at /media when archived."""
    store = ChatStore(data_dir, T)
    src = f"https://eu-api.asm.skype.com/v1/objects/{OBJ}/views/imgo"
    store.insert_page(
        [_m("9", _at(9), f'<p><img itemtype="http://schema.skype.com/AMSImage" src="{src}"></p>')]
    )
    (store.media_dir / f"{OBJ}.jpg").write_bytes(b"jpg")
    store.close()
    answer = await board.history(T, limit=1)
    assert answer["messages"][0]["html"] == f'<p><img src="/media/19%3Aaaaa%40thread.v2/{OBJ}.jpg"></p>'


# --- history: API fallback (011-AC5) ---


@pytest.fixture
def api_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int, int | None]]:
    calls: list[tuple[str, int, int, int | None]] = []
    page = [_m("1", "2026-09-15T10:00:00.100Z"), _m("2", "2026-09-15T10:00:00.900Z"), _m("3", _at(5))]

    def fake_history(
        settings: Any,
        token: str,
        thread_id: str,
        page_size: int,
        max_pages: int,
        end_before: int | None = None,
    ) -> list[dict[str, Any]]:
        calls.append((thread_id, page_size, max_pages, end_before))
        return page

    monkeypatch.setattr(W, "fetch_history", fake_history)
    return calls


async def test_history_without_archive_reads_one_api_page(board: W.Board, api_calls: list[Any]) -> None:
    """011-AC5: a board chat without messages.db gets one API page, `source: "api"`."""
    answer = await board.history(B, limit=50)
    assert (_ids(answer), answer["older"], answer["newer"], answer["source"]) == (
        ["1", "2", "3"],
        True,
        False,
        "api",
    )
    assert api_calls == [(B, 50, 1, None)]


async def test_history_api_before_cuts_at_the_exact_time(board: W.Board, api_calls: list[Any]) -> None:
    """011-AC5: the API window is second-granular, the cut is not."""
    answer = await board.history(B, before="2026-09-15T10:00:00.500Z")
    assert _ids(answer) == ["1"]
    epoch = 1789466400  # 2026-09-15T10:00:00Z
    assert api_calls[-1][3] == epoch + 1
    assert (await board.history(B, before="2026-09-15T10:00:00.000Z"))["older"] is False  # nothing left


async def test_history_indexed_chat_without_messages_db_uses_the_api(
    tmp_path: Path, quiet_directory: Directory, api_calls: list[Any]
) -> None:
    """011-AC5: indexed but never backfilled is "no messages.db"."""
    board = W.Board({}, quiet_directory, data_dir=_archive(tmp_path / "d", T, None))
    assert (await board.history(T))["source"] == "api"


async def test_history_unreadable_archive_falls_back_to_the_api(
    board: W.Board, data_dir: Path, api_calls: list[Any]
) -> None:
    """011-AC5: a corrupt messages.db does not kill the page."""
    chat = data_dir / "19:cccc@thread.tacv2"
    (chat / "messages.db").write_bytes(b"not a database" * 100)
    for leftover in chat.glob("messages.db-*"):
        leftover.unlink()
    assert (await board.history(C))["source"] == "api"


async def test_history_with_the_archive_gone_uses_the_api(
    tmp_path: Path, quiet_directory: Directory, api_calls: list[Any]
) -> None:
    """011-AC5: no index at all: the board's chats still get their API page."""
    board = W.Board({B: {"id": B, "last_activity": _at(1)}}, quiet_directory, data_dir=tmp_path / "missing")
    assert (await board.history(B))["source"] == "api"
    assert (await board.history(T))["error"] == "unknown chat"


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (
            httpx.HTTPStatusError(
                "500", request=httpx.Request("GET", "https://x"), response=httpx.Response(500)
            ),
            "Teams answered 500",
        ),
        (httpx.ConnectError("boom", request=httpx.Request("GET", "https://x")), "network error"),
        (RuntimeError("odd"), "odd"),
    ],
)
async def test_history_api_failure_answers_an_error(
    board: W.Board, monkeypatch: pytest.MonkeyPatch, exc: Exception, reason: str
) -> None:
    """011-AC5"""

    def failing(*args: Any) -> list[Any]:
        raise exc

    monkeypatch.setattr(W, "fetch_history", failing)
    assert await board.history(B) == {"history": B, "before": None, "error": reason}


async def test_history_without_a_connection(tmp_path: Path) -> None:
    """011-AC5: nothing archived and no API session: an error, not a crash."""
    board = W.Board({B: {"id": B, "last_activity": _at(1)}})
    assert await board.history(B) == {"history": B, "before": None, "error": "not connected"}


# --- unknown thread (011-AC9) ---


async def test_unknown_thread_is_refused_without_an_api_call(board: W.Board, no_api: None) -> None:
    """011-AC9: neither on the board nor in the archive: history, follow and send are refused."""
    assert await board.history(NOPE) == {"history": NOPE, "before": None, "error": "unknown chat"}
    assert await board.follow(None, NOPE) == {"follow": NOPE, "error": "unknown chat"}  # type: ignore[arg-type]
    assert await board.send(NOPE, "hi") == "unknown chat"
    assert board._follows == {}


async def test_unknown_thread_without_any_archive(quiet_directory: Directory, no_api: None) -> None:
    """011-AC9: no --data-dir at all."""
    board = W.Board({}, quiet_directory)
    assert await board.history(T) == {"history": T, "before": None, "error": "unknown chat"}


# --- message records ---


def test_record_fields() -> None:
    """011-AC4: the record shape the page relies on."""
    message = _m("7", _at(7), "<p>hi <b>you</b></p>", properties={"edittime": "1789466500000"})
    assert W.chat_record(message, T, ME) == {
        "id": "7",
        "at": _at(7),
        "from": ANN,
        "name": "Ann Lee",
        "me": False,
        "type": "text",
        "html": "<p>hi <b>you</b></p>",
        "edited": True,
        "deleted": False,
        "reactions": [],
        "mentions": None,
    }


def test_record_deleted_message_has_no_body() -> None:
    """011-AC7: a deleted message keeps its place and loses its body."""
    for props in ({"deletetime": "1789466500000"}, {"hardDeleteTime": "1789466500000"}):
        record = W.chat_record(_m("7", _at(7), "<p>secret</p>", properties=props), T, ME)
        assert (record["deleted"], record["html"]) == (True, "")
    assert W.chat_record(_m("8", _at(8), skypeeditedid="8"), T)["edited"] is True


def test_record_reactions_count_and_mine() -> None:
    """011-AC7: reactions line, `mine` when my MRI is among the users (JSON string or list)."""
    emotions = [
        {"key": "like", "users": [{"mri": ANN, "time": 1}, {"mri": ME, "time": 2}]},
        {"key": "heart", "users": [{"mri": ANN, "time": 3}]},
        {"key": "party_custom", "users": [{"mri": ANN}]},
        {"key": "laugh", "users": []},
        "junk",
    ]
    expected = [
        {"key": "like", "emoji": "👍", "count": 2, "mine": True},
        {"key": "heart", "emoji": "❤️", "count": 1, "mine": False},
        {"key": "party_custom", "emoji": ":party_custom:", "count": 1, "mine": False},
    ]
    for value in (emotions, json.dumps(emotions)):
        assert W.chat_record(_m("7", _at(7), properties={"emotions": value}), T, ME)["reactions"] == expected
    assert (
        W.chat_record(_m("7", _at(7), properties={"emotions": emotions}), T)["reactions"][0]["mine"] is False
    )


def test_record_mentions_and_me() -> None:
    """011-AC6: mention kind (spec 003) and the mention span for my MRI."""
    content = '<p><span itemtype="http://schema.skype.com/Mention" itemscope itemid="0">Me</span> look</p>'
    props = {"mentions": json.dumps([{"mentionType": "person", "itemid": 0, "mri": ME}])}
    record = W.chat_record(_m("7", _at(7), content, properties=props), T, ME)
    assert record["mentions"] == "me"
    assert record["html"] == f'<p><span class="mention mention-me" data-mri="{ME}">Me</span> look</p>'
    mine = W.chat_record(_m("8", _at(8), **{"from": f"https://h/v1/users/ME/contacts/{ME}"}), T, ME)
    assert (mine["me"], mine["from"], mine["mentions"]) == (True, ME, None)


@pytest.mark.parametrize(
    ("msgtype", "content", "props", "kind", "body"),
    [
        ("Text", "a < b & <i>c</i>", {}, "text", "a &lt; b &amp; &lt;i&gt;c&lt;/i&gt;"),
        ("Event/Call", "<ended/>", {}, "call", "📞 call ended"),
        ("ThreadActivity/AddMember", "<addmember/>", {}, "system", "👥 member added"),
        ("RichText/Media_Card", "<x/>", {}, "other", "🃏 card"),
        ("RichText/Html", "", {"files": json.dumps([{"fileName": "a<b>.pdf"}])}, "text", "📎 a&lt;b&gt;.pdf"),
    ],
)
def test_record_types_and_markers(
    msgtype: str, content: str, props: dict[str, Any], kind: str, body: str
) -> None:
    """011-AC4: non-text messages get the widget's marker, escaped."""
    record = W.chat_record(
        {"id": "1", "composetime": _at(1), "messagetype": msgtype, "content": content, "properties": props}, T
    )
    assert (record["type"], record["html"]) == (kind, body)


# --- socket: follow + relay (011-AC7), answers to the asker only ---


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def _served(board: W.Board, settings: Settings) -> AsyncIterator[str]:
    port = _free_port()
    task = asyncio.create_task(W.serve_board(board, settings, f"127.0.0.1:{port}"))
    try:
        await asyncio.sleep(0.2)
        yield f"127.0.0.1:{port}"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _connect(host: str) -> Any:
    return websockets.connect(f"ws://{host}/ws", origin=websockets.Origin(f"http://{host}"))


async def _frame(ws: Any) -> dict[str, Any]:
    """Next frame that is not a board payload (followers are board clients too)."""
    while True:
        frame = json.loads(await asyncio.wait_for(ws.recv(), 2))
        if "rows" not in frame:
            return dict(frame)


def _event(resource_type: str, thread_id: str, **resource: Any) -> dict[str, Any]:
    base = {
        "conversationLink": f"https://h/v1/users/ME/conversations/{thread_id}",
        "imdisplayname": "Ann Lee",
        "from": f"https://h/v1/users/ME/contacts/{ANN}",
    }
    return {"resourceType": resource_type, "resource": {**base, **resource}}


async def test_follow_relays_messages_edits_deletes_reactions_and_typing(
    board: W.Board, settings: Settings
) -> None:
    """011-AC7: a followed thread's live events reach the follower as msg / edit / typing frames."""
    async with _served(board, settings) as host, _connect(host) as ws:
        await ws.recv()
        await ws.send(json.dumps({"follow": T}))
        assert (await _frame(ws)).items() >= {"follow": T, "ok": True}.items()

        new = {"id": "9", "composetime": _at(9), "messagetype": "RichText/Html", "content": "<p>new</p>"}
        await board.on_event(_event("NewMessage", T, **new))
        msg = (await _frame(ws))["msg"]
        assert (msg["id"], msg["html"], msg["name"]) == ("9", "<p>new</p>", "Ann Lee")

        edited = {**new, "content": "<p>fixed</p>", "properties": {"edittime": "1789466500000"}}
        await board.on_event(_event("MessageUpdate", T, **edited))
        edit = (await _frame(ws))["edit"]
        assert (edit["id"], edit["html"], edit["edited"]) == ("9", "<p>fixed</p>", True)

        deleted = {**new, "content": "", "properties": {"deletetime": "1789466600000"}}
        await board.on_event(_event("MessageUpdate", T, **deleted))
        assert (await _frame(ws))["edit"]["deleted"] is True

        emotions = {"emotions": [{"key": "like", "users": [{"mri": ME, "time": 1}]}]}
        reaction = {"id": "9", "composetime": _at(9), "messagetype": "RichText/Html", "properties": emotions}
        await board.on_event(_event("MessageUpdate", T, **reaction))
        edit = (await _frame(ws))["edit"]
        assert edit["reactions"] == [{"key": "like", "emoji": "👍", "count": 1, "mine": True}]
        assert "html" not in edit  # partial update: the page keeps the body it shows

        await board.on_event(_event("NewMessage", T, messagetype="Control/Typing"))
        assert await _frame(ws) == {"typing": "Ann Lee", "on": True}
        await board.on_event(_event("NewMessage", T, messagetype="Control/ClearTyping"))
        assert await _frame(ws) == {"typing": "Ann Lee", "on": False}


async def test_follow_relays_only_its_thread_and_skips_noise(board: W.Board, settings: Settings) -> None:
    """011-AC7: other threads, my own typing, read markers and conversation updates are not relayed."""
    async with _served(board, settings) as host, _connect(host) as ws:
        await ws.recv()
        await ws.send(json.dumps({"follow": C}))  # an archived channel, outside the board's scope
        assert (await _frame(ws)).items() >= {"follow": C, "ok": True}.items()
        mine = f"https://h/v1/users/ME/contacts/{ME}"
        await board.on_event(
            _event("NewMessage", T, id="1", composetime=_at(1), messagetype="Text", content="x")
        )
        await board.on_event(_event("NewMessage", C, messagetype="Control/Typing", **{"from": mine}))
        horizon = "ThreadActivity/MemberConsumptionHorizonUpdate"
        await board.on_event(_event("NewMessage", C, id="2", composetime=_at(2), messagetype=horizon))
        await board.on_event(_event("NewMessage", C, id="3", composetime=_at(3), messagetype="Control/Pin"))
        await board.on_event({"resourceType": "ConversationUpdate", "resource": {"id": C}})
        await board.on_event(
            _event("NewMessage", C, id="4", composetime=_at(4), messagetype="Text", content="y")
        )
        assert (await _frame(ws))["msg"]["id"] == "4"  # the first frame relayed is the last event


async def test_follow_replaces_and_closing_cleans_up(board: W.Board, settings: Settings) -> None:
    """011-AC7: one followed thread per socket; a closed socket leaves no bookkeeping."""
    async with _served(board, settings) as host:
        async with _connect(host) as ws:
            await ws.recv()
            await ws.send(json.dumps({"follow": T}))
            await _frame(ws)
            await ws.send(json.dumps({"follow": B}))
            await _frame(ws)
            assert [tid for tid, _ in board._follows.values()] == [B]
        for _ in range(50):
            if not board._follows:
                break
            await asyncio.sleep(0.02)
        assert board._follows == {}


async def test_history_and_errors_go_to_the_asking_socket_only(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC4 + 011-AC5 + 011-AC9: answers reach the asker only; after an error the socket still works."""

    def failing(*args: Any) -> list[Any]:
        raise httpx.ConnectError("boom", request=httpx.Request("GET", "https://x"))

    monkeypatch.setattr(W, "fetch_history", failing)
    async with _served(board, settings) as host, _connect(host) as asker, _connect(host) as other:
        await asker.recv()
        await other.recv()
        await asker.send(json.dumps({"history": B}))
        assert await _frame(asker) == {"history": B, "before": None, "error": "network error"}
        await asker.send(json.dumps({"history": T, "limit": 2}))
        assert _ids(await _frame(asker)) == ["4", "5"]
        await asker.send(json.dumps({"follow": NOPE}))
        assert await _frame(asker) == {"follow": NOPE, "error": "unknown chat"}
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(other.recv(), 0.3)


async def _silent(ws: Any, wait: float = 0.3) -> None:
    """No frame other than board payloads arrives within `wait` seconds."""
    with pytest.raises(TimeoutError):
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), wait))
            assert "rows" in frame, frame


# --- review fixes ---


async def test_relay_skips_a_socket_that_does_not_follow(board: W.Board, settings: Settings) -> None:
    """011-AC7: only followers get relayed frames; another open page gets none."""
    async with _served(board, settings) as host, _connect(host) as ws, _connect(host) as idle:
        await ws.recv()
        await idle.recv()
        await ws.send(json.dumps({"follow": T}))
        assert (await _frame(ws)).items() >= {"follow": T, "ok": True}.items()
        await board.on_event(_event("NewMessage", T, messagetype="Control/Typing"))
        await board.on_event(
            _event("NewMessage", T, id="9", composetime=_at(9), messagetype="Text", content="x")
        )
        assert (await _frame(ws))["typing"] == "Ann Lee"
        await _silent(idle)


async def test_unreadable_archive_rows_are_skipped(
    tmp_path: Path, quiet_directory: Directory, no_api: None
) -> None:
    """011-AC4: a corrupt row costs that message, never the page."""
    data = _archive(tmp_path / "d", T, [_m("1", _at(1)), _m("5", _at(5))])
    oddly = json.dumps({**_m("4", _at(4)), "properties": "oops"})
    db = sqlite3.connect(data / T / "messages.db")
    with db:
        db.executemany(
            "INSERT INTO messages (id, composetime, raw) VALUES (?, ?, ?)",
            [("2", _at(2), "{bad"), ("3", _at(3), "[1]"), ("4", _at(4), oddly)],
        )
    db.close()
    board = W.Board({}, quiet_directory, me=ME, data_dir=data)
    answer = await board.history(T)
    assert (_ids(answer), answer["older"]) == (["1", "4", "5"], False)


async def test_any_history_failure_still_answers(board: W.Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """011-AC5: an unexpected failure is an error answer, not a dead task."""

    def broken(*args: Any) -> Any:
        raise ValueError("boom")

    monkeypatch.setattr(W, "read_page", broken)
    assert await board.history(T) == {"history": T, "before": None, "error": "history unavailable"}
    monkeypatch.setattr(W, "chat_record", broken)
    monkeypatch.setattr(W, "read_page", lambda *a: ([_m("1", _at(1))], False))
    assert (await board.history(T))["messages"] == []  # the bad record is skipped, the page answers


def test_record_survives_malformed_properties() -> None:
    """011-AC7: odd shapes from the stream or the archive never raise."""
    for props in ("oops", ["x"], {"emotions": [{"key": "like", "users": 5}], "mentions": 3}):
        record = W.chat_record(_m("1", _at(1), properties=props), T, ME)
        assert (record["reactions"], record["html"]) == ([], "<p>m1</p>")


async def test_records_are_built_off_the_event_loop(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch, api_calls: list[Any]
) -> None:
    """011-AC4 + 011-AC7: sanitising globs the (NFS) media folder: never on the loop thread."""
    loop_thread = threading.get_ident()
    threads: list[int] = []
    real = W.chat_record

    def spy(*args: Any) -> dict[str, Any]:
        threads.append(threading.get_ident())
        return real(*args)

    monkeypatch.setattr(W, "chat_record", spy)
    await board.history(T)
    await board.history(B)  # API path
    async with _served(board, settings) as host, _connect(host) as ws:
        await ws.recv()
        await ws.send(json.dumps({"follow": T}))
        await _frame(ws)
        await board.on_event(
            _event("NewMessage", T, id="9", composetime=_at(9), messagetype="Text", content="x")
        )
        await _frame(ws)
    assert len(threads) == 5 + 3 + 1
    assert loop_thread not in threads


async def test_relay_failure_does_not_cost_the_board_its_update(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC7: a relay that raises is logged; the row still moves and the stream goes on."""

    def broken(*args: Any) -> Any:
        raise TypeError("odd event")

    monkeypatch.setattr(W, "chat_record", broken)
    async with _served(board, settings) as host, _connect(host) as ws:
        await ws.recv()
        await ws.send(json.dumps({"follow": B}))
        await _frame(ws)
        await board.on_event(
            _event("NewMessage", B, id="9", composetime=_at(9), messagetype="Text", content="x")
        )
        assert board.rows[B]["last_id"] == "9"
        await board.on_event(_event("NewMessage", B, messagetype="Control/Typing"))
        assert await _frame(ws) == {"typing": "Ann Lee", "on": True}


async def test_typing_relay_names_who_stopped_and_expires(
    quiet_directory: Directory, data_dir: Path, settings: Settings
) -> None:
    """011-AC7: clear frames name the typer (groups), expire without ClearTyping, end on their message."""
    board = W.Board({}, quiet_directory, me=ME, data_dir=data_dir, typing_ttl=0.2)
    bob = {"from": "https://h/v1/users/ME/contacts/8:orgid:bob", "imdisplayname": "Bob Day"}
    async with _served(board, settings) as host, _connect(host) as ws:
        await ws.recv()
        await ws.send(json.dumps({"follow": C}))  # not on the board: the relay owns the timer
        await _frame(ws)
        await board.on_event(_event("NewMessage", C, messagetype="Control/Typing"))
        await board.on_event(_event("NewMessage", C, messagetype="Control/Typing", **bob))
        assert await _frame(ws) == {"typing": "Ann Lee", "on": True}
        assert await _frame(ws) == {"typing": "Bob Day", "on": True}
        await board.on_event(_event("NewMessage", C, messagetype="Control/ClearTyping", **bob))
        assert await _frame(ws) == {"typing": "Bob Day", "on": False}
        assert await _frame(ws) == {"typing": "Ann Lee", "on": False}  # Ann never cleared: expiry
        await board.on_event(_event("NewMessage", C, messagetype="Control/Typing"))
        assert await _frame(ws) == {"typing": "Ann Lee", "on": True}
        await board.on_event(
            _event("NewMessage", C, id="9", composetime=_at(9), messagetype="Text", content="x")
        )
        assert await _frame(ws) == {"typing": "Ann Lee", "on": False}
        assert (await _frame(ws))["msg"]["id"] == "9"
        await _silent(ws, 0.4)  # the expiry was cancelled: no second clear


@pytest.mark.parametrize("before", ["garbage", "2026-09-15", "15/09/2026 10:00:00", "x2026-09-15T10:00:00Z"])
async def test_history_refuses_a_malformed_cursor(board: W.Board, no_api: None, before: str) -> None:
    """011-AC4 + 011-AC5: a cursor that is not a time is refused before any read or API call."""
    for thread in (T, B):
        assert await board.history(thread, before=before) == {
            "history": thread,
            "before": before,
            "error": "invalid before",
        }


async def test_history_echoes_its_cursor(board: W.Board, api_calls: list[Any]) -> None:
    """011-AC4 + 011-AC5: the page matches each answer to its request by `before`."""
    assert (await board.history(T, before=_at(3)))["before"] == _at(3)
    assert (await board.history(B, before=_at(3)))["before"] == _at(3)
    assert (await board.history(T))["before"] is None


async def test_one_history_in_flight_per_socket(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC4: a second request while one is pending is refused `busy`; other sockets are not held up."""
    real = W.read_page

    def slow(*args: Any) -> Any:
        time.sleep(0.4)
        return real(*args)

    monkeypatch.setattr(W, "read_page", slow)
    async with _served(board, settings) as host, _connect(host) as one, _connect(host) as two:
        await one.recv()
        await two.recv()
        await one.send(json.dumps({"history": T, "limit": 1}))
        await two.send(json.dumps({"history": T, "limit": 2}))
        await one.send(json.dumps({"history": T, "before": _at(3)}))
        assert await _frame(one) == {"history": T, "before": _at(3), "error": "busy"}
        assert _ids(await _frame(one)) == ["5"]
        assert _ids(await _frame(two)) == ["4", "5"]
        await one.send(json.dumps({"history": T, "before": _at(3)}))
        assert _ids(await _frame(one)) == ["1", "2"]


async def test_empty_before_means_the_latest_page(board: W.Board, no_api: None) -> None:
    """011-AC4: `before: ""` is the latest page, like a missing or null cursor."""
    answer = await board.history(T, before="", limit=2)
    assert (_ids(answer), answer["before"]) == (["4", "5"], None)


async def test_own_typing_is_never_relayed(board: W.Board, settings: Settings) -> None:
    """011-AC7: my typing on another device, started or cleared, is not shown to the page."""
    mine = {"from": f"https://h/v1/users/ME/contacts/{ME}", "imdisplayname": "Me Myself"}
    async with _served(board, settings) as host, _connect(host) as ws:
        await ws.recv()
        await ws.send(json.dumps({"follow": B}))
        await _frame(ws)
        await board.on_event(_event("NewMessage", B, messagetype="Control/Typing", **mine))
        await board.on_event(_event("NewMessage", B, messagetype="Control/ClearTyping", **mine))
        await _silent(ws)


async def test_typing_expiry_off_the_board_does_not_rebroadcast(
    quiet_directory: Directory, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC7: a followed thread off the board expiring its typing leaves the board alone."""
    board = W.Board({}, quiet_directory, me=ME, data_dir=data_dir, typing_ttl=0.05)
    sent: list[str] = []
    monkeypatch.setattr(board, "broadcast", lambda: sent.append("board"))
    board._typing_arm(C, "Ann Lee")
    await asyncio.sleep(0.15)
    assert (C, "Ann Lee") not in board._typing_timers
    assert sent == []


# --- phase 2: routes, chat socket, send/open on a followed chat ---


def _media(data_dir: Path) -> Path:
    found = find_chat_dir(data_dir, T)
    assert found is not None
    media = found / "media"
    (media / f"{OBJ}.png").write_bytes(b"\x89PNG-fake")
    return media


def _chat_url(host: str, thread_id: str) -> str:
    return f"http://{host}/chat/{quote(thread_id, safe='')}"


async def test_chat_route_serves_the_page_for_board_and_archived_chats(
    board: W.Board, settings: Settings
) -> None:
    """011-AC3: `/chat/<id>` is the chat page for a thread on the board or in the archive, 404 else."""
    async with _served(board, settings) as host, httpx.AsyncClient() as client:
        for thread_id in (T, B, C):
            page = await client.get(_chat_url(host, thread_id))
            assert page.status_code == 200, thread_id
            assert page.headers["content-type"].startswith("text/html")
            csp = page.headers["content-security-policy"]
            script = page.text.split("<script>", 1)[1].split("</script>", 1)[0]
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            assert f"script-src 'sha256-{digest}';" in csp and "img-src 'self'" in csp
            assert "unsafe-inline" not in csp.split("script-src", 1)[1].split(";", 1)[0]
            assert "/ws?chat" in page.text
        for path in (_chat_url(host, NOPE), f"http://{host}/chat/", f"http://{host}/chat/{T}/x"):
            assert (await client.get(path)).status_code == 404, path


async def test_media_route_serves_an_archived_file(
    board: W.Board, settings: Settings, data_dir: Path
) -> None:
    """011-AC3: an archived asset is served with its type, sandboxed; a missing one is a 404."""
    _media(data_dir)
    async with _served(board, settings) as host, httpx.AsyncClient() as client:
        base = f"http://{host}/media/{quote(T, safe='')}"
        got = await client.get(f"{base}/{OBJ}.png")
        assert got.status_code == 200 and got.content == b"\x89PNG-fake"
        assert got.headers["content-type"] == "image/png"
        assert got.headers["x-content-type-options"] == "nosniff"
        assert got.headers["content-security-policy"] == "sandbox"
        assert got.headers["cross-origin-resource-policy"] == "same-origin"
        for path in (f"{base}/missing.png", f"{base}/%2e%2e%2Findex.db", f"http://{host}/media/{T}"):
            assert (await client.get(path)).status_code == 404, path


def test_media_file_stays_inside_the_media_folder(board: W.Board, data_dir: Path, tmp_path: Path) -> None:
    """011-AC3: traversal in every shape, symlinks out, partial downloads and unknown chats are refused."""
    media = _media(data_dir)
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"secret")
    (media / "out.png").symlink_to(secret)
    (media / "half.png.part").write_bytes(b"half")
    (media / "to-part.png").symlink_to(media / "half.png.part")
    (media / "sub").mkdir()
    thread = quote(T, safe="")
    assert board.media_file(f"/media/{thread}/{OBJ}.png") == (b"\x89PNG-fake", "image/png")
    refused = [
        "..",
        "%2e%2e",
        "%2E%2E%2Findex.db",
        "..%2F..%2Fsecret.png",
        "%2Fetc%2Fpasswd",
        "..%5Cindex.db",
        "out.png",  # symlink leaving the folder
        "to-part.png",  # symlink onto a partial download
        "half.png.part",
        "sub",
        "missing.png",
        "",
        f"{OBJ}.png%00.txt",
    ]
    for name in refused:
        assert board.media_file(f"/media/{thread}/{name}") is None, name
    assert board.media_file(f"/media/{thread}//etc/passwd") is None  # absolute: extra segment
    assert board.media_file(f"/media/{thread}/sub/x.png") is None
    assert board.media_file(f"/media/{quote(NOPE, safe='')}/{OBJ}.png") is None
    assert board.media_file(f"/media/{quote(B, safe='')}/{OBJ}.png") is None  # on the board, no archive


def test_media_file_refuses_oversized_files(
    board: W.Board, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC3: the whole-body response is capped."""
    _media(data_dir)
    monkeypatch.setattr(W, "_MEDIA_MAX", 4)
    assert board.media_file(f"/media/{quote(T, safe='')}/{OBJ}.png") is None


def _chat_socket(host: str) -> Any:
    return websockets.connect(f"ws://{host}/ws?chat", origin=websockets.Origin(f"http://{host}"))


async def test_chat_socket_never_receives_the_board(board: W.Board, settings: Settings) -> None:
    """011-AC10: a chat page's socket gets its chat's frames, never the board's rows; the widget does."""
    async with _served(board, settings) as host, _chat_socket(host) as chat, _connect(host) as widget:
        assert "rows" in json.loads(await widget.recv())
        await chat.send(json.dumps({"follow": T}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {
            "follow": T,
            "ok": True,
            "label": T,  # archived, not seeded: no better name known
            "opener": False,
            "page": W.page_version("chat.html"),
            "read_only": False,
        }
        assert len(board.clients) == 1
        board.broadcast()
        assert "rows" in json.loads(await widget.recv())
        await board.on_event(
            _event("NewMessage", T, id="9", composetime=_at(9), messagetype="Text", content="x")
        )
        assert json.loads(await asyncio.wait_for(chat.recv(), 2))["msg"]["id"] == "9"
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(chat.recv(), 0.3)


async def test_follow_names_a_board_chat_and_tells_the_opener(
    quiet_directory: Directory, data_dir: Path
) -> None:
    """011-AC19: the follow answer carries the row's label and whether the server has an opener."""
    rows = {B: {"id": B, "label": "Team · 4p", "last_activity": _at(1), "last_id": "1", "typing": []}}
    board = W.Board(rows, quiet_directory, me=ME, data_dir=data_dir, opener=["xdg-open"])
    answer = await board.follow(object(), B)  # type: ignore[arg-type]
    assert (answer["label"], answer["opener"]) == ("Team · 4p", True)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def fake_send(settings: Any, token: str, thread_id: str, text: str, me_name: str) -> dict[str, Any]:
        calls.append((thread_id, text))
        return {"status": 201}

    monkeypatch.setattr(W, "send_message", fake_send)
    return calls


async def test_send_reaches_a_followed_chat_off_the_board(
    board: W.Board, settings: Settings, sent: list[tuple[str, str]]
) -> None:
    """011-AC8: the composer's send posts once to a followed archived chat; anything else is refused."""
    async with _served(board, settings) as host, _chat_socket(host) as chat, _chat_socket(host) as other:
        await other.send(json.dumps({"send": T, "text": "not following"}))
        assert json.loads(await asyncio.wait_for(other.recv(), 2)) == {"sent": T, "error": "unknown chat"}
        await chat.send(json.dumps({"follow": T}))
        await chat.recv()
        await chat.send(json.dumps({"send": T, "text": "hello\nthere"}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {"sent": T, "ok": True}
        await chat.send(json.dumps({"send": NOPE, "text": "elsewhere"}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {"sent": NOPE, "error": "unknown chat"}
        await chat.send(json.dumps({"send": T, "text": "  "}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {"sent": T, "error": "empty message"}
    assert sent == [(T, "hello\nthere")]


async def test_send_failure_keeps_the_reason(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC8: a failed send answers the reason the page shows under the field."""

    def failing(*args: Any) -> None:
        raise httpx.ConnectError("boom", request=httpx.Request("POST", "https://x"))

    monkeypatch.setattr(W, "send_message", failing)
    async with _served(board, settings) as host, _chat_socket(host) as chat:
        await chat.send(json.dumps({"follow": T}))
        await chat.recv()
        await chat.send(json.dumps({"send": T, "text": "hi"}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {"sent": T, "error": "network error"}


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    class _Proc:
        async def wait(self) -> int:
            return 0

    async def fake_exec(*argv: str, **kw: Any) -> _Proc:
        calls.append(argv)
        return _Proc()

    monkeypatch.setattr(W.asyncio, "create_subprocess_exec", fake_exec)
    return calls


async def _until(calls: list[Any], n: int) -> None:
    for _ in range(100):
        if len(calls) >= n:
            return
        await asyncio.sleep(0.01)


async def test_open_reaches_a_followed_chat_and_lands_on_a_message(
    quiet_directory: Directory, data_dir: Path, settings: Settings, launched: list[tuple[str, ...]]
) -> None:
    """011-AC19: `open` runs the opener for a followed off-board chat, on a message id when it is digits."""
    rows = {B: {"id": B, "label": "B", "last_activity": _at(1), "last_id": "1", "typing": []}}
    board = W.Board(rows, quiet_directory, me=ME, data_dir=data_dir, opener=["xdg-open"])
    async with _served(board, settings) as host, _chat_socket(host) as chat:
        await chat.send(json.dumps({"open": T}))  # not followed yet: refused
        await chat.send(json.dumps({"follow": T}))
        await chat.recv()
        await chat.send(json.dumps({"open": T, "msg": "1789466500000"}))
        await chat.send(json.dumps({"open": T, "msg": "12a/../x"}))  # not an id: the chat link
        await chat.send(json.dumps({"open": T}))
        await chat.send(json.dumps({"open": NOPE, "msg": "1"}))
        await chat.send(json.dumps({"open": B, "msg": "²"}))  # unicode digit: ignored, row's last id
        await _until(launched, 4)
        await asyncio.sleep(0.1)
    assert launched == [
        ("xdg-open", W.deep_link(T, "1789466500000", "msteams")),
        ("xdg-open", W.deep_link(T, "", "msteams")),
        ("xdg-open", W.deep_link(T, "", "msteams")),
        ("xdg-open", W.deep_link(B, "1", "msteams")),
    ]


async def test_open_without_opener_is_left_to_the_page(
    board: W.Board, launched: list[tuple[str, ...]]
) -> None:
    """011-AC19: with `--opener none` the server opens nothing (the page opens the https link)."""
    assert await board.open_row(B, msg_id="1") is False
    assert launched == []


async def test_chat_and_media_refuse_a_foreign_host(
    board: W.Board, settings: Settings, data_dir: Path
) -> None:
    """011-AC10: DNS rebinding: a name other than the bound address reads neither a chat nor its files."""
    _media(data_dir)
    async with _served(board, settings) as host, httpx.AsyncClient() as client:
        thread = quote(T, safe="")
        for path in (f"/chat/{thread}", f"/media/{thread}/{OBJ}.png"):
            assert (await client.get(f"http://{host}{path}")).status_code == 200, path
            for foreign in ("evil.example", f"evil.example:{host.rsplit(':', 1)[1]}", "localhost"):
                got = await client.get(f"http://{host}{path}", headers={"Host": foreign})
                assert got.status_code == 404, (path, foreign)


async def test_chat_socket_checks_the_origin(board: W.Board, settings: Settings) -> None:
    """011-AC10: `/ws?chat` has the board's Origin rule: a foreign or missing Origin gets 403."""
    async with _served(board, settings) as host:
        for origin in ("http://evil.example", "null", None):
            with pytest.raises(websockets.InvalidStatus) as exc:
                await websockets.connect(
                    f"ws://{host}/ws?chat", origin=websockets.Origin(origin) if origin else None
                )
            assert exc.value.response.status_code == 403, origin


async def test_send_never_reaches_a_followed_channel(
    board: W.Board, settings: Settings, sent: list[tuple[str, str]]
) -> None:
    """011-AC8 + 011-AC9: a followed archived channel is read-only: send is refused, no API call."""
    async with _served(board, settings) as host, _chat_socket(host) as chat:
        await chat.send(json.dumps({"follow": C}))
        answer = json.loads(await asyncio.wait_for(chat.recv(), 2))
        assert (answer["ok"], answer["read_only"]) == (True, True)
        await chat.send(json.dumps({"send": C, "text": "hi"}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {"sent": C, "error": "unknown chat"}
    assert sent == []
