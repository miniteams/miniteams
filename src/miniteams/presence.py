"""Teams presence for the `web` process (spec 015): the native presence service and Trouter, never Graph.

The followed people get a snapshot (`getpresence`) and a subscription whose deliveries ride the web's
own Trouter socket (`<surl>/TeamsUnifiedPresenceService`). The subscription is re-posted on every
reconnect and every `PERIOD`; each post answers with a full snapshot callback. A state lives until
two periods pass without a confirmation (a callback or a snapshot; a post's `20200` is not one: a dead
socket gets it too), then it is unknown: no dot, never Offline. Our own presence is followed like anyone's
and is forced only by an explicit menu pick (spec 036, `force`). The socket is published as one of my
endpoints, so closing the Teams client does not leave me Offline.

Latest states persist in a local SQLite cache (`<cache_dir>/presence.sqlite3`, a subset of the spike's
proposed schema): a restart paints them at once, unconfirmed, until the first callback.
"""

import asyncio
import contextlib
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import structlog

from .config import Settings

log = structlog.get_logger()

HOST = "https://presence.teams.microsoft.com"
PATHS = ("/TeamsUnifiedPresenceService", "/unifiedPresenceService")  # Trouter delivery urls
PERIOD = 600.0  # seconds between renewals; spike: deltas kept coming 45 min unrenewed
_SWEEP = 30.0  # seconds between staleness checks
_IDLE_AFTER = 120.0  # seconds without any page before my endpoint reports inactive: a reload must not flap
_REPORT_EVERY = 120.0  # my activity is said again this often, as purple-teams does: a report may expire
_DEBOUNCE = 2.0  # seconds a follow-list change waits for its siblings (a burst of row moves)
_UNKNOWN = "PresenceUnknown"
_ACCEPTED = "20200"  # per-person status of an accepted subscription
_CACHE_MAX = 1000  # rows kept in the cache file, newest observations first
_MRI_RE = re.compile(r"8:orgid:[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")  # only org users have presence
_DM_RE = re.compile(r"19:([0-9a-f-]{36})_([0-9a-f-]{36})@unq\.gbl\.spaces")
_FORCEABLE = ("Available", "Busy", "DoNotDisturb", "BeRightBack", "Away")
# Spec 036: menu pick → `forceavailability` body. Spike: DELETE is refused, Offline alone lifts a force.
FORCE: dict[str, dict[str, str]] = {
    **{a: {"availability": a} for a in _FORCEABLE},
    "AppearOffline": {"availability": "Offline", "activity": "OffWork"},
    "Reset": {"availability": "Offline"},
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS presence (
    tenant_id TEXT NOT NULL,
    mri TEXT NOT NULL,
    availability TEXT NOT NULL,
    activity TEXT NOT NULL,
    observed_at_ms INTEGER NOT NULL,
    confirmed_until_ms INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, mri)
);
CREATE TABLE IF NOT EXISTS horizons (
    tenant_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    mri TEXT NOT NULL,
    msg_id INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, thread_id, mri)
);
"""
_HORIZONS_MAX = 5000  # read receipts kept (spec 011), newest updates first


def person(mri: Any) -> str:
    """The lowercase org MRI, or "" for anything that cannot have Teams presence."""
    low = mri.lower() if isinstance(mri, str) else ""
    return low if _MRI_RE.fullmatch(low) else ""


def dm_peer(thread_id: str, me: str) -> str:
    """The other member of a 1:1 chat (`19:<oid>_<oid>@unq.gbl.spaces`); "" for a self-chat or else."""
    found = _DM_RE.fullmatch(thread_id.lower())
    mine = me.lower().removeprefix("8:orgid:")
    if not found or not mine or mine not in found.groups() or found[1] == found[2]:
        return ""
    return person("8:orgid:" + (found[2] if found[1] == mine else found[1]))


def dm_ids(me: str, mri: Any) -> tuple[str, ...]:
    """Spec 023: the two ids the 1:1 of `me` and `mri` may have (Teams does not always sort them);
    () when either is no org user or both are me."""
    mine, theirs = (person(m).removeprefix("8:orgid:") for m in (me, mri))
    if not mine or not theirs or mine == theirs:
        return ()
    return f"19:{mine}_{theirs}@unq.gbl.spaces", f"19:{theirs}_{mine}@unq.gbl.spaces"


def forced(found: dict[str, Any]) -> str | None:
    """Spec 036: the menu entry a `forcedAvailability` stands for; None when the status is not forced."""
    force = found.get("forcedAvailability")
    avail = force.get("availability") if isinstance(force, dict) else None
    if avail == "Offline":
        return "AppearOffline"  # the only forced Offline: a Reset lifts the force instead of setting one
    return avail if avail in _FORCEABLE else None


def parse(entries: Any) -> dict[str, tuple[str, str, str | None] | None]:
    """`presence: [...]` of a snapshot or a callback → mri → (availability, activity, forced);
    None = unknown."""
    out: dict[str, tuple[str, str, str | None] | None] = {}
    for item in entries if isinstance(entries, list) else []:
        mri = person(item.get("mri")) if isinstance(item, dict) else ""
        if not mri:
            continue
        found = item.get("presence")
        avail = found.get("availability") if isinstance(found, dict) else None
        if not isinstance(avail, str) or not avail or avail == _UNKNOWN:
            out[mri] = None  # missing presence is not Offline
            continue
        activity = found.get("activity") if isinstance(found, dict) else None
        out[mri] = (avail, activity if isinstance(activity, str) and activity else avail, forced(found))
    return out


def statuses(body: Any) -> dict[str, str]:
    """mri → per-person status of a subscription answer (`addStatuses`)."""
    items = body.get("addStatuses") if isinstance(body, dict) else None
    return {
        mri: str(item.get("status"))
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict) and (mri := person(item.get("mri")))
    }


@dataclass(frozen=True)
class State:
    availability: str
    activity: str
    until: float  # wall seconds: unknown past it
    confirmed: bool  # False: from the cache, or no longer followed
    forced: str | None = None  # own state only (spec 036): the forced menu entry; never cached

    def frame(self) -> dict[str, Any]:
        return {"availability": self.availability, "activity": self.activity, "confirmed": self.confirmed}


class Cache:
    """Latest state per (tenant, mri), and read receipts (spec 011), on local disk; the web process is its
    only writer."""

    def __init__(self, path: Path, tenant: str) -> None:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.tenant = tenant
        self.db = sqlite3.connect(path, timeout=3)
        try:
            os.chmod(path, 0o600)  # who we follow, and when they were busy: owner only
            self.db.execute("PRAGMA journal_mode=WAL")
            # WAL + NORMAL: no fsync per commit, the writes stay off the Trouter loop's critical path.
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(_SCHEMA)
            # A file corrupt past its header opens fine and fails on the first read.
            if self.db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise sqlite3.DatabaseError("presence cache corrupt")
        except Exception:
            self.db.close()
            raise

    def load(self, now: float) -> dict[str, State]:
        """Rows still inside their confirmation deadline, unconfirmed until the service says so again."""
        rows = self.db.execute(
            "SELECT mri, availability, activity, confirmed_until_ms FROM presence"
            " WHERE tenant_id = ? AND confirmed_until_ms > ?",
            (self.tenant, int(now * 1000)),
        )
        return {mri: State(a, x, until / 1000, False) for mri, a, x, until in rows}

    def save(self, states: dict[str, State], now: float) -> None:
        with self.db:
            self.db.executemany(
                """INSERT INTO presence VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, mri) DO UPDATE SET
                observed_at_ms = excluded.observed_at_ms,
                availability = excluded.availability, activity = excluded.activity,
                confirmed_until_ms = excluded.confirmed_until_ms""",
                [
                    (self.tenant, mri, s.availability, s.activity, int(now * 1000), int(s.until * 1000))
                    for mri, s in states.items()
                ],
            )
            self.db.execute(
                "DELETE FROM presence WHERE tenant_id = ? AND mri NOT IN (SELECT mri FROM presence"
                " WHERE tenant_id = ? ORDER BY observed_at_ms DESC LIMIT ?)",
                (self.tenant, self.tenant, _CACHE_MAX),
            )

    def forget(self, mris: Iterable[str]) -> None:
        with self.db:
            self.db.executemany(
                "DELETE FROM presence WHERE tenant_id = ? AND mri = ?", [(self.tenant, m) for m in mris]
            )

    def horizons(self, thread_id: str) -> dict[str, int]:
        """Read receipts of a chat (spec 011): member → id of the newest message they read."""
        rows = self.db.execute(
            "SELECT mri, msg_id FROM horizons WHERE tenant_id = ? AND thread_id = ?", (self.tenant, thread_id)
        )
        return dict(rows)

    def save_horizons(self, thread_id: str, found: dict[str, int], now: float) -> None:
        with self.db:
            # Only a newer horizon writes: a late, replayed or unchanged one keeps the row and its age.
            written = self.db.executemany(
                """INSERT INTO horizons VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, thread_id, mri) DO UPDATE SET
                msg_id = excluded.msg_id, updated_at_ms = excluded.updated_at_ms
                WHERE excluded.msg_id > msg_id""",
                [(self.tenant, thread_id, mri, at, int(now * 1000)) for mri, at in found.items()],
            ).rowcount
            if not written:
                return
            self.db.execute(
                "DELETE FROM horizons WHERE tenant_id = ? AND rowid NOT IN (SELECT rowid FROM horizons"
                " WHERE tenant_id = ? ORDER BY updated_at_ms DESC LIMIT ?)",
                (self.tenant, self.tenant, _HORIZONS_MAX),
            )

    def close(self) -> None:
        self.db.close()


def open_cache(settings: Settings) -> Cache | None:
    """The cache file, or None when it cannot be opened: presence then simply starts empty."""
    path = settings.cache_dir / "presence.sqlite3"
    try:
        try:
            return Cache(path, settings.tenant_id)
        except sqlite3.OperationalError:
            raise  # locked or unreadable: not ours to delete
        except sqlite3.DatabaseError as exc:
            # Regenerable: a corrupt file is replaced, else every start would run without a cache.
            log.warning("presence_cache_reset", error=str(exc))
            for name in (path.name, path.name + "-wal", path.name + "-shm"):
                path.with_name(name).unlink(missing_ok=True)
            return Cache(path, settings.tenant_id)
    except (OSError, sqlite3.Error) as exc:
        log.warning("presence_cache_unavailable", error=str(exc), error_type=type(exc).__name__)
        return None


TokenFn = Callable[[], str | None]


class Presence:
    """Follow list → native snapshot + subscription; Trouter callbacks → states → `on_change`."""

    def __init__(
        self,
        settings: Settings,
        token: TokenFn,
        on_change: Callable[[], None],
        cache: Cache | None = None,
        client: httpx.AsyncClient | None = None,
        period: float = PERIOD,
        clock: Callable[[], float] = time.time,
        me: str = "",
    ) -> None:
        self.settings = settings
        self.me = person(me)  # own MRI: its frame entry says which status is forced
        self.token = token  # silent presence-resource token; None = unavailable
        self.on_change = on_change
        self.cache = cache
        self.period = period
        self.clock = clock
        self.http = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        self.states: dict[str, State] = cache.load(clock()) if cache else {}
        self.wanted: frozenset[str] = frozenset()
        self.subscribed: frozenset[str] = frozenset()  # accepted on the current socket
        self.session: tuple[str, str] | None = None  # (endpoint id, surl) of the current socket
        self.renewed = 0.0  # wall seconds of the last full post
        self.published: str | None = None  # the endpoint id last published as mine
        self.active = False  # a page of ours is open; none for a while and Teams shows me Away
        self._idle: asyncio.TimerHandle | None = None
        self._reporting = asyncio.Lock()  # one activity report at a time, each sends the latest state
        self.reported = 0.0  # wall seconds of the last activity report sent
        self._retry_at, self._retry_wait = 0.0, _SWEEP  # a refused publish is retried, backing off
        self._lock = asyncio.Lock()  # one post at a time: a renewal must not interleave a diff
        self._pending: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # --- what the pages see ---

    def frame(self) -> dict[str, Any]:
        """Known states of the followed people; anyone else is unknown (no dot). `self` names my entry."""
        shown = {m: self.states[m].frame() for m in sorted(self.wanted) if m in self.states}
        if self.me in shown:
            shown[self.me]["forced"] = self.states[self.me].forced
        return {"presence": shown, "self": self.me} if self.me else {"presence": shown}

    # --- follow list ---

    def want(self, mris: Iterable[str]) -> None:
        """The people the pages show; a change is posted after a short debounce."""
        wanted = frozenset(p for m in mris if (p := person(m)))
        if wanted == self.wanted:
            return
        for mri in self.wanted - wanted:
            # No longer followed: no update will come, so the state is no longer certain.
            if (state := self.states.get(mri)) is not None and state.confirmed:
                self.states[mri] = replace(state, confirmed=False)
        self.wanted = wanted
        self._schedule(_DEBOUNCE)

    def _schedule(self, delay: float) -> None:
        if self._pending is not None:
            self._pending.cancel()
        self._pending = asyncio.get_running_loop().call_later(delay, self._spawn)

    def _spawn(self) -> None:
        self._pending = None
        self._start(self.sync())

    def _start(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- my activity: the pages connected to `web` ---

    def pages(self, count: int) -> None:
        """Any page open: active at once; none left for `_IDLE_AFTER`: inactive."""
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        if count:
            self._activity(True)
        else:
            self._idle = asyncio.get_running_loop().call_later(_IDLE_AFTER, self._activity, False)

    def _activity(self, active: bool) -> None:
        self._idle = None
        if active != self.active:
            self.active = active
            self._start(self.report())

    async def report(self) -> None:
        """Tell the service whether my endpoint is active; nothing before it is published."""
        if self.session is None or self.published != self.session[0]:
            return  # the publish reports the state once it goes through
        epid = self.session[0]
        self.reported = self.clock()  # an attempt, failed or not: the periodic one must not spin
        try:
            token = await asyncio.to_thread(self.token)
        except Exception as exc:  # noqa: BLE001 — MSAL re-raises network errors
            log.warning("presence_activity_failed", error_type=type(exc).__name__)
            return
        if not token:
            log.warning("presence_activity_failed", error_type="token")
            return
        await self._report(self._headers(token, epid), epid)

    async def _report(self, headers: dict[str, str], epid: str) -> None:
        async with self._reporting:  # in order: each send carries the state current when it goes out
            self.reported = self.clock()
            body = {"endpointId": epid, "isActive": self.active}
            try:
                resp = await self.http.post(f"{HOST}/v1/me/reportmyactivity/", headers=headers, json=body)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("presence_activity_failed", error=str(exc), error_type=type(exc).__name__)
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                    self.published = None  # the service dropped my endpoint: the sweep publishes it again
                return
            log.debug("presence_activity", active=body["isActive"])

    # --- Trouter hooks ---

    async def on_session(self, epid: str, surl: str) -> None:
        """A (re)registered socket: a new callback url needs the whole set subscribed again."""
        self.session = (epid, surl)
        self.subscribed = frozenset()
        self.published = None  # the id is persisted: a new socket under the same id is published again
        self._retry_at, self._retry_wait = 0.0, _SWEEP
        self._schedule(0)  # not awaited: the socket's frames must not wait on HTTP

    async def on_presence(self, body: dict[str, Any]) -> None:
        """A callback: the initial snapshot of a post, or one person's change."""
        self._apply(parse(body.get("presence")))

    # --- the native service ---

    def _headers(self, token: str, epid: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "x-ms-client-user-agent": "Teams-V2-Desktop",
            "x-ms-client-version": self.settings.clientinfo_version,
            "x-ms-endpoint-id": epid,
            "x-ms-correlation-id": str(uuid.uuid4()),
        }

    async def sync(self, renew: bool = False) -> None:
        """Snapshot the newcomers, then post the whole followed set (purging the previous one);
        an unchanged set is posted again only to `renew` it."""
        async with self._lock:
            if self.session is None:
                return
            epid, surl = self.session
            wanted, gone = self.wanted, self.subscribed - self.wanted
            publish = renew or self.published != epid
            idle = (not wanted and not gone) or (wanted == self.subscribed and not renew)
            if idle and not publish:
                return
            try:
                token = await asyncio.to_thread(self.token)
            except Exception as exc:  # noqa: BLE001 — MSAL re-raises network errors; a later post retries
                log.warning("presence_token_unavailable", error=str(exc), error_type=type(exc).__name__)
                return
            if not token:
                log.warning("presence_token_unavailable")
                return
            headers = self._headers(token, epid)
            if publish:
                await self._publish(headers, epid)  # its failure must not cost the dots their subscription
                if self.session != (epid, surl):
                    self.published = None  # a new socket under the same id came in meanwhile
                    return  # its own sync is queued
            if idle:
                return
            try:
                if fresh := sorted(wanted - self.subscribed):
                    resp = await self.http.post(
                        f"{HOST}/v1/presence/getpresence/", headers=headers, json=[{"mri": m} for m in fresh]
                    )
                    resp.raise_for_status()
                    self._apply(parse(resp.json()))  # a snapshot counts as a confirmation
                body = {
                    "trouterUri": surl.rstrip("/") + PATHS[0],
                    "subscriptionsToAdd": [{"mri": m} for m in sorted(wanted)],
                    "subscriptionsToRemove": [{"mri": m} for m in sorted(gone)],
                    # Our own endpoint only: the set we post replaces whatever it held.
                    "shouldPurgePreviousSubscriptions": True,
                }
                url = f"{HOST}/v1/pubsub/subscriptions/{epid}"
                resp = await self.http.post(url, headers=headers, json=body)
                resp.raise_for_status()
                answered = statuses(resp.json() if resp.content else {})
            except (httpx.HTTPError, ValueError) as exc:
                # Nothing confirmed: two periods of this and the dots go, as they should.
                log.warning("presence_post_failed", error=str(exc), error_type=type(exc).__name__)
                return
            if self.session != (epid, surl):
                return  # the socket changed under the post: its own sync is queued
            self.subscribed = wanted
            self.renewed = self.clock()
            # 40020 (or any refusal): the service does not know this person's presence. A 20200 confirms
            # nothing: it comes back on a dead socket too, the snapshot callback is the confirmation.
            refused = [m for m, s in answered.items() if s != _ACCEPTED]
            self._apply(dict.fromkeys(refused))
            log.info("presence_subscribed", people=len(wanted), removed=len(gone), refused=len(refused))

    async def _publish(self, headers: dict[str, str], epid: str) -> None:
        """Our socket as one of my Teams endpoints: with none left (Teams client closed) I am Offline and
        a forced status shows nowhere. Transport: its activity comes from `reportmyactivity` (`report`)."""
        body = {
            "id": epid,
            "availability": "Available",
            "activity": "Available",
            "activityReporting": "Transport",
            "deviceType": "Desktop",
        }  # purple-teams' shape
        try:
            resp = await self.http.put(f"{HOST}/v1/me/endpoints/", headers=headers, json=body)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("presence_endpoint_failed", error=str(exc), error_type=type(exc).__name__)
            return
        self.published = epid
        self._retry_at, self._retry_wait = 0.0, _SWEEP
        log.info("presence_endpoint_published")
        await self._report(headers, epid)  # a published endpoint starts from what the pages say

    async def force(self, value: str) -> str:
        """Spec 036: force my status to a `FORCE` entry; "" once the service took it, else a short reason.
        Never retried: the page shows the reason, the next pick is the retry."""
        try:
            token = await asyncio.to_thread(self.token)
        except Exception as exc:  # noqa: BLE001 — MSAL re-raises network errors
            log.warning("presence_set_failed", value=value, reason="token", error_type=type(exc).__name__)
            return "token unavailable"
        if not token:
            log.warning("presence_set_failed", value=value, reason="token")
            return "token unavailable"
        # Our socket's endpoint as the collector's posts; without one a throwaway id, as the spike did.
        epid = self.session[0] if self.session else str(uuid.uuid4())
        try:
            resp = await self.http.put(
                f"{HOST}/v1/me/forceavailability/", headers=self._headers(token, epid), json=FORCE[value]
            )
        except httpx.HTTPError as exc:
            log.warning("presence_set_failed", value=value, reason="network", error_type=type(exc).__name__)
            return "network error"
        if resp.is_error:
            log.warning("presence_set_failed", value=value, reason="http", status=resp.status_code)
            return f"HTTP {resp.status_code}"
        log.info("presence_set", value=value)
        return ""

    async def run(self) -> None:
        """Renew every period, sweep stale states in between; until cancelled."""
        while True:
            await asyncio.sleep(_SWEEP)
            try:
                self.sweep()
                if self.session is not None and self.clock() - self.renewed >= self.period:
                    self.renewed = self.clock()  # one attempt per period, failed or not
                    await self.sync(renew=True)
                elif self.session is not None and self.published != self.session[0]:
                    if self.clock() >= self._retry_at:  # offline until a publish goes through
                        self._retry_at = self.clock() + self._retry_wait
                        self._retry_wait = min(self._retry_wait * 2, self.period)
                        await self.sync()
                elif self.session is not None and self.clock() - self.reported >= _REPORT_EVERY:
                    await self.report()
            except Exception as exc:  # noqa: BLE001 — a dead loop would leave every dot up forever
                log.warning("presence_renewal_failed", error=str(exc), error_type=type(exc).__name__)

    def sweep(self) -> None:
        now = self.clock()
        stale = [m for m, s in self.states.items() if s.until <= now]
        for mri in stale:
            del self.states[mri]
        if stale:
            self._forget(stale)
            self.on_change()

    async def close(self) -> None:
        """Drop our subscriptions (never the contact list) and release the client."""
        if self._pending is not None:
            self._pending.cancel()
        if self._idle is not None:
            self._idle.cancel()
        for task in self._tasks:
            task.cancel()
        try:
            if self.session is not None and self.subscribed:
                epid, surl = self.session
                token = await asyncio.to_thread(self.token)
                if token:
                    body = {
                        "trouterUri": surl.rstrip("/") + PATHS[0],
                        "subscriptionsToAdd": [],
                        "subscriptionsToRemove": [{"mri": m} for m in sorted(self.subscribed)],
                        "shouldPurgePreviousSubscriptions": True,
                    }
                    url = f"{HOST}/v1/pubsub/subscriptions/{epid}"
                    with contextlib.suppress(httpx.HTTPError):
                        await self.http.post(url, headers=self._headers(token, epid), json=body)
                    self.subscribed = frozenset()
        finally:
            await self.http.aclose()
            if self.cache is not None:
                self.cache.close()

    # --- states ---

    def _apply(self, found: dict[str, tuple[str, str, str | None] | None]) -> None:
        now = self.clock()
        until = now + 2 * self.period
        changed: dict[str, State] = {}
        unknown: list[str] = []
        for mri, value in found.items():
            if mri not in self.wanted:
                continue  # a late answer for someone no longer followed
            if value is None:
                if self.states.pop(mri, None) is not None:
                    unknown.append(mri)
                continue
            changed[mri] = State(value[0], value[1], until, True, value[2] if mri == self.me else None)
        old = {m: self.states.get(m) for m in changed}
        self.states.update(changed)
        if changed and self.cache is not None:
            try:
                self.cache.save(changed, now)
            except sqlite3.Error as exc:
                log.warning("presence_cache_write_failed", error=str(exc))
        self._forget(unknown)
        # A renewed deadline alone is no news for the pages.
        news = any(o is None or replace(o, until=0) != replace(changed[m], until=0) for m, o in old.items())
        if unknown or news:
            self.on_change()

    def _forget(self, mris: list[str]) -> None:
        if mris and self.cache is not None:
            try:
                self.cache.forget(mris)
            except sqlite3.Error as exc:
                log.warning("presence_cache_write_failed", error=str(exc))
