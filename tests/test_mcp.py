"""MCP server (spec 008): the protocol over text streams, find_chats and read_messages on a fixture."""

import io
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from miniteams import archive as AR
from miniteams import drafts as D
from miniteams import mcp as M
from miniteams import mcp_api as API
from miniteams.archive_store import ChatStore, Index
from miniteams.auth import AuthExpired, AuthUnavailable
from miniteams.config import Settings
from miniteams.dump import _epoch_seconds

_REAL_CLIENT = httpx.Client

PRIVATE = "19:a_b@unq.gbl.spaces"
GROUP = "19:room@thread.v2"
MEETING = "19:meeting_abc@thread.v2"
ME = "8:orgid:me"
OTHER = "8:orgid:other"


def _member(mri: str, name: str) -> dict[str, str]:
    return {"mri": mri, "name": name, "role": "User"}


def _conv(when: str) -> dict[str, Any]:
    return {"lastMessage": {"composetime": when, "content": "x"}}


def _msg(
    mid: str, when: str, content: str = "<p>hi <b>there</b> &amp; you</p>", **extra: Any
) -> dict[str, Any]:
    return {
        "id": mid,
        "composetime": when,
        "content": content,
        "messagetype": "RichText/Html",
        "imdisplayname": "Somebody",
        "from": f"https://h/v1/users/ME/contacts/{OTHER}",
        "properties": {},
        **extra,
    }


@pytest.fixture
def data(tmp_path: Path) -> Path:
    """Three chats through the archive's own writers, so the schema cannot drift from the reader."""
    root = tmp_path / "data"
    index = Index(root)
    index.upsert_chat(
        PRIVATE,
        label="Élodie BERNARD",
        participants=[_member(ME, "Me"), _member(OTHER, "Élodie BERNARD")],
        raw=_conv("2026-07-03T09:00:00Z"),
    )
    index.upsert_chat(
        GROUP,
        label="Paris office · 3p",
        topic="Paris office",
        participants=[_member(ME, "Me"), _member(OTHER, "Élodie BERNARD"), _member("8:orgid:x", "X")],
        raw=_conv("2026-07-01T09:00:00Z"),
    )
    index.upsert_chat(MEETING, label="Weekly · 2p", topic="Weekly", raw=_conv("2026-07-02T09:00:00Z"))
    index.mark_backfill_done(PRIVATE)
    index.touch(PRIVATE, "2026-07-04T00:00:00Z")
    index.set_meta("archiver_seen", "2026-07-04T00:00:30Z")
    index.set_meta("archiver_state", "following")
    index.set_meta("sync_started_at", "2026-07-04T00:00:00Z")
    index.close()
    store = ChatStore(root, PRIVATE)
    store.insert_page(
        [
            _msg("1", "2026-07-01T09:00:00.1000000Z"),
            _msg("2", "2026-07-02T09:00:00.1000000Z", properties={"edittime": "1"}),
            _msg("3", "2026-07-02T09:00:00.1000000Z", content="", properties={"deletetime": "1"}),
            _msg(
                "4",
                "2026-07-03T09:00:00.1000000Z",
                amsreferences=["o1"],
                properties={"files": '[{"fileName": "a.pdf"}]'},
            ),
        ]
    )
    store.close()
    return root


def _server(settings: Settings, data: Path, **kw: Any) -> M.Server:
    return M.Server(settings, data, stdout=io.StringIO(), **kw)


def _rpc(
    server: M.Server, method: str, params: dict[str, Any] | None = None, req_id: Any = 1
) -> dict[str, Any]:
    request: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        request["params"] = params
    response = server.handle(json.dumps(request))
    assert response is not None
    return response


def _call(server: M.Server, name: str, **args: Any) -> dict[str, Any]:
    result = _rpc(server, "tools/call", {"name": name, "arguments": args})["result"]
    return (
        {"error": result.get("isError", False), **json.loads(result["content"][0]["text"])}
        if not result.get("isError")
        else {"error": True, "text": result["content"][0]["text"]}
    )


# --- protocol ---


@pytest.mark.parametrize(
    ("asked", "answered"), [("2025-06-18", "2025-06-18"), ("2024-11-05", "2024-11-05"), ("1.0", "2025-06-18")]
)
def test_initialize_negotiates_the_version(settings: Settings, data: Path, asked: str, answered: str) -> None:
    result = _rpc(_server(settings, data), "initialize", {"protocolVersion": asked, "capabilities": {}})[
        "result"
    ]
    assert result["protocolVersion"] == answered
    assert "tools" in result["capabilities"] and result["serverInfo"]["name"] == "miniteams"
    assert result["instructions"]


def test_tools_list_and_read_only(settings: Settings, data: Path) -> None:
    tools = _rpc(_server(settings, data), "tools/list")["result"]["tools"]
    reads = ["find_chats", "read_messages", "search_messages", "read_transcript", "login"]
    writes = ["send_message", "list_scheduled", "cancel_scheduled", "update_scheduled", "update_message"]
    assert [t["name"] for t in tools] == reads[:4] + writes + reads[4:]
    assert all(t["inputSchema"]["type"] == "object" for t in tools)
    assert [t["name"] for t in tools if not t["annotations"]["readOnlyHint"]] == writes
    destructive = ["cancel_scheduled", "update_scheduled", "update_message"]
    assert [t["name"] for t in tools if t["annotations"]["destructiveHint"]] == destructive
    listed = _rpc(_server(settings, data, read_only=True), "tools/list")["result"]["tools"]
    assert [t["name"] for t in listed] == reads


def test_notifications_get_no_answer_and_unknown_methods_do(settings: Settings, data: Path) -> None:
    server = _server(settings, data)
    assert server.handle(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})) is None
    assert server.handle(json.dumps({"jsonrpc": "2.0", "method": "nothing/here"})) is None  # a notification
    assert _rpc(server, "nothing/here")["error"]["code"] == -32601
    assert _rpc(server, "ping")["result"] == {}


def test_bad_lines_answer_and_the_server_keeps_reading(settings: Settings, data: Path) -> None:
    out = io.StringIO()
    lines = ["{not json", json.dumps([1, 2]), json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})]
    server = M.Server(settings, data, stdin=io.StringIO("\n".join(lines) + "\n\n"), stdout=out)
    assert server.serve() == 0  # EOF exits 0
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [a.get("error", {}).get("code") for a in answers[:2]] == [-32700, -32600]
    assert answers[2] == {"jsonrpc": "2.0", "id": 9, "result": {}}
    assert all(line.count("\n") == 0 for line in out.getvalue().splitlines())


@pytest.mark.parametrize(
    ("name", "args", "reason"),
    [
        ("no_such_tool", {}, "unknown tool"),
        ("read_messages", {}, "missing argument: thread_id"),
        ("read_messages", {"thread_id": 5}, "thread_id must be string"),
        ("read_messages", {"thread_id": "x", "limit": True}, "limit must be integer"),
        ("read_messages", {"thread_id": "x", "limit": 0}, "limit must be >= 1"),
        ("find_chats", {"kind": "dm"}, "kind must be one of"),
        ("find_chats", {"colour": "blue"}, "unknown argument: colour"),
        ("find_chats", 3, "arguments must be an object"),
    ],
)
def test_bad_tool_calls_answer_invalid_params(
    settings: Settings, data: Path, name: str, args: Any, reason: str
) -> None:
    response = _rpc(_server(settings, data), "tools/call", {"name": name, "arguments": args})
    assert response["error"]["code"] == -32602 and reason in response["error"]["message"]


def test_a_failing_tool_is_an_error_result_not_a_crash(settings: Settings, data: Path) -> None:
    server = _server(settings, data)
    result = _call(server, "read_messages", thread_id="19:nobody@thread.v2")
    assert result["error"] and "unknown chat" in result["text"]
    assert _rpc(server, "ping")["result"] == {}


# --- find_chats ---


def test_find_chats_matches_label_topic_and_names_newest_first(settings: Settings, data: Path) -> None:
    out = _call(_server(settings, data), "find_chats")
    assert out["source"] == "archive" and out["archiver_state"] == "following"
    assert [c["id"] for c in out["chats"]] == [PRIVATE, MEETING, GROUP]
    assert [c["kind"] for c in out["chats"]] == ["private", "meeting", "group"]
    private = out["chats"][0]
    assert (
        private["backfill_done"]
        and not private["history_denied"]
        and private["synced_at"] == "2026-07-04T00:00:00Z"
    )
    assert private["participants"] == [{"name": "Me", "mri": ME}, {"name": "Élodie BERNARD", "mri": OTHER}]

    assert [c["id"] for c in _call(_server(settings, data), "find_chats", query="paris")["chats"]] == [GROUP]
    assert [c["id"] for c in _call(_server(settings, data), "find_chats", query="bernard")["chats"]] == [
        PRIVATE,
        GROUP,
    ]
    assert [c["id"] for c in _call(_server(settings, data), "find_chats", query="WEEKLY")["chats"]] == [
        MEETING
    ]


def test_find_chats_participant_is_membership_case_and_accents_ignored(
    settings: Settings, data: Path
) -> None:
    server = _server(settings, data)
    assert [c["id"] for c in _call(server, "find_chats", participant="elodie")["chats"]] == [PRIVATE, GROUP]
    assert [c["id"] for c in _call(server, "find_chats", participant="ÉLODIE", kind="private")["chats"]] == [
        PRIVATE
    ]
    assert [c["id"] for c in _call(server, "find_chats", participant=OTHER)["chats"]] == [PRIVATE, GROUP]
    assert [
        c["id"] for c in _call(server, "find_chats", participant="paris")["chats"]
    ] == []  # a topic, not a member
    assert [c["id"] for c in _call(server, "find_chats", participant="bernard", query="paris")["chats"]] == [
        GROUP
    ]


def test_find_chats_since_kind_and_limit(settings: Settings, data: Path) -> None:
    server = _server(settings, data)
    assert [c["id"] for c in _call(server, "find_chats", since="2026-07-02")["chats"]] == [PRIVATE, MEETING]
    assert [c["id"] for c in _call(server, "find_chats", kind="group")["chats"]] == [GROUP]
    assert [c["id"] for c in _call(server, "find_chats", limit=1)["chats"]] == [PRIVATE]
    bad = _call(server, "find_chats", since="yesterday")
    assert bad["error"] and "invalid ISO" in bad["text"]


# --- read_messages ---


def test_read_messages_plain_text_oldest_first_with_flags(settings: Settings, data: Path) -> None:
    out = _call(_server(settings, data), "read_messages", thread_id=PRIVATE)
    assert out["source"] == "archive" and out["archiver_seen"] == "2026-07-04T00:00:30Z"
    ids = [m["id"] for m in out["messages"]]
    assert ids == ["1", "2", "3", "4"] and "next_since" not in out
    first, edited, deleted, files = out["messages"]
    assert first["text"] == "hi there & you" and first["sender"] == "Somebody" and first["from"] == OTHER
    assert edited["edited"] and not edited["deleted"]
    assert deleted["deleted"] and deleted["text"] == ""
    assert files["attachments"] == 2


def test_read_messages_window_is_since_inclusive_until_exclusive(settings: Settings, data: Path) -> None:
    server = _server(settings, data)
    assert [
        m["id"] for m in _call(server, "read_messages", thread_id=PRIVATE, since="2026-07-02")["messages"]
    ] == ["2", "3", "4"]
    window = _call(server, "read_messages", thread_id=PRIVATE, since="2026-07-02", until="2026-07-02")
    assert [m["id"] for m in window["messages"]] == ["2", "3"]  # a date covers its whole day
    assert [
        m["id"]
        for m in _call(server, "read_messages", thread_id=PRIVATE, until="2026-07-02T09:00:00Z")["messages"]
    ] == ["1"]


def test_read_messages_cursor_resumes_with_no_gap_and_no_repeat(settings: Settings, data: Path) -> None:
    server = _server(settings, data)
    seen: list[str] = []
    since = ""
    for _ in range(10):
        page = (
            _call(server, "read_messages", thread_id=PRIVATE, since=since, limit=2)
            if since
            else _call(server, "read_messages", thread_id=PRIVATE, limit=2)
        )
        seen += [m["id"] for m in page["messages"]]
        if "next_since" not in page:
            break
        since = page["next_since"]
    # Messages 2 and 3 share a time: the page that reaches them takes both, so the cursor can
    # never land between them.
    assert seen == ["1", "2", "3", "4"]


def test_read_messages_refuses_an_unknown_id_without_building_a_path(
    settings: Settings, data: Path, monkeypatch
) -> None:
    server = _server(settings, data)
    touched: list[Path] = []
    real_is_file = Path.is_file

    def spy(self: Path) -> bool:
        touched.append(self)
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", spy)
    out = _call(server, "read_messages", thread_id="../../etc/passwd")
    assert out["error"] and "unknown chat" in out["text"]
    assert all("etc" not in str(p) for p in touched)


def test_an_indexed_chat_without_a_store_is_empty_not_an_error(settings: Settings, data: Path) -> None:
    out = _call(_server(settings, data), "read_messages", thread_id=MEETING)
    assert out["messages"] == [] and not out["error"]


def test_no_archive_creates_nothing_and_transcripts_name_the_path(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    absent = tmp_path / "nowhere"
    server = _server(settings, absent)
    _dead_session(server, monkeypatch)
    out = _call(server, "read_transcript", thread_id=PRIVATE)
    assert out["error"] and "unavailable" in out["text"] and str(absent) in out["text"]
    refused = _call(server, "find_chats")  # falls back to the API, which needs a sign-in
    assert refused["error"] and "not signed in" in refused["text"]
    assert not absent.exists()


def test_stdout_lines_are_single_json_objects_even_with_newlines_in_text(
    settings: Settings, data: Path
) -> None:
    root = data
    store = ChatStore(root, PRIVATE)
    store.insert_page([_msg("5", "2026-07-05T09:00:00Z", content="line one<br>line two\nline three")])
    store.close()
    out = io.StringIO()
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "read_messages", "arguments": {"thread_id": PRIVATE}},
    }
    M.Server(settings, root, stdin=io.StringIO(json.dumps(request) + "\n"), stdout=out).serve()
    lines = out.getvalue().splitlines()
    assert len(lines) == 1
    text = json.loads(json.loads(lines[0])["result"]["content"][0]["text"])["messages"][-1]["text"]
    assert text == "line one line two line three"


def test_the_cursor_keeps_sub_second_order(settings: Settings, data: Path) -> None:
    store = ChatStore(data, GROUP)
    times = [f"2026-07-06T10:00:00.{n:07d}Z" for n in (500000, 1000000, 1500000, 2000000, 2500000)]
    store.insert_page([_msg(f"g{n}", t) for n, t in enumerate(times)])
    store.close()
    server = _server(settings, data)
    seen: list[str] = []
    page = _call(server, "read_messages", thread_id=GROUP, limit=2)
    for _ in range(4):  # a cursor that does not advance must fail, not loop
        seen += [m["id"] for m in page["messages"]]
        if "next_since" not in page:
            break
        page = _call(server, "read_messages", thread_id=GROUP, since=page["next_since"], limit=2)
    assert seen == ["g0", "g1", "g2", "g3", "g4"]


def test_text_is_cut_at_one_thousand_characters(settings: Settings, data: Path) -> None:
    store = ChatStore(data, GROUP)
    store.insert_page([_msg("long", "2026-07-07T10:00:00Z", content="x" * 1500)])
    store.close()
    out = _call(_server(settings, data), "read_messages", thread_id=GROUP)
    assert len(out["messages"][0]["text"]) == 1000


def test_a_crashing_handler_answers_internal_error_and_the_server_goes_on(
    settings: Settings, data: Path, monkeypatch
) -> None:
    def boom(server: M.Server, args: dict[str, Any]) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(M.TOOLS[0], "handler", boom)
    server = _server(settings, data)
    response = _rpc(server, "tools/call", {"name": "find_chats", "arguments": {}})
    assert response["error"]["code"] == -32603 and "disk on fire" in response["error"]["message"]
    assert _rpc(server, "ping")["result"] == {}


# --- search_messages ---


@pytest.fixture
def searchable(data: Path) -> Path:
    """Messages in three chats plus a feed copy, so pruning and the `48:` rule are visible."""
    index = Index(data)
    index.upsert_chat("48:mentions", label="Mentions", raw=_conv("2026-07-03T10:00:00Z"))
    index.upsert_chat("48:notes", label="Notes to self", raw=_conv("2026-07-03T10:00:00Z"))
    index.close()
    for thread, when, text in (
        (GROUP, "2026-07-01T10:00:00Z", "the <b>Budget</b> review is Friday"),
        (MEETING, "2026-07-02T10:00:00Z", "budgét approved"),
        ("48:mentions", "2026-07-03T10:00:00Z", "the Budget review is Friday"),  # a feed copy
        ("48:notes", "2026-07-03T11:00:00Z", "remember the budget"),
        (PRIVATE, "2026-06-01T10:00:00Z", "old budget talk"),
    ):
        store = ChatStore(data, thread)
        store.insert_page([_msg(f"{thread}-s", when, content=text)])
        store.close()
    return data


def test_search_is_case_and_accent_insensitive_newest_first_and_skips_feeds(
    settings: Settings, searchable: Path
) -> None:
    out = _call(_server(settings, searchable), "search_messages", query="BUDGET", since="2026-06-01")
    assert out["source"] == "archive" and out["since"] == "2026-06-01T00:00:00"
    assert [(m["thread_id"], m["chat"]) for m in out["messages"]] == [
        ("48:notes", "Notes to self"),
        (MEETING, "Weekly · 2p"),
        (GROUP, "Paris office · 3p"),
        (PRIVATE, "Élodie BERNARD"),
    ]
    assert out["messages"][2]["text"] == "the Budget review is Friday" and not out["more"]


def test_search_prunes_by_last_activity_and_takes_one_chat(settings: Settings, searchable: Path) -> None:
    server = _server(settings, searchable)
    window = _call(server, "search_messages", query="budget", since="2026-07-02")
    assert [m["thread_id"] for m in window["messages"]] == ["48:notes", MEETING]
    assert window["chats_scanned"] == 3  # PRIVATE (last activity in July) is opened, GROUP is not
    one = _call(server, "search_messages", query="budget", since="2026-01-01", thread_id="48:mentions")
    assert [m["thread_id"] for m in one["messages"]] == ["48:mentions"]  # asked for by id: read
    assert one["chats_scanned"] == 1


def test_search_matches_stripped_text_not_markup(settings: Settings, searchable: Path) -> None:
    store = ChatStore(searchable, GROUP)
    store.insert_page(
        [_msg("markup", "2026-07-01T11:00:00Z", content='<a href="https://x/secretword">link</a>')]
    )
    store.close()
    out = _call(_server(settings, searchable), "search_messages", query="secretword", since="2026-01-01")
    assert out["messages"] == []


def test_search_default_window_is_seven_days_and_limit_reports_more(
    settings: Settings, searchable: Path, monkeypatch
) -> None:
    class _Now(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # noqa: ANN401
            return datetime(2026, 7, 5, 12, 0, tzinfo=tz)

    monkeypatch.setattr(M, "datetime", _Now)
    server = _server(settings, searchable)
    out = _call(server, "search_messages", query="budget")
    assert out["since"] == "2026-06-28T12:00:00"
    assert [m["thread_id"] for m in out["messages"]] == ["48:notes", MEETING, GROUP]
    cut = _call(server, "search_messages", query="budget", limit=1)
    assert [m["thread_id"] for m in cut["messages"]] == ["48:notes"] and cut["more"]


# --- read_transcript ---


@pytest.fixture
def recorded(data: Path) -> Path:
    media = data / MEETING / "media"
    media.mkdir(parents=True)
    (media / "sp-abc.transcript.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "id": "g/1",
                        "text": "hello",
                        "speakerDisplayName": "Anna",
                        "speakerId": "a@t",
                        "startOffset": "00:00:01",
                    },
                    {
                        "id": "g/2",
                        "text": "hi",
                        "speakerDisplayName": "",
                        "speakerId": "b@t",
                        "startOffset": "00:00:03",
                    },
                    {
                        "id": "g/3",
                        "text": "bye",
                        "speakerDisplayName": "Anna",
                        "speakerId": "a@t",
                        "startOffset": "00:00:09",
                    },
                ]
            }
        )
    )
    (media / "recordings.json").write_text(
        json.dumps(
            [
                {
                    "title": "Weekly",
                    "duration": "0:10:00",
                    "videos": ["Weekly.mp4"],
                    "transcripts": ["sp-abc.transcript.json", "sp-abc.transcript.vtt"],
                    "message_id": "r1",
                    "composetime": "2026-07-02T11:00:00Z",
                },
                {
                    "title": "Weekly again",
                    "duration": "0:05:00",
                    "videos": [],
                    "transcripts": ["x.transcript.vtt"],
                    "message_id": "r2",
                    "composetime": "2026-07-09T11:00:00Z",
                },
            ]
        )
    )
    return data


def test_transcript_lists_recordings_then_pages_turns(settings: Settings, recorded: Path) -> None:
    server = _server(settings, recorded)
    listing = _call(server, "read_transcript", thread_id=MEETING)
    assert listing["recordings"] == [
        {
            "message_id": "r1",
            "time": "2026-07-02T11:00:00Z",
            "title": "Weekly",
            "duration": "0:10:00",
            "has_transcript": True,
            "videos": 1,
        },
        {
            "message_id": "r2",
            "time": "2026-07-09T11:00:00Z",
            "title": "Weekly again",
            "duration": "0:05:00",
            "has_transcript": False,
            "videos": 0,
        },
    ]
    page = _call(server, "read_transcript", thread_id=MEETING, message_id="r1", limit=2)
    assert page["total"] == 3 and page["next_offset"] == 2 and page["title"] == "Weekly"
    assert page["turns"] == [
        {"speaker": "Anna", "start": "00:00:01", "text": "hello"},
        {"speaker": "b@t", "start": "00:00:03", "text": "hi"},  # no display name: the id, never dropped
    ]
    rest = _call(server, "read_transcript", thread_id=MEETING, message_id="r1", offset=2, limit=2)
    assert [t["text"] for t in rest["turns"]] == ["bye"] and "next_offset" not in rest


def test_transcript_errors(settings: Settings, recorded: Path) -> None:
    server = _server(settings, recorded)
    assert _call(server, "read_transcript", thread_id=GROUP)["recordings"] == []  # no manifest: no recordings
    vtt_only = _call(server, "read_transcript", thread_id=MEETING, message_id="r2")
    assert vtt_only["error"] and "no transcript on disk" in vtt_only["text"]
    unknown = _call(server, "read_transcript", thread_id=MEETING, message_id="nope")
    assert unknown["error"] and "no recording" in unknown["text"]


def test_transcript_name_cannot_leave_the_media_folder(settings: Settings, recorded: Path) -> None:
    manifest = recorded / MEETING / "media" / "recordings.json"
    entries = json.loads(manifest.read_text())
    entries[0]["transcripts"] = ["../../../../etc/passwd.transcript.json"]
    manifest.write_text(json.dumps(entries))
    out = _call(_server(settings, recorded), "read_transcript", thread_id=MEETING, message_id="r1")
    assert out["error"] and "outside the media folder" in out["text"]


# --- sign-in ---


class _Source:
    """A TokenSource double: `state` is live, dead or down; the device flow ends on `done`."""

    def __init__(self, state: str = "live") -> None:
        self.state = state
        self.flows: list[dict[str, Any]] = []
        self.done = threading.Event()
        self.outcome: dict[str, Any] = {"access_token": "a", "id_token": "b"}

    def refresh(self) -> dict[str, Any]:
        if self.state == "dead":
            raise AuthExpired("dead")
        if self.state == "down":
            raise AuthUnavailable("throttled")
        return {"access_token": "a", "id_token": "b", "id_token_claims": {"preferred_username": "me@example"}}

    def start_device_flow(self) -> dict[str, Any]:
        flow = {
            "user_code": f"CODE{len(self.flows)}",
            "verification_uri_complete": "https://login.microsoftonline.com/x?otc=CODE",
            "expires_at": time.time() + 900,
            "device_code": "secret-device-code",
        }
        self.flows.append(flow)
        return flow

    def finish_device_flow(self, flow: dict[str, Any]) -> dict[str, Any]:
        self.done.wait(5)
        if "access_token" in self.outcome:
            self.state = "live"
        return dict(self.outcome)

    def sharepoint_token(self, host: str) -> None:
        return None

    def graph_token(self) -> None:
        return None

    def ic3_token(self) -> str | None:
        return "ic3" if self.state == "live" else None


def _dead_session(server: M.Server, monkeypatch, state: str = "dead") -> _Source:
    source = _Source(state)
    monkeypatch.setattr(M, "token_source", lambda settings: source)
    monkeypatch.setattr(
        AR,
        "exchange_skype_token",
        lambda settings, token: {"skype_token": "sk", "expires_in": 3600, "region": "emea"},
    )
    return source


def test_login_with_a_live_credential_starts_no_flow(settings: Settings, data: Path, monkeypatch) -> None:
    server = _server(settings, data)
    source = _dead_session(server, monkeypatch, state="live")
    assert _call(server, "login") == {"error": False, "state": "signed_in", "account": "me@example"}
    assert source.flows == []


def test_a_dead_credential_offers_one_device_flow_until_it_completes(
    settings: Settings, data: Path, monkeypatch
) -> None:
    server = _server(settings, data)
    source = _dead_session(server, monkeypatch)
    server.archive._down_until = float("inf")  # archive gone: reads need the API
    refused = _call(server, "find_chats")
    assert refused["error"] and "CODE0" in refused["text"] and "login.microsoftonline.com" in refused["text"]
    assert "secret-device-code" not in refused["text"]
    pending = _call(server, "login")
    assert pending["state"] == "pending" and pending["code"] == "CODE0" and 0 < pending["expires_in"] <= 900
    assert len(source.flows) == 1  # one flow for the error and the login alike
    assert _call(server, "read_transcript", thread_id=PRIVATE)["error"]  # meanwhile: answered at once
    source.done.set()
    for _ in range(50):
        if source.state == "live":
            break
        time.sleep(0.02)
    assert _call(server, "login")["state"] == "signed_in"
    monkeypatch.setattr(
        M.ApiReader, "find_chats", lambda self, *a: {"chats": [], "labels_resolved": 0, "more": False}
    )
    assert _call(server, "find_chats")["source"] == "api"  # the refused tool now runs, no restart


def test_a_refused_sign_in_is_reported_and_the_next_call_starts_over(
    settings: Settings, data: Path, monkeypatch
) -> None:
    server = _server(settings, data)
    source = _dead_session(server, monkeypatch)
    source.outcome = {"error": "access_denied", "error_description": "user said no"}
    assert _call(server, "login")["state"] == "pending"
    source.done.set()
    for _ in range(50):
        if server.session._flow_result is not None:
            break
        time.sleep(0.02)
    refused = _call(server, "login")
    assert refused["error"] and "access_denied" in refused["text"] and "user said no" in refused["text"]
    assert _call(server, "login")["code"] == "CODE1"  # a new flow, a new code


def test_an_expired_flow_is_dropped_for_a_new_one(settings: Settings, data: Path, monkeypatch) -> None:
    server = _server(settings, data)
    source = _dead_session(server, monkeypatch)
    assert _call(server, "login")["code"] == "CODE0"
    source.flows[0]["expires_at"] = time.time() - 1
    assert _call(server, "login")["code"] == "CODE1"


def test_a_throttled_microsoft_starts_no_flow(settings: Settings, data: Path, monkeypatch) -> None:
    server = _server(settings, data)
    source = _dead_session(server, monkeypatch, state="down")
    out = _call(server, "login")
    assert out["error"] and "retry" in out["text"] and source.flows == []


def test_tools_stay_listed_whatever_the_state(settings: Settings, data: Path, monkeypatch) -> None:
    server = _server(settings, data)
    _dead_session(server, monkeypatch)
    before = [t["name"] for t in _rpc(server, "tools/list")["result"]["tools"]]
    _call(server, "login")
    assert [t["name"] for t in _rpc(server, "tools/list")["result"]["tools"]] == before
    assert "notifications/tools/list_changed" not in server._out.getvalue()  # type: ignore[attr-defined]


# --- availability probe ---


def test_a_hung_archive_falls_back_within_the_deadline_and_holds(
    settings: Settings, data: Path, monkeypatch
) -> None:
    monkeypatch.setattr(M, "_PROBE_DEADLINE", 0.2)
    monkeypatch.setattr(M, "_PROBE_HOLD", 0.5)
    server = _server(settings, data)
    probes = []

    def hang() -> None:
        probes.append(time.monotonic())
        time.sleep(2)

    server.archive._probe = hang
    started = time.monotonic()
    assert server.archive.available() is False
    assert time.monotonic() - started < 1.0
    assert server.archive.available() is False and len(probes) == 1  # held: no second probe
    time.sleep(0.6)
    server.archive._probe = server.archive._open_index
    assert server.archive.available() is True and len(probes) == 1  # back after the hold


def test_a_readable_archive_that_lacks_the_chat_stays_the_source(
    settings: Settings, data: Path, monkeypatch
) -> None:
    server = _server(settings, data)
    monkeypatch.setattr(M.Server, "api", lambda self: (_ for _ in ()).throw(AssertionError("API called")))
    out = _call(server, "read_messages", thread_id="19:unknown@thread.v2")
    assert out["error"] and "unknown chat" in out["text"]


# --- API fallback ---


def _api(
    monkeypatch, conversations: list[dict[str, Any]], history: dict[str, list[dict[str, Any]]]
) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(API, "fetch_conversations", lambda settings, token: iter([conversations]))

    def pages(settings, token, thread_id, page_size, max_pages, end_before=None):  # noqa: ANN001
        calls.append(thread_id)
        msgs = sorted(history.get(thread_id, []), key=lambda m: m["composetime"], reverse=True)
        if end_before is not None:
            msgs = [m for m in msgs if _epoch_seconds(m["composetime"]) <= end_before]
        for i in range(0, len(msgs), page_size):
            yield msgs[i : i + page_size]

    monkeypatch.setattr(API, "iter_history_pages", pages)

    async def label(self: Any, thread_id: str) -> str:
        return {PRIVATE: "Élodie BERNARD", GROUP: "Paris office · 3p"}.get(thread_id, thread_id)

    monkeypatch.setattr(API.Directory, "label", label)
    return calls


def _gone(settings: Settings, tmp_path: Path, monkeypatch) -> M.Server:
    server = _server(settings, tmp_path / "nowhere")
    _dead_session(server, monkeypatch, state="live")
    return server


def test_api_find_chats_matches_label_and_topic_only(settings: Settings, tmp_path: Path, monkeypatch) -> None:
    convs = [
        {"id": PRIVATE, "lastMessage": {"composetime": "2026-07-03T09:00:00Z"}},
        {
            "id": GROUP,
            "threadProperties": {"topic": "Paris office"},
            "lastMessage": {"composetime": "2026-07-01T09:00:00Z"},
        },
        {"id": "19:ch@thread.tacv2", "lastMessage": {"composetime": "2026-06-01T09:00:00Z"}},
    ]
    _api(monkeypatch, convs, {})
    server = _gone(settings, tmp_path, monkeypatch)
    out = _call(server, "find_chats")
    assert out["source"] == "api" and "archiver_seen" not in out
    assert [(c["id"], c["label"], c["kind"]) for c in out["chats"]] == [
        (PRIVATE, "Élodie BERNARD", "private"),
        (GROUP, "Paris office · 3p", "group"),
        ("19:ch@thread.tacv2", "19:ch@thread.tacv2", "channel"),
    ]
    assert "participants" in out["chats"][0] and "backfill_done" not in out["chats"][0]
    assert [c["id"] for c in _call(server, "find_chats", query="paris")["chats"]] == [GROUP]
    assert [c["id"] for c in _call(server, "find_chats", participant="bernard")["chats"]] == [PRIVATE]
    assert [c["id"] for c in _call(server, "find_chats", since="2026-07-02")["chats"]] == [PRIVATE]


def test_api_read_messages_defaults_to_seven_days_and_says_so(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    class _Now(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # noqa: ANN401
            return datetime(2026, 7, 10, 12, 0, tzinfo=tz)

    monkeypatch.setattr(M, "datetime", _Now)
    history = {
        PRIVATE: [
            _msg("old", "2026-06-01T09:00:00Z"),
            _msg("a", "2026-07-05T09:00:00Z"),
            _msg("b", "2026-07-06T09:00:00Z"),
        ]
    }
    calls = _api(monkeypatch, [], history)
    server = _gone(settings, tmp_path, monkeypatch)
    out = _call(server, "read_messages", thread_id=PRIVATE)
    assert out["source"] == "api" and out["since"] == "2026-07-03T12:00:00"
    assert [m["id"] for m in out["messages"]] == ["a", "b"] and calls == [PRIVATE]
    cut = _call(server, "read_messages", thread_id=PRIVATE, since="2026-01-01", limit=2)
    assert [m["id"] for m in cut["messages"]] == ["old", "a"] and cut["next_since"] == "2026-07-06T09:00:00Z"


def test_api_search_reads_active_chats_and_reports_the_rest(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(API, "_MAX_CHATS", 1)
    convs = [
        {"id": PRIVATE, "lastMessage": {"composetime": "2026-07-03T09:00:00Z"}},
        {"id": "48:mentions", "lastMessage": {"composetime": "2026-07-03T09:00:00Z"}},  # a feed: skipped
        {"id": GROUP, "lastMessage": {"composetime": "2026-07-01T09:00:00Z"}},
    ]
    history = {
        PRIVATE: [_msg("p", "2026-07-02T09:00:00Z", content="the <b>budget</b>")],
        GROUP: [_msg("g", "2026-07-01T08:00:00Z", content="budget")],
    }
    calls = _api(monkeypatch, convs, history)
    server = _gone(settings, tmp_path, monkeypatch)
    out = _call(server, "search_messages", query="BUDGET", since="2026-06-30")
    assert out["source"] == "api"
    assert [m["id"] for m in out["messages"]] == ["p"] and calls == [PRIVATE]
    assert out["chats_scanned"] == 1 and out["chats_left_out"] == 1 and out["more"]


# --- write tools ---


def _writes(monkeypatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def fake_send(settings, token, thread, text, name, *, is_html=False):  # noqa: ANN001
        calls.append(("send", thread, text, name, is_html))
        return {"clientmessageid": "cm1", "status": 201}

    def fake_edit(settings, token, thread, mid, text, *, is_html=False):  # noqa: ANN001
        calls.append(("edit", thread, mid, text, is_html))
        return {"status": 200}

    monkeypatch.setattr(M, "send_message", fake_send)
    monkeypatch.setattr(M, "edit_message", fake_edit)
    return calls


def test_send_and_update_go_out_once_with_the_exact_text(settings: Settings, data: Path, monkeypatch) -> None:
    server = _server(settings, data)
    _dead_session(server, monkeypatch, state="live")
    calls = _writes(monkeypatch)
    out = _call(server, "send_message", thread_id=PRIVATE, text="héllo <there>\nline")
    assert out == {"error": False, "thread_id": PRIVATE, "clientmessageid": "cm1", "status": 201}
    out = _call(server, "update_message", thread_id="48:notes", message_id="m9", text="fixed")
    assert out == {"error": False, "thread_id": "48:notes", "message_id": "m9", "status": 200}
    assert calls == [
        ("send", PRIVATE, "héllo <there>\nline", "", False),
        ("edit", "48:notes", "m9", "fixed", False),
    ]


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({"thread_id": PRIVATE, "text": "   "}, "non-empty"),
        ({"thread_id": PRIVATE, "text": "<" * 30000}, "28 KB"),
        ({"thread_id": "19:invented@thread.v2", "text": "hi"}, "unknown chat"),
    ],
)
def test_a_refused_send_makes_no_call(
    settings: Settings, data: Path, monkeypatch, args: dict[str, Any], reason: str
) -> None:
    server = _server(settings, data)
    _dead_session(server, monkeypatch, state="live")
    calls = _writes(monkeypatch)
    out = _call(server, "send_message", **args)
    assert out["error"] and reason in out["text"] and calls == []


def test_a_non_string_text_is_invalid_params(settings: Settings, data: Path, monkeypatch) -> None:
    calls = _writes(monkeypatch)
    response = _rpc(
        _server(settings, data),
        "tools/call",
        {"name": "send_message", "arguments": {"thread_id": PRIVATE, "text": 5}},
    )
    assert response["error"]["code"] == -32602 and calls == []


def test_a_dead_credential_sends_nothing_and_offers_the_login(
    settings: Settings, data: Path, monkeypatch
) -> None:
    server = _server(settings, data)
    _dead_session(server, monkeypatch)
    calls = _writes(monkeypatch)
    out = _call(server, "send_message", thread_id=PRIVATE, text="hi")
    assert out["error"] and "not signed in" in out["text"] and "CODE0" in out["text"] and calls == []


def test_with_the_archive_gone_the_thread_is_checked_on_the_api(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    server = _gone(settings, tmp_path, monkeypatch)
    calls = _writes(monkeypatch)
    known = {PRIVATE: {"topic": None, "members": []}}

    async def thread(self: Any, thread_id: str) -> dict[str, Any] | None:
        return known.get(thread_id)

    monkeypatch.setattr(API.Directory, "thread", thread)
    assert not _call(server, "send_message", thread_id=PRIVATE, text="hi")["error"]
    refused = _call(server, "send_message", thread_id="19:invented@thread.v2", text="hi")
    assert refused["error"] and "unknown chat" in refused["text"]
    assert [c[1] for c in calls] == [PRIVATE]


def test_teams_errors_become_tool_errors_without_the_token(
    settings: Settings, data: Path, monkeypatch
) -> None:
    import httpx

    server = _server(settings, data)
    _dead_session(server, monkeypatch, state="live")

    def refused(settings, token, thread, text, name, *, is_html=False):  # noqa: ANN001
        req = httpx.Request("POST", "https://msg.example/x")
        raise httpx.HTTPStatusError("403", request=req, response=httpx.Response(403, request=req))

    monkeypatch.setattr(M, "send_message", refused)
    out = _call(server, "send_message", thread_id="48:notes", text="hi")
    assert out["error"] and "403" in out["text"] and "sk" not in out["text"].split()
    monkeypatch.setattr(M, "send_message", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    assert "network" in _call(server, "send_message", thread_id="48:notes", text="hi")["text"]


def test_read_only_refuses_the_write_tools_before_any_call(
    settings: Settings, data: Path, monkeypatch
) -> None:
    server = _server(settings, data, read_only=True)
    _dead_session(server, monkeypatch, state="live")
    calls = _writes(monkeypatch)
    for name, args in (
        ("send_message", {"thread_id": "48:notes", "text": "hi"}),
        ("update_message", {"thread_id": "48:notes", "message_id": "1", "text": "x"}),
    ):
        response = _rpc(server, "tools/call", {"name": name, "arguments": args})
        assert response["error"]["code"] == -32602
    assert calls == []


def test_an_unknown_thread_is_refused_before_any_token_is_minted(
    settings: Settings, data: Path, monkeypatch
) -> None:
    server = _server(settings, data)
    source = _dead_session(server, monkeypatch)  # a refresh here would start a device flow
    calls = _writes(monkeypatch)
    out = _call(server, "send_message", thread_id="19:invented@thread.v2", text="hi")
    assert out["error"] and "unknown chat" in out["text"]
    assert source.flows == [] and calls == []


# --- scheduling ---

NOTES_THREAD_ID = "19:teamsstream_notes_x@thread.v2"


class _Drafts:
    """The drafts store behind the proxy host: rows in memory, requests recorded."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self.fail: int | None = None

    def add(
        self, draft_id: str, thread: str, send_at_ms: int, content: str = "<p>hi</p>", **extra: Any
    ) -> None:
        self.rows[draft_id] = {
            "id": draft_id,
            "clientmessageid": f"cm-{draft_id}",
            "innerThreadId": thread,
            "draftType": "ScheduledDraft",
            "draftDetails": {"sendAt": str(send_at_ms)},
            "content": content,
            "properties": {"draftId": f"cm-{draft_id}"},
            **extra,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert (
            request.url.host == "teams.cloud.microsoft" and request.headers["Authorization"] == "Bearer ic3"
        )
        assert request.url.path.startswith("/api/chatsvc/emea/v1/users/ME/drafts")
        self.requests.append((request.method, request.url.path.rsplit("/", 1)[-1]))
        if self.fail:
            return httpx.Response(self.fail, json={"errorCode": self.fail, "message": "nope"})
        draft_id = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET" and draft_id == "drafts":
            return httpx.Response(200, json={"drafts": list(self.rows.values())})
        if request.method == "GET":
            row = self.rows.get(draft_id)
            return httpx.Response(200, json=row) if row else httpx.Response(403, json={"errorCode": 209})
        if request.method == "POST":
            body = json.loads(request.content)
            new_id = str(1_800_000_000_000 + len(self.rows))
            self.rows[new_id] = {
                **body["message"],
                "id": new_id,
                "innerThreadId": body["innerThreadId"],
                "draftType": body["draftType"],
                "draftDetails": body["draftDetails"],
            }
            return httpx.Response(201, json={"OriginalArrivalTime": int(new_id)})
        if request.method == "PUT":
            body = json.loads(request.content)
            self.rows[draft_id] = {
                **self.rows[draft_id],
                **body["message"],
                "draftDetails": body["draftDetails"],
            }
            return httpx.Response(200, text="")
        if request.method == "DELETE":
            row = self.rows[draft_id]
            row["content"] = ""
            row["properties"] = {"deletetime": "1"}
            return httpx.Response(200, text="null")
        raise AssertionError(request.method)

    def install(self, monkeypatch) -> _Drafts:
        transport = httpx.MockTransport(self.handler)
        monkeypatch.setattr(D.httpx, "Client", lambda **kw: _REAL_CLIENT(transport=transport, **kw))
        monkeypatch.setattr(M, "notes_thread_id", lambda settings, token: NOTES_THREAD_ID)
        monkeypatch.setattr(M, "self_mri", lambda bearer: "8:orgid:me")
        return self


def _scheduling(
    settings: Settings, data: Path, monkeypatch
) -> tuple[M.Server, _Drafts, list[tuple[Any, ...]]]:
    server = _server(settings, data)
    _dead_session(server, monkeypatch, state="live")
    calls = _writes(monkeypatch)
    return server, _Drafts().install(monkeypatch), calls


def _in(**delta: Any) -> str:
    return (datetime.now(UTC) + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_send_at_schedules_instead_of_sending(settings: Settings, data: Path, monkeypatch) -> None:
    server, drafts, calls = _scheduling(settings, data, monkeypatch)
    out = _call(server, "send_message", thread_id=PRIVATE, text="later <b>", send_at=_in(days=2))
    assert not out["error"] and out["scheduled_id"] and out["send_at"].endswith("Z")
    assert calls == []  # nothing sent now
    (row,) = drafts.rows.values()
    assert row["innerThreadId"] == PRIVATE and row["content"] == "later &lt;b&gt;"
    assert row["properties"]["draftId"] == row["clientmessageid"] and row["from"] == "8:orgid:me"
    assert [r[0] for r in drafts.requests] == ["GET", "POST"]  # the listing, for a free client id


def test_notes_alias_is_resolved_for_scheduling(settings: Settings, data: Path, monkeypatch) -> None:
    server, drafts, _ = _scheduling(settings, data, monkeypatch)
    _call(server, "send_message", thread_id="48:notes", text="note", send_at=_in(days=1))
    (row,) = drafts.rows.values()
    assert row["innerThreadId"] == NOTES_THREAD_ID


@pytest.mark.parametrize(
    ("send_at", "reason"),
    [
        ("tomorrow", "ISO 8601"),
        ("2030-01-01T10:00:00", "offset"),
        (_in(seconds=2), "5 seconds"),
        (_in(days=-1), "5 seconds"),
        (_in(days=126), "125 days"),
    ],
)
def test_bad_send_at_is_refused_before_any_call(
    settings: Settings, data: Path, monkeypatch, send_at: str, reason: str
) -> None:
    server, drafts, calls = _scheduling(settings, data, monkeypatch)
    out = _call(server, "send_message", thread_id=PRIVATE, text="x", send_at=send_at)
    assert out["error"] and reason in out["text"]
    assert drafts.requests == [] and calls == []


def test_send_at_inside_the_bounds_goes_to_teams(settings: Settings, data: Path, monkeypatch) -> None:
    server, drafts, _ = _scheduling(settings, data, monkeypatch)
    for send_at in (_in(seconds=8), _in(days=124, hours=23), _in(days=122)):
        assert not _call(server, "send_message", thread_id=PRIVATE, text="x", send_at=send_at)["error"]
    assert len(drafts.rows) == 3


def test_a_teams_refusal_of_send_at_is_passed_on(settings: Settings, data: Path, monkeypatch) -> None:
    server, drafts, _ = _scheduling(settings, data, monkeypatch)
    drafts.fail = 400
    out = _call(server, "send_message", thread_id=PRIVATE, text="x", send_at=_in(days=1))
    assert out["error"] and "400" in out["text"] and "ic3" not in out["text"]


def test_a_new_draft_never_reuses_a_worn_client_id(settings: Settings, data: Path, monkeypatch) -> None:
    server, drafts, _ = _scheduling(settings, data, monkeypatch)
    fixed = 1_700_000_000_000
    monkeypatch.setattr(M.time, "time", lambda: fixed / 1000)
    drafts.add("old", PRIVATE, fixed + 10, content="", skypeeditedid=str(fixed))  # cancelled, id worn
    _call(server, "send_message", thread_id=PRIVATE, text="x", send_at=_in(days=1))
    new = [r for k, r in drafts.rows.items() if k != "old"][0]
    assert new["clientmessageid"] != str(fixed) and new["clientmessageid"].startswith(str(fixed))


def test_list_scheduled_keeps_pending_only(settings: Settings, data: Path, monkeypatch) -> None:
    server, drafts, _ = _scheduling(settings, data, monkeypatch)
    now_ms = int(time.time() * 1000)
    drafts.add("p1", PRIVATE, now_ms + 7_200_000, content="<p>see you</p>")
    drafts.add("p2", GROUP, now_ms + 3_600_000)
    drafts.add("sent", PRIVATE, now_ms - 60_000)
    drafts.add("gone", PRIVATE, now_ms + 60_000, content="")
    out = _call(server, "list_scheduled")
    assert [(d["id"], d["chat"], d["text"]) for d in out["scheduled"]] == [
        ("p2", "Paris office · 3p", "hi"),
        ("p1", "Élodie BERNARD", "see you"),
    ]
    assert [d["id"] for d in _call(server, "list_scheduled", thread_id=PRIVATE)["scheduled"]] == ["p1"]
    drafts.rows.clear()
    assert _call(server, "list_scheduled")["scheduled"] == []


def test_cancel_reads_the_state_first(settings: Settings, data: Path, monkeypatch) -> None:
    server, drafts, _ = _scheduling(settings, data, monkeypatch)
    now_ms = int(time.time() * 1000)
    drafts.add("p1", PRIVATE, now_ms + 3_600_000)
    wrong = _call(server, "cancel_scheduled", thread_id=GROUP, message_id="p1")
    assert wrong["error"] and "not aimed at" in wrong["text"]
    assert _call(server, "cancel_scheduled", thread_id=PRIVATE, message_id="p1")["cancelled"]
    assert drafts.rows["p1"]["content"] == ""
    again = _call(server, "cancel_scheduled", thread_id=PRIVATE, message_id="p1")
    assert again["error"] and "not pending: cancelled" in again["text"]
    assert [r for r in drafts.requests if r[0] == "DELETE"] == [("DELETE", "p1")]  # once
    unknown = _call(server, "cancel_scheduled", thread_id=PRIVATE, message_id="nope")
    assert unknown["error"] and "no scheduled message" in unknown["text"]


def test_update_moves_or_rewrites_and_keeps_the_pairing(settings: Settings, data: Path, monkeypatch) -> None:
    server, drafts, _ = _scheduling(settings, data, monkeypatch)
    now_ms = int(time.time() * 1000)
    drafts.add("p1", PRIVATE, now_ms + 3_600_000)
    drafts.rows["p1"]["properties"]["draftId"] = "knocked-apart"
    nothing = _call(server, "update_scheduled", thread_id=PRIVATE, message_id="p1")
    assert nothing["error"] and "nothing to change" in nothing["text"]
    moved = _call(server, "update_scheduled", thread_id=PRIVATE, message_id="p1", send_at=_in(days=3))
    assert not moved["error"] and drafts.rows["p1"]["content"] == "<p>hi</p>"
    assert int(drafts.rows["p1"]["draftDetails"]["sendAt"]) > now_ms + 2 * 86_400_000
    both = _call(
        server, "update_scheduled", thread_id=PRIVATE, message_id="p1", send_at=_in(days=4), text="new & text"
    )
    assert not both["error"] and drafts.rows["p1"]["content"] == "new &amp; text"
    assert drafts.rows["p1"]["properties"]["draftId"] == "cm-p1"
    assert [r[0] for r in drafts.requests if r[0] == "PUT"] == ["PUT", "PUT"]
    bad = _call(server, "update_scheduled", thread_id=PRIVATE, message_id="p1", send_at=_in(days=200))
    assert bad["error"] and "125 days" in bad["text"]


def test_scheduling_tools_leave_the_list_under_read_only(settings: Settings, data: Path) -> None:
    listed = [
        t["name"] for t in _rpc(_server(settings, data, read_only=True), "tools/list")["result"]["tools"]
    ]
    assert not {
        "send_message",
        "list_scheduled",
        "cancel_scheduled",
        "update_scheduled",
        "update_message",
    } & set(listed)
