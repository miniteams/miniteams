"""Conversation listing (chat-service `GET /users/ME/conversations`).

The server returns conversations newest-activity-first with the `lastMessage` embedded;
older pages are walked via `_metadata.backwardLink`. The list only carries the *last*
activity per conversation, so an `--until` bound in the past needs a one-request probe per
candidate chat: fetch the newest message before the bound and check it against `--since`.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from ._io import emit, force_blocking_stdout
from .config import Settings
from .directory import Directory
from .dump import _TARGET_TYPE, fetch_history
from .http import get_with_retry

log = structlog.get_logger()

_PAGE_SIZE = 100
_MAX_PAGES = 50  # safety cap on backwardLink walking


def parse_when(value: str, *, end: bool = False) -> datetime:
    """ISO date/datetime → aware UTC datetime; a date-only upper bound covers its whole day."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    if end and len(value) == 10:
        dt += timedelta(days=1)
    return dt


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def is_meeting(thread_id: str) -> bool:
    """Meeting-associated chat (the conversation attached to a Teams meeting)."""
    return thread_id.startswith("19:meeting_")


def is_private(thread_id: str) -> bool:
    """1:1 (@unq.gbl.spaces) and group chats (@thread.v2), excluding meeting chats/channels."""
    if "@unq.gbl.spaces" in thread_id:
        return True
    return thread_id.endswith("@thread.v2") and not is_meeting(thread_id)


def _version_iso(conv: dict[str, Any]) -> str:
    version = conv.get("version") or 0  # ms epoch; bumped on ANY thread update, not just messages
    if not version:
        return ""
    return datetime.fromtimestamp(int(version) / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_activity(conv: dict[str, Any]) -> str:
    """ISO-Z timestamp of the conversation's newest message ('' if none known)."""
    last = conv.get("lastMessage") or {}
    when = last.get("originalarrivaltime") or last.get("composetime") or ""
    return str(when) if when else _version_iso(conv)


def fetch_conversations(settings: Settings, skype_token: str) -> Iterator[list[dict[str, Any]]]:
    """Yield pages of conversations, newest activity first; stops at _MAX_PAGES."""
    url = (
        f"https://{settings.contacts_host}/v1/users/ME/conversations"
        f"?startTime=1&view=msnp24Equivalent&targetType={_TARGET_TYPE}&pageSize={_PAGE_SIZE}"
    )
    headers = {
        "X-Skypetoken": skype_token,
        "User-Agent": settings.user_agent,
        "Accept": "application/json; ver=1.0;",
    }
    with httpx.Client(timeout=30.0, headers=headers) as client:
        for page in range(_MAX_PAGES):
            resp = get_with_retry(client, url)
            data = resp.json()
            conversations = data.get("conversations") or []
            log.debug("conversations_page", page=page + 1, count=len(conversations))
            if not conversations:
                return
            yield conversations
            url = (data.get("_metadata") or {}).get("backwardLink") or ""
            if not url:
                return


def _newest_before(settings: Settings, skype_token: str, thread_id: str, until: datetime) -> str:
    """Composetime of the newest message strictly before `until` ('' if none)."""
    messages = fetch_history(
        settings,
        skype_token,
        thread_id,
        page_size=1,
        max_pages=1,
        end_before=int(until.timestamp()),
    )
    return str(messages[-1].get("composetime", "")) if messages else ""


async def list_chats(
    settings: Settings,
    skype_token: str,
    bearer: str,
    limit: int,
    since: datetime | None,
    until: datetime | None,
    include_all: bool,
    jsonl: bool,
) -> None:
    force_blocking_stdout()
    directory = Directory(settings)
    directory.set_token(skype_token, bearer)
    since_iso = _iso(since) if since else ""
    until_iso = _iso(until) if until else ""
    shown = 0

    for conversations in fetch_conversations(settings, skype_token):
        for conv in conversations:
            thread_id = str(conv.get("id") or "")
            when = last_activity(conv)
            if not thread_id or not when or (not include_all and not is_private(thread_id)):
                continue
            if since_iso and max(when, _version_iso(conv)) < since_iso:
                # The list is ordered by conversation *update* recency (version covers
                # membership-only bumps): everything after this point is older still.
                return
            if since_iso and when < since_iso:
                # Thread was bumped recently but its newest *message* predates the window.
                continue
            if until_iso and when >= until_iso and until is not None:
                # Last activity is past the bound — probe for the newest in-window message.
                when = _newest_before(settings, skype_token, thread_id, until)
                if not when or (since_iso and when < since_iso):
                    continue
            label = await directory.label(thread_id)
            if jsonl:
                record = {"id": thread_id, "last_activity": when, "label": label}
                await emit(json.dumps(record, ensure_ascii=False) + "\n")
            else:
                await emit(f"{when[:19]}Z  {thread_id}  {label}\n")
            shown += 1
            if limit and shown >= limit:
                return
