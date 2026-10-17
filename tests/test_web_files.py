"""Files from the composer on the page socket (spec 025 phase 2): the `send` frame's `files`, their
chunked binary frames spooled to unnamed temp files under the cache dir, the size cap while streaming,
one message with images and files, failures, and the checked retry. SharePoint and Teams are fakes."""

import asyncio
import io
import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
import test_web_images
from conftest import board_socket, wait
from test_files import SITE, FakeSharePoint
from test_images import png
from test_web_chat import _served
from test_web_images import B, Teams, _meta, _mine, _socket_answer

from miniteams import chat_verbs as CV
from miniteams import files as F
from miniteams import send as S
from miniteams import web as W
from miniteams.config import Settings

CHUNK = 1 << 20  # what the page sends per frame (ui.js FILE_CHUNK)
board = test_web_images.board
teams = test_web_images.teams


@pytest.fixture
def sp(monkeypatch: pytest.MonkeyPatch) -> FakeSharePoint:
    fake = FakeSharePoint()
    client = fake.client()
    monkeypatch.setattr(F, "_CLIENT", client)
    monkeypatch.setattr(S, "_CLIENT", client)
    monkeypatch.setattr(CV, "my_onedrive", lambda settings: F.OneDrive(SITE, "sp-token"))
    return fake


@pytest.fixture
def spools(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Path]]:
    """Every spool opened, with the directory it was opened in."""
    opened: list[tuple[Any, Path]] = []
    real = CV.tempfile.TemporaryFile

    def spy(*, dir: Path) -> Any:  # noqa: A002 — TemporaryFile's own keyword
        fh = real(dir=dir)
        opened.append((fh, Path(dir)))
        return fh

    monkeypatch.setattr(CV.tempfile, "TemporaryFile", spy)
    return opened


async def _stream(ws: Any, data: bytes, chunk: int = CHUNK) -> None:
    for at in range(0, len(data), chunk):
        await ws.send(data[at : at + chunk])


def _cards(body: dict[str, Any]) -> list[str]:
    return [e["fileName"] for e in json.loads(body["properties"]["files"])]


async def test_images_and_files_go_in_one_message(
    board: W.Board, settings: Settings, teams: Teams, sp: FakeSharePoint, spools: list[tuple[Any, Path]]
) -> None:
    """025-AC5: the images whole, then each file in chunks, become one message: the text, the inline
    images, the file cards; the bytes arrive intact; the page's clientmessageid is kept."""
    one, big, small = png(), bytes(range(256)) * (2 * CHUNK // 256 + 7), b"%PDF-1.7"
    verb = {
        "send": B,
        "text": "a < b",
        "images": _meta(one),
        "files": [{"name": "big.bin", "size": len(big)}, {"name": "a.pdf", "size": len(small)}],
        "cmid": "1760000000001",
    }
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        await ws.send(json.dumps(verb))
        await ws.send(one)
        await _stream(ws, big)
        await _stream(ws, small)
        assert await _socket_answer(ws) == {"sent": B, "ok": True}
    assert teams.uploads == [one] and teams.sends == []  # posted with its cards, not as a bare message
    (body,) = sp.posted
    assert body["clientmessageid"] == "1760000000001"
    assert body["content"].startswith("<p>a &lt; b</p><p><img ")
    assert _cards(body) == ["big.bin", "a.pdf"]
    assert [i["data"] for i in sp.items.values()] == [big, small]
    # Unnamed spools under the cache dir, all closed once sent.
    assert [d for _, d in spools] == [settings.cache_dir / "uploads"] * 2
    assert all(fh.closed for fh, _ in spools)
    assert list((settings.cache_dir / "uploads").iterdir()) == []


async def test_files_alone_are_a_message(
    board: W.Board, settings: Settings, teams: Teams, sp: FakeSharePoint
) -> None:
    """025-AC5: files without text are sent; a zero-byte file needs no frame."""
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        files = [{"name": "empty.txt", "size": 0}, {"name": "x.txt", "size": 1}]
        await ws.send(json.dumps({"send": B, "text": " ", "files": files}))
        await ws.send(b"x")
        assert await _socket_answer(ws) == {"sent": B, "ok": True}
        await ws.send(json.dumps({"send": B, "text": "", "files": [{"name": "e", "size": 0}]}))
        assert await _socket_answer(ws) == {"sent": B, "ok": True}
    assert [_cards(b) for b in sp.posted] == [["empty.txt", "x.txt"], ["e"]]
    assert [i["data"] for i in sp.items.values()] == [b"", b"x", b""]


@pytest.mark.parametrize(
    ("files", "error"),
    [
        ("x", "invalid files"),
        ([{"name": "a"}], "invalid files"),
        ([{"name": "a", "size": -1}], "invalid files"),
        ([{"name": "a", "size": 1.0}], "invalid files"),
        ([{"name": "a", "size": True}], "invalid files"),
        ([{"name": 5, "size": 1}], "invalid files"),
        ([{"name": "a", "size": 1}] * 11, "11 files: at most 10 per message"),
        ([{"name": "a", "size": (100 << 20) + 1}], "a: over 100 MiB"),
        ([{"name": "a:b", "size": 1}], "'a:b': not a file name OneDrive takes"),
    ],
)
def test_files_over_the_limits_are_refused(files: Any, error: str) -> None:
    """025-AC6: the server checks the announced files again."""
    assert CV.parse_send({"send": B, "text": "hi", "files": files}) == error


async def test_a_refused_send_awaits_no_frame(
    board: W.Board, settings: Settings, teams: Teams, sp: FakeSharePoint
) -> None:
    """025-AC6: refused at once, before any byte is spooled; the frames that follow are ignored."""
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        await ws.send(json.dumps({"send": B, "text": "a", "files": [{"name": "a", "size": 1}] * 11}))
        assert await _socket_answer(ws) == {"sent": B, "error": "11 files: at most 10 per message"}
        await ws.send(b"x")
        await ws.send(json.dumps({"send": B, "text": "b"}))
        assert await _socket_answer(ws) == {"sent": B, "ok": True}
    assert sp.requests == [] and board.chat._incoming == {}


async def test_exactly_the_limits_are_taken(board: W.Board) -> None:
    """025-AC6: ten files of 100 MiB each are within the limits (the bytes are not sent here)."""
    verb = {"send": B, "text": "", "files": [{"name": f"f{i}", "size": 100 << 20} for i in range(10)]}
    request = CV.parse_send(verb)
    assert isinstance(request, CV.SendRequest) and len(request.files) == 10


@pytest.mark.parametrize(
    ("frames", "error"),
    [
        ([b"123456"], "file size mismatch"),  # one chunk past the announced 5 bytes
        ([b"123", b"456"], "file size mismatch"),  # the second chunk crosses it
        ([b""], "file size mismatch"),  # an empty frame is no chunk
    ],
)
async def test_a_chunk_past_its_size_fails_and_uploads_nothing(
    board: W.Board,
    settings: Settings,
    teams: Teams,
    sp: FakeSharePoint,
    spools: list[tuple[Any, Path]],
    frames: list[bytes],
    error: str,
) -> None:
    """025-AC6: the cap holds while streaming: the bytes never exceed what the send announced; the
    spool is closed, nothing is uploaded or posted."""
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        await ws.send(json.dumps({"send": B, "text": "a", "files": [{"name": "a", "size": 5}]}))
        for f in frames:
            await ws.send(f)
        assert await _socket_answer(ws) == {"sent": B, "error": error}
    assert sp.requests == [] and teams.sends == []
    assert spools and all(fh.closed for fh, _ in spools)
    assert board.chat._incoming == {}


async def test_a_file_chunk_where_an_image_is_due_is_refused(board: W.Board) -> None:
    """025-AC5: images come first, whole: a chunk of another size in their place fails the send."""
    ws: Any = object()
    future = board.chat._expect(ws, (3,), (("a", 2),))
    board.chat.frame(ws, b"ab")
    with pytest.raises(ValueError, match="image size mismatch"):
        await future


async def test_frames_that_never_come_close_the_spools(
    board: W.Board,
    settings: Settings,
    teams: Teams,
    sp: FakeSharePoint,
    spools: list[tuple[Any, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """025-AC5: a stream cut short times out with a reason; its spool is closed, a late chunk ignored."""
    monkeypatch.setattr(CV, "_FRAMES_WAIT", 0.2)
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        await ws.send(json.dumps({"send": B, "text": "a", "files": [{"name": "a", "size": 5}]}))
        await ws.send(b"12")
        assert await _socket_answer(ws) == {"sent": B, "error": "files did not arrive"}
        await ws.send(b"345")  # late
        await ws.send(json.dumps({"send": B, "text": "b"}))
        assert await _socket_answer(ws) == {"sent": B, "ok": True}
    assert len(spools) == 1 and spools[0][0].closed and sp.items == {}


async def test_a_slow_stream_that_keeps_flowing_is_not_cut(
    board: W.Board, settings: Settings, teams: Teams, sp: FakeSharePoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """025-AC5: the wait is an idle one: chunks trickling in over far more than the idle time (a slow
    link) still make a message; a stream that then stalls fails after the idle time."""
    monkeypatch.setattr(CV, "_FRAMES_WAIT", 0.3)
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        await ws.send(json.dumps({"send": B, "text": "a", "files": [{"name": "a", "size": 6}]}))
        for chunk in (b"1", b"2", b"3", b"4", b"5", b"6"):  # 1.2 s in all, 0.2 s apart
            await asyncio.sleep(0.2)
            await ws.send(chunk)
        assert await _socket_answer(ws) == {"sent": B, "ok": True}
        loop = asyncio.get_running_loop()
        await ws.send(json.dumps({"send": B, "text": "b", "files": [{"name": "b", "size": 2}]}))
        await asyncio.sleep(0.2)
        await ws.send(b"1")
        stalled = loop.time()
        assert await _socket_answer(ws) == {"sent": B, "error": "files did not arrive"}
        assert 0.25 <= loop.time() - stalled < 1.5  # the idle time from the last chunk, not from the send
    assert [i["data"] for i in sp.items.values()] == [b"123456"]


async def test_a_closed_page_mid_stream_uploads_nothing(
    board: W.Board, settings: Settings, teams: Teams, sp: FakeSharePoint, spools: list[tuple[Any, Path]]
) -> None:
    """025-AC5: the socket dropped mid-file: the send is dropped, its spool closed, nothing uploaded."""
    async with _served(board, settings) as host:
        async with board_socket(host, board.secret) as ws:
            await ws.send(json.dumps({"send": B, "text": "a", "files": [{"name": "a", "size": 5}]}))
            await ws.send(b"12")
            await wait(lambda: bool(spools) and spools[0][0].tell() == 2, "first chunk")
        await wait(lambda: not board.chat._incoming and not board.chat._replies, "dropped")
    assert len(spools) == 1 and spools[0][0].closed and sp.requests == []


async def test_an_upload_failure_posts_nothing_and_says_why(
    board: W.Board, settings: Settings, teams: Teams, sp: FakeSharePoint, spools: list[tuple[Any, Path]]
) -> None:
    """025-AC4 (page side): a refused upload is a plain failure (not `unsure`): nothing posted, the
    images' uploads kept for the retry, the spools closed."""
    sp.fail["upload"] = 507
    one = png()
    verb = {"send": B, "text": "a", "images": _meta(one), "files": [{"name": "a", "size": 1}], "cmid": "7"}
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        await ws.send(json.dumps(verb))
        await ws.send(one)
        await ws.send(b"x")
        answer = await _socket_answer(ws)
    assert answer == {"sent": B, "error": "files not sent: Teams answered 507"}
    assert sp.posted == [] and sp.items == {} and len(spools) == 1 and spools[0][0].closed
    assert (B, "7") in board.chat._uploads  # ↻ reuses the inline image already uploaded


async def test_a_rejected_post_deletes_the_files(
    board: W.Board, settings: Settings, teams: Teams, sp: FakeSharePoint
) -> None:
    """025-AC4 (page side): Teams refusing the message leaves no orphan file."""
    sp.fail["post"] = 403
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        await ws.send(json.dumps({"send": B, "text": "a", "files": [{"name": "a", "size": 1}]}))
        await ws.send(b"x")
        answer = await _socket_answer(ws)
    assert "403" in answer["error"] and sp.items == {}


async def test_a_post_with_no_answer_is_unsure_and_keeps_the_files(
    board: W.Board, settings: Settings, teams: Teams, sp: FakeSharePoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """025-AC5, 017-AC13: a post lost in transit may have landed: `unsure` (the page's ↻ checks before
    sending again), and its files stay so a posted message keeps working cards."""
    monkeypatch.setattr(CV, "_ECHO_WAIT", 0.05)

    def hook(req: httpx.Request) -> None:
        if req.url.path.endswith("/messages"):
            raise httpx.ReadTimeout("slow")

    sp.hook = hook
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        await ws.send(json.dumps({"send": B, "text": "a", "files": [{"name": "a", "size": 1}], "cmid": "8"}))
        await ws.send(b"x")
        assert await _socket_answer(ws) == {"sent": B, "error": CV.UNSURE, "unsure": True}
    assert len(sp.items) == 1


async def test_a_checked_retry_of_a_posted_message_uploads_nothing(
    board: W.Board, settings: Settings, teams: Teams, sp: FakeSharePoint
) -> None:
    """025-AC5, 017-AC12: the retry of a message that went out finds it by its id: no upload, no post."""
    teams.history = [_mine("9", "42")]
    verb = {"send": B, "text": "a", "files": [{"name": "a", "size": 1}], "cmid": "42", "check": True}
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        await ws.send(json.dumps(verb))
        await ws.send(b"x")
        assert await _socket_answer(ws) == {"sent": B, "ok": True}
    assert sp.requests == [] and teams.history_calls == 1


async def test_a_retry_while_the_upload_runs_waits_for_it(
    board: W.Board, settings: Settings, teams: Teams, sp: FakeSharePoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """025-AC5, 017-AC13: a page resending after a reconnect while the first try still uploads gets that
    try's outcome: one upload, one post."""
    gate, started = threading.Event(), threading.Event()
    real = F.upload_files

    def slow(*a: Any) -> Any:
        started.set()
        gate.wait(5)
        return real(*a)

    monkeypatch.setattr(CV, "upload_files", slow)
    waiting = asyncio.Event()

    class Spy:  # CV's asyncio, its in-flight wait (shield) observed
        def __getattr__(self, name: str) -> Any:
            return getattr(asyncio, name)

        def shield(self, fut: Any) -> Any:
            waiting.set()
            return asyncio.shield(fut)

    monkeypatch.setattr(CV, "asyncio", Spy())
    verb = {"send": B, "text": "a", "files": [{"name": "a", "size": 1}], "cmid": "43"}
    async with _served(board, settings) as host:
        async with board_socket(host, board.secret) as first:
            await first.send(json.dumps(verb))
            await first.send(b"x")
            await asyncio.to_thread(started.wait, 5)
        async with board_socket(host, board.secret) as again:
            await again.send(json.dumps({**verb, "check": True}))
            await again.send(b"x")
            await asyncio.wait_for(waiting.wait(), 5)  # the retry waits on the first try, before it ends
            gate.set()
            assert await _socket_answer(again) == {"sent": B, "ok": True}
    assert len(sp.posted) == 1 and len(sp.items) == 1 and teams.history_calls == 0


class _Full(io.BytesIO):
    """A spool on a full disk."""

    def write(self, data: Any) -> int:
        raise OSError(28, "No space left on device")


async def test_a_full_disk_fails_the_send_not_the_socket(
    board: W.Board, settings: Settings, teams: Teams, sp: FakeSharePoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """025-AC5: a spool write failing (ENOSPC) fails that send once with the reason; the socket stays up."""
    monkeypatch.setattr(CV.tempfile, "TemporaryFile", lambda *, dir: _Full())  # noqa: A006
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        await ws.send(json.dumps({"send": B, "text": "a", "files": [{"name": "a", "size": 2}]}))
        await ws.send(b"x")
        await ws.send(b"y")  # after the failure: ignored
        assert await _socket_answer(ws) == {
            "sent": B,
            "error": "could not store the file: No space left on device",
        }
        await ws.send(json.dumps({"send": B, "text": "b"}))
        assert await _socket_answer(ws) == {"sent": B, "ok": True}
    assert sp.requests == [] and board.chat._incoming == {}


async def test_a_spool_that_cannot_be_created_answers_at_once(
    board: W.Board, settings: Settings, teams: Teams, sp: FakeSharePoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """025-AC5: no spool (a full or read-only cache dir): the page gets the reason, already opened ones
    are closed, the socket stays up."""
    made: list[Any] = []

    def second_fails(*, dir: Path) -> Any:  # noqa: A002
        if made:
            raise OSError(30, "Read-only file system")
        made.append(io.BytesIO())
        return made[0]

    monkeypatch.setattr(CV.tempfile, "TemporaryFile", second_fails)
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        files = [{"name": "a", "size": 1}, {"name": "b", "size": 1}]
        await ws.send(json.dumps({"send": B, "text": "a", "files": files}))
        assert await _socket_answer(ws) == {
            "sent": B,
            "error": "could not store the file: Read-only file system",
        }
        await ws.send(json.dumps({"send": B, "text": "b"}))
        assert await _socket_answer(ws) == {"sent": B, "ok": True}
    assert made[0].closed and board.chat._incoming == {} and sp.requests == []


async def test_a_send_short_of_its_files_is_told_attachments_missing(
    board: W.Board, settings: Settings, teams: Teams, sp: FakeSharePoint
) -> None:
    """025-AC5: the next send starting while one still lacks file bytes fails that one, by its name."""
    one = png()
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        await ws.send(json.dumps({"send": B, "text": "a", "files": [{"name": "a", "size": 5}]}))
        await ws.send(b"12")
        await ws.send(json.dumps({"send": B, "text": "b", "images": _meta(one)}))
        assert await _socket_answer(ws) == {"sent": B, "error": "attachments missing"}
        await ws.send(one)
        assert await _socket_answer(ws) == {"sent": B, "ok": True}
        await ws.send(json.dumps({"send": B, "text": "c", "images": _meta(one)}))
        await ws.send(json.dumps({"send": B, "text": "d", "images": _meta(one)}))
        assert await _socket_answer(ws) == {"sent": B, "error": "images missing"}
        await ws.send(one)
        assert await _socket_answer(ws) == {"sent": B, "ok": True}


def _quotable(board: W.Board, monkeypatch: pytest.MonkeyPatch, gate: asyncio.Event | None = None) -> None:
    """Message 1 quotable; its lookup waits on `gate` when given."""

    async def copies(thread_id: str, ids: list[str]) -> dict[str, Any]:
        if gate is not None:
            await gate.wait()
        return {
            i: {"id": i, "from": f"8:orgid:{i}", "messagetype": "RichText/Html", "content": "q"} for i in ids
        }

    monkeypatch.setattr(board.chat, "_server_copies", copies)
    monkeypatch.setattr(board.chat, "_reply_quote", lambda message: "<blockquote>q</blockquote>")


async def test_a_quoted_send_holds_the_action_slot_for_its_lookup_only(
    board: W.Board, settings: Settings, teams: Teams, sp: FakeSharePoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """025-AC5, 011-AC14: a quoted send with files frees the socket's action slot once its quote is read:
    a reaction on the same socket during its upload is not answered `busy`."""
    _quotable(board, monkeypatch)
    started, gate = threading.Event(), threading.Event()
    real = F.upload_files

    def slow(*a: Any) -> Any:
        started.set()
        gate.wait(5)
        return real(*a)

    monkeypatch.setattr(CV, "upload_files", slow)
    reactions: list[tuple[str, str]] = []
    monkeypatch.setattr(CV, "react", lambda s, t, th, msg, key, remove: reactions.append((msg, key)))
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        await ws.send(
            json.dumps({"send": B, "text": "a", "quote": ["1"], "files": [{"name": "a", "size": 1}]})
        )
        await ws.send(b"x")
        await asyncio.to_thread(started.wait, 5)
        held = set(board.chat._action_busy)
        await ws.send(json.dumps({"react": B, "msg": "1", "key": "like", "remove": False}))
        while "react" not in (reacted := json.loads(await asyncio.wait_for(ws.recv(), 5))):
            pass
        gate.set()
        assert await _socket_answer(ws) == {"sent": B, "ok": True}
    assert held == set() and sp.posted[0]["content"].startswith("<blockquote>q</blockquote>")
    assert reacted == {"react": B, "msg": "1", "ok": True} and reactions == [("1", "like")]


async def test_a_busy_quoted_send_with_files_closes_its_spool(
    board: W.Board,
    settings: Settings,
    teams: Teams,
    sp: FakeSharePoint,
    spools: list[tuple[Any, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """025-AC5, 011-AC14: a quoted send meeting another quote lookup on its socket answers `busy`, its
    spool closed, nothing uploaded."""
    gate = asyncio.Event()
    _quotable(board, monkeypatch, gate)
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        await ws.send(json.dumps({"send": B, "text": "first", "quote": ["1"]}))
        await wait(lambda: bool(board.chat._action_busy), "first lookup")
        await ws.send(
            json.dumps({"send": B, "text": "a", "quote": ["1"], "files": [{"name": "a", "size": 1}]})
        )
        await ws.send(b"x")
        assert await _socket_answer(ws) == {"sent": B, "error": "busy"}
        gate.set()
        assert await _socket_answer(ws) == {"sent": B, "ok": True}
    assert len(spools) == 1 and spools[0][0].closed and sp.items == {}
