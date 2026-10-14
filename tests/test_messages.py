"""Body decode, rendering, reactions, and delivery routing."""

import base64
import gzip
import json
from typing import Any

from miniteams import messages as M
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


async def test_event_to_record_includes_delivery_envelope(directory: Directory, monkeypatch) -> None:
    async def offline(self: Directory, key: str) -> str:
        return key

    monkeypatch.setattr(Directory, "display", offline)
    monkeypatch.setattr(Directory, "label", offline)
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


async def test_print_reactions_escapes_controls_from_remote_keys(directory: Directory, capsys) -> None:
    res = {"id": "m1", "content": "<p>x</p>", "conversationLink": NOTES_LINK}
    await _print_reactions(res, [{"key": "x\x1b[31m\x07\r\x9b", "users": [{"mri": "8:orgid:u1"}]}], directory)
    out = capsys.readouterr().out
    assert ":x\\u001b[31m\\u0007\\u000d\\u009b:" in out and not any(c in out for c in "\x1b\x07\r\x9b")


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


def test_is_edited_reads_both_edit_marks() -> None:
    """M9: an edit is `skypeeditedid` (live echo) or `properties.edittime` (stored copy)."""
    assert M.is_edited({"skypeeditedid": "1"}) and M.is_edited({"properties": {"edittime": "2"}})
    assert not M.is_edited({}) and not M.is_edited({"properties": {"edittime": ""}})
    assert not M.is_edited({"properties": "not a dict", "skypeeditedid": ""})


def test_fold_drops_case_and_accents() -> None:
    assert M.fold("ZOÉ Élodie") == "zoe elodie"


# --- thread activity (made-up MRIs) ---

_ME = "8:orgid:00000000-0000-0000-0000-0000000000aa"
_ANN = "8:orgid:00000000-0000-0000-0000-0000000000bb"
_BOB = "8:orgid:00000000-0000-0000-0000-0000000000cc"
_CARL = "8:orgid:00000000-0000-0000-0000-0000000000dd"
_NAMES = {_ANN: "Ann Lee", _BOB: "Bob Roe", _CARL: "Carl Day"}
_ADD = "ThreadActivity/AddMember"
_DEL = "ThreadActivity/DeleteMember"
_TOPIC = "ThreadActivity/TopicUpdate"


def _added(initiator: str, *targets: str) -> str:
    infos = "".join(
        f"<target>{t}</target><detailedtargetinfo><id>{t}</id><sharehistorytime>0</sharehistorytime>"
        "</detailedtargetinfo>"
        for t in targets
    )
    return (
        f"<addmember><eventtime>1721293708701</eventtime><initiator>{initiator}</initiator>"
        f"<rosterVersion>7</rosterVersion>{infos}</addmember>"
    )


def _deleted(initiator: str, target: str) -> str:
    return (
        f"<deletemember><eventtime>1</eventtime><initiator>{initiator}</initiator>"
        f"<target>{target}</target></deletemember>"
    )


def _topic(initiator: str, value: str) -> str:
    return (
        f"<topicupdate><eventtime>1</eventtime><initiator>{initiator}</initiator>"
        f"<value>{value}</value></topicupdate>"
    )


def _members(*people: str | tuple[str, str]) -> str:
    """A meeting chat's MemberJoined/MemberLeft body: the organizer initiates, never a member."""
    members = [
        {"id": p, "friendlyname": ""} if isinstance(p, str) else {"id": p[0], "friendlyname": p[1]}
        for p in people
    ]
    return json.dumps({"eventtime": 1, "initiator": _CARL, "members": members})


def _line(msgtype: str, content: str) -> str | None:
    activity = M.parse_activity(msgtype, content)
    return activity and M.activity_line(msgtype, activity, _ME, _NAMES.__getitem__)


def test_activity_lines_name_who_did_what() -> None:
    """Add (several targets), remove, leave, rename; our own MRI reads "You"."""
    assert _line(_ADD, _added(_ANN, _BOB, _CARL)) == "👥 Ann Lee added Bob Roe, Carl Day"
    assert _line(_ADD, _added(_ME, _BOB)) == "👥 You added Bob Roe"
    assert _line(_ADD, _added(_BOB, _BOB)) == "👥 Bob Roe joined"
    assert _line(_DEL, _deleted(_ANN, _BOB)) == "👥 Ann Lee removed Bob Roe"
    assert _line(_DEL, _deleted(_BOB, _BOB)) == "👥 Bob Roe left"
    assert _line(_DEL, _deleted(_ANN, _ME)) == "👥 Ann Lee removed You"
    assert _line(_TOPIC, _topic(_ANN, "Q&amp;A")) == "✎ Ann Lee renamed the chat to «Q&A»"


def test_meeting_member_events_name_the_members() -> None:
    """A meeting chat's JSON joins and leaves name their members, not the initiator."""
    joined, left = "ThreadActivity/MemberJoined", "ThreadActivity/MemberLeft"
    assert _line(joined, _members(_ANN, _BOB)) == "👥 Ann Lee, Bob Roe joined"
    assert _line(left, _members(_BOB)) == "👥 Bob Roe left"
    assert _line(left, _members((_BOB, "Bob F"), _ME)) == "👥 Bob F, You left"
    activity = M.parse_activity(left, _members(_BOB))
    assert activity is not None and activity.initiator == ""  # name_activity never looks the organizer up


def test_activity_friendly_names_win_and_name_their_own_mri() -> None:
    """A friendlyName (any case) names the MRI just before it; without a directory: "someone"."""
    friendly = (
        f"<deletemember><eventtime>1</eventtime><initiator>{_ANN}</initiator>"
        "<detailedinitiatorinfo><friendlyName>Ann F</friendlyName></detailedinitiatorinfo>"
        f"<target>{_BOB}</target><detailedtargetinfo><id>{_BOB}</id><friendlyname>Bob F</friendlyname>"
        "</detailedtargetinfo></deletemember>"
    )
    assert _line(_DEL, friendly) == "👥 Ann F removed Bob F"
    activity = M.parse_activity(_ADD, _added(_ANN, _BOB))
    assert activity is not None
    assert M.activity_line(_ADD, activity, _ME, None) == "👥 someone added someone"


def test_malformed_activity_is_none() -> None:
    """Empty, truncated, foreign or targetless content: None, the caller keeps the marker."""
    for msgtype, content in [
        (_ADD, ""),
        (_ADD, "<addmember/>"),
        (_ADD, f"<addmember><initiator>{_ANN}</initiator></addmember>"),  # nobody added
        (_ADD, f"<addmember><target>{_BOB}</target></addmember>"),  # nobody adding
        (_ADD, _added(_ANN, _BOB)[:60]),  # cut inside the initiator
        (_DEL, _added(_ANN, _BOB)),  # an addmember body under another type
        (_TOPIC, _topic(_ANN, "")),
        (_TOPIC, _topic(_ANN, "<script>x</script>")),  # raw markup is no leaf value
        ("ThreadActivity/MemberConsumptionHorizonUpdate", "<x/>"),
        (_ADD, '{"initiator": "8:orgid:x", "members": []}'),
        ("ThreadActivity/MemberLeft", '{"initiator": "8:orgid:x", "members": []}'),
        ("ThreadActivity/MemberLeft", '{"initiator": "8:orgid:x", "members": [{"friendlyname": "x"}]}'),
        ("ThreadActivity/MemberLeft", '{"initiator": "8:orgid:x", "members": "8:orgid:y"}'),
        ("ThreadActivity/MemberJoined", '[{"id": "8:orgid:y"}]'),
        ("ThreadActivity/MemberJoined", _members(_BOB)[:30]),
        ("ThreadActivity/MemberJoined", _added(_ANN, _BOB)),
    ]:
        assert M.parse_activity(msgtype, content) is None, content
