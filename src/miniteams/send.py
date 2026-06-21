"""Outbound message send + edit (chat-service REST, skype token).

Plain REST against the chat service — NOT the Trouter socket (that channel is receive-only).
A sent/edited message echoes back over the live stream (same `clientmessageid`), so a concurrent
`stream` shows it too.

`send_message` mirrors purple-teams `teams_send_message`. `edit_message` is reconstructed from
the inbound `MessageUpdate` + `skypeeditedid` wire format (purple-teams only *consumes* edits) and
the SkPy client: PUT the message id with `skypeeditedid` set to it — confirmed against live Teams.
"""

import html
import re
import time
from typing import Any
from urllib.parse import quote, unquote

import httpx
import structlog

from .config import Settings

log = structlog.get_logger()

# "Notes to self" — the write-safe default target.
NOTES_THREAD = "48:notes"

# Teams deep link: .../l/message/<thread>/<messageId>?context=...
_MESSAGE_LINK_RE = re.compile(r"/l/message/([^/]+)/([^/?#]+)")


def parse_message_link(value: str) -> tuple[str, str] | None:
    """Extract (thread_id, message_id) from a Teams /l/message/<thread>/<id> deep link."""
    match = _MESSAGE_LINK_RE.search(value)
    if not match:
        return None
    return unquote(match.group(1)), unquote(match.group(2))


def _content(text: str, is_html: bool) -> str:
    # messagetype is RichText/Html; plain text is escaped (newlines → <br>), --html sent verbatim.
    return text if is_html else html.escape(text).replace("\n", "<br>")


def _headers(skype_token: str, settings: Settings) -> dict[str, str]:
    return {
        "X-Skypetoken": skype_token,
        "User-Agent": settings.user_agent,
        "Accept": "application/json; ver=1.0;",
        "BehaviorOverride": "redirectAs404",
        "Origin": "https://teams.microsoft.com",
        "Referer": "https://teams.microsoft.com/",
    }


def _check(resp: httpx.Response, action: str) -> dict[str, Any]:
    resp.raise_for_status()
    data = resp.json() if resp.content else {}
    if isinstance(data, dict) and data.get("errorCode"):  # 2xx with an error envelope
        raise RuntimeError(f"{action} rejected: {data.get('errorCode')}: {data.get('message')}")
    return {"status": resp.status_code}


def send_message(
    settings: Settings,
    skype_token: str,
    thread_id: str,
    text: str,
    display_name: str,
    *,
    is_html: bool = False,
) -> dict[str, Any]:
    base = f"https://{settings.contacts_host}/v1/users/ME/conversations/{quote(thread_id, safe='')}"
    # JS-style epoch milliseconds; also the de-dup key for the self-echo over Trouter.
    client_message_id = str(int(time.time() * 1000))
    body = {
        "clientmessageid": client_message_id,
        "content": _content(text, is_html),
        "messagetype": "RichText/Html",
        "contenttype": "text",
        "imdisplayname": display_name,
    }
    resp = httpx.post(f"{base}/messages", headers=_headers(skype_token, settings), json=body, timeout=30.0)
    result = _check(resp, "send")
    log.info("message_sent", thread=thread_id, clientmessageid=client_message_id, status=result["status"])
    return {"clientmessageid": client_message_id, **result}


def edit_message(
    settings: Settings,
    skype_token: str,
    thread_id: str,
    message_id: str,
    text: str,
    *,
    is_html: bool = False,
) -> dict[str, Any]:
    url = (
        f"https://{settings.contacts_host}/v1/users/ME/conversations"
        f"/{quote(thread_id, safe='')}/messages/{quote(message_id, safe='')}"
    )
    body = {
        "content": _content(text, is_html),
        "messagetype": "RichText/Html",
        "contenttype": "text",
        "skypeeditedid": message_id,
    }
    result = _check(httpx.put(url, headers=_headers(skype_token, settings), json=body, timeout=30.0), "edit")
    log.info("message_edited", thread=thread_id, message_id=message_id, status=result["status"])
    return result
