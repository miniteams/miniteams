"""Presence collector (spec 015 phase 2): snapshot, subscription, callbacks, staleness, follow list, cache.

Synthetic MRIs, a mocked presence service (httpx.MockTransport), no network."""

import asyncio
import contextlib
import ctypes.util
import json
import logging
import os
import signal
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import structlog.testing

from miniteams import idle as I
from miniteams import presence as P
from miniteams import web as W
from miniteams.config import Settings
from miniteams.directory import Directory
from miniteams.stream import TrouterClient


def _mri(n: int) -> str:
    return f"8:orgid:00000000-0000-4000-8000-{n:012d}"


ME, ANN, BOB, CID, UNK = _mri(1), _mri(2), _mri(3), _mri(4), _mri(5)
EPID, SURL = "epid-1", "https://trouter.example/v4/f/abc/"
PERIOD = 600.0


def _entry(mri: str, availability: str, activity: str = "") -> dict[str, Any]:
    return {
        "mri": mri,
        "etag": "e",
        "presence": {"availability": availability, "activity": activity or availability},
    }


class Service:
    """The presence service: records every request; `states` answer snapshots, `unknown` gets 40020."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.states: dict[str, tuple[str, str]] = {}
        self.fail = False
        self.refused: set[str] = set()  # 40020 on subscribe whatever the snapshot said
        self.endpoint_fail = False  # 503 on publishing our endpoint only
        self.force_fail = False  # 503 on forceavailability only
        self.reread_fail = False  # 503 on getpresence only
        self.own: dict[str, Any] | None = None  # my entry as the service holds it (a re-read's answer)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(503)
        body = json.loads(request.content)
        if request.url.path == "/v1/me/endpoints/":
            return httpx.Response(503 if self.endpoint_fail else 200)
        if request.url.path == "/v1/me/forceavailability/":
            return httpx.Response(503 if self.force_fail else 200)
        if request.url.path == "/v1/presence/getpresence/":
            if self.reread_fail:
                return httpx.Response(503)
            out = []
            for item in body:
                if item["mri"] == ME and self.own is not None:
                    out.append(self.own)
                    continue
                a, x = self.states.get(item["mri"], ("PresenceUnknown", "PresenceUnknown"))
                out.append(_entry(item["mri"], a, x))
            return httpx.Response(200, json=out)
        adds = [
            {"mri": i["mri"], "status": "20200" if i["mri"] in self.states.keys() - self.refused else "40020"}
            for i in body["subscriptionsToAdd"]
        ]
        removes = [{"mri": i["mri"], "status": "20200"} for i in body["subscriptionsToRemove"]]
        return httpx.Response(200, json={"addStatuses": adds, "removeStatuses": removes})

    def bodies(self, path: str) -> list[Any]:
        return [json.loads(r.content) for r in self.requests if r.url.path.startswith(path)]


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def service() -> Iterator[Service]:
    found = Service()
    yield found
    # The spike: reportmyactivity shows nothing and keeps no endpoint alive; it is never sent.
    assert not [r for r in found.requests if "reportmyactivity" in r.url.path]


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _presence(
    settings: Settings,
    service: Service,
    clock: Clock,
    cache: P.Cache | None = None,
    token: P.TokenFn = lambda: "tok",
) -> tuple[P.Presence, list[int]]:
    changes: list[int] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(service.handler))
    on_change = lambda: changes.append(1)  # noqa: E731
    p = P.Presence(
        settings, token, on_change, cache, client=client, period=PERIOD, clock=clock, idle=_at_desk
    )
    return p, changes


async def _at_desk() -> float | None:
    return 0.0


async def _settle(p: P.Presence) -> None:
    """Run the debounced post now and wait for it."""
    if p._pending is not None:
        p._pending.cancel()
        p._pending = None
    await p.sync()


def _shown(p: P.Presence) -> dict[str, Any]:
    return dict(p.frame()["presence"])


# --- pure parsing ---


def test_parse_unknown_and_malformed_are_not_offline() -> None:
    """015-AC3: PresenceUnknown or a missing presence is unknown (None), never Offline; junk is skipped."""
    found = P.parse(
        [
            _entry(ANN.upper(), "Busy", "InAMeeting"),
            {"mri": BOB, "presence": {"availability": "PresenceUnknown"}},
            {"mri": CID},
            {"mri": "28:bot", "presence": {"availability": "Available"}},
            "junk",
            None,
        ]
    )
    assert found == {ANN: ("Busy", "InAMeeting", None, None), BOB: None, CID: None}
    assert P.parse("not a list") == {}


def test_dm_peer_is_the_other_member_only() -> None:
    oid = lambda m: m.removeprefix("8:orgid:")  # noqa: E731
    assert P.dm_peer(f"19:{oid(ME)}_{oid(ANN)}@unq.gbl.spaces", ME) == ANN
    assert P.dm_peer(f"19:{oid(ANN)}_{oid(ME)}@unq.gbl.spaces", ME) == ANN
    assert P.dm_peer(f"19:{oid(ME)}_{oid(ME)}@unq.gbl.spaces", ME) == ""  # self-chat
    assert P.dm_peer(f"19:{oid(ANN)}_{oid(BOB)}@unq.gbl.spaces", ME) == ""  # not mine
    assert P.dm_peer("19:abc@thread.v2", ME) == ""
    assert P.dm_peer(f"19:{oid(ME)}_{oid(ANN)}@unq.gbl.spaces", "") == ""


def test_dm_ids_are_both_orders_of_two_org_users() -> None:
    """Spec 023: the 1:1 of two people, either order; none for me, a non-user or a missing id."""
    oid = lambda m: m.removeprefix("8:orgid:")  # noqa: E731
    both = f"19:{oid(ME)}_{oid(ANN)}@unq.gbl.spaces", f"19:{oid(ANN)}_{oid(ME)}@unq.gbl.spaces"
    assert P.dm_ids(ME, ANN) == both
    assert P.dm_ids(ME, ANN.upper().replace("8:ORGID:", "8:orgid:")) == P.dm_ids(ME, ANN)
    for me, mri in ((ME, ME), (ME, "28:bot-id"), (ME, None), (ME, ""), ("", ANN), ("28:bot-id", ANN)):
        assert P.dm_ids(me, mri) == ()


# --- snapshot + subscription on the web's own socket ---


async def test_session_snapshots_then_subscribes_on_the_socket_surl(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """015-AC3: snapshot then subscription on `<surl>/TeamsUnifiedPresenceService`; 40020 → no state."""
    service.states = {ANN: ("Busy", "InAMeeting"), BOB: ("Offline", "OffWork")}
    p, changes = _presence(settings, service, clock)
    p.want([ANN, BOB, UNK, "not-an-mri"])
    await p.on_session(EPID, SURL)
    await _settle(p)
    (snapshot,) = service.bodies("/v1/presence/getpresence/")
    assert sorted(i["mri"] for i in snapshot) == [ANN, BOB, UNK]
    (sub,) = service.bodies("/v1/pubsub/subscriptions/")
    assert service.requests[-1].url.path == f"/v1/pubsub/subscriptions/{EPID}"
    assert sub["trouterUri"] == SURL.rstrip("/") + "/TeamsUnifiedPresenceService"
    assert sub["shouldPurgePreviousSubscriptions"] is True
    assert sorted(i["mri"] for i in sub["subscriptionsToAdd"]) == [ANN, BOB, UNK]
    assert _shown(p) == {
        ANN: {"availability": "Busy", "activity": "InAMeeting", "confirmed": True},
        BOB: {"availability": "Offline", "activity": "OffWork", "confirmed": True},
    }  # UNK: unknown, absent, never Offline
    assert changes
    await p.close()


async def test_callback_updates_the_person_and_tells_the_pages(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """015-AC4: a Trouter callback changes the state and calls on_change; an unchanged one stays quiet."""
    service.states = {ANN: ("Available", "Available")}
    p, changes = _presence(settings, service, clock)
    p.want([ANN])
    await p.on_session(EPID, SURL)
    await _settle(p)
    changes.clear()
    await p.on_presence({"presence": [_entry(ANN, "Busy", "InACall")]})
    assert _shown(p)[ANN]["availability"] == "Busy" and changes == [1]
    await p.on_presence({"presence": [_entry(ANN, "Busy", "InACall")]})
    assert changes == [1]  # a renewed deadline alone is no news
    await p.on_presence({"presence": [_entry(ANN, "PresenceUnknown")]})
    assert ANN not in _shown(p) and changes == [1, 1]
    await p.close()


async def test_trouter_routes_presence_deliveries_after_the_ack(
    settings: Settings, directory: Directory
) -> None:
    """015-AC4: the web socket hands presence deliveries to the hook, acked first, chat untouched."""
    got: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    async def on_presence(body: dict[str, Any]) -> None:
        got.append(body)

    async def on_event(obj: dict[str, Any]) -> None:
        events.append(obj)

    client = TrouterClient(
        settings,
        {"access_token": "a"},
        "sk",
        {},
        "s",
        "e",
        directory,
        on_event=on_event,
        on_presence=on_presence,
    )
    sent: list[str] = []

    class Ws:
        async def send(self, data: str) -> None:
            sent.append(data)

    client._ws = Ws()
    body = {"presence": [_entry(ANN, "Away")]}
    for path in ("TeamsUnifiedPresenceService", "unifiedPresenceService"):
        await client._on_delivery(
            json.dumps({"id": 1, "url": f"https://h/x/{path}", "body": json.dumps(body)})
        )
    event = {"type": "EventMessage", "resourceType": "NewMessage", "resource": {"id": "1"}}
    await client._on_delivery(
        json.dumps({"id": 2, "url": "https://h/x/messaging", "body": json.dumps(event)})
    )
    assert got == [body, body] and events == [event]
    assert len(sent) == 3 and all(s.startswith("3:::") for s in sent)


# --- freshness ---


async def test_two_periods_without_confirmation_drop_the_dot_and_a_reconnect_restores_it(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """015-AC5: stale after two periods (a dead socket confirms nothing); a reconnect re-subscribes all."""
    service.states = {ANN: ("Available", "Available")}
    p, changes = _presence(settings, service, clock)
    p.want([ANN])
    await p.on_session(EPID, SURL)
    await _settle(p)
    clock.now += 2 * PERIOD - 1
    p.sweep()
    assert ANN in _shown(p)
    clock.now += 2
    changes.clear()
    p.sweep()
    assert _shown(p) == {} and changes == [1]
    service.requests.clear()
    await p.on_session(EPID, "https://trouter.example/v4/f/new/")
    await _settle(p)
    (sub,) = service.bodies("/v1/pubsub/subscriptions/")
    assert sub["trouterUri"] == "https://trouter.example/v4/f/new/TeamsUnifiedPresenceService"
    assert service.bodies("/v1/presence/getpresence/") == [[{"mri": ANN}]]
    assert _shown(p)[ANN]["confirmed"] is True
    await p.close()


async def test_subscription_40020_means_unknown(settings: Settings, service: Service, clock: Clock) -> None:
    """015-AC3: a person the subscription refuses (40020) is unknown: their dot goes, not Offline."""
    service.states = {ANN: ("Available", ""), BOB: ("Offline", "")}
    service.refused = {BOB}
    p, _ = _presence(settings, service, clock)
    p.want([ANN, BOB])
    await p.on_session(EPID, SURL)
    await _settle(p)
    assert sorted(_shown(p)) == [ANN]
    await p.close()


async def _until(check: Any) -> None:
    for _ in range(400):
        if check():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never met")


async def test_renewal_every_period_confirms_unchanged_states(
    settings: Settings, service: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """015-AC5: the periodic re-post (no snapshot) is confirmed by its snapshot callback; a failed one
    confirms nothing."""
    monkeypatch.setattr(P, "_SWEEP", 0.001)
    service.states = {ANN: ("Away", "Away")}
    p, _ = _presence(settings, service, clock)
    p.want([ANN])
    await p.on_session(EPID, SURL)
    await _settle(p)
    service.requests.clear()
    clock.now += PERIOD
    renewals = asyncio.create_task(p.run())
    await _until(lambda: service.requests)
    renewals.cancel()
    assert [r.url.path for r in service.requests] == [
        "/v1/me/endpoints/",
        f"/v1/pubsub/subscriptions/{EPID}",
    ]
    await p.on_presence({"presence": [_entry(ANN, "Away")]})  # the renewal's snapshot callback
    assert p.states[ANN].until == clock.now + 2 * PERIOD
    clock.now += PERIOD
    service.fail = True
    await p.sync(renew=True)
    clock.now += PERIOD + 1
    p.sweep()
    assert _shown(p) == {}
    await p.close()


async def test_accepted_renewals_without_a_callback_go_stale(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """015-AC5: a dead socket still gets 20200 on every renewal; without a callback the dot goes."""
    service.states = {ANN: ("Away", "Away")}
    p, _ = _presence(settings, service, clock)
    p.want([ANN])
    await p.on_session(EPID, SURL)
    await _settle(p)
    for _ in range(2):
        clock.now += PERIOD
        await p.sync(renew=True)
        p.sweep()
    assert service.bodies("/v1/pubsub/subscriptions/")[-1]["subscriptionsToAdd"] == [{"mri": ANN}]
    assert _shown(p) == {}
    await p.close()


async def test_an_unchanged_follow_list_is_posted_again_only_to_renew(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """015-AC6: a queued sync with nothing changed posts nothing; a renewal posts the same set again."""
    service.states = {ANN: ("Away", "Away")}
    p, _ = _presence(settings, service, clock)
    p.want([ANN])
    await p.on_session(EPID, SURL)
    await _settle(p)
    service.requests.clear()
    await p.sync()
    assert service.requests == []
    await p.sync(renew=True)
    assert [r.url.path for r in service.requests] == [
        "/v1/me/endpoints/",
        f"/v1/pubsub/subscriptions/{EPID}",
    ]
    await p.close()


async def test_a_raising_token_or_sync_never_ends_the_renewals(
    settings: Settings, service: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """015-AC5: MSAL raising (network down) fails one post, not the renewal loop: the dots still go
    stale after two periods, and the loop survives any error of its own."""
    monkeypatch.setattr(P, "_SWEEP", 0.001)
    calls: list[int] = []

    def token() -> str:
        calls.append(1)
        if len(calls) > 1:
            raise ConnectionError("network down")
        return "tok"

    service.states = {ANN: ("Away", "Away")}
    p, _ = _presence(settings, service, clock, token=token)
    p.want([ANN])
    await p.on_session(EPID, SURL)
    await _settle(p)
    p.want([ANN, BOB])
    await _settle(p)  # a debounced post: the raise must not escape it
    assert len(calls) == 2
    renewals = asyncio.create_task(p.run())
    clock.now += PERIOD
    await _until(lambda: len(calls) >= 3)  # the renewal, then the due re-put of my endpoint
    clock.now += PERIOD + 1
    await _until(lambda: _shown(p) == {})
    assert not renewals.done()

    async def broken(renew: bool = False) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(p, "sync", broken)
    clock.now += PERIOD
    await asyncio.sleep(0.05)
    assert not renewals.done()
    renewals.cancel()
    calls.clear()  # the network is back for the unsubscribe
    await p.close()


# --- follow list ---


async def test_follow_list_changes_add_and_remove_subscriptions(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """015-AC6: a new follow is snapshotted and added, a dropped one removed and no longer shown."""
    service.states = {ANN: ("Available", ""), BOB: ("Busy", ""), CID: ("Away", "")}
    p, _ = _presence(settings, service, clock)
    p.want([ANN, BOB])
    await p.on_session(EPID, SURL)
    await _settle(p)
    service.requests.clear()
    p.want([BOB, CID])
    await _settle(p)
    assert service.bodies("/v1/presence/getpresence/") == [[{"mri": CID}]]
    (sub,) = service.bodies("/v1/pubsub/subscriptions/")
    assert [i["mri"] for i in sub["subscriptionsToAdd"]] == sorted([BOB, CID])
    assert [i["mri"] for i in sub["subscriptionsToRemove"]] == [ANN]
    assert sorted(_shown(p)) == sorted([BOB, CID])
    assert p.states[ANN].confirmed is False  # no update will come for her any more
    await p.close()
    (cleanup,) = service.bodies("/v1/pubsub/subscriptions/")[1:]
    assert cleanup["subscriptionsToAdd"] == [] and len(cleanup["subscriptionsToRemove"]) == 2


class _Recorder:
    def __init__(self) -> None:
        self.wanted: list[str] = []

    def want(self, mris: list[str]) -> None:
        self.wanted = list(mris)

    def frame(self) -> dict[str, Any]:
        return {"presence": {}}


def _dm(n: int) -> str:
    return f"19:{ME.removeprefix('8:orgid:')}_{_mri(100 + n).removeprefix('8:orgid:')}@unq.gbl.spaces"


class _Sock:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


async def test_board_follows_the_50_newest_peers_plus_bounded_authors() -> None:
    """015-AC6: ≤50 1:1 peers (newest first, groups ignored) + each chat page's ≤50 org authors."""
    rows = {
        _dm(i): {"id": _dm(i), "label": "x", "last_activity": f"2026-09-15T10:{i:02d}:00Z", "typing": []}
        for i in range(60)
    }
    rows["19:g@thread.v2"] = {"id": "19:g@thread.v2", "label": "g", "last_activity": "2027", "typing": []}
    board = W.Board(rows, me=ME)
    rec = _Recorder()
    board.presence = rec  # type: ignore[assignment]
    board.broadcast()
    assert rec.wanted == [ME, *(_mri(100 + i) for i in range(59, 9, -1))]  # 036-AC6: me first, always
    page: Any = _Sock()
    authors = ["28:bot", "junk", 5, *[_mri(500 + i) for i in range(70)], _mri(500)]
    await board._on_authors(page, {"authors": authors})
    assert rec.wanted[51:] == [_mri(500 + i) for i in range(49)]
    assert json.loads(page.sent[-1]) == {"presence": {}}  # the page gets the current states at once
    payload = json.loads(board.payload())
    assert {r["id"]: r["peer"] for r in payload["rows"]}[_dm(3)] == _mri(103)
    for n in range(6):  # many chat tabs: the whole follow list stays bounded, peers first
        await board._on_authors(_Sock(), {"authors": [_mri(1000 + 50 * n + i) for i in range(50)]})  # type: ignore[arg-type]
    assert len(rec.wanted) == 100 and rec.wanted[1:51] == [_mri(100 + i) for i in range(59, 9, -1)]
    tabs: list[Any] = [_Sock(), _Sock()]  # the same chat in two tabs counts its authors once
    board._authors.clear()
    for tab in tabs:
        await board._on_authors(tab, {"authors": [_mri(500 + i) for i in range(50)]})
    assert len(rec.wanted) == 100


# --- cache ---


async def test_restart_shows_cached_states_unconfirmed_until_the_first_callback(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """015-AC7: the cache file is local, owner-only; a restart paints it unconfirmed, a callback confirms."""
    service.states = {ANN: ("Busy", "InAMeeting"), BOB: ("Available", "")}
    cache = P.open_cache(settings)
    assert cache is not None
    path = settings.cache_dir / "presence.sqlite3"
    assert path.exists() and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    p, _ = _presence(settings, service, clock, cache)
    p.want([ANN, BOB])
    await p.on_session(EPID, SURL)
    await _settle(p)
    await p.close()

    clock.now += 60  # a quick restart
    service.requests.clear()
    p2, _ = _presence(settings, service, clock, P.open_cache(settings))
    p2.want([ANN, BOB])
    assert _shown(p2) == {
        ANN: {"availability": "Busy", "activity": "InAMeeting", "confirmed": False},
        BOB: {"availability": "Available", "activity": "Available", "confirmed": False},
    }
    assert service.requests == []  # painted from disk, before any request
    await p2.on_presence({"presence": [_entry(ANN, "Busy", "InAMeeting")]})
    assert _shown(p2)[ANN]["confirmed"] is True and _shown(p2)[BOB]["confirmed"] is False
    await p2.close()

    clock.now += 2 * PERIOD  # a restart after the deadline: nothing stale is shown
    p3, _ = _presence(settings, service, clock, P.open_cache(settings))
    p3.want([ANN, BOB])
    assert _shown(p3) == {}
    await p3.close()
    db = P.open_cache(settings)
    assert db is not None
    columns = {c for (_, c, *_) in db.db.execute("PRAGMA table_info(presence)")}
    assert columns == {"tenant_id", "mri", "availability", "activity", "observed_at_ms", "confirmed_until_ms"}
    tables = [t for (t,) in db.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    assert tables == ["presence", "horizons"]  # read receipts (spec 011): no names either
    columns = {c for (_, c, *_) in db.db.execute("PRAGMA table_info(horizons)")}
    assert columns == {"tenant_id", "thread_id", "mri", "msg_id", "updated_at_ms"}
    db.close()


async def test_a_person_turned_unknown_is_not_repainted_from_the_cache(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """015-AC7: unknown (callback or staleness) also leaves the cache: a restart shows no old dot."""
    service.states = {ANN: ("Busy", "InAMeeting"), BOB: ("Available", "")}
    p, _ = _presence(settings, service, clock, P.open_cache(settings))
    p.want([ANN, BOB])
    await p.on_session(EPID, SURL)
    await _settle(p)
    await p.on_presence({"presence": [_entry(ANN, "PresenceUnknown")]})
    await p.close()
    p2, _ = _presence(settings, service, clock, P.open_cache(settings))
    p2.want([ANN, BOB])
    assert sorted(_shown(p2)) == [BOB]
    await p2.close()


@pytest.mark.parametrize("damage", ["header", "pages"])
async def test_a_corrupt_cache_file_is_replaced(
    settings: Settings, service: Service, clock: Clock, damage: str
) -> None:
    """015-AC7: the cache is regenerable: a scrambled file is dropped and rebuilt, presence still starts."""
    cache = P.open_cache(settings)
    assert cache is not None
    for i in range(300):
        cache.save({_mri(10 + i): P.State("Away", "Away", clock.now + 60, True)}, clock.now)
    cache.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    cache.close()
    path = settings.cache_dir / "presence.sqlite3"
    data = bytearray(path.read_bytes())
    if damage == "header":
        data[:100] = os.urandom(100)
    else:  # past the header: the file opens, its first read fails
        data[4096:] = os.urandom(len(data) - 4096)
    path.write_bytes(bytes(data))
    cache = P.open_cache(settings)
    assert cache is not None and cache.load(clock.now) == {}
    p, _ = _presence(settings, service, clock, cache)
    p.want([ANN])
    await p.on_presence({"presence": [_entry(ANN, "Busy")]})
    assert cache.load(clock.now)[ANN].availability == "Busy"
    await p.close()


def test_cache_stays_bounded(settings: Settings, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    """015-AC6/AC7: the cache keeps the newest observations only."""
    monkeypatch.setattr(P, "_CACHE_MAX", 3)
    cache = P.open_cache(settings)
    assert cache is not None
    for i in range(6):
        cache.save({_mri(10 + i): P.State("Away", "Away", clock.now + 60, True)}, clock.now + i)
    left = {m for (m,) in cache.db.execute("SELECT mri FROM presence")}
    assert left == {_mri(13), _mri(14), _mri(15)}
    cache.close()


# --- own presence, Graph ---


async def test_the_collector_publishes_only_its_endpoint_never_graph(
    settings: Settings, service: Service, clock: Clock, directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """015-AC8: the collector by itself only reads presence, posts our own subscriptions and publishes its
    socket as my endpoint (picks and auto-Away are the only forced statuses), never Graph; web's
    socket, wired with the presence hooks, still sends its usual user.activity."""
    service.states = {ANN: ("Available", "")}
    p, _ = _presence(settings, service, clock)
    p.want([ANN, ME])
    await p.on_session(EPID, SURL)
    await _settle(p)
    await p.sync()
    await p.close()
    assert {r.url.host for r in service.requests} == {"presence.teams.microsoft.com"}
    assert {(r.method, r.url.path) for r in service.requests} == {
        ("PUT", "/v1/me/endpoints/"),
        ("POST", "/v1/presence/getpresence/"),
        ("POST", f"/v1/pubsub/subscriptions/{EPID}"),
    }

    sent: list[str] = []

    class Ws:
        async def send(self, data: str) -> None:
            sent.append(data)

    socket = TrouterClient(
        settings,
        {"access_token": "a"},
        "sk",
        {"connectparams": {}},
        "s",
        "e",
        directory,
        on_session=p.on_session,
        on_presence=p.on_presence,
    )
    socket._ws = Ws()
    await socket._authenticate()
    assert [json.loads(f.split(":", 3)[3])["name"] for f in sent] == ["user.authenticate", "user.activity"]

    seen: dict[str, Any] = {}
    calls: list[str] = []

    async def forever(*a: Any, **kw: Any) -> None:
        seen.update(kw)
        calls.append("stream")

    async def renewals(self: P.Presence) -> None:
        calls.append("renewals")
        await asyncio.Event().wait()

    follow = W.Board._follow_presence

    def seed(board: W.Board) -> None:
        calls.append("follow")
        follow(board)

    async def idle(*a: Any, **kw: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(W, "run_forever", forever)
    monkeypatch.setattr(W, "serve_board", idle)
    monkeypatch.setattr(W, "fill_from_listing", idle)
    monkeypatch.setattr(W.EmojiCache, "refresh", idle)
    monkeypatch.setattr(P.Presence, "run", renewals)
    monkeypatch.setattr(W.Board, "_follow_presence", seed)
    with pytest.raises(RuntimeError, match="live stream ended"):
        await W.run(settings, "sk", "", None, None, me=ME)
    assert seen["on_presence"] and seen["on_session"]
    assert "renewals" in calls and calls.index("follow") < calls.index("stream")  # seeded before the socket
    assert "user_activity" not in seen  # the widget's signal is untouched


# --- own presence (spec 036) ---


def _own(availability: str, activity: str, forced: dict[str, str] | None = None) -> dict[str, Any]:
    """My entry as the spike recorded it (misc/presence-spike/own*.json): forcedAvailability while forced."""
    entry = _entry(ME, availability, activity)
    if forced is not None:
        expiry = (
            "9999-12-31T23:59:59.9999999Z" if forced["availability"] == "Offline" else "2026-11-04T14:39:00Z"
        )
        entry["presence"]["forcedAvailability"] = {
            **forced,
            "expiry": expiry,
            "publishTime": "2026-10-28T14:39Z",
        }
    return entry


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        *(
            (_own(a, a, {"availability": a, "activity": a}), a)
            for a in ("Available", "Busy", "DoNotDisturb", "BeRightBack", "Away")
        ),
        (_own("Offline", "OffWork", {"availability": "Offline", "activity": "OffWork"}), "AppearOffline"),
        (_own("Busy", "InAMeeting"), None),  # after a Reset: no forcedAvailability, the automatic status
        (_own("Busy", "Busy", {"availability": "Bogus", "activity": "Busy"}), None),
        ({"mri": ME, "presence": {"availability": "Busy", "forcedAvailability": "Busy"}}, None),
    ],
)
def test_forced_availability_reads_as_the_menu_entry(entry: dict[str, Any], expected: str | None) -> None:
    """036-AC7/AC8: each forced status of the spike reads as its menu entry; a lifted one (Reset) as none."""
    ((_, (_, _, forced, _)),) = P.parse([entry]).items()  # type: ignore[misc]
    assert forced == expected


async def test_own_entry_carries_forced_and_others_do_not(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """036-AC6/AC7: the frame names my entry (`self`) and only mine carries `forced`; a lifted force alone
    is news for the pages; a cached state of mine has none."""
    changes: list[int] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(service.handler))
    p = P.Presence(
        settings, lambda: "tok", lambda: changes.append(1), client=client, clock=clock, me=ME.upper()
    )
    p.want([ME, ANN])
    busy = {"availability": "Busy", "activity": "Busy"}
    await p.on_presence(
        {
            "presence": [
                _own("Busy", "Busy", busy),
                {
                    **_entry(ANN, "Busy"),
                    "presence": {**_entry(ANN, "Busy")["presence"], "forcedAvailability": busy},
                },
            ]
        }
    )
    frame = p.frame()
    assert frame["self"] == ME
    assert frame["presence"][ME] == {
        "availability": "Busy",
        "activity": "Busy",
        "confirmed": True,
        "forced": "Busy",
    }
    assert frame["presence"][ANN] == {"availability": "Busy", "activity": "Busy", "confirmed": True}
    changes.clear()
    await p.on_presence({"presence": [_own("Busy", "Busy")]})  # Reset while my calendar says Busy too
    assert p.frame()["presence"][ME]["forced"] is None and changes == [1]
    await p.close()
    cache = P.Cache(settings.cache_dir / "presence.sqlite3", settings.tenant_id)
    cache.save({ME: P.State("Busy", "Busy", clock.now + 60, True, "Busy")}, clock.now)
    cached = P.Presence(
        settings, lambda: "tok", lambda: None, cache, client=httpx.AsyncClient(), clock=clock, me=ME
    )
    cached.want([ME])
    assert cached.frame()["presence"][ME] == {
        "availability": "Busy",
        "activity": "Busy",
        "confirmed": False,
        "forced": None,
    }
    await cached.close()


async def test_offline_presence_frame_has_no_self(settings: Settings, clock: Clock) -> None:
    """036-AC6: without my MRI the frame names no entry: the pages show the unknown ring."""
    p = P.Presence(settings, lambda: "tok", lambda: None, client=httpx.AsyncClient(), clock=clock)
    assert p.frame() == {"presence": {}}
    await p.close()


SETS = {  # 036-AC7/AC8: menu value → the exact forceavailability body (spike, phase 1)
    "Available": {"availability": "Available"},
    "Busy": {"availability": "Busy"},
    "DoNotDisturb": {"availability": "DoNotDisturb"},
    "BeRightBack": {"availability": "BeRightBack"},
    "Away": {"availability": "Away"},
    "AppearOffline": {"availability": "Offline", "activity": "OffWork"},
    "Reset": {"availability": "Offline"},
}
FORCE_URL = "https://presence.teams.microsoft.com/v1/me/forceavailability/"


def _forcing(
    settings: Settings, handler: Any, token: P.TokenFn = lambda: "tok"
) -> tuple[W.Board, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    async def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        out = handler(request)
        return await out if asyncio.iscoroutine(out) else out  # type: ignore[no-any-return]

    board = W.Board({}, me=ME)
    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    board.presence = P.Presence(settings, token, board.broadcast_presence, client=client, me=ME)
    return board, seen


async def _ask(board: W.Board, *verbs: dict[str, Any]) -> list[dict[str, Any]]:
    """Each verb through the socket dispatch, then every background answer awaited: the frames sent back."""
    ws: Any = _Sock()
    for verb in verbs:
        key = W._verb_key(board._handlers, board._accepts, verb)
        assert key == "setpresence"
        await board._dispatch(ws, key, verb)
    while board.chat._replies:
        await asyncio.gather(*board.chat._replies)
    return [json.loads(f) for f in ws.sent]


async def test_each_pick_sends_its_forceavailability_call(settings: Settings) -> None:
    """036-AC7/AC8: each menu value is one PUT with its exact body, the presence token, and an ok answer."""
    board, seen = _forcing(settings, lambda r: httpx.Response(200))
    for value, body in SETS.items():
        seen.clear()
        assert await _ask(board, {"setpresence": value}) == [{"setpresence": value, "ok": True}]
        ((request,),) = [seen]
        assert (request.method, str(request.url), json.loads(request.content)) == ("PUT", FORCE_URL, body)
        assert request.headers["Authorization"] == "Bearer tok"
    await board.presence.close()  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "value", ["Offline", "available", "", "Reset ", "busy", 5, None, ["Busy"], {"Busy": 1}]
)
async def test_a_bad_value_is_refused_without_a_call(settings: Settings, value: Any) -> None:
    """036-AC9: anything outside the menu is refused at once, the service never called."""
    board, seen = _forcing(settings, lambda r: pytest.fail("no call for a refused value"))
    assert await _ask(board, {"setpresence": value}) == [
        {"setpresence": value, "ok": False, "error": "unknown status"}
    ]
    assert seen == []
    await board.presence.close()  # type: ignore[union-attr]


def _raise_token() -> str:
    raise RuntimeError("network down")


@pytest.mark.parametrize(
    ("handler", "token", "error", "calls"),
    [
        (
            lambda r: httpx.Response(401, json={"substatuscode": {"value": 40102}}),
            lambda: "tok",
            "HTTP 401",
            1,
        ),
        (lambda r: httpx.Response(503), lambda: "tok", "HTTP 503", 1),
        (lambda r: pytest.fail("no call without a token"), lambda: None, "token unavailable", 0),
        (lambda r: pytest.fail("no call without a token"), _raise_token, "token unavailable", 0),
        (
            lambda r: (_ for _ in ()).throw(httpx.ConnectError("down", request=r)),
            lambda: "tok",
            "network error",
            1,
        ),
    ],
)
async def test_a_failed_set_answers_its_reason_once(
    settings: Settings, handler: Any, token: P.TokenFn, error: str, calls: int
) -> None:
    """036-AC9: an HTTP error, no token or a network error answers the reason; nothing is retried, my
    state is untouched, the log has the event and never the token."""
    board, seen = _forcing(settings, handler, token)
    assert board.presence is not None
    board.presence.want([ME])
    await board.presence.on_presence({"presence": [_own("Busy", "InAMeeting")]})
    before = board.presence_frame()
    with structlog.testing.capture_logs() as logs:
        assert await _ask(board, {"setpresence": "DoNotDisturb"}) == [
            {"setpresence": "DoNotDisturb", "ok": False, "error": error}
        ]
    assert len(seen) == calls
    assert board.presence_frame() == before
    assert [e["event"] for e in logs] == ["presence_set_failed"] and "tok" not in repr(logs).replace(
        "token", ""
    )
    await board.presence.close()


async def test_offline_board_refuses_and_one_set_at_a_time(settings: Settings) -> None:
    """036-AC9: no presence collector → refused; a pick from any page (another socket too) while one is in
    flight answers busy: an older pick never lands after a newer one."""
    offline = W.Board({}, me=ME)
    assert await _ask(offline, {"setpresence": "Busy"}) == [
        {"setpresence": "Busy", "ok": False, "error": "presence unavailable"}
    ]
    gate = asyncio.Event()

    async def held(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return httpx.Response(200)

    board, seen = _forcing(settings, held)
    widget: Any = _Sock()
    chat: Any = _Sock()
    await board._dispatch(widget, "setpresence", {"setpresence": "Busy"})
    await board._dispatch(widget, "setpresence", {"setpresence": "Away"})
    await board._dispatch(chat, "setpresence", {"setpresence": "Away"})
    assert [json.loads(f) for f in widget.sent] == [{"setpresence": "Away", "ok": False, "error": "busy"}]
    assert [json.loads(f) for f in chat.sent] == [{"setpresence": "Away", "ok": False, "error": "busy"}]
    gate.set()
    while board.chat._replies:
        await asyncio.gather(*board.chat._replies)
    assert json.loads(widget.sent[-1]) == {"setpresence": "Busy", "ok": True} and len(seen) == 1
    assert await _ask(board, {"setpresence": "Reset"}) == [{"setpresence": "Reset", "ok": True}]  # freed
    await board.presence.close()  # type: ignore[union-attr]


async def test_a_raising_set_frees_the_slot(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """036-AC9: a set that raises answers a failure and frees the board-wide slot: the next pick goes out."""
    board, seen = _forcing(settings, lambda r: httpx.Response(200))
    assert board.presence is not None
    real, raised = board.presence.force, []

    async def once(value: str) -> str:
        if not raised:
            raised.append(value)
            raise RuntimeError("boom")
        return await real(value)

    monkeypatch.setattr(board.presence, "force", once)
    assert await _ask(board, {"setpresence": "Busy"}) == [{"setpresence": "Busy", "error": "failed"}]
    assert await _ask(board, {"setpresence": "Away"}) == [{"setpresence": "Away", "ok": True}]
    assert len(seen) == 1
    await board.presence.close()


async def test_the_socket_is_published_as_my_endpoint(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """With the Teams client closed, no endpoint of mine is left: Offline, and a forced status shows nowhere.
    Each socket is published once as my endpoint (Transport activity), again on renewal and reconnect."""
    service.states = {ANN: ("Available", "")}
    p, _ = _presence(settings, service, clock)
    p.want([ANN])
    await p.on_session(EPID, SURL)
    await _settle(p)
    assert service.bodies("/v1/me/endpoints/") == [
        {
            "id": EPID,
            "availability": "Available",
            "activity": "Available",
            "activityReporting": "Transport",
            "deviceType": "Desktop",
        }
    ]
    p.want([ANN, BOB])
    await _settle(p)  # a follow change is not a new socket
    assert len(service.bodies("/v1/me/endpoints/")) == 1
    await p.sync(renew=True)
    await p.on_session(EPID, SURL + "2/")  # the id is persisted: a reconnect keeps it
    await _settle(p)
    assert [b["id"] for b in service.bodies("/v1/me/endpoints/")] == [EPID, EPID, EPID]
    await p.close()


async def test_a_refused_endpoint_keeps_the_dots(settings: Settings, service: Service, clock: Clock) -> None:
    """A refused publish is logged and tried again by the next post; the subscription still goes out."""
    service.states, service.endpoint_fail = {ANN: ("Available", "")}, True
    p, _ = _presence(settings, service, clock)
    p.want([ANN])
    await p.on_session(EPID, SURL)
    await _settle(p)
    assert p.subscribed == {ANN} and p.published is None
    service.endpoint_fail = False
    await p.sync()  # unchanged follow list: the publish alone is retried
    assert p.published == EPID and p.subscribed == {ANN}
    await p.close()


async def test_an_empty_follow_list_still_publishes(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """Nobody followed yet (no rows, `me` unresolved): the socket is still published as my endpoint."""
    p, _ = _presence(settings, service, clock)
    await p.on_session(EPID, SURL)
    await _settle(p)
    assert p.published == EPID and service.bodies("/v1/pubsub/") == []
    await p.close()


async def test_a_reconnect_during_a_publish_publishes_again(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """A new socket under the same id while a publish is in flight: the new socket is published too."""
    p, _ = _presence(settings, service, clock)
    real = service.handler

    def reconnect_meanwhile(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/me/endpoints/" and p.session == (EPID, SURL):
            p.session, p.published = (EPID, SURL + "2/"), None  # what on_session does, mid-request
        return real(request)

    p.http = httpx.AsyncClient(transport=httpx.MockTransport(reconnect_meanwhile))
    await p.on_session(EPID, SURL)
    await _settle(p)
    assert p.published is None
    await p.sync()
    assert p.published == EPID and len(service.bodies("/v1/me/endpoints/")) == 2
    await p.close()


async def test_the_sweep_retries_a_refused_publish(
    settings: Settings, service: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused publish is retried by the sweep, not left until the next renewal."""
    service.states, service.endpoint_fail = {ANN: ("Available", "")}, True
    p, _ = _presence(settings, service, clock)
    p.want([ANN])
    await p.on_session(EPID, SURL)
    await _settle(p)
    service.endpoint_fail = False
    p.renewed = clock()  # no renewal due: only the retry can publish
    monkeypatch.setattr(P, "_SWEEP", 0.01)
    loop = asyncio.create_task(p.run())
    for _ in range(200):
        if p.published == EPID:
            break
        await asyncio.sleep(0.01)
    loop.cancel()
    assert p.published == EPID
    await p.close()


async def _loop_for(p: P.Presence, seconds: float = 0.2) -> None:
    loop = asyncio.create_task(p.run())
    await asyncio.sleep(seconds)
    loop.cancel()


async def test_a_refused_publish_backs_off(
    settings: Settings, service: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publish refused for good is retried 30 s, 60 s, … apart (up to a period), not every sweep."""
    service.states, service.endpoint_fail = {ANN: ("Available", "")}, True
    p, _ = _presence(settings, service, clock)
    p.want([ANN])
    await p.on_session(EPID, SURL)
    await _settle(p)
    p.renewed = clock()
    monkeypatch.setattr(P, "_SWEEP", 0.005)
    tries = lambda: len(service.bodies("/v1/me/endpoints/"))  # noqa: E731
    await _loop_for(p)
    assert tries() == 2  # the first retry, then the wait
    clock.now += 30
    await _loop_for(p)
    assert tries() == 3
    clock.now += 30
    await _loop_for(p)
    assert tries() == 3  # the wait doubled
    clock.now += 30
    await _loop_for(p)
    assert tries() == 4
    await p.on_session(EPID, SURL + "2/")  # a new socket starts the wait over
    await _settle(p)
    assert tries() == 5
    await _loop_for(p)
    assert tries() == 6  # retried at once, then 30 s
    clock.now += 30
    await _loop_for(p)
    assert tries() == 7
    service.endpoint_fail = False
    clock.now += 60
    await _loop_for(p)
    assert p.published == EPID and tries() == 8
    service.endpoint_fail = True
    clock.now += 240  # a re-put refused: the wait starts over from 30 s
    await _loop_for(p)
    assert tries() == 9 and p.published is None
    clock.now += 30
    await _loop_for(p)
    assert tries() == 10
    await p.close()


async def test_my_endpoint_is_put_again_every_four_minutes(
    settings: Settings, service: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unrenewed, my endpoint shows inactive after ~6 min and goes after ~11 (spike): it is put again every
    240 s, not only on the 10-min renewal; a refused re-put falls back to the retries."""
    p, _ = _presence(settings, service, clock)
    await p.on_session(EPID, SURL)
    await _settle(p)
    p.renewed = clock.now + 10 * PERIOD  # no renewal in this test: only the re-put can publish
    monkeypatch.setattr(P, "_SWEEP", 0.005)
    puts = lambda: len(service.bodies("/v1/me/endpoints/"))  # noqa: E731
    clock.now += 239
    await _loop_for(p)
    assert puts() == 1
    clock.now += 1
    await _loop_for(p)
    assert puts() == 2  # once, not once per sweep
    clock.now += 240
    await _loop_for(p)
    assert puts() == 3 and p.published == EPID
    tokens: list[int] = []

    def no_token() -> None:
        tokens.append(1)

    p.token = no_token
    clock.now += 240
    await _loop_for(p)
    assert tokens == [1]  # no token: not once per sweep
    clock.now += 30
    await _loop_for(p)
    assert tokens == [1, 1]  # backed off like a refusal, not left until the next interval
    p.token = lambda: "tok"
    service.endpoint_fail = True
    clock.now += 60
    await _loop_for(p)
    assert p.published is None and puts() == 4
    clock.now += 120
    await _loop_for(p)
    assert puts() == 5  # retried, backing off
    await p.close()


# --- auto-Away (desktop idle) ---

FORCES = "/v1/me/forceavailability/"
AWAY = {"availability": "Away", "activity": "Away"}
BUSY = {"availability": "Busy", "activity": "Busy"}


class Desk:
    """`web` following me: the idle it samples, my callbacks, and the service's answer to a re-read."""

    def __init__(self, p: P.Presence, service: Service, clock: Clock) -> None:
        self.p, self.service, self.clock = p, service, clock
        self.seconds: float | None = 0.0
        self.snapshots = 0  # my getpresence calls before the auto-Away ones

    def rereads(self) -> int:
        return (
            sum(b == [{"mri": ME}] for b in self.service.bodies("/v1/presence/getpresence/")) - self.snapshots
        )

    async def idle(self) -> float | None:
        return self.seconds

    async def sweep(self, seconds: float | None = ...) -> None:  # type: ignore[assignment]
        """One sweep's idle sample, then its auto-Away check."""
        if seconds is not ...:
            self.seconds = seconds
        self.p.idle_seconds = await self.p.idle()
        await self.p.away()

    async def callback(
        self,
        forced: dict[str, str] | None = None,
        expiry: str = "",
        availability: str = "",
        late: bool = False,
    ) -> None:
        """My callback, a tick after the last set (a pick or auto-Away); the service holds the same, unless
        `late` (a stale callback)."""
        self.clock.now += 1
        avail = availability or (forced["availability"] if forced else "Available")
        entry = _own(avail, avail, forced)
        if forced and expiry:
            entry["presence"]["forcedAvailability"]["expiry"] = expiry
        if not late:
            self.service.own = entry
        await self.p.on_presence({"presence": [entry]})

    async def took(self) -> None:
        """The callback that follows the last force sent: forced as sent, expiring as asked."""
        body = self.service.bodies(FORCES)[-1]
        if body == {"availability": "Offline"}:
            await self.callback()
        else:
            avail = body["availability"]
            await self.callback(
                {"availability": avail, "activity": avail}, body.get("desiredExpirationTime", "")
            )


async def _auto(
    settings: Settings, service: Service, clock: Clock, forced: dict[str, str] | None = None
) -> Desk:
    service.states = {ME: ("Available", "")}
    client = httpx.AsyncClient(transport=httpx.MockTransport(service.handler))
    p = P.Presence(settings, lambda: "tok", lambda: None, client=client, period=PERIOD, clock=clock, me=ME)
    desk = Desk(p, service, clock)
    p.idle = desk.idle
    p.pages(1)  # the widget is open
    p.want([ME])
    await p.on_session(EPID, SURL)
    await _settle(p)
    await desk.callback(forced)
    desk.snapshots = desk.rereads()
    return desk


def _forces(service: Service) -> list[tuple[str, bool]]:
    """Each forceavailability sent: (availability, carries an expiry of its own)."""
    return [(b["availability"], "desiredExpirationTime" in b) for b in service.bodies(FORCES)]


async def test_desktop_idle_forces_away_then_lifts_it(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """5 min of desktop idle with no status forced: Away, through spec 036's call; back at the desktop, that
    Away is lifted (Reset)."""
    d = await _auto(settings, service, clock)
    await d.sweep(299)
    assert _forces(service) == []
    await d.sweep(300)
    assert _forces(service) == [("Away", True)] and d.p.auto_away
    await d.sweep(1)  # back before my callback: that state predates the set, it says nothing of the Away
    assert d.p.auto_away and len(_forces(service)) == 1
    await d.took()
    await d.sweep(300)
    assert len(_forces(service)) == 1  # already auto-Away
    await d.sweep(2)
    assert _forces(service) == [("Away", True), ("Offline", False)] and not d.p.auto_away
    await d.took()
    await d.sweep()
    await d.p.close()
    assert len(_forces(service)) == 2 and d.rereads() == 0  # nothing left to lift


async def test_my_callback_before_the_answer_counts(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """My Away callback arrives while the PUT is still waiting for its answer: that state is after the set,
    so my return decides on it and lifts the Away."""
    d = await _auto(settings, service, clock)

    async def early(request: httpx.Request) -> httpx.Response:
        answer = service.handler(request)
        body = json.loads(request.content)
        if request.url.path == FORCES and "desiredExpirationTime" in body:
            await d.callback(AWAY, expiry=body["desiredExpirationTime"])  # Trouter beats the HTTP answer
            clock.now += 1
        return answer

    d.p.http = httpx.AsyncClient(transport=httpx.MockTransport(early))
    await d.sweep(600)
    clock.now += 30
    await d.sweep(1)
    assert _forces(service) == [("Away", True), ("Offline", False)] and not d.p.auto_away
    await d.p.close()


@pytest.mark.parametrize(
    "forced", [BUSY, AWAY, {"availability": "Offline", "activity": "OffWork"}, {"availability": "Available"}]
)
async def test_a_forced_status_is_never_auto_away(
    settings: Settings, service: Service, clock: Clock, forced: dict[str, str]
) -> None:
    """A status I picked (Away and Appear offline included) is mine: idle never overrides it."""
    d = await _auto(settings, service, clock, forced)
    await d.sweep(3600)
    assert _forces(service) == [] and not d.p.auto_away
    await d.p.close()


@pytest.mark.parametrize("availability", ["Busy", "DoNotDisturb"])
async def test_a_meeting_or_call_is_never_hidden_by_auto_away(
    settings: Settings, service: Service, clock: Clock, availability: str
) -> None:
    """Busy or Do not disturb unforced (meeting, call, presenting, calendar): idle forces nothing; once
    Available again while still idle, Away on the next sweep."""
    d = await _auto(settings, service, clock)
    await d.callback(availability=availability)
    await d.sweep(600)
    assert _forces(service) == [] and not d.p.auto_away
    await d.callback()
    await d.sweep()
    assert _forces(service) == [("Away", True)] and d.p.auto_away
    await d.p.close()


async def test_unknown_own_state_or_idle_does_nothing(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """My state unknown or only cached, or no idle sample: no auto-Away."""
    d = await _auto(settings, service, clock)
    mine = d.p.states[ME]
    d.p.states[ME] = replace(mine, confirmed=False)  # as loaded from the cache: `forced` is never cached
    await d.sweep(3600)
    del d.p.states[ME]
    await d.sweep()
    d.p.states[ME] = mine
    await d.sweep(None)
    assert _forces(service) == [] and not d.p.auto_away
    await d.p.close()


async def test_no_auto_away_without_my_endpoint(settings: Settings, service: Service, clock: Clock) -> None:
    """Unpublished (refused), my endpoint carries no Away: the idle check waits for it."""
    service.endpoint_fail = True
    d = await _auto(settings, service, clock)
    await d.sweep(3600)
    assert _forces(service) == [] and d.p.published is None
    await d.p.close()


async def test_a_status_changed_in_teams_during_auto_away_stays(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """Set to Busy elsewhere while auto-Away (the service confirms it on a re-read): coming back lifts
    nothing, nor does the shutdown, and the Away is not renewed over it."""
    d = await _auto(settings, service, clock)
    await d.sweep(600)
    await d.took()
    await d.callback(BUSY)  # the Teams client, a phone…
    await d.p.close()
    assert _forces(service) == [("Away", True)]
    d = await _auto(settings, service, clock)
    await d.sweep(600)
    await d.took()
    await d.callback(BUSY)
    clock.now += 600
    await d.sweep()  # a renewal would be due
    assert not d.p.auto_away and d.rereads() == 1
    await d.sweep(1)
    await d.p.close()
    assert _forces(service) == [("Away", True)] * 2


async def test_a_stale_callback_does_not_end_auto_away(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """A late callback without my Away is checked against a re-read first: the service still holds it, so
    auto-Away goes on and is lifted on return; a failed re-read decides nothing."""
    d = await _auto(settings, service, clock)
    await d.sweep(600)
    await d.took()
    await d.callback(late=True)  # an older delivery, after the Away one
    service.reread_fail = True
    await d.sweep()
    assert d.p.auto_away and d.rereads() == 1
    service.reread_fail = False
    await d.sweep()
    assert d.p.auto_away and d.rereads() == 2 and d.p.states[ME].forced == "Away"
    await d.sweep(1)
    assert _forces(service) == [("Away", True), ("Offline", False)] and not d.p.auto_away
    await d.p.close()


async def test_an_away_picked_in_teams_during_auto_away_is_left_alone(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """Away picked in the Teams client while auto-Away: same availability, Teams' own expiry, not ours. It is
    never renewed, lifted on return nor at shutdown."""
    d = await _auto(settings, service, clock)
    await d.sweep(600)
    await d.took()
    await d.callback(AWAY, expiry="2026-11-05T09:00:00.0000000Z")
    clock.now += 600
    await d.sweep()
    await d.sweep(1)
    await d.p.close()
    assert _forces(service) == [("Away", True)] and not d.p.auto_away


async def test_my_away_expiry_matches_to_the_second(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """The service may answer the expiry with a fraction: still the Away we sent."""
    d = await _auto(settings, service, clock)
    await d.sweep(600)
    sent = service.bodies(FORCES)[-1]["desiredExpirationTime"]
    await d.callback(AWAY, expiry=sent.removesuffix("Z") + ".0000000Z")
    await d.sweep(1)
    assert _forces(service) == [("Away", True), ("Offline", False)] and d.rereads() == 0
    await d.p.close()


async def test_a_menu_pick_during_auto_away_owns_my_status(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """A pick while auto-Away ends it: back at the desktop, the pick is not reset."""
    d = await _auto(settings, service, clock)
    await d.sweep(600)
    await d.took()
    assert await d.p.force("Away") == ""  # the same status, now picked
    assert not d.p.auto_away
    await d.took()
    await d.sweep(1)
    await d.p.close()
    assert _forces(service) == [("Away", True), ("Away", False)]  # the pick: Teams' own expiry


def _holding_busy(d: Desk, gate: asyncio.Event) -> None:
    async def held(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content) == {"availability": "Busy"}:
            await gate.wait()
        return d.service.handler(request)

    d.p.http = httpx.AsyncClient(transport=httpx.MockTransport(held))


async def test_a_pick_racing_the_idle_check_wins(settings: Settings, service: Service, clock: Clock) -> None:
    """A pick in flight, or taken but not yet confirmed by my callback, is never overridden by auto-Away."""
    d = await _auto(settings, service, clock)
    d.seconds, gate = 600, asyncio.Event()
    _holding_busy(d, gate)
    pick = asyncio.create_task(d.p.force("Busy"))
    await _until(lambda: d.p._setting.locked())
    auto = asyncio.create_task(d.sweep())
    await asyncio.sleep(0.01)
    gate.set()
    assert await pick == ""
    await auto
    await d.sweep()  # taken, my callback not in yet
    assert _forces(service) == [("Busy", False)] and not d.p.auto_away
    await d.took()
    await d.sweep()
    await d.p.close()
    assert _forces(service) == [("Busy", False)]


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_an_auto_away_expires_unless_renewed_while_idle(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """A forced Away lasts 7 days by default: auto-Away asks for 15 min (a crash leaves no more) and is
    forced again, still idle, once less than 10 min are left; never before; a refused renewal is retried on
    the next sweep; a renewal not confirmed yet is still mine."""
    d = await _auto(settings, service, clock)
    await d.sweep(600)
    sent = clock.now
    assert service.bodies(FORCES) == [{"availability": "Away", "desiredExpirationTime": _iso(sent + 900)}]
    await d.took()
    clock.now = sent + 299  # 601 s left
    await d.sweep()
    assert len(_forces(service)) == 1
    clock.now = sent + 301  # 599 s left
    await d.sweep()
    assert service.bodies(FORCES)[-1] == {
        "availability": "Away",
        "desiredExpirationTime": _iso(clock.now + 900),
    }
    await d.sweep()
    assert len(_forces(service)) == 2  # 900 s left again
    service.force_fail = True
    clock.now += 301
    await d.sweep()  # no callback since the renewal: my state carries the previous expiry, still mine
    assert len(_forces(service)) == 3 and d.p.auto_away and d.rereads() == 0
    service.force_fail = False
    await d.sweep()
    assert _forces(service) == [("Away", True)] * 4
    await d.took()
    await d.sweep(1)
    assert _forces(service)[-1] == ("Offline", False) and d.rereads() == 0
    await d.p.close()


async def test_a_pick_racing_a_renewal_wins(settings: Settings, service: Service, clock: Clock) -> None:
    """A pick in flight when the renewal comes due: the renewal never lands over it."""
    d = await _auto(settings, service, clock)
    await d.sweep(600)
    await d.took()
    clock.now += 400
    gate = asyncio.Event()
    _holding_busy(d, gate)
    pick = asyncio.create_task(d.p.force("Busy"))
    await _until(lambda: d.p._setting.locked())
    renewal = asyncio.create_task(d.sweep())
    await asyncio.sleep(0.01)
    gate.set()
    assert await pick == ""
    await renewal
    await d.p.close()
    assert _forces(service) == [("Away", True), ("Busy", False)]


async def test_a_pick_racing_the_return_wins(settings: Settings, service: Service, clock: Clock) -> None:
    """A pick in flight when I come back: the Reset never lands over it."""
    d = await _auto(settings, service, clock)
    await d.sweep(600)
    await d.took()
    gate = asyncio.Event()
    _holding_busy(d, gate)
    pick = asyncio.create_task(d.p.force("Busy"))
    await _until(lambda: d.p._setting.locked())
    back = asyncio.create_task(d.sweep(1))
    await asyncio.sleep(0.01)
    gate.set()
    assert await pick == ""
    await back
    await d.p.close()
    assert _forces(service) == [("Away", True), ("Busy", False)]


async def test_shutdown_lifts_an_auto_away(settings: Settings, service: Service, clock: Clock) -> None:
    """`web` stopping while auto-Away: Reset, so I am not left forced Away by a process that is gone; a set
    cut short (no callback yet) is lifted too."""
    d = await _auto(settings, service, clock)
    await d.sweep(600)
    await d.took()
    await d.p.close()
    assert _forces(service) == [("Away", True), ("Offline", False)]
    d = await _auto(settings, service, clock)
    await d.sweep(600)
    await d.p.close()
    assert _forces(service)[2:] == [("Away", True), ("Offline", False)]


async def test_shutdown_waits_for_a_pick_in_flight(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """A pick in flight at shutdown owns my status: the shutdown does not reset over it."""
    d = await _auto(settings, service, clock)
    await d.sweep(600)
    await d.took()
    gate = asyncio.Event()
    _holding_busy(d, gate)
    pick = asyncio.create_task(d.p.force("Busy"))
    await _until(lambda: d.p._setting.locked())
    closing = asyncio.create_task(d.p.close())
    await asyncio.sleep(0.01)
    gate.set()
    assert await pick == ""
    await closing
    assert _forces(service) == [("Away", True), ("Busy", False)]


async def test_a_failed_auto_set_is_retried_by_a_later_sweep(
    settings: Settings, service: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused Away or Reset (HTTP, token) is tried again on the next sweep, once per sweep, never spun."""
    d = await _auto(settings, service, clock)
    service.force_fail = True
    await d.sweep(600)
    assert not d.p.auto_away and d.rereads() == 0  # not mine: nothing to re-read, nothing to lift
    d.p.renewed, d.p._publish_due = clock.now + 10 * PERIOD, clock.now + 10 * PERIOD
    sweeps: list[int] = []
    sweep = d.p.sweep

    def counted() -> None:
        sweeps.append(1)
        if len(sweeps) > 10:
            raise asyncio.CancelledError  # stop between sweeps: a set cut short would stay ours to lift
        sweep()

    monkeypatch.setattr(d.p, "sweep", counted)
    monkeypatch.setattr(P, "_SWEEP", 0.005)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(d.p.run(), 5)
    assert len(_forces(service)) - 1 == 10 and not d.p.auto_away  # one try per sweep
    service.force_fail = False
    await d.sweep()
    assert d.p.auto_away
    await d.took()
    d.p.token = lambda: None
    await d.sweep(1)
    assert d.p.auto_away  # Reset not sent: still mine to lift
    d.p.token = lambda: "tok"
    await d.sweep()
    assert not d.p.auto_away and _forces(service)[-1] == ("Offline", False)
    await d.p.close()


async def test_no_page_for_two_minutes_is_away_even_without_x11(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """No page of ours (widget, chat page) for 2 min, from the start too, with no idle source (Wayland):
    the endpoint is published and Away is forced; a page back with the desktop not idle lifts it."""
    p, _ = _presence(settings, service, clock)
    p.me = ME
    d = Desk(p, service, clock)
    p.idle, d.seconds = d.idle, None
    service.states = {ME: ("Available", "")}
    p.want([ME])
    await p.on_session(EPID, SURL)
    await _settle(p)
    assert p.published == EPID  # X11 idle is optional
    await d.callback()
    await d.sweep()
    assert _forces(service) == []
    clock.now += 120  # never a page since `web` started
    await d.sweep()
    assert _forces(service) == [("Away", True)] and p.auto_away
    await d.took()
    clock.now += 400
    await d.sweep()
    assert _forces(service) == [("Away", True)] * 2  # still no page: renewed
    await d.took()
    p.pages(1)
    await d.sweep()
    assert _forces(service)[-1] == ("Offline", False) and not p.auto_away
    await p.close()


async def test_a_page_reload_does_not_go_away(settings: Settings, service: Service, clock: Clock) -> None:
    """The last page closing then one opening within 2 min (a reload, a restart): nothing; the delay counts
    from the first close, not the latest."""
    d = await _auto(settings, service, clock)
    d.p.pages(0)
    clock.now += 100
    await d.sweep()
    d.p.pages(1)
    clock.now += 100
    await d.sweep()
    assert _forces(service) == []
    d.p.pages(0)
    clock.now += 100
    d.p.pages(0)  # another page closing: the gap still runs from the first
    clock.now += 19
    await d.sweep()
    assert _forces(service) == []
    clock.now += 1
    await d.sweep()
    assert _forces(service) == [("Away", True)]
    await d.p.close()


async def test_no_renewal_in_a_page_gap(settings: Settings, service: Service, clock: Clock) -> None:
    """Auto-Away, then the widget reloading while I am at the desk (neither away nor back yet): the Away is
    not renewed on a guess; the page back lifts it."""
    d = await _auto(settings, service, clock)
    await d.sweep(600)
    await d.took()
    clock.now += 250
    d.p.pages(0)
    clock.now += 60  # a renewal is due, the gap is under 2 min
    await d.sweep(1)
    assert _forces(service) == [("Away", True)] and d.p.auto_away
    d.p.pages(1)
    await d.sweep()
    assert _forces(service) == [("Away", True), ("Offline", False)]
    await d.p.close()


@pytest.mark.parametrize(
    ("page", "idle", "away"),
    [
        (True, 1.0, False),  # here
        (True, None, False),  # here, no idle source
        (True, 600.0, True),  # the widget open on an idle desktop
        (False, 1.0, True),  # at the desk, no page for 2 min
        (False, None, True),
        (False, 600.0, True),
    ],
)
async def test_either_source_goes_away(
    settings: Settings, service: Service, clock: Clock, page: bool, idle: float | None, away: bool
) -> None:
    d = await _auto(settings, service, clock)
    if not page:
        d.p.pages(0)
        clock.now += 120
    await d.sweep(idle)
    assert d.p.auto_away is away and len(_forces(service)) == away
    await d.p.close()


@pytest.mark.parametrize(
    ("page", "idle", "lifted"),
    [
        (True, 1.0, True),  # back: a page open and the desktop active
        (True, None, True),  # no idle source: a page open is enough
        (True, 600.0, False),  # the page came back, the desktop is still idle
        (False, 1.0, False),  # at the desk, no page yet
    ],
)
async def test_back_means_a_page_and_an_active_desktop(
    settings: Settings, service: Service, clock: Clock, page: bool, idle: float | None, lifted: bool
) -> None:
    d = await _auto(settings, service, clock)
    d.p.pages(0)
    clock.now += 120
    await d.sweep(600)
    await d.took()
    if page:
        d.p.pages(1)
    await d.sweep(idle)
    assert _forces(service)[1:] == ([("Offline", False)] if lifted else []) and d.p.auto_away is not lifted
    await d.p.close()


@pytest.mark.parametrize("leaving", [False, True])
@pytest.mark.parametrize("landed", [True, False])
async def test_an_away_whose_answer_is_lost(
    settings: Settings, service: Service, clock: Clock, landed: bool, leaving: bool
) -> None:
    """The first Away PUT times out: it stays mine. Reached the service: my return lifts it, and so does the
    shutdown. Never reached it: the re-read on my return ends auto-Away with nothing to lift; a shutdown
    before that re-read sends a harmless Reset."""
    d = await _auto(settings, service, clock)

    def lost(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if isinstance(body, dict) and body.get("desiredExpirationTime"):
            if not landed:
                raise httpx.ConnectError("unreachable", request=request)
            service.handler(request)
            entry = _own("Away", "Away", AWAY)
            entry["presence"]["forcedAvailability"]["expiry"] = body["desiredExpirationTime"]
            service.own = entry
            raise httpx.ReadTimeout("no answer", request=request)
        return service.handler(request)

    d.p.http = httpx.AsyncClient(transport=httpx.MockTransport(lost))
    await d.sweep(600)
    assert d.p.auto_away
    if not leaving:
        await d.sweep(1)
        assert not d.p.auto_away
    await d.p.close()
    lifted = landed or leaving
    assert _forces(service) == [("Away", True)] * landed + [("Offline", False)] * lifted


async def test_an_away_cut_short_by_the_shutdown_is_lifted(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """`web` stopping while the first Away PUT is in flight (it may land): the shutdown lifts it."""
    d = await _auto(settings, service, clock)
    gate = asyncio.Event()

    async def held(request: httpx.Request) -> httpx.Response:
        if request.url.path == FORCES and "desiredExpirationTime" in json.loads(request.content):
            service.handler(request)
            await gate.wait()
        return service.handler(request)

    d.p.http = httpx.AsyncClient(transport=httpx.MockTransport(held))
    sweep = asyncio.create_task(d.sweep(600))
    await _until(lambda: len(_forces(service)) == 1)
    sweep.cancel()  # `web` cancels the sweep loop before closing the collector
    with contextlib.suppress(asyncio.CancelledError):
        await sweep
    await d.p.close()
    assert _forces(service) == [("Away", True), ("Offline", False)]


async def test_a_renewal_whose_answer_is_lost_is_still_mine(
    settings: Settings, service: Service, clock: Clock
) -> None:
    """A renewal PUT that reached the service then timed out: its callback carries the new expiry, still
    mine, so my return lifts it."""
    d = await _auto(settings, service, clock)
    await d.sweep(600)
    await d.took()

    def lost(request: httpx.Request) -> httpx.Response:
        answer = service.handler(request)
        if len(service.bodies(FORCES)) == 2 and request.url.path == FORCES:
            raise httpx.ReadTimeout("no answer", request=request)
        return answer

    d.p.http = httpx.AsyncClient(transport=httpx.MockTransport(lost))
    clock.now += 301
    await d.sweep()
    await d.took()  # forced Away with the renewal's expiry
    await d.sweep(1)
    assert _forces(service) == [("Away", True), ("Away", True), ("Offline", False)] and not d.p.auto_away
    await d.p.close()


async def test_a_refused_or_tokenless_away_is_not_mine(
    settings: Settings, service: Service, clock: Clock
) -> None:
    d = await _auto(settings, service, clock)
    d.p.token = lambda: None
    await d.sweep(600)
    assert not d.p.auto_away
    d.p.token = lambda: "tok"
    service.force_fail = True
    await d.sweep()
    assert not d.p.auto_away
    await d.p.close()
    assert _forces(service) == [("Away", True)]  # the refused one: no Reset at shutdown


@pytest.mark.parametrize("failure", ["raises", "none", "unknown"])
async def test_a_failed_reread_keeps_auto_away(
    settings: Settings, service: Service, clock: Clock, failure: str
) -> None:
    """A re-read without an answer (token raising or missing, my presence unknown) decides nothing: auto-Away
    goes on and nothing is reset."""
    d = await _auto(settings, service, clock)
    await d.sweep(600)
    await d.took()
    await d.callback(late=True)
    if failure == "raises":
        d.p.token = _raise_token
    elif failure == "none":
        d.p.token = lambda: None
    else:
        service.own = {"mri": ME, "presence": {"availability": "PresenceUnknown"}}
    await d.sweep()
    await d.sweep(1)
    assert d.p.auto_away and _forces(service) == [("Away", True)]
    d.p.token = lambda: "tok"
    service.own = None
    await d.p.close()


async def test_one_failed_idle_sample_does_not_end_an_idle_away(
    settings: Settings, service: Service, clock: Clock, x11: None
) -> None:
    """Away from desktop idle with the widget open: one failed sample keeps the Away; three in a row are
    unknown idle, and a page open with unknown idle is back."""
    d = await _auto(settings, service, clock)
    probe = _child("print(600)")
    d.p.idle = probe
    await d.sweep()
    await d.took()
    probe.argv[-1] = "raise SystemExit(1)"
    await d.sweep()
    await d.sweep()
    assert _forces(service) == [("Away", True)] and d.p.auto_away
    await d.sweep()
    assert _forces(service) == [("Away", True), ("Offline", False)] and not d.p.auto_away
    await d.p.close()


# --- desktop idle: the child process ---


def _child(code: str, timeout: float = 2.0) -> I.DesktopIdle:
    return I.DesktopIdle([sys.executable, "-I", "-c", code], timeout)


@pytest.fixture
def x11(monkeypatch: pytest.MonkeyPatch) -> None:
    """An X11 session as far as the probe can tell: the fake children never open it."""
    monkeypatch.setenv("DISPLAY", os.environ.get("DISPLAY") or ":99")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("print(12.5)", 12.5),
        ("print(0)", 0.0),
        ("", None),  # no X11 session, library or extension
        ("print('abc')", None),
        ("print('nan')", None),
        ("print(-1)", None),
        ("print(12.5); raise SystemExit(1)", None),
        ("import sys; sys.stdout.buffer.write(bytes([255]))", None),
    ],
)
async def test_the_idle_child_answers_a_number_or_nothing(
    x11: None, code: str, expected: float | None
) -> None:
    probe = _child(code)
    assert await probe() == expected
    assert probe.child is not None and probe.child.returncode is not None


async def test_a_hung_idle_child_is_killed_and_reaped(x11: None) -> None:
    """A hung X server: the child is killed after its timeout and reaped; no second child meanwhile."""
    probe = _child("import time; time.sleep(30)", timeout=0.2)
    assert await probe() is None
    child = probe.child
    assert child is not None
    assert await asyncio.wait_for(child.wait(), 2) == -signal.SIGKILL
    assert await probe() is None and probe.child is not child  # reaped: a new child


async def test_one_idle_child_at_a_time(x11: None) -> None:
    probe = _child("import time; time.sleep(30)", timeout=0.3)
    first = asyncio.create_task(probe())
    await _until(lambda: probe.child is not None)
    child = probe.child
    assert await probe() is None and probe.child is child  # busy: no second child
    assert await first is None
    assert child is not None and await asyncio.wait_for(child.wait(), 2) == -signal.SIGKILL


async def test_idle_unknown_is_logged_once_per_change(x11: None) -> None:
    probe = _child("raise SystemExit(1)")
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    with structlog.testing.capture_logs() as logs:
        await probe()
        await probe()
        probe.argv[-1] = "print('x')"
        await probe()
        probe.argv[-1] = "print(3)"
        await probe()
        await probe()
    assert [(e["event"], e.get("reason")) for e in logs] == [
        ("desktop_idle_unknown", "exit"),
        ("desktop_idle_unknown", "garbage"),
        ("desktop_idle_known", None),
    ]


async def test_the_sweep_goes_on_with_a_hung_idle_child(
    settings: Settings, service: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch, x11: None
) -> None:
    """The child hangs every time: each sample is cut at its timeout, the sweep keeps running."""
    p, _ = _presence(settings, service, clock)
    p.me = ME  # sampled only with an own presence to auto-Away
    probe = _child("import time; time.sleep(30)", timeout=0.05)
    p.idle = probe
    sweeps: list[int] = []
    sweep = p.sweep

    def counted() -> None:
        sweeps.append(1)
        sweep()

    monkeypatch.setattr(p, "sweep", counted)
    monkeypatch.setattr(P, "_SWEEP", 0.005)
    await _loop_for(p, 0.5)
    assert len(sweeps) >= 3 and p.idle_seconds is None
    assert probe.child is not None
    await asyncio.wait_for(probe.child.wait(), 2)
    await p.close()


def test_the_real_idle_child_without_a_display() -> None:
    """`python -m miniteams.idle` with no X11 session: nothing printed, exit 0."""
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    done = subprocess.run(
        [sys.executable, "-I", "-m", "miniteams.idle"], env=env, capture_output=True, timeout=10, check=False
    )
    assert (done.returncode, done.stdout) == (0, b"")


@pytest.mark.parametrize("env", [{}, {"DISPLAY": ":0", "XDG_SESSION_TYPE": "wayland"}])
async def test_no_idle_child_without_an_x11_session(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    for name in ("DISPLAY", "XDG_SESSION_TYPE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    probe = _child("print(5)")
    assert await probe() is None and probe.child is None


async def test_a_transient_idle_failure_keeps_the_last_sample(
    x11: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed sample (timeout, busy, exit, garbage) answers the last good one twice in a row, then None;
    a good one starts over. No X11 session any more, or no child at all: None at once."""
    probe = _child("print(600)")
    assert await probe() == 600.0
    probe.argv[-1] = "raise SystemExit(1)"
    assert [await probe() for _ in range(3)] == [600.0, 600.0, None]
    probe.argv[-1] = "print(4)"
    assert await probe() == 4.0
    probe.argv[-1] = "print('x')"
    assert await probe() == 4.0
    probe.argv[-1] = "print(4)"
    assert await probe() == 4.0
    probe.argv[0] = "/nonexistent/python"
    assert await probe() is None
    probe.argv[0] = sys.executable
    assert await probe() == 4.0
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert await probe() is None


async def test_no_idle_sample_without_my_presence(
    settings: Settings, service: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No own MRI (no auto-Away possible): the sweep spawns no idle child."""
    p, _ = _presence(settings, service, clock)
    sampled: list[int] = []

    async def idle() -> float | None:
        sampled.append(1)
        return 0.0

    p.idle = idle
    monkeypatch.setattr(P, "_SWEEP", 0.005)
    await _loop_for(p, 0.05)
    assert sampled == []
    p.me = ME
    await _loop_for(p, 0.05)
    assert sampled
    await p.close()


@pytest.mark.skipif(
    not (os.environ.get("DISPLAY") and ctypes.util.find_library("X11") and ctypes.util.find_library("Xss")),
    reason="needs an X11 display with libX11 and libXss",
)
async def test_the_real_idle_child_reads_this_desktop() -> None:
    seconds = await I.DesktopIdle()()
    assert isinstance(seconds, float) and seconds >= 0


# --- desktop idle: the X11 query (the child's side) ---


@pytest.mark.parametrize(
    ("env", "libs"),
    [
        ({"XDG_SESSION_TYPE": "x11"}, {"X11": "libX11.so.6", "Xss": "libXss.so.1"}),  # no DISPLAY
        ({"DISPLAY": ":0", "XDG_SESSION_TYPE": "wayland"}, {"X11": "libX11.so.6", "Xss": "libXss.so.1"}),
        ({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}, {"X11": "libX11.so.6"}),  # no libXss
        ({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}, {"Xss": "libXss.so.1"}),  # no libX11
    ],
)
def test_x11_idle_is_none_without_x11_or_its_libraries(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], libs: dict[str, str]
) -> None:
    """No X11 session or a missing library: None, nothing loaded."""
    for name in ("DISPLAY", "XDG_SESSION_TYPE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(I.ctypes.util, "find_library", libs.get)
    monkeypatch.setattr(I.ctypes, "CDLL", lambda n: pytest.fail("nothing to load"))
    assert I.x11_idle() is None


def test_x11_idle_is_none_when_a_library_fails_to_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(I.ctypes.util, "find_library", lambda n: f"lib{n}.so")

    def broken(name: str) -> Any:
        raise OSError("cannot open shared object file")

    monkeypatch.setattr(I.ctypes, "CDLL", broken)
    assert I.x11_idle() is None


class _Fn:
    def __init__(self, result: Any) -> None:
        self.result, self.calls = result, 0

    def __call__(self, *args: Any) -> Any:
        self.calls += 1
        return self.result(*args) if callable(self.result) else self.result


def test_x11_idle_reads_the_screen_saver_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """The idle milliseconds in seconds; no display or no extension: None, the display always closed, and
    the info never queried without the extension (Xlib would exit on that X error)."""

    def fill(dpy: Any, root: Any, info: Any) -> int:
        info._obj.idle = 301_500
        return 1

    x11 = type("X11", (), {})()
    xss = type("Xss", (), {})()
    x11.XOpenDisplay, x11.XDefaultRootWindow, x11.XCloseDisplay = _Fn(1), _Fn(7), _Fn(None)
    xss.XScreenSaverQueryExtension, xss.XScreenSaverQueryInfo = _Fn(1), _Fn(fill)
    monkeypatch.setattr(I, "_libs", lambda: (x11, xss))
    assert I.x11_idle() == 301.5 and x11.XCloseDisplay.calls == 1
    xss.XScreenSaverQueryExtension = _Fn(0)
    xss.XScreenSaverQueryInfo = _Fn(lambda *a: pytest.fail("X error: the process would exit"))
    assert I.x11_idle() is None and x11.XCloseDisplay.calls == 2
    x11.XOpenDisplay = _Fn(None)
    assert I.x11_idle() is None and x11.XCloseDisplay.calls == 2
