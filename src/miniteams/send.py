"""Outbound message send, edit and reactions (chat-service REST, skype token).

Plain REST against the chat service — NOT the Trouter socket (that channel is receive-only).
A sent/edited message echoes back over the live stream (same `clientmessageid`), a reaction as a
`MessageUpdate` of its message, so a concurrent `stream` shows both.

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

# Kept open: a fresh TLS handshake per send doubles its latency. httpx clients are thread-safe.
_CLIENT = httpx.Client(timeout=30.0)

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


def message_html(text: str, is_html: bool) -> str:
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
    if resp.is_error:
        # raise_for_status() drops the body, and the body holds the reason (MessageAlreadyDeleted…).
        try:
            reason = str(resp.json().get("message") or "")
        except ValueError, AttributeError:  # non-JSON (gateway page) or non-object body
            reason = ""
        raise httpx.HTTPStatusError(
            f"{action} failed: HTTP {resp.status_code} {reason or resp.reason_phrase}"[:300],
            request=resp.request,
            response=resp,
        )
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
        "content": message_html(text, is_html),
        "messagetype": "RichText/Html",
        "contenttype": "text",
        "imdisplayname": display_name,
    }
    resp = _CLIENT.post(f"{base}/messages", headers=_headers(skype_token, settings), json=body)
    result = _check(resp, "send")
    log.info("message_sent", thread=thread_id, clientmessageid=client_message_id, status=result["status"])
    return {"clientmessageid": client_message_id, **result}


def mark_read(settings: Settings, skype_token: str, thread_id: str, message_id: str) -> dict[str, Any]:
    """Move Teams' own read marker of a chat to `message_id` (every device sees the chat as read)."""
    url = (
        f"https://{settings.contacts_host}/v1/users/ME/conversations"
        f"/{quote(thread_id, safe='')}/properties?name=consumptionhorizon"
    )
    # "<id>;<now ms>;<client message id>" — the client id is unknown here; the message id passes.
    body = {"consumptionhorizon": f"{message_id};{int(time.time() * 1000)};{message_id}"}
    result = _check(_CLIENT.put(url, headers=_headers(skype_token, settings), json=body), "read")
    log.info("marked_read", thread=thread_id, message_id=message_id, status=result["status"])
    return result


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
        "content": message_html(text, is_html),
        "messagetype": "RichText/Html",
        "contenttype": "text",
        "skypeeditedid": message_id,
    }
    result = _check(_CLIENT.put(url, headers=_headers(skype_token, settings), json=body), "edit")
    log.info("message_edited", thread=thread_id, message_id=message_id, status=result["status"])
    return result


def react(
    settings: Settings,
    skype_token: str,
    thread_id: str,
    message_id: str,
    key: str,
    *,
    remove: bool = False,
) -> dict[str, Any]:
    """Add (PUT) or remove (DELETE) your `key` reaction (`like`, `heart`, `1f525_fire`…, case-sensitive)."""
    url = (
        f"https://{settings.contacts_host}/v1/users/ME/conversations/{quote(thread_id, safe='')}"
        f"/messages/{quote(message_id, safe='')}/properties?name=emotions"
    )
    body = {"emotions": {"key": key, "value": int(time.time() * 1000)}}
    # httpx.Client.delete() takes no body; the chat service needs one to know which key to drop.
    resp = _CLIENT.request(
        "DELETE" if remove else "PUT", url, headers=_headers(skype_token, settings), json=body
    )
    result = _check(resp, "unreact" if remove else "react")
    log.info("message_reacted", thread=thread_id, message_id=message_id, key=key, removed=remove)
    return result
