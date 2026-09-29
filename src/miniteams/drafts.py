"""Teams' drafts store (`48:drafts`): scheduled and parked drafts, read side (spec 007).

Drafts are the messages of a pseudo conversation, yet the history call refuses its id
(400 `Invalid threadId`): they are listed by `GET /v1/users/ME/drafts`, newest first.
"""

from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog

from .config import Settings
from .http import get_with_retry

log = structlog.get_logger()

_PAGE_SIZE = 200  # the service refuses anything larger
_MAX_PAGES = 50  # safety cap on the walk


def iter_draft_pages(settings: Settings, skype_token: str) -> Iterator[list[dict[str, Any]]]:
    """Yield pages of drafts, sent and cancelled ones included, until the store is exhausted."""
    base = f"https://{settings.contacts_host}/v1/users/ME/drafts"
    headers = {
        "X-Skypetoken": skype_token,
        "User-Agent": settings.user_agent,
        "Accept": "application/json; ver=1.0;",
    }
    url = f"{base}?pageSize={_PAGE_SIZE}"
    seen = {url}
    with httpx.Client(timeout=30.0, headers=headers) as client:
        for page in range(_MAX_PAGES):
            data = get_with_retry(client, url).json()
            drafts = data.get("drafts") or []
            log.debug("drafts_page", page=page + 1, count=len(drafts))
            if not drafts:
                return
            yield drafts
            # `backwardLink` names the conversation path, which answers 400: only its query is usable.
            query = urlsplit(str((data.get("_metadata") or {}).get("backwardLink") or "")).query
            url = f"{base}?{query}"
            if not query or url in seen:
                return
            seen.add(url)
        log.warning("drafts_truncated", pages=_MAX_PAGES)
