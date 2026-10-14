"""Outbound message send, edit, reactions and typing indicator (chat-service REST, skype token).

Plain REST against the chat service — NOT the Trouter socket (that channel is receive-only).
A sent/edited message echoes back over the live stream (same `clientmessageid`), a reaction as a
`MessageUpdate` of its message, so a concurrent `stream` shows both.

`send_message` mirrors purple-teams `teams_send_message`, `send_typing` its
`teams_conv_send_typing_to_channel`. `edit_message` is reconstructed from
the inbound `MessageUpdate` + `skypeeditedid` wire format (purple-teams only *consumes* edits) and
the SkPy client: PUT the message id with `skypeeditedid` set to it — confirmed against live Teams.
`delete_message` was captured from the Teams web client.
"""

import hashlib
import html
import re
import time
from collections import Counter
from typing import Any, NoReturn
from urllib.parse import quote, unquote

import httpx
import structlog

from .config import Settings
from .images import MAX_OBJECT_ID, OBJECT_ID_RE, check_batch, images_html

log = structlog.get_logger()

# Kept open: a fresh TLS handshake per send doubles its latency. httpx clients are thread-safe.
_CLIENT = httpx.Client(timeout=30.0)

MAX_HTML_BYTES = 28 * 1024  # Teams' per-message limit, on the posted HTML

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


def too_long(text: str, *, is_html: bool = False) -> bool:
    """Over Teams' limit once posted: the cap is on the HTML bytes, so escaping counts."""
    return len(message_html(text, is_html).encode()) > MAX_HTML_BYTES


def _headers(skype_token: str, settings: Settings) -> dict[str, str]:
    return {
        "X-Skypetoken": skype_token,
        "User-Agent": settings.user_agent,
        "Accept": "application/json; ver=1.0;",
        "BehaviorOverride": "redirectAs404",
        "Origin": "https://teams.microsoft.com",
        "Referer": "https://teams.microsoft.com/",
    }


def _fail(resp: httpx.Response, action: str) -> NoReturn:
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


def _check(resp: httpx.Response, action: str) -> dict[str, Any]:
    if resp.is_error:
        _fail(resp, action)
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
    properties: dict[str, Any] | None = None,
    client_message_id: str | None = None,
) -> dict[str, Any]:
    base = f"https://{settings.contacts_host}/v1/users/ME/conversations/{quote(thread_id, safe='')}"
    # JS-style epoch milliseconds; also the de-dup key for the self-echo over Trouter.
    client_message_id = client_message_id or str(int(time.time() * 1000))
    body: dict[str, Any] = {
        "clientmessageid": client_message_id,
        "content": message_html(text, is_html),
        "messagetype": "RichText/Html",
        "contenttype": "text",
        "imdisplayname": display_name,
    }
    if properties is not None:
        body["properties"] = properties
    resp = _CLIENT.post(f"{base}/messages", headers=_headers(skype_token, settings), json=body)
    result = _check(resp, "send")
    log.info("message_sent", thread=thread_id, clientmessageid=client_message_id, status=result["status"])
    return {"clientmessageid": client_message_id, **result}


def upload_image(settings: Settings, skype_token: str, thread_id: str, data: bytes, filename: str) -> str:
    """Create an AMS object readable by `thread_id`, upload `data` into it, return its id
    (purple-teams `teams_conversation_send_image`, on the native client's asyncgw host)."""
    base = f"https://{settings.asyncgw_host}/v1/objects"
    headers = {
        "Authorization": f"skype_token {skype_token}",
        "User-Agent": settings.user_agent,
        "Origin": "https://teams.microsoft.com",
        "Referer": "https://teams.microsoft.com/",
    }
    body = {
        "type": "pish/image",
        "permissions": {thread_id: ["read"]},
        "filename": filename,
        "sharingMode": "Inline",
    }
    resp = _CLIENT.post(
        f"{base}/",
        headers={**headers, "Accept": "application/json", "x-ms-client-version": settings.clientinfo_version},
        json=body,
    )
    if not resp.is_success:  # a 3xx is not an object either: redirects are not followed
        _fail(resp, "image create")
    try:
        created = resp.json()
    except ValueError:
        created = None
    object_id = created.get("id") if isinstance(created, dict) else None
    if not isinstance(object_id, str) or not OBJECT_ID_RE.fullmatch(object_id):
        raise RuntimeError("image create returned no usable object id")
    resp = _CLIENT.put(
        f"{base}/{object_id}/content/imgpsh",
        headers={**headers, "Accept": "*/*", "Content-Type": "application/octet-stream"},
        content=data,
    )
    if not resp.is_success:  # the success body is not JSON, _check would choke on it
        _fail(resp, "image upload")
    log.info("image_uploaded", thread=thread_id, object_id=object_id, size=len(data))
    return object_id


def attach_images(
    settings: Settings,
    skype_token: str,
    thread_id: str,
    body: str,
    images: list[tuple[str, bytes]],
    cache: dict[str, list[str]] | None = None,
) -> str:
    """`body` (HTML) followed by `images` ((filename, bytes), in order) in one paragraph, uploaded.

    Every image is validated before the first upload, so a bad one uploads nothing. `cache` (sha256 →
    AMS object ids, one per occurrence) is read and filled: a retry reuses the objects a failed
    attempt already made, and the same picture twice in one message still gets two objects."""
    sniffed = check_batch([data for _, data in images])
    # Longest possible id as a stand-in: the real block can only be shorter, so nothing uploads in vain.
    widest = images_html(settings.asyncgw_host, [("0" * MAX_OBJECT_ID, image) for image in sniffed])
    if too_long(body + widest, is_html=True):
        raise ValueError("message too long for Teams once the images are added")
    cache = {} if cache is None else cache
    uploaded, seen = [], Counter[str]()
    for (filename, data), image in zip(images, sniffed, strict=True):
        digest = hashlib.sha256(data).hexdigest()
        objects, nth = cache.setdefault(digest, []), seen[digest]
        seen[digest] += 1
        if len(objects) == nth:
            objects.append(upload_image(settings, skype_token, thread_id, data, filename))
        uploaded.append((objects[nth], image))
    return body + images_html(settings.asyncgw_host, uploaded)


def send_images(
    settings: Settings,
    skype_token: str,
    thread_id: str,
    text: str,
    images: list[tuple[str, bytes]],
    display_name: str,
    *,
    is_html: bool = False,
    client_message_id: str | None = None,
) -> dict[str, Any]:
    """One message: `text` first, then `images` ((filename, bytes), in order) in one paragraph."""
    body = message_html(text, is_html)
    if body and not is_html:
        body = f"<p>{body}</p>"
    content = attach_images(settings, skype_token, thread_id, body, images)
    return send_message(
        settings,
        skype_token,
        thread_id,
        content,
        display_name,
        is_html=True,
        client_message_id=client_message_id,
    )


def send_typing(settings: Settings, skype_token: str, thread_id: str) -> dict[str, Any]:
    """Show "… is typing" to the chat's other members; it expires on their side unless re-sent."""
    url = f"https://{settings.contacts_host}/v1/users/ME/conversations/{quote(thread_id, safe='')}/messages"
    # No `Control/ClearTyping` counterpart: purple-teams dropped it, the indicator expires by itself.
    body = {"messagetype": "Control/Typing", "contenttype": "Application/Message", "content": ""}
    result = _check(_CLIENT.post(url, headers=_headers(skype_token, settings), json=body), "typing")
    log.debug("typing_sent", thread=thread_id, status=result["status"])
    return result


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


def delete_message(settings: Settings, skype_token: str, thread_id: str, message_id: str) -> dict[str, Any]:
    """Delete your message: `DELETE …/messages/<id>`, no query, no body (captured from Teams web,
    answers 200). The message stays, with `properties.deletetime` set and an empty content."""
    url = (
        f"https://{settings.contacts_host}/v1/users/ME/conversations"
        f"/{quote(thread_id, safe='')}/messages/{quote(message_id, safe='')}"
    )
    result = _check(_CLIENT.delete(url, headers=_headers(skype_token, settings)), "delete")
    log.info("message_deleted", thread=thread_id, message_id=message_id, status=result["status"])
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
