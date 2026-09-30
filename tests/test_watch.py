"""`miniteams watch` (spec 009): the matcher, the command, catch-up, the service lines."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from miniteams import cli
from miniteams import watch as W
from miniteams.auth import AuthExpired
from miniteams.config import Settings

ME = "8:orgid:me"
OTHER = "8:orgid:other"
THREAD = "19:a_b@unq.gbl.spaces"
ELSEWHERE = "19:room@thread.v2"
LINK = "https://h/v1/users/ME/conversations/"
START = 1_700_000_000_000
# An unsigned JWT whose payload is {"oid": "me"}: what self_mri reads.
FAKE_TOKEN = "eyJhbGciOiJub25lIn0.eyJvaWQiOiJtZSJ9."  # pragma: allowlist secret


def _message(
    thread: str = THREAD, mid: str = "1700000001000", sender: str = OTHER, **extra: Any
) -> dict[str, Any]:
    resource = {
        "id": mid,
        "composetime": "2026-07-01T09:00:00Z",
        "messagetype": "RichText/Html",
        "content": "<p>hello <b>you</b></p>",
        "from": f"{LINK.replace('/ME/conversations/', '/ME/contacts/')}{sender}",
        "imdisplayname": "Someone",
        "conversationLink": LINK + thread,
        "clientmessageid": f"cm-{mid}",
        **extra,
    }
    return {"type": "EventMessage", "resourceType": "NewMessage", "resource": resource}


def _reaction(
    thread: str = THREAD, mid: str = "1700000001000", by: str = ME, key: str = "like", at: int = START + 5000
) -> dict[str, Any]:
    event = _message(thread, mid)
    event["resourceType"] = "MessageUpdate"
    event["resource"]["properties"] = {"emotions": [{"key": key, "users": [{"mri": by, "time": at}]}]}
    return event


def _match(watch: W.Watch, event: dict[str, Any], sent: set[str] | None = None) -> bool:
    return W.match(watch, event, ME, START, sent or set()) is not None


# --- the matcher ---

ROWS = {
    "mine": W.Watch(sender="me"),
    "answer": W.Watch(thread=THREAD, after="1700000000500", sender="others", once=True),
    "chat": W.Watch(thread=THREAD),
    "reaction": W.Watch(event="reaction", sender="me"),
}
EVENTS = {
    "mine": _message(ELSEWHERE, sender=ME),
    "answer": _message(THREAD, mid="1700000000900", sender=OTHER),
    "chat": _message(THREAD, sender=OTHER),
    "reaction": _reaction(ELSEWHERE, by=ME),
}


@pytest.mark.parametrize("row", list(ROWS))
def test_each_row_of_the_table_matches_its_event_and_rejects_the_others(row: str) -> None:
    for name, event in EVENTS.items():
        # A message from someone else in the chat is both "chat" and "answer": the rows overlap there.
        expected = name == row or {row, name} == {"chat", "answer"}
        assert _match(ROWS[row], event) is expected, (row, name)


def test_from_is_judged_on_the_mri_not_the_name() -> None:
    mine = _message(sender=ME, imdisplayname="Someone")
    theirs = _message(sender=OTHER, imdisplayname="Me")
    assert _match(W.Watch(sender="me"), mine) and not _match(W.Watch(sender="me"), theirs)
    assert _match(W.Watch(thread=THREAD, sender="others"), theirs)
    assert not _match(W.Watch(thread=THREAD, sender="others"), mine)


def test_after_keeps_only_newer_messages_in_that_chat() -> None:
    watch = W.Watch(thread=THREAD, after="1700000001000")
    assert not _match(watch, _message(mid="1700000001000"))  # the message itself
    assert not _match(watch, _message(mid="1700000000999"))
    assert _match(watch, _message(mid="1700000001001"))
    assert not _match(watch, _message(ELSEWHERE, mid="1700000001001"))


def test_reactions_match_on_my_mri_and_a_time_after_the_start() -> None:
    mine = W.Watch(event="reaction", sender="me")
    assert _match(mine, _reaction(by=ME, at=START + 1))
    assert not _match(mine, _reaction(by=ME, at=START))  # already there when the watch began
    assert not _match(mine, _reaction(by=OTHER, at=START + 1))
    removed = _reaction(by=ME, at=START + 1)
    removed["resource"]["properties"]["emotions"][0]["users"] = []
    assert not _match(mine, removed)
    assert _match(W.Watch(event="reaction", sender="me", reaction="like"), _reaction(key="like"))
    assert not _match(W.Watch(event="reaction", sender="me", reaction="heart"), _reaction(key="like"))
    assert not _match(mine, _message(sender=ME))  # a message is not a reaction


def test_edits_deletes_typing_and_calls_never_match_a_message_watch() -> None:
    watch = W.Watch(thread=THREAD)
    edit = _message()
    edit["resourceType"] = "MessageUpdate"
    edit["resource"]["skypeeditedid"] = "1700000001000"
    delete = _message()
    delete["resourceType"] = "MessageUpdate"
    delete["resource"]["properties"] = {"deletetime": "1"}
    for event in (
        edit,
        delete,
        _message(messagetype="Control/Typing"),
        _message(messagetype="Event/Call"),
        _message(messagetype="ThreadActivity/AddMember"),
    ):
        assert not _match(watch, event)
    assert _match(watch, _message(messagetype="Text"))


def test_what_the_mcp_server_sent_never_matches() -> None:
    event = _message(sender=ME)
    assert _match(W.Watch(sender="me"), event)
    assert not _match(W.Watch(sender="me"), event, sent={"cm-1700000001000"})


def test_the_sent_file_is_bounded_and_a_broken_one_excludes_nothing(settings: Settings) -> None:
    assert W.load_sent_ids(settings) == set()
    for n in range(W._SENT_MAX + 5):
        W.note_sent(settings, f"cm-{n}")
    ids = W.load_sent_ids(settings)
    assert len(ids) == W._SENT_MAX and "cm-0" not in ids and f"cm-{W._SENT_MAX + 4}" in ids
    W.sent_file(settings).write_text("{not json")
    assert W.load_sent_ids(settings) == set()


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"sender": "others"},
        {"sender": "anyone", "after": "1"},
        {"thread": THREAD, "after": "1", "reaction": "like"},
    ],
)
def test_impossible_watches_are_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        W.Watch(**fields).validate()


# --- the command ---


@pytest.mark.parametrize(
    "argv",
    [
        ["watch"],
        ["watch", "--from", "others"],
        ["watch", "--after", "1"],
        ["watch", "--thread", THREAD, "--reaction", "like"],
    ],
)
def test_the_command_exits_2_before_any_socket(argv: list[str], monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        W, "run_forever", lambda *a, **k: (_ for _ in ()).throw(AssertionError("socket opened"))
    )
    monkeypatch.setenv("MINITEAMS_TENANT_ID", "t")
    with pytest.raises(SystemExit) as stop:
        cli.main(argv)
    assert stop.value.code == 2 and capsys.readouterr().out == ""


class _Stream:
    """A stand-in for run_forever: feeds events to the hooks, then ends or hangs."""

    def __init__(self, events: list[dict[str, Any]], *, end: str = "hang") -> None:
        self.events, self.end = events, end
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, settings: Settings, **kw: Any) -> None:
        self.calls.append(kw)
        await kw["on_gap"]("connected")
        for event in self.events:
            if event.get("gap"):
                await kw["on_gap"](event["gap"])
            else:
                await kw["on_event"](event)
        if self.end == "hang":
            await asyncio.sleep(10)
        # otherwise return: run_forever returns on a dead credential or a closed stdout


def _wire(monkeypatch, stream: _Stream, *, dead: bool = False) -> None:
    class _Tokens:
        def refresh(self) -> dict[str, Any]:
            if dead:
                raise AuthExpired("dead")
            return {"access_token": FAKE_TOKEN, "id_token": "b"}

    monkeypatch.setattr(W, "token_source", lambda settings: _Tokens())
    monkeypatch.setattr(W, "exchange_skype_token", lambda settings, token: {"skype_token": "sk"})
    monkeypatch.setattr(W, "run_forever", stream)

    async def label(self: Any, thread_id: str) -> str:
        return "Chat"

    async def display(self: Any, mri: str) -> str:
        return mri.rsplit(":", 1)[-1]

    monkeypatch.setattr(W.Directory, "label", label)
    monkeypatch.setattr(W.Directory, "display", display)


def _run(settings: Settings, watch: W.Watch, capsys) -> tuple[int, list[dict[str, Any]]]:
    rc = asyncio.run(asyncio.wait_for(W.run_watch(settings, watch), 5))
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    return rc, lines


def test_once_prints_one_line_and_exits_0_even_when_two_arrive_together(
    settings: Settings, monkeypatch, capsys
) -> None:
    stream = _Stream(
        [_message(mid="1700000001000", content="line one<br>line two\nthree"), _message(mid="1700000001001")]
    )
    _wire(monkeypatch, stream)
    rc, lines = _run(settings, W.Watch(thread=THREAD, once=True, note="why"), capsys)
    assert rc == 0 and len(lines) == 1
    assert lines[0] == {
        "event": "message",
        "thread": THREAD,
        "chat": "Chat",
        "sender": "Someone",
        "message_id": "1700000001000",
        "time": "2026-07-01T09:00:00Z",
        "text": "line one line two three",
        "reaction": "",
        "note": "why",
    }
    assert stream.calls[0]["prompt"] is False and stream.calls[0]["epid"]


def test_each_process_gets_its_own_endpoint_id(settings: Settings, monkeypatch, capsys) -> None:
    ids = []
    for _ in range(2):
        stream = _Stream([_message()])
        _wire(monkeypatch, stream)
        _run(settings, W.Watch(thread=THREAD, once=True), capsys)
        ids.append(stream.calls[0]["epid"])
    assert ids[0] != ids[1] and not list(settings.config_dir.glob("endpoint_id*"))


def test_a_dead_credential_prints_stopped_and_exits_1(settings: Settings, monkeypatch, capsys) -> None:
    stream = _Stream([])
    _wire(monkeypatch, stream, dead=True)
    rc, lines = _run(settings, W.Watch(thread=THREAD), capsys)
    assert rc == 1 and lines == [{"event": "stopped", "reason": "auth_expired: dead", "note": ""}]
    assert stream.calls == []  # no socket


def test_a_credential_dying_later_prints_stopped(settings: Settings, monkeypatch, capsys) -> None:
    _wire(monkeypatch, _Stream([_message()], end="return"))
    rc, lines = _run(settings, W.Watch(thread=THREAD), capsys)
    assert rc == 1 and [line["event"] for line in lines] == ["message", "stopped"]


def test_a_reconnect_prints_gap_and_the_watch_goes_on(settings: Settings, monkeypatch, capsys) -> None:
    _wire(monkeypatch, _Stream([{"gap": "connected"}, {"gap": "message_loss"}, _message()]))
    rc, lines = _run(settings, W.Watch(thread=THREAD, once=True), capsys)
    assert [(line["event"], line.get("reason")) for line in lines] == [
        ("gap", "connected"),
        ("gap", "message_loss"),
        ("message", None),
    ]


def test_the_eleventh_match_in_a_minute_is_held_back(settings: Settings, monkeypatch, capsys) -> None:
    events = [_message(mid=str(1700000001000 + n)) for n in range(14)]
    _wire(monkeypatch, _Stream(events, end="return"))
    rc, lines = _run(settings, W.Watch(thread=THREAD), capsys)
    printed = [line for line in lines if line["event"] == "message"]
    assert len(printed) == W._RATE_MAX
    assert {"event": "suppressed", "held_back": 4, "note": ""} in lines


def test_catch_up_prints_history_oldest_first_and_never_twice(
    settings: Settings, monkeypatch, capsys
) -> None:
    history = [
        _message(mid="1700000001003")["resource"] | {"composetime": "2026-07-01T09:03:00Z"},
        _message(mid="1700000001002")["resource"] | {"composetime": "2026-07-01T09:02:00Z"},
        _message(mid="1700000001000")["resource"]
        | {"composetime": "2026-07-01T09:00:00Z"},  # the --after one
    ]
    monkeypatch.setattr(W, "iter_history_pages", lambda *a, **k: iter([history]))
    _wire(monkeypatch, _Stream([_message(mid="1700000001002"), _message(mid="1700000001004")], end="return"))
    rc, lines = _run(settings, W.Watch(thread=THREAD, after="1700000001000"), capsys)
    assert [line["message_id"] for line in lines if line["event"] == "message"] == [
        "1700000001002",
        "1700000001003",
        "1700000001004",
    ]


def test_catch_up_alone_can_satisfy_once(settings: Settings, monkeypatch, capsys) -> None:
    history = [_message(mid="1700000001001")["resource"]]
    monkeypatch.setattr(W, "iter_history_pages", lambda *a, **k: iter([history]))
    stream = _Stream([_message(mid="1700000001002")])
    _wire(monkeypatch, stream)
    rc, lines = _run(settings, W.Watch(thread=THREAD, after="1700000001000", once=True), capsys)
    assert rc == 0 and [line["message_id"] for line in lines] == ["1700000001001"]
    assert stream.calls == []  # no socket needed


def test_stdout_lines_are_single_json_objects(settings: Settings, monkeypatch, capsys) -> None:
    _wire(monkeypatch, _Stream([_message(content="a\nb\r\nc")], end="return"))
    _, _ = _run(settings, W.Watch(thread=THREAD), capsys)
    out = capsys.readouterr().out
    assert out == "" or all(json.loads(line) for line in out.splitlines())


def test_a_closed_stdout_ends_quietly(settings: Settings, monkeypatch) -> None:
    _wire(monkeypatch, _Stream([], end="return"))
    monkeypatch.setattr(W, "_stdout_closed", lambda: True)
    rc = asyncio.run(W.run_watch(settings, W.Watch(thread=THREAD)))
    assert rc == 0


def test_settings_tenant(settings: Settings) -> None:
    assert sys.version_info >= (3, 14) and settings.tenant_id  # guards the fixture the CLI test relies on


def test_sent_file_lives_under_the_cache_dir(settings: Settings) -> None:
    assert W.sent_file(settings) == Path(settings.cache_dir) / "mcp-sent.json"


def test_a_crashing_stream_prints_stopped_and_exits_1(settings: Settings, monkeypatch, capsys) -> None:
    async def crash(settings: Settings, **kw: Any) -> None:
        raise RuntimeError("socket library gone")

    _wire(monkeypatch, _Stream([]))
    monkeypatch.setattr(W, "run_forever", crash)
    rc, lines = _run(settings, W.Watch(thread=THREAD), capsys)
    assert rc == 1 and lines == [{"event": "stopped", "reason": "crash: socket library gone", "note": ""}]


def test_catch_up_stops_walking_when_after_is_not_found(settings: Settings, monkeypatch, capsys) -> None:
    monkeypatch.setattr(W, "_CATCHUP_PAGES", 2)
    pages_served = []

    def pages(*a: Any, **k: Any):  # noqa: ANN202
        n = 0
        while True:  # an endless history: `--after` names a deleted message
            n += 1
            pages_served.append(n)
            yield [_message(mid=str(1700000009000 - n))["resource"]]

    monkeypatch.setattr(W, "iter_history_pages", pages)
    _wire(monkeypatch, _Stream([], end="return"))
    _run(settings, W.Watch(thread=THREAD, after="1"), capsys)
    assert pages_served == [1, 2]
