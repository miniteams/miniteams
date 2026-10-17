"""Composer completion, mentions and formatting on the server (spec 012 phase 1)."""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
import structlog.testing
from conftest import wait
from test_web_chat import (
    ANN,
    ME,
    NOPE,
    B,
    C,
    T,
    _archive,
    _at,
    _chat_socket,
    _cookie,
    _m,
    _Recorder,
    _send,
    _served,
)

from miniteams import chat_verbs as CV
from miniteams import compose, emojis
from miniteams import web as W
from miniteams.config import Settings
from miniteams.directory import Directory

ELO = "8:orgid:00000000-0000-0000-0000-00000000000d"
ZOE = "8:orgid:00000000-0000-0000-0000-00000000000f"  # unfolded, "Zoe" sorts before "Élodie"
BOT = "28:00000000-0000-0000-0000-0000000000ee"
TRIO = "19:trio0000@thread.v2"  # group of me, Ann and a bot: two humans
DUO = "19:00000000-0000-0000-0000-00000000000a_00000000-0000-0000-0000-00000000000b@unq.gbl.spaces"
OID = "0-weu-d1-feedc0ffee"
MENTION = "http://schema.skype.com/Mention"

_THREAD = Directory.thread  # the real one: `rosters` replaces it

_ROSTERS = {
    B: [
        {"mri": ME, "name": "Me Self"},
        {"mri": ANN, "name": "Ann Lee"},
        {"mri": ELO, "name": "Élodie Martin"},
        {"mri": BOT, "name": "Helper Bot"},
    ],
    DUO: [{"mri": ME, "name": "Me Self"}, {"mri": ANN, "name": "Ann Lee"}],
    TRIO: [
        {"mri": ME, "name": "Me Self"},
        {"mri": ANN, "name": "Ann Lee"},
        {"mri": BOT, "name": "Helper Bot"},
    ],
}


@pytest.fixture
def rosters(directory: Directory, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Directory.thread answers the fictional rosters above (None for any other thread); returns the
    threads asked, in order."""
    asked: list[str] = []

    async def fake_thread(
        self: Directory, thread_id: str, max_age: float | None = None
    ) -> dict[str, Any] | None:
        assert max_age == CV._ROSTER_TTL
        asked.append(thread_id)
        members = _ROSTERS.get(thread_id)
        return None if members is None else {"topic": None, "members": [dict(m) for m in members]}

    async def fake_label(self: Directory, thread_id: str) -> str:
        return f"label:{thread_id}"

    monkeypatch.setattr(Directory, "thread", fake_thread)
    monkeypatch.setattr(Directory, "label", fake_label)
    directory.me = ME
    return asked


def _row(thread_id: str) -> dict[str, Any]:
    return {
        "id": thread_id,
        "label": "x",
        "last_activity": _at(1),
        "last_id": "1",
        "seen_at": None,
        "typing": [],
    }


@pytest.fixture
def board(directory: Directory, rosters: list[str], tmp_path: Path) -> W.Board:
    data = _archive(tmp_path / "data", T, [_m("1", _at(1))])
    _archive(data, C, [])
    board = W.Board({B: _row(B), DUO: _row(DUO), TRIO: _row(TRIO)}, directory, me=ME, data_dir=data)
    board.emojis.names = {"fiesta": OID, "Firefly": "0-weu-d1-f1", "fire": "0-weu-d1-shadowed"}
    return board


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_send(s: Any, token: str, thread_id: str, text: str, name: str, **kw: Any) -> dict[str, Any]:
        calls.append((thread_id, text, kw))
        return {"status": 201}

    monkeypatch.setattr(CV, "send_message", fake_send)
    return calls


async def _complete(board: W.Board, kind: Any, q: Any, thread: Any = B, ws: Any = None) -> dict[str, Any]:
    return await board.chat.complete_answer(ws, {"complete": kind, "q": q, "thread": thread})


# --- emoji completion (012-AC1) ---


async def test_emoji_prefix_matches_builtin_and_custom_case_insensitive(board: W.Board) -> None:
    """012-AC1: `fi` → `fire` (unicode) and the custom names starting with `fi`, ≤ 12, any case."""
    answer = await _complete(board, "emoji", "fi")
    assert answer["complete"] == "emoji" and answer["q"] == "fi"
    items = answer["items"]
    assert {"name": "fire", "char": "🔥"} in items
    assert {"name": "fiesta", "custom": OID, "src": f"/emoji/{OID}"} in items
    assert {"name": "Firefly", "custom": "0-weu-d1-f1", "src": "/emoji/0-weu-d1-f1"} in items
    # The built-in `fire` wins: a custom one by that name would send the built-in character.
    assert [it for it in items if it["name"] == "fire"] == [{"name": "fire", "char": "🔥"}]
    assert (await _complete(board, "emoji", "FI"))["items"] == items
    assert (await _complete(board, "emoji", "fir"))["items"][:2] == [
        {"name": "fire", "char": "🔥"},
        {"name": "Firefly", "custom": "0-weu-d1-f1", "src": "/emoji/0-weu-d1-f1"},
    ]


async def test_emoji_list_is_capped_and_unknown_prefix_is_empty(board: W.Board) -> None:
    """012-AC1: at most 12 items; an unknown prefix answers `[]`."""
    many = await _complete(board, "emoji", "s")
    assert len(many["items"]) == 12
    assert (await _complete(board, "emoji", "zzqx"))["items"] == []


@pytest.mark.parametrize("q", [None, 3, "", "a" * 65, ["fi"]])
async def test_emoji_query_must_be_a_short_text(board: W.Board, q: Any) -> None:
    answer = await _complete(board, "emoji", q)
    assert answer == {"complete": "emoji", "q": q if isinstance(q, str) else None, "error": "invalid query"}


async def test_unknown_kind_is_an_error(board: W.Board, rosters: list[str]) -> None:
    assert (await _complete(board, "channel", "a"))["error"] == "invalid kind"
    assert rosters == []


# --- member completion (012-AC2) ---


async def test_members_are_the_humans_but_me_with_everyone_first_on_groups(board: W.Board) -> None:
    """012-AC2: an empty prefix lists everyone, then the humans; me and the bot are left out."""
    answer = await _complete(board, "member", "")
    assert answer == {
        "complete": "member",
        "q": "",
        "items": [
            {"name": "everyone", "mri": B, "everyone": True},
            {"name": "Ann Lee", "mri": ANN},
            {"name": "Élodie Martin", "mri": ELO},
        ],
    }


@pytest.mark.parametrize(
    ("q", "names"),
    [
        ("elo", ["Élodie Martin"]),  # accent folded
        ("ÉLO", ["Élodie Martin"]),
        ("mart", ["Élodie Martin"]),  # last name
        ("ann l", ["Ann Lee"]),  # full name, as the page asks once a space is typed
        ("lee", ["Ann Lee"]),
        ("ev", ["everyone"]),
        ("me", []),  # me is never offered
        ("help", []),  # nor a bot
        ("ee", []),  # prefix, not substring
    ],
)
async def test_member_prefix_on_first_last_and_full_name(board: W.Board, q: str, names: list[str]) -> None:
    """012-AC2: prefix match on first, last or full name, accents and case folded."""
    assert [it["name"] for it in (await _complete(board, "member", q))["items"]] == names


async def test_no_everyone_on_a_one_to_one(board: W.Board) -> None:
    """012-AC2: a 1:1 chat has no everyone."""
    assert (await _complete(board, "member", "", DUO))["items"] == [{"name": "Ann Lee", "mri": ANN}]


async def test_member_list_is_capped(board: W.Board, monkeypatch: pytest.MonkeyPatch) -> None:
    crowd = [{"mri": f"8:orgid:{i:032x}", "name": f"Ann {i}"} for i in range(20)]
    monkeypatch.setitem(_ROSTERS, B, crowd)
    assert len((await _complete(board, "member", "ann"))["items"]) == 12


async def test_member_lookup_refuses_a_chat_it_may_not_post_to(board: W.Board, rosters: list[str]) -> None:
    """012-AC2: a thread off the board and not followed (or a channel) is refused, no roster call."""
    for thread in (NOPE, T, C, 42, None):
        answer = await _complete(board, "member", "a", thread)
        assert answer["error"] == "unknown chat", thread
    assert rosters == []
    ws = object()
    assert (await board.follow(ws, T))["ok"]  # type: ignore[arg-type]
    answer = await _complete(board, "member", "a", T, ws)
    assert answer["error"] == "chat members unavailable"  # followed: asked, but no roster known
    assert rosters == [T]


# --- roster cache ---


async def test_roster_is_trusted_for_the_ttl_only(board: W.Board, rosters: list[str]) -> None:
    """Members join and leave without a board event: the roster is asked at most _ROSTER_TTL old
    (`rosters` checks the max_age; Directory.thread's own tests check what it does with it)."""
    await _complete(board, "member", "a")
    assert rosters == [B]


async def test_roster_lookup_does_not_hold_the_socket(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow roster fetch runs in the background: the page's next verb is answered meanwhile."""
    release = asyncio.Event()

    async def slow_thread(self: Directory, thread_id: str, max_age: float | None = None) -> dict[str, Any]:
        await asyncio.wait_for(release.wait(), 3)  # bounded: an inline lookup fails instead of hanging
        return {"topic": None, "members": _ROSTERS[B]}

    monkeypatch.setattr(Directory, "thread", slow_thread)
    async with _served(board, settings), _chat_socket(board) as ws:
        await ws.send(json.dumps({"complete": "member", "q": "an", "thread": B}))
        await ws.send(json.dumps({"preview": "**x**"}))
        assert json.loads(await asyncio.wait_for(ws.recv(), 2)) == {"preview": "<b>x</b>"}
        release.set()
        answer = json.loads(await asyncio.wait_for(ws.recv(), 2))
        assert answer["items"] == [{"name": "Ann Lee", "mri": ANN}]


# --- send with mentions (012-AC6) ---


async def test_send_refuses_a_mention_outside_the_roster(
    board: W.Board, sent: list[Any], rosters: list[str]
) -> None:
    """012-AC6: an mri the thread does not hold (or a bot, or a malformed entry) is refused, no API call."""
    stranger = "8:orgid:00000000-0000-0000-0000-0000000000ff"
    for mentions, error in (
        ([{"name": "Zed", "mri": stranger}], "unknown mention"),
        ([{"name": "Ann Lee", "mri": ANN}, {"name": "Zed", "mri": stranger}], "unknown mention"),
        ([{"name": "Helper Bot", "mri": BOT}], "unknown mention"),
        ([{"name": "everyone", "mri": DUO, "everyone": True}], "unknown mention"),  # not this thread
        ([{"name": "Ann Lee"}], "invalid mention"),
        ([{"name": "Ann Lee", "mri": ANN, "everyone": "yes"}], "invalid mention"),
        ({"name": "Ann Lee", "mri": ANN}, "invalid mention"),
        (["Ann Lee"], "invalid mention"),
    ):
        assert await _send(board, B, "hi @Ann Lee", None, None, mentions) == error, mentions
    assert sent == []


async def test_everyone_on_a_one_to_one_is_refused(board: W.Board, sent: list[Any]) -> None:
    """012-AC6: a 1:1 has no everyone, even with its own thread id."""
    everyone = [{"name": "everyone", "mri": DUO, "everyone": True}]
    assert await _send(board, DUO, "@everyone hi", None, None, everyone) == "unknown mention"
    assert sent == []


async def test_mentions_without_a_roster_are_refused(board: W.Board, sent: list[Any]) -> None:
    """L5: no roster to check a mention against is its own refusal, not an "unknown mention"."""
    ws = object()
    assert (await board.follow(ws, T))["ok"]  # type: ignore[arg-type]
    mention = [{"name": "Ann Lee", "mri": ANN}]
    assert await _send(board, T, "@Ann Lee", ws, None, mention) == "roster unavailable"
    assert await _send(board, T, "no mention", ws) is None  # the roster only matters to a mention
    assert [thread for thread, *_ in sent] == [T]


async def test_send_with_mentions_posts_spans_and_properties(board: W.Board, sent: list[Any]) -> None:
    """012-AC4/AC5 through `send`: Mention spans in text order, the JSON property, everyone on a group."""
    mentions = [{"name": "Ann Lee", "mri": ANN}, {"name": "everyone", "mri": B, "everyone": True}]
    assert await _send(board, B, "@everyone ping @Ann Lee <3", None, None, mentions) is None
    [(thread, body, kw)] = sent
    assert thread == B
    assert body == (
        f'<span itemtype="{MENTION}" itemscope="" itemid="0">Everyone</span> ping '
        f'<span itemtype="{MENTION}" itemscope="" itemid="1">Ann Lee</span> &lt;3'
    )
    assert kw["is_html"] is True
    assert json.loads(kw["properties"]["mentions"]) == [
        {"@type": MENTION, "itemid": 0, "mri": B, "mentionType": "everyone", "displayName": "Everyone"},
        {"@type": MENTION, "itemid": 1, "mri": ANN, "mentionType": "person", "displayName": "Ann Lee"},
    ]


async def test_send_over_the_socket_carries_mentions_and_format(
    board: W.Board, settings: Settings, sent: list[Any]
) -> None:
    async with _served(board, settings), _chat_socket(board) as ws:
        verb = {
            "send": B,
            "text": "- @Ann Lee",
            "mentions": [{"name": "Ann Lee", "mri": ANN}],
            "format": "full",
        }
        await ws.send(json.dumps(verb))
        assert json.loads(await asyncio.wait_for(ws.recv(), 2)) == {"sent": B, "ok": True}
        await ws.send(json.dumps({**verb, "mentions": [{"name": "Zed", "mri": "8:orgid:zed"}]}))
        assert json.loads(await asyncio.wait_for(ws.recv(), 2)) == {"sent": B, "error": "unknown mention"}
    [(_, body, kw)] = sent
    assert body == f'<ul><li><span itemtype="{MENTION}" itemscope="" itemid="0">Ann Lee</span></li></ul>'
    assert "properties" in kw


# --- formatting and plain text ---


async def test_format_full_and_inline(board: W.Board, sent: list[Any]) -> None:
    text = "- item\n**b**"
    assert await _send(board, B, text) is None
    assert await _send(board, B, text, fmt="inline") is None
    assert await _send(board, B, text, fmt="full") is None
    assert [c[1] for c in sent] == ["- item<br><b>b</b>"] * 2 + ["<ul><li>item</li></ul><b>b</b>"]
    assert all(c[2] == {"is_html": True} for c in sent)  # no mention: no properties
    for fmt in ("md", 1, ""):
        assert await _send(board, B, text, fmt=fmt) == "invalid format"
    assert len(sent) == 3


async def test_plain_text_is_sent_exactly_as_before(board: W.Board, sent: list[Any]) -> None:
    """No marker, emoji or mention: the raw text goes to send_message, which escapes it as it always did."""
    for text in ("a<b> & 'q'\nline two", "at 10:30: ok :nope:", "2*3*4 snake_case_name"):
        assert await _send(board, B, text, fmt="full") is None
    assert sent == [
        (B, "a<b> & 'q'\nline two", {}),
        (B, "at 10:30: ok :nope:", {}),
        (B, "2*3*4 snake_case_name", {}),
    ]


async def test_emojis_expand_with_the_fetched_custom_set(board: W.Board, sent: list[Any]) -> None:
    assert await _send(board, B, ":fire: :fiesta:") is None
    body = sent[0][1]
    assert body.startswith('🔥 <img alt="fiesta" itemid="fiesta;0-weu-d1-feedc0ffee"')


async def test_compose_errors_answer_the_page(board: W.Board, sent: list[Any]) -> None:
    """compose_html's ValueError becomes the answer, nothing sent."""
    many = [{"name": "Ann Lee", "mri": ANN}] * 51
    assert await _send(board, B, "@Ann Lee", None, None, many) == "too many mentions (51 > 50)"
    assert sent == []


async def test_byte_cap_applies_to_the_composed_body(board: W.Board, sent: list[Any]) -> None:
    """Markup grows the body: under the cap as typed, over it once `**x**` became `<b>x</b>`."""
    text = "**x** " + "y" * (compose.MAX_TEXT - 6)
    assert CV._text_error(text) is None
    assert await _send(board, B, text) == "message too long"
    assert await _send(board, B, "y" * compose.MAX_TEXT) is None  # at the limit, plain: goes through
    assert len(sent) == 1


async def test_quotes_go_before_the_composed_body(
    board: W.Board, sent: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def copies(self: CV.ChatVerbs, t: str, ids: list[str]) -> dict[str, Any]:
        return {"1": _m("1", _at(1), "<p>q</p>")}

    monkeypatch.setattr(CV.ChatVerbs, "_server_copies", copies)
    assert await _send(board, B, "**ok**", None, ["1"]) is None
    body = sent[0][1]
    assert body.startswith("<blockquote") and body.endswith("</blockquote><b>ok</b>")


# --- preview ---


async def test_preview_renders_without_sending(board: W.Board, sent: list[Any]) -> None:
    shown = board.chat.preview_answer({"preview": "**a** :fiesta: @Ann Lee\n- i", "format": "full"})
    assert shown["preview"].startswith('<b>a</b> <img alt="fiesta"')
    assert f'src="/emoji/{OID}"' in shown["preview"]  # the page cannot load the asyncgw URL
    assert shown["preview"].endswith("@Ann Lee<ul><li>i</li></ul>")  # mentions stay text
    assert board.chat.preview_answer({"preview": "- i", "format": None}) == {"preview": "- i"}
    assert board.chat.preview_answer({"preview": 3, "format": None})["error"] == "invalid preview"
    assert board.chat.preview_answer({"preview": "x", "format": "md"})["error"] == "invalid preview"
    assert board.chat.preview_answer({"preview": "x" * (compose.MAX_TEXT + 1), "format": None})[
        "error"
    ].startswith("message too long")
    assert sent == []


# --- /emoji/<id> (012-AC9) ---


@pytest.fixture
def emoji_cache(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """cached_image finds `<id>.png` in the cache; returns the ids it was asked for."""
    asked: list[str] = []
    cache = settings.cache_dir / "emojis"
    cache.mkdir(parents=True)
    (cache / f"{OID}.png").write_bytes(b"\x89PNG-emoji")

    async def fake_cached(s: Settings, token: str, oid: str) -> Path | None:
        asked.append(oid)
        found = cache / f"{oid}.png"
        return found if found.exists() else None

    monkeypatch.setattr(emojis, "cached_image", fake_cached)
    return asked


async def test_emoji_route_serves_a_known_custom_image(
    board: W.Board, settings: Settings, emoji_cache: list[str]
) -> None:
    """012-AC9: served from the cache, typed, sandboxed; unknown ids and traversal 404 without a fetch."""
    async with _served(board, settings) as host, httpx.AsyncClient(headers=_cookie(board)) as client:
        got = await client.get(f"http://{host}/emoji/{OID}")
        assert got.status_code == 200 and got.content == b"\x89PNG-emoji"
        assert got.headers["content-type"] == "image/png"
        assert got.headers["x-content-type-options"] == "nosniff"
        assert got.headers["content-security-policy"] == "sandbox"
        assert got.headers["cross-origin-resource-policy"] == "same-origin"
        assert emoji_cache == [OID]
        for path in (
            "/emoji/0-weu-d1-unknown",
            "/emoji/..%2F..%2Fcfg%2Fweb.json",
            "/emoji/../names.json",
            f"/emoji/{OID}/x",
            f"/emoji/{OID}.png",
            "/emoji/",
        ):
            assert (await client.get(f"http://{host}{path}")).status_code == 404, path
        assert emoji_cache == [OID]  # none of those reached the download path
        foreign = await client.get(f"http://{host}/emoji/{OID}", headers={"Host": "evil.example"})
        assert foreign.status_code == 404
    # An id the metadata itself carries is still held to the AMS shape before it reaches the cache.
    board.emojis.names["evil"] = "../cfg"
    assert await board.emojis.image("../cfg") is None
    assert emoji_cache == [OID]


async def test_emoji_file_stays_in_the_cache_dir(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """012-AC9: a path the download layer hands back outside the cache (or a partial one) is refused."""
    cache = settings.cache_dir / "emojis"
    cache.mkdir(parents=True)
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"no")
    (cache / f"{OID}.png").symlink_to(outside)
    (cache / f"{OID}.part").write_bytes(b"half")
    for candidate in (outside, cache / f"{OID}.png", cache / f"{OID}.part", cache / "missing.png"):

        async def fake(s: Any, t: str, oid: str, found: Path = candidate) -> Path:
            return found

        monkeypatch.setattr(emojis, "cached_image", fake)
        assert await board.emojis.image(OID) is None, candidate
    board.emojis.directory = None
    assert await board.emojis.image(OID) is None


# --- custom set fetch and refresh ---


async def test_custom_set_failure_keeps_the_builtin_set(
    board: W.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed fetch logs a warning and completes from the built-in set; a later fetch fills it."""
    board.emojis.names = {}
    fetched = ({"fiesta": OID}, {"fiesta": "fiesta;0-weu-d1-key"})
    outcomes: list[Exception | tuple[dict[str, str], dict[str, str]]] = [
        RuntimeError("no chat-service token"),
        fetched,
    ]

    def fake_fetch(settings: Settings, token: str) -> tuple[dict[str, str], dict[str, str]]:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(emojis, "_custom_set", fake_fetch)
    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(board.emojis.refresh(interval=3600))
        await wait(lambda: bool(logs), "the failure logged")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [entry["event"] for entry in logs] == ["custom_emojis_unavailable"]
    assert board.emojis.names == {}
    items = (await _complete(board, "emoji", "fi"))["items"]
    assert {"name": "fire", "char": "🔥"} in items and not any("custom" in it for it in items)
    task = asyncio.create_task(board.emojis.refresh(interval=3600))
    await wait(lambda: bool(board.emojis.names), "the names fetched")
    task.cancel()
    assert board.emojis.names == {"fiesta": OID}


def test_custom_emojis_need_the_chat_service_token(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Source:
        def __init__(self, token: str | None) -> None:
            self.token = token

        def csa_token(self) -> str | None:
            return self.token

    meta = {"categories": [{"emoticons": [{"id": "fiesta;0-weu-d1-key", "documentId": OID}]}]}
    seen: list[tuple[str, str]] = []

    def fake_meta(s: Settings, skype: str, csa: str) -> dict[str, Any]:
        seen.append((skype, csa))
        return meta

    monkeypatch.setattr(emojis, "fetch_metadata", fake_meta)
    monkeypatch.setattr(emojis, "token_source", lambda s: _Source("CSA"))
    assert emojis._custom_set(settings, "SK") == ({"fiesta": OID}, {"fiesta": "fiesta;0-weu-d1-key"})
    assert seen == [("SK", "CSA")]
    monkeypatch.setattr(emojis, "token_source", lambda s: _Source(None))
    with pytest.raises(RuntimeError, match="chat-service token"):
        emojis._custom_set(settings, "SK")


async def test_run_cancels_the_emoji_refresh_on_shutdown(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled = asyncio.Event()

    async def forever(self: emojis.EmojiCache, interval: float = 0) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def idle(*a: Any, **kw: Any) -> None:
        await asyncio.Event().wait()

    async def ended(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(emojis.EmojiCache, "refresh", forever)
    monkeypatch.setattr(W, "serve_board", idle)
    monkeypatch.setattr(W, "fill_from_listing", idle)
    monkeypatch.setattr(W, "run_forever", ended)
    with pytest.raises(RuntimeError, match="live stream ended"):
        await W.run(settings, "sk", "", None, None)
    await asyncio.wait_for(cancelled.wait(), 2)


# --- review round: shared fetches, failure windows, roster names ---


async def test_parallel_lookups_share_one_roster_fetch(
    board: W.Board, directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetched: list[str] = []
    release = asyncio.Event()

    async def slow_fetch(self: Directory, thread_id: str) -> dict[str, Any]:
        fetched.append(thread_id)
        await asyncio.wait_for(release.wait(), 3)
        return {"topic": None, "members": [dict(m) for m in _ROSTERS[thread_id]]}

    monkeypatch.setattr(Directory, "thread", _THREAD)
    monkeypatch.setattr(Directory, "_fetch_thread", slow_fetch)
    first = asyncio.create_task(_complete(board, "member", "a"))
    others = [asyncio.create_task(_complete(board, "member", q)) for q in ("an", "ann", "ann ")]
    await asyncio.sleep(0)
    first.cancel()  # a page closing mid-lookup does not cancel the fetch the others wait on
    release.set()
    assert [(await t)["items"] for t in others] == [[{"name": "Ann Lee", "mri": ANN}]] * 3
    assert fetched == [B]
    at, info = directory._threads[B]
    directory._threads[B] = (at - CV._ROSTER_TTL - 1, info)
    await asyncio.gather(*(_complete(board, "member", "a") for _ in range(3)))
    assert fetched == [B, B]


async def test_everyone_on_any_group_whatever_its_size(board: W.Board, sent: list[Any]) -> None:
    """As the Teams client: a group of two people (me, Ann, a bot) offers and accepts everyone."""
    assert (await _complete(board, "member", "", TRIO))["items"] == [
        {"name": "everyone", "mri": TRIO, "everyone": True},
        {"name": "Ann Lee", "mri": ANN},
    ]
    everyone = [{"name": "everyone", "mri": TRIO, "everyone": True}]
    assert await _send(board, TRIO, "@everyone hi", None, None, everyone) is None
    [(thread, body, kw)] = sent
    assert thread == TRIO and body.endswith('itemid="0">Everyone</span> hi')
    assert json.loads(kw["properties"]["mentions"])[0]["mentionType"] == "everyone"


async def test_mention_names_come_from_the_roster(board: W.Board, sent: list[Any]) -> None:
    """The page's name only locates `@Name` and must be the member's own; the span shows the roster's."""
    for wrong in ({"name": "Boss", "mri": ANN}, {"name": "Élodie Martin", "mri": ANN}):
        assert await _send(board, B, f"@{wrong['name']} hi", None, None, [wrong]) == "unknown mention"
    assert await _send(
        board, B, "@everyone hi", None, None, [{"name": "all", "mri": B, "everyone": True}]
    ) == ("unknown mention")
    assert sent == []
    picked = [{"name": "ann lee", "mri": ANN}, {"name": "elodie martin", "mri": ELO}]
    assert await _send(board, B, "@ann lee and @elodie martin, @ann leex", None, None, picked) is None
    [(_, body, kw)] = sent
    assert body == (
        f'<span itemtype="{MENTION}" itemscope="" itemid="0">Ann Lee</span> and '
        f'<span itemtype="{MENTION}" itemscope="" itemid="1">Élodie Martin</span>, @ann leex'
    )
    names = [e["displayName"] for e in json.loads(kw["properties"]["mentions"])]
    assert names == ["Ann Lee", "Élodie Martin"]


async def test_shortened_mentions_keep_the_roster_identity(board: W.Board, sent: list[Any]) -> None:
    picked = [{"name": "ann", "mri": ANN}, {"name": "elodie", "mri": ELO}]
    assert await _send(board, B, "@ann and @elodie", None, None, picked) is None
    [(_, body, kw)] = sent
    assert body == (
        f'<span itemtype="{MENTION}" itemscope="" itemid="0">Ann</span> and '
        f'<span itemtype="{MENTION}" itemscope="" itemid="1">Élodie</span>'
    )
    assert [(m["displayName"], m["mri"]) for m in json.loads(kw["properties"]["mentions"])] == [
        ("Ann", ANN),
        ("Élodie", ELO),
    ]
    for name in ("Lee", "An", "Boss"):
        assert (
            await _send(board, B, f"@{name}", None, None, [{"name": name, "mri": ANN}]) == "unknown mention"
        )
    assert len(sent) == 1


async def test_typed_names_elsewhere_in_the_text_stay_as_typed(board: W.Board, sent: list[Any]) -> None:
    """Only the mention takes the roster spelling: a URL, code and a link label/target keep `@ann lee`."""
    text = "https://x.com/@ann lee `@ann lee` [@ann lee](https://y/@ann) @ann lee"
    assert await _send(board, B, text, None, None, [{"name": "ann lee", "mri": ANN}]) is None
    [(_, body, _)] = sent
    assert body == (
        "https://x.com/@ann lee <code>@ann lee</code> "
        '<a href="https://y/@ann">@ann lee</a> '
        f'<span itemtype="{MENTION}" itemscope="" itemid="0">Ann Lee</span>'
    )


async def test_a_failed_emoji_download_is_not_retried_inside_the_window(
    board: W.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    async def failing(s: Settings, token: str, oid: str) -> Path | None:
        asked.append(oid)
        return None

    monkeypatch.setattr(emojis, "cached_image", failing)
    for _ in range(5):
        assert await board.emojis.image(OID) is None
    assert asked == [OID]
    board.emojis._failed[OID] -= emojis._FAILED_TTL + 1
    assert await board.emojis.image(OID) is None
    assert asked == [OID, OID]


async def test_cancelled_emoji_requests_share_one_download_and_its_failure_memo(
    board: W.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3: requests cut by the HTTP timeout neither restart the download nor lose its failure."""
    asked: list[str] = []
    release = asyncio.Event()

    async def slow_failing(s: Settings, token: str, oid: str) -> Path | None:
        asked.append(oid)
        await release.wait()
        return None

    monkeypatch.setattr(emojis, "cached_image", slow_failing)
    for _ in range(3):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(board.emojis.image(OID), 0.05)
    assert asked == [OID]
    release.set()
    await wait(lambda: OID in board.emojis._failed, "the failure noted")
    assert OID in board.emojis._failed and not board.emojis._downloads
    assert await board.emojis.image(OID) is None
    assert asked == [OID]


async def test_failed_emoji_downloads_are_bounded(board: W.Board, monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing(s: Settings, token: str, oid: str) -> Path | None:
        return None

    monkeypatch.setattr(emojis, "cached_image", failing)
    monkeypatch.setattr(emojis, "_FAILED_MAX", 2)
    board.emojis.names = {"a": "id-a", "b": "id-b", "c": "id-c"}
    for oid in ("id-a", "id-b", "id-c"):
        await board.emojis.image(oid)
    assert list(board.emojis._failed) == ["id-b", "id-c"]


async def test_a_failed_custom_set_is_retried_with_a_capped_backoff(
    board: W.Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    board.emojis.names = {}
    outcomes: list[Exception | tuple[dict[str, str], dict[str, str]]] = [RuntimeError("down")] * 4 + [
        ({"fiesta": OID}, {})
    ]

    def fake_fetch(settings: Settings, token: str) -> tuple[dict[str, str], dict[str, str]]:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(emojis, "_custom_set", fake_fetch)
    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(board.emojis.refresh(interval=3600, retry=0.01, retry_max=0.02))
        await wait(lambda: bool(board.emojis.names), "the names fetched")
        task.cancel()
    assert board.emojis.names == {"fiesta": OID}  # long before the 6 h interval
    waits = [e["retry_in"] for e in logs if e["event"] == "custom_emojis_unavailable"]
    assert waits == [0.01, 0.02, 0.02, 0.02]


def test_custom_names_compose_cannot_expand_are_left_out(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    emoticons = [
        {"id": f"{name};0-weu-d1-{n}", "shortcuts": [name], "documentId": f"0-weu-d1-{n}"}
        for n, name in enumerate(["ok_1+x", "two words", "é-accent", "x" * 65, "a:b"])
    ]

    class _Source:
        def csa_token(self) -> str:
            return "CSA"

    monkeypatch.setattr(emojis, "token_source", lambda s: _Source())
    monkeypatch.setattr(emojis, "fetch_metadata", lambda *a: {"categories": [{"emoticons": emoticons}]})
    assert emojis._custom_set(settings, "SK") == ({"ok_1+x": "0-weu-d1-0"}, {"ok_1+x": "ok_1+x;0-weu-d1-0"})


async def test_custom_emojis_use_the_configured_asyncgw_host(board: W.Board, sent: list[Any]) -> None:
    """Another region's host goes into the sent <img>; the preview still points at /emoji/<id>."""
    assert board.directory is not None
    board.directory.settings.asyncgw_host = "eu-prod.asyncgw.teams.microsoft.com"
    assert await _send(board, B, ":fiesta:") is None
    assert (
        f'src="https://eu-prod.asyncgw.teams.microsoft.com/v1/objects/{OID}/views/imgt2_anim"' in sent[0][1]
    )
    shown = board.chat.preview_answer({"preview": ":fiesta:"})["preview"]
    assert f'src="/emoji/{OID}"' in shown and "asyncgw" not in shown


async def test_a_custom_emoji_item_carries_the_key_the_cli_lists(
    board: W.Board, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """016-AC2, 016-AC6: a custom item's `key` is Teams' emoticon id, byte-identical to what `emojis`
    lists (rows()["key"]) and what `react` sends, even when its tail is not the image's documentId."""
    meta = {
        "categories": [
            {"emoticons": [{"id": "fiesta;0-weu-d1-key", "shortcuts": ["fiesta"], "documentId": OID}]}
        ]
    }

    class _Source:
        def csa_token(self) -> str:
            return "CSA"

    monkeypatch.setattr(emojis, "token_source", lambda s: _Source())
    monkeypatch.setattr(emojis, "fetch_metadata", lambda *a: meta)
    board.emojis.names, board.emojis.keys = emojis._custom_set(settings, "SK")
    listed = emojis.rows(meta)[0]["key"]
    items = (await _complete(board, "emoji", "fies"))["items"]
    assert items == [{"name": "fiesta", "custom": OID, "src": f"/emoji/{OID}", "key": listed}]
    assert listed.partition(";")[2] != OID
    request = CV.parse_act("react", {"react": T, "msg": "1", "key": items[0]["key"]})
    assert isinstance(request, CV.ActRequest) and request.key == listed


# --- the follow answer's members (spec 022) ---


def _counted(board: W.Board, thread_id: str, n: int) -> None:
    board.rows[thread_id]["label"] = f"Team · {n}p"  # the popover opens on the label's count


async def _pushed(ws: _Recorder) -> dict[str, Any]:
    await wait(lambda: bool(ws.frames), "members frame")
    [frame] = ws.frames
    return frame


async def test_follow_answers_at_once_and_pushes_the_people_me_first_then_by_name(
    board: W.Board, rosters: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """022-AC1: a roster to fetch is not waited on: the follow answers without it, then a `members` frame
    brings its people (bots counted apart), me first, then by name with accents folded."""
    monkeypatch.setitem(_ROSTERS, B, [{"mri": ZOE, "name": "Zoe Ray"}, *_ROSTERS[B]])
    _counted(board, B, 5)
    ws = _Recorder()
    answer = await board.follow(ws, B)  # type: ignore[arg-type]
    assert answer["ok"] and "people" not in answer
    assert await _pushed(ws) == {
        "members": B,
        "people": [
            {"name": "Me Self", "mri": ME, "me": True},
            {"name": "Ann Lee", "mri": ANN, "me": False},
            {"name": "Élodie Martin", "mri": ELO, "me": False},
            {"name": "Zoe Ray", "mri": ZOE, "me": False},
        ],
        "members_total": 4,
        "apps": 1,
    }
    assert rosters == [B]


async def test_no_roster_asked_without_a_count_nor_for_a_channel(board: W.Board, rosters: list[str]) -> None:
    """022-AC1: a label with no member count (1:1, untitled group) or a channel: `people` None, no fetch."""
    board.rows[C] = _row(C)
    _counted(board, C, 9)
    ws = _Recorder()
    for thread in (DUO, TRIO, C):
        assert (await board.follow(ws, thread))["people"] is None  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    assert (rosters, ws.frames) == ([], [])


async def test_a_roster_landing_after_the_page_moved_on_is_not_pushed(
    board: W.Board, directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """022-AC1: the fetch runs off the socket loop; the page left the chat before it landed: no frame.
    A roster that cannot be had pushes `people: None` (022-AC5)."""
    release = asyncio.Event()

    async def slow_fetch(self: Directory, thread_id: str) -> dict[str, Any] | None:
        await release.wait()
        return {"topic": None, "members": [dict(m) for m in _ROSTERS[thread_id]]} if thread_id == B else None

    monkeypatch.setattr(Directory, "thread", _THREAD)
    monkeypatch.setattr(Directory, "_fetch_thread", slow_fetch)
    _counted(board, B, 4)
    _counted(board, TRIO, 3)
    ws = _Recorder()
    assert "people" not in await board.follow(ws, B)  # type: ignore[arg-type]
    assert "people" not in await board.follow(ws, TRIO)  # type: ignore[arg-type]
    release.set()
    assert await _pushed(ws) == {"members": TRIO, "people": None}
    await wait(lambda: B in directory._threads, "B's roster cached")
    assert len(ws.frames) == 1


async def test_a_cached_roster_is_answered_inline_and_a_stale_one_refetched(
    board: W.Board, directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """022-AC1, 022-AC4: any cached copy is in the follow answer itself, no `members` frame; a stale one
    is refetched behind it. A big one sends the first 50 people by name, their count and the apps'."""
    crowd = [{"mri": f"8:orgid:{i:032x}", "name": f"Zed {i:03d}"} for i in range(120)]
    bots = [{"mri": f"28:{i:032x}", "name": f"Aa Bot {i}"} for i in range(10)]
    info = {"topic": None, "members": [*reversed(crowd), *bots]}
    fetched: list[str] = []

    async def fetch(self: Directory, thread_id: str) -> dict[str, Any]:
        fetched.append(thread_id)
        return info

    monkeypatch.setattr(Directory, "thread", _THREAD)
    monkeypatch.setattr(Directory, "_fetch_thread", fetch)
    directory._threads[B] = (time.monotonic(), info)
    _counted(board, B, 130)
    ws = _Recorder()
    answer = await board.follow(ws, B)  # type: ignore[arg-type]
    assert (answer["members_total"], answer["apps"]) == (120, 10)
    assert [m["name"] for m in answer["people"]] == [f"Zed {i:03d}" for i in range(50)]
    await asyncio.sleep(0.05)
    assert fetched == []  # fresh: Teams is not asked
    directory._threads[B] = (time.monotonic() - CV._ROSTER_TTL - 1, info)
    assert (await board.follow(ws, B))["members_total"] == 120  # type: ignore[arg-type]
    await wait(lambda: fetched == [B], "stale roster refetched")
    assert ws.frames == []


async def test_a_cached_404_is_answered_inline_as_no_roster(
    board: W.Board, directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """022-AC5: a roster Teams answered 404 for is cached as such: `people: None` at once, no frame."""
    monkeypatch.setattr(Directory, "thread", _THREAD)
    monkeypatch.setattr(Directory, "_fetch_thread", lambda *a: pytest.fail("no roster fetch expected"))
    directory._threads[B] = (time.monotonic(), None)
    _counted(board, B, 4)
    ws = _Recorder()
    assert (await board.follow(ws, B))["people"] is None  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    assert ws.frames == []
