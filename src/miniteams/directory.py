"""Thread + identity resolution, cached (handoff §M5).

A single `GET /v1/threads/<id>?view=msnp24Equivalent` (X-Skypetoken) returns the member roster
*with* display names already bundled (`members[].friendlyName`) — so no separate profile lookup
is needed. The topic isn't formally part of that response, but `properties.topic` is usually
present; we read it opportunistically and fall back to the roster otherwise.

Caches live for the process lifetime and survive reconnects (only the skype token is refreshed
per session). Lookups never raise into the stream — a failed fetch degrades to the bare id.
"""

from typing import Any
from urllib.parse import quote

import httpx
import structlog

from .config import Settings

log = structlog.get_logger()

# MRI prefix → friendly label for special threads that have no real roster/topic.
_SPECIAL_THREADS = {"48:notes": "Notes to self"}


def _mri_from_userlink(user_link: str | None) -> str:
    # userLink is a contact URL ending in the MRI, e.g. ".../v1/users/8:orgid:<guid>".
    return user_link.rsplit("/", 1)[-1] if user_link else ""


class Directory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.skype_token = ""
        self._threads: dict[str, dict[str, Any] | None] = {}
        self._names: dict[str, str] = {}  # MRI → display name
        self._reactions: dict[str, dict[str, set[str]]] = {}  # msg_id → {key → set(MRI)}

    def set_token(self, skype_token: str) -> None:
        self.skype_token = skype_token

    def note_name(self, mri: str, name: str | None) -> None:
        if mri and name:
            self._names[mri] = name

    def name_for(self, mri: str) -> str:
        # Strip the "8:orgid:" style prefix for an unresolved MRI.
        return self._names.get(mri) or mri.split(":")[-1]

    def reaction_diff(self, msg_id: str, key: str, users: list[str]) -> tuple[set[str], set[str]]:
        """Update stored reaction state for (msg_id, key); return (added_mris, removed_mris)."""
        prev = self._reactions.setdefault(msg_id, {}).get(key, set())
        current = set(users)
        self._reactions[msg_id][key] = current
        return current - prev, prev - current

    async def thread(self, thread_id: str) -> dict[str, Any] | None:
        if thread_id in self._threads:  # cached (incl. negative results)
            return self._threads[thread_id]
        info = await self._fetch_thread(thread_id)
        self._threads[thread_id] = info
        return info

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
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001 — enrichment is best-effort, never fatal
            log.debug("thread_fetch_failed", thread=thread_id, error=str(exc))
            return None

        members: list[dict[str, Any]] = []
        for m in data.get("members") or []:
            mri = m.get("linkedMri") or _mri_from_userlink(m.get("userLink")) or m.get("mri") or ""
            name = m.get("friendlyName") or m.get("friendlyname") or ""
            self.note_name(mri, name)
            members.append({"mri": mri, "name": name or self.name_for(mri), "role": m.get("role")})

        topic = (data.get("properties") or {}).get("topic") or None
        return {"topic": topic, "members": members}

    async def label(self, thread_id: str) -> str:
        """Human-readable thread label: topic · N, else roster names, else bare id."""
        if thread_id in _SPECIAL_THREADS:
            return _SPECIAL_THREADS[thread_id]
        info = await self.thread(thread_id)
        if not info:
            return thread_id
        members = info["members"]
        if info["topic"]:
            return f"{info['topic']} · {len(members)}p"
        if members:
            names = ", ".join(m["name"] for m in members if m["name"])
            return names or thread_id
        return thread_id
