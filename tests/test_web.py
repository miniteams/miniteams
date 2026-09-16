"""Web widget (spec 002): row mapping, scope filter, bind persistence, page + socket serving."""

import asyncio
import json
import socket
from typing import Any

import httpx
import pytest
import websockets

from miniteams import web as W
from miniteams.config import Settings
from miniteams.directory import Directory


@pytest.fixture
def quiet_directory(directory: Directory, monkeypatch: pytest.MonkeyPatch) -> Directory:
    """No network: label = thread id, unknown senders resolve to their MRI."""

    async def fake_label(self: Directory, thread_id: str) -> str:
        return f"label:{thread_id}"

    async def fake_display(self: Directory, mri: str) -> str:
        return f"name:{mri}"

    monkeypatch.setattr(Directory, "label", fake_label)
    monkeypatch.setattr(Directory, "display", fake_display)
    return directory


def _conv(thread_id: str, when: str, **last: Any) -> dict[str, Any]:
    return {"id": thread_id, "lastMessage": {"composetime": when, **last}}


# --- snippet / scope ---


def test_in_scope_private_group_meeting_not_channel() -> None:
    assert W.in_scope("19:a_b@unq.gbl.spaces")
    assert W.in_scope("19:group@thread.v2")
    assert W.in_scope("19:meeting_abc@thread.v2")
    assert not W.in_scope("19:channel@thread.tacv2")
    assert not W.in_scope("48:notes")


def test_snippet_strips_html_and_collapses_whitespace() -> None:
    assert W.snippet("RichText/Html", "<p>Hello &amp;\n  <b>bye</b></p>") == "Hello & bye"
    # A mention tag glued to the next word must not fuse the two.
    assert W.snippet("RichText/Html", '<at id="8:x">Bob</at>dis moi') == "Bob dis moi"
    assert W.snippet("Text", "  plain\ttext  ") == "plain text"


def test_snippet_truncates_with_ellipsis() -> None:
    out = W.snippet("Text", "x" * 500)
    assert len(out) == W._SNIPPET_LEN
    assert out.endswith("…")
    assert W.snippet("Text", "x" * W._SNIPPET_LEN) == "x" * W._SNIPPET_LEN  # boundary: untouched


def test_snippet_html_with_only_media_gets_a_marker() -> None:
    assert W.snippet("RichText/Html", '<div><img src="https://x/y.png" itemtype="…"></div>') == "🖼 image"
    assert W.snippet("RichText/Html", "<div><a href='x'></a></div>") == "📎 attachment"
    # Deleted messages come back from the list with their type but an empty body.
    assert W.snippet("RichText/Html", "  \n ") == "🗑 deleted"
    assert W.snippet("Text", "") == "🗑 deleted"


def test_snippet_file_and_card_posts_are_not_deleted() -> None:
    files = {"files": [{"fileName": "Facture.pdf"}, {"fileName": "notes.txt"}]}
    assert W.snippet("RichText/Html", "", files) == "📎 Facture.pdf, notes.txt"
    assert W.snippet("RichText/Html", "", {"files": [], "cards": [{"cardClientId": "x"}]}) == "🃏 card"
    assert W.snippet("RichText/Html", "<p>see attached</p>", files) == "see attached"  # body wins
    assert W.snippet("RichText/Html", "", {"files": [{}]}) == "📎 file"
    assert W.snippet("RichText/Html", "", {}) == "🗑 deleted"


def test_snippet_markers_for_non_text_types() -> None:
    assert W.snippet("RichText/Media_GenericFile", "<URIObject…>") == "📎 file"
    assert W.snippet("ThreadActivity/AddMember", "<addmember/>") == "👥 member added"
    assert W.snippet("Some/Unknown", "<x/>") == "[Unknown]"
    assert W.snippet("", "") == ""


# --- rows ---


async def test_row_prefers_imdisplayname_then_directory(quiet_directory: Directory) -> None:
    conv = _conv(
        "19:g@thread.v2",
        "2026-09-15T10:00:00Z",
        messagetype="Text",
        content="hi",
        imdisplayname="Alice",
        **{"from": "8:orgid:alice"},
    )
    row = await W.row_from_conversation(conv, quiet_directory)
    assert row == {
        "id": "19:g@thread.v2",
        "label": "label:19:g@thread.v2",
        "last_activity": "2026-09-15T10:00:00Z",
        "last_id": "",
        "read_id": "",
        "unread": False,
        "sender": "Alice",
        "text": "hi",
        "seen_at": None,
        "typing": [],
    }
    conv["lastMessage"].pop("imdisplayname")
    assert (await W.row_from_conversation(conv, quiet_directory))["sender"] == "name:8:orgid:alice"


async def test_row_falls_back_to_listing_topic_when_lookup_fails(directory: Directory, monkeypatch) -> None:
    async def unresolved(self: Directory, thread_id: str) -> str:
        return thread_id  # what label() returns when the thread fetch failed

    monkeypatch.setattr(Directory, "label", unresolved)
    conv = {**_conv("19:meeting_x@thread.v2", "2026-09-15T10:00:00Z"), "threadProperties": {"topic": "Sync"}}
    assert (await W.row_from_conversation(conv, directory))["label"] == "Sync"
    conv["threadProperties"] = {}
    assert (await W.row_from_conversation(conv, directory))["label"] == "19:meeting_x@thread.v2"


async def test_row_system_event_has_no_sender(quiet_directory: Directory) -> None:
    conv = _conv(
        "19:g@thread.v2",
        "2026-09-15T10:00:00Z",
        messagetype="ThreadActivity/AddMember",
        content="<addmember/>",
        **{"from": "19:g@thread.v2"},
    )
    row = await W.row_from_conversation(conv, quiet_directory)
    assert (row["sender"], row["text"]) == ("", "👥 member added")


async def test_row_without_last_message_is_empty_but_valid(quiet_directory: Directory) -> None:
    row = await W.row_from_conversation({"id": "19:x@thread.v2", "version": 1757930400000}, quiet_directory)
    assert row["sender"] == "" and row["text"] == ""
    assert row["last_activity"] == "2025-09-15T10:00:00Z"  # falls back to version


async def test_bootstrap_filters_scope_and_caps(quiet_directory: Directory) -> None:
    pages = [
        [
            _conv("19:chan@thread.tacv2", "2026-09-15T12:00:00Z"),  # channel: out
            {"id": "19:noinfo@thread.v2"},  # no activity at all: out
            _conv("19:a@unq.gbl.spaces", "2026-09-15T11:00:00Z"),
            _conv("19:meeting_m@thread.v2", "2026-09-15T10:00:00Z"),
        ],
        [_conv("19:b@thread.v2", "2026-09-15T09:00:00Z")],
    ]
    rows = await W.bootstrap(pages, quiet_directory, limit=2)
    assert list(rows) == ["19:a@unq.gbl.spaces", "19:meeting_m@thread.v2"]  # capped before page 2
    assert list(await W.bootstrap(pages, quiet_directory, limit=0)) == [
        "19:a@unq.gbl.spaces",
        "19:meeting_m@thread.v2",
        "19:b@thread.v2",
    ]


async def test_bootstrap_resolves_ambiguous_stubs_with_one_history_call(
    quiet_directory: Directory, monkeypatch
) -> None:
    fetched: list[str] = []
    full = {
        "19:file@thread.v2": {
            "id": "1",
            "messagetype": "RichText/Html",
            "content": "",
            "properties": {"files": [{"fileName": "Facture.pdf"}]},
            "imdisplayname": "Ed",
        },
        "19:gone@thread.v2": {
            "id": "2",
            "messagetype": "RichText/Html",
            "content": "",
            "properties": {"deletetime": "1"},
        },
        "19:moved@thread.v2": {"id": "other", "messagetype": "Text", "content": "newer"},  # id mismatch
    }

    def fake_history(settings: Any, token: str, thread_id: str, page_size: int, max_pages: int) -> list[Any]:
        fetched.append(thread_id)
        assert page_size > 1  # pageSize=1 skips the newest message on the real API
        if thread_id == "19:down@thread.v2":
            raise RuntimeError("503")
        return [{"id": "older", "messagetype": "Text", "content": "x"}, full[thread_id]]

    monkeypatch.setattr(W, "fetch_history", fake_history)
    stub = {"messagetype": "RichText/Html", "content": ""}
    pages = [
        [
            _conv("19:file@thread.v2", "2026-09-15T10:00:00Z", id="1", **stub),
            _conv("19:gone@thread.v2", "2026-09-15T09:00:00Z", id="2", **stub),
            _conv("19:moved@thread.v2", "2026-09-15T08:00:00Z", id="3", **stub),
            _conv("19:down@thread.v2", "2026-09-15T07:00:00Z", id="4", **stub),
            _conv("19:text@thread.v2", "2026-09-15T06:00:00Z", id="5", messagetype="Text", content="hi"),
        ]
    ]
    rows = await W.bootstrap(pages, quiet_directory, limit=0)
    assert (
        rows["19:file@thread.v2"]["text"] == "📎 Facture.pdf" and rows["19:file@thread.v2"]["sender"] == "Ed"
    )
    assert rows["19:gone@thread.v2"]["text"] == "🗑 deleted"
    assert rows["19:moved@thread.v2"]["text"] == "🗑 deleted"  # stale stub: keep, do not guess
    assert rows["19:down@thread.v2"]["text"] == "🗑 deleted"  # fetch failed: bootstrap survives
    assert sorted(fetched) == [
        "19:down@thread.v2",
        "19:file@thread.v2",
        "19:gone@thread.v2",
        "19:moved@thread.v2",
    ]


def test_unread_from_consumption_horizon() -> None:
    assert W.read_up_to({"consumptionhorizon": "1789308085736;1789370244184;175117"}) == "1789308085736"
    assert W.read_up_to({}) == "" and W.read_up_to(None) == ""
    assert W.is_unread("1789558193478", "1789308085736") is True
    assert W.is_unread("1789308085736", "1789308085736") is False
    assert W.is_unread("1789000000000", "1789308085736") is False
    assert W.is_unread("", "1") is False and W.is_unread("1", "") is False  # unknown: never bold


async def test_row_unread_flag_from_listing(quiet_directory: Directory) -> None:
    conv = {
        **_conv("19:g@thread.v2", "2026-09-15T10:00:00Z", id="200", messagetype="Text", content="x"),
        "properties": {"consumptionhorizon": "100;1;1"},
    }
    row = await W.row_from_conversation(conv, quiet_directory)
    assert (row["read_id"], row["unread"]) == ("100", True)
    conv["properties"] = {"consumptionhorizon": "200;1;1"}
    assert (await W.row_from_conversation(conv, quiet_directory))["unread"] is False


async def test_read_marker_moves_with_devices_and_new_messages(board: W.Board) -> None:
    row = board.rows["19:a@thread.v2"]
    row["read_id"] = "10"
    row["unread"] = False
    await board.on_event(_msg("19:a@thread.v2", "2026-09-15T12:00:00Z", "ping", msg_id="11"))
    assert row["unread"] is True
    # Read on the phone: Teams pushes a ConversationUpdate keyed by the thread id.
    update = {
        "type": "EventMessage",
        "resourceType": "ConversationUpdate",
        "resource": {
            "id": "19:a@thread.v2",
            "properties": {"consumptionhorizon": "11;1;1"},
            "lastMessage": {"id": "11"},
        },
    }
    await board.on_event(update)
    assert (row["read_id"], row["unread"]) == ("11", False)
    # Unknown thread or missing horizon: ignored, no row created.
    await board.on_event(
        {
            "type": "EventMessage",
            "resourceType": "ConversationUpdate",
            "resource": {"id": "19:ghost@thread.v2", "properties": {"consumptionhorizon": "1;1;1"}},
        }
    )
    await board.on_event(
        {
            "type": "EventMessage",
            "resourceType": "ConversationUpdate",
            "resource": {"id": "19:a@thread.v2", "properties": {}},
        }
    )
    assert "19:ghost@thread.v2" not in board.rows and row["read_id"] == "11"


async def test_page_version_in_payload_and_watch_broadcasts_on_change(monkeypatch) -> None:
    versions = iter(["v1", "v1", "v2", "v2"])
    monkeypatch.setattr(W, "page_version", lambda: next(versions))
    board = W.Board({})
    assert json.loads(board.payload())["page"] == "v1"
    sent: list[str] = []
    monkeypatch.setattr(board, "broadcast", lambda: sent.append("b"))
    task = asyncio.create_task(board.watch_page(interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    assert sent == ["b"]  # exactly one broadcast, when v1 → v2 was observed


def test_board_payload_sorted_newest_first() -> None:
    board = W.Board(
        {
            "old": {"id": "old", "last_activity": "2026-01-01T00:00:00Z"},
            "new": {"id": "new", "last_activity": "2026-09-01T00:00:00Z"},
        }
    )
    assert [r["id"] for r in json.loads(board.payload())["rows"]] == ["new", "old"]


# --- bind ---


def test_parse_bind() -> None:
    assert W.parse_bind("127.0.0.42:8080") == ("127.0.0.42", 8080)
    assert W.parse_bind("::1:8080") == ("::1", 8080)
    for bad in ("nohost", ":80", "host:", "host:abc"):
        with pytest.raises(ValueError):
            W.parse_bind(bad)


def test_parse_bind_refuses_non_loopback() -> None:
    for exposed in ("0.0.0.0:8080", "192.168.1.10:8080", ":::80"):
        with pytest.raises(ValueError, match="loopback"):
            W.parse_bind(exposed)
    with pytest.raises(ValueError):  # bracketed v6 is not parsed at all → still refused
        W.parse_bind("[::]:80")


def test_load_bind_draws_once_then_persists(settings: Settings) -> None:
    host, port = W.load_bind(settings)
    assert host.startswith("127.0.0.") and 2 <= int(host.rsplit(".", 1)[1]) <= 254
    assert 32768 <= port <= 60999
    assert W.load_bind(settings) == (host, port)  # second run: same URL
    assert json.loads((settings.config_dir / "web.json").read_text()) == {"host": host, "port": port}


def test_load_bind_override_wins_and_does_not_persist(settings: Settings) -> None:
    assert W.load_bind(settings, "127.0.0.9:9999") == ("127.0.0.9", 9999)
    assert not (settings.config_dir / "web.json").exists()


def test_load_bind_recovers_from_corrupt_file(settings: Settings) -> None:
    settings.config_dir.mkdir(parents=True)
    (settings.config_dir / "web.json").write_text("{not json")
    host, port = W.load_bind(settings)
    assert host.startswith("127.0.0.")
    assert json.loads((settings.config_dir / "web.json").read_text())["port"] == port


# --- serving ---


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_serve_page_and_initial_rows(settings: Settings) -> None:
    board = W.Board({"t": {"id": "t", "last_activity": "2026-09-15T10:00:00Z", "label": "L"}})
    port = _free_port()
    task = asyncio.create_task(W.serve_board(board, settings, f"127.0.0.1:{port}"))
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(50):  # server task needs a few loop turns to bind
                await asyncio.sleep(0.02)
                try:
                    page = await client.get(f"http://127.0.0.1:{port}/")
                    break
                except httpx.ConnectError:
                    continue
            assert page.status_code == 200
            assert page.headers["content-type"].startswith("text/html")
            assert "<title>miniteams</title>" in page.text
            assert (await client.get(f"http://127.0.0.1:{port}/nope")).status_code == 404
        own = f"http://127.0.0.1:{port}"  # what the served page sends; the server accepts only this
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws", origin=own) as ws:
            first = json.loads(await ws.recv())
            assert first["rows"][0]["id"] == "t"
            assert len(board.clients) == 1
            board.rows["u"] = {"id": "u", "last_activity": "2026-09-15T11:00:00Z", "label": "U"}
            board.broadcast()
            assert [r["id"] for r in json.loads(await ws.recv())["rows"]] == ["u", "t"]
        await asyncio.sleep(0.05)
        assert board.clients == set()
        # Another site's page — or no page at all — must not read the list (cross-site websocket
        # hijacking). Both shapes: foreign Origin and missing Origin.
        for origin in ("http://evil.example", None):
            with pytest.raises(websockets.InvalidStatus) as exc:
                await websockets.connect(f"ws://127.0.0.1:{port}/ws", origin=origin)
            assert exc.value.response.status_code == 403
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_serve_redraws_when_persisted_port_is_taken(settings: Settings, monkeypatch) -> None:
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    busy = int(taken.getsockname()[1])
    W.save_bind(settings, "127.0.0.1", busy)
    free = _free_port()
    monkeypatch.setattr(W, "_draw_bind", lambda: ("127.0.0.1", free))
    task = asyncio.create_task(W.serve_board(W.Board({}), settings, None))
    try:
        await asyncio.sleep(0.2)
        assert json.loads((settings.config_dir / "web.json").read_text()) == {
            "host": "127.0.0.1",
            "port": free,
        }
        async with websockets.connect(f"ws://127.0.0.1:{free}/ws", origin=f"http://127.0.0.1:{free}") as ws:
            assert json.loads(await ws.recv())["rows"] == []
    finally:
        taken.close()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# --- live events (phase 2) ---


def _event(resource_type: str, thread_id: str, **resource: Any) -> dict[str, Any]:
    base = {"conversationLink": f"https://h/v1/users/ME/conversations/{thread_id}", "imdisplayname": "Bob"}
    return {"type": "EventMessage", "resourceType": resource_type, "resource": {**base, **resource}}


def _msg(thread_id: str, when: str, text: str, msg_id: str = "1", **extra: Any) -> dict[str, Any]:
    return _event(
        "NewMessage", thread_id, id=msg_id, composetime=when, messagetype="Text", content=text, **extra
    )


@pytest.fixture
def board(quiet_directory: Directory) -> W.Board:
    rows = {
        "19:a@thread.v2": {
            "id": "19:a@thread.v2",
            "label": "A",
            "last_activity": "2026-09-15T10:00:00Z",
            "last_id": "10",
            "sender": "Ann",
            "text": "old",
            "seen_at": None,
            "typing": [],
        },
        "19:b@thread.v2": {
            "id": "19:b@thread.v2",
            "label": "B",
            "last_activity": "2026-09-15T11:00:00Z",
            "last_id": "20",
            "sender": "Ben",
            "text": "newer",
            "seen_at": None,
            "typing": [],
        },
    }
    return W.Board(rows, quiet_directory, typing_ttl=0.05)


def _order(board: W.Board) -> list[str]:
    return [r["id"] for r in json.loads(board.payload())["rows"]]


async def test_new_message_moves_row_to_top(board: W.Board) -> None:
    assert _order(board) == ["19:b@thread.v2", "19:a@thread.v2"]
    await board.on_event(_msg("19:a@thread.v2", "2026-09-15T12:00:00Z", "hi <b>there</b>", msg_id="11"))
    assert _order(board) == ["19:a@thread.v2", "19:b@thread.v2"]
    row = board.rows["19:a@thread.v2"]
    assert (row["sender"], row["text"], row["last_id"]) == ("Bob", "hi <b>there</b>", "11")


async def test_rename_and_roster_change_refresh_the_label(board: W.Board, monkeypatch) -> None:
    labels = iter(["Renamed · 3p", "Renamed · 4p"])
    forgotten: list[str] = []
    monkeypatch.setattr(Directory, "forget", lambda self, t: forgotten.append(t))

    async def fake_label(self: Directory, thread_id: str) -> str:
        return next(labels)

    monkeypatch.setattr(Directory, "label", fake_label)
    rename = _event(
        "NewMessage",
        "19:a@thread.v2",
        id="12",
        composetime="2026-09-15T12:00:00Z",
        messagetype="ThreadActivity/TopicUpdate",
        content="<topicupdate><value>Renamed</value></topicupdate>",
        **{"from": "19:a@thread.v2"},
    )
    await board.on_event(rename)
    row = board.rows["19:a@thread.v2"]
    assert (row["label"], row["sender"], row["text"]) == ("Renamed · 3p", "", "✎ renamed")
    assert forgotten == ["19:a@thread.v2"]
    joined = dict(rename, resource=dict(rename["resource"], id="13", messagetype="ThreadActivity/AddMember"))
    await board.on_event(joined)
    assert row["label"] == "Renamed · 4p" and row["text"] == "👥 member added"
    plain = _msg("19:a@thread.v2", "2026-09-15T13:00:00Z", "hi", msg_id="14")
    await board.on_event(plain)
    assert forgotten == ["19:a@thread.v2", "19:a@thread.v2"]  # a normal message does not refetch
    # Removed from the chat (or rate-limited): the lookup yields the bare id → keep the old name.
    labels = iter(["19:a@thread.v2"])
    left = dict(joined, resource=dict(joined["resource"], id="15", messagetype="ThreadActivity/DeleteMember"))
    await board.on_event(left)
    assert row["label"] == "Renamed · 4p" and row["text"] == "👥 member removed"


async def test_new_message_on_unknown_thread_creates_row(board: W.Board) -> None:
    await board.on_event(_msg("19:new@unq.gbl.spaces", "2026-09-15T13:00:00Z", "yo"))
    assert _order(board)[0] == "19:new@unq.gbl.spaces"
    assert board.rows["19:new@unq.gbl.spaces"]["label"] == "label:19:new@unq.gbl.spaces"


async def test_out_of_scope_and_control_events_are_ignored(board: W.Board) -> None:
    before = json.loads(board.payload())
    await board.on_event(_msg("19:chan@thread.tacv2", "2026-09-15T13:00:00Z", "channel"))
    await board.on_event(_msg("48:notes", "2026-09-15T13:00:00Z", "notes"))
    await board.on_event(_event("NewMessage", "19:a@thread.v2", messagetype="Control/ReadReceipt"))
    await board.on_event(
        _event(
            "NewMessage",
            "19:a@thread.v2",
            id="99",
            composetime="2026-09-15T14:00:00Z",
            messagetype="ThreadActivity/MemberConsumptionHorizonUpdate",
            content="<x/>",
        )
    )
    await board.on_event(_event("ThreadUpdate", "19:a@thread.v2"))
    assert json.loads(board.payload()) == before


async def test_typing_sets_clears_and_expires(board: W.Board) -> None:
    await board.on_event(_event("NewMessage", "19:a@thread.v2", messagetype="Control/Typing"))
    await board.on_event(_event("NewMessage", "19:a@thread.v2", messagetype="Control/Typing"))  # idempotent
    assert board.rows["19:a@thread.v2"]["typing"] == ["Bob"]
    await board.on_event(_event("NewMessage", "19:a@thread.v2", messagetype="Control/ClearTyping"))
    assert board.rows["19:a@thread.v2"]["typing"] == []
    await board.on_event(_event("NewMessage", "19:a@thread.v2", messagetype="Control/Typing"))
    await asyncio.sleep(0.1)  # > typing_ttl: Teams may never send ClearTyping
    assert board.rows["19:a@thread.v2"]["typing"] == []
    assert board._typing_timers == {}
    # Typing on a thread we never listed is not an event worth a row.
    await board.on_event(_event("NewMessage", "19:ghost@thread.v2", messagetype="Control/Typing"))
    assert "19:ghost@thread.v2" not in board.rows


async def test_message_ends_senders_typing(board: W.Board) -> None:
    await board.on_event(_event("NewMessage", "19:a@thread.v2", messagetype="Control/Typing"))
    await board.on_event(_msg("19:a@thread.v2", "2026-09-15T12:00:00Z", "sent"))
    assert board.rows["19:a@thread.v2"]["typing"] == []
    assert board._typing_timers == {}


async def test_edit_and_delete_only_touch_the_last_message(board: W.Board) -> None:
    edit = _event(
        "MessageUpdate",
        "19:a@thread.v2",
        id="10",
        messagetype="Text",
        content="fixed",
        skypeeditedid="10",
        properties={"edittime": "1"},
    )
    await board.on_event(edit)
    assert board.rows["19:a@thread.v2"]["text"] == "fixed"
    assert board.rows["19:a@thread.v2"]["last_activity"] == "2026-09-15T10:00:00Z"  # an edit is not a bump
    older = _event(
        "MessageUpdate",
        "19:a@thread.v2",
        id="9",
        messagetype="Text",
        content="older",
        properties={"edittime": "1"},
    )
    await board.on_event(older)
    assert board.rows["19:a@thread.v2"]["text"] == "fixed"
    gone = _event(
        "MessageUpdate",
        "19:a@thread.v2",
        id="10",
        messagetype="Text",
        content="",
        properties={"deletetime": "1"},
    )
    await board.on_event(gone)
    assert board.rows["19:a@thread.v2"]["text"] == "🗑 deleted"
    # An update carrying neither edit nor delete nor emotions is noise.
    await board.on_event(_event("MessageUpdate", "19:a@thread.v2", id="10", properties={}))
    assert board.rows["19:a@thread.v2"]["text"] == "🗑 deleted"


def _reaction(thread_id: str, msg_id: str, key: str, mri: str, time_ms: int) -> dict[str, Any]:
    return _event(
        "MessageUpdate",
        thread_id,
        id=msg_id,
        messagetype="Text",
        content="old",
        properties={"emotions": [{"key": key, "users": [{"mri": mri, "time": time_ms}]}]},
    )


async def test_reaction_ignored_unless_enabled(board: W.Board) -> None:
    before = json.loads(board.payload())
    await board.on_event(_reaction("19:a@thread.v2", "10", "like", "8:orgid:x", 1789600000000))
    assert json.loads(board.payload()) == before


async def test_reaction_becomes_last_event_and_bumps(board: W.Board) -> None:
    board.reactions = True
    await board.on_event(_reaction("19:a@thread.v2", "10", "like", "8:orgid:x", 1789600000000))
    row = board.rows["19:a@thread.v2"]
    assert row["text"] == "👍 name:8:orgid:x reacted" and row["sender"] == ""
    assert row["last_activity"] == "2026-09-16T23:06:40.000Z"
    assert _order(board)[0] == "19:a@thread.v2"
    # Same reaction re-delivered (nothing added) → no change; unreact → no change either.
    await board.on_event(_reaction("19:a@thread.v2", "10", "like", "8:orgid:x", 1789600000000))
    assert row["last_activity"] == "2026-09-16T23:06:40.000Z"
    await board.on_event(_reaction("19:unknown@thread.v2", "1", "like", "8:orgid:x", 1))  # no row: ignored
    assert "19:unknown@thread.v2" not in board.rows


# --- seen marker (phase 3) ---


async def test_seen_persists_and_newer_message_unhides(
    quiet_directory: Directory, settings: Settings
) -> None:
    seen_path = settings.config_dir / "seen.json"
    rows = {
        "19:a@thread.v2": {
            "id": "19:a@thread.v2",
            "label": "A",
            "last_activity": "2026-09-15T10:00:00Z",
            "last_id": "1",
            "sender": "",
            "text": "",
            "seen_at": None,
            "typing": [],
        }
    }
    board = W.Board(rows, quiet_directory, seen_path=seen_path)
    assert board.mark_seen("19:a@thread.v2") is True
    assert rows["19:a@thread.v2"]["seen_at"] == "2026-09-15T10:00:00Z"
    assert json.loads(seen_path.read_text()) == {"19:a@thread.v2": "2026-09-15T10:00:00Z"}
    assert board.mark_seen("19:nope@thread.v2") is False  # unknown id: no-op, nothing written
    # The page says what it showed; a message that landed since keeps the row visible.
    rows["19:a@thread.v2"]["last_activity"] = "2026-09-15T11:00:00Z"
    assert board.mark_seen("19:a@thread.v2", at="2026-09-15T10:30:00Z") is True
    assert rows["19:a@thread.v2"]["seen_at"] == "2026-09-15T10:30:00Z"
    board.mark_seen("19:a@thread.v2", at="2026-09-15T23:00:00Z")  # a stamp from the future is clamped
    assert rows["19:a@thread.v2"]["seen_at"] == "2026-09-15T11:00:00Z"
    # A restart reloads the marker onto the bootstrap rows…
    again = W.Board(
        {"19:a@thread.v2": dict(rows["19:a@thread.v2"], seen_at=None)}, quiet_directory, seen_path=seen_path
    )
    assert again.rows["19:a@thread.v2"]["seen_at"] == "2026-09-15T11:00:00Z"
    # …and a newer message moves last_activity past it: the page un-hides (seen_at < last_activity).
    await again.on_event(_msg("19:a@thread.v2", "2026-09-15T12:00:00Z", "new"))
    row = again.rows["19:a@thread.v2"]
    assert row["seen_at"] < row["last_activity"]
    # A thread first seen live still carries its stored marker.
    await again.on_event(_msg("19:late@thread.v2", "2026-09-15T12:00:00Z", "x"))
    assert again.rows["19:late@thread.v2"]["seen_at"] is None


def test_load_seen_tolerates_missing_or_corrupt(settings: Settings) -> None:
    path = settings.config_dir / "seen.json"
    assert W._load_seen(None) == {} and W._load_seen(path) == {}
    settings.config_dir.mkdir(parents=True)
    path.write_text("[1, 2]")
    assert W._load_seen(path) == {}
    path.write_text('{"19:a@thread.v2": "2026-09-15T10:00:00Z"}')
    assert W._load_seen(path) == {"19:a@thread.v2": "2026-09-15T10:00:00Z"}


async def test_seen_verb_over_websocket(settings: Settings) -> None:
    board = W.Board(
        {"t": {"id": "t", "last_activity": "2026-09-15T10:00:00Z", "label": "L", "seen_at": None}},
        seen_path=settings.config_dir / "seen.json",
    )
    port = _free_port()
    task = asyncio.create_task(W.serve_board(board, settings, f"127.0.0.1:{port}"))
    try:
        await asyncio.sleep(0.2)
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}") as ws:
            await ws.recv()
            await ws.send("not json")  # ignored, socket stays up
            await ws.send(json.dumps({"seen": 42}))  # wrong shape, ignored
            await ws.send(json.dumps({"seen": "t", "at": 5}))  # bad `at` shape → ignored, id still stamped
            frame = json.loads(await ws.recv())
            assert frame["rows"][0]["seen_at"] == "2026-09-15T10:00:00Z"
        assert json.loads((settings.config_dir / "seen.json").read_text()) == {"t": "2026-09-15T10:00:00Z"}
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
