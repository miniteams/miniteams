"""Chat-list filtering: date parsing, private-chat detection, window/probe logic."""

from datetime import UTC, datetime

from miniteams import chats as C
from miniteams.config import Settings


def test_parse_when_date_only_until_covers_whole_day() -> None:
    assert C.parse_when("2026-07-01", end=True) == datetime(2026, 7, 2, tzinfo=UTC)
    assert C.parse_when("2026-07-01") == datetime(2026, 7, 1, tzinfo=UTC)


def test_is_private() -> None:
    assert C.is_private("19:a_b@unq.gbl.spaces")
    assert C.is_private("19:groupchat@thread.v2")
    assert not C.is_private("19:meeting_abc@thread.v2")
    assert not C.is_private("19:channel@thread.tacv2")


def test_last_activity_prefers_message_time_falls_back_to_version() -> None:
    conv = {"lastMessage": {"originalarrivaltime": "2026-07-01T10:00:00.123Z"}}
    assert C.last_activity(conv) == "2026-07-01T10:00:00.123Z"
    assert C.last_activity({"version": 1751364000000}) == "2025-07-01T10:00:00Z"
    assert C.last_activity({}) == ""


async def test_list_chats_skips_empty_activity_and_version_only_bumps(
    settings: Settings, monkeypatch, capsys
) -> None:
    """A conv with no activity info must be skipped, not end the listing; a thread bumped by a
    membership-only event (recent version, old lastMessage) is filtered but doesn't stop paging."""
    conversations = [
        {"id": "19:noinfo@unq.gbl.spaces"},  # no lastMessage, no version
        {  # recent version bump, but newest message predates --since
            "id": "19:bumped@unq.gbl.spaces",
            "version": 1784332800000,  # 2026-07-18
            "lastMessage": {"composetime": "2026-01-05T09:00:00Z"},
        },
        {"id": "19:active@unq.gbl.spaces", "lastMessage": {"composetime": "2026-07-15T09:00:00Z"}},
    ]
    monkeypatch.setattr(C, "fetch_conversations", lambda s, tok: iter([conversations]))

    async def fake_label(self, thread_id: str) -> str:
        return "who"

    monkeypatch.setattr(C.Directory, "label", fake_label)

    await C.list_chats(
        settings,
        "sk",
        "",
        limit=0,
        since=C.parse_when("2026-07-01"),
        until=None,
        include_all=False,
        jsonl=False,
    )
    out = capsys.readouterr().out
    assert "19:active@unq.gbl.spaces" in out  # listing survived the two edge convs before it
    assert "19:noinfo@unq.gbl.spaces" not in out
    assert "19:bumped@unq.gbl.spaces" not in out


async def test_list_chats_window_probe(settings: Settings, monkeypatch, capsys) -> None:
    """--until in the past: recent-last-activity chat is probed; qualifies only if the
    newest in-window message is at/after --since."""
    conversations = [
        {"id": "19:recent@unq.gbl.spaces", "lastMessage": {"composetime": "2026-07-20T09:00:00Z"}},
        {"id": "19:channel@thread.tacv2", "lastMessage": {"composetime": "2026-07-10T09:00:00Z"}},
        {"id": "19:inwin@unq.gbl.spaces", "lastMessage": {"composetime": "2026-07-02T12:00:00Z"}},
        {"id": "19:old@unq.gbl.spaces", "lastMessage": {"composetime": "2026-06-01T09:00:00Z"}},
    ]
    monkeypatch.setattr(C, "fetch_conversations", lambda s, tok: iter([conversations]))
    # Probe: "recent" chat had an in-window message; called only for the recent chat.
    probed: list[str] = []

    def fake_history(s, tok, thread_id, page_size, max_pages, end_before=None):
        probed.append(thread_id)
        return [{"composetime": "2026-07-03T08:00:00Z"}]

    monkeypatch.setattr(C, "fetch_history", fake_history)

    async def fake_label(self, thread_id: str) -> str:
        return "who"

    monkeypatch.setattr(C.Directory, "label", fake_label)

    await C.list_chats(
        settings,
        "sk",
        "",
        limit=0,
        since=C.parse_when("2026-07-01"),
        until=C.parse_when("2026-07-05", end=True),
        include_all=False,
        jsonl=False,
    )
    out = capsys.readouterr().out
    assert probed == ["19:recent@unq.gbl.spaces"]
    assert "19:recent@unq.gbl.spaces" in out  # probe found in-window activity
    assert "19:inwin@unq.gbl.spaces" in out  # last activity already inside window
    assert "19:channel@thread.tacv2" not in out  # not a private chat
    assert "19:old@unq.gbl.spaces" not in out  # older than --since (also stops paging)
