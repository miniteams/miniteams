"""Inbound delivery routing, body decode, and chat-message printing.

Trouter delivers each event as a pseudo-HTTP request whose `body` is a stringified (and often
gzip+base64-wrapped) JSON. Chat messages arrive on the `/messaging` endpoint as `EventMessage`
notifications; everything else (presence, calls) is ignored for the MVP dump.
"""

import base64
import gzip
import html
import json
import re
from typing import Any

import structlog

from . import attachments
from ._io import emit
from .directory import Directory

log = structlog.get_logger()

# messagetypes worth printing — skip Control/* (typing indicators, read receipts, …).
_TEXT_TYPES = {"RichText/Html", "Text"}
_MEDIA_TYPES = {
    "RichText/Media_GenericFile",
    "RichText/UriObject",
    "RichText/Media_Card",
    "RichText/Media_CallRecording",
    "RichText/Media_LocalRecording",
}
_PRINTABLE = _TEXT_TYPES | _MEDIA_TYPES
_TAG_RE = re.compile(r"<[^>]+>")
_THREAD_RE = re.compile(r"/conversations/([^/]+)")

# Teams reaction keys → emoji; unknown keys fall back to ":key:".
EMOJI = {
    "like": "👍",
    "heart": "❤️",
    "laugh": "😆",
    "surprised": "😮",
    "sad": "😢",
    "angry": "😡",
    "yes": "✅",
    "no": "❌",
}


def _gunzip_b64(data: str) -> str:
    return gzip.decompress(base64.b64decode(data)).decode("utf-8")


def _decode_body(req: dict[str, Any]) -> dict[str, Any] | None:
    """Unwrap the three documented encodings into the real payload object."""
    body = req.get("body")
    if not body:
        return None
    headers = {k.lower(): v for k, v in (req.get("headers") or {}).items()}
    if headers.get("x-microsoft-skype-content-encoding") == "gzip":
        body = _gunzip_b64(body)
    obj = json.loads(body)
    if isinstance(obj, dict):
        if "cp" in obj:  # base64 → gunzip → real payload
            obj = json.loads(_gunzip_b64(obj["cp"]))
        elif "gp" in obj:  # base64 → parse directly
            obj = json.loads(base64.b64decode(obj["gp"]).decode("utf-8"))
    return obj if isinstance(obj, dict) else None


def strip_html(content: str) -> str:
    return html.unescape(_TAG_RE.sub("", content)).strip()


def thread_of(resource: dict[str, Any]) -> str:
    link = resource.get("conversationLink") or ""
    match = _THREAD_RE.search(link)
    if match:
        return match.group(1)
    return str(resource.get("to", ""))


async def _print_message(resource: dict[str, Any], directory: Directory, tag: str = "") -> None:
    msgtype = resource.get("messagetype", "")
    if msgtype not in _PRINTABLE:
        log.debug("skipped_message", messagetype=msgtype)
        return
    sender_mri = resource.get("from", "")
    sender = resource.get("imdisplayname")
    directory.note_name(sender_mri, sender)  # feed the cache from free message metadata
    sender = sender or await directory.display(sender_mri)
    when = resource.get("composetime") or resource.get("originalarrivaltime") or ""
    label = await directory.label(thread_of(resource))
    content = resource.get("content", "")

    notes = await attachments.process(
        content,
        msgtype,
        directory.skype_token,
        directory.settings.media_dir,
        directory.settings.download_media,
    )
    text = strip_html(content) if msgtype == "RichText/Html" else content if msgtype == "Text" else ""
    suffix = (" " + " ".join(notes)) if notes else ""
    await emit(f"[{when}] ({label}) {tag}{sender}: {text}{suffix}".rstrip() + "\n")


async def _print_deleted(resource: dict[str, Any], directory: Directory) -> None:
    when = (resource.get("properties") or {}).get("deletetime") or resource.get("composetime") or ""
    label = await directory.label(thread_of(resource))
    sender = await directory.display(resource.get("from", ""))
    await emit(f"[{when}] ({label}) 🗑 {sender} deleted a message\n")


async def print_resource(resource: dict[str, Any], directory: Directory) -> None:
    """Render one message resource — shared by the live stream and the history dump."""
    await _print_message(resource, directory)


async def emit_raw_delivery(req: dict[str, Any]) -> None:
    """Emit one inbound delivery verbatim as NDJSON — every endpoint, decoded, no filtering."""
    try:
        body: Any = _decode_body(req)
    except Exception as exc:  # noqa: BLE001 — keep the firehose flowing on a bad frame
        body = {"_decode_error": str(exc), "_raw": req.get("body")}
    record = {
        "kind": "delivery",
        "id": req.get("id"),
        "method": req.get("method"),
        "url": req.get("url"),
        "headers": req.get("headers"),
        "body": body,
    }
    await emit(json.dumps(record, ensure_ascii=False, default=str) + "\n")


async def emit_raw_named(data: str) -> None:
    """Emit a named Socket.IO event (trouter.connected, message_loss, …) as NDJSON."""
    try:
        payload: Any = json.loads(data)
    except json.JSONDecodeError:
        payload = data
    await emit(json.dumps({"kind": "named", "event": payload}, ensure_ascii=False, default=str) + "\n")


async def event_to_record(obj: dict[str, Any], req: dict[str, Any], directory: Directory) -> dict[str, Any]:
    """Full-detail JSONL record for one live stream event (max info, no media download)."""
    resource = obj.get("resource") or {}
    record: dict[str, Any] = {
        "type": obj.get("type"),
        "resourceType": obj.get("resourceType"),
        "time": obj.get("time"),
        # Trouter delivery envelope — the transport-level "tech stuff".
        "delivery": {
            "id": req.get("id"),
            "method": req.get("method"),
            "url": req.get("url"),
            "headers": req.get("headers"),
        },
    }
    if resource:
        record["message"] = await resource_to_record(resource, directory)
    else:
        record["raw"] = obj
    return record


async def resource_to_record(resource: dict[str, Any], directory: Directory) -> dict[str, Any]:
    """Full-detail structured record for JSONL output — keeps the raw resource verbatim."""
    msgtype = resource.get("messagetype", "")
    sender_mri = resource.get("from", "")
    directory.note_name(sender_mri, resource.get("imdisplayname"))
    content = resource.get("content", "")
    props = resource.get("properties") or {}
    thread_id = thread_of(resource)
    text = strip_html(content) if msgtype == "RichText/Html" else content if msgtype == "Text" else ""
    return {
        "id": resource.get("id"),
        "time": resource.get("composetime") or resource.get("originalarrivaltime"),
        "thread_id": thread_id,
        "thread_label": await directory.label(thread_id),
        "sender_mri": sender_mri,
        "sender": resource.get("imdisplayname") or await directory.display(sender_mri),
        "messagetype": msgtype,
        "text": text,
        "content_raw": content,
        "attachments": attachments.extract(content, msgtype),
        "reactions": props.get("emotions"),
        # Surfaced technical identifiers (also present in `raw`, promoted for convenience).
        "clientmessageid": resource.get("clientmessageid"),
        "sequenceId": resource.get("sequenceId"),
        "version": resource.get("version"),
        "conversationid": resource.get("conversationid"),
        "conversation_link": resource.get("conversationLink"),
        "etag": resource.get("etag"),
        "skypeeditedid": resource.get("skypeeditedid"),
        "deletetime": props.get("deletetime"),
        "properties": props,
        "raw": resource,  # full server object, nothing dropped
    }


async def _print_reactions(resource: dict[str, Any], emotions: list[Any], directory: Directory) -> None:
    msg_id = str(resource.get("id") or resource.get("clientmessageid") or "")
    when = resource.get("composetime") or resource.get("originalarrivaltime") or ""
    label = await directory.label(thread_of(resource))
    raw = resource.get("content") or ""
    snippet = strip_html(raw)[:40] if raw else ""
    ctx = f' to "{snippet}"' if snippet else ""
    for emotion in emotions:
        key = emotion.get("key", "?")
        emoji = EMOJI.get(key, f":{key}:")
        users = [str(u.get("mri", "")) for u in (emotion.get("users") or [])]
        added, removed = directory.reaction_diff(msg_id, key, users)
        for mri in added:
            await emit(f"[{when}] ({label}) ↳ {emoji} {await directory.display(mri)} reacted{ctx}\n")
        for mri in removed:
            await emit(f"[{when}] ({label}) ↳ {emoji}✖ {await directory.display(mri)} unreacted{ctx}\n")


async def _print_typing(resource: dict[str, Any], directory: Directory, started: bool) -> None:
    when = resource.get("composetime") or resource.get("originalarrivaltime") or ""
    label = await directory.label(thread_of(resource))
    who = await directory.display(resource.get("from", ""))
    verb = "is typing…" if started else "stopped typing"
    await emit(f"[{when}] ({label}) ✍ {who} {verb}\n")


def decode_event(req: dict[str, Any]) -> dict[str, Any] | None:
    """The `EventMessage` carried by a `/messaging` delivery, or None for anything else."""
    url = req.get("url", "")
    endpoint = url.rsplit("/", 1)[-1]
    if endpoint != "messaging":  # presence / call signaling — ignore for MVP
        log.debug("delivery_ignored", endpoint=endpoint)
        return None
    try:
        obj = _decode_body(req)
    except Exception as exc:  # noqa: BLE001 — never let one bad frame kill the stream
        log.warning("decode_failed", error=str(exc))
        return None
    if not obj or obj.get("type") != "EventMessage":
        log.debug("non_event", type=obj.get("type") if obj else None)
        return None
    return obj


async def handle_delivery(
    req: dict[str, Any], directory: Directory, jsonl: bool = False, typing: bool = False
) -> None:
    obj = decode_event(req)
    if obj is None:
        return

    if jsonl:
        record = await event_to_record(obj, req, directory)
        await emit(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return

    resource = obj.get("resource") or {}
    resource_type = obj.get("resourceType")
    if resource_type == "NewMessage":
        msgtype = resource.get("messagetype", "")
        if msgtype in ("Control/Typing", "Control/ClearTyping"):
            if typing:
                await _print_typing(resource, directory, started=msgtype == "Control/Typing")
            return
        await _print_message(resource, directory)
    elif resource_type == "MessageUpdate":
        # MessageUpdate carries reactions (emotions), deletions (deletetime), or edits.
        props = resource.get("properties") or {}
        if props.get("emotions") is not None:
            await _print_reactions(resource, props["emotions"], directory)
        elif props.get("deletetime"):
            await _print_deleted(resource, directory)
        elif resource.get("skypeeditedid") or props.get("edittime"):
            await _print_message(resource, directory, tag="✏ ")
        else:
            log.debug("message_update", id=resource.get("id"))
    else:
        log.debug("ignored_resource", resource_type=resource_type)
