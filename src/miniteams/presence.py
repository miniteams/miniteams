"""Teams presence for the `web` process (spec 015): the native presence service and Trouter, never Graph.

The followed people get a snapshot (`getpresence`) and a subscription whose deliveries ride the web's
own Trouter socket (`<surl>/TeamsUnifiedPresenceService`). The subscription is re-posted on every
reconnect and every `PERIOD`; each post answers with a full snapshot callback. A state lives until
two periods pass without a confirmation (a callback or a snapshot; a post's `20200` is not one: a dead
socket gets it too), then it is unknown: no dot, never Offline. Our own presence is followed like anyone's
and is forced by an explicit menu pick (spec 036, `force`), or set Away after 5 min of desktop idle or 2 min
without a page of ours until I come back. The socket is published as one of my endpoints, so closing the Teams
client does not leave me Offline.

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
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog

from .config import Settings
from .idle import DesktopIdle

log = structlog.get_logger()

HOST = "https://presence.teams.microsoft.com"
PATHS = ("/TeamsUnifiedPresenceService", "/unifiedPresenceService")  # Trouter delivery urls
PERIOD = 600.0  # seconds between renewals; spike: deltas kept coming 45 min unrenewed
_SWEEP = 30.0  # seconds between staleness checks
_PUBLISH_EVERY = 240.0  # seconds between re-puts of my endpoint: unrenewed, inactive at ~6 min, gone at ~11
_AWAY_AFTER = 300.0  # seconds of desktop idle before auto-Away, as the Teams desktop client
_NO_PAGE_AFTER = (
    120.0  # seconds without any page (widget, chat page) before auto-Away: a reload must not flap
)
_AWAY_HOLD = 900.0  # seconds an auto-Away is forced for: a crash leaves it this long, not Teams' 7 days
_AWAY_RENEW = 600.0  # an auto-Away with less than this left is forced again while still idle
_NETWORK = "network error"  # a set that may have reached the service
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


Found = tuple[str, str, str | None, str | None]  # availability, activity, forced entry, its expiry


def parse(entries: Any) -> dict[str, Found | None]:
    """`presence: [...]` of a snapshot or a callback → mri → (availability, activity, forced, expiry);
    None = unknown."""
    out: dict[str, Found | None] = {}
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
        force = found.get("forcedAvailability")
        expiry = force.get("expiry") if isinstance(force, dict) else None
        out[mri] = (
            avail,
            activity if isinstance(activity, str) and activity else avail,
            forced(found),
            expiry if isinstance(expiry, str) else None,
        )
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
    expiry: str | None = None  # own state only: when the forced status lifts (ISO UTC); never cached

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


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        idle: Callable[[], Awaitable[float | None]] | None = None,
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
        self._publish_due, self._publish_wait = 0.0, _SWEEP  # next publish; a failed one backs off
        self.idle = idle or DesktopIdle()  # desktop idle seconds; None = unknown
        self.idle_seconds: float | None = None  # the latest sample; None = unknown (no X11)
        self._pageless_since: float | None = clock()  # since when no page is open; None: one is
        self._setting = asyncio.Lock()  # one forceavailability at a time: a pick and the idle check
        self._set_at = 0.0  # wall seconds of the last accepted forceavailability
        self._own_at = 0.0  # wall seconds of my last confirmed state
        self._away: list[float] = []  # auto-Away expiries sent, newest first, at most 2; [] = none
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
        task = asyncio.create_task(self.sync())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- Trouter hooks ---

    async def on_session(self, epid: str, surl: str) -> None:
        """A (re)registered socket: a new callback url needs the whole set subscribed again."""
        self.session = (epid, surl)
        self.subscribed = frozenset()
        self.published = None  # the id is persisted: a new socket under the same id is published again
        self._publish_due, self._publish_wait = 0.0, _SWEEP
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

    async def sync(self, renew: bool = False, publish: bool = False) -> None:
        """Snapshot the newcomers, then post the whole followed set (purging the previous one);
        an unchanged set is posted again only to `renew` it. My endpoint goes first when unpublished,
        on `renew` and on `publish`."""
        async with self._lock:
            if self.session is None:
                return
            epid, surl = self.session
            wanted, gone = self.wanted, self.subscribed - self.wanted
            publish = publish or renew or self.published != epid
            nothing_to_post = (not wanted and not gone) or (wanted == self.subscribed and not renew)
            if nothing_to_post and not publish:
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
            if nothing_to_post:
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
        a forced status shows nowhere. Refused: unpublished, the sweep retries it."""
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
            self.published = None
            return
        self.published = epid
        self._publish_due, self._publish_wait = self.clock() + _PUBLISH_EVERY, _SWEEP
        log.info("presence_endpoint_published")

    async def force(self, value: str) -> str:
        """Spec 036: force my status to a `FORCE` entry; "" once the service took it, else a short reason.
        Never retried: the page shows the reason, the next pick is the retry. A pick taken ends an
        auto-Away: the pick owns my status from then on."""
        async with self._setting:
            error = await self._set(value)
            if not error:
                self._away = []
            return error

    async def _set(self, value: str, until: float | None = None, renew: bool = False) -> str:
        """`FORCE[value]`, expiring at `until` (else Teams' default); a `renew` keeps my confirmed state."""
        body = dict(FORCE[value])
        if until is not None:
            body["desiredExpirationTime"] = _iso(until)
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
        sent = self.clock()  # my callback may come before the answer: a state after this one is fresh
        try:
            resp = await self.http.put(
                f"{HOST}/v1/me/forceavailability/", headers=self._headers(token, epid), json=body
            )
        except httpx.HTTPError as exc:
            log.warning("presence_set_failed", value=value, reason="network", error_type=type(exc).__name__)
            return _NETWORK
        if resp.is_error:
            log.warning("presence_set_failed", value=value, reason="http", status=resp.status_code)
            return f"HTTP {resp.status_code}"
        if not renew:
            self._set_at = sent
        log.info("presence_set", value=value)
        return ""

    @property
    def auto_away(self) -> bool:
        return bool(self._away)

    def _ours(self, own: State) -> bool:
        """My Away is the one auto-Away forced: an Away picked in Teams meanwhile has another expiry."""
        return own.forced == "Away" and (own.expiry or "")[:19] in {_iso(at)[:19] for at in self._away}

    async def _reread(self) -> State | None:
        """My presence asked again (`getpresence`): a late callback must not end an auto-Away. None when
        unknown or the call failed."""
        try:
            token = await asyncio.to_thread(self.token)
        except Exception as exc:  # noqa: BLE001 — MSAL re-raises network errors
            log.warning("presence_reread_failed", reason="token", error_type=type(exc).__name__)
            return None
        if not token:
            log.warning("presence_reread_failed", reason="token")
            return None
        epid = self.session[0] if self.session else str(uuid.uuid4())
        try:
            resp = await self.http.post(
                f"{HOST}/v1/presence/getpresence/",
                headers=self._headers(token, epid),
                json=[{"mri": self.me}],
            )
            resp.raise_for_status()
            found = parse(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("presence_reread_failed", reason="http", error_type=type(exc).__name__)
            return None
        if found.get(self.me) is None or self.me not in self.wanted:
            return None  # an answer `_apply` would drop would leave the old state to decide on
        self._apply(found)
        return self.states.get(self.me)

    def pages(self, count: int) -> None:
        """The pages connected to `web` (widget, chat pages): none for `_NO_PAGE_AFTER` counts as away."""
        if count:
            self._pageless_since = None
        elif self._pageless_since is None:
            self._pageless_since = self.clock()

    async def away(self) -> None:
        """Auto-Away: desktop idle `_AWAY_AFTER` or no page for `_NO_PAGE_AFTER`, over my published
        endpoint, no status forced and not Busy or Do not disturb, forces Away for `_AWAY_HOLD`, forced again
        while I stay away; back (a page open, the desktop not idle), that Away is lifted, any other status
        left alone. Decides only on a state of mine confirmed since the last set; unknown state: nothing."""
        own = self.states.get(self.me)
        if own is None or not own.confirmed or self._own_at <= self._set_at:
            return
        idle_away = self.idle_seconds is not None and self.idle_seconds >= _AWAY_AFTER  # unknown: not idle
        pageless = self._pageless_since is not None and self.clock() - self._pageless_since >= _NO_PAGE_AFTER
        absent, present = idle_away or pageless, self._pageless_since is None and not idle_away
        if not self._away:
            published = self.session is not None and self.published == self.session[0]
            busy = own.availability in ("Busy", "DoNotDisturb")  # a meeting, a call, presenting: never hidden
            if not absent or own.forced is not None or busy or not published:
                return
            async with self._setting:
                if self._own_at <= self._set_at:
                    return  # a pick went out while we waited: it owns my status
                # Before the PUT: a set cut short (shutdown, network) may have landed, it stays ours to lift.
                self._away = [self.clock() + _AWAY_HOLD]
                error = await self._set("Away", self._away[0])
                if error and error != _NETWORK:
                    self._away = []  # refused: retried by a later sweep
                elif not error:
                    log.info("presence_auto_away", source="idle" if idle_away else "no page")
            return
        if not self._ours(own):
            mine = await self._reread()
            if mine is None:
                return  # unknown: asked again on a later sweep
            if not self._ours(mine):
                if self._away:  # not cleared by a pick meanwhile
                    self._away = []  # changed in Teams meanwhile, lapsed or never landed: that status stays
                    log.info("presence_auto_back", reset=False)
                return
        if present:
            async with self._setting:
                if self._away and not await self._set("Reset"):  # a pick meanwhile cleared it
                    self._away = []
                    log.info("presence_auto_back", reset=True)
        elif absent and self._away[0] - self.clock() < _AWAY_RENEW:
            async with self._setting:
                if not self._away:
                    return  # a pick meanwhile ended it
                # The previous expiry kept: my state may still carry it until the callback.
                standing, self._away = self._away, [self.clock() + _AWAY_HOLD, self._away[0]]
                if (error := await self._set("Away", self._away[0], renew=True)) and error != _NETWORK:
                    self._away = standing  # refused: renewed by a later sweep

    async def run(self) -> None:
        """Renew every period, sweep stale states and sample the desktop idle in between; until cancelled."""
        while True:
            await asyncio.sleep(_SWEEP)
            try:
                self.sweep()
                if self.me:  # no own presence, no auto-Away: nothing to sample for
                    self.idle_seconds = await self.idle()
                if self.session is not None and self.clock() - self.renewed >= self.period:
                    self.renewed = self.clock()  # one attempt per period, failed or not
                    await self.sync(renew=True)
                elif self.session is not None and self.clock() >= self._publish_due:
                    # Until a publish goes through (any failure, token included): backing off.
                    self._publish_due = self.clock() + self._publish_wait
                    self._publish_wait = min(self._publish_wait * 2, self.period)
                    await self.sync(publish=True)
                await self.away()
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
        """Lift an auto-Away, drop our subscriptions (never the contact list) and release the client."""
        if self._pending is not None:
            self._pending.cancel()
        for task in self._tasks:
            task.cancel()
        try:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._lift(), 3)
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

    async def _lift(self) -> None:
        """Not left forced Away by a process that is gone; a status set in Teams meanwhile stays."""
        async with self._setting:
            own = self.states.get(self.me)
            if self._away and (own is None or own.forced is None or self._ours(own)):
                await self._set("Reset")

    # --- states ---

    def _apply(self, found: dict[str, Found | None]) -> None:
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
            mine = mri == self.me
            changed[mri] = State(value[0], value[1], until, True, *(value[2:] if mine else (None, None)))
            if mine:
                self._own_at = now
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
