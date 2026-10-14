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
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from .attachments import DEFAULT_MEDIA
from .config import Settings, write_private
from .http import aget_with_retry, failure

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

# Lookups run inline in the event loop and a 429 retry can take ~30s: don't repeat that per message.
_THREAD_RETRY_AFTER = 60.0


def is_app(mri: str) -> bool:
    """`28:<id>` MRIs are bots/apps; people are `8:orgid:<guid>`."""
    return mri.rsplit("/", 1)[-1].startswith("28:")


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


def _bare(mri: str) -> str:
    # A message's `from` is a contact URL whose host varies by region: key the cache on the MRI.
    return mri.rsplit("/", 1)[-1]


def _mri_from_userlink(user_link: str | None) -> str:
    # userLink is a contact URL ending in the MRI, e.g. ".../v1/users/8:orgid:<guid>".
    return _bare(user_link or "")


class Directory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.skype_token = ""
        self.bearer = ""  # AAD id_token, for the profile lookup (Bearer auth)
        self.me = ""  # own MRI; left out of roster-built labels (a 1:1 is named after the other)
        self.media = DEFAULT_MEDIA  # `stream --media`: attachment types printed and downloaded
        # thread_id → (monotonic time fetched, info); LRU-bounded (see _THREADS_MAX).
        self._threads: OrderedDict[str, tuple[float, dict[str, Any] | None]] = OrderedDict()
        self._thread_fetch: dict[str, asyncio.Task[dict[str, Any] | None]] = {}  # in flight
        # thread_id → monotonic time of a transient failure; capped like _threads.
        self._thread_failed: OrderedDict[str, float] = OrderedDict()
        self._names_path = settings.cache_dir / "names.json"
        self._names: dict[str, str] = self._load_names()  # MRI → display name (persisted)
        # msg_id → {key → set(MRI)}; LRU-bounded (see _REACTION_LRU_MAX) to cap memory.
        self._reactions: OrderedDict[str, dict[str, set[str]]] = OrderedDict()

    def _load_names(self) -> dict[str, str]:
        try:
            loaded = json.loads(self._names_path.read_text())
            # Contact-URL keys are never looked up: lookups key on the bare MRI.
            return {k: v for k, v in loaded.items() if "/" not in k} if isinstance(loaded, dict) else {}
        except OSError, ValueError:
            return {}

    def _save_names(self) -> None:
        try:
            write_private(self._names_path, json.dumps(self._names, ensure_ascii=False))
        except OSError as exc:  # cache is best-effort; a write failure must not break the stream
            log.debug("names_cache_save_failed", error=str(exc))

    def set_token(self, skype_token: str, bearer: str = "") -> None:
        self.skype_token = skype_token
        self._thread_failed.clear()  # an expired token may be why they failed
        if bearer:
            self.bearer = bearer

    def note_name(self, mri: str, name: str | None) -> None:
        mri = _bare(mri)
        if mri and name:
            self._names[mri] = name

    def _unknown(self, mri: str) -> bool:
        return self._names.get(mri) == mri.split(":")[-1]  # the negative-cache marker

    def name_for(self, mri: str) -> str:
        # Strip the "8:orgid:" style prefix for an unresolved MRI.
        mri = _bare(mri)
        return self._names.get(mri) or mri.split(":")[-1]

    async def resolve(self, mris: list[str], *, refresh: bool = False) -> None:
        """Batched MRI → display-name lookup; caches results (incl. negatives, to avoid refetch).
        `refresh` re-asks for negatives too: a user unknown once may resolve now."""
        todo = sorted(
            {m for m in map(_bare, mris) if m and (m not in self._names or (refresh and self._unknown(m)))}
        )
        if not todo or not self.bearer:
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
            log.debug("profile_resolve_failed", count=len(todo), error=failure(exc))
            return
        for user in data.get("value") or data.get("resolvedUsers") or []:
            self.note_name(user.get("mri", ""), user.get("displayName"))
        # Negative-cache genuinely-unknown MRIs (lookup succeeded but returned no name) so we
        # don't re-query them; transient failures above return early and stay retryable.
        for mri in todo:
            self._names.setdefault(mri, mri.split(":")[-1])
        self._save_names()

    async def display(self, mri: str) -> str:
        """Resolve an MRI to a display name (cached); falls back to the stripped id."""
        mri = _bare(mri)
        if mri not in self._names and self.bearer and mri.startswith("8:orgid:"):
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
            self.note_name(mri, name)
            members.append({"mri": mri, "name": name, "role": m.get("role")})

        # Resolve roster members the thread didn't name (bare-guid MRIs) in one batched call.
        await self.resolve([m["mri"] for m in members if not m["name"]])
        for m in members:
            m["name"] = m["name"] or self.name_for(m["mri"])
        self._save_names()  # persist friendlyName-sourced names too

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
