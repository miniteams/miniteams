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
        "sender": "Alice",
        "text": "hi",
        "seen_at": None,
        "typing": [],
    }
    conv["lastMessage"].pop("imdisplayname")
    assert (await W.row_from_conversation(conv, quiet_directory))["sender"] == "name:8:orgid:alice"


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
            assert json.loads(await ws.recv()) == {"rows": []}
    finally:
        taken.close()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
