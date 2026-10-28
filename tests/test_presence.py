"""Presence collector (spec 015 phase 2): snapshot, subscription, callbacks, staleness, follow list, cache.

Synthetic MRIs, a mocked presence service (httpx.MockTransport), no network."""

import asyncio
import json
import os
import stat
from typing import Any

import httpx
import pytest
import structlog.testing

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

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(503)
        body = json.loads(request.content)
        if request.url.path == "/v1/presence/getpresence/":
            out = []
            for item in body:
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
def service() -> Service:
    return Service()


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
    p = P.Presence(settings, token, on_change, cache, client=client, period=PERIOD, clock=clock)
    return p, changes


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
    assert found == {ANN: ("Busy", "InAMeeting", None), BOB: None, CID: None}
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
    assert [r.url.path for r in service.requests] == [f"/v1/pubsub/subscriptions/{EPID}"]
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
    assert [r.url.path for r in service.requests] == [f"/v1/pubsub/subscriptions/{EPID}"]
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
    await _until(lambda: len(calls) == 3)
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


async def test_no_request_publishes_own_presence_or_touches_graph(
    settings: Settings, service: Service, clock: Clock, directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """015-AC8: the collector by itself only reads presence and posts our own subscriptions (a status menu
    pick, spec 036, is the only set), never Graph; web's
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
    ((_, (_, _, forced)),) = P.parse([entry]).items()  # type: ignore[misc]
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
