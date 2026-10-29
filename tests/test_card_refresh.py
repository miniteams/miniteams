"""Spec 037 phase 1: app card refresh client and `data/cards.db`, against a fake Teams (MockTransport)."""

import asyncio
import base64
import contextlib
import copy
import gc
import json
import logging
import sqlite3
import sys
import threading
import warnings
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
from conftest import wait
from structlog.testing import capture_logs

from miniteams import card_refresh as CR
from miniteams import chat_verbs as CV
from miniteams.archive_store import ChatStore
from miniteams.config import Settings
from miniteams.render import cards_html

T = "19:card-chat@thread.v2"
ME = "8:orgid:00000000-0000-4000-8000-00000000000a"
OTHER = "8:orgid:00000000-0000-4000-8000-00000000000b"
APP = "aaaaaaaa-0000-4000-8000-000000000001"
BOT = "bbbbbbbb-0000-4000-8000-000000000002"
SKYPE, BEARER = "skype-secret-token", "bearer-secret-token"
T0 = 1_800_000_000.0
INVOKE = f"/v1/agents/28%3A{BOT}/invoke"


def _content(status: str, refresh: dict[str, Any] | None = None) -> dict[str, Any]:
    card: dict[str, Any] = {
        "type": "AdaptiveCard",
        "body": [{"type": "TextBlock", "text": f"Ticket: {status}"}],
    }
    if refresh is not None:
        card["refresh"] = refresh
    return card


ACTION = {"type": "Action.Execute", "verb": "refreshCard", "data": {"ticket": "T-1"}}


def _entry(status: str = "OPEN", refresh: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "appId": APP,
        "cardClientId": "card-1",
        "appName": "Tracker",
        "contentType": CR._ADAPTIVE,
        "content": _content(status, {"action": ACTION} if refresh is None else refresh),
        **extra,
    }


def _msg(*entries: dict[str, Any], mid: str = "1700000000001", **extra: Any) -> dict[str, Any]:
    return {
        "id": mid,
        "clientmessageid": "9900001",
        "messagetype": "RichText/Html",
        "content": "<p>see the ticket</p>",
        "properties": {"cards": json.dumps(list(entries) or [_entry()])},
        **extra,
    }


def _ok(status: str = "DONE") -> dict[str, Any]:
    return {"statusCode": 200, "type": CR._ADAPTIVE, "value": _content(status, {"action": ACTION})}


Reply = httpx.Response | Exception


class Teams:
    """Fake batchedDefinitions + invoke; records every request."""

    def __init__(self, invoke: Reply | None = None, apps: Reply | None = None) -> None:
        self.invoke = invoke if invoke is not None else httpx.Response(200, json=_ok())
        self.apps = (
            apps if apps is not None else httpx.Response(200, json=[{"id": APP, "bots": [{"id": BOT}]}])
        )
        self.calls: list[httpx.Request] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        reply = self.apps if request.url.path.endswith("batchedDefinitions") else self.invoke
        if self.gate is not None and request.url.path.endswith("/invoke"):
            await self.gate.wait()
        if isinstance(reply, Exception):
            raise reply
        return reply

    def paths(self) -> list[str]:
        return [r.url.raw_path.decode() for r in self.calls]


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def make(settings: Settings, tmp_path: Path, clock: Clock) -> Callable[[Teams], CR.CardRefresher]:
    settings.contacts_host = "chat.example.test"
    return lambda teams: CR.CardRefresher(
        settings, CR.CardStore(tmp_path / "cards.db"), transport=httpx.MockTransport(teams), clock=clock
    )


async def _go(refresher: CR.CardRefresher, message: dict[str, Any], me: str = ME) -> CR.Cards:
    return await refresher.refresh(T, message, me=me, skype_token=SKYPE, bearer=BEARER)


def _text(card: dict[str, Any] | None) -> str:
    return str(card["body"][0]["text"]) if card else ""


@pytest.mark.parametrize(
    ("entry", "eligible"),
    [
        (_entry(), True),
        (_entry(refresh={"action": ACTION, "userIds": []}), True),
        (_entry(refresh={"action": ACTION, "userIds": [OTHER, ME]}), True),
        (_entry(refresh={"action": ACTION, "userIds": [OTHER]}), False),
        (_entry(refresh={"action": ACTION, "userIds": [1, None, {"id": ME}]}), False),
        (_entry(refresh={"action": ACTION, "userIds": ME}), False),
        (_entry(refresh={"action": {**ACTION, "type": "Action.Submit"}}), False),
        (_entry(refresh={"action": {**ACTION, "verb": 3}}), False),
        (_entry(refresh={"action": {**ACTION, "verb": ""}}), False),
        (_entry(refresh={"action": "Action.Execute"}), False),
        (_entry(refresh=[ACTION]), False),  # type: ignore[arg-type]
        ({**_entry(), "content": {"type": "AdaptiveCard", "body": []}}, False),
        ({**_entry(), "content": json.dumps(_entry()["content"])}, False),
        ({**_entry(), "appId": None}, False),
        ({**_entry(), "cardClientId": 7}, False),
        ("card", False),
    ],
)
def test_refresh_action_eligibility(entry: Any, eligible: bool) -> None:
    """037-AC4: an Execute refresh with a verb, on an app card, for no one in particular or for me."""
    assert (CR.refresh_action(entry, ME) == ACTION) is eligible


async def test_ineligible_and_swift_cards_never_ask(make: Callable[[Teams], CR.CardRefresher]) -> None:
    """037-AC4: no refresh, someone else's refresh, a Swift card: no request at all."""
    teams = Teams()
    refresher = make(teams)
    swift = {"attachments": [{"content": _content("OPEN", {"action": ACTION}), "appId": APP}]}
    b64 = base64.b64encode(json.dumps(swift).encode()).decode()
    messages = [
        _msg(_entry(refresh={})),
        _msg(_entry(refresh={"action": ACTION, "userIds": [OTHER]})),
        {"id": "1700000000009", "messagetype": "RichText/Media_Card", "content": f'<Swift b64="{b64}"/>'},
        _msg(_entry(), mid=""),
    ]
    for message in messages:
        assert await _go(refresher, message) == {}
    assert teams.calls == []


async def test_refresh_asks_the_bot_as_teams_does(make: Callable[[Teams], CR.CardRefresher]) -> None:
    """037-AC2: the definition lookup and the invoke carry the spiked URL, headers and body."""
    teams = Teams()
    got = await _go(make(teams), _msg())
    assert _text(got["card-1"]) == "Ticket: DONE"
    apps, invoke = teams.calls
    assert str(apps.url) == CR.APPS_URL
    assert apps.headers["Authorization"] == f"Bearer {BEARER}" and apps.headers["X-Skypetoken"] == SKYPE
    assert json.loads(apps.content) == [APP]
    assert invoke.url.host == "chat.example.test" and invoke.url.raw_path.decode() == INVOKE
    assert invoke.headers["X-Skypetoken"] == SKYPE and "Authorization" not in invoke.headers
    assert invoke.headers["X-MS-AutoRefreshRequestInThread"] == T
    assert json.loads(invoke.content) == {
        "name": "adaptiveCard/action",
        "appId": APP,
        "value": {"action": ACTION, "trigger": "automatic"},
        "serverMessageId": "1700000000001",
        "clientMessageId": "9900001",
        "conversation": {"id": T},
    }


@pytest.mark.parametrize(
    ("definition", "bot"),
    [
        (
            [
                {"id": "other-app"},
                {"id": APP, "inputExtensions": [{"botId": "ext-bot"}], "bots": [{"id": BOT}]},
            ],
            "ext-bot",
        ),
        ([{"id": APP, "inputExtensions": [{"botId": ""}], "bots": [{"id": BOT}]}], BOT),
        ([{"id": APP, "inputExtensions": "x", "bots": [{"id": BOT}]}], BOT),
    ],
)
async def test_bot_id_from_the_app_definition(
    make: Callable[[Teams], CR.CardRefresher], definition: list[Any], bot: str
) -> None:
    """037-AC7: the matching app's `inputExtensions[0].botId`, else `bots[0].id`."""
    teams = Teams(apps=httpx.Response(200, json=definition))
    await _go(make(teams), _msg())
    assert teams.paths()[-1] == f"/v1/agents/28%3A{bot}/invoke"


async def test_refreshed_card_is_kept_and_shown_after_restart(
    make: Callable[[Teams], CR.CardRefresher], clock: Clock
) -> None:
    """037-AC2, 037-AC3: inside the interval the stored card is used with no request, from a new refresher
    too, and it is what the row renders; past the interval the bot is asked again."""
    message = _msg()
    teams = Teams()
    await _go(make(teams), message)
    assert len(teams.calls) == 2
    clock.now += CR.REFRESH_INTERVAL - 1
    teams2 = Teams()
    restarted = make(teams2)
    stored = restarted.stored(T, message)
    assert _text(stored["card-1"]) == "Ticket: DONE"
    assert _text((await _go(restarted, message))["card-1"]) == "Ticket: DONE"
    assert teams2.calls == []
    record = CV.chat_record(message, T, ME, cards=stored)
    assert "Ticket: DONE" in record["html"] and "Ticket: OPEN" not in record["html"]
    assert "Ticket: OPEN" in CV.chat_record(message, T, ME)["html"]
    assert "Ticket: DONE" in cards_html(message, overrides=stored)
    clock.now += 1
    teams2.invoke = httpx.Response(200, json=_ok("CLOSED"))
    assert _text((await _go(restarted, message))["card-1"]) == "Ticket: CLOSED"
    assert teams2.paths() == ["/api/mt/beta/users/apps/batchedDefinitions", INVOKE]


async def test_stored_card_decides_the_next_refresh(
    make: Callable[[Teams], CR.CardRefresher], clock: Clock
) -> None:
    """037-AC4: a refreshed card that drops its `refresh` is not refreshed again."""
    teams = Teams(invoke=httpx.Response(200, json={**_ok(), "value": _content("FINAL")}))
    refresher = make(teams)
    await _go(refresher, _msg())
    clock.now += CR.REFRESH_INTERVAL * 2
    assert _text((await _go(refresher, _msg()))["card-1"]) == "Ticket: FINAL"
    assert len(teams.calls) == 2


def _big() -> httpx.Response:
    return httpx.Response(200, json={**_ok(), "value": {**_content("x"), "pad": "x" * CR._CARD_MAX}})


@pytest.mark.parametrize(
    ("teams", "reason"),
    [
        (Teams(apps=httpx.Response(500)), "app_definition"),
        (Teams(apps=httpx.Response(200, json=[])), "app_definition"),
        (Teams(apps=httpx.Response(200, json={"id": APP})), "app_definition"),
        (Teams(apps=httpx.Response(200, json=[{"id": APP, "bots": [{"id": 5}]}])), "no_bot"),
        (Teams(apps=httpx.ConnectError("down")), "network"),
        (Teams(invoke=httpx.Response(401, json={"statusCode": 401})), "http"),
        (Teams(invoke=httpx.Response(500, text="oops")), "http"),
        (Teams(invoke=httpx.Response(200, json={"statusCode": 412, "type": "x"})), "status"),
        (Teams(invoke=httpx.Response(200, json=[_ok()])), "status"),
        (Teams(invoke=httpx.Response(200, text="not json")), "status"),
        (
            Teams(invoke=httpx.Response(200, json={**_ok(), "type": "application/vnd.microsoft.error"})),
            "not_a_card",
        ),
        (Teams(invoke=httpx.Response(200, json={**_ok(), "value": "card"})), "not_a_card"),
        (Teams(invoke=httpx.Response(200, json={**_ok(), "value": {"type": "HeroCard"}})), "not_a_card"),
        (Teams(invoke=_big()), "too_large"),
        (Teams(invoke=httpx.ReadTimeout("slow")), "timeout"),
    ],
)
async def test_failure_keeps_the_card_and_waits_the_interval(
    make: Callable[[Teams], CR.CardRefresher], clock: Clock, teams: Teams, reason: str
) -> None:
    """037-AC5: the card shown so far stays, one structured line without tokens, no retry before the
    interval; 037-AC7: a failed definition lookup is negative-cached as long."""
    message = _msg()
    good = make(Teams())
    await _go(good, message)
    clock.now += CR.REFRESH_INTERVAL
    refresher = make(teams)
    with capture_logs() as logs:
        got = await _go(refresher, message)
    assert _text(got["card-1"]) == "Ticket: DONE"
    assert _text(refresher.stored(T, message)["card-1"]) == "Ticket: DONE"
    (line,) = [entry for entry in logs if entry["event"] == "card_refresh_failed"]
    assert line["reason"] == reason and line["app_id"] == APP
    assert SKYPE not in str(logs) and BEARER not in str(logs) and "Ticket" not in str(logs)
    asked = len(teams.calls)
    clock.now += CR.REFRESH_INTERVAL - 1
    await _go(refresher, message)
    assert len(teams.calls) == asked
    clock.now += 1
    await _go(refresher, message)
    assert len(teams.calls) > asked


@pytest.mark.parametrize(
    ("reply", "wait"),
    [
        (httpx.Response(429, headers={"Retry-After": "3600"}), 3600),
        (httpx.Response(429, headers={"Retry-After": "5"}), CR.REFRESH_INTERVAL),
        (httpx.Response(200, json={"statusCode": 429, "value": 7200}), 7200),
        (httpx.Response(429, headers={"Retry-After": "60"}, json={"statusCode": 429, "value": 4000}), 4000),
        (httpx.Response(200, json={"statusCode": 429, "value": 10**12}), 86400),
        (httpx.Response(200, json={"statusCode": 429, "value": True}), CR.REFRESH_INTERVAL),
        (httpx.Response(429, headers={"Retry-After": "soon"}), CR.REFRESH_INTERVAL),
    ],
)
async def test_throttled_waits_its_retry_after(
    make: Callable[[Teams], CR.CardRefresher], tmp_path: Path, reply: httpx.Response, wait: float
) -> None:
    """037-AC5: a 429 waits at least its Retry-After (header or body `value`, both read as seconds), at
    most a day."""
    with capture_logs() as logs:
        assert await _go(make(Teams(invoke=reply)), _msg()) == {}
    assert logs[-1]["reason"] == "throttled" and logs[-1]["status"] == 429
    ((card, _, retry),) = CR.CardStore(tmp_path / "cards.db").load(T, "1700000000001").values()
    assert card is None and retry == T0 + wait


async def test_bot_ids_cached_bounded_and_negative_expiry(
    make: Callable[[Teams], CR.CardRefresher], clock: Clock
) -> None:
    """037-AC7: one lookup per app per process, at most 64 apps kept, an app without a bot asked again
    only after the refresh interval."""
    teams = Teams()
    refresher = make(teams)
    for i in range(3):
        await _go(refresher, _msg(mid=f"17000000000{i:02}"))
    assert teams.paths().count("/api/mt/beta/users/apps/batchedDefinitions") == 1

    async def any_app(request: httpx.Request) -> httpx.Response:
        (app,) = json.loads(request.content)
        return httpx.Response(200, json=[{"id": app, "bots": [{"id": BOT}]}])

    refresher._transport = httpx.MockTransport(any_app)
    for i in range(CR._BOTS_MAX + 5):
        assert await refresher._bot(f"app-{i}", SKYPE, BEARER) == BOT
    assert (
        len(refresher._bots) == CR._BOTS_MAX and APP not in refresher._bots and "app-0" not in refresher._bots
    )

    none = Teams(apps=httpx.Response(200, json=[{"id": APP}]))
    refresher = make(none)
    for i in range(3):
        await _go(refresher, _msg(mid=f"17000000001{i:02}"))
    assert len(none.calls) == 1
    clock.now += CR.REFRESH_INTERVAL
    await _go(refresher, _msg(mid="1700000000199"))
    assert len(none.calls) == 2


async def test_negative_cache_reports_the_original_failure(make: Callable[[Teams], CR.CardRefresher]) -> None:
    """037-AC5, 037-AC7: a cached failed lookup is logged with its own cause, not as a missing bot."""
    teams = Teams(apps=httpx.ConnectError("down"))
    refresher = make(teams)
    with capture_logs() as logs:
        await _go(refresher, _msg(mid="1700000000301"))
        await _go(refresher, _msg(mid="1700000000302"))
    assert [(e["reason"], e["status"]) for e in logs if e["event"] == "card_refresh_failed"] == [
        ("network", None),
        ("network", None),
    ]
    assert len(teams.calls) == 1


async def test_cache_hits_do_not_wait_on_another_apps_lookup(
    make: Callable[[Teams], CR.CardRefresher],
) -> None:
    """037-AC7: a known app's bot is answered while another app's definition lookup hangs."""
    gate = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return httpx.Response(200, json=[])

    refresher = make(Teams())
    refresher._bots[APP] = (BOT, float("inf"), None)
    refresher._transport = httpx.MockTransport(slow)
    hung = asyncio.create_task(refresher._bot("other-app", SKYPE, BEARER))
    await asyncio.sleep(0.01)
    async with asyncio.timeout(2):
        assert await refresher._bot(APP, SKYPE, BEARER) == BOT
    gate.set()
    with pytest.raises(CR.Failed):
        await hung


def test_one_refresher_across_event_loops(make: Callable[[Teams], CR.CardRefresher], clock: Clock) -> None:
    """037-AC2, 037-AC7: the MCP runs each tool in its own `asyncio.run`; the refresher works in each,
    and the bot id found in the first is reused in the second."""
    teams = Teams()
    refresher = make(teams)
    many = [_entry(cardClientId=f"card-{i}") for i in range(6)]

    async def contended() -> CR.Cards:
        # Callers queue on the semaphore, which binds it to this loop.
        teams.gate = asyncio.Event()
        task = asyncio.create_task(_go(refresher, _msg(*many)))
        await wait(lambda: teams.paths().count(INVOKE) % 6 == CR._CONCURRENCY, "4 invokes in flight")
        teams.gate.set()
        return await task

    with capture_logs() as logs:
        first = asyncio.run(contended())
        clock.now += CR.REFRESH_INTERVAL
        second = asyncio.run(contended())
    assert len(first) == len(second) == 6
    assert [e for e in logs if e["event"] == "card_refresh_failed"] == []
    assert teams.paths().count("/api/mt/beta/users/apps/batchedDefinitions") == 1
    assert teams.paths().count(INVOKE) == 12


async def test_unwritable_store_still_waits_the_interval(
    make: Callable[[Teams], CR.CardRefresher], clock: Clock, tmp_path: Path
) -> None:
    """037-AC5: when cards.db cannot be written, the retry time kept in memory still holds the bot off."""
    teams = Teams(invoke=httpx.Response(500))
    refresher = make(teams)
    refresher.store = CR.CardStore(tmp_path / "missing-dir" / "cards.db")
    for _ in range(5):
        await _go(refresher, _msg())
        clock.now += 60
    assert teams.paths().count(INVOKE) == 1
    clock.now += CR.REFRESH_INTERVAL
    await _go(refresher, _msg())
    assert teams.paths().count(INVOKE) == 2
    for i in range(CR._SAVED_MAX + 10):
        await refresher._save(T, str(i), "card-1", None, 1)
    assert len(refresher._saved) == CR._SAVED_MAX


async def test_unwritable_store_still_shows_the_refreshed_card(
    make: Callable[[Teams], CR.CardRefresher], clock: Clock, tmp_path: Path
) -> None:
    """037-AC5: with cards.db unwritable, the card refreshed in this process is the one shown, inside the
    interval and after a later failure."""
    teams = Teams()
    refresher = make(teams)
    refresher.store = CR.CardStore(tmp_path / "missing-dir" / "cards.db")
    assert _text((await _go(refresher, _msg()))["card-1"]) == "Ticket: DONE"
    clock.now += 60
    assert _text((await _go(refresher, _msg()))["card-1"]) == "Ticket: DONE"
    assert _text(refresher.stored(T, _msg())["card-1"]) == "Ticket: DONE"
    assert teams.paths().count(INVOKE) == 1
    teams.invoke = httpx.Response(500)
    clock.now += CR.REFRESH_INTERVAL
    assert _text((await _go(refresher, _msg()))["card-1"]) == "Ticket: DONE"
    assert _text(refresher.stored(T, _msg())["card-1"]) == "Ticket: DONE"
    assert teams.paths().count(INVOKE) == 2


async def test_the_later_save_wins(make: Callable[[Teams], CR.CardRefresher], clock: Clock) -> None:
    """037-AC3: a card another process stored after this one's save is shown, in the same second too (the
    store keeps seconds); an older stored one is not."""
    refresher = make(Teams())
    clock.now = T0 + 0.5
    await _go(refresher, _msg())
    store = refresher.store
    assert _text(refresher.stored(T, _msg())["card-1"]) == "Ticket: DONE"
    store.save(T, "1700000000001", "card-1", _content("SAME SECOND"), T0 + 0.9, T0 + 601)
    assert _text(refresher.stored(T, _msg())["card-1"]) == "Ticket: SAME SECOND"
    store.save(T, "1700000000001", "card-1", _content("ELSEWHERE"), T0 + 1, T0 + 601)
    assert _text(refresher.stored(T, _msg())["card-1"]) == "Ticket: ELSEWHERE"
    store.save(T, "1700000000001", "card-1", _content("OLDER"), T0 - 100, T0 + 601)
    assert _text(refresher.stored(T, _msg())["card-1"]) == "Ticket: DONE"


@pytest.mark.parametrize(
    ("apps", "reason"),
    [
        (httpx.Response(200, json=[{"id": APP, "bots": [{"id": BOT}]}]), None),
        (httpx.Response(500), "app_definition"),
    ],
)
async def test_concurrent_first_lookups_share_one_request(
    make: Callable[[Teams], CR.CardRefresher], apps: httpx.Response, reason: str | None
) -> None:
    """037-AC7: four messages of one app shown at once look its bot up once; a failed lookup gives every
    waiter the same reason and one negative entry."""
    gate = asyncio.Event()
    teams = Teams(apps=apps)
    asked: list[str] = []

    async def held(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("batchedDefinitions"):
            asked.append(request.url.path)
            await gate.wait()
        return await teams(request)

    refresher = make(teams)
    refresher._transport = httpx.MockTransport(held)
    with capture_logs() as logs:
        tasks = [asyncio.create_task(_go(refresher, _msg(mid=f"17000000004{i}"))) for i in range(4)]
        await wait(lambda: len(asked) == 1, "the lookup in flight")
        await asyncio.sleep(0.05)
        assert len(asked) == 1
        gate.set()
        got = [await t for t in tasks]
    assert teams.paths().count("/api/mt/beta/users/apps/batchedDefinitions") == 1
    assert refresher._lookups == {}
    failed = [e["reason"] for e in logs if e["event"] == "card_refresh_failed"]
    if reason is None:
        assert all(len(g) == 1 for g in got) and failed == []
    else:
        assert got == [{}] * 4 and failed == [reason] * 4 and refresher._bots[APP][0] == ""


async def test_definitions_429_holds_the_app(
    make: Callable[[Teams], CR.CardRefresher], clock: Clock, tmp_path: Path
) -> None:
    """037-AC5: a throttled definition lookup holds the app for its Retry-After: no lookup, no invoke, no
    log line until it ends; the card's retry is the hold's end."""
    teams = Teams(apps=httpx.Response(429, headers={"Retry-After": "3600"}))
    refresher = make(teams)
    with capture_logs() as logs:
        await _go(refresher, _msg())
        clock.now += CR.REFRESH_INTERVAL
        await _go(refresher, _msg())
        await _go(refresher, _msg(mid="1700000000501"))
    assert [e["reason"] for e in logs if e["event"] == "card_refresh_failed"] == ["throttled"]
    assert len(teams.calls) == 1
    assert refresher._bots[APP][1] == refresher._holds[APP] == T0 + 3600
    rows = CR.CardStore(tmp_path / "cards.db")
    assert rows.load(T, "1700000000001")["card-1"].retry == T0 + 3600
    assert rows.load(T, "1700000000501")["card-1"].retry == T0 + 3600
    teams.apps = httpx.Response(200, json=[{"id": APP, "bots": [{"id": BOT}]}])
    clock.now = T0 + 3600
    assert await _go(refresher, _msg())
    assert teams.paths() == ["/api/mt/beta/users/apps/batchedDefinitions"] * 2 + [INVOKE]


async def test_invoke_429_holds_every_card_of_the_app(make: Callable[[Teams], CR.CardRefresher]) -> None:
    """037-AC5: one throttled invoke holds the app: the other cards of that message and of five more
    messages ask nothing."""
    teams = Teams(invoke=httpx.Response(429, headers={"Retry-After": "3600"}))
    refresher = make(teams)
    with capture_logs() as logs:
        await _go(refresher, _msg(*(_entry(cardClientId=f"c{i}") for i in range(6))))
        for m in range(5):
            await _go(
                refresher, _msg(*(_entry(cardClientId=f"c{i}") for i in range(3)), mid=f"17000000006{m}")
            )
    assert teams.paths().count(INVOKE) == 1
    assert [e["reason"] for e in logs if e["event"] == "card_refresh_failed"] == ["throttled"]
    assert all(r.retry == T0 + 3600 for r in refresher._saved.values()) and len(refresher._saved) == 21


def test_holds_are_bounded_and_expire(make: Callable[[Teams], CR.CardRefresher], clock: Clock) -> None:
    """037-AC5: at most 64 apps held; a longer wait extends a hold, a shorter one does not shorten it; an
    ended hold is dropped and no longer holds."""
    refresher = make(Teams())
    for i in range(CR._HOLDS_MAX + 6):
        assert refresher._hold(f"app-{i}", 3600) == T0 + 3600
    assert len(refresher._holds) == CR._HOLDS_MAX and "app-0" not in refresher._holds
    assert refresher._hold("app-69", 60) == T0 + 3600
    assert refresher._hold("app-69", 7200) == refresher._held("app-69") == T0 + 7200
    clock.now += 7200
    assert refresher._held("app-69") == 0.0
    refresher._hold("fresh", 0)
    assert list(refresher._holds) == ["fresh"] and refresher._held("fresh") == T0 + 7200 + CR.REFRESH_INTERVAL


def test_a_closed_loop_does_not_block_the_next(
    make: Callable[[Teams], CR.CardRefresher], monkeypatch: pytest.MonkeyPatch
) -> None:
    """037-AC6: a loop closed with a refresh still pending leaves its in-flight entry; the next loop
    rebinds instead of refusing."""
    teams = Teams()
    refresher = make(teams)
    loop = asyncio.new_event_loop()
    teams.gate = asyncio.Event()
    task = loop.create_task(_go(refresher, _msg()))
    loop.run_until_complete(wait(lambda: teams.paths().count(INVOKE) == 1, "the invoke in flight"))
    assert refresher._inflight
    del task
    _abandon(loop, monkeypatch)
    teams.gate = None
    got = asyncio.run(_go(refresher, _msg(mid="1700000000901")))
    assert _text(got["card-1"]) == "Ticket: DONE"


def test_two_loops_at_once_are_refused(make: Callable[[Teams], CR.CardRefresher]) -> None:
    """037-AC6: one refresher on two running loops is a wiring bug: the second is refused before any state
    changes, and the first loop's refresh completes."""
    teams = Teams()
    started, release = threading.Event(), threading.Event()

    async def held(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/invoke"):
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
        return await teams(request)

    refresher = make(teams)
    refresher._transport = httpx.MockTransport(held)
    first: list[CR.Cards] = []
    thread = threading.Thread(target=lambda: first.append(asyncio.run(_go(refresher, _msg()))))
    with capture_logs() as logs:
        thread.start()
        try:
            assert started.wait(2)
            with pytest.raises(RuntimeError, match="two event loops"):
                asyncio.run(_go(refresher, _msg(mid="1700000000555")))
        finally:
            release.set()
            thread.join(5)
    assert _text(first[0]["card-1"]) == "Ticket: DONE"
    assert [e for e in logs if e["event"] == "card_refresh_failed"] == []
    assert not any(m == "1700000000555" for _, m, _ in refresher._saved)
    assert refresher.store.load(T, "1700000000555") == {}


class Trickle(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            await asyncio.sleep(0.01)
            yield b" "


async def test_a_trickling_reply_times_out_and_frees_its_slot(
    make: Callable[[Teams], CR.CardRefresher], monkeypatch: pytest.MonkeyPatch
) -> None:
    """037-AC5: a reply that never ends fails as a timeout within the whole-request deadline."""
    monkeypatch.setattr(CR, "_POST_DEADLINE", 0.1)
    refresher = make(Teams(invoke=httpx.Response(200, stream=Trickle())))
    with capture_logs() as logs:
        async with asyncio.timeout(2):
            assert await _go(refresher, _msg()) == {}
    assert [e["reason"] for e in logs if e["event"] == "card_refresh_failed"] == ["timeout"]
    assert refresher._sem._value == CR._CONCURRENCY


@pytest.mark.parametrize(("pad", "ok"), [(300 * 1024, True), (CR._DEFINITIONS_MAX, False)])
async def test_definitions_reply_has_its_own_cap(
    make: Callable[[Teams], CR.CardRefresher], pad: int, ok: bool
) -> None:
    """037-AC7: a large app definition (300 KiB) still names the bot; past 1 MiB it is a failure."""
    definition = [{"id": APP, "bots": [{"id": BOT}], "description": "x" * pad}]
    teams = Teams(apps=httpx.Response(200, json=definition))
    with capture_logs() as logs:
        got = await _go(make(teams), _msg())
    assert bool(got) is ok
    assert [e["reason"] for e in logs if e["event"] == "card_refresh_failed"] == ([] if ok else ["too_large"])


async def test_unexpected_error_names_its_class(
    make: Callable[[Teams], CR.CardRefresher], monkeypatch: pytest.MonkeyPatch
) -> None:
    """037-AC5: a bug is one `error` line naming the exception class, never its message."""
    refresher = make(Teams())

    async def bug(*args: Any) -> Any:
        raise KeyError(SKYPE)

    monkeypatch.setattr(refresher, "_invoke", bug)
    with capture_logs() as logs:
        assert await _go(refresher, _msg()) == {}
    (line,) = [e for e in logs if e["event"] == "card_refresh_failed"]
    assert line["reason"] == "error" and line["error"] == "KeyError" and SKYPE not in str(logs)


async def test_one_request_per_message_at_a_time(make: Callable[[Teams], CR.CardRefresher]) -> None:
    """037-AC6 (client side): concurrent callers share one request; a caller cancelled mid-way leaves the
    others their answer and the in-flight table empty."""
    teams = Teams()
    teams.gate = asyncio.Event()
    refresher = make(teams)
    a = asyncio.create_task(_go(refresher, _msg()))
    b = asyncio.create_task(_go(refresher, _msg()))
    c = asyncio.create_task(_go(refresher, _msg()))
    await wait(lambda: len(teams.calls) == 2, "the invoke in flight")
    a.cancel()
    teams.gate.set()
    got_b, got_c = await b, await c
    assert _text(got_b["card-1"]) == _text(got_c["card-1"]) == "Ticket: DONE"
    assert a.cancelled() and len(teams.calls) == 2 and refresher._inflight == {}


async def test_inflight_cleared_on_error_and_concurrency_capped(
    make: Callable[[Teams], CR.CardRefresher], monkeypatch: pytest.MonkeyPatch
) -> None:
    """037-AC6 (client side): an exception still clears the in-flight entry; at most 4 bot calls at once."""
    refresher = make(Teams())

    monkeypatch.setattr(refresher, "_due", lambda *a: (_ for _ in ()).throw(RuntimeError("bug")))
    with pytest.raises(RuntimeError):
        await _go(refresher, _msg())
    assert refresher._inflight == {}
    monkeypatch.undo()

    teams = Teams()
    teams.gate = asyncio.Event()
    refresher = make(teams)
    entries = [_entry(cardClientId=f"card-{i}") for i in range(8)]
    task = asyncio.create_task(_go(refresher, _msg(*entries)))
    await wait(lambda: teams.paths().count(INVOKE) == CR._CONCURRENCY, "4 invokes in flight")
    await asyncio.sleep(0.05)
    assert teams.paths().count(INVOKE) == CR._CONCURRENCY
    teams.gate.set()
    assert len(await task) == 8


async def test_a_failing_store_runner_is_a_failed_read_and_write(settings: Settings, tmp_path: Path) -> None:
    """037-AC5: the store runner raising (archive pool refusing, past its deadline) keeps the card shown so
    far and the refreshed one in memory; nothing raises, nothing stays in flight; no token logged."""

    async def refusing(fn: Callable[..., Any], *args: Any) -> Any:
        raise CV.ArchiveUnavailable("archive unavailable (relay)")

    teams = Teams()
    settings.contacts_host = "chat.example.test"
    refresher = CR.CardRefresher(
        settings, CR.CardStore(tmp_path / "cards.db"), transport=httpx.MockTransport(teams), blocking=refusing
    )
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    with capture_logs() as logs:
        got = await _go(refresher, _msg())
    assert _text(got["card-1"]) == "Ticket: DONE" and refresher._inflight == {}
    assert {e["event"] for e in logs} == {"card_store_read_failed", "card_store_write_failed"}
    assert SKYPE not in str(logs) and BEARER not in str(logs)
    assert not (tmp_path / "cards.db").exists()
    assert _text(refresher.stored(T, _msg())["card-1"]) == "Ticket: DONE"  # this process's save


def test_load_many_reads_a_page_in_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """037-AC2: one page's rows in few queries, each under the variable limit; the same rows as `load`."""
    monkeypatch.setattr(CR, "_IDS_PER_QUERY", 2)
    store = CR.CardStore(tmp_path / "cards.db")
    for i in range(5):
        store.save(T, str(i), "card-1", _content(f"S{i}"), T0, T0 + 60)
    store.save("19:other@thread.v2", "1", "card-1", _content("X"), T0, T0 + 60)
    rows = store.load_many(T, [str(i) for i in range(6)])
    assert sorted(rows) == ["0", "1", "2", "3", "4"]
    assert all(rows[str(i)] == store.load(T, str(i)) for i in range(5))
    assert _text(rows["4"]["card-1"].card) == "Ticket: S4" and rows["4"]["card-1"].retry == T0 + 60
    assert store.load_many(T, []) == {} and CR.CardStore(tmp_path / "none.db").load_many(T, ["1"]) == {}


async def test_no_tokens_no_request(make: Callable[[Teams], CR.CardRefresher]) -> None:
    """037-AC5: without tokens nothing is asked and nothing is marked failed."""
    teams = Teams()
    refresher = make(teams)
    assert await refresher.refresh(T, _msg(), me=ME, skype_token="", bearer=BEARER) == {}
    assert await refresher.refresh(T, _msg(), me=ME, skype_token=SKYPE, bearer="") == {}
    assert teams.calls == [] and not refresher.store.path.exists()


async def test_message_without_client_id(make: Callable[[Teams], CR.CardRefresher]) -> None:
    """037-AC2: a message without `clientmessageid` still asks, with an empty one."""
    teams = Teams()
    message = _msg()
    del message["clientmessageid"]
    await _go(make(teams), message)
    assert json.loads(teams.calls[-1].content)["clientMessageId"] == ""


async def test_refresh_leaves_the_archive_alone(
    make: Callable[[Teams], CR.CardRefresher], tmp_path: Path
) -> None:
    """037-AC10: the raw message and its messages.db row are unchanged; only cards.db is written."""
    message = _msg(version="1700000000001")
    chat = ChatStore(tmp_path, T)
    chat.insert_page([message])
    chat.close()
    db_file = chat.dir / "messages.db"
    with contextlib.closing(sqlite3.connect(db_file)) as db:
        before = db.execute("SELECT * FROM messages").fetchall()
    snapshot = copy.deepcopy(message)
    files = {p.name for p in tmp_path.rglob("*")}
    await _go(make(Teams()), message)
    assert message == snapshot
    with contextlib.closing(sqlite3.connect(db_file)) as db:
        assert db.execute("SELECT * FROM messages").fetchall() == before
    assert {p.name for p in tmp_path.rglob("*")} - files <= {"cards.db", "cards.db-wal", "cards.db-shm"}
    assert not (tmp_path / "index.db").exists()


def test_store_reads_survive_missing_corrupt_and_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """037-AC3: reading cards.db never raises into rendering, never creates it."""
    path = tmp_path / "cards.db"
    store = CR.CardStore(path)
    assert store.load(T, "1") == {} and not path.exists()
    assert CR.CardRefresher(settings, store).stored(T, _msg()) == {}
    store.save(T, "1", "card-1", _content("DONE"), T0, T0 + 1)
    assert _text(store.load(T, "1")["card-1"][0]) == "Ticket: DONE"
    monkeypatch.setattr(CR, "_BUSY_TIMEOUT", 0.05)
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as lock:
        lock.execute("PRAGMA locking_mode=EXCLUSIVE")
        lock.execute("BEGIN EXCLUSIVE")
        assert store.load(T, "1") == {}
        store.save(T, "1", "card-1", None, T0, T0 + 2)  # logged, not raised
        lock.execute("ROLLBACK")
    path.write_bytes(b"not a database" * 100)
    assert store.load(T, "1") == {}
    CR.CardStore(tmp_path / "gone" / "cards.db").save(T, "1", "card-1", _content("x"), T0, T0)
    assert not (tmp_path / "gone").exists()


def test_store_rows_and_failure_keeps_card(tmp_path: Path) -> None:
    """037-AC5: a failure only moves `retry_at`; the schema is the spec's."""
    path = tmp_path / "cards.db"
    store = CR.CardStore(path)
    store.save(T, "1", "card-1", _content("DONE"), T0, T0 + 600)
    store.save(T, "1", "card-1", None, T0 + 10, T0 + 900)
    with contextlib.closing(sqlite3.connect(path)) as db:
        assert [r[1] for r in db.execute("PRAGMA table_info(cards)")] == [
            "thread_id",
            "message_id",
            "card_id",
            "card",
            "refreshed_at",
            "retry_at",
        ]
        ((card, refreshed, retry),) = db.execute("SELECT card, refreshed_at, retry_at FROM cards").fetchall()
        assert json.loads(card) == _content("DONE")
        assert refreshed == CR._iso(T0) and retry == CR._iso(T0 + 900)
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        db.execute("UPDATE cards SET card = '[1]', retry_at = 'garbage'")
        db.commit()
        assert store.load(T, "1") == {"card-1": (None, T0, 0.0)}
    db_text = '{"type": "AdaptiveCard", "body": ['
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE cards SET card = ?, retry_at = ?", (db_text, CR._iso(T0)))
    assert store.load(T, "1") == {"card-1": (None, T0, T0)}


async def test_cards_without_id(make: Callable[[Teams], CR.CardRefresher]) -> None:
    """037-AC4: a card without cardClientId is neither stored nor refreshed."""
    teams = Teams()
    refresher = make(teams)
    bare = {k: v for k, v in _entry().items() if k != "cardClientId"}
    assert refresher.stored(T, _msg(bare)) == {}
    assert await _go(refresher, _msg(bare)) == {}
    assert teams.calls == [] and not refresher.store.path.exists()


class Recorded(httpx.AsyncClient):
    """The client a refresh opens, kept to check it is closed."""

    made: list[Recorded] = []

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        Recorded.made.append(self)


async def test_cancelled_refresh_closes_its_client(
    make: Callable[[Teams], CR.CardRefresher], monkeypatch: pytest.MonkeyPatch
) -> None:
    """037-AC6 (client side): a refresh cancelled mid-request closes its client and leaves no in-flight
    entry; a finished one, and the bot lookup, close theirs too."""
    monkeypatch.setattr(Recorded, "made", [])
    monkeypatch.setattr(CR.httpx, "AsyncClient", Recorded)
    teams = Teams()
    teams.gate = asyncio.Event()
    refresher = make(teams)
    caller = asyncio.create_task(_go(refresher, _msg()))
    await wait(lambda: teams.paths().count(INVOKE) == 1, "the invoke in flight")
    (inner,) = refresher._inflight.values()
    inner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await wait(lambda: refresher._inflight == {}, "in-flight cleared")
    teams.gate.set()
    assert await _go(refresher, _msg(mid="1700000000777"))
    assert len(Recorded.made) == 3 and all(c.is_closed for c in Recorded.made)  # two messages, one lookup


async def test_concurrency_cap_holds_across_messages(make: Callable[[Teams], CR.CardRefresher]) -> None:
    """037-AC6 (client side): the cap is per refresher, not per message."""
    teams = Teams()
    teams.gate = asyncio.Event()
    refresher = make(teams)
    tasks = [
        asyncio.create_task(
            _go(refresher, _msg(*(_entry(cardClientId=f"c{i}") for i in range(3)), mid=f"17{m}"))
        )
        for m in range(3)
    ]
    await wait(lambda: teams.paths().count(INVOKE) == CR._CONCURRENCY, "4 invokes in flight")
    await asyncio.sleep(0.05)
    assert teams.paths().count(INVOKE) == CR._CONCURRENCY
    teams.gate.set()
    assert [len(await t) for t in tasks] == [3, 3, 3]


def test_overrides_only_replace_their_card() -> None:
    """037-AC3: an override replaces the content of its cardClientId only; hostile overrides are ignored."""
    other = {**_entry("OTHER"), "cardClientId": "card-2"}
    message = _msg(_entry(), other, {"content": _content("NOID")})
    out = cards_html(message, overrides={"card-1": _content("DONE"), "card-2": "x", "0": _content("BAD")})  # type: ignore[dict-item]
    assert "Ticket: DONE" in out and "Ticket: OTHER" in out and "Ticket: NOID" in out
    assert "Ticket: OPEN" not in out and "BAD" not in out


async def test_a_longer_concurrent_429_extends_the_hold(
    make: Callable[[Teams], CR.CardRefresher], clock: Clock
) -> None:
    """037-AC5: two invokes in flight answer 429 for 600 s then for 3600 s: the hold and the second card
    wait 3600 s, and the first card asks nothing before then."""
    gate = asyncio.Event()
    replies = [600, 3600]
    teams = Teams()

    async def throttled(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/invoke"):
            return await teams(request)
        teams.calls.append(request)
        await gate.wait()
        return httpx.Response(429, headers={"Retry-After": str(replies.pop(0))})

    refresher = make(teams)
    refresher._transport = httpx.MockTransport(throttled)
    message = _msg(_entry(cardClientId="a"), _entry(cardClientId="b"))
    task = asyncio.create_task(_go(refresher, message))
    await wait(lambda: teams.paths().count(INVOKE) == 2, "both invokes in flight")
    gate.set()
    assert await task == {}
    assert refresher._held(APP) == T0 + 3600
    assert max(r.retry for r in refresher._saved.values()) == T0 + 3600
    clock.now += CR.REFRESH_INTERVAL
    await _go(refresher, message)
    assert teams.paths().count(INVOKE) == 2
    assert all(r.retry >= T0 + 3600 for r in refresher._saved.values())


def _abandon(loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch) -> None:
    """Close `loop` with its tasks still pending; their teardown at collection is the scenario."""
    monkeypatch.setattr(sys, "unraisablehook", lambda unraisable: None)
    loop.set_exception_handler(lambda loop, context: None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        loop.close()
        gc.collect()


def test_a_closed_loop_lookup_does_not_block_the_next(
    make: Callable[[Teams], CR.CardRefresher], monkeypatch: pytest.MonkeyPatch
) -> None:
    """037-AC7: a bot lookup left pending in a closed loop is not awaited by the next loop."""
    gate: list[asyncio.Event] = []
    teams = Teams()

    async def held(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("batchedDefinitions") and gate:
            await gate[0].wait()
        return await teams(request)

    refresher = make(teams)
    refresher._transport = httpx.MockTransport(held)
    loop = asyncio.new_event_loop()
    gate.append(asyncio.Event())
    pending = loop.create_task(_go(refresher, _msg()))
    loop.run_until_complete(
        wait(lambda: bool(refresher._lookups) and len(teams.calls) == 0, "lookup pending")
    )
    loop.run_until_complete(asyncio.sleep(0.02))
    assert refresher._lookups and APP not in refresher._bots
    del pending
    _abandon(loop, monkeypatch)
    gate.clear()

    async def again() -> CR.Cards:
        async with asyncio.timeout(2):
            return await _go(refresher, _msg(mid="1700000000902"))

    assert _text(asyncio.run(again())["card-1"]) == "Ticket: DONE"


async def test_a_cancelled_first_caller_leaves_the_shared_lookup(
    make: Callable[[Teams], CR.CardRefresher],
) -> None:
    """037-AC7: two messages wait on one lookup; cancelling the first one's refresh does not cancel the
    lookup the second waits on."""
    gate = asyncio.Event()
    teams = Teams()
    asked: list[str] = []

    async def held(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("batchedDefinitions"):
            asked.append(request.url.path)
            await gate.wait()
        return await teams(request)

    refresher = make(teams)
    refresher._transport = httpx.MockTransport(held)
    first = asyncio.create_task(_go(refresher, _msg()))
    second = asyncio.create_task(_go(refresher, _msg(mid="1700000000903")))
    await wait(lambda: len(asked) == 1 and len(refresher._inflight) == 2, "both waiting on the lookup")
    await asyncio.sleep(0.02)
    refresher._inflight[(T, "1700000000001", 0)].cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    async with asyncio.timeout(2):
        assert _text((await second)["card-1"]) == "Ticket: DONE"
    assert len(asked) == 1


async def test_a_newer_version_does_not_join_the_older_ones_refresh(
    make: Callable[[Teams], CR.CardRefresher],
) -> None:
    """037-AC6: two views of one version share a request; an edit adding a card, refreshed meanwhile, gets
    its own, so the added card is asked too."""
    teams = Teams()
    teams.gate = asyncio.Event()
    refresher = make(teams)
    old = _msg(version="1000")
    first = asyncio.create_task(_go(refresher, old))
    again = asyncio.create_task(_go(refresher, dict(old)))
    await wait(lambda: teams.paths().count(INVOKE) == 1, "the first invoke")
    edited = _msg(_entry(), _entry(cardClientId="card-2"), version="2000")
    newer = asyncio.create_task(_go(refresher, edited))
    await wait(lambda: len(refresher._inflight) == 2, "the newer refresh")
    teams.gate.set()
    assert sorted(await newer) == ["card-1", "card-2"]
    assert sorted(await first) == sorted(await again) == ["card-1"]
    assert refresher._inflight == {}
