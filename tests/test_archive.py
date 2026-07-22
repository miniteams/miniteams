"""Archive command (spec 001 phase 2): full run, resume, top-up overlap, discovery, isolation."""

from pathlib import Path
from typing import Any

import pytest

from miniteams import archive as AR
from miniteams.archive_store import ChatStore, Index, chat_dir_name
from miniteams.config import Settings
from miniteams.dump import _epoch_seconds

C1 = "19:alice@unq.gbl.spaces"
C2 = "19:bob@unq.gbl.spaces"


def _msg(mid: str, when: str) -> dict[str, Any]:
    return {"id": mid, "composetime": when, "content": mid}


class _FakeApi:
    """Serves message pages newest-first from an in-memory chronological store."""

    def __init__(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self.data = data
        self.calls: list[tuple[str, int | None]] = []

    def iter_pages(self, settings, token, thread_id, page_size, max_pages, end_before=None):  # noqa: ANN001 — test double mirrors iter_history_pages
        self.calls.append((thread_id, end_before))
        msgs = sorted(self.data.get(thread_id, []), key=lambda m: m["composetime"], reverse=True)
        if end_before is not None:
            msgs = [m for m in msgs if _epoch_seconds(m["composetime"]) <= end_before]
        for i in range(0, len(msgs), page_size):
            yield msgs[i : i + page_size]


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch) -> None:
    monkeypatch.setattr(AR, "_INTER_PAGE_DELAY", 0)


def _wire(monkeypatch, api: _FakeApi, convs: list[str]) -> None:
    monkeypatch.setattr(AR, "iter_history_pages", api.iter_pages)
    monkeypatch.setattr(AR, "fetch_conversations", lambda s, tok: iter([[{"id": c} for c in convs]]))

    async def fake_thread(self, thread_id: str) -> dict[str, Any]:
        return {"topic": "", "members": [{"mri": "8:orgid:x", "name": "X"}]}

    async def fake_label(self, thread_id: str) -> str:
        return "label"

    monkeypatch.setattr(AR.Directory, "thread", fake_thread)
    monkeypatch.setattr(AR.Directory, "label", fake_label)


async def _run(settings: Settings, data: Path, **kw: Any) -> None:
    """run_archive with a static token and avatars off — the default for non-avatar tests."""
    kw.setdefault("download_avatars", False)
    await AR.run_archive(settings, data, token_provider=AR.StaticToken("sk", ""), **kw)


def _stored_ids(data_dir: Path, thread_id: str) -> set[str]:
    store = ChatStore(data_dir, thread_id)
    ids = {r[0] for r in store._db.execute("SELECT id FROM messages").fetchall()}
    store.close()
    return ids


async def test_full_run_stores_all_and_marks_done(settings: Settings, tmp_path, monkeypatch) -> None:
    api = _FakeApi(
        {
            C1: [_msg("a1", "2026-07-01T09:00:00Z"), _msg("a2", "2026-07-02T09:00:00Z")],
            C2: [_msg("b1", "2026-07-03T09:00:00Z")],
        }
    )
    _wire(monkeypatch, api, [C1, C2])
    data = tmp_path / "data"
    await _run(settings, data)

    assert _stored_ids(data, C1) == {"a1", "a2"}
    assert _stored_ids(data, C2) == {"b1"}
    index = Index(data)
    chats = {c["id"]: c for c in index.chats()}
    assert chats[C1]["backfill_done"] and chats[C1]["participants"] == [{"mri": "8:orgid:x", "name": "X"}]
    assert chats[C1]["last_fetch_at"]
    index.close()


async def test_resume_after_interrupt_no_gap_no_dup(settings: Settings, tmp_path, monkeypatch) -> None:
    full = [_msg(f"m{i}", f"2026-07-{i:02d}T09:00:00Z") for i in range(1, 6)]
    data = tmp_path / "data"
    # Simulate a crash after only the newest 2 landed and backfill NOT marked done.
    pre = ChatStore(data, C1)
    pre.insert_page([full[4], full[3]])  # m5, m4
    pre.close()

    api = _FakeApi({C1: full})
    _wire(monkeypatch, api, [C1])
    await _run(settings, data)

    assert _stored_ids(data, C1) == {"m1", "m2", "m3", "m4", "m5"}
    store = ChatStore(data, C1)
    assert store.count() == 5  # no duplicates
    store.close()
    index = Index(data)
    assert index.backfill_done(C1)
    index.close()


async def test_topup_stops_at_overlap(settings: Settings, tmp_path, monkeypatch) -> None:
    full = [_msg(f"m{i}", f"2026-07-{i:02d}T09:00:00Z") for i in range(1, 11)]
    data = tmp_path / "data"
    # Already fully backfilled with the oldest 8; two new messages arrived (m9, m10).
    pre = ChatStore(data, C1)
    pre.insert_page(list(reversed(full[:8])))
    pre.close()
    index = Index(data)
    index.upsert_chat(C1)
    index.mark_backfill_done(C1)
    index.close()

    api = _FakeApi({C1: full})
    _wire(monkeypatch, api, [C1])
    await _run(settings, data)

    assert _stored_ids(data, C1) == {f"m{i}" for i in range(1, 11)}
    # Backfill must NOT run again (already done): every history call was a top-up (end_before=None).
    assert all(end is None for _, end in api.calls)


async def test_new_chat_discovered_on_second_run(settings: Settings, tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    api1 = _FakeApi({C1: [_msg("a1", "2026-07-01T09:00:00Z")]})
    _wire(monkeypatch, api1, [C1])
    await _run(settings, data)

    api2 = _FakeApi(
        {
            C1: [_msg("a1", "2026-07-01T09:00:00Z")],
            C2: [_msg("b1", "2026-07-05T09:00:00Z")],
        }
    )
    _wire(monkeypatch, api2, [C1, C2])  # C2 now present in enumeration
    await _run(settings, data)

    assert _stored_ids(data, C2) == {"b1"}
    index = Index(data)
    assert {c["id"] for c in index.chats()} == {C1, C2}
    index.close()


async def test_one_failing_chat_does_not_abort_others(settings: Settings, tmp_path, monkeypatch) -> None:
    api = _FakeApi({C2: [_msg("b1", "2026-07-03T09:00:00Z")]})
    original = api.iter_pages

    def boom(settings, token, thread_id, page_size, max_pages, end_before=None):  # noqa: ANN001
        if thread_id == C1:
            raise RuntimeError("boom")
        return original(settings, token, thread_id, page_size, max_pages, end_before)

    _wire(monkeypatch, api, [C1, C2])
    monkeypatch.setattr(AR, "iter_history_pages", boom)
    await _run(settings, tmp_path / "data")

    assert _stored_ids(tmp_path / "data", C2) == {"b1"}  # C1 failed, C2 still archived


def test_enumerate_scope_default_and_all(settings: Settings, monkeypatch) -> None:
    ids = [
        "19:x@unq.gbl.spaces",  # 1:1
        "19:g@thread.v2",  # group
        "19:meeting_abc@thread.v2",  # meeting
        "19:ch@thread.tacv2",  # channel
    ]
    monkeypatch.setattr(AR, "fetch_conversations", lambda s, t: iter([[{"id": i} for i in ids]]))
    default = [c["id"] for c in AR._enumerate(settings, "sk", include_all=False)]
    assert default == [
        "19:x@unq.gbl.spaces",
        "19:g@thread.v2",
        "19:meeting_abc@thread.v2",
    ]  # meetings in, channel out
    assert [c["id"] for c in AR._enumerate(settings, "sk", include_all=True)] == ids  # --all: everything


def test_chat_dir_name_used_for_folder(settings: Settings, tmp_path) -> None:
    store = ChatStore(tmp_path, "19:a/b@thread.v2")
    assert store.dir.name == chat_dir_name("19:a/b@thread.v2") == "19:a_b@thread.v2"
    store.close()


async def test_media_downloaded_skipped_and_failure_nonfatal(
    settings: Settings, tmp_path, monkeypatch
) -> None:
    """Media pass: process() is called per message with attachments into the chat's media/;
    a message without attachments is skipped; a download failure never stops the loop."""
    IMG = '<img itemtype="http://schema.skype.com/AMSImage" src="https://api.asm.skype.com/v1/objects/o9/views/imgo">'
    data = tmp_path / "data"
    pre = ChatStore(data, C1)
    pre.insert_page(
        [
            {
                "id": "m1",
                "composetime": "2026-07-01T09:00:00Z",
                "content": IMG,
                "messagetype": "RichText/Media_AudioCallRecording",
            },
            {"id": "m2", "composetime": "2026-07-02T09:00:00Z", "content": "hi", "messagetype": "Text"},
            {
                "id": "m3",
                "composetime": "2026-07-03T09:00:00Z",
                "content": IMG,
                "messagetype": "RichText/Media_AudioCallRecording",
            },
        ]
    )
    pre.close()
    index = Index(data)
    index.upsert_chat(C1)
    index.mark_backfill_done(C1)
    index.close()

    seen: list[tuple[str, Path]] = []

    async def fake_process(content, msgtype, token, media_dir, download):  # noqa: ANN001
        seen.append((content, media_dir))
        if len(seen) == 1:
            raise RuntimeError("download boom")  # first one fails — must not abort
        return ["[image: u → local]"]

    monkeypatch.setattr(AR.attachments, "process", fake_process)
    api = _FakeApi({C1: []})
    _wire(monkeypatch, api, [C1])
    await _run(settings, data, download_media=True)

    # Called only for the two messages that actually carry an image (m2 text skipped).
    assert len(seen) == 2
    assert all(md == pre.media_dir for _, md in seen)


async def test_no_media_skips_download(settings: Settings, tmp_path, monkeypatch) -> None:
    IMG = '<img itemtype="http://schema.skype.com/AMSImage" src="https://api.asm.skype.com/v1/objects/o1/views/imgo">'
    data = tmp_path / "data"
    pre = ChatStore(data, C1)
    pre.insert_page(
        [
            {
                "id": "m1",
                "composetime": "2026-07-01T09:00:00Z",
                "content": IMG,
                "messagetype": "RichText/Media_AudioCallRecording",
            }
        ]
    )
    pre.close()
    index = Index(data)
    index.upsert_chat(C1)
    index.mark_backfill_done(C1)
    index.close()

    called = False

    async def fake_process(*a, **k):  # noqa: ANN002, ANN003
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(AR.attachments, "process", fake_process)
    api = _FakeApi({C1: []})
    _wire(monkeypatch, api, [C1])
    await _run(settings, data, download_media=False)
    assert not called


async def test_raw_conversation_metadata_stored(settings: Settings, tmp_path, monkeypatch) -> None:
    """Enumeration passes the full conversation object; index.db keeps it (lastMessage etc.)."""
    conv = {"id": C1, "lastMessage": {"composetime": "2026-07-02T09:00:00Z", "content": "hi"}, "version": 42}
    monkeypatch.setattr(
        AR, "iter_history_pages", _FakeApi({C1: [_msg("a1", "2026-07-01T09:00:00Z")]}).iter_pages
    )
    monkeypatch.setattr(AR, "fetch_conversations", lambda s, tok: iter([[conv]]))

    async def fake_thread(self, tid):  # noqa: ANN001
        return {"topic": "T", "members": [], "picture": None}

    async def fake_label(self, tid):  # noqa: ANN001
        return "L"

    monkeypatch.setattr(AR.Directory, "thread", fake_thread)
    monkeypatch.setattr(AR.Directory, "label", fake_label)
    data = tmp_path / "data"
    await _run(settings, data)

    (chat,) = Index(data).chats()
    assert chat["raw"]["lastMessage"]["content"] == "hi"
    assert chat["raw"]["version"] == 42


async def test_avatars_group_icon_and_members(settings: Settings, tmp_path, monkeypatch) -> None:
    """_download_avatars fetches the group icon (from properties.picture) + each member avatar."""
    store = ChatStore(tmp_path / "data", C1)
    info = {
        "topic": "T",
        "picture": "URL@https://asyncgw/objects/g1/views/avatar_fullsize",
        "members": [{"mri": "8:orgid:x"}, {"mri": "8:orgid:y"}],
    }
    calls: list[str] = []

    async def fake_group(client, pic, tok, adir):  # noqa: ANN001
        calls.append(f"group:{pic}")
        return str(adir / "group.jpg")

    async def fake_user(client, mri, bearer, adir):  # noqa: ANN001
        calls.append(f"user:{mri}")
        return str(adir / f"{mri}.jpg")

    monkeypatch.setattr(AR.avatars, "fetch_group_icon", fake_group)
    monkeypatch.setattr(AR.avatars, "fetch_user_avatar", fake_user)
    n = await AR._download_avatars(store, info, "sk", "bearer")
    store.close()
    assert n == 3  # 1 group + 2 members
    assert calls[0].startswith("group:") and "user:8:orgid:x" in calls


def test_refreshing_token_reexchanges_before_expiry(settings: Settings, monkeypatch) -> None:
    """RefreshingToken re-mints once, caches until near expiry, then re-mints again."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(AR.time, "monotonic", lambda: clock["t"])
    exchanges: list[int] = []

    class _Src:
        def refresh(self):
            return {"access_token": "a", "id_token": "b"}

    def fake_exchange(s, at):  # noqa: ANN001
        exchanges.append(1)
        return {"skype_token": f"sk{len(exchanges)}", "expires_in": 600}

    monkeypatch.setattr(AR, "exchange_skype_token", fake_exchange)
    prov = AR.RefreshingToken(settings, _Src())

    assert prov.token() == "sk1"  # first call mints
    assert prov.token() == "sk1"  # cached (well before expiry)
    assert prov.bearer() == "b"
    clock["t"] += 600  # past expiry - margin (600 - 300)
    assert prov.token() == "sk2"  # re-minted
    assert len(exchanges) == 2
