"""Chat view server side (spec 011 phase 1): `history`, `follow`, message records, live relay."""

import asyncio
import base64
import contextlib
import functools
import hashlib
import json
import logging
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
import structlog
import structlog.testing
import websockets
from conftest import board_socket, cookie_header, wait

from miniteams import chat_verbs as CV
from miniteams import compose
from miniteams import web as W
from miniteams.archive_store import ChatStore, Index, Page, find_chat_dir
from miniteams.config import Settings
from miniteams.directory import Directory
from miniteams.emojis import EmojiCache
from miniteams.messages import EMOJI, REACTION_KEY_MAX, _reaction_base, reaction_emoji, reaction_key
from miniteams.teams_emojis import TEAMS_EMOJIS

T = "19:aaaa@thread.v2"  # archived, not on the board
MEETING = "19:meeting_fictional@thread.v2"
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


async def _history(
    board: W.Board,
    thread: Any,
    before: Any = None,
    limit: Any = None,
    *,
    after: Any = None,
    around: Any = None,
) -> dict[str, Any]:
    """The `history` verb as the socket answers it."""
    verb = {"history": thread, "before": before, "limit": limit, "after": after, "around": around}
    return await board.chat.history_answer({k: v for k, v in verb.items() if v is not None})


async def _send(
    board: W.Board,
    thread: Any,
    text: Any,
    ws: Any = None,
    quote: Any = None,
    mentions: Any = None,
    fmt: Any = None,
) -> str | None:
    """The `send` verb; its refusal or failure reason, None once sent."""
    verb = {"send": thread, "text": text, "quote": quote, "mentions": mentions, "format": fmt}
    error: str | None = (await board.chat.send_answer(ws, verb)).get("error")
    return error


async def _search(board: W.Board, thread: Any, q: Any) -> dict[str, Any]:
    return await board.chat.search_answer({"search": thread, "q": q})


class _Recorder:
    """A socket that keeps what it is sent."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send(self, data: str) -> None:
        self.frames.append(json.loads(data))


async def _answered(head: dict[str, Any], work: Any) -> dict[str, Any]:
    """What the page receives for `work`, through `answer` as the socket handlers run it."""
    ws = _Recorder()
    await CV.answer(ws, head, work)  # type: ignore[arg-type]
    [frame] = ws.frames
    return frame


@pytest.fixture
def quiet_directory(directory: Directory, monkeypatch: pytest.MonkeyPatch) -> Directory:
    async def fake_display(self: Directory, mri: str) -> str:
        return f"name:{mri}"

    async def fake_label(self: Directory, thread_id: str) -> str:
        return f"label:{thread_id}"

    async def no_roster(self: Directory, thread_id: str) -> dict[str, Any] | None:
        return None  # what a 404 caches: a follow of a counted chat fetches its roster (spec 022)

    monkeypatch.setattr(Directory, "display", fake_display)
    monkeypatch.setattr(Directory, "label", fake_label)
    monkeypatch.setattr(Directory, "_fetch_thread", no_roster)
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
    monkeypatch.setattr(CV, "fetch_history", lambda *a: pytest.fail("no API call expected"))
    monkeypatch.setattr(CV, "send_message", lambda *a: pytest.fail("no API call expected"))


def _ids(answer: dict[str, Any]) -> list[str]:
    return [m["id"] for m in answer["messages"]]


# --- history: archive (011-AC4) ---


async def test_history_latest_page_oldest_first(board: W.Board, no_api: None) -> None:
    """011-AC4: the last `limit` messages, oldest first, `older` while earlier ones exist."""
    answer = await _history(board, T, limit=2)
    assert answer["history"] == T
    assert (_ids(answer), answer["older"], answer["newer"], answer["source"]) == (
        ["4", "5"],
        True,
        False,
        "archive",
    )
    assert set(answer) == {"history", "before", "messages", "older", "newer", "source"}
    assert answer["before"] is None
    before = await _history(board, T, before=answer["messages"][0]["at"], limit=2)
    assert (_ids(before), before["older"]) == (["2", "3"], True)
    first = await _history(board, T, before=before["messages"][0]["at"], limit=2)
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
    answer = await _history(board, T, limit=3)
    while True:
        pages.insert(0, _ids(answer))
        if not answer["older"]:
            break
        answer = await _history(board, T, before=answer["messages"][0]["at"], limit=3)
    # 5, 6, 7 fill the page; 3 and 4 share 5's time, so they join it rather than be skipped.
    assert pages == [["1", "2"], ["3", "4", "5", "6", "7"]]
    assert (await _history(board, T, before=_at(1)))["messages"] == []


async def test_history_limit_default_and_cap(tmp_path: Path, quiet_directory: Directory) -> None:
    """011-AC4: default page 100, capped at 200, junk limits fall back to the default."""
    data = _archive(tmp_path / "d", T, [_m(str(i), _at(i)) for i in range(250)])
    board = W.Board({}, quiet_directory, data_dir=data)
    assert len((await _history(board, T))["messages"]) == 100
    assert len((await _history(board, T, limit=500))["messages"]) == 200
    for junk in (0, -3, "50", True, 2.5, None):
        assert len((await _history(board, T, limit=junk))["messages"]) == 100


async def test_history_of_an_empty_archived_chat(
    tmp_path: Path, quiet_directory: Directory, no_api: None
) -> None:
    """011-AC4: an archived chat without messages is an empty page, not an error."""
    board = W.Board({}, quiet_directory, data_dir=_archive(tmp_path / "d", T, []))
    assert await _history(board, T) == {
        "history": T,
        "before": None,
        "messages": [],
        "older": False,
        "newer": False,
        "source": "archive",
    }


async def test_history_refuses_a_non_text_cursor(board: W.Board, no_api: None) -> None:
    """011-AC4"""
    assert await _history(board, T, before=123) == {"history": T, "before": None, "error": "invalid before"}


async def test_history_rewrites_archived_images(board: W.Board, data_dir: Path) -> None:
    """011-AC4 + 011-AC6: images in a history page point at /media when archived."""
    store = ChatStore(data_dir, T)
    src = f"https://eu-api.asm.skype.com/v1/objects/{OBJ}/views/imgo"
    store.insert_page(
        [_m("9", _at(9), f'<p><img itemtype="http://schema.skype.com/AMSImage" src="{src}"></p>')]
    )
    (store.media_dir / f"{OBJ}.jpg").write_bytes(b"jpg")
    store.close()
    answer = await _history(board, T, limit=1)
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

    monkeypatch.setattr(CV, "fetch_history", fake_history)
    return calls


async def test_history_without_archive_reads_one_api_page(board: W.Board, api_calls: list[Any]) -> None:
    """011-AC5: a board chat without messages.db gets one API page, `source: "api"`."""
    answer = await _history(board, B, limit=50)
    assert (_ids(answer), answer["older"], answer["newer"], answer["source"]) == (
        ["1", "2", "3"],
        True,
        False,
        "api",
    )
    assert api_calls == [(B, 50, 1, None)]


async def test_history_api_before_cuts_at_the_exact_time(board: W.Board, api_calls: list[Any]) -> None:
    """011-AC5: the API window is second-granular, the cut is not."""
    answer = await _history(board, B, before="2026-09-15T10:00:00.500Z")
    assert _ids(answer) == ["1"]
    epoch = 1789466400  # 2026-09-15T10:00:00Z
    assert api_calls[-1][3] == epoch + 1
    assert (await _history(board, B, before="2026-09-15T10:00:00.000Z"))["older"] is False  # nothing left


async def test_history_indexed_chat_without_messages_db_uses_the_api(
    tmp_path: Path, quiet_directory: Directory, api_calls: list[Any]
) -> None:
    """011-AC5: indexed but never backfilled is "no messages.db"."""
    board = W.Board({}, quiet_directory, data_dir=_archive(tmp_path / "d", T, None))
    assert (await _history(board, T))["source"] == "api"


async def test_history_unreadable_archive_falls_back_to_the_api(
    board: W.Board, data_dir: Path, api_calls: list[Any]
) -> None:
    """011-AC5: a corrupt messages.db does not kill the page."""
    chat = data_dir / "19:cccc@thread.tacv2"
    (chat / "messages.db").write_bytes(b"not a database" * 100)
    for leftover in chat.glob("messages.db-*"):
        leftover.unlink()
    assert (await _history(board, C))["source"] == "api"


async def test_history_with_the_archive_gone_uses_the_api(
    tmp_path: Path, quiet_directory: Directory, api_calls: list[Any]
) -> None:
    """011-AC5: no index at all: the board's chats still get their API page."""
    board = W.Board({B: {"id": B, "last_activity": _at(1)}}, quiet_directory, data_dir=tmp_path / "missing")
    assert (await _history(board, B))["source"] == "api"
    assert (await _history(board, T))["error"] == "unknown chat"


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

    monkeypatch.setattr(CV, "fetch_history", failing)
    assert await _history(board, B) == {"history": B, "before": None, "error": reason}


async def test_history_without_a_connection(tmp_path: Path) -> None:
    """011-AC5: nothing archived and no API session: an error, not a crash."""
    board = W.Board({B: {"id": B, "last_activity": _at(1)}})
    assert await _history(board, B) == {"history": B, "before": None, "error": "not connected"}


# --- history: an archive behind the board row ---


@pytest.fixture
def ahead(board: W.Board, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """T on the board with message 7, the archive ending at 5; the API holds 4 to 7. Its calls' sizes."""
    board.rows[T] = {"id": T, "last_activity": _at(7), "last_id": "7", "typing": []}
    calls: list[int] = []

    def fake_history(*args: Any) -> list[dict[str, Any]]:
        calls.append(args[3])
        return [_m(str(i), _at(i)) for i in (4, 5, 6, 7)]

    monkeypatch.setattr(CV, "fetch_history", fake_history)
    return calls


async def test_history_latest_page_merges_what_the_archive_lacks(board: W.Board, ahead: list[int]) -> None:
    """The archived tail, then the API's newer messages once each, oldest first, in one API call."""
    answer = await _history(board, T)
    assert (_ids(answer), answer["source"], "behind" in answer) == (
        ["1", "2", "3", "4", "5", "6", "7"],
        "archive",
        False,
    )
    assert answer["messages"][-1]["html"] == "<p>m7</p>"  # a record, like the archived ones
    assert ahead == [CV._CHAT_PAGE]


async def test_history_after_page_at_the_archive_end_merges_too(board: W.Board, ahead: list[int]) -> None:
    """A reconnect walks back down with `after`: its last page catches up like the latest one."""
    answer = await _history(board, T, after=_at(3))
    assert (_ids(answer), answer["newer"]) == (["4", "5", "6", "7"], False)
    assert (_ids(await _history(board, T, after=_at(5))), len(ahead)) == (["6", "7"], 2)


async def test_history_api_down_answers_the_archive_flagged_behind(
    board: W.Board, ahead: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline: the archive's page and the id it lags, not an error."""

    def failing(*args: Any) -> list[Any]:
        raise httpx.ConnectError("offline", request=httpx.Request("GET", "https://x"))

    monkeypatch.setattr(CV, "fetch_history", failing)
    answer = await _history(board, T)
    assert (_ids(answer), answer["behind"], "error" in answer) == (["1", "2", "3", "4", "5"], "7", False)


@pytest.mark.parametrize("last_id", ["5", "3", "", "x"])
async def test_history_up_to_date_archive_makes_no_api_call(
    board: W.Board, no_api: None, last_id: str
) -> None:
    """The board row no newer than the archive (or unknown): the archive alone, unflagged."""
    board.rows[T] = {"id": T, "last_activity": _at(5), "last_id": last_id, "typing": []}
    answer = await _history(board, T)
    assert (_ids(answer), "behind" in answer) == (["1", "2", "3", "4", "5"], False)


async def test_history_older_pages_ignore_the_board_row(board: W.Board, no_api: None) -> None:
    """Only the archive's tail can lag: a `before` or mid-archive `after` page reads it alone."""
    board.rows[T] = {"id": T, "last_activity": _at(7), "last_id": "7", "typing": []}
    assert _ids(await _history(board, T, before=_at(3))) == ["1", "2"]
    assert _ids(await _history(board, T, after=_at(1), limit=2)) == ["2", "3"]


# --- unknown thread (011-AC9) ---


async def test_unknown_thread_is_refused_without_an_api_call(board: W.Board, no_api: None) -> None:
    """011-AC9: neither on the board nor in the archive: history, follow and send are refused."""
    assert await _history(board, NOPE) == {"history": NOPE, "before": None, "error": "unknown chat"}
    assert await board.follow(None, NOPE) == {"follow": NOPE, "error": "unknown chat"}  # type: ignore[arg-type]
    assert await _send(board, NOPE, "hi") == "unknown chat"
    assert board._follows == {}


async def test_unknown_thread_without_any_archive(quiet_directory: Directory, no_api: None) -> None:
    """011-AC9: no --data-dir at all."""
    board = W.Board({}, quiet_directory)
    assert await _history(board, T) == {"history": T, "before": None, "error": "unknown chat"}


# --- message records ---


def test_record_fields() -> None:
    """011-AC4: the record shape the page relies on."""
    message = _m("7", _at(7), "<p>hi <b>you</b></p>", properties={"edittime": "1789466500000"})
    assert CV.chat_record(message, T, ME) == {
        "id": "7",
        "at": _at(7),
        "from": ANN,
        "name": "Ann Lee",
        "me": False,
        "type": "text",
        "editable": True,
        "html": "<p>hi <b>you</b></p>",
        "edited": True,
        "deleted": False,
        "edittime": "1789466500000",
        "reactions": [],
        "mentions": None,
        "cmid": "",
    }


def test_record_carries_its_clientmessageid() -> None:
    """017-AC5: the page matches Teams' echo to its queued bubble on the message's clientmessageid."""
    assert CV.chat_record(_m("7", _at(7), clientmessageid="1789466500123"), T, ME)["cmid"] == "1789466500123"


def test_record_deleted_message_has_no_body() -> None:
    """011-AC7: a deleted message keeps its place and loses its body."""
    for props in ({"deletetime": "1789466500000"}, {"hardDeleteTime": "1789466500000"}):
        record = CV.chat_record(_m("7", _at(7), "<p>secret</p>", properties=props), T, ME)
        assert (record["deleted"], record["html"]) == (True, "")
    assert CV.chat_record(_m("8", _at(8), skypeeditedid="8"), T)["edited"] is True


@pytest.mark.parametrize(
    ("key", "emoji"),
    [
        ("2705_whiteheavycheckmark", "✅"),
        ("1f680_rocket", "🚀"),
        ("fire", "🔥"),
        ("cry", "😢"),
        ("crossedfingers", "🤞"),
        ("launch", "🚀"),
        ("loudlycrying", "😭"),
        ("yes", "👍"),
        ("no", "👎"),
        ("yes-tone1", "👍🏻"),
        ("yes-tone2", "👍🏼"),
        ("yes-tone3", "👍🏽"),
        ("yes-tone4", "👍🏾"),
        ("yes-tone5", "👍🏿"),
        ("no-tone2", "👎🏼"),
        ("crossedfingers-tone3", "🤞🏽"),
        ("clappinghands-tone4", "👏🏾"),
        ("handsinair-tone5", "🙌🏿"),
        ("praying-tone1", "🙏🏻"),
        ("victory-tone2", "✌🏼"),
        ("1f919_callmehand-tone4", "🤙🏾"),
        ("270d_writinghand-tone3", "✍🏽"),
        ("1f469-200d-1f4bb_technologist-tone2", "👩🏼‍💻"),
        ("launch-tone1", ":launch-tone1:"),
        ("bartlett-tone2", "⚽"),  # Teams tones its footballer art, not the ball
        ("bartlett-tone6", ":bartlett-tone6:"),
        ("party_custom-tone1", ":party_custom-tone1:"),
        ("yes-tone0", ":yes-tone0:"),
        ("yes-tone6", ":yes-tone6:"),
        ("unknown👍-tone1", ":unknown👍-tone1:"),
        ("1f44d_thumbsup-tone1-tone2", ":1f44d_thumbsup-tone1-tone2:"),
        ("1f44d_thumbsup-tone6", ":1f44d_thumbsup-tone6:"),
        ("1f44d-1f3fd_thumbsup-tone1", ":1f44d-1f3fd_thumbsup-tone1:"),
        ("1f469-200d-1f467_family-tone1", ":1f469-200d-1f467_family-tone1:"),
        ("1f44d-1f3fd_thumbsup", "👍🏽"),
        ("1f469-200d-1f4bb_technologist", "👩‍💻"),
        ("110000_invalid", ":110000_invalid:"),
        ("d800_invalid", ":d800_invalid:"),
        ("party_custom", ":party_custom:"),
        ("raisedfist", "✊"),
        ("raisedfist-tone3", "✊🏽"),
        ("ok", "👌"),
        ("pointupindex", "☝️"),
        ("pointupindex-tone2", "☝🏼"),
        ("fistbump", "🤜🤛"),
        ("fistbump-tone4", "🤜🏾🤛🏾"),
        # Teams' own catalog (teams_emojis.py), keys seen in real chats.
        ("rock", "🤘"),
        ("smilingfacewithtear", "🥲"),
        ("meltingface", "🫠"),
        ("pinchedfingers", "🤌"),
        ("pinchedfingers-tone3", "🤌🏽"),
        ("dance", "🕺"),
        ("stareyes", "🤩"),
        ("heartonfire", "❤️\u200d🔥"),
        ("sad", "😢"),
        ("rocket;0-frc-d4-0123456789abcdef", ":rocket:"),
        ("heartyellow", "💛"),
        ("hearteyes", "😍"),
        ("seenoevil", "🙈"),
        ("upsidedownface", "🙃"),
        ("1f3f4-e0067-e0062-e0065-e006e-e0067-e007f_england", "🏴󠁧󠁢󠁥󠁮󠁧󠁿"),
        ("202e_x", ":202e_x:"),
        ("200f_x", ":200f_x:"),
        ("2028_x", ":2028_x:"),
        ("0007_bell", ":0007_bell:"),
        ("e0000_x", ":e0000_x:"),
        ("partyparrot;0-frs-d1-0123456789abcdef", ":partyparrot:"),
    ],
)
def test_record_decodes_unicode_reaction_keys(key: str, emoji: str) -> None:
    emotions = [{"key": key, "users": [{"mri": ME}]}]
    record = CV.chat_record(_m("7", _at(7), properties={"emotions": emotions}), T, ME)
    assert record["reactions"] == [{"key": key, "emoji": emoji, "count": 1, "mine": True, "people": [ME]}]


def test_record_points_a_known_custom_reaction_at_its_image() -> None:
    """A custom emoji has no character: its image when the emoji cache serves it, else the page names it."""
    emotions = [
        {"key": f"kekwait;{OBJ}", "users": [{"mri": ME}]},
        {"key": "partyparrot;x9", "users": [{"mri": ME}]},
    ]
    record = CV.chat_record(
        _m("7", _at(7), properties={"emotions": emotions}), T, ME, custom=frozenset({OBJ})
    )
    assert [(r["emoji"], r.get("src")) for r in record["reactions"]] == [
        (":kekwait:", f"/emoji/{OBJ}"),
        (":partyparrot:", None),
    ]
    # The key's tail is not the image id: the emoji cache maps the key to it.
    emotions = [{"key": "kekwait;0-weu-d1-tail", "users": [{"mri": ME}]}]
    message = _m("7", _at(7), properties={"emotions": emotions})
    images = {"kekwait;0-weu-d1-tail": OBJ}
    record = CV.chat_record(message, T, ME, custom=frozenset({OBJ}), images=images)
    assert record["reactions"][0]["src"] == f"/emoji/{OBJ}"
    assert "src" not in CV.chat_record(message, T, ME, custom=frozenset({OBJ}))["reactions"][0]


def test_emoji_cache_maps_reaction_keys_to_image_ids() -> None:
    cache = EmojiCache(None)
    cache.names = {"kekwait": OBJ, "gone": "0-weu-d1-gone"}
    cache.keys = {"kekwait": "kekwait;0-weu-d1-tail", "nameless": "nameless;0-weu-d1-x"}
    assert cache.images() == {"kekwait;0-weu-d1-tail": OBJ}


_SENDABLE = sorted({_reaction_base(name) for name in [*EMOJI, *TEAMS_EMOJIS]})


def test_a_character_with_themed_variants_goes_as_its_plain_name() -> None:
    """⚽ is not `bartlett` (a footballer), 🙁 not `saddog`, ♻ not `windturbine`."""
    assert [reaction_key(codes) for codes in ("26bd", "1f641", "267b-fe0f")] == [
        "soccerball",
        "1f641_slightlyfrowningface",
        "recycle",
    ]


@pytest.mark.parametrize("char", _SENDABLE, ids=ascii)
def test_a_sent_reaction_shows_the_character_picked(char: str) -> None:
    """What the page sends for a character (its code points) comes back as that character, so the
    picker finds the chip again."""
    key = reaction_key("-".join(f"{ord(c):04x}" for c in char))
    if char == "\U0001facd":  # 🫍 is newer than Python's Unicode: refused like any unassigned point
        assert key is None
        return
    assert key is not None
    assert reaction_key(key) == key  # a chip of it can be clicked again
    assert reaction_emoji(key).replace("\ufe0f", "") == char.replace("\ufe0f", "")


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
        {"key": "like", "emoji": "👍", "count": 2, "mine": True, "people": [ANN, ME]},
        {"key": "heart", "emoji": "❤️", "count": 1, "mine": False, "people": [ANN]},
        {"key": "party_custom", "emoji": ":party_custom:", "count": 1, "mine": False, "people": [ANN]},
    ]
    for value in (emotions, json.dumps(emotions)):
        assert CV.chat_record(_m("7", _at(7), properties={"emotions": value}), T, ME)["reactions"] == expected
    assert (
        CV.chat_record(_m("7", _at(7), properties={"emotions": emotions}), T)["reactions"][0]["mine"] is False
    )


def test_record_mentions_and_me() -> None:
    """011-AC6: mention kind (spec 003) and the mention span for my MRI."""
    content = '<p><span itemtype="http://schema.skype.com/Mention" itemscope itemid="0">Me</span> look</p>'
    props = {"mentions": json.dumps([{"mentionType": "person", "itemid": 0, "mri": ME}])}
    record = CV.chat_record(_m("7", _at(7), content, properties=props), T, ME)
    assert record["mentions"] == "me"
    assert record["html"] == f'<p><span class="mention mention-me" data-mri="{ME}">Me</span> look</p>'


@pytest.mark.parametrize(
    ("thread", "mri", "known", "title"),
    [
        (T, ANN, {ANN: "Ann Lee"}, ' title="Ann Lee"'),
        (T, ME, {ME: "Me Self"}, ' title="Me Self"'),
        (T, ANN, {}, ""),  # unknown: no title, never the MRI's tail
        (T, ANN, {ANN: ANN.rsplit(":", 1)[-1]}, ""),  # the directory's negative cache
        (T, T, {T: "Everyone"}, ""),  # everyone: nothing extra
        (MEETING, MEETING, {MEETING: "Weekly sync"}, ""),
        (T, ANN, {ANN: 'A "<b>" & B'}, ' title="A &quot;&lt;b&gt;&quot; &amp; B"'),
    ],
)
def test_a_mention_shows_its_persons_full_name_on_hover(
    thread: str, mri: str, known: dict[str, str], title: str
) -> None:
    """033-AC9: a mention span carries its person's full name as `title`, from the directory (which holds the
    chats' roster names); unknown, or everyone, no title."""
    directory = Directory.__new__(Directory)
    directory._names = dict(known)
    kind = "everyone" if mri == thread else "person"
    content = '<p><span itemtype="http://schema.skype.com/Mention" itemscope itemid="0">Ann</span> look</p>'
    props = {"mentions": json.dumps([{"mentionType": kind, "itemid": 0, "mri": mri}])}
    record = CV.chat_record(
        _m("7", _at(7), content, properties=props),
        thread,
        ME,
        name_for=directory.name_for,
        known_name=directory.known_name,
    )
    cls = "mention mention-me" if mri == ME else "mention"
    assert record["html"] == f'<p><span class="{cls}" data-mri="{mri}"{title}>Ann</span> look</p>'


def test_record_resolves_reaction_people_without_losing_unknown_users() -> None:
    emotions = [{"key": "launch", "users": [{"mri": ANN}, {"mri": ME}, {"mri": "unknown"}]}]
    names = {ANN: "Ann Lee", ME: "Me Self"}
    record = CV.chat_record(
        _m("7", _at(7), properties={"emotions": emotions}), T, ME, name_for=lambda mri: names.get(mri, mri)
    )
    assert record["reactions"][0]["people"] == ["Ann Lee", "Me Self", "unknown"]
    mine = CV.chat_record(_m("8", _at(8), **{"from": f"https://h/v1/users/ME/contacts/{ME}"}), T, ME)
    assert (mine["me"], mine["from"], mine["mentions"]) == (True, ME, None)


async def test_history_names_reaction_people(
    tmp_path: Path, quiet_directory: Directory, no_api: None
) -> None:
    """013: history pages resolve reaction MRIs through the directory."""
    emotions = [{"key": "like", "users": [{"mri": ANN}]}]
    board = W.Board(
        {},
        quiet_directory,
        data_dir=_archive(tmp_path / "d", T, [_m("1", _at(1), properties={"emotions": emotions})]),
    )
    quiet_directory._names[ANN] = "Ann Lee"
    [message] = (await _history(board, T))["messages"]
    assert message["reactions"][0]["people"] == ["Ann Lee"]


def test_a_blank_roster_name_gives_its_pick_no_form() -> None:
    """A whitespace-only display name is no name: its pick reads nothing at its `@`, which then resolves
    as unpicked (refused here), not an IndexError."""
    [anchored] = CV._anchored_picks(T, {ANN: "  "}, [{"name": "x", "mri": ANN, "at": 0}], [None])
    assert anchored["forms"] == []
    with pytest.raises(ValueError, match="unknown mention"):
        compose.resolve_mentions("@x", [{"mri": ANN, "name": "  "}], T, ME, picks=[anchored])


_FILE_CARD = (
    '<span class="file"><span class="file-icon">📕</span> <span class="file-name">a.pdf</span></span>'
)


@pytest.mark.parametrize(
    ("msgtype", "content", "props", "kind", "body"),
    [
        ("Text", "a < b & <i>c</i>", {}, "text", "a &lt; b &amp; &lt;i&gt;c&lt;/i&gt;"),
        ("Event/Call", "<ended/>", {}, "call", "📞 call ended"),
        ("ThreadActivity/AddMember", "<addmember/>", {}, "system", "👥 member added"),
        ("RichText/Media_Card", "<x/>", {}, "other", "🃏 card"),
        ("RichText/Media_Card", "", {"files": [{"fileName": "a.pdf"}]}, "text", _FILE_CARD),
    ],
)
def test_record_types_and_markers(
    msgtype: str, content: str, props: dict[str, Any], kind: str, body: str
) -> None:
    """011-AC4: non-text messages get the widget's marker, escaped."""
    record = CV.chat_record(
        {"id": "1", "composetime": _at(1), "messagetype": msgtype, "content": content, "properties": props}, T
    )
    assert (record["type"], record["html"]) == (kind, body)


@pytest.mark.parametrize("as_string", [True, False])
def test_record_shows_file_cards_after_the_text(as_string: bool) -> None:
    """027-AC1: a message's file entries render as cards after its text, the text itself unchanged; a
    file-only message shows its cards instead of the 📎 marker; a deleted one shows none."""
    files = [{"fileName": "a.pdf"}]
    props = {"files": json.dumps(files) if as_string else files}
    assert (
        CV.chat_record(_m("1", _at(1), "<p>see</p>", properties=props), T)["html"]
        == "<p>see</p>" + _FILE_CARD
    )
    only = CV.chat_record({**_m("1", _at(1)), "content": "", "properties": props}, T)
    assert only["html"] == _FILE_CARD
    gone = CV.chat_record(_m("1", _at(1), properties={**props, "deletetime": "1789466500000"}), T)
    assert "file" not in gone["html"]
    assert CV.chat_record(_m("1", _at(1), properties={"files": "[]"}), T)["html"] == "<p>m1</p>"


@pytest.mark.parametrize(
    ("msgtype", "content"),
    [("Event/Call", "<ended/>"), ("ThreadActivity/AddMember", "<addmember/>"), ("ThreadActivity/X", "<x/>")],
)
def test_call_and_activity_records_show_no_file_cards(msgtype: str, content: str) -> None:
    """027-AC1: file cards belong to messages; a call or a system line carrying `files` shows none."""
    message = {
        **_m("1", _at(1), content),
        "messagetype": msgtype,
        "properties": {"files": [{"fileName": "a.pdf"}]},
    }
    assert "file" not in CV.chat_record(message, T)["html"]


def test_record_names_a_pasted_file_link_once() -> None:
    """027-AC6: a reference entry the body links renames that link, no second one; unlinked, it adds one."""
    url = "https://example.sharepoint.com/:x:/s/team/EbUdGeT"
    props = {"files": [{"fileName": "budget.xlsx", "state": "reference", "fileInfo": {"shareUrl": url}}]}
    linked = CV.chat_record(_m("1", _at(1), f'<p><a href="{url}?e=x">{url}</a></p>', properties=props), T)
    assert linked["html"] == f'<p><a href="{url}?e=x" rel="noopener" target="_blank">budget.xlsx</a></p>'
    alone = CV.chat_record(_m("1", _at(1), "<p>see</p>", properties=props), T)
    assert alone["html"] == f'<p>see</p><p><a href="{url}" rel="noopener" target="_blank">budget.xlsx</a></p>'


_SHARE = "https://example-my.sharepoint.com/:w:/g/personal/someone_example_com/EaBcDeF"
_LOCAL = f"/media/{quote(T, safe='')}/"


_OBJ = "https://example-my.sharepoint.com/personal/someone_example_com/Documents/plan%20v2.docx"


def _card_href(media: Path | None, entry: dict[str, Any], saved: dict[str, str] | None = None) -> str | None:
    message = _m("1", _at(1), "<p>x</p>", properties={"files": [entry]})
    html = CV.chat_record(message, T, media_dir=media, saved=saved)["html"]
    return html.split('href="')[1].split('"')[0] if 'href="' in html else None


def test_record_card_opens_the_archived_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """027-AC5, AC7: a card opens the copy the archive saved for its own URL through /media, in a new tab;
    without one, or with a copy /media refuses, the hotlink; a file of the same name never stands in."""
    media = tmp_path / "media"
    media.mkdir()
    (media / "plan v2.docx").write_bytes(b"doc")
    (media / "plan v2 (2).docx").write_bytes(b"other doc")
    (media / "big.pdf").write_bytes(b"x" * 11)
    (media / "dir.pdf").mkdir()
    (media / "half.pdf.part").write_bytes(b"x")
    (tmp_path / "outside.pdf").write_bytes(b"x")
    (media / "out.pdf").symlink_to(tmp_path / "outside.pdf")  # a link out of the folder: /media refuses it
    linked = {"fileName": "plan v2.docx", "fileInfo": {"shareUrl": _SHARE}}
    local = f"{_LOCAL}plan%20v2.docx"
    saved = {_SHARE: "plan v2.docx"}
    html = CV.chat_record(_m("1", _at(1), properties={"files": [linked]}), T, media_dir=media, saved=saved)[
        "html"
    ]
    assert f'<a class="file" href="{local}" rel="noopener" target="_blank">' in html
    assert (
        _card_href(media, {"fileName": "x", "objectUrl": _OBJ}, {_OBJ: "plan v2.docx"}) == local
    )  # objectUrl
    # Two files named alike: each card its own copy, whatever the entry's name says.
    other = {"fileName": "plan v2.docx", "fileInfo": {"shareUrl": f"{_SHARE}2"}}
    both = {_SHARE: "plan v2.docx", f"{_SHARE}2": "plan v2 (2).docx"}
    assert _card_href(media, other, both) == f"{_LOCAL}plan%20v2%20%282%29.docx"
    assert _card_href(media, linked, both) == local
    # A name on disk without a record for the URL: the hotlink (an older copy, or another file).
    assert _card_href(media, linked) == _SHARE
    assert _card_href(media, other, saved) == f"{_SHARE}2"
    assert _card_href(media, {"fileName": "plan v2.docx"}, saved) is None  # no URL: no link at all
    for name in ("absent.docx", "dir.pdf", "half.pdf.part", "out.pdf", "a\x00b", "", "big.pdf", "../media/x"):
        monkeypatch.setattr(CV, "_MEDIA_MAX", 10)  # big.pdf: over what /media serves
        assert _card_href(media, linked, {_SHARE: name}) == _SHARE, name
    assert _card_href(None, linked, saved) == _SHARE  # no archive folder
    ref = {**linked, "state": "reference"}  # never downloaded: its link stays the shared one
    assert _card_href(media, ref, saved) == _SHARE


async def test_history_cards_open_their_own_copies(
    board: W.Board, data_dir: Path, no_api: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """027-AC7: a history page finds each card's copy by its URL, two files of one name each their own, in one
    read-only query for the page."""
    two = f"{_SHARE}2"
    store = ChatStore(data_dir, T)
    store.insert_page(
        [
            _m("8", _at(8), properties={"files": [{"fileName": "a.pdf", "fileInfo": {"shareUrl": _SHARE}}]}),
            _m("9", _at(9), properties={"files": json.dumps([{"fileName": "a.pdf", "objectUrl": two}])}),
        ]
    )
    (store.media_dir / "a.pdf").write_bytes(b"1")
    (store.media_dir / "a (2).pdf").write_bytes(b"2")
    store.save_file([_SHARE], "a.pdf", "")
    store.save_file([two], "a (2).pdf", "")
    store.close()
    calls: list[list[str]] = []
    real = CV.saved_files
    monkeypatch.setattr(CV, "saved_files", lambda chat_dir, urls: calls.append(urls) or real(chat_dir, urls))
    answer = await _history(board, T, limit=2)
    hrefs = [m["html"].split('href="')[1].split('"')[0] for m in answer["messages"]]
    assert hrefs == [f"{_LOCAL}a.pdf", f"{_LOCAL}a%20%282%29.pdf"]
    assert calls == [[_SHARE, two]]
    calls.clear()
    await _history(board, T, before=_at(8), limit=2)  # a page without files: no query
    assert calls == []


async def test_history_of_an_archive_older_than_the_record(
    board: W.Board, data_dir: Path, no_api: None
) -> None:
    """027-AC7: an archive not yet opened by phase 3 has no table: the card takes the hotlink, and the
    read-only lookup never creates the table."""
    store = ChatStore(data_dir, T)
    store.insert_page(
        [_m("9", _at(9), properties={"files": [{"fileName": "a.pdf", "fileInfo": {"shareUrl": _SHARE}}]})]
    )
    (store.media_dir / "a.pdf").write_bytes(b"1")
    store._db.execute("DROP TABLE sp_files")
    store._db.commit()
    store.close()
    answer = await _history(board, T, limit=1)
    assert f'href="{_SHARE}"' in answer["messages"][0]["html"]
    db = sqlite3.connect(store.dir / "messages.db")
    try:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name = 'sp_files'").fetchone() is None
    finally:
        db.close()


def test_page_files_survives_an_unreadable_archive(tmp_path: Path) -> None:
    """027-AC7: no messages.db, or a broken one, costs the cards their local copy only."""
    files = {"files": [{"fileName": "a.pdf", "fileInfo": {"shareUrl": _SHARE}}]}
    message = _m("1", _at(1), properties=files)
    assert CV.page_files(tmp_path / "media", [message]) == {}
    (tmp_path / "messages.db").write_bytes(b"not a database")
    assert CV.page_files(tmp_path / "media", [message]) == {}
    assert CV.page_files(None, [message]) == {}


_BOB = "8:orgid:00000000-0000-0000-0000-00000000000c"
_CARL = "8:orgid:00000000-0000-0000-0000-00000000000d"
_ACTS = "19:acts@thread.v2"  # archived by the test that reads it


def _activity(msg_id: str, msgtype: str, content: str) -> dict[str, Any]:
    return {**_m(msg_id, _at(int(msg_id)), content), "messagetype": msgtype, "from": "", "imdisplayname": ""}


def test_activity_records_say_who_did_what_as_text() -> None:
    """A system line names the people (directory names, "You" for us) and stays text: no markup passes."""
    names = {ANN: "Ann Lee", _BOB: "Bob <b>Roe</b>"}.get
    added = _activity(
        "1",
        "ThreadActivity/AddMember",
        f"<addmember><initiator>{ME}</initiator><target>{ANN}</target><target>{_BOB}</target></addmember>",
    )
    record = CV.chat_record(added, T, ME, name_for=lambda m: names(m) or "?")
    assert (record["type"], record["html"]) == ("system", "👥 You added Ann Lee, Bob &lt;b&gt;Roe&lt;/b&gt;")
    topic = (
        "<topicupdate><initiator>{}</initiator><value>&lt;script&gt;x&lt;/script&gt;</value></topicupdate>"
    )
    record = CV.chat_record(_activity("2", "ThreadActivity/TopicUpdate", topic.format(ANN)), T, ME)
    assert record["html"] == "✎ someone renamed the chat to «&lt;script&gt;x&lt;/script&gt;»"


async def test_history_names_activity_people_once(
    board: W.Board, data_dir: Path, quiet_directory: Directory, monkeypatch: pytest.MonkeyPatch, no_api: None
) -> None:
    """Unknown MRIs of a page's system lines are resolved before the answer; cached ones ask nothing."""
    removed = f"<deletemember><initiator>{_BOB}</initiator><target>{_CARL}</target></deletemember>"
    left = f"<deletemember><initiator>{_CARL}</initiator><target>{_CARL}</target></deletemember>"
    gone = "ThreadActivity/DeleteMember"
    _archive(data_dir, _ACTS, [_activity("1", gone, removed), _m("2", _at(2)), _activity("3", gone, left)])
    asked: list[Any] = []

    async def post(self: httpx.AsyncClient, url: str, **kw: Any) -> httpx.Response:
        asked.append(kw["json"])
        users = [{"mri": _BOB, "displayName": "Bob Roe"}, {"mri": _CARL, "displayName": "Carl Day"}]
        return httpx.Response(200, json={"value": users}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    quiet_directory.bearer = "b"
    for _ in range(2):
        answer = await _history(board, _ACTS)
        lines = [m["html"] for m in answer["messages"] if m["type"] == "system"]
        assert lines == ["👥 Bob Roe removed Carl Day", "👥 Carl Day left"]
        assert all("_activity" not in m for m in answer["messages"])
    assert asked == [[_BOB, _CARL]]


def _card(*body: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"type": "AdaptiveCard", "body": list(body), **extra}


_BUILD_CARD = _card(
    {"type": "TextBlock", "text": "Build failed", "weight": "Bolder", "size": "Large"},
    {"type": "FactSet", "facts": [{"title": "Branch", "value": "main"}]},
    {"type": "Image", "url": f"https://eu-api.asm.skype.com/v1/objects/{OBJ}/views/imgo", "altText": "shot"},
    {"type": "Image", "url": "https://cdn.example.org/logo.png", "altText": "logo"},
    actions=[{"type": "Action.OpenUrl", "title": "Open", "url": "https://ci.example.org/1"}],
)


def _swift_message(payload: dict[str, Any]) -> dict[str, Any]:
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    content = f'<URIObject type="SWIFT.1"><Swift b64="{b64}"></Swift></URIObject>'
    return {"id": "1", "composetime": _at(1), "messagetype": "RichText/Media_Card", "content": content}


def test_record_renders_a_media_card(tmp_path: Path) -> None:
    """011-AC24: a Swift card renders as card HTML, typed text; images only from the archive."""
    (tmp_path / f"{OBJ}.png").write_bytes(b"png")
    attachment = {"contentType": "application/vnd.microsoft.card.adaptive", "content": _BUILD_CARD}
    record = CV.chat_record(_swift_message({"attachments": [attachment]}), T, ME, tmp_path)
    assert record["type"] == "text"
    out = record["html"]
    assert (
        out.startswith('<div class="card">') and '<div class="tb w-bolder s-large">Build failed</div>' in out
    )
    assert '<table class="facts"><tr><th>Branch</th><td>main</td></tr></table>' in out
    assert f'<img src="/media/19%3Aaaaa%40thread.v2/{OBJ}.png" alt="shot">' in out
    assert "<p>logo</p>" in out and "cdn.example.org" not in out
    assert 'href="https://ci.example.org/1"' in out


def test_record_appends_properties_cards_after_the_sanitised_body() -> None:
    """011-AC24: `properties.cards` (a JSON string) follows the body, its classes intact."""
    cards = [{"appName": "CI", "content": _BUILD_CARD}]
    message = _m("7", _at(7), '<p onclick="x()">see below</p>', properties={"cards": json.dumps(cards)})
    record = CV.chat_record(message, T, ME)
    assert record["type"] == "text"
    assert record["html"].startswith('<p>see below</p><div class="card"><div class="card-app">CI</div>')
    assert '<table class="facts">' in record["html"] and "onclick" not in record["html"]


def test_record_card_type_only_upgrades_other() -> None:
    """011-AC24: a card makes `other` a text record, never editable; a system message stays system."""
    cards = {"cards": [{"content": _BUILD_CARD}]}
    mine = {"from": f"https://h/v1/users/ME/contacts/{ME}"}
    attachment = {"contentType": "application/vnd.microsoft.card.adaptive", "content": _BUILD_CARD}
    record = CV.chat_record({**_swift_message({"attachments": [attachment]}), **mine}, T, ME)
    assert (record["type"], record["editable"], record["me"]) == ("text", False, True)
    added = {**_m("8", _at(8)), "messagetype": "ThreadActivity/AddMember", "properties": cards}
    record = CV.chat_record(added, T, ME)
    assert (record["type"], record["editable"]) == ("system", False)
    assert '<div class="card">' in record["html"]


@pytest.mark.parametrize(
    "message",
    [
        _swift_message({"attachments": [{"content": _card({"type": "Unknown"})}]}),
        {**_m("7", _at(7)), "content": "", "properties": {"cards": [{"content": _card()}]}},
    ],
)
def test_record_with_nothing_to_render_keeps_the_marker(message: dict[str, Any]) -> None:
    """011-AC24: an empty body and cards that render nothing fall back to the card marker."""
    record = CV.chat_record(message, T, ME)
    assert record["html"] == "🃏 card"


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
        for _ in range(500):
            if task.done():
                task.result()  # the bind failed (port taken since _free_port): say why
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(0.01)
                continue
            # A real request, not a bare connect: the server logs an aborted handshake as an error.
            cookie = f"Cookie: mt={board.secret}\r\n"
            writer.write(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{cookie}\r\n".encode())
            status = b""
            with contextlib.suppress(OSError, TimeoutError):
                status = await asyncio.wait_for(reader.readline(), 1)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            if task.done():
                task.result()  # a foreign listener on that port answered while our bind failed
            if status.startswith(b"HTTP/1.1 200 "):
                break
        else:
            raise TimeoutError(f"board not listening on {port}")
        yield f"127.0.0.1:{port}"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _host(board: W.Board) -> str:
    return board.base_url.removeprefix("http://")


def _cookie(board: W.Board) -> dict[str, str]:
    return cookie_header(board.secret)


def _connect(board: W.Board) -> Any:
    return board_socket(_host(board), board.secret)


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
    async with _served(board, settings), _connect(board) as ws:
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
        assert board.directory is not None
        board.directory._names[ME] = "Me Self"
        reaction = {"id": "9", "composetime": _at(9), "messagetype": "RichText/Html", "properties": emotions}
        await board.on_event(_event("MessageUpdate", T, **reaction))
        edit = (await _frame(ws))["edit"]
        assert edit["reactions"] == [
            {"key": "like", "emoji": "👍", "count": 1, "mine": True, "people": ["Me Self"]}
        ]
        assert "html" not in edit  # partial update: the page keeps the body it shows

        await board.on_event(_event("NewMessage", T, messagetype="Control/Typing"))
        assert await _frame(ws) == {"typing": "Ann Lee", "on": True, "thread": T}
        await board.on_event(_event("NewMessage", T, messagetype="Control/ClearTyping"))
        assert await _frame(ws) == {"typing": "Ann Lee", "on": False, "thread": T}


async def test_follow_relays_only_its_thread_and_skips_noise(board: W.Board, settings: Settings) -> None:
    """011-AC7: other threads, my own typing, read markers and conversation updates are not relayed."""
    async with _served(board, settings), _connect(board) as ws:
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
    async with _served(board, settings):
        async with _connect(board) as ws:
            await ws.recv()
            await ws.send(json.dumps({"follow": T}))
            await _frame(ws)
            await ws.send(json.dumps({"follow": B}))
            await _frame(ws)
            assert [tid for tid, _ in board._follows.values()] == [B]
        await wait(lambda: not board._follows, "the follow dropped")


async def test_history_and_errors_go_to_the_asking_socket_only(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC4 + 011-AC5 + 011-AC9: answers reach the asker only; after an error the socket still works."""

    def failing(*args: Any) -> list[Any]:
        raise httpx.ConnectError("boom", request=httpx.Request("GET", "https://x"))

    monkeypatch.setattr(CV, "fetch_history", failing)
    async with _served(board, settings), _connect(board) as asker, _connect(board) as other:
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
    async with _served(board, settings), _connect(board) as ws, _connect(board) as idle:
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
    answer = await _history(board, T)
    assert (_ids(answer), answer["older"]) == (["1", "4", "5"], False)


async def test_any_history_failure_still_answers(board: W.Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """011-AC5: an unexpected failure is an error answer, not a dead task."""

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("boom")

    monkeypatch.setattr(CV, "read_page", broken)
    verb = {"history": T}
    with structlog.testing.capture_logs() as logs:
        got = await _answered(CV._head_history(verb), board.chat.history_answer(verb))
    assert got == {"history": T, "before": None, "error": "failed"}
    assert [(e["event"], e["verb"], e["error_type"]) for e in logs] == [
        ("verb_failed", "history", "ValueError")
    ]
    monkeypatch.setattr(CV, "chat_record", broken)
    monkeypatch.setattr(CV, "read_page", lambda *a, **k: Page([_m("1", _at(1))], False, False))
    assert (await _history(board, T))["messages"] == []  # the bad record is skipped, the page answers


def test_record_survives_malformed_properties() -> None:
    """011-AC7: odd shapes from the stream or the archive never raise."""
    for props in ("oops", ["x"], {"emotions": [{"key": "like", "users": 5}], "mentions": 3}):
        record = CV.chat_record(_m("1", _at(1), properties=props), T, ME)
        assert (record["reactions"], record["html"]) == ([], "<p>m1</p>")


async def test_records_are_built_off_the_event_loop(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch, api_calls: list[Any]
) -> None:
    """011-AC4 + 011-AC7: sanitising globs the (NFS) media folder: never on the loop thread."""
    loop_thread = threading.get_ident()
    threads: list[int] = []
    real = CV.chat_record

    def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        threads.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(CV, "chat_record", spy)  # pages
    monkeypatch.setattr(W, "chat_record", spy)  # live relay
    await _history(board, T)
    await _history(board, B)  # API path
    async with _served(board, settings), _connect(board) as ws:
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

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise TypeError("odd event")

    monkeypatch.setattr(W, "chat_record", broken)
    async with _served(board, settings), _connect(board) as ws:
        await ws.recv()
        await ws.send(json.dumps({"follow": B}))
        await _frame(ws)
        await board.on_event(
            _event("NewMessage", B, id="9", composetime=_at(9), messagetype="Text", content="x")
        )
        assert board.rows[B]["last_id"] == "9"
        await board.on_event(_event("NewMessage", B, messagetype="Control/Typing"))
        assert await _frame(ws) == {"typing": "Ann Lee", "on": True, "thread": B}


async def test_typing_relay_names_who_stopped_and_expires(
    quiet_directory: Directory, data_dir: Path, settings: Settings
) -> None:
    """011-AC7: clear frames name the typer (groups), expire without ClearTyping, end on their message."""
    board = W.Board({}, quiet_directory, me=ME, data_dir=data_dir, typing_ttl=0.2)
    bob = {"from": "https://h/v1/users/ME/contacts/8:orgid:bob", "imdisplayname": "Bob Day"}
    async with _served(board, settings), _connect(board) as ws:
        await ws.recv()
        await ws.send(json.dumps({"follow": C}))  # not on the board: the relay owns the timer
        await _frame(ws)
        await board.on_event(_event("NewMessage", C, messagetype="Control/Typing"))
        await board.on_event(_event("NewMessage", C, messagetype="Control/Typing", **bob))
        assert await _frame(ws) == {"typing": "Ann Lee", "on": True, "thread": C}
        assert await _frame(ws) == {"typing": "Bob Day", "on": True, "thread": C}
        await board.on_event(_event("NewMessage", C, messagetype="Control/ClearTyping", **bob))
        assert await _frame(ws) == {"typing": "Bob Day", "on": False, "thread": C}
        assert await _frame(ws) == {
            "typing": "Ann Lee",
            "on": False,
            "thread": C,
        }  # Ann never cleared: expiry
        await board.on_event(_event("NewMessage", C, messagetype="Control/Typing"))
        assert await _frame(ws) == {"typing": "Ann Lee", "on": True, "thread": C}
        await board.on_event(
            _event("NewMessage", C, id="9", composetime=_at(9), messagetype="Text", content="x")
        )
        assert await _frame(ws) == {"typing": "Ann Lee", "on": False, "thread": C}
        assert (await _frame(ws))["msg"]["id"] == "9"
        await _silent(ws, 0.4)  # the expiry was cancelled: no second clear


@pytest.mark.parametrize("before", ["garbage", "2026-09-15", "15/09/2026 10:00:00", "x2026-09-15T10:00:00Z"])
async def test_history_refuses_a_malformed_cursor(board: W.Board, no_api: None, before: str) -> None:
    """011-AC4 + 011-AC5: a cursor that is not a time is refused before any read or API call."""
    for thread in (T, B):
        assert await _history(board, thread, before=before) == {
            "history": thread,
            "before": before,
            "error": "invalid before",
        }


async def test_history_echoes_its_cursor(board: W.Board, api_calls: list[Any]) -> None:
    """011-AC4 + 011-AC5: the page matches each answer to its request by `before`."""
    assert (await _history(board, T, before=_at(3)))["before"] == _at(3)
    assert (await _history(board, B, before=_at(3)))["before"] == _at(3)
    assert (await _history(board, T))["before"] is None


async def test_one_history_in_flight_per_socket(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC4: a second request while one is pending is refused `busy`; other sockets are not held up."""
    real = CV.read_page

    def slow(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.4)
        return real(*args, **kwargs)

    monkeypatch.setattr(CV, "read_page", slow)
    async with _served(board, settings), _connect(board) as one, _connect(board) as two:
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
    answer = await _history(board, T, before="", limit=2)
    assert (_ids(answer), answer["before"]) == (["4", "5"], None)


async def test_own_typing_is_never_relayed(board: W.Board, settings: Settings) -> None:
    """011-AC7: my typing on another device, started or cleared, is not shown to the page."""
    mine = {"from": f"https://h/v1/users/ME/contacts/{ME}", "imdisplayname": "Me Myself"}
    async with _served(board, settings), _connect(board) as ws:
        await ws.recv()
        await ws.send(json.dumps({"follow": B}))
        await _frame(ws)
        await board.on_event(_event("NewMessage", B, messagetype="Control/Typing", **mine))
        await board.on_event(_event("NewMessage", B, messagetype="Control/ClearTyping", **mine))
        await _silent(ws)


async def test_follow_answers_who_already_types(
    quiet_directory: Directory, data_dir: Path, settings: Settings
) -> None:
    """011-AC7: a page following mid-typing gets the typers the row shows, and their expiry."""
    rows = {B: {"id": B, "label": "B", "last_activity": _at(1), "last_id": "1", "typing": []}}
    board = W.Board(rows, quiet_directory, me=ME, data_dir=data_dir, typing_ttl=0.5)
    mine = {"from": f"https://h/v1/users/ME/contacts/{ME}", "imdisplayname": "Me Myself"}
    async with _served(board, settings), _connect(board) as ws:
        await ws.recv()
        await board.on_event(_event("NewMessage", B, messagetype="Control/Typing"))
        await board.on_event(_event("NewMessage", B, messagetype="Control/Typing", **mine))
        assert "Ann Lee" in board.rows[B]["typing"]
        await ws.send(json.dumps({"follow": T}))
        assert (await _frame(ws))["typing"] == []  # another chat's typers stay there
        await ws.send(json.dumps({"follow": B}))
        assert (await _frame(ws))["typing"] == ["Ann Lee"]  # my own typing is not news to the page
        assert await _frame(ws) == {"typing": "Ann Lee", "on": False, "thread": B}  # TTL, no ClearTyping
        assert board.rows[B]["typing"] == []


async def test_typing_expiry_off_the_board_does_not_rebroadcast(
    quiet_directory: Directory, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC7: a followed thread off the board expiring its typing leaves the board alone."""
    board = W.Board({}, quiet_directory, me=ME, data_dir=data_dir, typing_ttl=0.05)
    sent: list[str] = []
    monkeypatch.setattr(board, "broadcast", lambda: sent.append("board"))
    board._typing_arm(C, ANN, "Ann Lee")
    await asyncio.sleep(0.15)
    assert (C, ANN) not in board._typing_timers
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
    async with _served(board, settings) as host, httpx.AsyncClient(headers=_cookie(board)) as client:
        for thread_id in (T, B, C):
            page = await client.get(_chat_url(host, thread_id))
            assert page.status_code == 200, thread_id
            assert page.headers["content-type"].startswith("text/html")
            csp = page.headers["content-security-policy"]
            script = page.text.split("<script>", 1)[1].split("</script>", 1)[0]
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            assert f"script-src 'sha256-{digest}';" in csp
            # blob: only for the pasted images' thumbnails (spec 012 phase 5b).
            # Giphy's own copy is the page's last resort (031-AC4); no other remote image source (031-AC5).
            assert [d for d in csp.split("; ") if d.startswith("img-src")] == [
                "img-src 'self' blob: https://*.giphy.com"
            ]
            assert "unsafe-inline" not in csp.split("script-src", 1)[1].split(";", 1)[0]
            assert "/ws?chat" in page.text
        widget = await client.get(f"http://{host}/")
        # No page tells another site which chat it shows (a Giphy fallback, a link opened).
        assert widget.headers["referrer-policy"] == page.headers["referrer-policy"] == "no-referrer"
        board_csp = widget.headers["content-security-policy"]
        assert [d for d in board_csp.split("; ") if d.startswith("img-src")] == ["img-src 'self' blob:"]
        for path in (_chat_url(host, NOPE), f"http://{host}/chat/", f"http://{host}/chat/{T}/x"):
            assert (await client.get(path)).status_code == 404, path


async def test_media_route_serves_an_archived_file(
    board: W.Board, settings: Settings, data_dir: Path
) -> None:
    """011-AC3: an archived asset is served with its type, sandboxed; a missing one is a 404."""
    _media(data_dir)
    async with _served(board, settings) as host, httpx.AsyncClient(headers=_cookie(board)) as client:
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
    assert board.chat.media_file(f"/media/{thread}/{OBJ}.png") == (b"\x89PNG-fake", "image/png")
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
        assert board.chat.media_file(f"/media/{thread}/{name}") is None, name
    assert board.chat.media_file(f"/media/{thread}//etc/passwd") is None  # absolute: extra segment
    assert board.chat.media_file(f"/media/{thread}/sub/x.png") is None
    assert board.chat.media_file(f"/media/{quote(NOPE, safe='')}/{OBJ}.png") is None
    assert board.chat.media_file(f"/media/{quote(B, safe='')}/{OBJ}.png") is None  # on the board, no archive


GW = "fr-prod.asyncgw.teams.microsoft.com"


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """Stub Teams: `fresh` is an image, anything else refused; returns the (token, host, id) asked."""
    asked: list[tuple[str, str, str]] = []

    async def fake_live(
        client: Any, token: str, host: str, oid: str, view: str = "imgo"
    ) -> tuple[bytes, str] | None:
        asked.append((token, host, oid))
        return (b"\x89PNG-live", "image/png") if oid == "fresh" else None

    monkeypatch.setattr(CV, "live_image", fake_live)
    return asked


async def test_media_route_serves_an_image_archived_after_the_render(
    board: W.Board, settings: Settings, data_dir: Path, live: list[Any]
) -> None:
    """011-AC3: `/media/<thread>/<id>?host=` (rendered before the archiver had it) finds the file by id."""
    _media(data_dir)
    async with _served(board, settings) as host, httpx.AsyncClient(headers=_cookie(board)) as client:
        got = await client.get(f"http://{host}/media/{quote(T, safe='')}/{OBJ}?host={GW}")
        assert got.status_code == 200 and got.content == b"\x89PNG-fake"
        assert got.headers["content-type"] == "image/png"
    assert live == []


async def test_media_route_fetches_an_unarchived_image_from_teams(
    board: W.Board, settings: Settings, live: list[Any]
) -> None:
    """011-AC3: an image not archived (yet, or never: media excluded) is fetched with the web's token,
    sandboxed; a refused one 404s and is not asked again; a foreign host is never contacted."""
    token = board.chat.directory.skype_token  # type: ignore[union-attr]
    async with _served(board, settings) as host, httpx.AsyncClient(headers=_cookie(board)) as client:
        for thread_id in (T, B):  # archived chat, chat without an archive
            got = await client.get(f"http://{host}/media/{quote(thread_id, safe='')}/fresh?host={GW}")
            assert got.status_code == 200 and got.content == b"\x89PNG-live"
            assert got.headers["content-type"] == "image/png"
            assert got.headers["content-security-policy"] == "sandbox"
            assert got.headers["x-content-type-options"] == "nosniff"
        base = f"http://{host}/media/{quote(T, safe='')}"
        for _ in range(2):
            assert (await client.get(f"{base}/gone?host={GW}")).status_code == 404
        assert live == [(token, GW, "fresh"), (token, GW, "fresh"), (token, GW, "gone")]
        for path in (
            "/fresh",
            "/fresh?host=evil.example",
            f"/fresh?host={GW}.evil.example",
            f"/fresh?host=x%40{GW}",
            f"/fresh?host={GW}%3A8443",
            f"/fresh?host=a.{GW}",
            f"/%2e%2e?host={GW}",
            f"/fre*?host={GW}",
            f"/fresh/x?host={GW}",
            f"/x%2Ffresh?host={GW}",
        ):
            assert (await client.get(f"{base}{path}")).status_code == 404, path
        assert len(live) == 3  # none of those reached Teams
        anonymous = await client.get(f"{base}/fresh?host={GW}", headers={"Cookie": ""})
        assert anonymous.status_code == 403


async def test_media_route_fetches_nothing_for_an_unknown_chat(
    board: W.Board, settings: Settings, live: list[Any]
) -> None:
    """011-AC3: a thread neither on the board nor in the archive is a 404, Teams never asked."""
    async with _served(board, settings) as host, httpx.AsyncClient(headers=_cookie(board)) as client:
        got = await client.get(f"http://{host}/media/{quote(NOPE, safe='')}/fresh?host={GW}")
        assert got.status_code == 404
    assert live == []


async def test_media_route_live_fetch_follows_the_token(board: W.Board, live: list[Any]) -> None:
    """011-AC3: no token, no fetch; a refusal is not asked again under the same token, but is once
    the token is refreshed (an expired token must not hide images for the memo's whole window)."""
    directory = board.chat.directory
    assert directory is not None
    path = f"/media/{quote(T, safe='')}/gone"
    directory.set_token("")
    assert await board.chat.media(path, GW) is None
    assert live == []
    directory.set_token("old")
    for _ in range(2):
        assert await board.chat.media(path, GW) is None
    assert live == [("old", GW, "gone")]
    directory.set_token("new")
    assert await board.chat.media(path, GW) is None
    assert live == [("old", GW, "gone"), ("new", GW, "gone")]


async def test_media_route_live_fetches_run_four_at_a_time(
    board: W.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC3: a history page full of unarchived images does not fan out to Teams all at once."""
    release, started = asyncio.Event(), []

    async def held(client: Any, token: str, host: str, oid: str, view: str = "imgo") -> None:
        started.append(oid)
        await release.wait()

    monkeypatch.setattr(CV, "live_image", held)
    asks = [asyncio.create_task(board.chat.media(f"/media/{quote(T, safe='')}/img{i}", GW)) for i in range(6)]
    await wait(lambda: len(started) >= 4, "four fetches started")
    await asyncio.sleep(0.05)
    assert len(started) == 4
    release.set()
    assert await asyncio.gather(*asks) == [None] * 6
    assert len(started) == 6


async def test_media_route_live_fetch_gives_up_inside_the_request_deadline(
    board: W.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC3: a fetch slower than the deadline is a failure, not an answer nobody waits for."""
    asked: list[str] = []

    async def hung(client: Any, token: str, host: str, oid: str, view: str = "imgo") -> None:
        asked.append(oid)
        await asyncio.Event().wait()

    monkeypatch.setattr(CV, "live_image", hung)
    monkeypatch.setattr(CV, "_LIVE_DEADLINE", 0.05)
    path = f"/media/{quote(T, safe='')}/slow"
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    with structlog.testing.capture_logs() as logs:
        for _ in range(2):
            assert await asyncio.wait_for(board.chat.media(path, GW), 2) is None
    assert asked == ["slow"]  # the timeout is memoised like any failure
    timeouts = [e for e in logs if e["event"] == "live_image_failed"]
    assert [(e["host"], e["object_id"], e["error"]) for e in timeouts] == [(GW, "slow", "timeout")]


async def test_media_route_live_fetch_asks_the_view_the_message_names(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """031-AC6: `view=` is the view fetched (a Teams GIF's `imgo` is a still); one off the pattern is imgo.
    Each view has its own failure memo."""
    asked: list[tuple[str, str]] = []

    async def fake(client: Any, token: str, host: str, oid: str, view: str = "imgo") -> None:
        asked.append((oid, view))

    monkeypatch.setattr(CV, "live_image", fake)
    anim = "imgpsh_mobile_save_anim"
    async with _served(board, settings) as host, httpx.AsyncClient(headers=_cookie(board)) as client:
        base = f"http://{host}/media/{quote(T, safe='')}"
        for i, view in enumerate((anim, "imgo", "..%2Fx", "%2F", "IMGO", "a" * 41, "", "a-b")):
            assert (await client.get(f"{base}/o{i}?host={GW}&view={view}")).status_code == 404
        for _ in range(2):  # memoised per view: a refused still never hides the animated one
            await client.get(f"{base}/same?host={GW}&view={anim}")
            await client.get(f"{base}/same?host={GW}")
    assert asked == [("o0", anim)] + [(f"o{i}", "imgo") for i in range(1, 8)] + [
        ("same", anim),
        ("same", "imgo"),
    ]


GIPHY_ID = "l0MYt5jPR6QX5pnqM"
GIPHY_PATH = f"media2.giphy.com/media/v1.Y2lk/{GIPHY_ID}/giphy.gif"


@pytest.fixture
def giphy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub Giphy: a GIF for GIPHY_ID, anything else refused; returns the URLs asked."""
    asked: list[str] = []

    async def fake(client: Any, url: str) -> tuple[bytes, str] | None:
        asked.append(url)
        return (b"GIF89a-live", "image/gif") if GIPHY_ID in url else None

    monkeypatch.setattr(CV, "giphy_image", fake)
    return asked


async def test_media_route_serves_an_archived_giphy_image_without_giphy(
    board: W.Board, settings: Settings, data_dir: Path, giphy: list[str], live: list[Any]
) -> None:
    """031-AC2, 031-AC6: the archived copy by id (the `.gif` before a `.full.png`), Giphy never asked."""
    media = _media(data_dir)
    (media / f"giphy-{GIPHY_ID}.gif").write_bytes(b"GIF89a-archived")
    (media / f"{OBJ}.full.png").write_bytes(b"still")
    (media / f"{OBJ}.gif").write_bytes(b"GIF89a-teams")
    (media / f"{OBJ}.png").unlink()
    async with _served(board, settings) as host, httpx.AsyncClient(headers=_cookie(board)) as client:
        base = f"http://{host}/media/{quote(T, safe='')}"
        got = await client.get(f"{base}/giphy-{GIPHY_ID}?giphy={GIPHY_PATH}")
        assert got.status_code == 200 and got.content == b"GIF89a-archived"
        assert got.headers["content-type"] == "image/gif"
        got = await client.get(f"{base}/{OBJ}?host={GW}")
        assert got.content == b"GIF89a-teams"
    assert giphy == [] and live == []


async def test_media_route_fetches_an_unarchived_giphy_image(
    board: W.Board, settings: Settings, giphy: list[str], live: list[Any]
) -> None:
    """031-AC3: fetched from the rebuilt Giphy URL, sandboxed; a refusal 404s and is remembered whatever
    the token; any other host, path or name never reaches Giphy (nor Teams)."""
    url = f"https://{GIPHY_PATH}"
    directory = board.chat.directory
    assert directory is not None
    async with _served(board, settings) as host, httpx.AsyncClient(headers=_cookie(board)) as client:
        for thread_id in (T, B):
            base = f"http://{host}/media/{quote(thread_id, safe='')}"
            got = await client.get(f"{base}/giphy-{GIPHY_ID}?giphy={quote(GIPHY_PATH, safe='/')}")
            assert got.status_code == 200 and got.content == b"GIF89a-live"
            assert got.headers["content-type"] == "image/gif"
            assert got.headers["content-security-policy"] == "sandbox"
        assert giphy == [url, url]
        gone = "media2.giphy.com/media/gone1234/giphy.gif"
        for token in ("a", "b"):  # no token on the request: one memo for every token
            directory.set_token(token)
            assert (await client.get(f"{base}/giphy-gone1234?giphy={gone}")).status_code == 404
        assert giphy == [url, url, f"https://{gone}"]
        for path in (
            f"/giphy-{GIPHY_ID}?giphy=evil.example/media/{GIPHY_ID}/giphy.gif",
            f"/giphy-{GIPHY_ID}?giphy=media2.giphy.com.evil.example/media/{GIPHY_ID}/giphy.gif",
            f"/giphy-{GIPHY_ID}?giphy=x%40media2.giphy.com/media/{GIPHY_ID}/giphy.gif",
            f"/giphy-{GIPHY_ID}?giphy=media2.giphy.com%3A8443/media/{GIPHY_ID}/giphy.gif",
            f"/giphy-{GIPHY_ID}?giphy=media2.giphy.com/media/{GIPHY_ID}/giphy.svg",
            f"/giphy-{GIPHY_ID}?giphy=media2.giphy.com/media/{GIPHY_ID}/giphy.gif%3F",
            f"/giphy-other?giphy={GIPHY_PATH}",  # the name must be the URL's id
            f"/{OBJ}x?giphy={GIPHY_PATH}",
            f"/giphy-{GIPHY_ID}?giphy=//{GIPHY_PATH}",
            f"/giphy-{GIPHY_ID}/x?giphy={GIPHY_PATH}",
        ):
            assert (await client.get(f"{base}{path}")).status_code == 404, path
        assert len(giphy) == 3 and live == []
        anonymous = await client.get(f"{base}/giphy-{GIPHY_ID}?giphy={GIPHY_PATH}", headers={"Cookie": ""})
        assert anonymous.status_code == 403
        unknown = f"http://{host}/media/{quote(NOPE, safe='')}/giphy-{GIPHY_ID}?giphy={GIPHY_PATH}"
        assert (await client.get(unknown)).status_code == 404
    assert len(giphy) == 3


async def test_giphy_fetches_share_the_live_slots_and_deadline(
    board: W.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """031-AC3: Giphy and Teams fetches run four at a time together; a slow one is a remembered failure."""
    release, started = asyncio.Event(), []

    async def held(client: Any, *args: str) -> None:
        started.append(args[0] if len(args) == 1 else args[2])  # Giphy's URL, Teams' object id
        await release.wait()

    monkeypatch.setattr(CV, "giphy_image", held)
    monkeypatch.setattr(CV, "live_image", held)
    thread = quote(T, safe="")
    asks = [
        asyncio.create_task(
            board.chat.media(f"/media/{thread}/giphy-id{i}", "", f"media{i}.giphy.com/media/id{i}/giphy.gif")
        )
        for i in range(3)
    ] + [asyncio.create_task(board.chat.media(f"/media/{thread}/img{i}", GW)) for i in range(3)]
    await wait(lambda: len(started) >= 4, "four fetches started")
    await asyncio.sleep(0.05)
    assert len(started) == 4
    release.set()
    assert await asyncio.gather(*asks) == [None] * 6
    monkeypatch.setattr(CV, "_LIVE_DEADLINE", 0.05)
    release.clear()
    started.clear()
    path = f"/media/{thread}/giphy-slow"
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    with structlog.testing.capture_logs() as logs:
        for _ in range(2):
            assert await asyncio.wait_for(board.chat.media(path, "", "i.giphy.com/slow.gif"), 2) is None
    assert started == ["https://i.giphy.com/slow.gif"]
    timeouts = [e for e in logs if e["event"] == "live_image_failed"]
    assert [(e["giphy"], e["error"]) for e in timeouts] == [("https://i.giphy.com/slow.gif", "timeout")]


async def test_gif_route_serves_the_pasted_gif_to_the_cookie_holder_only(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pasted GIF's original: fetched by `remote_gif` (cookie only), sandboxed; a refusal 404s."""
    asked: list[str] = []

    async def fake(url: str) -> bytes | None:
        asked.append(url)
        return b"GIF89a-remote" if "/a.gif" in url else None

    monkeypatch.setattr(CV, "remote_gif", fake)
    url = "https://gifs.example/a.gif?x=1&y=2"
    async with _served(board, settings) as host, httpx.AsyncClient(headers=_cookie(board)) as client:
        got = await client.get(f"http://{host}/gif", params={"url": url})
        assert got.status_code == 200 and got.content == b"GIF89a-remote"
        assert got.headers["content-type"] == "image/gif"
        assert got.headers["content-security-policy"] == "sandbox"
        assert (
            await client.get(f"http://{host}/gif", params={"url": "https://gifs.example/b.gif"})
        ).status_code == 404
        anonymous = await client.get(f"http://{host}/gif", params={"url": url}, headers={"Cookie": ""})
        assert anonymous.status_code == 403
        wrong = await client.get(f"http://{host}/gif", params={"url": url}, headers={"Cookie": "mt=nope"})
        assert wrong.status_code == 403
    assert asked == [url, "https://gifs.example/b.gif"]


async def test_gif_fetches_share_the_live_deadline(board: W.Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung remote GIF gives up inside the request deadline."""

    async def hung(url: str) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(CV, "remote_gif", hung)
    monkeypatch.setattr(CV, "_LIVE_DEADLINE", 0.05)
    assert await asyncio.wait_for(board.chat.gif("https://gifs.example/a.gif"), 2) is None


def test_media_file_refuses_oversized_files(
    board: W.Board, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC3: the whole-body response is capped."""
    _media(data_dir)
    monkeypatch.setattr(CV, "_MEDIA_MAX", 4)
    assert board.chat.media_file(f"/media/{quote(T, safe='')}/{OBJ}.png") is None


def test_media_file_unreadable_is_none(
    board: W.Board, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC3: a file gone or unreadable between the check and the read is a 404, not an error."""
    _media(data_dir)

    def gone(self: Path) -> bytes:
        raise PermissionError(self)

    monkeypatch.setattr(Path, "read_bytes", gone)
    assert board.chat.media_file(f"/media/{quote(T, safe='')}/{OBJ}.png") is None


def _chat_socket(board: W.Board) -> Any:
    return board_socket(_host(board), board.secret, chat=True)


async def test_chat_socket_never_receives_the_board(board: W.Board, settings: Settings) -> None:
    """011-AC10: a chat page's socket gets its chat's frames, never the board's rows; the widget does."""
    async with _served(board, settings), _chat_socket(board) as chat, _connect(board) as widget:
        assert "rows" in json.loads(await widget.recv())
        await chat.send(json.dumps({"follow": T}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {
            "follow": T,
            "ok": True,
            "label": "Chat",  # archived, not seeded: the index names it
            "opener": False,
            "page": W.page_version("chat.html"),
            "read_only": False,
            "typing": [],
            "people": None,  # no member count in the label: no roster (spec 022)
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


async def test_a_split_chat_socket_subscribes_to_the_board(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC23: `board` gives a chat socket the widget's payload and every later broadcast; seen and
    mute work from it; a chat socket that did not ask still gets no rows."""
    monkeypatch.setattr(W, "set_alerts", lambda *args: {"status": 200})  # 030: Teams accepts the mute
    async with _served(board, settings), _chat_socket(board) as split, _chat_socket(board) as plain:
        await split.send(json.dumps({"follow": T}))
        assert (await _frame(split))["follow"] == T
        await split.send(json.dumps({"board": True}))
        first = json.loads(await asyncio.wait_for(split.recv(), 2))
        assert first == json.loads(board.payload())
        assert [r["id"] for r in first["rows"]] == [B]
        await split.send(json.dumps({"board": True}))  # twice: still one subscription, no second payload
        await split.send(json.dumps({"seen": B, "at": _at(1)}))
        assert json.loads(await asyncio.wait_for(split.recv(), 2))["rows"][0]["seen_at"] == _at(1)
        await split.send(json.dumps({"mute": B}))
        assert json.loads(await asyncio.wait_for(split.recv(), 2))["rows"][0]["muted"] is True
        await split.send(json.dumps({"unmute": B}))
        assert json.loads(await asyncio.wait_for(split.recv(), 2))["rows"][0]["muted"] is False
        await split.send(json.dumps({"unseen": B}))
        assert json.loads(await asyncio.wait_for(split.recv(), 2))["rows"][0]["seen_at"] is None
        # A new message on a row that is not followed: its row moves, no message frame of it.
        await board.on_event(
            _event("NewMessage", B, id="7", composetime=_at(7), messagetype="Text", content="hi")
        )
        frame = json.loads(await asyncio.wait_for(split.recv(), 2))
        assert set(frame) == {"rows", "page", "opener", "browser", "me", "busy", "sections"}
        assert (frame["rows"][0]["last_activity"], frame["rows"][0]["text"]) == (_at(7), "hi")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(split.recv(), 0.3)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(plain.recv(), 0.3)
        assert len(board.clients) == 1
    await wait(lambda: not board.clients, "the clients gone")


async def test_switching_the_followed_chat_stops_the_old_relay(board: W.Board, settings: Settings) -> None:
    """011-AC22: after `follow` on another chat, the previous one's live events no longer reach the socket."""
    async with _served(board, settings), _chat_socket(board) as ws:
        await ws.send(json.dumps({"follow": T}))
        assert (await _frame(ws))["follow"] == T
        await ws.send(json.dumps({"follow": B}))
        assert (await _frame(ws))["follow"] == B
        await board.on_event(_event("NewMessage", T, messagetype="Control/Typing"))
        await board.on_event(
            _event("NewMessage", T, id="8", composetime=_at(8), messagetype="Text", content="old")
        )
        await board.on_event(
            _event("NewMessage", B, id="9", composetime=_at(9), messagetype="Text", content="new")
        )
        assert (await _frame(ws))["msg"]["id"] == "9"  # the first frame relayed is the new chat's


async def test_relayed_frames_name_their_chat(board: W.Board, settings: Settings) -> None:
    """011-AC22: message, edit and typing frames carry `thread`: a page that switched drops the left one's."""
    async with _served(board, settings), _chat_socket(board) as ws:
        await ws.send(json.dumps({"follow": T}))
        assert (await _frame(ws))["follow"] == T
        new = {"id": "9", "composetime": _at(9), "messagetype": "Text", "content": "x"}
        await board.on_event(_event("NewMessage", T, **new))
        await board.on_event(_event("MessageUpdate", T, **{**new, "content": "y"}))
        await board.on_event(_event("NewMessage", T, messagetype="Control/Typing"))
        await board.on_event(_event("NewMessage", T, messagetype="Control/ClearTyping"))
        frames = [await _frame(ws) for _ in range(4)]
        assert [next(k for k in ("msg", "edit", "typing") if k in f) for f in frames] == [
            "msg",
            "edit",
            "typing",
            "typing",
        ]
        assert {f["thread"] for f in frames} == {T}


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

    def fake_send(
        settings: Any, token: str, thread_id: str, text: str, me_name: str, **kw: Any
    ) -> dict[str, Any]:
        calls.append((thread_id, text))
        return {"status": 201}

    monkeypatch.setattr(CV, "send_message", fake_send)
    return calls


async def test_send_reaches_a_followed_chat_off_the_board(
    board: W.Board, settings: Settings, sent: list[tuple[str, str]]
) -> None:
    """011-AC8: the composer's send posts once to a followed archived chat; anything else is refused."""
    async with _served(board, settings), _chat_socket(board) as chat, _chat_socket(board) as other:
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

    monkeypatch.setattr(CV, "send_message", failing)
    async with _served(board, settings), _chat_socket(board) as chat:
        await chat.send(json.dumps({"follow": T}))
        await chat.recv()
        await chat.send(json.dumps({"send": T, "text": "hi"}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {"sent": T, "error": "network error"}


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    class _Proc:
        stderr = None

        async def wait(self) -> int:
            return 0

    async def fake_exec(*argv: str, **kw: Any) -> _Proc:
        calls.append(argv)
        return _Proc()

    # Only miniteams.web sees the fake: patching asyncio itself would also catch playwright's driver.
    class _Asyncio:
        create_subprocess_exec = staticmethod(fake_exec)

        def __getattr__(self, name: str) -> Any:
            return getattr(asyncio, name)

    monkeypatch.setattr(W, "asyncio", _Asyncio())
    return calls


async def _until(calls: list[Any], n: int) -> None:
    await wait(lambda: len(calls) >= n, f"{n} calls")


async def test_open_reaches_a_followed_chat_and_lands_on_a_message(
    quiet_directory: Directory, data_dir: Path, settings: Settings, launched: list[tuple[str, ...]]
) -> None:
    """011-AC19: `open` runs the opener for a followed off-board chat, on a message id when it is digits."""
    rows = {B: {"id": B, "label": "B", "last_activity": _at(1), "last_id": "1", "typing": []}}
    board = W.Board(rows, quiet_directory, me=ME, data_dir=data_dir, opener=["xdg-open"])
    async with _served(board, settings), _chat_socket(board) as chat:
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
    assert board.secret and not any(board.secret in arg for argv in launched for arg in argv)  # Teams never


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
    async with _served(board, settings) as host, httpx.AsyncClient(headers=_cookie(board)) as client:
        thread = quote(T, safe="")
        for path in (f"/chat/{thread}", f"/media/{thread}/{OBJ}.png"):
            assert (await client.get(f"http://{host}{path}")).status_code == 200, path
            for foreign in ("evil.example", f"evil.example:{host.rsplit(':', 1)[1]}", "localhost"):
                got = await client.get(f"http://{host}{path}", headers={"Host": foreign})
                assert got.status_code == 404, (path, foreign)


async def test_chat_socket_checks_the_origin(board: W.Board, settings: Settings) -> None:
    """011-AC10: `/ws?chat` has the board's Origin rule: a foreign or missing Origin gets 403."""
    async with _served(board, settings) as host:
        for origin in ("http://evil.example", "null", None):  # the cookie right: only the Origin is wrong
            with pytest.raises(websockets.InvalidStatus) as exc:
                await websockets.connect(
                    f"ws://{host}/ws?chat",
                    origin=websockets.Origin(origin) if origin else None,
                    additional_headers=_cookie(board),
                )
            assert exc.value.response.status_code == 403, origin


async def test_send_never_reaches_a_followed_channel(
    board: W.Board, settings: Settings, sent: list[tuple[str, str]]
) -> None:
    """011-AC8 + 011-AC9: a followed archived channel is read-only: send is refused, no API call."""
    async with _served(board, settings), _chat_socket(board) as chat:
        await chat.send(json.dumps({"follow": C}))
        answer = json.loads(await asyncio.wait_for(chat.recv(), 2))
        assert (answer["ok"], answer["read_only"]) == (True, True)
        await chat.send(json.dumps({"send": C, "text": "hi"}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {"sent": C, "error": "unknown chat"}
    assert sent == []


# --- middle click "chat here" through --browser (011-AC1) ---


async def test_open_chat_runs_the_browser_on_our_own_chat_page(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC1: `{"open", "chat": true}` runs --browser on http://<bind>/chat/<encoded id>, `msg` ignored;
    unknown threads and a board without --browser launch nothing."""
    launched: list[tuple[str, ...]] = []

    class _Proc:
        stderr = None

        async def wait(self) -> int:
            return 0

    async def fake_exec(*argv: str, **kw: Any) -> _Proc:
        launched.append(argv)
        return _Proc()

    monkeypatch.setattr(W.asyncio, "create_subprocess_exec", fake_exec)
    board.browser = ["firefox"]
    assert await board.open_row(B, "chat") is False  # not bound yet: no URL to point at
    board.browser = None
    async with _served(board, settings) as host:
        async with _connect(board) as ws:
            await ws.recv()
            # Verbs run in order: a follow answer means every verb sent before it ran.
            await ws.send(json.dumps({"open": B, "chat": True}))  # no --browser yet
            await ws.send(json.dumps({"follow": B}))
            await _frame(ws)
            assert launched == []
            board.browser = ["firefox", "--new-tab"]
            await ws.send(json.dumps({"open": B, "chat": True, "msg": "1"}))
            await ws.send(json.dumps({"open": NOPE, "chat": True}))
            await ws.send(json.dumps({"follow": B}))
            await _frame(ws)
    token = board.token
    assert launched == [("firefox", "--new-tab", f"http://{host}/chat/19%3Abbbb%40thread.v2#{token}")]
    assert board.secret not in repr(launched)


async def test_open_chat_carries_the_split_setting(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC21: `chatnav: "split"` adds `?chatnav=split` to the chat page URL; any other value is
    ignored, and `chatnav` alone does not make a chat open."""
    launched: list[tuple[str, ...]] = []

    class _Proc:
        stderr = None

        async def wait(self) -> int:
            return 0

    async def fake_exec(*argv: str, **kw: Any) -> _Proc:
        launched.append(argv)
        return _Proc()

    monkeypatch.setattr(W.asyncio, "create_subprocess_exec", fake_exec)
    board.browser = ["firefox"]
    async with _served(board, settings) as host:
        async with _connect(board) as ws:
            await ws.recv()
            for chatnav in ("split", "split&x=1", '"><', ["split"], True, "list"):
                await ws.send(json.dumps({"open": B, "chat": True, "chatnav": chatnav}))
            await ws.send(json.dumps({"open": B, "chat": "split", "chatnav": "split"}))
            await ws.send(json.dumps({"follow": B}))
            await _frame(ws)
    page, token = f"http://{host}/chat/19%3Abbbb%40thread.v2", f"#{board.token}"
    assert launched == [("firefox", page + "?chatnav=split" + token)] + [("firefox", page + token)] * 5
    assert board.secret not in repr(launched)


async def test_a_chat_open_carries_the_launch_token(
    board: W.Board, settings: Settings, launched: list[tuple[str, ...]]
) -> None:
    """--browser gets the chat URL with the current token as its fragment, never the secret: the page
    trades it once at /auth for the cookie."""
    board.browser = ["firefox"]
    async with _served(board, settings) as host, httpx.AsyncClient() as client:
        assert await board.open_row(B, "split")
        [(_, url)] = launched
        page, _, token = url.partition("#")
        assert page == f"http://{host}/chat/{quote(B, safe='')}?chatnav=split" and token == board.token
        auth = {"X-Miniteams-Token": token}
        got = await client.get(f"http://{host}/auth", headers=auth)
        assert got.status_code == 204 and got.headers["set-cookie"].startswith(f"mt={board.secret};")
        assert (await client.get(f"http://{host}/auth", headers=auth)).status_code == 403  # spent
        assert board.token != token and await board.open_row(B, "chat")
        assert launched[-1][1].endswith(f"#{board.token}")


# --- message actions: quote, react, edit, delete (011 phase 4) ---

BO = "8:orgid:00000000-0000-0000-0000-00000000000c"
_LONG = "<p>R&amp;D <b>plan</b> " + "x" * 120 + "</p>"
_LONG_PREVIEW = ("R&D plan " + "x" * 120)[:100].replace("&", "&amp;")  # first 100 plain chars, escaped


@pytest.fixture
def act_board(tmp_path: Path, quiet_directory: Directory) -> W.Board:
    """T archived with Ann's message 1, my message 2 and Bo's message 3 (a name to escape); my deleted
    message 4, my file post 5, a sender-less system event 6."""
    mine = {"from": f"https://h/v1/users/ME/contacts/{ME}", "imdisplayname": "Me Self"}
    bo = {"from": f"https://h/v1/users/ME/contacts/{BO}", "imdisplayname": "Bo <B> & Co"}
    data = _archive(
        tmp_path / "acts",
        T,
        [
            _m("1", _at(1), _LONG),
            _m("2", _at(2), "<p>mine</p>", **mine),
            _m("3", _at(3), '<p>a"b</p>', **bo),
            _m("4", _at(4), "", **{**mine, "properties": {"deletetime": "1789466600000"}}),
            _m("5", _at(5), "<p>f</p>", **{**mine, "messagetype": "RichText/Media_GenericFile"}),
            _m("6", _at(6), "<addmember/>", **{"messagetype": "ThreadActivity/AddMember", "from": ""}),
        ],
    )
    _archive(data, C, [_m("1", _at(1), **mine)])
    rows = {
        B: {"id": B, "label": "B", "last_activity": _at(1), "last_id": "1", "seen_at": None, "typing": []}
    }
    return W.Board(rows, quiet_directory, me=ME, data_dir=data)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Every Teams call the actions make (writes and the API history read), API mocked."""
    made: list[tuple[Any, ...]] = []

    def fake_send(s: Any, token: str, thread_id: str, text: str, name: str, **kw: Any) -> dict[str, Any]:
        made.append(("send", thread_id, text, kw))
        return {"status": 201}

    def fake_react(s: Any, token: str, thread_id: str, msg: str, key: str, *, remove: bool) -> dict[str, Any]:
        made.append(("react", thread_id, msg, key, remove))
        return {"status": 200}

    def fake_edit(s: Any, token: str, thread_id: str, msg: str, text: str, **kw: Any) -> dict[str, Any]:
        made.append(("edit", thread_id, msg, text, *([kw] if kw else [])))  # kw: spec 019's composed edit
        return {"status": 200}

    def fake_delete(s: Any, token: str, thread_id: str, msg: str) -> dict[str, Any]:
        made.append(("delete", thread_id, msg))
        return {"status": 200}

    def fake_history(
        s: Any, token: str, thread_id: str, size: int, pages: int, end: int | None = None
    ) -> list[dict[str, Any]]:
        made.append(("history", thread_id))
        return []

    for name, fake in (
        ("send_message", fake_send),
        ("react", fake_react),
        ("edit_message", fake_edit),
        ("delete_message", fake_delete),
        ("fetch_history", fake_history),
    ):
        monkeypatch.setattr(CV, name, fake)
    return made


def _writes(made: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return [c for c in made if c[0] != "history"]


async def _following(board: W.Board, thread_id: str) -> Any:
    ws = object()
    assert (await board.follow(ws, thread_id))["ok"]  # type: ignore[arg-type]
    return ws


def _block(msg_id: str, mri: str, name: str, preview: str) -> str:
    return (
        f'<blockquote itemscope itemtype="http://schema.skype.com/Reply" itemid="{msg_id}">'
        f'<strong itemprop="mri" itemid="{mri}">{name}</strong>'
        f'<span itemprop="time" itemid="{msg_id}"></span>'
        f'<p itemprop="preview">{preview}</p></blockquote>'
    )


async def test_quote_prepends_reply_blocks_in_order_from_the_servers_copy(
    act_board: W.Board, calls: list[tuple[Any, ...]]
) -> None:
    """011-AC11: one Reply block per id, in the order given, author and preview from the archive, escaped."""
    ws = await _following(act_board, T)
    assert await _send(act_board, T, "ok <then>\nbye", ws, ["3", "1"]) is None
    assert calls == [
        (
            "send",
            T,
            _block("3", BO, "Bo &lt;B&gt; &amp; Co", "a&quot;b")
            + _block("1", ANN, "Ann Lee", _LONG_PREVIEW)
            + "ok &lt;then&gt;<br>bye",
            {"is_html": True},
        )
    ]


async def test_quote_refusals_make_no_write(act_board: W.Board, calls: list[tuple[Any, ...]]) -> None:
    """011-AC11: unknown id, more than 10, duplicates, malformed ids: refused before any write."""
    ws = await _following(act_board, T)
    eleven = [str(i) for i in range(1, 12)]
    for ids, error in (
        (["1", "99"], "unknown quoted message"),
        (eleven, "too many quotes"),
        (["1", "1"], "invalid quote"),
        (["1x"], "invalid quote"),
        ([1], "invalid quote"),
        ("1", "invalid quote"),
        ([{"id": "1"}], "invalid quote"),
    ):
        assert await _send(act_board, T, "hi", ws, ids) == error, ids
    assert _writes(calls) == []
    # A message the archive lacks is looked for in the latest API page, then the one ending at it.
    assert calls == [("history", T)] * 2


async def test_quote_found_in_the_api_page_when_the_archive_lacks_it(
    act_board: W.Board, monkeypatch: pytest.MonkeyPatch, calls: list[tuple[Any, ...]]
) -> None:
    """011-AC11: a chat without archive (and a message newer than it) quotes from one API page."""
    fresh = _m("7", _at(7), "<p>fresh</p>")
    monkeypatch.setattr(CV, "fetch_history", lambda *a: [fresh])
    assert await _send(act_board, B, "re", None, ["7"]) is None
    ws = await _following(act_board, T)
    assert await _send(act_board, T, "re", ws, ["1", "7"]) is None
    fresh_block = _block("7", ANN, "Ann Lee", "fresh")
    assert [c[2] for c in calls] == [
        fresh_block + "re",
        _block("1", ANN, "Ann Lee", _LONG_PREVIEW) + fresh_block + "re",
    ]


async def test_quote_counts_toward_the_size_cap(act_board: W.Board, calls: list[tuple[Any, ...]]) -> None:
    """011-AC11 + 011-AC13: the 28 KB cap applies to the body with its quotes."""
    ws = await _following(act_board, T)
    text = "y" * (compose.MAX_TEXT - 100)
    assert await _send(act_board, T, text, ws) is None
    assert await _send(act_board, T, text, ws, ["1"]) == "message too long"
    assert [c[0] for c in calls] == ["send"]


async def test_react_adds_and_removes_one_of_the_accepted_keys(
    act_board: W.Board, calls: list[tuple[Any, ...]]
) -> None:
    """011-AC12: one react() per verb with the key and `remove`; other keys and bad flags refused."""
    ws = await _following(act_board, T)
    assert await act_board.chat.act_answer(
        ws, "react", {"react": T, "msg": "1", "key": "like", "remove": False}
    ) == {
        "react": T,
        "msg": "1",
        "ok": True,
    }
    assert (
        await act_board.chat.act_answer(ws, "react", {"react": T, "msg": "2", "key": "heart", "remove": True})
    )["ok"]
    for key in ("praying", "upsidedownface"):  # Teams' own names for 🙏 and 🙃
        request = {"react": T, "msg": "3", "key": key, "remove": False}
        assert (await act_board.chat.act_answer(ws, "react", request))["ok"]
    for request, error in (
        ({"key": "<b>"}, "invalid reaction"),
        ({"key": "like "}, "invalid reaction"),
        ({"key": ["like"]}, "invalid reaction"),
        ({"key": "like", "remove": "yes"}, "invalid reaction"),
        ({"key": "like", "msg": "../1"}, "invalid message"),
        ({"key": "like", "msg": None}, "invalid message"),
    ):
        answer = await act_board.chat.act_answer(ws, "react", {"react": T, "msg": "1", **request})
        assert answer.get("error") == error, request
    assert _writes(calls) == [
        ("react", T, "1", "like", False),
        ("react", T, "2", "heart", True),
        ("react", T, "3", "praying", False),
        ("react", T, "3", "upsidedownface", False),
    ]


async def test_edit_only_my_own_message_with_valid_text(
    act_board: W.Board, calls: list[tuple[Any, ...]]
) -> None:
    """011-AC13: edit() once on my message; another's, empty or over 28 KB: refused, no API write."""
    ws = await _following(act_board, T)
    assert await act_board.chat.act_answer(ws, "edit", {"edit": T, "msg": "2", "text": "fixed <b>"}) == {
        "edit": T,
        "msg": "2",
        "ok": True,
    }
    for msg, text, error in (
        ("1", "hijack", "not your message"),
        ("3", "hijack", "not your message"),
        ("2", "  ", "empty message"),
        ("2", None, "empty message"),
        ("2", "<" * (compose.MAX_TEXT // 4 + 1), "message too long"),
    ):
        answer = await act_board.chat.act_answer(ws, "edit", {"edit": T, "msg": msg, "text": text})
        assert answer.get("error") == error, (msg, error)
    assert _writes(calls) == [("edit", T, "2", "fixed <b>")]


async def test_delete_only_my_own_message(act_board: W.Board, calls: list[tuple[Any, ...]]) -> None:
    """011-AC14: delete() once on my message; on another's it is refused without an API write."""
    ws = await _following(act_board, T)
    assert await act_board.chat.act_answer(ws, "delete", {"delete": T, "msg": "1"}) == {
        "delete": T,
        "msg": "1",
        "error": "not your message",
    }
    assert await act_board.chat.act_answer(ws, "delete", {"delete": T, "msg": "2"}) == {
        "delete": T,
        "msg": "2",
        "ok": True,
    }
    assert _writes(calls) == [("delete", T, "2")]


async def test_own_message_is_decided_without_a_known_me(
    act_board: W.Board, calls: list[tuple[Any, ...]]
) -> None:
    """011-AC13 + 011-AC14: with my MRI unknown, no message is mine: edit and delete refused."""
    act_board.chat.me = ""
    ws = await _following(act_board, T)
    for verb, extra in (("edit", {"text": "x"}), ("delete", {})):
        answer = await act_board.chat.act_answer(ws, verb, {verb: T, "msg": "2", **extra})
        assert answer["error"] == "not your message"
    assert _writes(calls) == []


async def test_actions_refused_on_channels_and_chats_not_followed(
    act_board: W.Board, calls: list[tuple[Any, ...]]
) -> None:
    """011-AC9 + 011-AC12..14: a followed channel, or a chat this socket does not follow: refused, no call."""
    channel = await _following(act_board, C)
    stranger = object()
    for ws, thread_id in ((channel, C), (stranger, T), (channel, NOPE)):
        for verb, extra in (("react", {"key": "like"}), ("edit", {"text": "x"}), ("delete", {})):
            answer = await act_board.chat.act_answer(ws, verb, {verb: thread_id, "msg": "1", **extra})
            assert answer["error"] == "unknown chat", (thread_id, verb)
        assert await _send(act_board, thread_id, "x", ws, ["1"]) == "unknown chat"
    assert calls == []


async def test_action_failure_answers_the_reason(
    act_board: W.Board, monkeypatch: pytest.MonkeyPatch, calls: list[tuple[Any, ...]]
) -> None:
    """011-AC13: a Teams refusal reaches the page as the reason, the socket survives it."""

    def refused(*args: Any) -> None:
        request = httpx.Request("PUT", "https://x")
        raise httpx.HTTPStatusError("no", request=request, response=httpx.Response(403, request=request))

    monkeypatch.setattr(CV, "edit_message", refused)
    ws = await _following(act_board, T)
    answer = await act_board.chat.act_answer(ws, "edit", {"edit": T, "msg": "2", "text": "x"})
    assert answer == {"edit": T, "msg": "2", "error": "Teams answered 403"}


async def test_action_verbs_over_the_socket(
    act_board: W.Board, settings: Settings, calls: list[tuple[Any, ...]]
) -> None:
    """011-AC11..AC14: each verb answers the asking socket only, in `{verb: thread, msg, ok|error}` shape."""
    async with (
        _served(act_board, settings),
        _chat_socket(act_board) as chat,
        _chat_socket(act_board) as other,
    ):
        await other.send(json.dumps({"follow": T}))
        await other.recv()
        await chat.send(json.dumps({"follow": T}))
        await chat.recv()
        await chat.send(json.dumps({"react": T, "msg": "1", "key": "laugh", "remove": False}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {"react": T, "msg": "1", "ok": True}
        await chat.send(json.dumps({"edit": T, "msg": "1", "text": "x"}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {
            "edit": T,
            "msg": "1",
            "error": "not your message",
        }
        await chat.send(json.dumps({"delete": T, "msg": "2"}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {"delete": T, "msg": "2", "ok": True}
        await chat.send(json.dumps({"send": T, "text": "re", "quote": ["2"]}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {"sent": T, "ok": True}
        await _silent(other)
    assert [c[0] for c in calls] == ["react", "delete", "send"]
    assert calls[-1][2] == _block("2", ME, "Me Self", "mine") + "re"


async def test_server_copy_survives_a_broken_archive(
    act_board: W.Board, monkeypatch: pytest.MonkeyPatch, calls: list[tuple[Any, ...]]
) -> None:
    """011-AC11: a corrupt row or a corrupt messages.db counts as "not archived": the API page decides."""
    chat_dir = find_chat_dir(act_board.data_dir, T)  # type: ignore[arg-type]
    assert chat_dir is not None
    db = sqlite3.connect(chat_dir / "messages.db")
    with db:
        db.execute("INSERT INTO messages (id, composetime, raw) VALUES ('8', ?, '{bad')", (_at(8),))
    db.close()
    ws = await _following(act_board, T)
    assert await _send(act_board, T, "x", ws, ["8"]) == "unknown quoted message"
    (chat_dir / "messages.db").write_bytes(b"not a database" * 100)
    for leftover in chat_dir.glob("messages.db-*"):
        leftover.unlink()
    monkeypatch.setattr(CV, "fetch_history", lambda *a: [_m("2", _at(2))])
    assert await _send(act_board, T, "x", ws, ["2"]) is None
    assert [c[0] for c in calls] == ["history", "history", "send"]


async def test_actions_without_a_connection(act_board: W.Board, calls: list[tuple[Any, ...]]) -> None:
    """011-AC12..AC14: no Teams session: refused, nothing called."""
    act_board.chat.directory = None
    ws = await _following(act_board, T)
    assert (await act_board.chat.act_answer(ws, "delete", {"delete": T, "msg": "2"}))[
        "error"
    ] == "not connected"
    assert await _send(act_board, T, "x", ws, ["2"]) == "not connected"
    assert calls == []


async def test_edit_and_delete_refuse_my_deleted_or_non_text_messages(
    act_board: W.Board, calls: list[tuple[Any, ...]]
) -> None:
    """011-AC13 + 011-AC14: the server's copy must be a live text message of mine to edit, live to delete."""
    ws = await _following(act_board, T)
    for verb, msg, error in (
        ("edit", "4", "message deleted"),
        ("delete", "4", "message deleted"),
        ("edit", "5", "cannot edit this message"),
    ):
        answer = await act_board.chat.act_answer(ws, verb, {verb: T, "msg": msg, "text": "x"})
        assert answer.get("error") == error, (verb, msg)
    assert (await act_board.chat.act_answer(ws, "delete", {"delete": T, "msg": "5"}))[
        "ok"
    ]  # a file post of mine goes
    assert _writes(calls) == [("delete", T, "5")]


async def test_quote_refuses_non_text_or_sender_less_messages(
    act_board: W.Board, calls: list[tuple[Any, ...]]
) -> None:
    """011-AC11: a file post or a system event cannot be quoted; nothing is posted."""
    ws = await _following(act_board, T)
    for ids in (["5"], ["6"], ["1", "6"]):
        assert await _send(act_board, T, "x", ws, ids) == "cannot quote this message", ids
    assert calls == []


async def test_one_message_action_in_flight_per_socket(
    act_board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch, calls: list[tuple[Any, ...]]
) -> None:
    """011-AC12..AC14: while one action runs, another action or a quoted send on that socket answers
    `busy`; a plain send and another socket are not held."""
    entered, release = threading.Event(), threading.Event()
    real = CV.react

    def held(*args: Any, **kw: Any) -> Any:
        entered.set()
        release.wait(5)
        return real(*args, **kw)

    monkeypatch.setattr(CV, "react", held)
    async with (
        _served(act_board, settings),
        _chat_socket(act_board) as chat,
        _chat_socket(act_board) as other,
    ):
        for ws in (chat, other):
            await ws.send(json.dumps({"follow": T}))
            await ws.recv()
        await chat.send(json.dumps({"react": T, "msg": "1", "key": "like", "remove": False}))
        assert await asyncio.to_thread(entered.wait, 5)
        await chat.send(json.dumps({"delete": T, "msg": "2"}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {
            "delete": T,
            "msg": "2",
            "error": "busy",
        }
        await chat.send(json.dumps({"edit": T, "msg": "2", "text": "x"}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {"edit": T, "msg": "2", "error": "busy"}
        await chat.send(json.dumps({"send": T, "text": "q", "quote": ["1"]}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {"sent": T, "error": "busy"}
        await chat.send(json.dumps({"send": T, "text": "plain"}))
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {"sent": T, "ok": True}
        await other.send(json.dumps({"delete": T, "msg": "2"}))
        assert json.loads(await asyncio.wait_for(other.recv(), 2)) == {"delete": T, "msg": "2", "ok": True}
        release.set()
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {"react": T, "msg": "1", "ok": True}
        await chat.send(json.dumps({"delete": T, "msg": "2"}))  # released: the next one runs
        assert json.loads(await asyncio.wait_for(chat.recv(), 2)) == {"delete": T, "msg": "2", "ok": True}
    assert _writes(calls) == [
        ("send", T, "plain", {}),
        ("delete", T, "2"),
        ("react", T, "1", "like", False),
        ("delete", T, "2"),
    ]


# --- phase 5: paging forward, around, search (011-AC17, 011-AC18) ---

TIED = [_at(1), _at(2), _at(3), _at(3), _at(3), _at(4), _at(5)]  # 3, 4 and 5 share one time


@pytest.fixture
def tied(tmp_path: Path, quiet_directory: Directory) -> W.Board:
    data = _archive(tmp_path / "d", T, [_m(str(i), t) for i, t in enumerate(TIED, 1)])
    return W.Board({}, quiet_directory, me=ME, data_dir=data)


async def test_history_after_pages_forward_without_overlap_or_gap(tied: W.Board, no_api: None) -> None:
    """011-AC17: walking `after` from the start returns every message once, `newer` false at the end;
    the page boundary falls inside the tied time and the tie joins the page."""
    pages: list[tuple[list[str], bool, bool]] = []
    answer = await _history(tied, T, after=_at(0), limit=3)
    while True:
        pages.append((_ids(answer), answer["older"], answer["newer"]))
        if not answer["newer"]:
            break
        answer = await _history(tied, T, after=answer["messages"][-1]["at"], limit=3)
    assert pages == [(["1", "2", "3", "4", "5"], False, True), (["6", "7"], True, False)]
    assert answer["after"] == _at(3) and "before" not in answer
    assert (await _history(tied, T, after=_at(5)))["messages"] == []


async def test_history_around_returns_both_sides_of_the_anchor(board: W.Board, no_api: None) -> None:
    """011-AC17: about limit/2 on each side, the anchor time on the newer side, flags at both ends; near
    an end the short side's share goes to the other, so the page stays full."""
    cases = [
        (_at(3), 2, ["2", "3"], True, True),
        ("2026-09-15T10:00:03.500Z", 2, ["3", "4"], True, True),  # between two messages
        (_at(1), 4, ["1", "2", "3", "4"], False, True),  # nothing before the first
        (_at(9), 4, ["2", "3", "4", "5"], True, False),  # nothing after the last
        (_at(2), 4, ["1", "2", "3", "4"], False, True),  # one before: three from the anchor on
        (_at(5), 4, ["2", "3", "4", "5"], True, False),  # one from the anchor: three before
        (_at(3), 100, ["1", "2", "3", "4", "5"], False, False),
    ]
    for anchor, limit, ids, older, newer in cases:
        answer = await _history(board, T, around=anchor, limit=limit)
        assert (_ids(answer), answer["older"], answer["newer"]) == (ids, older, newer), anchor
        assert (answer["around"], answer["source"]) == (anchor, "archive")
        assert "before" not in answer


async def test_history_around_keeps_a_tied_time_whole(tied: W.Board, no_api: None) -> None:
    """011-AC17: messages sharing the anchor's time all come back, then paging either way neither
    repeats nor skips one."""
    answer = await _history(tied, T, around=_at(3), limit=2)
    assert (_ids(answer), answer["older"], answer["newer"]) == (["2", "3", "4", "5"], True, True)
    back = await _history(tied, T, before=answer["messages"][0]["at"], limit=2)
    forth = await _history(tied, T, after=answer["messages"][-1]["at"], limit=2)
    assert (_ids(back), back["older"], _ids(forth), forth["newer"]) == (["1"], False, ["6", "7"], False)


async def test_history_flags_at_both_ends_of_the_latest_page(board: W.Board, no_api: None) -> None:
    """011-AC17: the latest page is never `newer`; an old `before` page is."""
    latest = await _history(board, T, limit=2)
    assert (latest["older"], latest["newer"]) == (True, False)
    first = await _history(board, T, before=_at(2), limit=2)
    assert (_ids(first), first["older"], first["newer"]) == (["1"], False, True)


@pytest.mark.parametrize("key", ["after", "around"])
@pytest.mark.parametrize("value", ["garbage", "2026-09-15", "x2026-09-15T10:00:00Z", 123, ["x"]])
async def test_history_refuses_a_malformed_after_or_around(
    board: W.Board, no_api: None, key: str, value: Any
) -> None:
    """011-AC17: `after` and `around` are checked like `before`, before any read or API call."""
    for thread in (T, B):
        assert await _history(board, thread, **{key: value}) == {
            "history": thread,
            key: value if isinstance(value, str) else None,
            "error": f"invalid {key}",
        }


async def test_history_refuses_two_cursors(board: W.Board, no_api: None) -> None:
    """011-AC17: one cursor per request; the answer echoes the first one."""
    assert await _history(board, T, before=_at(3), after=_at(1)) == {
        "history": T,
        "before": _at(3),
        "error": "one cursor at most",
    }


@pytest.mark.parametrize("key", ["after", "around"])
async def test_after_and_around_need_the_archive(board: W.Board, no_api: None, key: str) -> None:
    """011-AC17: the API source only serves the latest page and `before`: no API call for the others."""
    assert await _history(board, B, **{key: _at(3)}) == {
        "history": B,
        key: _at(3),
        "error": "not available without archive",
    }


async def test_history_cursor_echo_and_busy_over_the_socket(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC17: answers and `busy` refusals echo the cursor they were asked with."""
    real = CV.read_page

    def slow(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.3)
        return real(*args, **kwargs)

    monkeypatch.setattr(CV, "read_page", slow)
    async with _served(board, settings), _connect(board) as ws:
        await ws.recv()
        await ws.send(json.dumps({"history": T, "around": _at(3), "limit": 2}))
        await ws.send(json.dumps({"history": T, "after": _at(1)}))
        assert await _frame(ws) == {"history": T, "after": _at(1), "error": "busy"}
        answer = await _frame(ws)
        assert (answer["around"], _ids(answer)) == (_at(3), ["2", "3"])


@pytest.fixture
def searchable(tmp_path: Path, quiet_directory: Directory) -> W.Board:
    texts = ["Réunion demain", "nothing here", "la REUNION est annulée", "<p>réunion <b>finie</b></p>"]
    messages = [_m(str(i), _at(i), f"<p>{t}</p>" if "<" not in t else t) for i, t in enumerate(texts, 1)]
    data = _archive(tmp_path / "d", T, messages)
    _archive(data, C, None)  # indexed, never backfilled
    return W.Board({B: {"id": B, "last_activity": _at(1)}}, quiet_directory, me=ME, data_dir=data)


async def test_search_folds_case_and_accents_newest_first(searchable: W.Board, no_api: None) -> None:
    """011-AC18: case and accents ignored, newest first, hits as {id, at, name, text}."""
    answer = await _search(searchable, T, " reunion ")
    assert answer["search"] == T
    assert [h["id"] for h in answer["hits"]] == ["4", "3", "1"]
    assert answer["hits"][0] == {"id": "4", "at": _at(4), "name": "Ann Lee", "text": "réunion finie"}
    assert [h["id"] for h in (await _search(searchable, T, "ANNULÉE"))["hits"]] == ["3"]
    assert (await _search(searchable, T, "absent"))["hits"] == []


async def test_search_caps_at_fifty_newest(tmp_path: Path, quiet_directory: Directory, no_api: None) -> None:
    """011-AC18: at most 50 hits, the newest ones, over the whole history."""
    data = _archive(tmp_path / "d", T, [_m(str(i), _at(i), f"<p>hit {i}</p>") for i in range(120)])
    board = W.Board({}, quiet_directory, me=ME, data_dir=data)
    hits = (await _search(board, T, "HIT"))["hits"]
    assert [h["id"] for h in hits] == [str(i) for i in range(119, 69, -1)]


@pytest.mark.parametrize("thread", [B, C, NOPE])
async def test_search_without_archive_is_an_error(searchable: W.Board, no_api: None, thread: str) -> None:
    """011-AC18: on the board only, indexed without messages, or unknown: no archive, no API call."""
    assert await _search(searchable, thread, "x") == {
        "search": thread,
        "q": "x",
        "error": "no archive for this chat",
    }


async def test_search_with_no_data_dir(quiet_directory: Directory, no_api: None) -> None:
    """011-AC18"""
    board = W.Board({B: {"id": B, "last_activity": _at(1)}}, quiet_directory)
    assert (await _search(board, B, "x"))["error"] == "no archive for this chat"


@pytest.mark.parametrize("query", ["", "   ", None, 3])
async def test_search_refuses_an_empty_query(searchable: W.Board, query: Any) -> None:
    """011-AC18: nothing to look for is refused before any read."""
    assert (await _search(searchable, T, query))["error"] == "empty query"


async def test_search_runs_off_the_event_loop_and_failures_answer(
    searchable: W.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC18: the whole-history scan never blocks the loop; an unexpected failure is an answer."""
    loop_thread = threading.get_ident()
    threads: list[int] = []
    real = CV.search_chat

    def spy(*args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        threads.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(CV, "search_chat", spy)
    assert len((await _search(searchable, T, "reunion"))["hits"]) == 3
    assert threads and loop_thread not in threads

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.DatabaseError("disk image is malformed")

    monkeypatch.setattr(CV, "search_chat", broken)
    verb = {"search": T, "q": "reunion"}
    assert await _answered(CV._head_search(verb), searchable.chat.search_answer(verb)) == {
        "search": T,
        "q": "reunion",
        "error": "failed",
    }


async def test_search_over_the_socket_one_at_a_time(
    searchable: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """011-AC18: the verb answers the asker; a second search while one runs is refused `busy`."""
    real = CV.search_chat

    def slow(*args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        time.sleep(0.3)
        return real(*args, **kwargs)

    monkeypatch.setattr(CV, "search_chat", slow)
    async with _served(searchable, settings), _connect(searchable) as ws:
        await ws.recv()
        await ws.send(json.dumps({"search": T, "q": "reunion"}))
        await ws.send(json.dumps({"search": T, "q": "demain"}))
        assert await _frame(ws) == {"search": T, "q": "demain", "error": "busy"}
        assert [h["id"] for h in (await _frame(ws))["hits"]] == ["4", "3", "1"]


async def test_a_burst_at_one_time_keeps_the_page_bounded(
    tmp_path: Path, quiet_directory: Directory, no_api: None
) -> None:
    """011-AC17: messages tied with the page's edge join it, up to a cap: a burst at one exact time
    never makes a page of the whole burst."""
    burst = [_m(f"b{i:03d}", _at(5)) for i in range(300)]
    data = _archive(tmp_path / "d", T, [_m("1", _at(1)), *burst, _m("9", _at(9))])
    board = W.Board({}, quiet_directory, me=ME, data_dir=data)
    for answer in (
        await _history(board, T, limit=10),
        await _history(board, T, after=_at(1), limit=10),
        await _history(board, T, around=_at(5), limit=10),
    ):
        assert 10 <= len(answer["messages"]) <= 10 + 200


@pytest.mark.parametrize(
    "cursor",
    [
        "2026-09-15T10:00:00Zjunk",
        "2026-09-15T10:00:00.5Z; x",
        "2026-09-15T10:00:00+02:00",
        "2026-09-15T10:00",
    ],
)
async def test_history_refuses_trailing_junk_after_a_time(board: W.Board, no_api: None, cursor: str) -> None:
    """011-AC17: a cursor is a whole composetime, not one that merely starts like it."""
    for key in ("before", "after", "around"):
        assert (await _history(board, T, **{key: cursor}))["error"] == f"invalid {key}"


@pytest.mark.parametrize(
    "cursor", ["2026-09-15T10:00:03", "2026-09-15T10:00:03Z", "2026-09-15T10:00:03.1234567Z"]
)
async def test_history_accepts_every_composetime_shape(board: W.Board, no_api: None, cursor: str) -> None:
    """011-AC17: with or without fraction and Z, as the archive and the page's dates write them."""
    for key in ("before", "after", "around"):
        assert "error" not in await _history(board, T, **{key: cursor}), key


def _with_bad_rows(data: Path, times: list[str]) -> None:
    db = sqlite3.connect(data / T / "messages.db")
    with db:
        db.executemany(
            "INSERT INTO messages (id, composetime, raw) VALUES (?, ?, ?)",
            [(f"bad{i}", t, "{bad") for i, t in enumerate(times)],
        )
    db.close()


@pytest.mark.parametrize(
    ("good", "bad", "cursor", "ids", "older", "newer"),
    [
        ([1, 2, 3, 4, 5], [6, 7, 8, 9, 10], {}, ["1", "2", "3", "4", "5"], False, False),  # latest window
        ([1, 2, 8, 9], [3, 4, 5, 6, 7], {"before": _at(8)}, ["1", "2"], False, True),
        ([1, 7], [2, 3, 4, 5, 6], {"after": _at(1)}, ["7"], True, False),
    ],
)
async def test_a_window_of_unreadable_rows_is_read_past(
    tmp_path: Path,
    quiet_directory: Directory,
    no_api: None,
    good: list[int],
    bad: list[int],
    cursor: dict[str, str],
    ids: list[str],
    older: bool,
    newer: bool,
) -> None:
    """011-AC15 + 011-AC17: a page whose whole window is unreadable reads on instead of answering
    "empty, but more", which would have the page ask the same edge again."""
    data = _archive(tmp_path / "d", T, [_m(str(i), _at(i)) for i in good])
    _with_bad_rows(data, [_at(i) for i in bad])
    board = W.Board({}, quiet_directory, me=ME, data_dir=data)
    answer = await _history(board, T, limit=5, **cursor)
    assert (_ids(answer), answer["older"], answer["newer"]) == (ids, older, newer)


async def test_reading_past_unreadable_rows_is_bounded(
    tmp_path: Path, quiet_directory: Directory, no_api: None
) -> None:
    """011-AC15: five windows at most; past them the page gets an empty answer it does not repeat."""
    data = _archive(tmp_path / "d", T, [_m(str(i), _at(i)) for i in range(31, 36)])
    _with_bad_rows(data, [_at(i) for i in range(1, 31)])
    board = W.Board({}, quiet_directory, me=ME, data_dir=data)
    answer = await _history(board, T, before=_at(31), limit=5)
    assert (answer["messages"], answer["older"]) == ([], True)  # windows 26-30 down to 6-10
    assert _ids(await _history(board, T, before=_at(6), limit=5)) == []


@pytest.mark.parametrize("cursor", ["２０２６-09-15T10:00:00Z", "2026-09-15T10:00:0٣Z"])
async def test_history_refuses_non_ascii_digits(board: W.Board, no_api: None, cursor: str) -> None:
    """011-AC17: `\\d` would take any Unicode digit; a cursor is ASCII."""
    assert (await _history(board, T, after=cursor))["error"] == "invalid after"


# --- wire parsers (pure) ---


def test_parse_send_types_a_valid_verb_and_names_the_first_refusal() -> None:
    verb = {
        "send": T,
        "text": "hi",
        "quote": ["1", "2"],
        "mentions": [{"name": "A", "mri": ANN, "at": 1}],
        "format": "full",
    }
    want = CV.SendRequest(T, "hi", ("1", "2"), ({"name": "A", "mri": ANN, "at": 1},), True)
    assert CV.parse_send(verb) == want
    assert CV.parse_send({"send": T, "text": "hi"}) == CV.SendRequest(T, "hi")
    for extra, reason in (
        ({"text": " "}, "empty message"),
        ({"text": "<" * (compose.MAX_TEXT // 4 + 1)}, "message too long"),
        ({"quote": "1"}, "invalid quote"),
        ({"quote": ["1", "1"]}, "invalid quote"),
        ({"quote": ["x"]}, "invalid quote"),
        ({"quote": [str(n) for n in range(11)]}, "too many quotes"),
        ({"mentions": {"name": "A"}}, "invalid mention"),
        ({"mentions": [{"name": "A", "mri": ANN, "everyone": "yes", "at": 0}]}, "invalid mention"),
        ({"format": "md"}, "invalid format"),
        ({"send": 3}, "unknown chat"),
    ):
        assert CV.parse_send({"send": T, "text": "hi", **extra}) == reason, extra


@pytest.mark.parametrize(
    ("text", "at"),
    [("hi", None), ("hi", True), ("hi", 1.0), ("hi", "1"), ("hi", -1), ("hi", 2), ("😀@A", 1), ("😀@A", 4)],
)
def test_a_pick_anchor_must_be_an_offset_of_the_text(text: str, at: Any) -> None:
    """033: every pick is anchored at an offset of the text, in UTF-16 code units: missing, not an int, past
    the text or inside an emoji's surrogate pair, the verb is refused before any work."""
    pick = {"name": "A", "mri": ANN} | ({} if at is None else {"at": at})
    assert CV.parse_send({"send": T, "text": text, "mentions": [pick]}) == "invalid mention"
    edit = {"edit": T, "msg": "1", "text": text, "mentions": [pick], "keep_quotes": [], "keep_images": []}
    assert CV.parse_act("edit", edit) == "invalid mention"


@pytest.mark.parametrize("verb", ["send", "edit"])
def test_a_long_blank_text_beside_images_is_refused_before_its_picks_are_mapped(verb: str) -> None:
    """033 review L3: whitespace beside images skips the text check; a megabyte of it with a pick is refused
    as too long before any offset table is built over it."""
    text, pick = " " * 1_000_000, {"name": "A", "mri": ANN, "at": 0}
    start = time.process_time()
    if verb == "send":
        got = CV.parse_send(
            {"send": T, "text": text, "images": [{"w": 1, "h": 1, "size": 1}], "mentions": [pick]}
        )
    else:
        edit = {
            "edit": T,
            "msg": "1",
            "text": text,
            "mentions": [pick],
            "keep_quotes": [],
            "keep_images": ["0-weu-d1-a"],
        }
        got = CV.parse_act("edit", edit)
    assert got == "message too long" and time.process_time() - start < 0.1


@pytest.mark.parametrize(("text", "unit", "index"), [("😀@A", 2, 1), ("a😀𝐀 @A", 6, 4), ("@A", 0, 0)])
def test_an_anchor_crosses_from_the_pages_unit_to_the_texts(text: str, unit: int, index: int) -> None:
    """033: the page anchors a pick in UTF-16 code units (an emoji counts two), the server reads its text by
    character: the `@` after astral characters is the one anchored."""
    request = CV.parse_send({"send": T, "text": text, "mentions": [{"name": "A", "mri": ANN, "at": unit}]})
    assert isinstance(request, CV.SendRequest) and request.mentions[0]["at"] == index
    assert text[index] == "@" and compose.utf16_units(text)[index] == unit


def test_react_accepts_the_three_key_shapes_and_sends_teams_keys() -> None:
    """016-AC2, 016-AC3, 016-AC4: Teams names (toned too), Unicode keys and custom keys pass as given;
    bare code points become Teams' name or its `<hex>_<name>` key; anything else is refused."""

    def sent(key: Any) -> str:
        request = CV.parse_act("react", {"react": T, "msg": "1", "key": key})
        return request.key if isinstance(request, CV.ActRequest) else request

    custom = "party-parrot;0-wus-d1-0123456789abcdef0123456789abcdef"
    for key in (
        "like",
        "yes-tone1",
        "whew",
        "starMSER",
        "1f525_fire",
        "1f9d1-200d-1f4bb_technologist",
        "happy_person_raising_one_hand-tone3",  # catalog ids outside both shapes
        "50th_mail",
        custom,
    ):
        assert sent(key) == key
    for key, teams in (
        ("1f64f", "praying"),  # 🙏
        ("2764-fe0f", "heart"),  # ❤️, its presentation selector left out of the lookup
        ("1f44d", "like"),  # not `yes`
        ("1f643", "upsidedownface"),
        ("1f680", "launch"),  # Teams' only name for 🚀
        ("1f622", "sad"),  # the default reactions keep their names: 😢 is `sad`, not `cry`
        ("1f621", "angry"),
        ("1f44f", "clappinghands"),  # TEAMS_PLAIN: not 'sarcastic'
        ("1f918", "1f918_signofthehorns"),  # the character's own id before `rock`
        ("1f642", "smile"),  # catalog order: `smile` before `50th_smile`
        ("1f44a", "1f44a_oncomingfist"),  # 👊, not `fistbump` (🤜🤛)
        ("1f61b", "1f61b_facewithtongue"),  # 😛, not `tongueout` (😋)
        ("1f910", "1f910_zippermouthface"),  # 🤐, not `lipssealed` (🤫)
        ("1f620", "1f620_angryface"),  # 😠: no Teams name shows it (`angry` is 😡 here)
        ("1f98a", "foxhug"),  # 🦊: Teams' only fox
        ("2665-fe0f", "2665_blackheartsuit"),  # a final U+FE0F dropped, as Teams' own keys do
    ):
        assert sent(key) == teams, key
    for key in (
        "<img src=x>",
        "like<b>",
        'like"',
        "like tone",
        "a;",
        ";0-abc",
        "name;id;more",
        "1f64f_",
        "happy_person_raising_one_hand-tone6",
        "50th_mail-tone1-tone2",
        "d800-0041",  # starts with a letter: not a Unicode key, and not a name
        "0000",  # a control
        "200d",  # a lone joiner: no name to send
        "1f64f_" + "x" * REACTION_KEY_MAX,
        "a" * 65,
        "x;" + "a" * 129,
        "",
        None,
        7,
    ):
        assert sent(key) == "invalid reaction", key


def test_parse_act_checks_what_each_action_needs() -> None:
    assert CV.parse_act("react", {"react": T, "msg": "1", "key": "like"}) == CV.ActRequest(
        "react", T, "1", "like"
    )
    assert CV.parse_act("edit", {"edit": T, "msg": "1", "text": "x"}) == CV.ActRequest(
        "edit", T, "1", text="x"
    )
    assert CV.parse_act("delete", {"delete": T, "msg": "1", "text": 5}) == CV.ActRequest("delete", T, "1")
    for action, extra, reason in (
        ("delete", {"msg": "1a"}, "invalid message"),
        ("delete", {"msg": 1}, "invalid message"),
        ("react", {"key": "no pe"}, "invalid reaction"),
        ("react", {"key": "like", "remove": "yes"}, "invalid reaction"),
        ("edit", {"text": ""}, "empty message"),
    ):
        assert CV.parse_act(action, {action: T, "msg": "1", **extra}) == reason, extra


def test_parse_history_picks_one_cursor_and_bounds_the_size() -> None:
    assert CV.parse_history({"history": T}) == CV.HistoryRequest(T, "before", "", 100)
    assert CV.parse_history({"history": T, "before": "", "limit": 500}) == CV.HistoryRequest(
        T, "before", "", 200
    )
    assert CV.parse_history({"history": T, "around": _at(3), "limit": True}) == CV.HistoryRequest(
        T, "around", _at(3), 100
    )
    assert CV.parse_history({"history": T, "after": f"{_at(3)}x"}) == "invalid after"
    assert CV.parse_history({"history": T, "before": _at(3), "after": _at(1)}) == "one cursor at most"


def test_parse_search_complete_preview() -> None:
    assert CV.parse_search({"search": T, "q": " x "}) == CV.SearchRequest(T, "x")
    assert CV.parse_search({"search": T, "q": " "}) == "empty query"
    assert CV.parse_complete({"complete": "emoji", "q": "fi"}) == CV.CompleteRequest("emoji", "fi")
    assert CV.parse_complete({"complete": "member", "q": "", "thread": T}) == CV.CompleteRequest(
        "member", "", T
    )
    assert CV.parse_complete({"complete": "emoji", "q": ""}) == "invalid query"
    assert CV.parse_complete({"complete": "member", "q": "a" * 65, "thread": T}) == "invalid query"
    assert CV.parse_complete({"complete": "room", "q": "a"}) == "invalid kind"
    assert CV.parse_complete({"complete": "member", "q": "a"}) == "unknown chat"
    assert CV.parse_preview({"preview": "x"}) == CV.PreviewRequest("x", False)
    assert CV.parse_preview({"preview": "x", "format": "full"}) == CV.PreviewRequest("x", True)
    assert CV.parse_preview({"preview": None}) == "invalid preview"
    # L2: the composer's character cap, before any rendering work.
    assert CV.parse_preview({"preview": "x" * compose.MAX_TEXT}) == CV.PreviewRequest(
        "x" * compose.MAX_TEXT, False
    )
    assert CV.parse_preview({"preview": "x" * (compose.MAX_TEXT + 1)}) == "message too long"


def test_parse_open_maps_the_flags_to_one_target() -> None:
    assert W.parse_open({"open": T}) == ("app", "")
    assert W.parse_open({"open": T, "web": True, "msg": "7"}) == ("web", "7")
    assert W.parse_open({"open": T, "msg": "7x"}) == ("app", "")
    assert W.parse_open({"open": T, "chat": True, "web": True, "msg": "7"}) == ("chat", "")
    assert W.parse_open({"open": T, "chat": True, "chatnav": "split"}) == ("split", "")
    assert W.parse_open({"open": T, "chatnav": "split"}) == ("app", "")


# --- wave 2 review fixes ---


def _relayed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Frames the board relays to followers, decoded; row broadcasts left out."""
    frames: list[dict[str, Any]] = []

    def capture(sockets: Any, frame: str) -> None:
        if "thread" in (decoded := json.loads(frame)):
            frames.append(decoded)

    monkeypatch.setattr(W, "broadcast", capture)
    return frames


async def _settled(frames: list[Any], n: int) -> None:
    await wait(lambda: len(frames) >= n, f"{n} frames")


async def test_a_slow_media_folder_never_holds_the_stream(
    board: W.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1: on_event only queues the relay: a 3 s glob per record leaves the stream free, order kept."""
    frames = _relayed(monkeypatch)
    release = threading.Event()
    real = W.chat_record

    def slow(*args: Any, **kwargs: Any) -> dict[str, Any]:
        release.wait(3)  # the NFS media glob the review measured
        return real(*args, **kwargs)

    monkeypatch.setattr(W, "chat_record", slow)
    await _following(board, C)  # off the board: every frame is the relay's
    started = time.monotonic()
    await board.on_event(_event("NewMessage", C, id="9", composetime=_at(9), messagetype="Text", content="x"))
    await board.on_event(_event("NewMessage", C, messagetype="Control/Typing"))
    await board.on_event(
        _event("NewMessage", C, id="10", composetime=_at(10), messagetype="Text", content="y")
    )
    assert time.monotonic() - started < 0.5
    release.set()
    await _settled(frames, 4)
    kinds = [(next(k for k in ("msg", "typing") if k in f), f.get("on")) for f in frames]
    assert kinds == [("msg", None), ("typing", True), ("typing", False), ("msg", None)]
    assert [f["msg"]["id"] for f in frames if "msg" in f] == ["9", "10"]
    board.close()


async def test_the_relay_queue_is_bounded_and_says_once_what_it_drops(
    quiet_directory: Directory, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1 + L5: past the bound a relay is dropped, never awaited: the followers get one `gap` frame, the
    log one line per stall, and the count once the queue drains. The next stall says so again."""
    monkeypatch.setattr(W, "_RELAY_QUEUE", 2)
    rows = {B: {"id": B, "label": "B", "last_activity": _at(1), "last_id": "1", "typing": []}}
    board = W.Board(rows, quiet_directory, me=ME, data_dir=data_dir)
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(W, "broadcast", lambda sockets, frame: sent.append(json.loads(frame)))
    await _following(board, T)
    await _following(board, B)

    def burst(ids: range, thread: str = T) -> None:
        for i in ids:  # no await between them: the consumer cannot run
            resource = _event(
                "NewMessage", thread, id=str(i), composetime=_at(i), messagetype="Text", content="x"
            )
            forward = functools.partial(board._forward, thread, "NewMessage", resource["resource"], "Text")
            board._enqueue_relay(thread, forward)

    def relayed() -> list[str]:
        return [f["msg"]["id"] for f in sent if "msg" in f]

    with structlog.testing.capture_logs() as logs:
        burst(range(1, 6))
        burst(range(20, 21), B)
        assert sent == [{"gap": T}, {"gap": B}]
        await wait(lambda: any(e["event"] == "follow_relay_resumed" for e in logs), "the stall's end")
        # M5: once drained, every chat that lost a relay hears it again: its page resyncs past the hole.
        assert [("gap", f["gap"]) if "gap" in f else ("msg", f["msg"]["id"]) for f in sent] == [
            ("gap", T),
            ("gap", B),
            ("msg", "1"),
            ("msg", "2"),
            ("gap", T),
            ("gap", B),
        ]
        burst(range(6, 10))
        await wait(lambda: sum(e["event"] == "follow_relay_resumed" for e in logs) == 2, "the second end")
    assert relayed() == ["1", "2", "6", "7"]
    assert [f["gap"] for f in sent if "gap" in f] == [T, B, T, B, T, T]
    events = [(e["event"], e.get("dropped")) for e in logs if e["event"].startswith("follow_relay")]
    assert events == [
        ("follow_relay_stalled", None),
        ("follow_relay_resumed", 4),
        ("follow_relay_stalled", None),
        ("follow_relay_resumed", 2),
    ]
    board.close()
    assert board._relay_task is None


async def test_a_hung_media_folder_relays_without_media(
    board: W.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1: a record whose media read outlives the relay deadline is built without the folder, and the
    following records skip it for _MEDIA_BACKOFF: the queue keeps moving."""
    frames = _relayed(monkeypatch)
    release = threading.Event()
    seen: list[Any] = []
    real = W.chat_record

    def record(
        message: dict[str, Any],
        thread_id: str,
        me: str,
        media: Any,
        custom: Any,
        name_for: Any = None,
        images: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        seen.append(media)
        if media is not None:
            release.wait(5)
        return real(message, thread_id, me, media, custom, name_for, images, **kwargs)

    monkeypatch.setattr(W, "chat_record", record)
    monkeypatch.setitem(CV._DEADLINES, "relay", 0.05)
    await _following(board, T)  # archived: a media folder
    try:
        for i in (9, 10):
            await board.on_event(_event("NewMessage", T, id=str(i), composetime=_at(i), messagetype="Text"))
        await _settled(frames, 2)
    finally:
        release.set()
    assert [f["msg"]["id"] for f in frames] == ["9", "10"]
    assert seen[0] is not None and seen[1:] == [None, None]  # the timed-out read, its fallback, then none
    board.close()


async def test_a_live_activity_line_names_its_people(board: W.Board, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relayed system line asks the directory for the people it lacks; no private key goes out."""
    frames = _relayed(monkeypatch)
    asked: list[list[str]] = []

    async def resolve(mris: list[str], *, refresh: bool = False) -> None:
        asked.append(mris)
        board.directory.note_name(_BOB, "Bob Roe")

    assert board.directory is not None
    monkeypatch.setattr(board.directory, "resolve", resolve)
    await _following(board, C)
    removed = f"<deletemember><initiator>{ME}</initiator><target>{_BOB}</target></deletemember>"
    await board.on_event(_event("NewMessage", C, **_activity("9", "ThreadActivity/DeleteMember", removed)))
    await _settled(frames, 1)
    assert frames[0]["msg"]["html"] == "👥 You removed Bob Roe" and "_activity" not in frames[0]["msg"]
    assert asked == [[_BOB]]
    board.close()


async def test_a_relay_that_hangs_is_cut_and_the_next_one_goes(
    board: W.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1: a relay stuck past _RELAY_MAX (a name lookup) is abandoned, logged, and the queue moves on."""
    frames = _relayed(monkeypatch)
    monkeypatch.setattr(W, "_RELAY_MAX", 0.05)
    await _following(board, C)

    async def stuck() -> None:
        await asyncio.sleep(5)

    with structlog.testing.capture_logs() as logs:
        board._enqueue_relay(C, stuck)
        await board.on_event(
            _event("NewMessage", C, id="9", composetime=_at(9), messagetype="Text", content="x")
        )
        await _settled(frames, 1)
    assert [f["msg"]["id"] for f in frames] == ["9"]
    assert [e["error_type"] for e in logs if e["event"] == "follow_relay_failed"] == ["TimeoutError"]
    board.close()


async def test_archive_reads_have_their_own_bounded_pool() -> None:
    """H2 + M11: 40 archive reads hung on NFS take exactly _ARCHIVE_WORKERS archive threads; a Teams
    call's to_thread is not held; closing the pool does not wait and cancels the queued reads."""
    release = threading.Event()
    names: list[str] = []

    def hung(i: int) -> None:
        names.append(threading.current_thread().name)
        release.wait(5)

    reads = [asyncio.ensure_future(CV.io("dir", hung, i)) for i in range(40)]
    media = [asyncio.ensure_future(CV.io("media", hung, i)) for i in range(CV._MEDIA_READS + 1)]
    try:
        await wait(lambda: len(names) == CV._ARCHIVE_WORKERS + CV._MEDIA_READS, "the pools' threads busy")
        await asyncio.wait_for(asyncio.to_thread(lambda: None), 1)
        started = time.monotonic()
        CV.close_archive_pool()
        assert time.monotonic() - started < 0.5
        with pytest.raises(CV.ArchiveUnavailable, match="archive pool closed"):  # L5: a 503, not a 500
            CV.archive_pool("dir")
        with pytest.raises(CV.ArchiveUnavailable, match="archive pool closed"):
            await CV.io("dir", hung, 40)
    finally:
        release.set()
        outcomes = await asyncio.gather(*reads, return_exceptions=True)
        media_outcomes = await asyncio.gather(*media, return_exceptions=True)
    workers = CV._ARCHIVE_WORKERS + CV._MEDIA_READS
    assert len(names) == workers and len(set(names)) == workers
    assert all(name.startswith("archive") for name in names)
    cancelled = [o for o in outcomes if isinstance(o, asyncio.CancelledError)]
    assert len(cancelled) == 40 - CV._ARCHIVE_WORKERS
    media_cancelled = [isinstance(o, asyncio.CancelledError) for o in media_outcomes]
    assert media_cancelled == [False] * CV._MEDIA_READS + [True]


async def test_a_full_backlog_refuses_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """M2: with the workers hung and the backlog full of deadline-cancelled reads, a further read is
    refused at once and the queue stops growing."""
    monkeypatch.setattr(CV, "_BACKLOG", 3)
    monkeypatch.setitem(CV._DEADLINES, "dir", 0.1)
    release = threading.Event()
    try:
        timed_out = await asyncio.gather(
            *(CV.io("dir", release.wait, 5) for _ in range(CV._ARCHIVE_WORKERS + 3)), return_exceptions=True
        )
        assert all(isinstance(o, CV.ArchiveUnavailable) for o in timed_out)
        queue = CV.archive_pool("dir")._work_queue
        assert queue.qsize() == 3
        started = time.monotonic()
        with pytest.raises(CV.ArchiveUnavailable):
            await CV.io("dir", release.wait, 5)
        assert time.monotonic() - started < 0.05
        assert queue.qsize() == 3
    finally:
        release.set()


async def test_stuck_archive_threads_counts_media_threads() -> None:
    """M3: a read hung in the media pool is an archive thread too."""
    release, started = threading.Event(), threading.Event()

    def hung() -> bool:
        started.set()
        return release.wait(5)

    read = asyncio.ensure_future(CV.io("media", hung))
    try:
        # Not a thread count: a previous test's exiting thread would pass it before this read runs.
        await wait(started.is_set, "the media read running")
        assert await asyncio.to_thread(CV.stuck_archive_threads, 0.05) == 1
    finally:
        release.set()
    assert await read is True


async def test_media_reads_hold_two_threads_until_they_really_end(
    board: W.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H2 + H3: 6 hung media reads, in a burst or one after another, never take more than _MEDIA_READS
    threads, past their deadline too; each gives up at its deadline and the other archive reads stay free."""
    release = threading.Event()
    running: set[str] = set()
    most = 0

    def hung(path: str) -> None:
        nonlocal most
        running.add(path)
        most = max(most, len(running))
        release.wait(5)
        running.discard(path)

    monkeypatch.setattr(board.chat, "media_file", hung)
    monkeypatch.setitem(CV._DEADLINES, "media", 0.1)
    try:
        burst = await asyncio.gather(
            *(board.chat.media(f"/media/{T}/{i}") for i in range(3)), return_exceptions=True
        )
        assert all(isinstance(o, CV.ArchiveUnavailable) for o in burst)
        for i in range(3, 6):  # past the deadline: the two hung threads still hold the media pool
            refused = time.monotonic()
            with pytest.raises(CV.ArchiveUnavailable):
                await board.chat.media(f"/media/{T}/{i}")
            assert time.monotonic() - refused < 0.5
        assert await CV.io("dir", lambda: "free") == "free"  # the other archive pool stays free
        assert most == CV._MEDIA_READS == len(running)
    finally:
        release.set()
    await wait(lambda: not running, "the hung threads end")
    monkeypatch.setattr(board.chat, "media_file", lambda path: (b"x", "image/png"))
    assert await board.chat.media(f"/media/{T}/again") == (b"x", "image/png")


async def test_a_third_media_request_answers_503_at_its_deadline(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H2: over HTTP, with both media threads on hung reads, the next image is a 503 at its deadline."""
    release = threading.Event()
    started_reads: list[str] = []

    def hung(path: str) -> None:
        started_reads.append(path)
        release.wait(5)

    monkeypatch.setattr(board.chat, "media_file", hung)
    monkeypatch.setitem(CV._DEADLINES, "media", 0.1)
    try:
        async with _served(board, settings) as host, httpx.AsyncClient(headers=_cookie(board)) as client:
            hung_gets = [
                asyncio.ensure_future(client.get(f"http://{host}/media/{quote(T, safe='')}/{i}.png"))
                for i in (1, 2)
            ]
            await wait(lambda: len(started_reads) == 2, "two reads running")
            started = time.monotonic()
            got = await client.get(f"http://{host}/media/{quote(T, safe='')}/3.png")
            assert (got.status_code, time.monotonic() - started < 0.5) == (503, True)
            release.set()
            assert [r.status_code for r in await asyncio.gather(*hung_gets)] == [503, 503]  # past deadline
    finally:
        release.set()


async def test_io_rethrows_a_timeout_of_its_own_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """L8: a TimeoutError raised by the work itself is not the deadline: it stays a TimeoutError."""

    def times_out() -> None:
        raise TimeoutError("socket")

    with pytest.raises(TimeoutError, match="socket") as exc:
        await CV.io("dir", times_out)
    assert not isinstance(exc.value, CV.ArchiveUnavailable)


async def test_archive_unavailable_is_logged_once_per_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """M8: a dead mount fails every read; one `archive_unavailable` warning per 30 s says so, with the
    kind and how many archive threads are busy."""
    release = threading.Event()
    monkeypatch.setitem(CV._DEADLINES, "dir", 0.02)
    monkeypatch.setattr(CV, "_unavailable_warned", -CV._UNAVAILABLE_EVERY)
    try:
        with structlog.testing.capture_logs() as logs:
            for _ in range(3):
                with pytest.raises(CV.ArchiveUnavailable):
                    await CV.io("dir", release.wait, 5)
            monkeypatch.setattr(CV, "_unavailable_warned", CV._unavailable_warned - CV._UNAVAILABLE_EVERY)
            with pytest.raises(CV.ArchiveUnavailable):
                await CV.io("dir", release.wait, 5)
    finally:
        release.set()
    warned = [e["kind"] for e in logs if e["event"] == "archive_unavailable"]
    assert warned == ["dir", "dir"]


async def test_board_close_leaves_the_pool_to_the_process(board: W.Board) -> None:
    """M6: closing a board stops its relays only; the archive pool is closed once, by run()."""
    await _following(board, T)
    board._enqueue_relay(T, lambda: asyncio.sleep(0))
    task = board._relay_task
    board.close()
    assert board._relay_task is None and task is not None
    assert await CV.io("dir", lambda: 1) == 1


async def test_a_hung_archive_answers_archive_unavailable(
    board: W.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H2: history, search and follow past their deadline answer `archive unavailable`; the stuck
    threads stay, the callers do not wait."""
    release = threading.Event()

    def hung(*args: Any, **kwargs: Any) -> None:
        release.wait(5)

    for kind in ("dir", "history", "search"):
        monkeypatch.setitem(CV._DEADLINES, kind, 0.05)
    try:
        monkeypatch.setattr(CV, "read_page", hung)
        assert (await _history(board, T))["error"] == "archive unavailable"
        monkeypatch.setattr(CV, "search_chat", hung)
        assert await _search(board, T, "x") == {"search": T, "q": "x", "error": "archive unavailable"}
        monkeypatch.setattr(CV, "find_chat_dir", hung)
        monkeypatch.setattr(W, "archive_dir", hung)
        assert (await _history(board, T))["error"] == "archive unavailable"
        ws: Any = object()
        assert await board.follow(ws, T) == {"follow": T, "error": "archive unavailable"}
    finally:
        release.set()


def test_index_label_survives_a_broken_or_missing_index(data_dir: Path, tmp_path: Path) -> None:
    """L10: the index names an archived chat; no data dir, no index or an unreadable one is ""."""
    assert CV.index_label(data_dir, T) == "Chat"
    assert CV.index_label(None, T) == ""
    assert CV.index_label(tmp_path / "nowhere", T) == ""
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "index.db").write_bytes(b"not a database" * 100)
    assert CV.index_label(broken, T) == ""
    assert CV.archive_dir(None, T) is None and CV.archive_dir(broken, T) is None


async def test_a_failing_verb_answers_failed_and_keeps_the_socket(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M11: a background verb that raises is logged once and answered `failed` with its head; a
    synchronous one is logged; the socket stays either way."""

    async def broken(*args: Any) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(board.chat, "history_answer", broken)
    monkeypatch.setitem(board._handlers, "board", broken)
    with structlog.testing.capture_logs() as logs:
        async with _served(board, settings), _chat_socket(board) as ws:
            await ws.send(json.dumps({"history": T, "after": _at(1)}))
            assert await _frame(ws) == {"history": T, "after": _at(1), "error": "failed"}
            await ws.send(json.dumps({"board": True}))
            await ws.send(json.dumps({"follow": T}))
            assert (await _frame(ws)).items() >= {"follow": T, "ok": True}.items()
    failed = [e for e in logs if e["event"] == "verb_failed"]
    assert [(e["verb"], e["error_type"]) for e in failed] == [
        ("history", "RuntimeError"),
        ("board", "RuntimeError"),
    ]
    assert all(e["exc_info"] is True for e in failed)  # L5: the traceback, one JSON field


async def test_the_read_verb_marks_the_chat_read(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L8: `read` tells Teams the row is read up to its last message; an unknown chat calls nothing."""
    marked: list[tuple[str, str]] = []
    monkeypatch.setattr(W, "mark_read", lambda s, token, thread, msg: marked.append((thread, msg)))
    async with _served(board, settings), _connect(board) as ws:
        await ws.recv()
        await ws.send(json.dumps({"read": NOPE}))
        await ws.send(json.dumps({"read": B}))
        await wait(lambda: bool(marked), "mark_read called")
    assert marked == [(B, "1")]


OLD = "1789466000000"  # far older than the latest API page


@pytest.fixture
def old_page(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """The API holds my message OLD only in the page ending at it; the latest page lacks it."""
    mine = {"from": f"https://h/v1/users/ME/contacts/{ME}", "imdisplayname": "Me Self"}
    old = _m(OLD, _at(1), "<p>old</p>", **mine)

    def fake_history(
        s: Any, token: str, thread_id: str, size: int, pages: int, end: int | None = None
    ) -> list[dict[str, Any]]:
        calls.append(("history", thread_id, end))
        return [old] if end == int(OLD) // 1000 + 1 else [_m("1789467000000", _at(9))]

    monkeypatch.setattr(CV, "fetch_history", fake_history)
    return calls


async def test_old_messages_of_an_api_only_chat_stay_actionable(
    act_board: W.Board, old_page: list[tuple[Any, ...]]
) -> None:
    """M5: past the latest page, quote/edit/delete look up the page ending at the message; react needs
    no copy at all."""
    assert (await act_board.chat.act_answer(None, "react", {"react": B, "msg": OLD, "key": "like"}))["ok"]
    assert old_page == [("react", B, OLD, "like", False)]
    assert (await act_board.chat.act_answer(None, "edit", {"edit": B, "msg": OLD, "text": "x"}))["ok"]
    assert (await act_board.chat.act_answer(None, "delete", {"delete": B, "msg": OLD}))["ok"]
    assert await _send(act_board, B, "re", None, [OLD]) is None
    lookups = [c for c in old_page if c[0] == "history"]
    assert lookups == [("history", B, None), ("history", B, int(OLD) // 1000 + 1)] * 3
    assert old_page[-1][2] == _block(OLD, ME, "Me Self", "old") + "re"
    assert (await act_board.chat.act_answer(None, "edit", {"edit": B, "msg": "1789465000000", "text": "x"}))[
        "error"
    ] == "unknown message"


async def test_follow_names_an_archived_channel_from_the_index(board: W.Board) -> None:
    """M6: a chat outside the seed (a channel) takes the index's label, not its raw id."""
    assert (await board.follow(object(), C))["label"] == "Chat"  # type: ignore[arg-type]


async def test_unreadable_windows_hand_the_page_a_cursor_to_go_on(
    tmp_path: Path, quiet_directory: Directory, no_api: None
) -> None:
    """L4: past the bounded read-ahead the answer carries where it stopped; following it reaches the
    messages beyond, both ways."""
    data = _archive(tmp_path / "d", T, [_m(str(i), _at(i)) for i in (1, 32)])
    _with_bad_rows(data, [_at(i) for i in range(2, 32)])
    board = W.Board({}, quiet_directory, me=ME, data_dir=data)
    answer = await _history(board, T, before=_at(32), limit=5)
    assert (answer["messages"], answer["older"], answer["cursor"]) == ([], True, _at(7))
    assert _ids(await _history(board, T, before=answer["cursor"], limit=5)) == ["1"]
    answer = await _history(board, T, after=_at(1), limit=5)
    assert (answer["messages"], answer["newer"], answer["cursor"]) == ([], True, _at(26))
    answer = await _history(board, T, after=answer["cursor"], limit=5)
    assert (_ids(answer), "cursor" in answer) == (["32"], False)


class _Sink:
    """A socket that keeps what it is sent."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send(self, data: str) -> None:
        self.frames.append(json.loads(data))


async def test_busy_answers_echo_their_verb_like_the_real_answers(board: W.Board) -> None:
    """M7: one head per verb, busy or not: `q` only when it is a string, the history cursor as asked."""
    ws: Any = _Sink()
    chat = board.chat
    chat._search_busy.add(ws)
    chat._history_busy.add(ws)
    await chat._on_search(ws, {"search": T, "q": 5})
    await chat._on_search(ws, {"search": T, "q": "x"})
    await chat._on_history(ws, {"history": T, "around": _at(3)})
    await wait(lambda: len(ws.frames) == 3, "three busy answers")
    assert ws.frames == [
        {"search": T, "q": None, "error": "busy"},
        {"search": T, "q": "x", "error": "busy"},
        {"history": T, "around": _at(3), "error": "busy"},
    ]
    assert await chat.search_answer({"search": T, "q": 5}) == {"search": T, "q": None, "error": "empty query"}
    chat._history_busy.clear()
    await chat._on_history(ws, {"history": T, "around": _at(3), "limit": 1})
    await wait(lambda: len(ws.frames) == 4, "the history answer")
    assert ws.frames[3]["around"] == _at(3) and "messages" in ws.frames[3]
    assert chat._history_busy == set()  # the slot is freed once answered


def test_verb_keys_match_their_declared_value_shapes(board: W.Board) -> None:
    """L7: a thread id string by default; `board` only with true; ANY_VALUE keys on presence alone."""
    assert CV.ANY_VALUE == {"preview"}

    def key(verb: dict[str, Any]) -> str | None:
        return W._verb_key(board._handlers, board._accepts, verb)

    assert key({"preview": None}) == "preview" and key({"preview": 3}) == "preview"
    assert key({"board": True}) == "board"
    assert key({"board": "x"}) is None and key({"board": 1}) is None
    assert key({"send": 3}) is None and key({"send": T}) == "send"
    assert key({"seen": T, "preview": "x"}) == "seen"  # dispatch order: the first listed wins
    assert key({}) is None


async def test_a_hung_archive_falls_back_to_the_api_for_server_copies(
    board: W.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M13: the server's copy of a quoted or acted-on message comes from the API when the archive read
    outlives its deadline."""
    release = threading.Event()
    monkeypatch.setitem(CV._DEADLINES, "history", 0.05)
    monkeypatch.setattr(board.chat, "_archived_copies", lambda *a: release.wait(5) and {})
    asked: list[list[str]] = []

    def api(thread_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        asked.append(ids)
        return {i: {"id": i, "from": "api"} for i in ids}

    monkeypatch.setattr(board.chat, "_api_copies", api)
    try:
        assert await board.chat._server_copies(T, ["1", "2"]) == {
            "1": {"id": "1", "from": "api"},
            "2": {"id": "2", "from": "api"},
        }
    finally:
        release.set()
    assert asked == [["1", "2"]]


def test_the_board_takes_every_chat_verb_from_chat_verbs(board: W.Board) -> None:
    """M12: one source for the chat verbs; the board's own come first and last, in dispatch order."""
    chat = list(board.chat.handlers())
    assert list(board._handlers) == [
        "seen",
        "unseen",
        "read",
        "mute",
        "unmute",
        # spec 026: chat sections
        *(f"section_{v}" for v in ("new", "rename", "order", "move", "delete", "file", "chat_move")),
        *chat,
        "board",
        "follow",
        "dm",  # spec 023: my 1:1 with a sender
        "open",
        "authors",  # spec 015: a chat page's authors, for their presence
    ]


# --- my 1:1 with a sender (spec 023) ---


def _dm(a: str, b: str) -> str:
    return f"19:{a.removeprefix('8:orgid:')}_{b.removeprefix('8:orgid:')}@unq.gbl.spaces"


def _row_of(thread_id: str) -> dict[str, dict[str, Any]]:
    return {thread_id: {"id": thread_id, "label": "Ann Lee", "last_activity": _at(1), "seen_at": None}}


@pytest.mark.parametrize("order", ["me first", "them first"])
async def test_dm_answers_a_board_row_in_either_order(
    tmp_path: Path, quiet_directory: Directory, no_api: None, order: str
) -> None:
    """023-AC2/AC5: a 1:1 on the board, either id order, answered from the rows: no index read (there is
    none), no Teams call; an MRI in upper case finds it too."""
    dm = _dm(ME, ANN) if order == "me first" else _dm(ANN, ME)
    board = W.Board(_row_of(dm), quiet_directory, me=ME, data_dir=tmp_path / "missing")
    for mri in (ANN, ANN.upper().replace("8:ORGID:", "8:orgid:")):
        assert await board.dm(mri) == {"dm": mri, "chat": dm}


@pytest.mark.parametrize("order", ["me first", "them first"])
async def test_dm_answers_the_archive_index_in_either_order(
    tmp_path: Path, quiet_directory: Directory, no_api: None, order: str
) -> None:
    """023-AC2/AC5: a 1:1 only in the archive index, either id order."""
    dm = _dm(ME, ANN) if order == "me first" else _dm(ANN, ME)
    board = W.Board({}, quiet_directory, me=ME, data_dir=_archive(tmp_path / "d", dm, None))
    assert await board.dm(ANN) == {"dm": ANN, "chat": dm}


async def test_dm_without_a_one_to_one_says_so(
    tmp_path: Path, quiet_directory: Directory, data_dir: Path, no_api: None
) -> None:
    """023-AC5: no 1:1 on the board nor in the archive, or no archive at all: "no private chat"; someone
    else's 1:1 with them is not mine."""
    other = "8:orgid:00000000-0000-0000-0000-00000000000c"
    for board in (
        W.Board(_row_of(_dm(other, ANN)), quiet_directory, me=ME, data_dir=data_dir),
        W.Board({}, quiet_directory, me=ME),
    ):
        assert await board.dm(ANN) == {"dm": ANN, "error": "no private chat with them yet"}


@pytest.mark.parametrize(
    "mri", ["", "28:bot-id", "8:orgid:not-a-guid", ME, ME.upper().replace("8:ORGID:", "8:orgid:")]
)
async def test_dm_of_no_person_or_of_me_is_unknown(
    quiet_directory: Directory, data_dir: Path, no_api: None, mri: str
) -> None:
    """023-AC5: no org user, or me (I am excluded): "unknown person", nothing looked up."""
    board = W.Board(_row_of(_dm(ME, ME)), quiet_directory, me=ME, data_dir=data_dir)
    assert await board.dm(mri) == {"dm": mri, "error": "unknown person"}


async def test_dm_without_my_own_id_is_unknown(quiet_directory: Directory, no_api: None) -> None:
    """023-AC5: a server that does not know me cannot name our 1:1."""
    board = W.Board(_row_of(_dm(ME, ANN)), quiet_directory)
    assert await board.dm(ANN) == {"dm": ANN, "error": "unknown person"}


@pytest.mark.parametrize("archive", ["no folder", "no index", "unreadable index"])
async def test_dm_without_a_readable_archive_index_finds_none(
    tmp_path: Path, quiet_directory: Directory, no_api: None, archive: str
) -> None:
    """023-AC5: a data dir without a readable index is no 1:1 there, not a failure."""
    data = tmp_path / "d"
    if archive != "no folder":
        data.mkdir()
    if archive == "unreadable index":
        (data / "index.db").write_text("not a database")
    board = W.Board({}, quiet_directory, me=ME, data_dir=data)
    assert await board.dm(ANN) == {"dm": ANN, "error": "no private chat with them yet"}


async def test_dm_with_the_archive_stuck_says_so(
    tmp_path: Path, quiet_directory: Directory, monkeypatch: pytest.MonkeyPatch, no_api: None
) -> None:
    """023-AC5: an archive read past its deadline: "archive unavailable"."""

    async def stuck(*_: Any) -> Any:
        raise CV.ArchiveUnavailable("archive unavailable (dir)")

    monkeypatch.setattr(W, "io", stuck)
    board = W.Board({}, quiet_directory, me=ME, data_dir=tmp_path / "d")
    assert await board.dm(ANN) == {"dm": ANN, "error": "archive unavailable"}


async def test_dm_verb_on_the_chat_socket(
    tmp_path: Path, quiet_directory: Directory, settings: Settings, no_api: None
) -> None:
    """023-AC5: the page's `dm` verb answers on its socket, the chat under `chat` (a `thread` key would be
    dropped by a page showing another chat); no archive index answers "no private chat", no failure."""
    dm = _dm(ANN, ME)
    board = W.Board(_row_of(dm), quiet_directory, me=ME, data_dir=tmp_path / "missing")
    cy = "8:orgid:00000000-0000-0000-0000-00000000000c"
    with structlog.testing.capture_logs() as logs:
        async with _served(board, settings), board_socket(_host(board), board.secret, chat=True) as ws:
            await ws.send(json.dumps({"dm": ANN}))
            assert await _frame(ws) == {"dm": ANN, "chat": dm}
            await ws.send(json.dumps({"dm": cy}))
            assert await _frame(ws) == {"dm": cy, "error": "no private chat with them yet"}
    assert [e for e in logs if e["event"] == "verb_failed"] == []
