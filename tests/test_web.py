"""Web widget (spec 002): row mapping, scope filter, bind persistence, page + socket serving."""

import asyncio
import contextlib
import functools
import json
import os
import re
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog.testing
import websockets
import websockets.sync.client
from conftest import board_socket, cookie_header, wait
from test_web_chat import _relayed, _send

from miniteams import chat_verbs as CV
from miniteams import web as W
from miniteams.config import Settings
from miniteams.directory import Directory
from miniteams.messages import SNIPPET_LEN


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
    assert W.snippet("Text", "y" * 900) == "y" * 900  # a long message reaches the tooltip whole
    out = W.snippet("Text", "x" * (SNIPPET_LEN * 2))
    assert len(out) == SNIPPET_LEN
    assert out.endswith("…")
    assert W.snippet("Text", "x" * SNIPPET_LEN) == "x" * SNIPPET_LEN  # boundary: untouched


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


def test_snippet_names_files_like_the_cards() -> None:
    """027-AC1: the list snippet names a file as its card does: fileName, else title, else "file"."""
    files = [
        {"title": "deck.pptx"},
        {"fileName": "", "title": "t.txt"},
        {"fileName": 5},
        {"fileName": "a.pdf"},
    ]
    assert W.snippet("RichText/Html", "", {"files": files}) == "📎 deck.pptx, t.txt, file, a.pdf"


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
        "muted": False,
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


async def test_bootstrap_filters_scope_and_caps(quiet_directory: Directory, monkeypatch) -> None:
    monkeypatch.setattr(W, "fetch_history", lambda *a: [])  # no recent page: rows stay as listed
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
        stderr = None

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
    assert await board.open_row("t", "web") is True
    assert launched[-1] == ("firefox", W.deep_link("t", "42", "https"))
    assert await board.open_row("nope") is False and len(launched) == 2
    assert json.loads(board.payload())["opener"] is True and json.loads(board.payload())["browser"] is True
    assert json.loads(board.payload())["rows"][0]["link"] == W.deep_link("t", "42")
    # No opener configured: the verb is a no-op and the page is told to follow its own link.
    plain = W.Board(dict(rows), opener=None)
    assert await plain.open_row("t") is False and json.loads(plain.payload())["opener"] is False
    assert await plain.open_row("t", "web") is False and json.loads(plain.payload())["browser"] is False


async def test_a_failing_opener_is_logged_with_its_stderr() -> None:
    rows = {"t": {"id": "t", "last_activity": "2026-09-15T10:00:00Z", "last_id": "42", "seen_at": None}}
    fail = ["sh", "-c", 'echo "Error: no DISPLAY environment variable specified" >&2; exit 3', "sh"]
    board = W.Board(rows, opener=fail, browser=["sh", "-c", "echo chatter >&2", "sh"])
    with structlog.testing.capture_logs() as logs:
        assert await board.open_row("t") is True
        assert await board.open_row("t", "web") is True
        for _ in range(300):
            if any(e["event"] == "opener_exit" for e in logs):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)  # the browser's own exit would have logged by now
    exits = [e for e in logs if e["event"] == "opener_exit"]
    assert len(exits) == 1, exits  # rc 0 stays quiet, whatever it printed
    assert exits[0]["rc"] == 3 and exits[0]["opener"] == "sh"
    assert exits[0]["stderr"] == "Error: no DISPLAY environment variable specified"


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


def test_draw_bind_is_a_127_sibling_and_an_ephemeral_port() -> None:
    host, port = W._draw_bind()
    assert host.startswith("127.0.0.") and 2 <= int(host.rsplit(".", 1)[1]) <= 254
    assert 32768 <= port <= 60999


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
        async with httpx.AsyncClient(headers=cookie_header(W.load_secret(settings))) as client:
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
        async with board_socket(f"127.0.0.1:{port}", board.secret) as ws:
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
        cookie = cookie_header(board.secret)  # the cookie right: only the Origin is wrong
        for origin in ("http://evil.example", None):
            with pytest.raises(websockets.InvalidStatus) as exc:
                await websockets.connect(
                    f"ws://127.0.0.1:{port}/ws", origin=origin, additional_headers=cookie
                )
            assert exc.value.response.status_code == 403
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def _taken(host: str) -> tuple[socket.socket, int, int]:
    """A listening socket on `host`, its port, and a port free on `host`."""
    taken = socket.socket()
    taken.bind((host, 0))
    taken.listen()
    with socket.socket() as probe:
        probe.bind((host, 0))
        free = int(probe.getsockname()[1])
    return taken, int(taken.getsockname()[1]), free


async def _serves(board: W.Board, settings: Settings, bind: str | None) -> None:
    """serve_board until bound and answering, then cancelled."""
    task = asyncio.create_task(W.serve_board(board, settings, bind))
    try:
        await wait(lambda: board.publish is not None, "bound")
        async with board_socket(board.base_url.removeprefix("http://"), board.secret) as ws:
            assert json.loads(await ws.recv())["rows"] == []
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_a_first_run_draws_a_bind_and_keeps_it(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """024-AC9: without web.json the bind is drawn, a drawn port found taken is redrawn, and the bound
    address is persisted for the next run."""
    host = "127.0.0.33"
    taken, busy, free = _taken(host)
    monkeypatch.setattr(W, "_draw_bind", lambda: (host, busy))
    monkeypatch.setattr(W, "_draw_port", lambda: free)
    board = W.Board({})
    try:
        await _serves(board, settings, None)
    finally:
        taken.close()
    assert board.base_url == f"http://{host}:{free}"
    saved = {"host": host, "port": free, "token": board.token}
    assert json.loads((settings.config_dir / "web.json").read_text()) == saved


async def test_a_taken_persisted_bind_fails_and_keeps_web_json(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """024-AC9: web.json's address taken: the start fails naming it and web.json, nothing redrawn; the
    command exits non-zero (`command_failed`, 1) on that error."""
    taken, busy, _ = _taken("127.0.0.33")
    W.save_bind(settings, "127.0.0.33", busy, "old")
    before = (settings.config_dir / "web.json").read_bytes()
    monkeypatch.setattr(W, "_draw_port", lambda: pytest.fail("the port was redrawn"))
    monkeypatch.setattr(W, "_draw_bind", lambda: pytest.fail("the bind was redrawn"))
    board = W.Board({})
    try:
        with pytest.raises(RuntimeError) as exc:
            await asyncio.wait_for(W.serve_board(board, settings, None, W._persisted_bind(settings)), 5)
    finally:
        taken.close()
    message = str(exc.value)
    assert f"127.0.0.33:{busy}" in message and str(settings.config_dir / "web.json") in message
    assert "in use: is another miniteams web running? stop it, or delete web.json" in message
    assert (settings.config_dir / "web.json").read_bytes() == before and board.publish is None


async def test_web_exits_on_a_taken_persisted_bind(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """024-AC9 end to end: run() hands web.json's bind to the real serve_board, which fails on it."""
    taken, busy, _ = _taken("127.0.0.33")
    W.save_bind(settings, "127.0.0.33", busy, "old")
    path = settings.config_dir / "web.json"
    before = path.read_bytes()

    async def idle(*_: Any, **__: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(W, "fill_from_listing", idle)
    monkeypatch.setattr(W, "run_forever", idle)
    monkeypatch.setattr(W.EmojiCache, "refresh", idle)  # no token fetch
    monkeypatch.setattr(W, "_draw_bind", lambda: pytest.fail("the bind was redrawn"))
    try:
        with pytest.raises(RuntimeError, match=f"cannot listen on 127.0.0.33:{busy} .*another miniteams web"):
            await asyncio.wait_for(W.run(settings, "sk", "", None, None, data_dir=tmp_path), 5)
    finally:
        taken.close()
    assert path.read_bytes() == before


def _hung_peer(host: str, secret: str) -> socket.socket:
    """A page socket that opens, then never writes again: the server's close frame is never answered."""
    sock = socket.create_connection(W.parse_bind(host))
    sock.sendall(
        f"GET /ws HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
        f"Origin: http://{host}\r\nCookie: mt={secret}\r\n\r\n".encode()
    )
    head = b""
    while not head.endswith(b"\r\n\r\n"):
        head += sock.recv(1)  # byte by byte: the frames after the headers stay for `_close_frame`
    assert head.startswith(b"HTTP/1.1 101")
    return sock


def _close_frame(sock: socket.socket) -> bytes:
    """The payload of the first close frame the server sends (unmasked frames), within 2 s."""
    sock.settimeout(2)
    stream = sock.makefile("rb")
    while True:
        first, size = stream.read(2)
        size &= 0x7F
        if size >= 126:
            size = int.from_bytes(stream.read(2 if size == 126 else 8))
        payload = stream.read(size)
        if first == 0x88:
            return payload


@pytest.mark.parametrize("peer", ["board", "chat", "hung", "stuck"])
def test_a_first_ctrl_c_exits_with_a_page_connected(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: str
) -> None:
    """Ctrl-C (the main task cancelled, then asyncio.run's sweep of the rest) with a page connected: the
    page is closed with 1001 at once and run() returns; a peer that never answers the close holds it
    the close timeout at most, a verb that never returns the close grace at most."""

    async def idle(*_: Any, **__: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(W, "fill_from_listing", idle)
    monkeypatch.setattr(W, "run_forever", idle)
    monkeypatch.setattr(W.EmojiCache, "refresh", idle)  # no token fetch
    monkeypatch.setattr(W.Board, "_on_seen", idle)  # `stuck`: its handler never returns
    monkeypatch.setattr(W, "serve", functools.partial(W.serve, close_timeout=0.5))
    monkeypatch.setattr(W, "_CLOSE_GRACE", 1.0)
    host = f"127.0.0.1:{_free_port()}"
    secret = W.load_secret(settings)
    main: list[asyncio.Task[None]] = []

    async def web() -> None:
        main.append(asyncio.current_task())  # type: ignore[arg-type]
        await W.run(settings, "sk", "", None, host, data_dir=tmp_path)

    def serve() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            asyncio.run(web())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client: Any = None
    try:
        for _ in range(250):
            try:
                if peer == "hung":
                    client = _hung_peer(host, secret)
                else:
                    client = websockets.sync.client.connect(
                        f"ws://{host}/ws{'?chat' if peer == 'chat' else ''}",
                        origin=websockets.Origin(f"http://{host}"),
                        additional_headers=cookie_header(secret),
                    )
                break
            except ConnectionRefusedError:
                time.sleep(0.02)
        assert client is not None, "server never listened"
        if peer == "stuck":
            client.send(json.dumps({"seen": "t"}))
            time.sleep(0.1)  # the verb is in its handler before the Ctrl-C
        main[0].get_loop().call_soon_threadsafe(main[0].cancel)
        if peer == "hung":
            assert _close_frame(client)[:2] == (1001).to_bytes(2)
        else:
            with pytest.raises(websockets.ConnectionClosedOK) as closed:
                while True:
                    client.recv(timeout=2)  # the board's frames, then the close
            assert closed.value.rcvd is not None and closed.value.rcvd.code == 1001
        thread.join(5)
        assert not thread.is_alive(), "web held the exit with a page connected"
    finally:
        if client is not None:
            client.close()  # releases a hung server, so a failure does not leak its thread
        thread.join(5)


def _unreadable(path: Path) -> None:
    path.write_text('{"host": "127.0.0.9", "port": 40000, "token": "t"}')
    path.chmod(0)
    if os.access(path, os.R_OK):
        pytest.skip("running as root")


@pytest.mark.parametrize(
    ("write", "reason"),
    [
        (lambda p: p.write_text("{not json"), "Expecting property name"),
        (_unreadable, "Permission denied"),
        (lambda p: p.write_text('{"host": "192.168.1.10", "port": 40000}'), "non-loopback"),
        (lambda p: p.write_text('{"host": "127.0.0.9", "port": 0}'), "expected ADDR:PORT"),
        (lambda p: p.write_text('{"host": "127.0.0.9", "port": 70000}'), "expected ADDR:PORT"),
        (lambda p: p.write_text('{"port": 40000}'), 'missing "host"'),
        (lambda p: p.write_text('{"host": "127.0.0.9"}'), 'missing "port"'),
        (lambda p: p.write_text("[]"), "list indices"),
    ],
    ids=[
        "not-json",
        "unreadable",
        "non-loopback",
        "port-0",
        "port-70000",
        "no-host",
        "no-port",
        "not-an-object",
    ],
)
async def test_an_unusable_web_json_fails_and_is_kept(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, write: Callable[[Path], object], reason: str
) -> None:
    """024-AC9: an unusable web.json is an error naming it, never a silent redraw over it; `web` fails on
    it first thing, before any seeding, token or presence work."""
    path = settings.config_dir / "web.json"
    settings.config_dir.mkdir(parents=True)
    write(path)
    before = (path.stat().st_mtime_ns, path.stat().st_size)
    for name in ("_draw_bind", "Directory", "_load_seen", "token_source", "open_cache", "serve_board"):
        monkeypatch.setattr(W, name, lambda *_, name=name, **__: pytest.fail(f"{name} reached"))
    with pytest.raises(RuntimeError, match=f"cannot use {re.escape(str(path))}: .*{re.escape(reason)}"):
        await asyncio.wait_for(W.run(settings, "skype", "bearer", None, None, data_dir=path.parent), 5)
    assert (path.stat().st_mtime_ns, path.stat().st_size) == before
    # A --bind run leaves it alone too: its token goes to stderr, not over the unusable file.
    assert W.publish(settings, "127.0.0.33", 4000, True, "tok") is False
    assert (path.stat().st_mtime_ns, path.stat().st_size) == before


def test_parse_bind_refuses_port_0_and_past_65535() -> None:
    for bad in ("127.0.0.1:0", "127.0.0.1:65536", "[::1]:0"):
        with pytest.raises(ValueError, match="expected ADDR:PORT"):
            W.parse_bind(bad)
    assert W.parse_bind("127.0.0.1:65535") == ("127.0.0.1", 65535)


async def test_a_taken_explicit_bind_fails_as_before(settings: Settings) -> None:
    """--bind keeps its semantics: a taken address raises the bind error, nothing redrawn nor written."""
    taken, busy, _ = _taken("127.0.0.33")
    try:
        with pytest.raises(OSError, match="in use"):
            await asyncio.wait_for(W.serve_board(W.Board({}), settings, f"127.0.0.33:{busy}"), 5)
    finally:
        taken.close()
    assert not (settings.config_dir / "web.json").exists()


async def test_an_explicit_bind_is_used_over_web_json(settings: Settings) -> None:
    W.save_bind(settings, "127.0.0.9", 40000, "live")
    board = W.Board({})
    taken, _, free = _taken("127.0.0.33")
    taken.close()
    await _serves(board, settings, f"127.0.0.33:{free}")
    assert board.base_url == f"http://127.0.0.33:{free}"


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


async def test_typing_clears_queue_behind_pending_relays(
    board: W.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4: an expiry and a board message clear typing through the relay queue, never ahead of it."""
    frames = _relayed(monkeypatch)
    thread = "19:a@thread.v2"
    board._follows[object()] = (thread, None)  # type: ignore[index]
    gate = asyncio.Event()

    async def parked() -> None:
        await gate.wait()
        board._relay_now(thread, {"msg": "queued"})

    board._enqueue_relay(thread, parked)
    await asyncio.sleep(0)  # the relay task is now parked on it
    board._typing_arm(thread, "Bob")
    board._typing_expired(thread, "Bob")
    board._typing_arm(thread, "Bob")
    await board.on_event(_msg(thread, "2026-09-15T12:00:00Z", "sent", msg_id="11"))
    assert frames == []
    gate.set()
    await wait(lambda: len(frames) >= 4, "4 frames")
    assert [next(iter(f)) for f in frames] == ["msg", "typing", "typing", "msg"]
    assert frames[0]["msg"] == "queued"
    board.close()


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
        async with board_socket(f"127.0.0.1:{port}", board.secret) as ws:
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


@pytest.mark.parametrize("deleted", [{"deletetime": "1"}, {"hardDeleteTime": "1"}])
async def test_deleting_the_mentioning_message_clears_it(mboard: W.Board, deleted: dict[str, str]) -> None:
    """M9: a soft or a hard delete, as messages.is_deleted reads them."""
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
            properties=deleted,
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
    assert again.rows["t"]["muted"] is False  # 030: the row's mute is Teams', not the file's
    assert again.set_muted("t", True) is True and again.set_muted("t", False) is True
    assert json.loads(muted_path.read_text()) == {}
    teams_muted = W.Board({"u": dict(rows["t"], id="u", muted=True)}, muted_path=muted_path)
    assert teams_muted.set_muted("u", False) is True  # muted in Teams only: nothing in the file to drop
    assert board.set_muted("nope", True) is False


async def test_live_row_on_a_muted_thread_stays_muted(board: W.Board) -> None:
    """030: a chat the listing has not shown takes its mute from the archive seed."""
    board.seed["19:new@thread.v2"] = {"label": "New", "muted": True}
    await board.on_event(_msg("19:new@thread.v2", "2026-09-15T13:00:00Z", "yo"))
    assert board.rows["19:new@thread.v2"]["muted"] is True
    await board.on_event(_msg("19:a@thread.v2", "2026-09-15T13:00:00Z", "yo"))
    assert board.rows["19:a@thread.v2"]["muted"] is False


@pytest.mark.parametrize("alerts", ["false", "False", False])
def test_teams_alerts_false_is_muted(alerts: Any) -> None:
    """030-AC1: Teams sends the string "false"; a boolean or capitalised one means the same."""
    assert W.is_muted({"alerts": alerts}) is True


@pytest.mark.parametrize("props", [None, {}, {"alerts": "true"}, {"alerts": True}, {"alerts": ""}])
def test_teams_alerts_absent_or_true_is_not_muted(props: dict[str, Any] | None) -> None:
    """030-AC2"""
    assert W.is_muted(props) is False


async def test_listing_row_carries_teams_mute_through_replace(quiet_directory: Directory) -> None:
    """030-AC1 / 030-AC2: the listing's `alerts` sets the row, and replace() keeps it."""
    muted = _conv("19:m@thread.v2", "2026-09-15T10:00:00Z", id="1") | {"properties": {"alerts": "false"}}
    loud = _conv("19:l@thread.v2", "2026-09-15T10:00:00Z", id="1") | {"properties": {"alerts": "true"}}
    bare = _conv("19:b@thread.v2", "2026-09-15T10:00:00Z", id="1")
    rows = {c["id"]: await W.row_from_conversation(c, quiet_directory) for c in (muted, loud, bare)}
    assert [r["muted"] for r in rows.values()] == [True, False, False]
    board = W.Board({"19:m@thread.v2": dict(rows["19:m@thread.v2"], muted=False)})
    board.replace(rows)
    assert {t: r["muted"] for t, r in board.rows.items()} == {
        "19:m@thread.v2": True,
        "19:l@thread.v2": False,
        "19:b@thread.v2": False,
    }


async def test_conversation_update_alerts_moves_the_mute(board: W.Board, monkeypatch) -> None:
    """030-AC7: Teams' mute event carries `alerts` and no `consumptionhorizon`."""
    sent: list[str] = []
    monkeypatch.setattr(board, "broadcast", lambda: sent.append("b"))

    async def update(props: dict[str, Any], thread_id: str = "19:a@thread.v2") -> None:
        await board.on_event(_event("ConversationUpdate", thread_id, id=thread_id, properties=props))

    row = board.rows["19:a@thread.v2"]
    read = (row.get("read_id"), row.get("read_at"), row.get("unread"))
    await update({"alerts": "false"})
    assert row["muted"] is True and sent == ["b"]
    assert (row.get("read_id"), row.get("read_at"), row.get("unread")) == read  # read state untouched
    await update({"alerts": "true"})
    assert row["muted"] is False and sent == ["b", "b"]
    row["muted"] = True
    horizon = {"consumptionhorizon": "1;1789473600000;1"}
    await update(horizon)
    assert row["muted"] is True and row["read_id"] == "1" and sent == ["b", "b", "b"]
    await update({"topic": "x"})
    await update({"alerts": "false"}, "19:ghost@thread.v2")
    assert sent == ["b", "b", "b"] and "19:ghost@thread.v2" not in board.rows


async def test_mute_of_an_off_board_chat_survives_a_reseed(board: W.Board) -> None:
    """030-AC7: a mute for a chat with no row reaches the row a later message builds."""
    board.seed["19:off@thread.v2"] = {"label": "Off", "muted": False}
    await board.on_event(
        _event(
            "ConversationUpdate", "19:off@thread.v2", id="19:off@thread.v2", properties={"alerts": "false"}
        )
    )
    assert "19:off@thread.v2" not in board.rows
    board.seed = {"19:off@thread.v2": {"label": "Off", "muted": False}}  # archived_label's index re-read
    await board.on_event(_msg("19:off@thread.v2", "2026-09-15T13:00:00Z", "yo"))
    assert board.rows["19:off@thread.v2"]["muted"] is True


async def test_mute_of_a_pruned_row_reaches_its_next_row(board: W.Board) -> None:
    """030-AC7: prune drops a row muted live; the chat's next row is still muted."""
    board.seed["19:a@thread.v2"] = {"label": "A", "muted": False}
    await board.on_event(
        _event("ConversationUpdate", "19:a@thread.v2", id="19:a@thread.v2", properties={"alerts": "false"})
    )
    assert board.rows["19:a@thread.v2"]["muted"] is True
    assert board.prune({"19:b@thread.v2"}, "2026-09-15T10:00:00Z") == 1
    await board.on_event(_msg("19:a@thread.v2", "2026-09-15T13:00:00Z", "yo"))
    assert board.rows["19:a@thread.v2"]["muted"] is True


async def test_replace_keeps_the_listing_mute_on_a_row_the_stream_moved(board: W.Board) -> None:
    """030-AC1: the stream's newer row wins the content, the listing still owns the mute."""
    await board.on_event(_msg("19:new@thread.v2", "2026-09-15T13:00:00Z", "yo"))
    assert board.rows["19:new@thread.v2"]["muted"] is False
    older = {"id": "19:new@thread.v2", "last_activity": "2026-09-15T12:00:00Z", "muted": True}
    board.replace({"19:new@thread.v2": older})
    row = board.rows["19:new@thread.v2"]
    assert row["muted"] is True and row["text"] == "yo"


@pytest.mark.parametrize("listed_at", ["2026-09-15T12:00:00Z", "2026-09-15T09:00:00Z"])
async def test_replace_keeps_a_mute_that_landed_during_the_fill(board: W.Board, listed_at: str) -> None:
    """030-AC7: the listing snapshot predates the mute event; newer listing row, then newer live row."""
    snapshot = {"id": "19:a@thread.v2", "last_activity": listed_at, "muted": False}
    await board.on_event(
        _event("ConversationUpdate", "19:a@thread.v2", id="19:a@thread.v2", properties={"alerts": "false"})
    )
    board.replace({"19:a@thread.v2": snapshot})
    assert board.rows["19:a@thread.v2"]["muted"] is True
    assert board.rows["19:a@thread.v2"]["muted"] == board._live_mute["19:a@thread.v2"]


async def test_unmute_as_a_removed_alerts_property(board: W.Board, monkeypatch) -> None:
    """030-AC7: `propertiesUpdated` names `alerts` while `properties` no longer holds it."""
    sent: list[str] = []
    monkeypatch.setattr(board, "broadcast", lambda: sent.append("b"))
    row = board.rows["19:a@thread.v2"]
    row["muted"] = True

    async def update(**resource: Any) -> None:
        await board.on_event(_event("ConversationUpdate", "19:a@thread.v2", id="19:a@thread.v2", **resource))

    await update(properties={}, propertiesUpdated=["consumptionhorizon"])
    await update(properties={}, propertiesUpdated="alerts")  # not a list: ignored
    assert row["muted"] is True and sent == []
    await update(properties={}, propertiesUpdated=["alerts"])
    assert row["muted"] is False and sent == ["b"]


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


# --- quick reply (spec 006) ---


async def test_send_verb_posts_as_me_and_reports_failures(board: W.Board, monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []
    failure: list[Exception] = []

    def fake_send(settings: Any, token: str, thread_id: str, text: str, name: str) -> dict[str, Any]:
        calls.append((thread_id, text, name))
        if failure:
            raise failure.pop()
        return {"status": 201}

    monkeypatch.setattr(CV, "send_message", fake_send)
    board.chat.me_name = "Me"
    before = json.dumps(board.rows, sort_keys=True)
    assert await _send(board, "19:a@thread.v2", "ok\nje regarde") is None
    assert calls == [("19:a@thread.v2", "ok\nje regarde", "Me")]
    assert json.dumps(board.rows, sort_keys=True) == before  # the Trouter echo updates the row

    request = httpx.Request("POST", "https://x")
    failure.append(httpx.HTTPStatusError("403", request=request, response=httpx.Response(403)))
    assert await _send(board, "19:a@thread.v2", "x") == "Teams answered 403"
    failure.append(httpx.ConnectError("boom", request=request))
    assert await _send(board, "19:a@thread.v2", "x") == "network error"
    failure.append(RuntimeError("send rejected: Forbidden: nope"))
    assert await _send(board, "19:a@thread.v2", "x") == "send rejected: Forbidden: nope"
    assert len(calls) == 4


@pytest.mark.parametrize(
    ("thread", "text", "reason"),
    [
        ("19:nope@thread.v2", "hi", "unknown chat"),
        ("19:a@thread.v2", "", "empty message"),
        ("19:a@thread.v2", " \n\t", "empty message"),
        ("19:a@thread.v2", 42, "empty message"),
        ("19:a@thread.v2", None, "empty message"),
        ("19:a@thread.v2", "x" * (28 * 1024 + 1), "message too long"),
        ("19:a@thread.v2", "<" * (28 * 1024 // 4 + 1), "message too long"),  # escaping counts
        ("19:a@thread.v2", "é" * (28 * 1024 // 2 + 1), "message too long"),  # bytes, not chars
    ],
)
async def test_send_verb_refuses_without_calling_teams(
    board: W.Board, monkeypatch, thread: str, text: Any, reason: str
) -> None:
    monkeypatch.setattr(CV, "send_message", lambda *a: pytest.fail("no API call expected"))
    assert await _send(board, thread, text) == reason


async def test_send_verb_at_the_size_limit_goes_through(board: W.Board, monkeypatch) -> None:
    monkeypatch.setattr(CV, "send_message", lambda *a: {"status": 201})
    assert await _send(board, "19:a@thread.v2", "x" * (28 * 1024)) is None


async def test_send_verb_without_directory_is_refused() -> None:
    board = W.Board({"t": {"id": "t", "last_activity": "2026-09-15T10:00:00Z", "seen_at": None}})
    assert await _send(board, "t", "hi") == "not connected"


async def test_send_ack_goes_to_the_sender_only(
    settings: Settings, quiet_directory: Directory, monkeypatch
) -> None:
    monkeypatch.setattr(CV, "send_message", lambda *a: {"status": 201})
    board = W.Board(
        {"t": {"id": "t", "last_activity": "2026-09-15T10:00:00Z", "label": "L", "seen_at": None}},
        quiet_directory,
    )
    port = _free_port()
    task = asyncio.create_task(W.serve_board(board, settings, f"127.0.0.1:{port}"))
    try:
        await asyncio.sleep(0.2)
        async with (
            board_socket(f"127.0.0.1:{port}", board.secret) as sender,
            board_socket(f"127.0.0.1:{port}", board.secret) as other,
        ):
            await sender.recv()
            await other.recv()
            await sender.send(json.dumps({"send": "t", "text": "hi"}))
            assert json.loads(await sender.recv()) == {"sent": "t", "ok": True}
            await sender.send(json.dumps({"send": "t", "text": ""}))
            assert json.loads(await sender.recv()) == {"sent": "t", "error": "empty message"}
            await sender.send(json.dumps({"send": 7, "text": "hi"}))  # wrong shape: ignored, no ack
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(other.recv(), 0.3)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(sender.recv(), 0.3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_slow_send_does_not_hold_the_page_other_verbs(
    settings: Settings, quiet_directory: Directory, monkeypatch
) -> None:
    def slow_send(*args: Any) -> dict[str, Any]:
        time.sleep(1.0)
        return {"status": 201}

    monkeypatch.setattr(CV, "send_message", slow_send)
    board = W.Board(
        {"t": {"id": "t", "last_activity": "2026-09-15T10:00:00Z", "label": "L", "seen_at": None}},
        quiet_directory,
    )
    port = _free_port()
    task = asyncio.create_task(W.serve_board(board, settings, f"127.0.0.1:{port}"))
    try:
        await asyncio.sleep(0.2)
        async with board_socket(f"127.0.0.1:{port}", board.secret) as ws:
            await ws.recv()
            await ws.send(json.dumps({"send": "t", "text": "hi"}))
            await ws.send(json.dumps({"seen": "t"}))
            frame = json.loads(await asyncio.wait_for(ws.recv(), 0.5))  # the seen broadcast, not the ack
            assert frame["rows"][0]["seen_at"] == "2026-09-15T10:00:00Z"
            assert json.loads(await asyncio.wait_for(ws.recv(), 2)) == {"sent": "t", "ok": True}
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_own_messages_read_you(quiet_directory: Directory) -> None:
    """Like Teams' chat list: our own last message is "You", whatever our display name."""
    me = "8:orgid:me-guid"
    mine = {"from": f"https://h/v1/users/ME/contacts/{me}", "imdisplayname": "Damien DEGOIS"}
    theirs = {"from": "https://h/v1/users/ME/contacts/8:orgid:ann", "imdisplayname": "Ann"}
    started = '<partlist type="started" alt=""><part identity="{}"><name>x</name></part></partlist>'
    call = {"messagetype": "Event/Call", "from": theirs["from"]}

    assert await W.sender_of(mine, quiet_directory) == "Damien DEGOIS"  # own MRI unknown: no guess
    quiet_directory.me = me
    assert await W.sender_of(mine, quiet_directory) == "You"
    assert await W.sender_of(theirs, quiet_directory) == "Ann"
    assert await W.sender_of({**call, "content": started.format(me)}, quiet_directory) == "You"
    ann_call = {**call, "content": started.format("8:orgid:ann")}
    assert await W.sender_of(ann_call, quiet_directory) == "name:8:orgid:ann"
    assert await W.sender_of({"from": "", "imdisplayname": ""}, quiet_directory) == ""
