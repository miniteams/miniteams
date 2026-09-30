"""Teams' drafts store (`48:drafts`): scheduled and parked drafts. Read side (spec 007), write side
(spec 008).

Drafts are the messages of a pseudo conversation, yet the history call refuses its id
(400 `Invalid threadId`): they are listed by `GET /v1/users/ME/drafts`, newest first.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit

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


# --- write side (spec 008): the drafts store behind the chat-service proxy ---

# The regional host lists drafts but answers writes with 404 or 500; the proxy takes an ic3 bearer.
PROXY_BASE = "https://teams.cloud.microsoft/api/chatsvc"
# The service's own bounds, read from its refusals (docs/scheduled-send-wire.md). The Teams
# client's date picker stops at 120 days; beyond that it can still delete a draft, not move it.
SCHEDULE_MIN_AHEAD = timedelta(seconds=5)
SCHEDULE_MAX_AHEAD = timedelta(days=125)


def notes_thread_id(settings: Settings, skype_token: str) -> str:
    """The thread behind `48:notes`: the drafts store refuses the alias and takes the thread."""
    url = f"https://{settings.contacts_host}/v1/users/ME/conversations/48%3Anotes?view=msnp24Equivalent"
    headers = {"X-Skypetoken": skype_token, "User-Agent": settings.user_agent, "Accept": "application/json"}
    with httpx.Client(timeout=30.0, headers=headers) as client:
        data = get_with_retry(client, url).json()
    return str((data.get("threadProperties") or {}).get("originalThreadId") or "")


def draft_state(draft: dict[str, Any], now_ms: int) -> str:
    """`cancelled` (body emptied), `parked` (no send time), `pending` or `sent`."""
    if not str(draft.get("content") or "").strip():
        return "cancelled"
    if draft.get("draftType") == "RegularDraft":
        return "parked"
    return "pending" if int((draft.get("draftDetails") or {}).get("sendAt") or 0) > now_ms else "sent"


def unused_client_id(drafts: list[dict[str, Any]], wanted: str) -> str:
    """A client id no draft wears: a cancelled draft keeps its id in `skypeeditedid`, and a new
    draft reusing it is stored and sent yet drawn by no client, so nobody can cancel it by hand."""
    taken = {
        str(v)
        for d in drafts
        for v in (
            d.get("clientmessageid"),
            d.get("skypeeditedid"),
            (d.get("properties") or {}).get("draftId"),
        )
        if v
    }
    candidate = wanted
    for nonce in range(1, 10_000):
        if candidate not in taken:
            return candidate
        candidate = f"{wanted}{nonce}"
    raise RuntimeError("no free client id")


def draft_payload(
    inner_thread_id: str, message: dict[str, Any], send_at_ms: int, draft_id: str = "-1"
) -> dict[str, Any]:
    """The store wants the message under `message`, with the draft fields repeated on top:
    `sendAt` as epoch ms outside and ISO inside. `conversationLink` is never dereferenced."""
    thread_id = inner_thread_id.split(";messageid=")[0]
    return {
        "draftDetails": {"sendAt": str(send_at_ms)},
        "draftType": "ScheduledDraft",
        "innerThreadId": inner_thread_id,
        "message": {
            **message,
            "id": draft_id,
            "type": "Message",
            "conversationid": thread_id,
            "conversationLink": f"blah/{thread_id}",
            "draftDetails": {
                "sendAt": datetime.fromtimestamp(send_at_ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            },
            "threadtype": "streamofdrafts",
            "innerThreadId": inner_thread_id,
        },
    }


def new_draft_message(
    content_html: str, mri: str, display_name: str, client_message_id: str
) -> dict[str, Any]:
    """A fresh draft body. The service rejects a partial property bag, so the whole set goes."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "from": mri,
        "fromUserId": mri,
        "composetime": now,
        "originalarrivaltime": now,
        "content": content_html,
        "messagetype": "RichText/Html",
        "contenttype": "Text",
        "imdisplayname": display_name,
        "clientmessageid": client_message_id,
        "callId": "",
        "state": 0,
        "version": "0",
        "amsreferences": [],
        "properties": {
            "importance": "",
            "subject": "",
            "title": "",
            "cards": "[]",
            "links": "[]",
            "mentions": "[]",
            "onbehalfof": None,
            "files": "[]",
            "policyViolation": None,
            "formatVariant": "TEAMS",
            # The clients pair the row with its bubble on this: apart, the draft is drawn as sent.
            "draftId": client_message_id,
        },
        "crossPostChannels": [],
    }


class DraftsStore:
    """Writes to the drafts store through the proxy. One client, one bearer, for one server."""

    def __init__(self, ic3_token: str, region: str) -> None:
        self.base = f"{PROXY_BASE}/{region or 'emea'}/v1/users/ME/drafts"
        self._client = httpx.Client(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {ic3_token}",
                "BehaviorOverride": "redirectAs404",
                "x-ms-migration": "True",
                "Accept": "application/json",
            },
        )

    def list(self) -> list[dict[str, Any]]:
        resp = self._client.get(f"{self.base}?pageSize={_PAGE_SIZE}")
        resp.raise_for_status()
        return list(resp.json().get("drafts") or [])

    def get(self, draft_id: str) -> dict[str, Any] | None:
        resp = self._client.get(f"{self.base}/{quote(draft_id, safe='')}")
        # An id that never was a draft answers 403 MessageIdNotInAllowedRange, not 404.
        if resp.status_code in (403, 404):
            return None
        resp.raise_for_status()
        return dict(resp.json())

    def create(self, payload: dict[str, Any]) -> str:
        resp = self._client.post(self.base, json=payload)
        resp.raise_for_status()
        draft_id = str(resp.json().get("OriginalArrivalTime") or "")
        log.info("draft_scheduled", draft_id=draft_id, thread=payload.get("innerThreadId"))
        return draft_id

    def update(self, draft_id: str, payload: dict[str, Any]) -> None:
        resp = self._client.put(f"{self.base}/{quote(draft_id, safe='')}", json=payload)
        resp.raise_for_status()
        log.info("draft_updated", draft_id=draft_id)

    def cancel(self, draft_id: str) -> None:
        resp = self._client.delete(f"{self.base}/{quote(draft_id, safe='')}")
        resp.raise_for_status()
        log.info("draft_cancelled", draft_id=draft_id)
