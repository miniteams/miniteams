"""Reads straight from the chat service, for when the archive cannot be reached (spec 008).

Same records as the archive path, `source: "api"`. Bounded walks: the listing stops after
`_MAX_CONVERSATIONS`, a history after `_MAX_PAGES` pages, a search after `_MAX_CHATS` chats.
"""

import asyncio
from collections.abc import Callable
from typing import Any

import structlog

from .chats import fetch_conversations, is_system, last_activity
from .config import Settings
from .directory import Directory
from .dump import _epoch_seconds, iter_history_pages
from .send import NOTES_THREAD

log = structlog.get_logger()

_MAX_CONVERSATIONS = 500  # listing rows walked by find_chats
_MAX_LABELS = 100  # candidates whose label is resolved (one thread fetch each for a 1:1)
_MAX_PAGES = 50  # history pages per chat
_MAX_CHATS = 50  # chats a search reads
_PAGE_SIZE = 100

Record = Callable[[dict[str, Any]], dict[str, Any]]
Kind = Callable[[str], str]
Fold = Callable[[str], str]


class ApiReader:
    def __init__(
        self, settings: Settings, skype_token: str, bearer: str, *, record: Record, kind: Kind, fold: Fold
    ):
        self.settings, self.skype_token = settings, skype_token
        self.record, self.kind, self.fold = record, kind, fold
        self.directory = Directory(settings)
        self.directory.set_token(skype_token, bearer)

    def _conversations(self, since: str) -> list[dict[str, Any]]:
        """Listing rows active at or after `since`, newest first, at most `_MAX_CONVERSATIONS`."""
        rows: list[dict[str, Any]] = []
        for page in fetch_conversations(self.settings, self.skype_token):
            for conv in page:
                thread_id = str(conv.get("id") or "")
                if not thread_id:
                    continue
                if since and last_activity(conv) < since and not is_system(thread_id):
                    return rows  # the listing is ordered by activity: nothing newer follows
                rows.append(conv)
                if len(rows) >= _MAX_CONVERSATIONS:
                    return rows
        return rows

    def find_chats(self, query: str, participant: str, kind: str, since: str, limit: int) -> dict[str, Any]:
        q, who = self.fold(query), self.fold(participant)
        candidates = [
            c
            for c in self._conversations(since)
            if (not kind or self.kind(str(c["id"])) == kind) and (not since or last_activity(c) >= since)
        ]
        found: list[dict[str, Any]] = []

        async def _labels(convs: list[dict[str, Any]]) -> list[str]:
            return list(await asyncio.gather(*(self.directory.label(str(c["id"])) for c in convs)))

        head = candidates[:_MAX_LABELS]
        labels = asyncio.run(_labels(head))
        for conv, label in zip(head, labels, strict=True):
            thread_id = str(conv["id"])
            topic = str((conv.get("threadProperties") or {}).get("topic") or "")
            haystack = self.fold(f"{label} {topic}")
            if q and q not in haystack:
                continue
            if who and who not in self.fold(label):
                continue
            found.append(
                {
                    "id": thread_id,
                    "label": label,
                    "kind": self.kind(thread_id),
                    "last_activity": last_activity(conv),
                    "participants": [],
                }
            )
            if len(found) >= limit:
                break
        return {"chats": found, "labels_resolved": len(head), "more": len(candidates) > len(head)}

    def _history(self, thread_id: str, since: str, until: str) -> list[dict[str, Any]]:
        """Messages inside [since, until), oldest first, walking the history back from `until`."""
        end_before = _epoch_seconds(until) if until else None
        collected: dict[str, dict[str, Any]] = {}
        pages = 0
        for page in iter_history_pages(self.settings, self.skype_token, thread_id, _PAGE_SIZE, 0, end_before):
            pages += 1
            for message in page:
                when = str(message.get("composetime") or "")
                if when >= since and (not until or when < until):
                    collected[str(message.get("id") or "")] = message
            if str(page[-1].get("composetime") or "") < since or pages >= _MAX_PAGES:
                break
        return sorted(collected.values(), key=lambda m: str(m.get("composetime") or ""))

    def read_messages(self, thread_id: str, since: str, until: str, limit: int) -> dict[str, Any]:
        messages = self._history(thread_id, since, until)
        result: dict[str, Any] = {"thread_id": thread_id, "since": since}
        if len(messages) > limit:
            cut = str(messages[limit].get("composetime") or "")
            messages = [m for m in messages[:limit] if str(m.get("composetime") or "") < cut]
            result["next_since"] = cut
        result["messages"] = [self.record(m) for m in messages]
        return result

    def search_messages(
        self, query: str, since: str, until: str, thread_id: str, limit: int
    ) -> dict[str, Any]:
        needle = self.fold(query)
        active: list[dict[str, Any]] = []
        if thread_id:
            targets = [(thread_id, "")]
        else:
            active = [
                c
                for c in self._conversations(since)
                if not is_system(str(c["id"])) or str(c["id"]) == NOTES_THREAD
            ]
            targets = [(str(c["id"]), "") for c in active[:_MAX_CHATS]]
        hits: list[dict[str, Any]] = []
        for chat_id, label in targets:
            for message in self._history(chat_id, since, until):
                record = self.record(message)
                if needle in self.fold(record["text"]):
                    hits.append({**record, "thread_id": chat_id, "chat": label})
        hits.sort(key=lambda h: h["time"], reverse=True)
        left_out = max(0, len(active) - _MAX_CHATS)
        return {
            "chats_scanned": len(targets),
            "chats_left_out": left_out,
            "more": len(hits) > limit or left_out > 0,
            "messages": hits[:limit],
        }
