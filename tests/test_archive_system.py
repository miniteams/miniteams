"""The `48:` conversations in the archive (spec 007): scope, feeds, media, live, widget, drafts."""

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_archive import _FakeApi, _msg, _run, _spy_log, _stored_ids, _wire

from miniteams import archive as AR
from miniteams import archive_live as AL
from miniteams import chats, directory
from miniteams import drafts as D
from miniteams import web as W
from miniteams.archive_store import ChatStore, Index
from miniteams.config import Settings

SRC = Path(AR.__file__).parent
HISTORY = [
    "48:annotations",
    "48:calllogs",
    "48:mentions",
    "48:notes",
    "48:notifications",
    "48:saved",
    "48:starred",
    "48:threads",
]
DRAFTS = "48:drafts"
NOTES = "48:notes"


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch) -> None:
    monkeypatch.setattr(AR, "_INTER_PAGE_DELAY", 0)


@pytest.mark.parametrize("thread_id", [*HISTORY, DRAFTS, "48:anything-teams-adds-later"])
def test_every_48_conversation_is_in_scope_without_a_flag(thread_id: str) -> None:
    assert chats.in_archive_scope(thread_id)
    assert chats.in_archive_scope(thread_id, include_all=True)


@pytest.mark.parametrize(
    ("thread_id", "default", "with_all"),
    [
        ("19:x@unq.gbl.spaces", True, True),
        ("19:g@thread.v2", True, True),
        ("19:meeting_abc@thread.v2", True, True),
        ("19:ch@thread.tacv2", False, True),
        ("8:orgid:48:notes", False, True),  # `48:` inside an id is not the prefix
        ("148:notes", False, True),
        ("", False, False),
    ],
)
def test_scope_outside_the_48_prefix(thread_id: str, default: bool, with_all: bool) -> None:
    assert chats.in_archive_scope(thread_id) is default
    assert chats.in_archive_scope(thread_id, include_all=True) is with_all


def test_the_scope_rule_has_one_copy() -> None:
    for name in ("archive.py", "archive_live.py"):
        source = (SRC / name).read_text()
        assert not re.search(r"\bis_(private|meeting|system)\b", source), name
        assert not re.search(r"""startswith\(\s*["']48:""", source), name
        assert "in_archive_scope(" in source, name


def test_every_48_conversation_has_a_label() -> None:
    assert set(directory._SPECIAL_THREADS) == {*HISTORY, DRAFTS}
    assert all(label and not label.startswith("48:") for label in directory._SPECIAL_THREADS.values())


async def test_a_pass_archives_all_nine(settings: Settings, tmp_path, monkeypatch) -> None:
    api = _FakeApi({t: [_msg(f"{t}-1", "2026-07-01T09:00:00Z")] for t in HISTORY})
    _wire(monkeypatch, api, [*HISTORY, DRAFTS, "19:ch@thread.tacv2"])
    _DraftsApi(_page([_draft("d1")])).install(monkeypatch)
    data = tmp_path / "data"
    await _run(settings, data)

    index = Index(data)
    stored = {c["id"] for c in index.chats()}
    index.close()
    assert stored == {*HISTORY, DRAFTS}  # the channel stays behind --all
    for thread_id in HISTORY:
        assert _stored_ids(data, thread_id) == {f"{thread_id}-1"}
    assert _stored_ids(data, DRAFTS) == {"d1"}


async def test_a_second_pass_adds_nothing(settings: Settings, tmp_path, monkeypatch) -> None:
    api = _FakeApi({NOTES: [_msg("n1", "2026-07-01T09:00:00Z"), _msg("n2", "2026-07-02T09:00:00Z")]})
    conv = {"id": NOTES, "version": 7, "lastMessage": {"composetime": "2026-07-02T09:00:00Z"}}
    _wire(monkeypatch, api, [])
    monkeypatch.setattr(AR, "fetch_conversations", lambda s, tok: iter([[conv]]))
    data = tmp_path / "data"
    await _run(settings, data)
    first = len(api.calls)
    await _run(settings, data)

    assert _stored_ids(data, NOTES) == {"n1", "n2"}
    assert len(api.calls) == first  # unchanged conversation: no history call at all


async def test_an_empty_feed_gets_its_row_and_is_not_a_failure(
    settings: Settings, tmp_path, monkeypatch
) -> None:
    api = _FakeApi({})
    _wire(monkeypatch, api, ["48:saved"])
    data = tmp_path / "data"
    events = _spy_log(monkeypatch)
    await _run(settings, data)
    recap = next(kw for _, event, kw in events if event == "archive_recap")
    assert not any(event == "chat_archive_failed" for _, event, _ in events)
    assert (recap["chats"], recap["failed"], recap["denied"]) == (1, 0, 0)

    index = Index(data)
    rows = {c["id"]: c for c in index.chats()}
    index.close()
    assert rows["48:saved"]["backfill_done"]
    assert _stored_ids(data, "48:saved") == set()


@pytest.mark.parametrize("named", [False, True])
async def test_drafts_never_reach_the_history_call(
    settings: Settings, tmp_path, monkeypatch, named: bool
) -> None:
    api = _FakeApi({})
    _wire(monkeypatch, api, [DRAFTS, NOTES])
    drafts = _DraftsApi(_page([_draft("d1")])).install(monkeypatch)
    await _run(settings, tmp_path / "data", thread=DRAFTS if named else None)

    assert DRAFTS not in {thread for thread, _ in api.calls}
    assert len(drafts.urls) == 1
    assert _stored_ids(tmp_path / "data", DRAFTS) == {"d1"}


IMG = '<img itemtype="http://schema.skype.com/AMSImage" src="https://api.asm.skype.com/v1/objects/o1/views/imgo">'


async def _notes_with_an_image(settings: Settings, data: Path, monkeypatch, **kw: Any) -> list[Path]:
    seen: list[Path] = []

    async def fake_process(content, msgtype, token, media_dir, **_: Any):  # noqa: ANN001
        seen.append(media_dir)
        return []

    monkeypatch.setattr(AR.attachments, "process", fake_process)
    message = {
        "id": "n1",
        "composetime": "2026-07-01T09:00:00Z",
        "content": IMG,
        "messagetype": "RichText/Html",
    }
    _wire(monkeypatch, _FakeApi({NOTES: [message]}), [NOTES])
    await _run(settings, data, **kw)
    return seen


async def test_notes_media_lands_in_the_notes_folder(settings: Settings, tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    seen = await _notes_with_an_image(settings, data, monkeypatch)
    assert seen == [data.resolve() / NOTES / "media"]


async def test_no_media_stores_notes_and_downloads_nothing(settings: Settings, tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    seen = await _notes_with_an_image(settings, data, monkeypatch, download_media=False)
    assert seen == []
    assert _stored_ids(data, NOTES) == {"n1"}


def _event(thread: str, mid: str) -> dict[str, Any]:
    resource = {
        "id": mid,
        "version": "1",
        "composetime": "2026-09-23T10:00:01Z",
        "messagetype": "RichText/Html",
        "content": mid,
        "conversationLink": f"https://h/v1/users/ME/conversations/{thread}",
    }
    return {"type": "EventMessage", "resourceType": "NewMessage", "resource": resource}


async def test_live_writes_a_note_as_it_arrives(settings: Settings, tmp_path) -> None:
    live = AL.LiveArchive(settings, tmp_path / "data", AR.StaticToken("sk"))
    try:
        await live.on_event(_event(NOTES, "n1"))
        await live.on_event(_event("19:ch@thread.tacv2", "c1"))
        assert _stored_ids(live.data_dir, NOTES) == {"n1"}
        assert not (live.data_dir / "19:ch@thread.tacv2").exists()  # channels still need --all
    finally:
        live.close()


def test_the_widget_ignores_archived_48_conversations(tmp_path) -> None:
    data = tmp_path / "data"
    index = Index(data)
    last = {"composetime": "2026-07-02T09:00:00Z", "content": "hi", "messagetype": "Text"}
    for thread_id in [*HISTORY, DRAFTS]:
        index.upsert_chat(thread_id, label="x", raw={"id": thread_id, "lastMessage": last})
    index.close()
    before = W.seed_from_archive(data, "8:orgid:me")
    index = Index(data)
    index.upsert_chat(
        "19:a_b@unq.gbl.spaces", label="A", raw={"id": "19:a_b@unq.gbl.spaces", "lastMessage": last}
    )
    index.close()

    assert before == {}
    assert set(W.seed_from_archive(data, "8:orgid:me")) == {"19:a_b@unq.gbl.spaces"}
    assert json.dumps(W.seed_from_archive(data, "8:orgid:me")).count("48:") == 0


# --- drafts ---

BACK = "https://h/v1/users/ME/conversations/48:drafts/messages?startTime=0&syncState={}&pageSize=200"
_REAL_CLIENT = httpx.Client  # a test installs the fake twice: the second must not wrap the first


def _draft(draft_id: str, version: int = 1, content: str = "text", **props: Any) -> dict[str, Any]:
    return {
        "id": draft_id,
        "version": str(version),
        "composetime": "2026-07-01T09:00:00.1230000Z",
        "draftType": "ScheduledDraft",
        "innerThreadId": "19:a_b@unq.gbl.spaces",
        "draftDetails": {"sendAt": "1790755200000"},
        "content": content,
        "properties": props,
    }


def _page(drafts: list[dict[str, Any]], back: str = "") -> dict[str, Any]:
    meta = {"syncState": BACK.format("sync"), **({"backwardLink": BACK.format(back)} if back else {})}
    return {"_metadata": meta, "drafts": drafts}


class _DraftsApi:
    """The drafts endpoint behind a real httpx client: canned pages in order, requests recorded."""

    def __init__(self, *pages: dict[str, Any], status: int = 200) -> None:
        self.pages, self.status = list(pages), status
        self.urls: list[str] = []

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        assert request.url.path == "/v1/users/ME/drafts"  # the conversation path answers 400
        assert request.headers["X-Skypetoken"] == "sk"
        if self.status != 200:
            return httpx.Response(self.status, json={"errorCode": self.status})
        return httpx.Response(200, json=self.pages.pop(0) if self.pages else {"drafts": []})

    def install(self, monkeypatch) -> _DraftsApi:
        transport = httpx.MockTransport(self._answer)
        monkeypatch.setattr(D.httpx, "Client", lambda **kw: _REAL_CLIENT(transport=transport, **kw))
        return self


def _rows(data: Path, table: str) -> dict[str, dict[str, Any]]:
    store = ChatStore(data, DRAFTS)
    try:
        key = "id" if table == "messages" else "id || '@' || version"
        return {r[0]: json.loads(r[1]) for r in store._db.execute(f"SELECT {key}, raw FROM {table}")}
    finally:
        store.close()


async def _pass(settings: Settings, data: Path, monkeypatch, *pages: dict[str, Any]) -> _DraftsApi:
    _wire(monkeypatch, _FakeApi({}), [DRAFTS])
    api = _DraftsApi(*pages).install(monkeypatch)
    await _run(settings, data)
    return api


async def test_drafts_are_read_across_pages(settings: Settings, tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    api = await _pass(
        settings,
        data,
        monkeypatch,
        _page([_draft("d3"), _draft("d2")], back="p2"),
        _page([_draft("d1")]),
    )

    assert set(_rows(data, "messages")) == {"d1", "d2", "d3"}
    assert [url.split("?", 1)[1] for url in api.urls] == [
        "pageSize=200",
        "startTime=0&syncState=p2&pageSize=200",  # the query of backwardLink, on the drafts path
    ]


@pytest.mark.parametrize("second", [_page([_draft("d1")], back="p2"), _page([], back="p3")])
async def test_the_draft_walk_ends_on_a_repeated_link_or_an_empty_page(
    settings: Settings, tmp_path, monkeypatch, second: dict[str, Any]
) -> None:
    data = tmp_path / "data"
    api = await _pass(settings, data, monkeypatch, _page([_draft("d2")], back="p2"), second)
    assert len(api.urls) == 2


async def test_an_unchanged_draft_is_not_rewritten(settings: Settings, tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    await _pass(settings, data, monkeypatch, _page([_draft("d1")]))
    events = _spy_log(monkeypatch)
    await _pass(settings, data, monkeypatch, _page([_draft("d1")]))

    assert _rows(data, "message_versions") == {}
    refreshed = next(kw for _, event, kw in events if event == "drafts_refreshed")
    assert (refreshed["new"], refreshed["updated"], refreshed["stale"]) == (0, 0, 1)
    recap = next(kw for _, event, kw in events if event == "archive_recap")
    assert recap["new_messages"] == 0


async def test_an_edited_draft_keeps_its_old_version(settings: Settings, tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    await _pass(settings, data, monkeypatch, _page([_draft("d1", 1, "first")]))
    await _pass(settings, data, monkeypatch, _page([_draft("d1", 2, "second", edittime="1")]))

    assert _rows(data, "messages")["d1"]["content"] == "second"
    assert _rows(data, "message_versions")["d1@1"]["content"] == "first"


async def test_a_cancelled_draft_keeps_its_text(settings: Settings, tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    await _pass(settings, data, monkeypatch, _page([_draft("d1", 1, "say this")]))
    await _pass(settings, data, monkeypatch, _page([_draft("d1", 3, "", deletetime="1")]))

    tombstone = _rows(data, "messages")["d1"]
    assert tombstone["content"] == "" and "deletetime" in tombstone["properties"]
    assert _rows(data, "message_versions")["d1@1"]["content"] == "say this"


async def test_no_draft_row_stays_pending(settings: Settings, tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    await _pass(settings, data, monkeypatch, _page([_draft("d1")]))
    store = ChatStore(data, DRAFTS)
    try:
        assert store._db.execute("SELECT COUNT(*) FROM messages WHERE pending = 1").fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize("status", [400, 500])
async def test_a_failing_draft_call_is_counted_and_spares_the_rest(
    settings: Settings, tmp_path, monkeypatch, status: int
) -> None:
    data = tmp_path / "data"
    _wire(monkeypatch, _FakeApi({NOTES: [_msg("n1", "2026-07-01T09:00:00Z")]}), [DRAFTS, NOTES])
    _DraftsApi(status=status).install(monkeypatch)
    events = _spy_log(monkeypatch)
    await _run(settings, data)

    recap = next(kw for _, event, kw in events if event == "archive_recap")
    assert (recap["chats"], recap["failed"]) == (2, 1)
    assert [kw["thread"] for _, event, kw in events if event == "chat_archive_failed"] == [DRAFTS]
    assert _stored_ids(data, NOTES) == {"n1"}


async def test_an_empty_draft_store_is_not_a_failure(settings: Settings, tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    events = _spy_log(monkeypatch)
    await _pass(settings, data, monkeypatch, _page([]))

    recap = next(kw for _, event, kw in events if event == "archive_recap")
    assert (recap["chats"], recap["failed"]) == (1, 0)
    assert _rows(data, "messages") == {}


async def test_drafts_are_re_read_when_the_conversation_looks_unchanged(
    settings: Settings, tmp_path, monkeypatch
) -> None:
    # The listing's `version` and `lastMessage` are what let other chats skip a pass.
    conv = {"id": DRAFTS, "version": 7, "lastMessage": {"composetime": "2026-07-01T09:00:00.1230000Z"}}
    data = tmp_path / "data"
    for page in (_page([_draft("d1", 1, "first")]), _page([_draft("d1", 2, "second")])):
        _wire(monkeypatch, _FakeApi({}), [])
        monkeypatch.setattr(AR, "fetch_conversations", lambda s, tok: iter([[conv]]))
        _DraftsApi(page).install(monkeypatch)
        await _run(settings, data)

    assert _rows(data, "messages")["d1"]["content"] == "second"


async def test_a_draft_walk_cut_by_the_page_cap_says_so(settings: Settings, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(D, "_MAX_PAGES", 1)
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(D.log, "warning", lambda event, **kw: events.append((event, kw)))
    data = tmp_path / "data"
    await _pass(settings, data, monkeypatch, _page([_draft("d2")], back="p2"), _page([_draft("d1")]))

    assert set(_rows(data, "messages")) == {"d2"}
    assert events == [("drafts_truncated", {"pages": 1})]
