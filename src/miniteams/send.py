"""Outbound message send (purple-teams teams_send_message).

A plain REST POST to the chat service with the skype token — NOT over the Trouter socket
(that channel is receive-only). The sent message echoes back over the live stream carrying the
same `clientmessageid`, so a concurrent `stream` will show it too.
"""

import html
import time
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from .config import Settings

log = structlog.get_logger()

# "Notes to self" — the write-safe default target.
NOTES_THREAD = "48:notes"


def send_message(
    settings: Settings, skype_token: str, thread_id: str, text: str, display_name: str
) -> dict[str, Any]:
    url = f"https://{settings.contacts_host}/v1/users/ME/conversations/{quote(thread_id, safe='')}/messages"
    # JS-style epoch milliseconds; also the de-dup key for the self-echo over Trouter.
    client_message_id = str(int(time.time() * 1000))
    body = {
        "clientmessageid": client_message_id,
        # messagetype is RichText/Html, so content is HTML — escape plain text and keep newlines.
        "content": html.escape(text).replace("\n", "<br>"),
        "messagetype": "RichText/Html",
        "contenttype": "text",
        "imdisplayname": display_name,
    }
    headers = {
        "X-Skypetoken": skype_token,
        "User-Agent": settings.user_agent,
        "Accept": "application/json; ver=1.0;",
        "BehaviorOverride": "redirectAs404",
        "Origin": "https://teams.microsoft.com",
        "Referer": "https://teams.microsoft.com/",
    }
    resp = httpx.post(url, headers=headers, json=body, timeout=30.0)
    resp.raise_for_status()
    data = resp.json() if resp.content else {}
    # The service can return 2xx with an error envelope.
    if isinstance(data, dict) and data.get("errorCode"):
        raise RuntimeError(f"send rejected: {data.get('errorCode')}: {data.get('message')}")
    log.info("message_sent", thread=thread_id, clientmessageid=client_message_id, status=resp.status_code)
    return {"clientmessageid": client_message_id, "status": resp.status_code}
