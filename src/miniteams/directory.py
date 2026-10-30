"""Thread + identity resolution, cached.

A single `GET /v1/threads/<id>?view=msnp24Equivalent` (X-Skypetoken) returns the member roster
*with* display names already bundled (`members[].friendlyName`) — so no separate profile lookup
is needed. The topic isn't formally part of that response, but `properties.topic` is usually
present; we read it opportunistically and fall back to the roster otherwise.

Caches live for the process lifetime and survive reconnects; the token pair is renewed by the
stream while a session lasts (`set_token`). Lookups never raise into the stream — a failed fetch
degrades to the bare id and is not retried for `_THREAD_RETRY_AFTER`.
"""

import asyncio
import json
import math
import sqlite3
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote

import httpx
import structlog

from .attachments import DEFAULT_MEDIA
from .config import Settings
from .http import aget_with_retry, failure
from .names_store import ARCHIVED, DECLARED, LIVE, Entry, NameStore, Row, now_ms

log = structlog.get_logger()

# MRI prefix → friendly label for special threads that have no real roster/topic.
_SPECIAL_THREADS = {
    "48:notes": "Notes to self",
    "48:drafts": "Drafts",
    "48:annotations": "Annotations",
    "48:calllogs": "Call logs",
    "48:mentions": "Mentions",
    "48:notifications": "Notifications",
    "48:saved": "Saved",
    "48:starred": "Starred",
    "48:threads": "Followed threads",
}

# Reaction state is per-message and `run_forever` runs indefinitely, so an unbounded dict leaks.
# Diffs only need the *recent* messages' prior state; cap to an LRU window. A reaction landing on
# an evicted message simply re-announces its current reactors as "added" — acceptable & rare.
_REACTION_LRU_MAX = 4096

# Thread info is per chat and the stream runs for days: an LRU window, as for reactions. An evicted
# thread costs one refetch on its next lookup.
_THREADS_MAX = 4096

# meta keys of the one-off imports into names.db (038-AC4).
_CACHE_IMPORT = "import:names-v2+names-json"
_ROSTER_IMPORT = "import:index-rosters"

# A names.db that could not be opened, or a write that failed, is tried again at most this often (seconds).
_STORE_RETRY_AFTER = 60.0

# A name noted again within this long of its stored sighting is not written again: a live note per message
# would be a write per message. Within a rank the newest sighting wins, so the store lags by up to this.
_RESEEN_MS = 24 * 3600 * 1000

# Lookups run inline in the event loop and a 429 retry can take ~30s: don't repeat that per message.
_THREAD_RETRY_AFTER = 60.0


def is_app(mri: str) -> bool:
    """`28:<id>` MRIs are bots/apps; people are `8:orgid:<guid>`."""
    return mri.rsplit("/", 1)[-1].startswith("28:")


def humans(members: list[dict[str, Any]]) -> dict[str, str]:
    """Roster → {mri: display name} of its people; bots and apps left out."""
    return {str(m["mri"]): str(m["name"] or "") for m in members if not is_app(str(m["mri"]))}


def roster_label(topic: str | None, members: list[dict[str, Any]], me: str) -> str:
    """Topic · N, else the roster's people but me, else its apps, else anyone named; "" for nobody.

    Apps/bots sit in the roster as `28:` members (Jira Cloud, Confluence Cloud…): a chat is named
    after its people, unless there is nobody else to name it after."""
    if topic:
        return f"{topic} · {len(members)}p"
    named = [m for m in members if m.get("name")]
    others = [str(m["name"]) for m in named if m.get("mri") != me and not is_app(str(m.get("mri") or ""))]
    apps = [str(m["name"]) for m in named if is_app(str(m.get("mri") or ""))]
    return ", ".join(others or apps or [str(m["name"]) for m in named])


def sender_name(mri: str, declared: object, known_name: Callable[[str], str] | None) -> str:
    """Who sent a message: the directory's name for `mri`, else the name this message declares, else "".

    The declared `imdisplayname` is free text the sending client chose; Teams attributes by `from`."""
    return (known_name(mri) if known_name and mri else "") or str(declared or "")


def _bare(mri: str) -> str:
    # A message's `from` is a contact URL whose host varies by region: key the cache on the MRI.
    return mri.rsplit("/", 1)[-1]


def _tail(mri: str) -> str:
    """An unresolved MRI's label, e.g. the guid of `8:orgid:<guid>`."""
    return mri.split(":")[-1]


def _mri_from_userlink(user_link: str | None) -> str:
    # userLink is a contact URL ending in the MRI, e.g. ".../v1/users/8:orgid:<guid>".
    return _bare(user_link or "")


def _open_store(path: Path) -> NameStore | None:
    try:
        return NameStore(path)
    except (OSError, sqlite3.Error) as exc:  # names then live in memory only: the stream goes on
        log.warning("names_store_unavailable", path=str(path), error=str(exc))
        return None


def _cached_names(path: Path) -> list[tuple[str, str]]:
    """(MRI, value) pairs of an old JSON names cache; [] when missing or corrupt."""
    try:
        loaded = json.loads(path.read_text())
    except OSError, ValueError:
        return []
    # Contact-URL keys are never looked up: lookups key on the bare MRI.
    items = loaded.items() if isinstance(loaded, dict) else []
    return [(k, v) for k, v in items if isinstance(k, str) and "/" not in k and isinstance(v, str) and v]


class _Held(NamedTuple):
    name: str
    rank: int
    seen: int  # the newest sighting of this name at this rank this process knows of
    written: int  # its sighting the store holds, as far as this process knows


def _ms(value: object) -> int:
    try:
        return int(str(value))
    except ValueError:
        return 0


def roster_entries(index: Path) -> list[Entry]:
    """The names stored rosters give their members, seen at their conversation's `version` (stored with
    the roster; `last_fetch_at` moves on passes that keep the roster); read-only: the archive is never
    written here."""
    from .archive_store import open_untouched  # archive_store imports messages, which imports this module

    db = open_untouched(index)
    try:
        rosters = db.execute(
            "SELECT participants, CASE WHEN json_valid(raw) THEN json_extract(raw, '$.version') END"
            " FROM chats"
        ).fetchall()
    finally:
        db.close()
    entries: list[Entry] = []
    for text, version in rosters:
        seen = _ms(version)  # epoch ms; 0 for a chat stored without its conversation
        try:
            people = json.loads(text or "[]")
        except ValueError:
            continue
        for p in people if isinstance(people, list) else []:
            if not isinstance(p, dict):
                continue
            mri, name = _bare(str(p.get("mri") or "")), str(p.get("name") or "")
            if mri and name and name != _tail(mri):
                entries.append((mri, name, ARCHIVED, "index-roster", seen))
    return entries


class Directory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.skype_token = ""
        self.bearer = ""  # AAD access token, for the profile lookup (Bearer auth)
        self.me = ""  # own MRI; left out of roster-built labels (a 1:1 is named after the other)
        self.media = DEFAULT_MEDIA  # `stream --media`: attachment types printed and downloaded
        # thread_id → (monotonic time fetched, info); LRU-bounded (see _THREADS_MAX).
        self._threads: OrderedDict[str, tuple[float, dict[str, Any] | None]] = OrderedDict()
        self._thread_fetch: dict[str, asyncio.Task[dict[str, Any] | None]] = {}  # in flight
        # thread_id → monotonic time of a transient failure; capped like _threads.
        self._thread_failed: OrderedDict[str, float] = OrderedDict()
        # Read cache of the store, bare MRI → its best name; a miss reads the store other processes fill.
        self._names: dict[str, _Held] = {}
        self._missed: set[str] = set()  # a successful lookup found no name for them
        # (MRI, rank) → latest entry not written yet: one per person and rank, kept while writes fail.
        self._pending: dict[tuple[str, int], Entry] = {}
        self._pending_lock = threading.Lock()  # the event loop and executor threads both note names
        self._failed_at = -math.inf  # names.db is left alone for _STORE_RETRY_AFTER after a failure
        self._cache_due = False  # the cache import found no usable store: the next store call runs it
        # msg_id → {key → set(MRI)}; LRU-bounded (see _REACTION_LRU_MAX) to cap memory.
        self._reactions: OrderedDict[str, dict[str, set[str]]] = OrderedDict()
        self._store: NameStore | None = None
        self._open_lock = threading.Lock()
        self._reopen()

    def _reopen(self) -> NameStore | None:
        """Open names.db; a failed open pauses like a failed call, so a days-long process gets it back."""
        with self._open_lock:
            if self._store is not None or self._paused():  # opened, or just failed, while this waited
                return self._store
            store = _open_store(self.settings.config_dir / "names.db")
            if store is None:
                self._pause()
                return None
            weakref.finalize(self, store.close)
            self._store = store
        with self._pending_lock:
            self._cache_due = False
        self._import_cached_names()
        self._put([])
        return store

    def _pause(self) -> None:
        with self._pending_lock:
            self._failed_at = time.monotonic()

    def _paused(self) -> bool:
        return time.monotonic() - self._failed_at < _STORE_RETRY_AFTER

    def _stored[T](self, op: Callable[[NameStore], T], default: T) -> T:
        """`op` on the store, or `default`: no store, or paused after a failure (each call may wait 2 s)."""
        if self._paused():
            return default
        store = self._store or self._reopen()
        if store is None:
            return default
        with self._pending_lock:
            cache, self._cache_due = self._cache_due, False
        if cache:
            self._import_cached_names()
            if self._paused():  # it failed again: one failure per window
                return default
        try:
            return op(store)
        except sqlite3.Error as exc:  # a locked or broken names.db must not break the stream
            log.warning("names_store_failed", error=str(exc))
            self._pause()
            return default

    def _remember(self, rows: dict[str, Row]) -> None:
        for mri, row in rows.items():
            if row.name:
                held = self._names.get(mri)
                same = held is not None and (held.name, held.rank) == (row.name, row.rank)
                seen = max(row.seen_at, held.seen) if same and held is not None else row.seen_at
                self._names[mri] = _Held(row.name, row.rank, seen, row.seen_at)
            if row.looked_up:
                self._missed.add(mri)

    def _note(self, pairs: list[tuple[str, str]], rank: int, source: str) -> None:
        """Record (MRI, name) pairs at `rank`, seen now; the store keeps the best (038-AC2)."""
        seen = now_ms()
        self.note_entries((mri, name, rank, source, seen) for mri, name in pairs)

    def note_entries(self, entries: Iterable[Entry]) -> None:
        """Record names, each at its own rank and sighting time (038-AC8), in one write; the store keeps
        the best (038-AC2)."""
        batch: list[Entry] = []
        for mri, name, rank, source, seen in entries:
            mri = _bare(mri)
            if not mri or not name or name == _tail(mri):
                continue
            held = self._names.get(mri)
            if held is not None and (held.rank > rank or held.rank == rank and seen < held.seen):
                continue  # could not win: ranks only grow, and within one the newest sighting wins
            if (
                held is not None
                and (held.name, held.rank) == (name, rank)
                and seen <= held.written + _RESEEN_MS
            ):
                # Stored or pending: a write per message would stall each on a held lock.
                self._names[mri] = held._replace(seen=seen)
                continue
            self._names[mri] = _Held(name, rank, seen, seen)  # kept even when the store is down
            batch.append((mri, name, rank, source, seen))
        self._put(batch)

    def flush(self, tries: int = 3, wait: Callable[[float], object] | None = None) -> bool:
        """Write what is queued, `tries` times at most, waiting out a failing store between them: a run
        about to end must not drop it. False, logged, when some stayed unwritten."""
        for attempt in range(tries):
            if attempt:
                (wait or time.sleep)(_STORE_RETRY_AFTER)
            if attempt == tries - 1:
                with self._pending_lock:
                    self._failed_at = -math.inf  # the last chance: no pause skips it
            self._put([])
            with self._pending_lock:
                left = len(self._pending)
            if not left:
                return True
        log.error("names_unwritten", count=left)
        return False

    def _queue(self, entries: Iterable[Entry]) -> None:
        """Keep the latest seen entry per (MRI, rank); the caller holds _pending_lock."""
        for entry in entries:
            held = self._pending.get((entry[0], entry[2]))
            if held is None or entry[4] >= held[4]:
                self._pending[entry[0], entry[2]] = entry

    def _put(self, entries: list[Entry]) -> None:
        """Write `entries` and those still queued; queued again when the store is missing or paused.
        The store's SQL keeps the best of each."""
        with self._pending_lock:
            self._queue(entries)
            if not self._pending:
                return
            batch, self._pending = self._pending, {}
        rows = self._stored(lambda s: s.put(batch.values()), None)
        if rows is None:
            with self._pending_lock:
                newer, self._pending = self._pending, batch
                self._queue(newer.values())  # what was noted meanwhile wins ties
            return
        self._remember(rows)

    def _note_missed(self, mris: list[str]) -> None:
        self._missed.update(mris)
        self._stored(lambda s: s.missed(mris, now_ms()), None)

    def _import_cached_names(self) -> None:
        """038-AC4: names-v2.json as archived, the older names.json as declared, once per names.db."""
        done = self._stored(lambda s: s.imported(_CACHE_IMPORT), None)
        if done is None:
            return self._cache_import_due()
        if done:
            return
        entries: list[Entry] = []
        missed: list[str] = []
        for mri, name in _cached_names(self.settings.cache_dir / "names-v2.json"):
            if name == _tail(mri):
                missed.append(mri)  # its negative-cache marker
            else:
                entries.append((mri, name, ARCHIVED, "names-v2", 0))
        old = _cached_names(self.settings.cache_dir / "names.json")
        entries += [(mri, name, DECLARED, "names-json", 0) for mri, name in old if name != _tail(mri)]
        if self._stored(lambda s: s.import_once(_CACHE_IMPORT, entries, missed, now_ms()), None) is None:
            self._cache_import_due()

    def _cache_import_due(self) -> None:
        with self._pending_lock:
            self._cache_due = True

    def import_rosters(self, data_dir: Path, wait: Callable[[float], object] = time.sleep) -> None:
        """038-AC4: the names stored rosters in `<data_dir>/index.db` hold, once per names.db and archive.

        While names.db is unusable it waits and tries again: its Directory may be a throwaway."""
        index = data_dir / "index.db"
        if not index.is_file():
            return
        while True:
            try:
                if self._import_roster(index):
                    return
            except (OSError, sqlite3.Error) as exc:  # the archive unreadable: retried on the next start
                log.warning("names_roster_import_failed", error=str(exc))
                return
            wait(_STORE_RETRY_AFTER)

    def _import_roster(self, index: Path) -> bool:
        """One attempt; False when names.db was unusable. The archive is read only once the store answered."""
        key = f"{_ROSTER_IMPORT}:{index.resolve()}"  # per archive: a scratch one must not mark the real one
        done = self._stored(lambda s: s.imported(key), None)
        if done is None:
            return False
        if done:
            return True
        entries = roster_entries(index)
        return self._stored(lambda s: s.import_once(key, entries, [], now_ms()), None) is not None

    def start_roster_import(self, data_dir: Path) -> None:
        """`import_rosters` in a daemon thread: a hung archive mount holds neither the start nor the exit."""
        threading.Thread(target=self.import_rosters, args=(data_dir,), daemon=True).start()

    def set_token(self, skype_token: str, bearer: str = "") -> None:
        self.skype_token = skype_token
        self._thread_failed.clear()  # an expired token may be why they failed
        if bearer:
            self.bearer = bearer

    def refresh(self) -> None:
        """Forget what this instance cached (names, rosters) and the store's pause, for a long-lived owner
        acting per request (MCP): it reads what others learned since, and a failed store gets one more try.

        Also drops thread fetches left by a closed event loop, which a later loop must never await."""
        self._names.clear()
        self._missed.clear()
        self._threads.clear()
        self._thread_failed.clear()
        self._thread_fetch.clear()
        with self._pending_lock:
            self._failed_at = -math.inf

    def note_name(self, mri: str, name: str | None, source: str = "profile") -> None:
        """A live name, from a profile lookup or a fetched roster: the best rank."""
        self._note([(mri, name or "")], LIVE, source)

    def note_declared(self, mri: str, name: str | None, seen: int) -> None:
        """A name a message sent at `seen` (epoch ms) declares for its sender: a last-resort label, never a
        known name. The message's time, not now: an old message shown again must not beat a newer name."""
        self.note_entries([(mri, name or "", DECLARED, "declared", seen)])

    def known_name(self, mri: str) -> str:
        """The live or archived name of `mri`; "" when it has none."""
        mri = _bare(mri)
        held = self._names.get(mri)
        if mri and (held is None or held.rank < ARCHIVED):
            self._remember(self._stored(lambda s: s.get([mri]), {}))
            held = self._names.get(mri)
        return held.name if held is not None and held.rank >= ARCHIVED else ""

    def name_for(self, mri: str) -> str:
        """A label for `mri`: its known name, else the last name a message declared, else its bare id."""
        mri = _bare(mri)
        if known := self.known_name(mri):  # reads the store into _names
            return known
        held = self._names.get(mri)
        return held.name if held else _tail(mri)

    def _lookup_due(self, mri: str, refresh: bool) -> bool:
        held = self._names.get(mri)
        return (held is None or held.rank < LIVE) and (refresh or mri not in self._missed)

    async def resolve(self, mris: list[str], *, refresh: bool = False) -> None:
        """Batched MRI → display-name lookup of those without a live name; an MRI it names nobody for is
        noted, and asked again only with `refresh`: a user unknown once may resolve now."""
        if not self.bearer:
            return
        wanted = {m for m in map(_bare, mris) if m and self._lookup_due(m, refresh)}
        if wanted:
            self._remember(self._stored(lambda s: s.get(wanted), {}))
        todo = sorted(m for m in wanted if self._lookup_due(m, refresh))
        if not todo:
            return
        headers = {
            "Authorization": f"Bearer {self.bearer}",
            "X-Skypetoken": self.skype_token,
            "Content-Type": "application/json",
            "User-Agent": self.settings.user_agent,
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(self.settings.profiles_url, headers=headers, json=todo)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001 — transient failure: leave uncached, retry later
            log.warning("profile_resolve_failed", count=len(todo), error=failure(exc))
            return
        users = data.get("value") or data.get("resolvedUsers") or []
        pairs = [(_bare(str(u.get("mri") or "")), str(u.get("displayName") or "")) for u in users]
        self._note(pairs, LIVE, "profile")
        # Noted only after a successful lookup: transient failures above return early and stay retryable.
        named = {mri for mri, name in pairs if name and name != _tail(mri)}
        if missed := [mri for mri in todo if mri not in named]:
            self._note_missed(missed)

    async def display(self, mri: str) -> str:
        """Resolve an MRI to a display name (cached); falls back to the stripped id."""
        mri = _bare(mri)
        if self.bearer and mri.startswith("8:orgid:") and self._lookup_due(mri, False):
            await self.resolve([mri])
        return self.name_for(mri)

    def reaction_diff(self, msg_id: str, key: str, users: list[str]) -> tuple[set[str], set[str]]:
        """Update stored reaction state for (msg_id, key); return (added_mris, removed_mris)."""
        keys = self._reactions.setdefault(msg_id, {})
        self._reactions.move_to_end(msg_id)  # mark recently-active for the LRU
        prev = keys.get(key, set())
        current = set(users)
        keys[key] = current
        while len(self._reactions) > _REACTION_LRU_MAX:
            self._reactions.popitem(last=False)  # evict least-recently-active message
        return current - prev, prev - current

    def forget(self, thread_id: str) -> None:
        """Drop cached thread info so the next label() refetches it (rename, roster change)."""
        self._threads.pop(thread_id, None)
        self._thread_failed.pop(thread_id, None)

    def known(self, thread_id: str) -> bool:
        """A copy is cached, stale or a 404: thread() answers it without waiting on a fetch."""
        return thread_id in self._threads

    async def thread(self, thread_id: str, max_age: float | None = None) -> dict[str, Any] | None:
        """Topic, roster and picture of a thread; None when unknown or not fetchable right now.

        A cached copy older than `max_age` seconds is refetched in the background (members join and
        leave without an event reaching us) and returned meanwhile: the next caller gets the refetch.
        Concurrent callers share one fetch, which a cancelled caller does not cancel.
        """
        cached = self._threads.get(thread_id)
        if cached is not None and (max_age is None or time.monotonic() - cached[0] <= max_age):
            self._threads.move_to_end(thread_id)
            return cached[1]
        failed_at = self._thread_failed.get(thread_id)
        if failed_at is not None and time.monotonic() - failed_at < _THREAD_RETRY_AFTER:
            return cached[1] if cached else None
        task = self._thread_fetch.get(thread_id)
        if task is None:
            task = self._thread_fetch[thread_id] = asyncio.create_task(self._load_thread(thread_id))
            task.add_done_callback(lambda _: self._thread_fetch.pop(thread_id, None))
        if cached is not None:
            return cached[1]  # stale while revalidating: the page asks per keystroke
        # Shielded: the page asks per keystroke, and one closing must not cancel what the others await.
        return await asyncio.shield(task)

    async def _load_thread(self, thread_id: str) -> dict[str, Any] | None:
        try:
            info = await self._fetch_thread(thread_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                # 429/5xx: a transient failure must not pin the bare id as the label for the
                # whole process lifetime — retry once the window has passed instead.
                log.debug("thread_fetch_failed", thread=thread_id, status=exc.response.status_code)
                return self._thread_failure(thread_id)
            info = None
        except Exception as exc:  # noqa: BLE001 — enrichment is best-effort, never fatal
            log.debug("thread_fetch_failed", thread=thread_id, error=failure(exc))
            return self._thread_failure(thread_id)
        self._thread_failed.pop(thread_id, None)
        self._threads[thread_id] = (time.monotonic(), info)  # a definitive 404 is cached too
        self._threads.move_to_end(thread_id)
        while len(self._threads) > _THREADS_MAX:
            self._threads.popitem(last=False)
        return info

    def _thread_failure(self, thread_id: str) -> dict[str, Any] | None:
        """Note a transient failure; the stale copy, if any, is kept and returned."""
        # Forever-running stream: cap it; thread() ignores entries past _THREAD_RETRY_AFTER anyway.
        self._thread_failed[thread_id] = time.monotonic()
        self._thread_failed.move_to_end(thread_id)
        while len(self._thread_failed) > _THREADS_MAX:
            self._thread_failed.popitem(last=False)
        stale = self._threads.get(thread_id)
        return stale[1] if stale else None

    async def _fetch_thread(self, thread_id: str) -> dict[str, Any] | None:
        url = (
            f"https://{self.settings.contacts_host}"
            f"/v1/threads/{quote(thread_id, safe='')}?view=msnp24Equivalent"
        )
        headers = {
            "X-Skypetoken": self.skype_token,
            "User-Agent": self.settings.user_agent,
            "Accept": "application/json; ver=1.0;",
            "BehaviorOverride": "redirectAs404",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Parallel label resolution (web bootstrap) trips the chat-service rate limit;
            # honour Retry-After instead of pinning the bare id. thread() decides what is cacheable.
            resp = await aget_with_retry(client, url, headers=headers)
            data = resp.json()

        members: list[dict[str, Any]] = []
        for m in data.get("members") or []:
            mri = m.get("linkedMri") or _mri_from_userlink(m.get("userLink")) or m.get("mri") or ""
            name = m.get("friendlyName") or m.get("friendlyname") or ""
            members.append({"mri": mri, "name": name, "role": m.get("role")})
        self._note([(m["mri"], m["name"]) for m in members], LIVE, "roster")

        # Resolve roster members the thread didn't name (bare-guid MRIs) in one batched call.
        await self.resolve([m["mri"] for m in members if not m["name"]])
        for m in members:
            m["name"] = m["name"] or self.known_name(m["mri"]) or _tail(m["mri"])  # never a declared name

        props = data.get("properties") or {}
        topic = props.get("topic") or None
        # `picture` is `URL@<AMS url>` when the chat has a custom icon (see avatars.fetch_group_icon).
        # `createdat` (ms epoch) lets the archive confirm a backfill reached the very first message.
        return {
            "topic": topic,
            "members": members,
            "picture": props.get("picture") or None,
            "created_at": props.get("createdat"),
        }

    async def label(self, thread_id: str) -> str:
        """Human-readable thread label: topic · N, else roster names, else bare id."""
        if thread_id in _SPECIAL_THREADS:
            return _SPECIAL_THREADS[thread_id]
        info = await self.thread(thread_id)
        if not info:
            return thread_id
        return roster_label(info["topic"], info["members"], self.me) or thread_id
