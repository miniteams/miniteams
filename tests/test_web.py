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


def test_snippet_drops_a_leading_reply_quote() -> None:
    quote = (
        '<blockquote itemscope itemtype="http://schema.skype.com/Reply" itemid="177">'
        '<strong itemprop="mri" itemid="8:orgid:x">Bob</strong>'
        '<span itemprop="time" itemid="177"></span>'
        '<p itemprop="preview">question</p></blockquote>'
    )
    assert W.snippet("RichText/Html", quote + "\n<p>ma réponse</p>") == "ma réponse"
    # CRLF-separated variant, and the quote attributes in the other order Teams emits.
    crlf = quote.replace("itemscope ", 'itemscope="" ')
    assert W.snippet("RichText/Html", crlf + "\r\n<p>oui</p>") == "oui"
    # Nothing but the quote: keep it, an empty preview says less than the quoted text.
    assert W.snippet("RichText/Html", quote) == "Bob question"
    # A trailing quote is the sender's own words first — left alone.
    assert W.snippet("RichText/Html", "<p>mon texte</p>" + quote) == "mon texte Bob question"
    # Older quotes carry no itemtype at all.
    bare = "<blockquote>\r\n<p>la question</p>\r\n</blockquote>\r\n<p>la réponse</p>"
    assert W.snippet("RichText/Html", bare) == "la réponse"


def test_snippet_keeps_a_forwarded_body() -> None:
    # A forward is a blockquote whose content is the message itself, not a quote of another one.
    fwd = (
        '<blockquote itemtype="http://schema.skype.com/Forward"><p>Hello</p>'
        "<p>le contenu transféré</p></blockquote>"
    )
    assert W.snippet("RichText/Html", fwd) == "Hello le contenu transféré"
    assert W.snippet("RichText/Html", fwd + "<p>fyi</p>") == "Hello le contenu transféré fyi"


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
    # Teams ships files/cards as JSON strings: an empty '[]' is not a card.
    assert W.snippet("RichText/Html", '<p><img src="x"></p>', {"cards": "[]", "files": "[]"}) == "🖼 image"
    assert W.snippet("RichText/Html", "", {"cards": '[{"cardClientId": "x"}]', "files": "[]"}) == "🃏 card"
    assert W.snippet("RichText/Html", "", {"files": '[{"fileName": "a.pdf"}]'}) == "📎 a.pdf"
    assert W.snippet("RichText/Html", "", {"files": "not json"}) == "🗑 deleted"


def test_snippet_renders_emoji_alt() -> None:
    assert (
        W.snippet("RichText/Html", '<p><emoji id="1f600_grinningface" alt="😀" title="Grinning"></emoji></p>')
        == "😀"
    )
    assert W.snippet("RichText/Html", '<p>ok <emoji id="x" alt="👍" title="thumbs"></emoji></p>') == "ok 👍"


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
        "read_at": 0,
        "unread": False,
        "mention": None,
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


def test_deep_link_formats() -> None:
    link = W.deep_link("19:e45a@thread.v2", "1789552947563")
    assert link == (
        "https://teams.microsoft.com/l/message/19%3Ae45a%40thread.v2/1789552947563"
        "?context=%7B%22contextType%22%3A%22chat%22%7D"
    )
    assert W.deep_link("19:e45a@thread.v2", "1", "msteams").startswith(
        "msteams://teams.cloud.microsoft/l/message/"
    )


async def test_open_row_launches_opener_or_browser_without_marking_seen(
    settings: Settings, monkeypatch
) -> None:
    launched: list[tuple[str, ...]] = []

    class _Proc:
        async def wait(self) -> int:
            return 0

    async def fake_exec(*argv: str, **kw: Any) -> _Proc:
        launched.append(argv)
        return _Proc()

    monkeypatch.setattr(W.asyncio, "create_subprocess_exec", fake_exec)
    rows = {"t": {"id": "t", "last_activity": "2026-09-15T10:00:00Z", "last_id": "42", "seen_at": None}}
    board = W.Board(
        rows, seen_path=settings.config_dir / "seen.json", opener=["xdg-open"], browser=["firefox"]
    )
    assert await board.open_row("t") is True
    assert launched == [("xdg-open", W.deep_link("t", "42", "msteams"))]
    assert rows["t"]["seen_at"] is None  # opening is not reading
    assert await board.open_row("t", web=True) is True
    assert launched[-1] == ("firefox", W.deep_link("t", "42", "https"))
    assert await board.open_row("nope") is False and len(launched) == 2
    assert json.loads(board.payload())["opener"] is True and json.loads(board.payload())["browser"] is True
    assert json.loads(board.payload())["rows"][0]["link"] == W.deep_link("t", "42")
    # No opener configured: the verb is a no-op and the page is told to follow its own link.
    plain = W.Board(dict(rows), opener=None)
    assert await plain.open_row("t") is False and json.loads(plain.payload())["opener"] is False
    assert await plain.open_row("t", web=True) is False and json.loads(plain.payload())["browser"] is False


async def test_open_row_survives_missing_opener_binary(settings: Settings) -> None:
    rows = {"t": {"id": "t", "last_activity": "2026-09-15T10:00:00Z", "last_id": "42", "seen_at": None}}
    board = W.Board(rows, opener=["/nonexistent/opener"])
    assert await board.open_row("t") is False
    assert rows["t"]["seen_at"] is None  # not marked seen when nothing opened


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
            # View params (theme, avatars, tab) ride the query string of the same page.
            view = await client.get(f"http://127.0.0.1:{port}/?theme=dark&avatars=0")
            assert view.status_code == 200 and "<title>miniteams</title>" in view.text
            assert (await client.get(f"http://127.0.0.1:{port}/nope")).status_code == 404
            assert (await client.get(f"http://127.0.0.1:{port}/nope?theme=dark")).status_code == 404
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


async def test_bare_id_label_is_retried_on_next_message(board: W.Board, monkeypatch) -> None:
    calls: list[str] = []
    answers = iter(["19:a@thread.v2", "Alice, Bob"])  # failed lookup (expired token, 429), then ok

    async def fake_label(self: Directory, thread_id: str) -> str:
        calls.append(thread_id)
        return next(answers)

    monkeypatch.setattr(Directory, "label", fake_label)
    row = board.rows["19:a@thread.v2"]
    row["label"] = "19:a@thread.v2"
    await board.on_event(_msg("19:a@thread.v2", "2026-09-15T12:00:00Z", "hi", msg_id="11"))
    assert row["label"] == "19:a@thread.v2"
    await board.on_event(_msg("19:a@thread.v2", "2026-09-15T12:01:00Z", "hi", msg_id="12"))
    assert row["label"] == "Alice, Bob"
    await board.on_event(_msg("19:a@thread.v2", "2026-09-15T12:02:00Z", "hi", msg_id="13"))
    assert calls == ["19:a@thread.v2", "19:a@thread.v2"]  # resolved: no further lookup


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


def test_unseen_undoes_seen_and_persists(settings: Settings) -> None:
    seen_path = settings.config_dir / "seen.json"
    rows = {"t": {"id": "t", "last_activity": "2026-09-15T10:00:00Z", "last_id": "1", "seen_at": None}}
    board = W.Board(rows, seen_path=seen_path)
    assert board.mark_unseen("t") is False  # nothing to undo
    board.mark_seen("t")
    assert board.mark_unseen("t") is True
    assert rows["t"]["seen_at"] is None and json.loads(seen_path.read_text()) == {}
    assert board.mark_unseen("nope") is False


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
            await ws.send(json.dumps({"unseen": "t"}))
            assert json.loads(await ws.recv())["rows"][0]["seen_at"] is None
            await ws.send(json.dumps({"mute": "t"}))
            assert json.loads(await ws.recv())["rows"][0]["muted"] is True
            await ws.send(json.dumps({"unmute": "t"}))
            assert json.loads(await ws.recv())["rows"][0]["muted"] is False
            await ws.send(json.dumps({"seen": "t"}))
            await ws.recv()
        assert json.loads((settings.config_dir / "seen.json").read_text()) == {"t": "2026-09-15T10:00:00Z"}
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# --- mentions (spec 003, phase 1) ---

ME = "8:orgid:me-guid"


def _mentions(*entries: dict[str, Any]) -> str:
    return json.dumps(list(entries))  # Teams ships the list as a JSON string


def _person(mri: str) -> dict[str, Any]:
    return {
        "@type": "http://schema.skype.com/Mention",
        "mentionType": "person",
        "mri": mri,
        "displayName": "x",
    }


def _everyone(thread_id: str) -> dict[str, Any]:
    return {"mentionType": "everyone", "mri": thread_id, "displayName": "Tout le monde"}


@pytest.fixture
def mboard(quiet_directory: Directory, settings: Settings) -> W.Board:
    rows = {
        "19:a@thread.v2": {
            "id": "19:a@thread.v2",
            "label": "A",
            "last_activity": "2026-09-15T10:00:00Z",
            "last_id": "1789466400000",
            "read_id": "1789466400000",
            "read_at": W._ms("2026-09-15T10:00:05Z"),  # read right after the last message
            "unread": False,
            "sender": "Ann",
            "text": "old",
            "seen_at": None,
            "typing": [],
        }
    }
    return W.Board(rows, quiet_directory, seen_path=settings.config_dir / "seen.json", me=ME)


def test_mention_kind_person_everyone_bot_contact_self() -> None:
    props = lambda *e: {"properties": {"mentions": _mentions(*e)}}  # noqa: E731
    assert W.mention_kind({**props(_person(ME)), "from": "8:orgid:other"}, ME) == "me"
    assert W.mention_kind({**props(_everyone("19:t@thread.v2")), "from": "8:orgid:other"}, ME) == "all"
    # A direct entry wins over an @everyone in the same message, whatever the order.
    assert W.mention_kind({**props(_everyone("19:t"), _person(ME)), "from": "8:orgid:o"}, ME) == "me"
    assert W.mention_kind(props(_person("8:orgid:someone-else")), ME) is None
    assert W.mention_kind(props({"mentionType": "bot", "mri": "28:bot"}), ME) is None
    assert W.mention_kind(props({"mentionType": "BOT", "mri": "28:bot"}), ME) is None
    assert W.mention_kind(props({"mentionType": "share-contact", "mri": ME}), ME) is None
    # Own message (from ends with the MRI, as in `https://.../contacts/8:orgid:...`).
    assert W.mention_kind({**props(_person(ME)), "from": f"https://h/v1/users/ME/contacts/{ME}"}, ME) is None
    # Lists (already decoded), garbage strings and no `me` at all.
    assert W.mention_kind({"properties": {"mentions": [_person(ME)]}}, ME) == "me"
    assert W.mention_kind({"properties": {"mentions": "not json"}}, ME) is None
    assert W.mention_kind(props(_person(ME)), "") is None


def test_mention_time_prefers_edittime() -> None:
    msg = {"composetime": "2026-09-15T10:00:00Z", "properties": {"edittime": "1789470000000"}}
    assert W.mention_time(msg) == (W._iso(1789470000000), True)
    assert W.mention_time({"composetime": "2026-09-15T10:00:00Z"}) == ("2026-09-15T10:00:00Z", False)
    assert W.mention_time(
        {"originalarrivaltime": "2026-09-15T10:00:00Z", "properties": {"edittime": ""}}
    ) == (
        "2026-09-15T10:00:00Z",
        False,
    )


def test_ms_and_read_at_parse_teams_shapes() -> None:
    assert W._ms("2024-07-11T14:52:51.8610000Z") == 1720709571861  # 7-digit fraction
    assert W._ms("2026-09-15T10:00:00Z") == 1789466400000
    assert W._ms(None) == 0 and W._ms("") == 0 and W._ms("garbage") == 0
    assert W.read_at_ms({"consumptionhorizon": "1789308085736;1789370244184;175117"}) == 1789370244184
    assert W.read_at_ms({"consumptionhorizon": "1789308085736"}) == 0 and W.read_at_ms(None) == 0


def test_self_mri_from_access_token() -> None:
    import base64

    def jwt(claims: dict[str, Any]) -> str:
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return f"eyJhbGciOiJub25lIn0.{body}.sig"

    assert W.self_mri(jwt({"oid": "abc-123", "upn": "x"})) == "8:orgid:abc-123"
    assert W.self_mri(jwt({"upn": "x"})) == ""
    assert W.self_mri("not.a-jwt") == "" and W.self_mri("") == ""


async def test_live_mention_sets_row_and_everyone_is_outranked(mboard: W.Board) -> None:
    t = "19:a@thread.v2"
    await mboard.on_event(
        _event(
            "NewMessage",
            t,
            id="1789473600000",
            composetime="2026-09-15T12:00:00Z",
            messagetype="RichText/Html",
            content="<p>ping <at>me</at></p>",
            properties={"mentions": _mentions(_person(ME))},
            **{"from": "8:orgid:bob"},
        )
    )
    row = mboard.rows[t]
    assert row["mention"] == {
        "kind": "me",
        "by": "Bob",
        "text": "ping me",
        "at": "2026-09-15T12:00:00Z",
        "edited": False,
        "msg_id": "1789473600000",
    }
    # A later @everyone does not downgrade the direct mention…
    await mboard.on_event(
        _msg(
            t,
            "2026-09-15T12:01:00Z",
            "all hands",
            msg_id="1789473660000",
            properties={"mentions": _mentions(_everyone(t))},
            **{"from": "8:orgid:bob"},
        )
    )
    assert row["mention"]["kind"] == "me" and row["mention"]["msg_id"] == "1789473600000"
    assert row["text"] == "all hands"  # …but the last message still moves on
    # A message mentioning someone else leaves it untouched.
    await mboard.on_event(
        _msg(
            t,
            "2026-09-15T12:02:00Z",
            "x",
            msg_id="1789473720000",
            properties={"mentions": _mentions(_person("8:orgid:zed"))},
        )
    )
    assert row["mention"]["kind"] == "me"


async def test_everyone_then_direct_upgrades(mboard: W.Board) -> None:
    t = "19:a@thread.v2"
    await mboard.on_event(
        _msg(
            t,
            "2026-09-15T12:00:00Z",
            "all",
            msg_id="1789473600000",
            properties={"mentions": _mentions(_everyone(t))},
            **{"from": "8:orgid:bob"},
        )
    )
    assert mboard.rows[t]["mention"]["kind"] == "all"
    await mboard.on_event(
        _msg(
            t,
            "2026-09-15T12:01:00Z",
            "you",
            msg_id="1789473660000",
            properties={"mentions": _mentions(_person(ME))},
            **{"from": "8:orgid:bob"},
        )
    )
    assert mboard.rows[t]["mention"]["kind"] == "me" and mboard.rows[t]["mention"]["text"] == "you"


async def test_edit_of_an_older_read_message_adds_a_mention(mboard: W.Board) -> None:
    t = "19:a@thread.v2"
    edit_ms = W._ms("2026-09-15T13:00:00Z")
    edit = _event(
        "MessageUpdate",
        t,
        id="1789460000000",
        messagetype="RichText/Html",
        content="<p>now <at>me</at></p>",
        skypeeditedid="1789460000000",
        composetime="2026-09-15T08:00:00Z",
        properties={"edittime": str(edit_ms), "mentions": _mentions(_person(ME))},
        **{"from": "8:orgid:bob"},
    )
    await mboard.on_event(edit)
    row = mboard.rows[t]
    assert row["mention"] == {
        "kind": "me",
        "by": "Bob",
        "text": "now me",
        "at": W._iso(edit_ms),
        "edited": True,
        "msg_id": "1789460000000",
    }
    assert (row["text"], row["last_activity"], row["unread"]) == ("old", "2026-09-15T10:00:00Z", False)
    # The same message edited again without the mention: cleared. A different message's edit: no-op.
    other = dict(
        edit, resource=dict(edit["resource"], id="1789461000000", properties={"edittime": str(edit_ms)})
    )
    await mboard.on_event(other)
    assert row["mention"]["msg_id"] == "1789460000000"
    gone = dict(
        edit, resource=dict(edit["resource"], properties={"edittime": str(edit_ms + 1)}, content="plain")
    )
    await mboard.on_event(gone)
    assert row["mention"] is None


async def test_deleting_the_mentioning_message_clears_it(mboard: W.Board) -> None:
    t = "19:a@thread.v2"
    await mboard.on_event(
        _msg(
            t,
            "2026-09-15T12:00:00Z",
            "you",
            msg_id="1789473600000",
            properties={"mentions": _mentions(_person(ME))},
            **{"from": "8:orgid:bob"},
        )
    )
    await mboard.on_event(
        _event(
            "MessageUpdate",
            t,
            id="1789473600000",
            messagetype="Text",
            content="",
            properties={"deletetime": "1"},
        )
    )
    assert mboard.rows[t]["mention"] is None and mboard.rows[t]["text"] == "🗑 deleted"


async def test_seen_clears_the_mention_and_survives_restart(mboard: W.Board, settings: Settings) -> None:
    t = "19:a@thread.v2"
    edit_ms = W._ms("2026-09-15T13:00:00Z")
    await mboard.on_event(
        _event(
            "MessageUpdate",
            t,
            id="1789460000000",
            messagetype="Text",
            content="you",
            skypeeditedid="1789460000000",
            composetime="2026-09-15T08:00:00Z",
            properties={"edittime": str(edit_ms), "mentions": _mentions(_person(ME))},
            **{"from": "8:orgid:bob"},
        )
    )
    row = mboard.rows[t]
    # The page sends the activity it displayed (older than the mention): the stamp still reaches it.
    assert mboard.mark_seen(t, at="2026-09-15T10:00:00Z")
    assert row["mention"] is None and W._ms(row["seen_at"]) >= edit_ms
    # Restart: bootstrap sees the same mention in history but the seen stamp is past it.
    seen = json.loads((settings.config_dir / "seen.json").read_text())
    fresh = {**row, "seen_at": seen[t], "mention": None}
    history = [
        {
            "id": "1789460000000",
            "messagetype": "Text",
            "content": "you",
            "composetime": "2026-09-15T08:00:00Z",
            "properties": {"edittime": str(edit_ms), "mentions": _mentions(_person(ME))},
            "from": "8:orgid:bob",
        }
    ]
    await W._scan_mentions(fresh, history, mboard.directory, ME)  # type: ignore[arg-type]
    assert fresh["mention"] is None


async def test_seen_clears_the_mention_but_still_caps_at_what_was_shown(mboard: W.Board) -> None:
    """Seen always clears the mention (spec 003); a plain message that landed after the render
    still keeps the row visible, as before."""
    t = "19:a@thread.v2"
    await mboard.on_event(
        _msg(
            t,
            "2026-09-15T12:00:00Z",
            "you",
            msg_id="1789473600000",
            properties={"mentions": _mentions(_person(ME))},
            **{"from": "8:orgid:bob"},
        )
    )
    await mboard.on_event(_msg(t, "2026-09-15T12:30:00Z", "later", msg_id="1789475400000"))
    assert mboard.mark_seen(t, at="2026-09-15T12:00:00Z")  # the page had rendered before "later"
    row = mboard.rows[t]
    assert row["mention"] is None and row["seen_at"] == "2026-09-15T12:00:00Z"
    assert W._ms(row["seen_at"]) < W._ms(row["last_activity"])  # "later" stays visible


async def test_teams_read_marker_clears_only_after_the_mention(mboard: W.Board) -> None:
    t = "19:a@thread.v2"
    await mboard.on_event(
        _msg(
            t,
            "2026-09-15T12:00:00Z",
            "you",
            msg_id="1789473600000",
            properties={"mentions": _mentions(_person(ME))},
            **{"from": "8:orgid:bob"},
        )
    )
    before = W._ms("2026-09-15T11:59:00Z")
    await mboard.on_event(
        _event("ConversationUpdate", t, id=t, properties={"consumptionhorizon": f"1789473600000;{before};1"})
    )
    assert mboard.rows[t]["mention"]["kind"] == "me" and mboard.rows[t]["unread"] is False
    after = W._ms("2026-09-15T12:00:30Z")
    await mboard.on_event(
        _event("ConversationUpdate", t, id=t, properties={"consumptionhorizon": f"1789473600000;{after};1"})
    )
    assert mboard.rows[t]["mention"] is None
    # A mention that arrives after the read marker is not pre-cleared by it.
    await mboard.on_event(
        _msg(
            t,
            "2026-09-15T12:05:00Z",
            "again",
            msg_id="1789473900000",
            properties={"mentions": _mentions(_person(ME))},
            **{"from": "8:orgid:bob"},
        )
    )
    assert mboard.rows[t]["mention"]["text"] == "again"


async def test_bootstrap_scans_history_for_visible_mentions(quiet_directory: Directory, monkeypatch) -> None:
    read_at = W._ms("2026-09-15T09:00:00Z")
    t_new, t_read, t_seen, t_all = (
        "19:new@thread.v2",
        "19:read@thread.v2",
        "19:seen@thread.v2",
        "19:all@thread.v2",
    )
    history = {
        # newest mention after the read marker → visible, direct wins over a later @everyone
        t_new: [
            {
                "id": "1",
                "messagetype": "Text",
                "content": "you",
                "composetime": "2026-09-15T10:00:00Z",
                "properties": {"mentions": _mentions(_person(ME))},
                "from": "8:orgid:bob",
                "imdisplayname": "Bob",
            },
            {
                "id": "2",
                "messagetype": "Text",
                "content": "all",
                "composetime": "2026-09-15T10:30:00Z",
                "properties": {"mentions": _mentions(_everyone(t_new))},
                "from": "8:orgid:bob",
            },
        ],
        # mentioned before the read marker → already read in Teams
        t_read: [
            {
                "id": "3",
                "messagetype": "Text",
                "content": "you",
                "composetime": "2026-09-15T08:00:00Z",
                "properties": {"mentions": _mentions(_person(ME))},
                "from": "8:orgid:bob",
            }
        ],
        # mentioned after the read marker but seen in the widget after it
        t_seen: [
            {
                "id": "4",
                "messagetype": "Text",
                "content": "you",
                "composetime": "2026-09-15T10:00:00Z",
                "properties": {"mentions": _mentions(_person(ME))},
                "from": "8:orgid:bob",
            }
        ],
        t_all: [
            {
                "id": "5",
                "messagetype": "Text",
                "content": "all",
                "composetime": "2026-09-15T10:00:00Z",
                "properties": {"mentions": _mentions(_everyone(t_all))},
                "from": "8:orgid:bob",
            }
        ],
    }
    fetched: list[str] = []

    def fake_history(settings: Any, token: str, thread_id: str, page_size: int, max_pages: int) -> list[Any]:
        fetched.append(thread_id)
        assert page_size == W._HISTORY_PAGE
        return history[thread_id]

    monkeypatch.setattr(W, "fetch_history", fake_history)
    horizon = {"consumptionhorizon": f"1;{read_at};1"}
    pages = [
        [
            _conv(t_new, "2026-09-15T10:30:00Z", id="2", messagetype="Text", content="all")
            | {"properties": horizon},
            _conv(t_read, "2026-09-15T08:00:00Z", id="3", messagetype="Text", content="you")
            | {"properties": horizon},
            _conv(t_seen, "2026-09-15T10:00:00Z", id="4", messagetype="Text", content="you")
            | {"properties": horizon},
            _conv(t_all, "2026-09-15T10:00:00Z", id="5", messagetype="Text", content="all")
            | {"properties": horizon},
        ]
    ]
    rows = await W.bootstrap(pages, quiet_directory, limit=0, me=ME, seen={t_seen: "2026-09-15T10:00:00Z"})
    assert rows[t_new]["mention"]["kind"] == "me" and rows[t_new]["mention"]["by"] == "Bob"
    assert rows[t_read]["mention"] is None
    assert rows[t_seen]["mention"] is None and rows[t_seen]["seen_at"] == "2026-09-15T10:00:00Z"
    assert rows[t_all]["mention"]["kind"] == "all"
    assert sorted(fetched) == sorted(history)  # one history page per row, none twice
    # Without `me` (token claims unreadable) nothing is fetched for plain rows.
    fetched.clear()
    rows = await W.bootstrap(pages, quiet_directory, limit=0)
    assert fetched == [] and all(r["mention"] is None for r in rows.values())


async def test_label_omits_self_from_roster(directory: Directory, monkeypatch) -> None:
    directory.me = ME

    async def fake_thread(self: Directory, thread_id: str) -> dict[str, Any]:
        return {
            "topic": None,
            "members": [{"mri": "8:orgid:eric", "name": "Eric"}, {"mri": ME, "name": "Me"}],
            "picture": None,
        }

    monkeypatch.setattr(Directory, "thread", fake_thread)
    assert await directory.label("19:x@unq.gbl.spaces") == "Eric"

    async def only_me(self: Directory, thread_id: str) -> dict[str, Any]:
        return {"topic": None, "members": [{"mri": ME, "name": "Me"}], "picture": None}

    monkeypatch.setattr(Directory, "thread", only_me)
    assert await directory.label("19:solo@thread.v2") == "Me"  # never an empty label


def test_seen_ignores_an_unparsable_at(mboard: W.Board) -> None:
    t = "19:a@thread.v2"
    assert mboard.mark_seen(t, at="garbage")
    assert mboard.rows[t]["seen_at"] == "2026-09-15T10:00:00Z"


async def test_bootstrap_scan_picks_the_newest_by_mention_time(quiet_directory: Directory) -> None:
    """An older message edited to mention me outranks a later-composed mention: compare by `at`."""
    edit_ms = W._ms("2026-09-15T13:00:00Z")
    history = [
        {
            "id": "1",
            "messagetype": "Text",
            "content": "edited in",
            "composetime": "2026-09-15T08:00:00Z",
            "properties": {"edittime": str(edit_ms), "mentions": _mentions(_person(ME))},
            "from": "8:orgid:bob",
        },
        {
            "id": "2",
            "messagetype": "Text",
            "content": "composed later",
            "composetime": "2026-09-15T10:00:00Z",
            "properties": {"mentions": _mentions(_person(ME))},
            "from": "8:orgid:bob",
        },
    ]
    row: dict[str, Any] = {"id": "19:x@thread.v2", "seen_at": None, "read_at": 0}
    await W._scan_mentions(row, history, quiet_directory, ME)
    assert (row["mention"]["msg_id"], row["mention"]["edited"]) == ("1", True)
    # Same date order without the edit: the later-composed one wins.
    del history[0]["properties"]["edittime"]
    await W._scan_mentions(row, history, quiet_directory, ME)
    assert row["mention"]["msg_id"] == "2"


def test_payload_carries_my_display_name(quiet_directory: Directory) -> None:
    quiet_directory.note_name(ME, "Damien DEGOIS")
    board = W.Board({}, quiet_directory, me=ME)
    assert json.loads(board.payload())["me"] == "Damien DEGOIS"
    assert json.loads(W.Board({}, quiet_directory).payload())["me"] == ""  # no me: the page shows "@you"


async def test_read_verb_moves_teams_marker_and_survives_failure(board: W.Board, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_mark_read(settings: Any, token: str, thread_id: str, message_id: str) -> dict[str, Any]:
        calls.append((thread_id, message_id))
        if thread_id == "19:b@thread.v2":
            raise RuntimeError("503")
        return {"status": 200}

    monkeypatch.setattr(W, "mark_read", fake_mark_read)
    assert await board.mark_read("19:a@thread.v2") is True
    assert await board.mark_read("19:b@thread.v2") is False  # logged, row untouched
    assert await board.mark_read("19:nope@thread.v2") is False
    board.rows["19:a@thread.v2"]["last_id"] = ""
    assert await board.mark_read("19:a@thread.v2") is False  # nothing to point the marker at
    assert calls == [("19:a@thread.v2", "10"), ("19:b@thread.v2", "20")]
    assert board.rows["19:b@thread.v2"].get("unread") is not True  # local state never guessed


def test_mute_persists_and_unmute_undoes(settings: Settings) -> None:
    muted_path = settings.config_dir / "muted.json"
    rows = {"t": {"id": "t", "last_activity": "2026-09-15T10:00:00Z", "last_id": "1", "seen_at": None}}
    board = W.Board(rows, muted_path=muted_path)
    assert rows["t"]["muted"] is False
    assert board.set_muted("t", False) is False  # nothing to undo
    assert board.set_muted("t", True) is True and rows["t"]["muted"] is True
    assert board.set_muted("t", True) is False  # already muted
    assert list(json.loads(muted_path.read_text())) == ["t"]
    again = W.Board({"t": dict(rows["t"], muted=False)}, muted_path=muted_path)  # restart
    assert again.rows["t"]["muted"] is True
    assert again.set_muted("t", False) is True and json.loads(muted_path.read_text()) == {}
    assert board.set_muted("nope", True) is False


async def test_live_row_on_a_muted_thread_stays_muted(board: W.Board, settings: Settings) -> None:
    board.muted_path = settings.config_dir / "muted.json"
    board._muted["19:new@thread.v2"] = "2026-09-15T00:00:00Z"
    await board.on_event(_msg("19:new@thread.v2", "2026-09-15T13:00:00Z", "yo"))
    assert board.rows["19:new@thread.v2"]["muted"] is True
    await board.on_event(_msg("19:a@thread.v2", "2026-09-15T13:00:00Z", "yo"))
    assert board.rows["19:a@thread.v2"]["muted"] is False


async def test_bootstrap_gives_a_message_id_to_rows_listed_without_one(
    quiet_directory: Directory, monkeypatch
) -> None:
    """The chat-only deep link form reloads the desktop client (and drops a call): land on a message."""
    fetched: list[str] = []

    def fake_history(settings: Any, token: str, thread_id: str, page_size: int, max_pages: int) -> list[Any]:
        fetched.append(thread_id)
        return [
            {"id": "7", "messagetype": "Text", "content": "old"},
            {"id": "8", "messagetype": "Text", "content": "new"},
        ]

    monkeypatch.setattr(W, "fetch_history", fake_history)
    bare = {"id": "19:bare@thread.v2", "lastMessage": {}, "version": 1789466400000}
    listed = _conv("19:ok@thread.v2", "2026-09-15T10:00:00Z", id="5", messagetype="Text", content="hi")
    rows = await W.bootstrap([[bare, listed]], quiet_directory, limit=0)
    assert rows["19:bare@thread.v2"]["last_id"] == "8" and "/l/message/" in W.deep_link(
        "19:bare@thread.v2", "8"
    )
    assert rows["19:ok@thread.v2"]["last_id"] == "5"
    assert fetched == ["19:bare@thread.v2"]  # only the bare row costs a history call


async def test_label_omits_apps_and_bots_from_roster(directory: Directory, monkeypatch) -> None:
    directory.me = ME
    rosters = {
        "19:dm@unq.gbl.spaces": [
            {"mri": "8:orgid:eric", "name": "Eric"},
            {"mri": ME, "name": "Me"},
            {"mri": "28:4aa38041-66a2", "name": "Confluence Cloud"},
        ],
        "19:bot@unq.gbl.spaces": [{"mri": ME, "name": "Me"}, {"mri": "28:polly", "name": "Polly"}],
        "19:grp@thread.v2": [
            {"mri": "8:orgid:a", "name": "Ann"},
            {"mri": "28:jira", "name": "Jira Cloud"},
            {"mri": "8:orgid:b", "name": "Ben"},
        ],
    }

    async def fake_thread(self: Directory, thread_id: str) -> dict[str, Any]:
        return {"topic": None, "members": rosters[thread_id], "picture": None}

    monkeypatch.setattr(Directory, "thread", fake_thread)
    assert await directory.label("19:dm@unq.gbl.spaces") == "Eric"
    assert await directory.label("19:bot@unq.gbl.spaces") == "Polly"  # nobody else: the bot names it
    assert await directory.label("19:grp@thread.v2") == "Ann, Ben"


async def test_call_events_are_not_credited_to_the_organizer(board: W.Board) -> None:
    t = "19:a@thread.v2"
    organizer = {"from": "https://h/v1/users/ME/contacts/8:orgid:me", "imdisplayname": ""}
    scheduled = (
        '<partlist alt =""></partlist><meetingDetails><organizerUpn>me@x</organizerUpn></meetingDetails>'
    )
    await board.on_event(
        _event(
            "NewMessage",
            t,
            id="30",
            composetime="2026-09-15T12:00:00Z",
            messagetype="Event/Call",
            content=scheduled,
            **organizer,
        )
    )
    row = board.rows[t]
    assert (row["sender"], row["text"]) == ("", "📞 call started")
    ended = (
        '<ended/><partlist alt="" count="2">'
        '<part identity="8:orgid:a"><displayName>Ann</displayName></part></partlist>'
    )
    await board.on_event(
        _event(
            "NewMessage",
            t,
            id="31",
            composetime="2026-09-15T12:30:00Z",
            messagetype="Event/Call",
            content=ended,
            **organizer,
        )
    )
    assert (row["sender"], row["text"]) == ("", "📞 call ended")
    adhoc = (
        '<partlist type="started" alt="">'
        '<part identity="8:orgid:juan"><name>8:orgid:juan</name></part></partlist>'
    )
    await board.on_event(
        _event(
            "NewMessage",
            t,
            id="32",
            composetime="2026-09-15T13:00:00Z",
            messagetype="Event/Call",
            content=adhoc,
            **organizer,
        )
    )
    assert (row["sender"], row["text"]) == ("name:8:orgid:juan", "📞 call started")
