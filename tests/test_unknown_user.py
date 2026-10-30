"""038-AC6: a person no source names reads "Unknown user", never their guid; the MRI stays beside the label.
Fictional MRIs and names only."""

import asyncio
import json
import re
from pathlib import Path

import pytest

from miniteams import chat_verbs as CV
from miniteams import compose
from miniteams import messages as M
from miniteams import web as W
from miniteams.archive_store import Index
from miniteams.directory import UNKNOWN_USER, Directory, member_label, roster_label
from miniteams.mcp import Archive, message_record

ME = "8:orgid:00000000-0000-4000-8000-0000000000aa"
ANN = "8:orgid:00000000-0000-4000-8000-0000000000bb"
GONE = "8:orgid:00000000-0000-4000-8000-0000000000cc"  # nobody ever named them
BOT = "28:00000000-0000-4000-8000-0000000000dd"
T = "19:unknown-user-test@thread.v2"
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MEMBERS = [
    {"mri": ME, "name": "Me Self"},
    {"mri": ANN, "name": "Ann Lee"},
    {"mri": GONE, "name": UNKNOWN_USER},
]


def _message(sender: str, declared: str = "", **extra: object) -> dict[str, object]:
    return {
        "id": "7",
        "composetime": "2026-10-30T10:00:00.000Z",
        "messagetype": "RichText/Html",
        "content": "<p>hi</p>",
        "from": f"https://h/v1/users/ME/contacts/{sender}",
        "imdisplayname": declared,
        **extra,
    }


def test_labels_fall_back_to_unknown_user_and_a_bot_keeps_its_own(directory: Directory) -> None:
    directory.note_name(ANN, "Ann Lee")
    assert (directory.name_for(GONE), directory.name_for(ANN), directory.name_for(BOT)) == (
        UNKNOWN_USER,
        "Ann Lee",
        BOT.split(":")[-1],
    )
    # A stored roster: empty, the bare guid, or this label all read the label; a name stays.
    for stored in ("", GONE.split(":")[-1], UNKNOWN_USER):
        assert member_label(GONE, stored) == UNKNOWN_USER
        assert roster_label(None, [{"mri": GONE, "name": stored}, {"mri": ANN, "name": "Ann Lee"}], ME) == (
            "Unknown user, Ann Lee"
        )
    assert member_label(ANN, "Ann Lee") == "Ann Lee"
    assert member_label(BOT, "") == "" and member_label(BOT, "Helper Bot") == "Helper Bot"


async def test_display_and_the_fetched_roster_say_unknown_user(
    directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_directory_fetch import _patch, _Resp

    members = [{"mri": GONE, "friendlyName": ""}, {"mri": ANN, "friendlyName": "Ann Lee"}, {"mri": BOT}]
    _patch(monkeypatch, lambda url: _Resp({"members": members}))
    assert await directory.display(GONE) == UNKNOWN_USER
    info = await directory.thread(T)
    assert info is not None
    assert [m["name"] for m in info["members"]] == [UNKNOWN_USER, "Ann Lee", BOT.split(":")[-1]]
    assert await directory.label(T) == "Unknown user, Ann Lee"
    assert directory.known_name(GONE) == ""  # the label is never stored as their name


def test_the_unknown_label_is_never_stored_as_a_name(directory: Directory) -> None:
    directory.note_name(GONE, UNKNOWN_USER)
    directory.note_declared(GONE, UNKNOWN_USER, 1)
    assert directory._store is not None and directory._store.get([GONE]) == {}


def test_member_lines_are_escaped_html_with_the_mri_as_tooltip(directory: Directory) -> None:
    directory.note_name(ANN, "<b>Ann</b> & co")
    added = M.Activity(ANN, (GONE, ME), "", {})
    assert M.activity_line("ThreadActivity/AddMember", added, ME, directory.name_for) == (
        f'👥 &lt;b&gt;Ann&lt;/b&gt; &amp; co added <span title="{GONE}">Unknown user</span>, You'
    )
    odd = '8:orgid:x"><img src=x onerror=alert(1)>'  # the tooltip's value is attribute-escaped
    left = M.Activity("", (odd,), "", {})
    assert M.activity_line("ThreadActivity/MemberLeft", left, ME, directory.name_for) == (
        '👥 <span title="8:orgid:x&quot;&gt;&lt;img src=x onerror=alert(1)&gt;">Unknown user</span> left'
    )
    topic = M.Activity(GONE, (), "<i>Q&A</i>", {})
    assert M.activity_line("ThreadActivity/TopicUpdate", topic, ME, directory.name_for) == (
        f'✎ <span title="{GONE}">Unknown user</span> renamed the chat to «&lt;i&gt;Q&amp;A&lt;/i&gt;»'
    )


def test_a_friendlyname_that_is_the_guid_names_nobody(directory: Directory) -> None:
    """A member event naming its target by the bare guid reads "Unknown user", or the name known since."""
    tail = GONE.split(":")[-1]
    added = (
        f"<addmember><eventtime>1</eventtime><initiator>{ANN}</initiator><target>{GONE}</target>"
        f"<detailedtargetinfo><id>{GONE}</id><friendlyname>{tail}</friendlyname></detailedtargetinfo></addmember>"
    )
    left = json.dumps({"eventtime": 1, "initiator": ANN, "members": [{"id": GONE, "friendlyname": tail}]})
    directory.note_name(ANN, "Ann Lee")
    for msgtype, content, line in [
        ("ThreadActivity/AddMember", added, "👥 Ann Lee added {}"),
        ("ThreadActivity/MemberLeft", left, "👥 {} left"),
    ]:
        activity = M.parse_activity(msgtype, content)
        assert activity is not None and activity.names.get(GONE) is None  # name_activity resolves them
        unknown = f'<span title="{GONE}">Unknown user</span>'
        assert M.activity_line(msgtype, activity, ME, directory.name_for) == line.format(unknown)
    directory.note_name(GONE, "Gil Gone")
    activity = M.parse_activity("ThreadActivity/AddMember", added)
    assert activity is not None
    assert M.activity_line("ThreadActivity/AddMember", activity, ME, directory.name_for) == (
        "👥 Ann Lee added Gil Gone"
    )


def test_the_chat_record_names_the_sender_and_reactors_and_keeps_the_mri(directory: Directory) -> None:
    directory.note_name(ANN, "Ann Lee")
    emotions = [
        {"key": "like", "users": [{"mri": GONE}, {"mri": ANN}]},
        {"key": "heart", "users": [{"mri": ANN}]},
    ]
    record = CV.chat_record(
        _message(GONE, properties={"emotions": emotions}),
        T,
        ME,
        name_for=directory.name_for,
        known_name=directory.known_name,
    )
    assert (record["name"], record["from"]) == (UNKNOWN_USER, GONE)
    like, heart = record["reactions"]
    assert (like["people"], like["mris"]) == ([UNKNOWN_USER, "Ann Lee"], [GONE, ANN])
    assert heart["people"] == ["Ann Lee"] and "mris" not in heart  # named people only: no tooltip data
    named = CV.chat_record(_message(ANN), T, ME, name_for=directory.name_for, known_name=directory.known_name)
    assert named["name"] == "Ann Lee"
    bot = CV.chat_record(_message(BOT), T, ME, name_for=directory.name_for, known_name=directory.known_name)
    assert bot["name"] == ""  # bots: as before, the page shows "?"
    system = _message(
        "19:unknown-user-test@thread.v2",
        messagetype="ThreadActivity/DeleteMember",
        content=f"<deletemember><eventtime>1</eventtime><initiator>{GONE}</initiator>"
        f"<target>{GONE}</target></deletemember>",
    )
    line = CV.chat_record(system, T, ME, name_for=directory.name_for, known_name=directory.known_name)
    assert line["html"] == f'👥 <span title="{GONE}">Unknown user</span> left'


def test_mcp_records_say_unknown_user_and_keep_the_mri(directory: Directory, tmp_path: Path) -> None:
    record = message_record(_message(GONE), directory.known_name)
    assert (record["sender"], record["from"]) == (UNKNOWN_USER, GONE)
    assert message_record(_message(GONE, "Gone Declared"), directory.known_name)["sender"] == "Gone Declared"
    assert message_record(_message(BOT), directory.known_name)["sender"] == ""  # bots: as before
    data = tmp_path / "data"
    index = Index(data)
    index.upsert_chat(T, label="Chat", participants=[{"mri": GONE, "name": GONE.split(":")[-1]}, MEMBERS[1]])
    index.close()
    (chat,) = Archive(data).find_chats("", "", "", "", 10)
    assert chat["participants"] == [{"name": UNKNOWN_USER, "mri": GONE}, {"name": "Ann Lee", "mri": ANN}]
    assert not GUID.search(json.dumps([p["name"] for p in chat["participants"]]))


@pytest.mark.parametrize("q", ["", "u", "Unknown", "unknown user"])
def test_an_unnamed_member_is_never_a_mention_candidate(q: str) -> None:
    items = CV._member_items({m["mri"]: m["name"] for m in MEMBERS}, T, ME, q)
    assert UNKNOWN_USER not in [i["name"] for i in items]
    assert GONE not in [i["mri"] for i in items]


@pytest.mark.parametrize("text", ["@Unknown hi", "@Unknown user hi", "@{Unknown user|them} hi"])
def test_unknown_user_mentions_nobody(text: str) -> None:
    with pytest.raises(ValueError, match="unknown mention"):
        compose.resolve_mentions(text, MEMBERS, T, ME)


def _hits_board(directory: Directory, data: Path) -> W.Board:
    return W.Board({}, directory, me=ME, data_dir=data)


async def test_a_search_hit_from_an_unnamed_sender_is_unknown_user_with_its_mri(
    directory: Directory, tmp_path: Path
) -> None:
    from test_web_chat import _archive, _search

    root = _archive(tmp_path / "data", T, [_message(GONE), {**_message(ANN, "Ann Lee"), "id": "8"}])
    hits = (await _search(_hits_board(directory, root), T, "hi"))["hits"]
    assert sorted((h["name"], h["from"]) for h in hits) == [("Ann Lee", ANN), (UNKNOWN_USER, GONE)]


def test_a_quoted_unknown_user_carries_the_mri_on_hover(directory: Directory) -> None:
    directory.note_name(ANN, "Ann Lee")
    for author, name, title in [(GONE, UNKNOWN_USER, f' title="{GONE}"'), (ANN, "Ann Lee", "")]:
        quote = compose.reply_quote({**_message(author), "id": "5"}, name)
        reply = _message(ANN, content=f"{quote}<p>agreed</p>")
        shown = CV.chat_record(reply, T, ME, name_for=directory.name_for, known_name=directory.known_name)
        assert f"<strong{title}>{name}</strong>" in shown["html"]
    odd = '8:orgid:x"><img src=x onerror=alert(1)>'  # attribute-escaped, as the MRI arrives escaped in itemid
    quote = compose.reply_quote({**_message(odd), "id": "5"}, UNKNOWN_USER)
    shown = CV.chat_record(_message(ANN, content=quote), T, ME, known_name=directory.known_name)["html"]
    assert (
        '<strong title="8:orgid:x&quot;&gt;&lt;img src=x onerror=alert(1)&gt;">Unknown user</strong>' in shown
    )


async def _feed(board: W.Board, event: dict[str, object]) -> None:
    """The event, then every relay it queued: a marker queued last ends them."""
    await board.on_event(event)
    done = asyncio.Event()

    async def mark() -> None:
        done.set()

    board._enqueue_relay(T, mark)
    await asyncio.wait_for(done.wait(), 2)


async def test_two_unknown_typers_stay_two_when_one_stops(
    directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typing is keyed by MRI: the first "Unknown user" to stop leaves the other typing."""
    from test_web_chat import _event

    other = "8:orgid:00000000-0000-4000-8000-0000000000ee"
    row = {"id": T, "label": "T", "last_activity": "2026-10-30T10:00:00Z", "last_id": "1", "typing": []}
    board = W.Board({T: row}, directory, me=ME, typing_ttl=60)
    frames: list[dict[str, object]] = []
    monkeypatch.setattr(board, "_relay_now", lambda thread, frame: frames.append(frame))
    monkeypatch.setattr(board, "_relay_queued", lambda thread, frame: frames.append(frame))
    board._follows[object()] = (T, None)  # type: ignore[index]
    try:
        for mri in (GONE, other):
            who = {"from": f"https://h/v1/users/ME/contacts/{mri}", "imdisplayname": ""}
            await _feed(board, _event("NewMessage", T, messagetype="Control/Typing", **who))
        assert row["typing"] == [UNKNOWN_USER]  # rows keep names: the widget shows one label
        assert {(f["mri"], f["on"]) for f in frames} == {(GONE, True), (other, True)}
        gone = {"from": f"https://h/v1/users/ME/contacts/{GONE}", "imdisplayname": ""}
        await _feed(board, _event("NewMessage", T, messagetype="Control/ClearTyping", **gone))
        assert row["typing"] == [UNKNOWN_USER]  # the other one still types
        assert frames[-1] == {"typing": UNKNOWN_USER, "on": False, "mri": GONE}
        assert [mri for (tid, mri) in board._typing_timers if tid == T] == [other]
    finally:
        board.close()


def test_a_declared_name_that_is_the_guid_is_no_name(directory: Directory) -> None:
    said = _message(GONE, GONE.split(":")[-1])
    record = CV.chat_record(said, T, ME, name_for=directory.name_for, known_name=directory.known_name)
    assert (record["name"], record["from"]) == (UNKNOWN_USER, GONE)
    assert (message_record(said, directory.known_name)["sender"], message_record(said)["from"]) == (
        UNKNOWN_USER,
        GONE,
    )
