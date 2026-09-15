"""Body decode, rendering, reactions, and delivery routing."""

import base64
import gzip
import json
from typing import Any

from miniteams.directory import Directory
from miniteams.messages import (
    _decode_body,
    _print_reactions,
    emit_raw_delivery,
    emit_raw_named,
    event_to_record,
    handle_delivery,
    resource_to_record,
    strip_html,
    thread_of,
)

NOTES_LINK = "https://h/v1/users/ME/conversations/48:notes"


def gzip_b64(obj: Any) -> str:
    return base64.b64encode(gzip.compress(json.dumps(obj).encode())).decode()


def b64(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def test_decode_plain_body() -> None:
    req = {"body": json.dumps({"type": "EventMessage"})}
    assert _decode_body(req) == {"type": "EventMessage"}


def test_decode_gzip_header() -> None:
    inner = {"type": "EventMessage", "resource": {"x": 1}}
    req = {"headers": {"X-Microsoft-Skype-Content-Encoding": "gzip"}, "body": gzip_b64(inner)}
    assert _decode_body(req) == inner


def test_decode_cp_member_is_gzipped() -> None:
    payload = {"type": "EventMessage", "resource": {"a": 2}}
    req = {"body": json.dumps({"cp": gzip_b64(payload)})}
    assert _decode_body(req) == payload


def test_decode_gp_member_is_plain_base64() -> None:
    payload = {"type": "EventMessage", "resource": {"b": 3}}
    req = {"body": json.dumps({"gp": b64(payload)})}
    assert _decode_body(req) == payload


def test_decode_empty_body_returns_none() -> None:
    assert _decode_body({}) is None


def test_strip_html_unescapes_and_removes_tags() -> None:
    assert strip_html("<p>Hello &amp; <b>bye</b></p>") == "Hello & bye"


def test_thread_id_from_conversation_link() -> None:
    res = {"conversationLink": "https://h/v1/users/ME/conversations/19:abc@thread.v2/messages/7"}
    assert thread_of(res) == "19:abc@thread.v2"


def test_thread_id_falls_back_to_to() -> None:
    assert thread_of({"to": "48:notes"}) == "48:notes"


async def test_resource_to_record_surfaces_fields(directory: Directory) -> None:
    res = {
        "id": "1",
        "messagetype": "RichText/Html",
        "from": "8:orgid:guid",
        "imdisplayname": "Alice",
        "content": "<p>hi</p>",
        "composetime": "2026-01-01T00:00:00.000Z",
        "conversationLink": NOTES_LINK,
        "sequenceId": 9,
    }
    rec = await resource_to_record(res, directory)
    assert rec["text"] == "hi"
    assert rec["sender"] == "Alice"
    assert rec["thread_label"] == "Notes to self"
    assert rec["sequenceId"] == 9
    assert rec["raw"] is res


async def test_event_to_record_includes_delivery_envelope(directory: Directory) -> None:
    obj = {"type": "EventMessage", "resourceType": "NewMessage", "resource": {"from": "8:o:u"}}
    req = {"id": 42, "method": "POST", "url": "https://h/x/messaging", "headers": {"a": "b"}}
    rec = await event_to_record(obj, req, directory)
    assert rec["delivery"] == {
        "id": 42,
        "method": "POST",
        "url": "https://h/x/messaging",
        "headers": {"a": "b"},
    }
    assert rec["resourceType"] == "NewMessage"
    assert "message" in rec


async def test_print_reactions_added_then_removed(directory: Directory, capsys) -> None:
    res = {"id": "m1", "content": "<p>x</p>", "conversationLink": NOTES_LINK}
    await _print_reactions(res, [{"key": "like", "users": [{"mri": "8:orgid:u1"}]}], directory)
    out1 = capsys.readouterr().out
    assert "👍" in out1 and "reacted" in out1
    # Same message, user gone → removal.
    await _print_reactions(res, [{"key": "like", "users": []}], directory)
    assert "unreacted" in capsys.readouterr().out


async def test_handle_delivery_prints_new_message(directory: Directory, capsys) -> None:
    body = {
        "type": "EventMessage",
        "resourceType": "NewMessage",
        "resource": {
            "messagetype": "Text",
            "from": "8:o:u",
            "imdisplayname": "Al",
            "content": "hey",
            "conversationLink": NOTES_LINK,
            "composetime": "t",
        },
    }
    await handle_delivery({"url": "https://h/x/messaging", "body": json.dumps(body)}, directory)
    assert "hey" in capsys.readouterr().out


async def test_handle_delivery_ignores_non_messaging(directory: Directory, capsys) -> None:
    await handle_delivery({"url": "https://h/x/unifiedPresenceService", "body": "{}"}, directory)
    assert capsys.readouterr().out == ""


async def test_emit_raw_delivery_includes_all_endpoints(capsys) -> None:
    # a presence event (not /messaging) — raw mode emits it anyway, decoded
    req = {
        "id": 7,
        "method": "POST",
        "url": "https://h/x/unifiedPresenceService",
        "headers": {"k": "v"},
        "body": json.dumps({"availability": "Away"}),
    }
    await emit_raw_delivery(req)
    rec = json.loads(capsys.readouterr().out)
    assert rec["kind"] == "delivery"
    assert rec["url"].endswith("/unifiedPresenceService")
    assert rec["body"] == {"availability": "Away"}


async def test_emit_raw_named(capsys) -> None:
    await emit_raw_named('{"name":"trouter.connected","args":[{"ttl":1}]}')
    rec = json.loads(capsys.readouterr().out)
    assert rec == {"kind": "named", "event": {"name": "trouter.connected", "args": [{"ttl": 1}]}}


async def test_typing_shown_only_when_enabled(directory: Directory, capsys) -> None:
    body = {
        "type": "EventMessage",
        "resourceType": "NewMessage",
        "resource": {
            "messagetype": "Control/Typing",
            "from": "8:o:u",
            "imdisplayname": "Al",
            "conversationLink": NOTES_LINK,
        },
    }
    req = {"url": "https://h/x/messaging", "body": json.dumps(body)}
    await handle_delivery(req, directory)  # typing=False → silent
    assert capsys.readouterr().out == ""
    await handle_delivery(req, directory, typing=True)
    out = capsys.readouterr().out
    assert "✍" in out and "is typing" in out


async def test_handle_delivery_edit_marks_edited(directory: Directory, capsys) -> None:
    body = {
        "type": "EventMessage",
        "resourceType": "MessageUpdate",
        "resource": {
            "messagetype": "Text",
            "from": "8:o:u",
            "imdisplayname": "Al",
            "content": "fixed",
            "conversationLink": NOTES_LINK,
            "skypeeditedid": "1",
        },
    }
    await handle_delivery({"url": "https://h/x/messaging", "body": json.dumps(body)}, directory)
    out = capsys.readouterr().out
    assert "✏" in out and "fixed" in out


async def test_handle_delivery_delete_renders(directory: Directory, capsys) -> None:
    body = {
        "type": "EventMessage",
        "resourceType": "MessageUpdate",
        "resource": {
            "from": "8:o:u",
            "imdisplayname": "Al",
            "conversationLink": NOTES_LINK,
            "properties": {"deletetime": 123},
        },
    }
    await handle_delivery({"url": "https://h/x/messaging", "body": json.dumps(body)}, directory)
    assert "🗑" in capsys.readouterr().out


async def test_handle_delivery_message_update_without_emotions_is_silent(
    directory: Directory, capsys
) -> None:
    body = {"type": "EventMessage", "resourceType": "MessageUpdate", "resource": {"id": "1"}}
    await handle_delivery({"url": "https://h/x/messaging", "body": json.dumps(body)}, directory)
    assert capsys.readouterr().out == ""
