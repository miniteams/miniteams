"""Teams presence for the `web` process (spec 015): the native presence service and Trouter, never Graph.

The followed people get a snapshot (`getpresence`) and a subscription whose deliveries ride the web's
own Trouter socket (`<surl>/TeamsUnifiedPresenceService`). The subscription is re-posted on every
reconnect and every `PERIOD`; each post answers with a full snapshot callback. A state lives until
two periods pass without a confirmation (a callback or a snapshot; a post's `20200` is not one: a dead
socket gets it too), then it is unknown: no dot, never Offline. Our own presence is never published.

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
from collections.abc import Callable, Iterable
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
_DEBOUNCE = 2.0  # seconds a follow-list change waits for its siblings (a burst of row moves)
_UNKNOWN = "PresenceUnknown"
_ACCEPTED = "20200"  # per-person status of an accepted subscription
_CACHE_MAX = 1000  # rows kept in the cache file, newest observations first
_MRI_RE = re.compile(r"8:orgid:[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")  # only org users have presence
_DM_RE = re.compile(r"19:([0-9a-f-]{36})_([0-9a-f-]{36})@unq\.gbl\.spaces")

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


def parse(entries: Any) -> dict[str, tuple[str, str] | None]:
    """`presence: [...]` of a snapshot or a callback → mri → (availability, activity); None = unknown."""
    out: dict[str, tuple[str, str] | None] = {}
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
        out[mri] = (avail, activity if isinstance(activity, str) and activity else avail)
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
    ) -> None:
        self.settings = settings
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
        self._lock = asyncio.Lock()  # one post at a time: a renewal must not interleave a diff
        self._pending: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # --- what the pages see ---

    def frame(self) -> dict[str, Any]:
        """Known states of the followed people; anyone else is unknown (no dot)."""
        return {"presence": {m: self.states[m].frame() for m in sorted(self.wanted) if m in self.states}}

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
            if (not wanted and not gone) or (wanted == self.subscribed and not renew):
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

    async def run(self) -> None:
        """Renew every period, sweep stale states in between; until cancelled."""
        while True:
            await asyncio.sleep(_SWEEP)
            try:
                self.sweep()
                if self.session is not None and self.clock() - self.renewed >= self.period:
                    self.renewed = self.clock()  # one attempt per period, failed or not
                    await self.sync(renew=True)
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

    def _apply(self, found: dict[str, tuple[str, str] | None]) -> None:
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
            changed[mri] = State(value[0], value[1], until, True)
        old = {m: self.states.get(m) for m in changed}
        self.states.update(changed)
        if changed and self.cache is not None:
            try:
                self.cache.save(changed, now)
            except sqlite3.Error as exc:
                log.warning("presence_cache_write_failed", error=str(exc))
        self._forget(unknown)
        # A renewed deadline alone is no news for the pages.
        if unknown or any(o is None or o.frame() != changed[m].frame() for m, o in old.items()):
            self.on_change()

    def _forget(self, mris: list[str]) -> None:
        if mris and self.cache is not None:
            try:
                self.cache.forget(mris)
            except sqlite3.Error as exc:
                log.warning("presence_cache_write_failed", error=str(exc))
