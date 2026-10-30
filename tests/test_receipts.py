"""Recipient read receipts (spec 011 amendment, phase 2): live event, snapshot, max-merge, cache, board.

Fixtures are the spike's sanitized shapes (`misc/presence-spike/receipts-result.json`) with synthetic
MRIs and chat ids; a mocked chat service (httpx.MockTransport), no network."""

import asyncio
import json
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import test_web_chat
from test_web_chat import ANN, ME, T, _at, _connect, _frame, _served

from miniteams import presence as P
from miniteams import receipts as R
from miniteams import web as W
from miniteams.config import Settings
from miniteams.directory import Directory

BOB = "8:orgid:00000000-0000-0000-0000-00000000000c"
CID = "8:orgid:00000000-0000-0000-0000-00000000000d"
DAN = "8:orgid:00000000-0000-0000-0000-00000000000e"
G = "19:0123456789abcdef0123456789abcdef@thread.v2"
board, data_dir, quiet_directory = test_web_chat.board, test_web_chat.data_dir, test_web_chat.quiet_directory


def _live(member: str, horizon: str, thread_id: str = G, messagetype: str = R.EVENT) -> dict[str, Any]:
    """The spike's live delivery resource, `NewMessage` on `/messaging`."""
    content = {
        "user": member,
        "consumptionhorizon": horizon,
        "messageVisibilityTime": 1704206918393,
        "version": "1791926762851",
    }
    return {
        "messagetype": messagetype,
        "type": "Message",
        "isactive": False,
        "from": f"https://h/v1/users/ME/conversations/{thread_id}",
        "conversationLink": f"https://h/v1/users/ME/conversations/{thread_id}",
        "originalarrivaltime": "2026-10-13T19:05:21.3480000Z",
        "content": json.dumps(content),
        "inQuarantine": False,
    }


def _snapshot(*members: tuple[str, str], thread_id: str = G) -> dict[str, Any]:
    """The spike's `GET /v1/threads/<id>/consumptionhorizons` answer."""
    return {
        "id": thread_id,
        "version": "1791928233065",
        "consumptionhorizons": [
            {"id": m, "consumptionhorizon": h, "messageVisibilityTime": 1704546353454} for m, h in members
        ],
    }


class ChatService:
    """Answers the horizons snapshot per thread; records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: dict[str, dict[str, Any]] = {}
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status)
        thread_id = request.url.path.removeprefix("/v1/threads/").removesuffix("/consumptionhorizons")
        return httpx.Response(200, json=self.answers.get(thread_id, _snapshot(thread_id=thread_id)))


@pytest.fixture
def chat_service() -> ChatService:
    return ChatService()


def _receipts(
    directory: Directory, service: ChatService, cache: P.Cache | None = None
) -> tuple[R.Receipts, list[tuple[str, dict[str, Any]]]]:
    sent: list[tuple[str, dict[str, Any]]] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(service.handler))
    receipts = R.Receipts(directory, ME, lambda t, f: sent.append((t, f)), cache, client)
    return receipts, sent


async def _settle(receipts: R.Receipts) -> None:
    while receipts._tasks:
        await asyncio.gather(*receipts._tasks)


def _ids(frame: dict[str, Any]) -> dict[str, str]:
    return {m: r["id"] for m, r in frame["receipts"].items()}


# --- pure parsing ---


def test_parse_event_reads_member_and_message_boundary() -> None:
    """The member is `content.user`, the boundary the first horizon field (not the read time)."""
    assert R.parse_event(_live(ANN, "1791918321348;1791926765519;1744182453429526799")) == (
        ANN.lower(),
        1791918321348,
    )
    assert R.parse_event(_live(ANN.upper(), "5;6;7")) == (ANN.lower(), 5)


@pytest.mark.parametrize(
    "resource",
    [
        _live(ANN, "5;6;7", messagetype="ThreadActivity/TopicUpdate"),
        _live(ANN, "5;6;7", messagetype="ThreadActivity/AddMember"),
        _live(ANN, "5;6;7", messagetype="RichText/Html"),
        {**_live(ANN, "5;6;7"), "content": "not json"},
        {**_live(ANN, "5;6;7"), "content": "[1, 2]"},
        {**_live(ANN, "5;6;7"), "content": json.dumps({"consumptionhorizon": "5;6;7"})},
        {**_live(ANN, "5;6;7"), "content": None},
        _live("", "5;6;7"),
        _live(ANN, "abc;6;7"),
        _live(ANN, "0;0;0"),
        _live(ANN, "-5;6;7"),
        _live(ANN, ""),
        _live(ANN, "٣;6;7"),  # a non-ASCII digit isdigit() but is no message id
        _live(ANN, "9223372036854775808;6;7"),  # 2**63: no SQLite INTEGER holds it
        _live(ANN, "1" * 5000 + ";6;7"),  # past int()'s digit limit
    ],
)
def test_parse_event_ignores_unrelated_and_malformed(resource: dict[str, Any]) -> None:
    assert R.parse_event(resource) is None


def test_an_eighteen_digit_horizon_is_a_message_id() -> None:
    assert R.parse_event(_live(ANN, "9" * 18 + ";6;7")) == (ANN.lower(), int("9" * 18))


def test_parse_snapshot_keeps_only_members_with_a_valid_horizon() -> None:
    body = _snapshot((ANN, "100;1;1"), (ME, "300;1;1"), (BOB, "garbage"), ("", "5;1;1"))
    body["consumptionhorizons"] += [{"id": CID}, "junk", None]
    assert R.parse_snapshot(body) == {ANN.lower(): 100, ME.lower(): 300}
    junks: list[Any] = [None, [], {"consumptionhorizons": "x"}, {"consumptionhorizons": None}, "x"]
    for junk in junks:
        assert R.parse_snapshot(junk) == {}


# --- state ---


async def test_live_horizons_only_move_forward_and_ours_never_shows(
    directory: Directory, chat_service: ChatService
) -> None:
    """011-AC3: an out-of-order event never regresses a confirmed receipt; our own read is never shown."""
    receipts, sent = _receipts(directory, chat_service)
    directory.note_name(ANN.lower(), "Ann Lee")
    receipts.open(G)
    await _settle(receipts)
    sent.clear()
    receipts.on_event(G, _live(ANN, "200;1;1"))
    assert [(t, _ids(f)) for t, f in sent] == [(G, {ANN.lower(): "200"})]
    assert sent[-1][1]["receipts"][ANN.lower()]["name"] == "Ann Lee"
    receipts.on_event(G, _live(ANN, "100;1;1"))  # late: older than what is shown
    receipts.on_event(G, _live(ANN, "200;1;1"))  # replayed
    receipts.on_event(G, _live(ME, "900;1;1"))
    receipts.on_event(G, _live(ME.upper(), "901;1;1"))
    assert len(sent) == 1 and receipts.shown[G] == {ANN.lower(): 200}
    receipts.on_event(G, _live(ANN, "201;1;1"))
    assert _ids(sent[-1][1]) == {ANN.lower(): "201"}
    await receipts.close()


async def test_our_horizon_is_left_out_whatever_the_case_of_our_mri(
    directory: Directory, chat_service: ChatService
) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(chat_service.handler))
    receipts = R.Receipts(directory, ME.upper(), lambda t, f: sent.append((t, f)), None, client)
    chat_service.answers[G] = _snapshot((ME, "9;1;1"), (ANN, "8;1;1"))
    receipts.open(G)
    await _settle(receipts)
    receipts.on_event(G, _live(ME, "10;1;1"))
    assert receipts.shown[G] == {ANN.lower(): 8} and _ids(sent[-1][1]) == {ANN.lower(): "8"}
    await receipts.close()


async def test_open_snapshots_the_chat_one_to_one_and_partial_group(
    directory: Directory, chat_service: ChatService
) -> None:
    """011-AC5: a 1:1 shows the peer only (we are left out); a partial group shows the confirmed members
    only, an absent member stays unknown. One GET on the chat host, nothing else."""
    dm = f"19:{ME.removeprefix('8:orgid:')}_{ANN.removeprefix('8:orgid:')}@unq.gbl.spaces"
    chat_service.answers[dm] = _snapshot((ANN, "10;1;1"), (ME, "12;1;1"), thread_id=dm)
    chat_service.answers[G] = _snapshot((ANN, "20;1;1"), (BOB, "18;1;1"), (ME, "21;1;1"))  # CID, DAN: absent
    receipts, sent = _receipts(directory, chat_service)
    receipts.open(dm)
    receipts.open(G)
    assert [(t, f) for t, f in sent] == [(dm, {"receipts": {}}), (G, {"receipts": {}})]  # nothing known yet
    await _settle(receipts)
    last = {t: _ids(f) for t, f in sent}
    assert last == {dm: {ANN.lower(): "10"}, G: {ANN.lower(): "20", BOB.lower(): "18"}}
    assert {(r.method, r.url.host, r.url.raw_path.decode()) for r in chat_service.requests} == {
        ("GET", directory.settings.contacts_host, f"/v1/threads/{quote(t, safe='')}/consumptionhorizons")
        for t in (dm, G)
    }
    await receipts.close()


async def test_a_snapshot_never_regresses_a_live_receipt_and_a_failure_keeps_what_is_known(
    directory: Directory, chat_service: ChatService
) -> None:
    receipts, sent = _receipts(directory, chat_service)
    chat_service.answers[G] = _snapshot((ANN, "50;1;1"))
    receipts.open(G)
    receipts.on_event(G, _live(ANN, "70;1;1"))  # arrives while the GET is in flight
    await _settle(receipts)
    assert receipts.shown[G] == {ANN.lower(): 70}
    chat_service.status = 401
    receipts.resync()
    await _settle(receipts)
    assert receipts.shown[G] == {ANN.lower(): 70} and _ids(sent[-1][1]) == {ANN.lower(): "70"}
    await receipts.close()


async def test_events_of_chats_not_followed_reach_the_cache_only(
    settings: Settings, directory: Directory, chat_service: ChatService
) -> None:
    cache = P.open_cache(settings)
    assert cache is not None
    receipts, sent = _receipts(directory, chat_service, cache)
    receipts.on_event(G, _live(ANN, "40;1;1"))
    assert sent == [] and receipts.shown == {}
    assert cache.horizons(G) == {ANN.lower(): 40}
    await receipts.close()
    cache.close()


async def test_an_oversized_horizon_is_dropped_and_the_other_members_still_show(
    settings: Settings, directory: Directory, chat_service: ChatService
) -> None:
    """A horizon too long for int() or SQLite's int64 must not kill the snapshot nor the live path."""
    cache = P.open_cache(settings)
    assert cache is not None
    receipts, sent = _receipts(directory, chat_service, cache)
    chat_service.answers[G] = _snapshot(
        (BOB, "9223372036854775808;1;1"), (CID, "1" * 5000 + ";1;1"), (ANN, "7;1;1")
    )
    receipts.open(G)
    await _settle(receipts)
    receipts.on_event(G, _live(DAN, "9223372036854775808;1;1"))
    receipts.on_event(G, _live(DAN, "2" * 5000 + ";1;1"))
    receipts.on_event(G, _live(BOB, "8;1;1"))
    assert receipts.shown[G] == {ANN.lower(): 7, BOB.lower(): 8}
    assert _ids(sent[-1][1]) == {ANN.lower(): "7", BOB.lower(): "8"}
    assert cache.horizons(G) == {ANN.lower(): 7, BOB.lower(): 8}
    await receipts.close()
    cache.close()


async def test_asks_during_a_snapshot_fold_into_one_more_get(directory: Directory) -> None:
    """Asks during a chat's GET fold into one more GET after it; a quiet chat is not asked again."""
    asked: list[str] = []
    answer = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.raw_path.decode())
        await answer.wait()
        return httpx.Response(200, json=_snapshot())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    receipts = R.Receipts(directory, ME, lambda t, f: None, None, client)
    chats = [G, "19:b@thread.v2", "19:c@thread.v2"]
    for thread_id in chats:
        receipts.open(thread_id)
    for _ in range(5):
        receipts.resync()
        await asyncio.sleep(0)
    for _ in range(20):
        receipts.open(G)  # chat switches back and forth in split view
    while len(asked) < 3:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert sorted(asked) == sorted(f"/v1/threads/{quote(t, safe='')}/consumptionhorizons" for t in chats)
    answer.set()
    await _settle(receipts)
    assert len(asked) == 6  # one re-ask per chat, however many asks came in
    receipts.resync()
    await _settle(receipts)
    assert len(asked) == 9
    receipts.open(G)
    await _settle(receipts)
    assert len(asked) == 10  # nothing pending: a lone ask is a lone GET
    await receipts.close()


async def test_a_resync_during_a_snapshot_still_gets_the_newer_horizon(directory: Directory) -> None:
    """The GET in flight may have read the server before the reconnect: the re-ask catches up."""
    horizon = "5;1;1"
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        answer = _snapshot((ANN, horizon))  # what the server holds when it reads
        await gate.wait()
        return httpx.Response(200, json=answer)

    sent: list[tuple[str, dict[str, Any]]] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    receipts = R.Receipts(directory, ME, lambda t, f: sent.append((t, f)), None, client)
    receipts.open(G)
    await asyncio.sleep(0.05)
    horizon = "9;1;1"  # ANN read on while our socket was down
    receipts.resync()
    gate.set()
    await _settle(receipts)
    assert _ids(sent[-1][1]) == {ANN: "9"}
    await receipts.close()


async def test_with_our_mri_unknown_no_receipt_is_shown(
    directory: Directory, chat_service: ChatService
) -> None:
    """Our own read could not be told apart from a reader's: nothing shows, nothing is asked."""
    sent: list[tuple[str, dict[str, Any]]] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(chat_service.handler))
    receipts = R.Receipts(directory, "", lambda t, f: sent.append((t, f)), None, client)
    chat_service.answers[G] = _snapshot((ME, "9;1;1"), (ANN, "8;1;1"))
    receipts.open(G)
    await _settle(receipts)
    receipts.on_event(G, _live(ME, "10;1;1"))
    receipts.resync()
    await _settle(receipts)
    assert sent == [] and receipts.shown == {} and chat_service.requests == []
    await receipts.close()


async def test_a_live_reader_never_named_yet_is_resolved_at_once(
    directory: Directory, chat_service: ChatService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No snapshot names a reader between reconnects: the live event asks the directory itself, once."""
    asked: list[list[str]] = []

    async def resolve(mris: list[str], *, refresh: bool = False) -> None:
        asked.append(mris)
        directory.note_name(DAN.lower(), "Dan Fox")

    receipts, sent = _receipts(directory, chat_service)
    directory.note_name(ANN.lower(), "Ann Lee")
    receipts.open(G)
    await _settle(receipts)
    monkeypatch.setattr(directory, "resolve", resolve)
    receipts.on_event(G, _live(ANN, "5;1;1"))  # named already: no lookup
    receipts.on_event(G, _live(DAN, "6;1;1"))
    assert sent[-1][1]["receipts"][DAN.lower()]["name"] == "Unknown user"  # painted at once (038-AC6)
    await _settle(receipts)
    assert asked == [[DAN.lower()]]
    assert sent[-1][1]["receipts"][DAN.lower()]["name"] == "Dan Fox"
    receipts.on_event(G, _live(DAN, "7;1;1"))
    await _settle(receipts)
    assert asked == [[DAN.lower()]]
    await receipts.close()


# --- cache ---


async def test_restart_paints_cached_horizons_before_the_snapshot(
    settings: Settings, directory: Directory, chat_service: ChatService
) -> None:
    """The cache only speeds the first paint: owner-only, tenant-scoped, merged with the snapshot by max."""
    cache = P.open_cache(settings)
    assert cache is not None
    first, _ = _receipts(directory, chat_service, cache)
    first.on_event(G, _live(ANN, "60;1;1"))
    first.on_event(G, _live(BOB, "30;1;1"))
    await first.close()
    cache.close()
    other = P.Cache(settings.cache_dir / "presence.sqlite3", "another-tenant")
    other.save_horizons(G, {CID.lower(): 99}, 0)
    other.close()

    cache = P.open_cache(settings)
    assert cache is not None
    chat_service.answers[G] = _snapshot((ANN, "55;1;1"), (BOB, "35;1;1"))
    receipts, sent = _receipts(directory, chat_service, cache)
    receipts.open(G)
    assert _ids(sent[0][1]) == {ANN.lower(): "60", BOB.lower(): "30"}  # before any request
    await _settle(receipts)
    assert _ids(sent[-1][1]) == {ANN.lower(): "60", BOB.lower(): "35"}
    assert cache.horizons(G) == {ANN.lower(): 60, BOB.lower(): 35}
    assert (settings.cache_dir / "presence.sqlite3").stat().st_mode & 0o777 == 0o600
    await receipts.close()
    cache.close()


def test_cache_horizons_stay_bounded_and_never_move_backwards(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(P, "_HORIZONS_MAX", 3)
    cache = P.open_cache(settings)
    assert cache is not None
    for i in range(6):
        cache.save_horizons(f"19:t{i}@thread.v2", {ANN.lower(): 10 + i}, 1000.0 + i)
    left = {t for (t,) in cache.db.execute("SELECT thread_id FROM horizons")}
    assert left == {"19:t3@thread.v2", "19:t4@thread.v2", "19:t5@thread.v2"}
    cache.save_horizons("19:t5@thread.v2", {ANN.lower(): 2}, 2000.0)
    assert cache.horizons("19:t5@thread.v2") == {ANN.lower(): 15}
    cache.close()


def test_cache_writes_only_a_newer_horizon(settings: Settings) -> None:
    """A replayed, late or unchanged horizon (every snapshot) neither rewrites a row nor runs the eviction."""
    cache = P.open_cache(settings)
    assert cache is not None
    cache.save_horizons(G, {ANN.lower(): 10, BOB.lower(): 20}, 1.0)
    statements: list[str] = []
    cache.db.set_trace_callback(statements.append)
    cache.save_horizons(G, {ANN.lower(): 10, BOB.lower(): 19}, 2.0)
    assert not [s for s in statements if s.startswith("DELETE")]
    ages = "SELECT mri, msg_id, updated_at_ms FROM horizons ORDER BY mri"
    assert list(cache.db.execute(ages)) == [(ANN.lower(), 10, 1000), (BOB.lower(), 20, 1000)]
    cache.save_horizons(G, {ANN.lower(): 11}, 3.0)
    assert list(cache.db.execute(ages)) == [(ANN.lower(), 11, 3000), (BOB.lower(), 20, 1000)]
    assert [s for s in statements if s.startswith("DELETE")]
    cache.close()


async def test_a_corrupt_cache_is_rebuilt_with_its_receipts_table(
    settings: Settings, directory: Directory, chat_service: ChatService
) -> None:
    path = settings.cache_dir / "presence.sqlite3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x00garbage" * 1000)
    cache = P.open_cache(settings)
    assert cache is not None and cache.horizons(G) == {}
    receipts, _ = _receipts(directory, chat_service, cache)
    receipts.on_event(G, _live(ANN, "8;1;1"))
    assert cache.horizons(G) == {ANN.lower(): 8}
    await receipts.close()
    cache.close()


# --- board ---


async def test_board_relays_receipts_to_followers_without_touching_rows_or_seen(
    board: W.Board, settings: Settings, chat_service: ChatService
) -> None:
    """011-AC3: a receipt is no activity: row time, order, unread and seen stay; the page gets a
    `receipts` frame naming its chat."""
    assert board.directory is not None
    client = httpx.AsyncClient(transport=httpx.MockTransport(chat_service.handler))
    board.receipts = R.Receipts(board.directory, ME, board._receipts_frame, None, client)
    chat_service.answers[T] = _snapshot((ANN, "3;1;1"), thread_id=T)
    board.rows[T] = {
        "id": T,
        "label": "T",
        "last_activity": _at(5),
        "last_id": "5",
        "read_id": "4",
        "unread": True,
        "seen_at": _at(4),
        "typing": [],
    }
    async with _served(board, settings), _connect(board) as ws:
        await ws.recv()
        before = json.dumps(board.rows, sort_keys=True)  # after the payload stamped its links
        await ws.send(json.dumps({"follow": T}))
        frames = [await _frame(ws) for _ in range(3)]
        assert {"receipts": {}, "thread": T} in frames  # the cached paint, empty here
        assert any(f.get("follow") == T for f in frames)
        assert {
            "receipts": {ANN.lower(): {"id": "3", "name": "Unknown user"}},
            "thread": T,
        } in frames
        await board.on_event({"resourceType": "NewMessage", "resource": _live(ANN, "5;1;1", thread_id=T)})
        assert _ids(await _frame(ws)) == {ANN.lower(): "5"}
        with pytest.raises(TimeoutError):
            await _frame(ws)  # no board payload, no message frame
    assert json.dumps(board.rows, sort_keys=True) == before
    await board.receipts.close()


async def test_reconnect_snapshots_every_followed_chat_again(
    board: W.Board, settings: Settings, chat_service: ChatService
) -> None:
    """011-AC5: a Trouter reconnect or loss re-snapshots the followed chats, and only them."""
    assert board.directory is not None
    client = httpx.AsyncClient(transport=httpx.MockTransport(chat_service.handler))
    board.receipts = R.Receipts(board.directory, ME, board._receipts_frame, None, client)
    async with _served(board, settings), _connect(board) as ws:
        await ws.recv()
        await ws.send(json.dumps({"follow": T}))
        while not chat_service.requests:
            await asyncio.sleep(0.01)
        await _settle(board.receipts)
        chat_service.requests.clear()
        await board.on_gap("connected")
        await _settle(board.receipts)
        await board.on_gap("message_loss")
        await _settle(board.receipts)
        paths = [r.url.raw_path.decode() for r in chat_service.requests]
        assert paths == [f"/v1/threads/{quote(T, safe='')}/consumptionhorizons"] * 2
    for _ in range(200):  # the closed page's chat is no longer followed
        if not board.receipts.shown:
            break
        await asyncio.sleep(0.01)
    assert board.receipts.shown == {}
    chat_service.requests.clear()
    await board.on_gap("connected")
    await _settle(board.receipts)
    assert chat_service.requests == []
    await board.receipts.close()


async def test_switching_chats_in_split_view_keeps_only_the_shown_one(
    board: W.Board, settings: Settings, chat_service: ChatService
) -> None:
    """One socket follows one chat: a switch drops the left chat's receipts, its events reach no page."""
    assert board.directory is not None
    client = httpx.AsyncClient(transport=httpx.MockTransport(chat_service.handler))
    board.receipts = R.Receipts(board.directory, ME, board._receipts_frame, None, client)
    for thread_id in (T, G):
        board.rows[thread_id] = {
            "id": thread_id,
            "label": "x",
            "last_activity": _at(1),
            "last_id": "1",
            "typing": [],
        }
    async with _served(board, settings), _connect(board) as ws:
        await ws.recv()
        await ws.send(json.dumps({"follow": T}))
        while T not in board.receipts.shown:
            await asyncio.sleep(0.01)
        await ws.send(json.dumps({"follow": G}))
        while G not in board.receipts.shown:
            await asyncio.sleep(0.01)
        assert set(board.receipts.shown) == {G}
    await _settle(board.receipts)
    await board.receipts.close()


def test_receipt_event_stays_silent_on_the_board() -> None:
    assert R.EVENT in W._SILENT_TYPES
