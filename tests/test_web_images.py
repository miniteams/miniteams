"""Pasted images on the page socket (spec 012 phase 5b): the `send` frame, its binary image frames,
validation, upload reuse on retry, and unsure sends (no blind resend)."""

import asyncio
import json
import threading
from typing import Any

import httpx
import pytest
import websockets
from conftest import board_socket
from test_images import jpeg, png
from test_web_chat import _served

from miniteams import chat_verbs as CV
from miniteams import send as S
from miniteams import web as W
from miniteams.config import Settings
from miniteams.directory import Directory

B = "19:bbbb@thread.v2"  # on the board
ME = "8:orgid:00000000-0000-0000-0000-00000000000a"


@pytest.fixture
def board(directory: Directory, monkeypatch: pytest.MonkeyPatch) -> W.Board:
    async def name(self: Directory, key: str) -> str:
        return f"name:{key}"

    monkeypatch.setattr(Directory, "display", name)
    monkeypatch.setattr(Directory, "label", name)
    rows = {
        B: {"id": B, "label": "B", "last_activity": "2026-10-13T10:00:00.000Z", "seen_at": None, "typing": []}
    }
    return W.Board(rows, directory, me=ME)


class Teams:
    """Fake chat service: uploads hand out ids in order, sends are recorded; `fail_upload` /
    `fail_send` raise on the n-th call (1-based)."""

    def __init__(self) -> None:
        self.uploads: list[bytes] = []
        self.sends: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_upload = 0
        self.fail_send: BaseException | None = None
        self.history: list[dict[str, Any]] | Exception = []
        self.history_calls = 0
        self.history_sizes: list[int] = []

    def upload(self, settings: Any, token: str, thread: str, data: bytes, filename: str) -> str:
        self.uploads.append(data)
        if len(self.uploads) == self.fail_upload:
            raise httpx.HTTPStatusError("image upload failed", request=None, response=httpx.Response(403))  # type: ignore[arg-type]
        return f"0-weu-d1-{len(self.uploads):032x}"

    def send(self, settings: Any, token: str, thread: str, text: str, name: str, **kw: Any) -> dict[str, Any]:
        self.sends.append((thread, text, kw))
        if self.fail_send is not None:
            raise self.fail_send
        return {"status": 201}

    def fetch(self, settings: Any, token: str, thread: str, size: int, pages: int) -> list[dict[str, Any]]:
        self.history_calls += 1
        self.history_sizes.append(size * pages)
        if isinstance(self.history, Exception):
            raise self.history
        return self.history


@pytest.fixture
def teams(monkeypatch: pytest.MonkeyPatch) -> Teams:
    fake = Teams()
    monkeypatch.setattr(S, "upload_image", fake.upload)
    monkeypatch.setattr(CV, "send_message", fake.send)
    monkeypatch.setattr(CV, "fetch_history", fake.fetch)
    return fake


def _meta(*blobs: bytes, w: int = 3, h: int = 2) -> list[dict[str, Any]]:
    return [{"w": w, "h": h, "size": len(b)} for b in blobs]


async def _answer(board: W.Board, verb: dict[str, Any], blobs: list[bytes] | None = None) -> dict[str, Any]:
    """The `send` answer with its image frames already arrived."""
    frames = None
    if blobs is not None:
        frames = asyncio.get_running_loop().create_future()
        frames.set_result(blobs)
    return await board.chat.send_answer(None, verb, frames)


async def _socket_answer(ws: Any) -> dict[str, Any]:
    while True:
        frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
        if "sent" in frame:
            return dict(frame)


# --- wire: the send frame, then one binary frame per image, in order ---


async def test_images_follow_their_send_in_order(board: W.Board, settings: Settings, teams: Teams) -> None:
    """012-AC21, 012-AC24: binary frames pair with the pending send in order; one message, text
    first, then the images, sized from their bytes; the page's clientmessageid is kept."""
    one, two = png(3, 2), jpeg(5, 4)
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        verb = {"send": B, "text": "hi", "images": _meta(one, two), "cmid": "1760000000000"}
        await ws.send(json.dumps(verb))
        await ws.send(one)
        await ws.send(two)
        assert await _socket_answer(ws) == {"sent": B, "ok": True}
    assert teams.uploads == [one, two]
    [(thread, content, kw)] = teams.sends
    assert thread == B and kw == {"is_html": True, "client_message_id": "1760000000000"}
    assert content.startswith("<p>hi</p><p><img ")
    first, second = content.index(f"0-weu-d1-{1:032x}"), content.index(f"0-weu-d1-{2:032x}")
    assert first < second
    assert 'height="2"' in content[:second] and 'width="5"' in content[second:]


async def test_images_alone_are_a_message(board: W.Board, teams: Teams) -> None:
    """012-AC21: images without text are sent; empty text without images is still refused."""
    one = png()
    assert await _answer(board, {"send": B, "text": " ", "images": _meta(one)}, [one]) == {
        "sent": B,
        "ok": True,
    }
    assert teams.sends[0][1].startswith("<p><img ")
    assert await _answer(board, {"send": B, "text": " "}) == {"sent": B, "error": "empty message"}


@pytest.mark.parametrize(
    ("over", "error"),
    [
        ({"images": "x"}, "invalid images"),
        ({"images": [1]}, "invalid images"),
        ({"images": [{"w": 3, "h": 2, "size": 9}] * 9}, "at most 8 images per message"),
        ({"images": [{"w": 3.0, "h": 2, "size": 9}]}, "image dimensions must be integers 1-100000"),
        ({"images": [{"w": 3, "h": 0, "size": 9}]}, "image dimensions must be integers 1-100000"),
        ({"images": [{"w": 3, "h": True, "size": 9}]}, "image dimensions must be integers 1-100000"),
        ({"images": [{"w": 3, "h": 2, "size": 0}]}, "invalid images"),
        ({"images": [{"w": 3, "h": 2, "size": True}]}, "invalid images"),
        ({"images": [{"w": 3, "h": 2, "size": (10 << 20) + 1}]}, "an image is over 10 MiB"),
        ({"images": [{"w": 3, "h": 2, "size": 9 << 20}] * 5}, "images are over 40 MiB in total"),
        ({"cmid": "abc"}, "invalid message id"),
        ({"cmid": 17}, "invalid message id"),
        ({"check": True}, "invalid message id"),
        ({"cmid": "1", "check": "yes"}, "invalid message id"),
    ],
)
def test_send_refuses_bad_image_announcements(over: dict[str, Any], error: str) -> None:
    """012-AC24, 012-AC25: count, byte and dimension limits on the announced images, before any frame
    is awaited; the message id is digits only."""
    assert CV.parse_send({"send": B, "text": "hi", **over}) == error


@pytest.mark.parametrize("blob", [b"GIF8" + b"\0" * 20, b"<svg onload=alert(1)>", png()[:20]])
async def test_frame_bytes_decide_not_the_label(board: W.Board, teams: Teams, blob: bytes) -> None:
    """012-AC24, 012-AC25: the bytes are sniffed (an image label or size claims nothing); a refused
    image uploads and sends nothing."""
    answer = await _answer(board, {"send": B, "text": "hi", "images": _meta(blob)}, [blob])
    assert answer == {"sent": B, "error": "unsupported image: PNG, JPEG, GIF or WebP only"}
    assert teams.uploads == [] and teams.sends == []


async def test_short_and_missing_frames_fail_the_send(
    board: W.Board, settings: Settings, teams: Teams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """012-AC24: a frame of another size than announced, a send whose frames never come, and an
    earlier send still short of frames when the next one starts all fail without sending."""
    monkeypatch.setattr(CV, "_FRAMES_WAIT", 0.3)
    one = png()
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        await ws.send(json.dumps({"send": B, "text": "a", "images": _meta(one)}))
        await ws.send(one[:-1])
        assert await _socket_answer(ws) == {"sent": B, "error": "image size mismatch"}
        await ws.send(json.dumps({"send": B, "text": "b", "images": _meta(one, one)}))
        await ws.send(one)
        assert await _socket_answer(ws) == {"sent": B, "error": "images did not arrive"}
        await ws.send(json.dumps({"send": B, "text": "c", "images": _meta(one, one)}))
        await ws.send(one)
        await ws.send(json.dumps({"send": B, "text": "d", "images": _meta(one)}))
        assert await _socket_answer(ws) == {"sent": B, "error": "images missing"}
        await ws.send(one)
        assert await _socket_answer(ws) == {"sent": B, "ok": True}
        await ws.send(one)  # no send waiting: ignored, the socket stays up
        await ws.send(json.dumps({"send": B, "text": "e"}))
        assert await _socket_answer(ws) == {"sent": B, "ok": True}
    assert teams.uploads == [one]
    assert [text for _, text, _ in teams.sends][0].startswith("<p>d</p>")


async def test_closed_socket_drops_its_waiting_send(board: W.Board, settings: Settings, teams: Teams) -> None:
    """012-AC24: frames cut short by a closed page upload nothing."""
    one = png()
    async with _served(board, settings) as host:
        async with board_socket(host, board.secret) as ws:
            await ws.send(json.dumps({"send": B, "text": "a", "images": _meta(one, one)}))
            await ws.send(one)
        for _ in range(100):
            if not board.chat._incoming and not board.chat._replies:
                break
            await asyncio.sleep(0.01)
    # The waiting send is cancelled, not left to time out 30 s later.
    assert board.chat._incoming == {} and board.chat._replies == set()
    assert teams.uploads == [] and teams.sends == []


async def test_oversize_frame_closes_the_socket(board: W.Board, settings: Settings, teams: Teams) -> None:
    """012-AC25: a frame past MAX_FRAME (an image over the limit, plus room) is refused by the socket."""
    full = b"\0" * (10 << 20)
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        await ws.send(json.dumps({"send": B, "text": "", "images": _meta(full)}))
        await ws.send(full)  # within the frame limit: it reaches the content check
        assert await _socket_answer(ws) == {
            "sent": B,
            "error": "unsupported image: PNG, JPEG, GIF or WebP only",
        }
        await ws.send(b"\0" * (W.MAX_FRAME + 1))
        with pytest.raises(websockets.ConnectionClosed) as closed:
            while True:
                await asyncio.wait_for(ws.recv(), 5)
        assert closed.value.rcvd is not None and closed.value.rcvd.code == 1009


# --- failures: the draft stays, a retry reuses what was uploaded ---


async def test_failed_upload_is_reused_on_retry(board: W.Board, teams: Teams) -> None:
    """012-AC23: an upload failure answers the reason; the retry of the same draft (same cmid) uploads
    only what is missing; another draft reuses nothing; success forgets the draft's objects."""
    one, two = png(3, 2), png(4, 2)
    verb = {"send": B, "text": "hi", "images": _meta(one, two), "cmid": "11"}
    teams.fail_upload = 2
    assert await _answer(board, verb, [one, two]) == {"sent": B, "error": "Teams answered 403"}
    assert teams.sends == []
    teams.fail_upload = 0
    assert await _answer(board, verb, [one, two]) == {"sent": B, "ok": True}
    assert teams.uploads == [one, two, two]
    assert f"0-weu-d1-{1:032x}" in teams.sends[0][1] and f"0-weu-d1-{3:032x}" in teams.sends[0][1]
    assert await _answer(board, verb, [one, two]) == {"sent": B, "ok": True}
    assert await _answer(board, {**verb, "cmid": "12"}, [one, two]) == {"sent": B, "ok": True}
    assert len(teams.uploads) == 7


async def test_same_picture_twice_gets_two_objects(board: W.Board, teams: Teams) -> None:
    """012-AC20: one picture pasted twice is two images of the message."""
    one = png()
    assert await _answer(
        board, {"send": B, "text": "", "images": _meta(one, one), "cmid": "5"}, [one, one]
    ) == {
        "sent": B,
        "ok": True,
    }
    assert len(teams.uploads) == 2
    content = teams.sends[0][1]
    assert f'id="0-weu-d1-{1:032x}"' in content and f'id="0-weu-d1-{2:032x}"' in content


async def test_upload_cache_is_bounded(board: W.Board, teams: Teams) -> None:
    """Forever-running server: failed drafts keep at most _UPLOAD_DRAFTS upload sets."""
    one = png()
    teams.fail_send = httpx.ConnectError("down")
    for i in range(CV._UPLOAD_DRAFTS + 5):
        await _answer(board, {"send": B, "text": "", "images": _meta(one), "cmid": str(i + 1)}, [one])
    assert len(board.chat._uploads) == CV._UPLOAD_DRAFTS
    assert (B, "1") not in board.chat._uploads and (B, str(CV._UPLOAD_DRAFTS + 5)) in board.chat._uploads
    # A retry touches its draft: the least recently tried one ("6"), retried, outlives "7".
    await _answer(board, {"send": B, "text": "", "images": _meta(one), "cmid": "6"}, [one])
    await _answer(board, {"send": B, "text": "", "images": _meta(one), "cmid": "99"}, [one])
    assert (B, "6") in board.chat._uploads and (B, "7") not in board.chat._uploads


# --- unsure sends: no blind resend ---


async def test_unsure_send_settles_on_its_echo(
    board: W.Board, teams: Teams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """012-AC23: a request lost after it may have reached Teams waits for its own echo: echoed, it
    answers ok; it is posted once, never again on its own."""
    monkeypatch.setattr(CV, "_ECHO_WAIT", 5.0)
    teams.fail_send = httpx.ReadTimeout("slow")
    task = asyncio.create_task(_answer(board, {"send": B, "text": "hi", "cmid": "42"}))
    await asyncio.sleep(0.2)
    assert not task.done()
    await board.on_event({"resourceType": "NewMessage", "resource": _echo("41")})  # someone else's
    await asyncio.sleep(0.05)
    assert not task.done()
    await board.on_event({"resourceType": "NewMessage", "resource": _echo("42")})
    assert await asyncio.wait_for(task, 2) == {"sent": B, "ok": True}
    assert len(teams.sends) == 1


@pytest.mark.parametrize(
    "lost",
    [
        httpx.ReadTimeout("slow"),
        httpx.ReadError("reset"),
        httpx.RemoteProtocolError("eof"),
        httpx.WriteTimeout("w"),
        httpx.WriteError("w"),
    ],
)
async def test_unsure_send_without_echo_says_so(
    board: W.Board, teams: Teams, monkeypatch: pytest.MonkeyPatch, lost: Exception
) -> None:
    """012-AC23: no echo in time: `unsure`, posted once."""
    monkeypatch.setattr(CV, "_ECHO_WAIT", 0.05)
    teams.fail_send = lost
    assert await _answer(board, {"send": B, "text": "hi", "cmid": "42"}) == {
        "sent": B,
        "error": CV.UNSURE,
        "unsure": True,
    }
    assert len(teams.sends) == 1 and board.chat._echoes == {}


@pytest.mark.parametrize(
    "never_sent", [httpx.ConnectError("refused"), httpx.ConnectTimeout("t"), httpx.PoolTimeout("p")]
)
async def test_unsent_failure_is_not_unsure(board: W.Board, teams: Teams, never_sent: Exception) -> None:
    """012-AC23: a request that never left is a plain failure, retried without a check."""
    teams.fail_send = never_sent
    answer = await _answer(board, {"send": B, "text": "hi", "cmid": "42"})
    assert answer == {"sent": B, "error": "network error"}


def _echo(cmid: str) -> dict[str, Any]:
    return {
        "conversationLink": f"https://h/v1/users/ME/conversations/{B}",
        "id": "1760000000001",
        "clientmessageid": cmid,
        "composetime": "2026-10-13T10:00:01.000Z",
        "messagetype": "RichText/Html",
        "content": "<p>hi</p>",
        "from": f"https://h/v1/users/ME/contacts/{ME}",
        "imdisplayname": "Me",
    }


async def test_checked_retry_sends_only_when_missing(board: W.Board, teams: Teams) -> None:
    """012-AC23: a checked retry reads the chat's latest messages first: my message with that id found,
    it is not posted again (someone else's same id does not count); absent, it is posted with the same
    clientmessageid; unreadable, nothing goes."""
    one = png()
    verb = {"send": B, "text": "hi", "images": _meta(one), "cmid": "42", "check": True}
    teams.history = [_mine("1", "41"), _mine("2", "42")]
    assert await _answer(board, verb, [one]) == {"sent": B, "ok": True}
    assert teams.sends == [] and teams.uploads == []
    teams.history = [
        _mine("1", "41"),
        {**_mine("2", "42"), "from": "https://h/v1/users/ME/contacts/8:orgid:other"},
    ]
    assert await _answer(board, verb, [one]) == {"sent": B, "ok": True}
    assert [kw["client_message_id"] for _, _, kw in teams.sends] == ["42"]
    teams.history = httpx.ConnectError("down")
    answer = await _answer(board, verb, [one])
    assert answer == {"sent": B, "error": "could not check whether it went out: network error"}
    assert len(teams.sends) == 1 and teams.history_calls == 3
    assert teams.history_sizes == [100] * 3  # the latest 100 messages, one page
    assert await _answer(board, {**verb, "check": False}, [one]) == {"sent": B, "ok": True}
    assert teams.history_calls == 3  # no check asked: none made


async def test_same_draft_cannot_send_twice_at_once(board: W.Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """012-AC23: a retry while the first try of the same draft still runs is refused, not raced."""
    release, started, calls = threading.Event(), threading.Event(), []

    def slow(*args: Any, **kw: Any) -> dict[str, Any]:
        calls.append(kw)
        started.set()
        release.wait(5)
        return {"status": 201}

    monkeypatch.setattr(CV, "send_message", slow)
    verb = {"send": B, "text": "hi", "cmid": "42"}
    first = asyncio.create_task(_answer(board, verb))
    await asyncio.to_thread(started.wait, 5)
    assert await _answer(board, verb) == {"sent": B, "error": "still sending: try again in a moment"}
    release.set()
    assert await first == {"sent": B, "ok": True}
    assert [kw["client_message_id"] for kw in calls] == ["42"]
    assert await _answer(board, verb) == {"sent": B, "ok": True}  # once answered, the draft may go again


def _mine(msg_id: str, cmid: str) -> dict[str, Any]:
    return {"id": msg_id, "clientmessageid": cmid, "from": f"https://h/v1/users/ME/contacts/{ME}"}


@pytest.mark.parametrize(
    "posted_anyway",
    [
        ValueError("Expecting value: line 1 column 1 (char 0)"),  # a 201 whose body is not JSON
        httpx.HTTPStatusError("send failed: HTTP 504", request=None, response=httpx.Response(504)),  # type: ignore[arg-type]
    ],
)
async def test_a_definite_looking_failure_is_still_checked_on_retry(
    board: W.Board, teams: Teams, posted_anyway: Exception
) -> None:
    """012-AC23: a failure that may hide a posted message: the page's retry of the same draft carries
    `check`, the lookup finds it, nothing is posted twice and its objects are not kept."""
    one = png()
    verb = {"send": B, "text": "hi", "images": _meta(one), "cmid": "42"}
    teams.fail_send = posted_anyway
    assert "error" in await _answer(board, verb, [one])
    teams.fail_send = None
    teams.history = [_mine("9", "42")]
    assert await _answer(board, {**verb, "check": True}, [one]) == {"sent": B, "ok": True}
    assert len(teams.sends) == 1 and teams.history_calls == 1 and teams.uploads == [one]
    assert (B, "42") not in board.chat._uploads


async def test_late_frame_after_the_wait_leaves_the_socket_up(
    board: W.Board, settings: Settings, teams: Teams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """012-AC24: frames that come after their send gave up are dropped with its partial blobs; the
    socket and the next sends carry on."""
    monkeypatch.setattr(CV, "_FRAMES_WAIT", 0.2)
    one = png()
    async with _served(board, settings) as host, board_socket(host, board.secret) as ws:
        await ws.send(json.dumps({"send": B, "text": "a", "images": _meta(one, one)}))
        await ws.send(one)
        assert await _socket_answer(ws) == {"sent": B, "error": "images did not arrive"}
        assert board.chat._incoming == {}
        await ws.send(one)  # late
        await ws.send(json.dumps({"send": B, "text": "b", "images": _meta(one)}))
        await ws.send(one)
        assert await _socket_answer(ws) == {"sent": B, "ok": True}
    assert teams.uploads == [one] and len(teams.sends) == 1


async def test_a_frame_for_a_cancelled_wait_is_ignored(board: W.Board) -> None:
    """012-AC24: a frame meeting a send that stopped waiting neither raises nor stays queued."""
    ws: Any = object()
    future = board.chat._expect(ws, (3,))
    future.cancel()
    board.chat.frame(ws, b"abc")
    assert board.chat._incoming == {}


async def test_a_retry_during_the_roster_fetch_is_refused(
    board: W.Board, teams: Teams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """012-AC23: the draft is marked in flight before the first await: a checked retry arriving while
    the roster is fetched cannot post it a second time."""
    gate = asyncio.Event()

    async def slow_members(thread_id: str) -> list[dict[str, Any]]:
        await gate.wait()
        return [{"mri": "8:orgid:ann", "name": "Ann Lee"}]

    monkeypatch.setattr(board.chat, "_members", slow_members)
    mention = {"name": "Ann Lee", "mri": "8:orgid:ann"}
    first = asyncio.create_task(
        _answer(board, {"send": B, "text": "@Ann Lee hi", "mentions": [mention], "cmid": "42"})
    )
    await asyncio.sleep(0.05)
    retry = {"send": B, "text": "@Ann Lee hi", "cmid": "42", "check": True}
    assert await _answer(board, retry) == {"sent": B, "error": "still sending: try again in a moment"}
    gate.set()
    assert await first == {"sent": B, "ok": True}
    assert len(teams.sends) == 1 and teams.history_calls == 0


async def test_an_echo_in_another_chat_does_not_settle(
    board: W.Board, teams: Teams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """012-AC23: the echo that settles an unsure send is its own chat's: the same id elsewhere is not."""
    monkeypatch.setattr(CV, "_ECHO_WAIT", 0.5)
    teams.fail_send = httpx.ReadTimeout("slow")
    task = asyncio.create_task(_answer(board, {"send": B, "text": "hi", "cmid": "42"}))
    await asyncio.sleep(0.1)
    other = {**_echo("42"), "conversationLink": "https://h/v1/users/ME/conversations/19:cccc@thread.v2"}
    await board.on_event({"resourceType": "NewMessage", "resource": other})
    assert (await task)["error"] == CV.UNSURE


async def test_a_quote_comes_before_the_images(
    board: W.Board, teams: Teams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """012-AC21: a quoted reply with images: quote block, then the text, then the images."""
    quoted = {
        "id": "1700000000000",
        "from": "https://h/v1/users/ME/contacts/8:orgid:ann",
        "imdisplayname": "Ann Lee",
        "messagetype": "RichText/Html",
        "content": "<p>original</p>",
        "composetime": "2026-10-13T09:00:00.000Z",
    }

    async def copies(thread_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        return {quoted["id"]: quoted}

    monkeypatch.setattr(board.chat, "_server_copies", copies)
    one = png()
    verb = {"send": B, "text": "hi", "quote": [quoted["id"]], "images": _meta(one)}
    assert await _answer(board, verb, [one]) == {"sent": B, "ok": True}
    content = teams.sends[0][1]
    assert content.index("original") < content.index("<p>hi</p>") < content.index("<img ")


@pytest.mark.parametrize(
    ("text", "start"),
    [("**b**", "<p><b>b</b></p><p><img "), ("- a\n- b", "<ul><li>a</li><li>b</li></ul><p><img ")],
)
async def test_full_format_with_images_has_one_paragraph_level(
    board: W.Board, teams: Teams, text: str, start: str
) -> None:
    """012-AC21: the chat page's full format: inline text gets one paragraph, a block none."""
    one = png()
    verb = {"send": B, "text": text, "format": "full", "images": _meta(one)}
    assert await _answer(board, verb, [one]) == {"sent": B, "ok": True}
    assert teams.sends[0][1].startswith(start)
