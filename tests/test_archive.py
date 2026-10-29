"""Archive command (spec 001 phase 2): full run, resume, top-up overlap, discovery, isolation."""

import time
from pathlib import Path
from typing import Any

import pytest

from miniteams import archive as AR
from miniteams.archive_store import ChatStore, Index, chat_dir_name
from miniteams.auth import AuthExpired
from miniteams.config import Settings
from miniteams.dump import epoch_seconds

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
            msgs = [m for m in msgs if epoch_seconds(m["composetime"]) <= end_before]
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

    async def fake_process(
        content,
        msgtype,
        token,
        media_dir,
        download,
        client=None,
        sp_token=None,
        skip_url=None,
        on_fail=None,
        media=None,
        files=None,
        store=None,
    ):  # noqa: ANN001
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
    assert prov.bearer() == "a"  # never the id_token: the profile lookup refuses it
    clock["t"] += 600  # past expiry - margin (600 - 300)
    assert prov.token() == "sk2"  # re-minted
    assert len(exchanges) == 2


def test_refreshing_token_remembers_a_refused_sharepoint_host(settings: Settings, monkeypatch) -> None:
    """A host AAD refuses is not re-asked on every message of a pass, but is again after a while."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(AR.time, "monotonic", lambda: clock["t"])
    asked: list[str] = []

    class _Src:
        def sharepoint_token(self, host: str) -> str | None:
            asked.append(host)
            return "tok" if host == "c.sharepoint.com" else None

    prov = AR.RefreshingToken(settings, _Src())  # type: ignore[arg-type]
    assert [prov.sharepoint_token("x.sharepoint.com") for _ in range(3)] == [None] * 3
    assert [prov.sharepoint_token("c.sharepoint.com") for _ in range(2)] == ["tok"] * 2
    assert asked == ["x.sharepoint.com", "c.sharepoint.com", "c.sharepoint.com"]  # successes not memoised
    clock["t"] += AR.RefreshingToken._SP_REFUSED_TTL + 1
    assert prov.sharepoint_token("x.sharepoint.com") is None
    assert asked[-1] == "x.sharepoint.com"  # asked again once the memo expired


def test_refreshing_token_memo_is_locked_and_aad_is_not(settings: Settings) -> None:
    entered: list[int] = []

    class _SpyLock:
        def __init__(self) -> None:
            self.held = False

        def __enter__(self) -> None:
            entered.append(1)
            self.held = True

        def __exit__(self, *_: Any) -> None:
            self.held = False

    class _Src:
        def sharepoint_token(self, host: str) -> str | None:
            assert not prov._lock.held  # type: ignore[attr-defined]  # AAD round trip outside the lock
            return None

    prov = AR.RefreshingToken(settings, _Src())  # type: ignore[arg-type]
    prov._lock = _SpyLock()  # type: ignore[assignment]
    assert prov.sharepoint_token("x.sharepoint.com") is None
    assert len(entered) == 2  # memo read, then memo write
    assert prov.sharepoint_token("x.sharepoint.com") is None
    assert len(entered) == 3  # memo hit: read only


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"content-type": "text/vtt", "content-length": str(60 * 1024 * 1024)}, 413),  # over the cap
        ({"content-type": "video/mp4"}, 415),  # not a transcript
    ],
)
async def test_a_refused_transcript_is_denied(
    settings: Settings, tmp_path, headers: dict[str, str], status: int
) -> None:
    """Otherwise the same oversized or mistyped URI is pulled again on every pass: an over-cap body
    for good, a wrong type (maybe an error page served 200) on the 404 backoff ladder."""
    from functools import partial

    from miniteams import attachments as A

    class _Resp:
        status_code = 200

        def __init__(self, hdr: dict[str, str]) -> None:
            self.headers = hdr

        def raise_for_status(self) -> None:
            return None

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_: Any) -> bool:
            return False

        async def aiter_bytes(self, size: int = 65536) -> Any:
            raise AssertionError("refused before any body byte")
            yield b""

    class _C:
        def stream(self, method: str, url: str, **kw: Any) -> _Resp:
            json = (kw.get("params") or {}).get("format") == "json"
            return _Resp(
                {**headers, "content-type": "application/json"} if json and status == 413 else headers
            )

    url = "https://c-my.sharepoint.com/personal/x/_api/v2.1/drives/b/items/01/content"
    store = ChatStore(tmp_path / "data", C1)
    try:
        keys = [f"{url}#.json", f"{url}#.vtt"]
        for attempt in (1, 2):
            notes = await A.process(
                f'<item type="onedriveForBusinessTranscript" uri="{url}" />',
                "RichText/Media_CallRecording",
                "sk",
                store.media_dir,
                download=True,
                client=_C(),  # type: ignore[arg-type]
                sp_token=lambda h: "T",
                on_fail=partial(AR.record_asset_failure, store),
            )
            assert notes == [f"[transcript(sp): {url}]"]
            rows = store._db.execute(
                "SELECT url, status, attempts, retry_after FROM denied_assets ORDER BY url"
            ).fetchall()
            if status == 413:
                assert rows == [(k, 413, 1, "") for k in keys]  # permanent
                assert store.denied_urls("9999-12-31T00:00:00Z") == set(keys)
                break
            assert [r[:3] for r in rows] == [(k, 415, attempt) for k in keys]  # attempts grow
            assert all(r[3] for r in rows)  # backed off, not permanent
            assert store.denied_urls("9999-12-31T00:00:00Z") == set()
    finally:
        store.close()


def _spy_log(monkeypatch) -> list[tuple[str, str, dict[str, Any]]]:
    """Record archive log calls; the test config filters info out before any capture."""
    events: list[tuple[str, str, dict[str, Any]]] = []

    class _Log:
        def __getattr__(self, level):  # noqa: ANN001
            return lambda event, **kw: events.append((level, event, kw))

    monkeypatch.setattr(AR, "log", _Log())
    return events


async def test_resume_skips_unchanged_chat(settings: Settings, tmp_path, monkeypatch) -> None:
    """A backfilled chat whose enumeration lastMessage <= stored newest is skipped entirely:
    no thread fetch, no history call, no media/avatar rescan."""
    data = tmp_path / "data"
    pre = ChatStore(data, C1)
    pre.insert_page([_msg("a1", "2026-07-01T09:00:00Z")])
    pre.close()
    index = Index(data)
    index.upsert_chat(C1)
    index.mark_backfill_done(C1)
    index.close()

    hist_calls: list[str] = []
    monkeypatch.setattr(AR, "iter_history_pages", lambda *a, **k: hist_calls.append("x") or iter([]))
    conv = {"id": C1, "lastMessage": {"composetime": "2026-07-01T09:00:00Z"}}  # == stored newest
    monkeypatch.setattr(AR, "fetch_conversations", lambda s, tok: iter([[conv]]))

    thread_fetched = []

    async def fake_thread(self, tid):  # noqa: ANN001
        thread_fetched.append(tid)
        return {"topic": "", "members": [], "picture": None}

    monkeypatch.setattr(AR.Directory, "thread", fake_thread)
    events = _spy_log(monkeypatch)
    await _run(settings, data)

    assert hist_calls == []  # no top-up / backfill request
    assert [kw["by"] for _, event, kw in events if event == "chat_unchanged"] == ["activity"]
    assert thread_fetched == []  # thread metadata not even fetched


def _seed_synced(data: Path, msgs: list[dict[str, Any]], version: int) -> None:
    pre = ChatStore(data, C1)
    pre.insert_page(msgs)
    pre.close()
    index = Index(data)
    index.upsert_chat(C1)
    index.mark_backfill_done(C1)
    index.mark_synced(C1, version)
    index.close()


def _count_history(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(AR, "iter_history_pages", lambda *a, **k: calls.append("x") or iter([]))
    return calls


@pytest.mark.parametrize(
    "msgs",
    [[_msg("a1", "2026-07-01T09:00:00Z")], []],
    ids=["no-last-message", "empty-store"],
)
async def test_resume_skips_chat_with_unchanged_version(settings, tmp_path, monkeypatch, msgs) -> None:
    """Meeting chats: no lastMessage (version fallback is newer than any message) or no message
    at all. The timestamp check can never pass; an unchanged version must still skip."""
    data = tmp_path / "data"
    _seed_synced(data, msgs, version=1790000000000)  # 2026-09: after the stored message
    hist = _count_history(monkeypatch)
    conv = {"id": C1, "version": 1790000000000}
    monkeypatch.setattr(AR, "fetch_conversations", lambda s, tok: iter([[conv]]))
    _wire_directory(monkeypatch)
    events = _spy_log(monkeypatch)
    await _run(settings, data)
    assert hist == []
    assert [kw["by"] for _, event, kw in events if event == "chat_unchanged"] == ["version"]


async def test_resume_fetches_chat_whose_version_moved(settings, tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    _seed_synced(data, [_msg("a1", "2026-07-01T09:00:00Z")], version=1790000000000)
    hist = _count_history(monkeypatch)
    conv = {"id": C1, "version": 1790000000001}
    monkeypatch.setattr(AR, "fetch_conversations", lambda s, tok: iter([[conv]]))
    _wire_directory(monkeypatch)
    await _run(settings, data)
    assert hist  # top-up ran
    index = Index(data)
    assert index.synced_version(C1) == 1790000000001  # recorded after the complete pass
    index.close()


async def test_failed_pass_does_not_record_version(settings, tmp_path, monkeypatch) -> None:
    """The version is the skip proof: storing it before the pass completes would freeze a chat
    whose fetch failed."""
    data = tmp_path / "data"
    _seed_synced(data, [_msg("a1", "2026-07-01T09:00:00Z")], version=1790000000000)

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("history down")

    monkeypatch.setattr(AR, "iter_history_pages", boom)
    conv = {"id": C1, "version": 1790000000001}
    monkeypatch.setattr(AR, "fetch_conversations", lambda s, tok: iter([[conv]]))
    _wire_directory(monkeypatch)
    await _run(settings, data)  # run_archive logs chat_archive_failed and carries on
    index = Index(data)
    assert index.synced_version(C1) == 1790000000000
    index.close()


def _wire_directory(monkeypatch) -> None:
    async def fake_thread(self, tid):  # noqa: ANN001
        return {"topic": "", "members": [], "picture": None}

    async def fake_label(self, tid):  # noqa: ANN001
        return "L"

    monkeypatch.setattr(AR.Directory, "thread", fake_thread)
    monkeypatch.setattr(AR.Directory, "label", fake_label)


async def test_verify_media_forces_pass_on_unchanged_chat(settings: Settings, tmp_path, monkeypatch) -> None:
    """--verify-media disables the unchanged fast-skip so the media pass re-runs (skip-exists
    downloads only what's missing) on an already-backfilled, unchanged chat."""
    data = tmp_path / "data"
    pre = ChatStore(data, C1)
    pre.insert_page([_msg("a1", "2026-07-01T09:00:00Z")])
    pre.close()
    index = Index(data)
    index.upsert_chat(C1)
    index.mark_backfill_done(C1)
    index.close()

    conv = {"id": C1, "lastMessage": {"composetime": "2026-07-01T09:00:00Z"}}  # unchanged
    monkeypatch.setattr(AR, "fetch_conversations", lambda s, tok: iter([[conv]]))
    monkeypatch.setattr(AR, "iter_history_pages", _FakeApi({C1: []}).iter_pages)
    _wire_directory(monkeypatch)

    media_ran = []
    monkeypatch.setattr(AR, "_download_media", lambda *a, **k: media_ran.append(1) or _acount())
    await _run(settings, data, download_media=True, verify_media=True)
    assert media_ran == [1]  # media pass ran despite the chat being unchanged


async def _acount() -> int:
    return 0


async def test_assets_only_downloads_media_no_history_no_avatars(settings, tmp_path, monkeypatch) -> None:
    """--assets-only: media pass only, from the index, no topup/backfill/thread-fetch/avatars."""
    data = tmp_path / "data"
    pre = ChatStore(data, C1)
    pre.insert_page([_msg("a1", "2026-07-01T09:00:00Z")])
    pre.close()
    index = Index(data)
    index.upsert_chat(C1)
    index.mark_backfill_done(C1)
    index.close()

    hist = []
    monkeypatch.setattr(AR, "iter_history_pages", lambda *a, **k: hist.append(1) or iter([]))
    enum = []
    monkeypatch.setattr(AR, "fetch_conversations", lambda *a, **k: enum.append(1) or iter([]))
    thread_fetched = []

    async def fake_thread(self, tid):  # noqa: ANN001
        thread_fetched.append(tid)
        return {}

    monkeypatch.setattr(AR.Directory, "thread", fake_thread)
    media_ran = []
    monkeypatch.setattr(AR, "_download_media", lambda *a, **k: media_ran.append(1) or _acount())
    avatars_ran = []
    monkeypatch.setattr(AR, "_download_avatars", lambda *a, **k: avatars_ran.append(1) or _acount())

    await AR.run_archive(settings, data, token_provider=AR.StaticToken("sk", ""), assets_only=True)

    assert media_ran == [1]  # media pass ran
    assert hist == [] and enum == []  # no history, no network enumeration (used index)
    assert thread_fetched == [] and avatars_ran == []  # no metadata, no avatars


# --- recap counts + auth-expired stop (return values feeding the run recap / --loop) ---


class _DyingToken:
    """Valid for the first `good` token() calls, then AuthExpired — simulates a refresh token
    dying mid-run (good=1: survives enumeration, dies at the first per-chat mint)."""

    def __init__(self, good: int) -> None:
        self.good = good
        self.calls = 0

    def token(self) -> str:
        self.calls += 1
        if self.calls > self.good:
            raise AuthExpired("refresh token dead")
        return "sk"

    def bearer(self) -> str:
        return ""

    def sharepoint_token(self, host: str) -> str | None:
        return None


async def test_archive_chat_returns_counts(settings: Settings, tmp_path, monkeypatch) -> None:
    api = _FakeApi({C1: [_msg("m1", "2026-07-01T09:00:00Z"), _msg("m2", "2026-07-01T10:00:00Z")]})
    _wire(monkeypatch, api, [C1])
    data = tmp_path / "data"
    index = Index(data)
    counts = await AR.archive_chat(
        settings,
        "sk",
        "",
        C1,
        data,
        AR.Directory(settings),
        index,
        download_media=False,
        download_avatars=False,
    )
    index.close()
    assert counts == {"new": 2, "media": 0, "avatars": 0}


async def test_run_archive_returns_false_when_auth_ok(settings: Settings, tmp_path, monkeypatch) -> None:
    api = _FakeApi({C1: [_msg("m1", "2026-07-01T09:00:00Z")]})
    _wire(monkeypatch, api, [C1])
    expired = await AR.run_archive(
        settings,
        tmp_path / "data",
        token_provider=AR.StaticToken("sk", ""),
        download_media=False,
        download_avatars=False,
    )
    assert expired is False


async def test_run_archive_auth_expired_at_enumeration(settings: Settings, tmp_path) -> None:
    # The --loop failure mode: token dies during the sleep, next run's FIRST token use is
    # enumeration — must return True (clean stop), not raise.
    expired = await AR.run_archive(settings, tmp_path / "data", token_provider=_DyingToken(good=0))
    assert expired is True


async def test_run_archive_auth_expired_mid_run(settings: Settings, tmp_path, monkeypatch) -> None:
    api = _FakeApi({C1: [_msg("m1", "2026-07-01T09:00:00Z")]})
    _wire(monkeypatch, api, [C1])
    data = tmp_path / "data"
    expired = await AR.run_archive(settings, data, token_provider=_DyingToken(good=1))
    assert expired is True
    assert not ChatStore(data, C1).count()  # stopped before archiving the chat


async def test_media_denied_cache_skips_and_records(settings: Settings, tmp_path, monkeypatch) -> None:
    """403 on an asset → recorded; next pass never re-polls it; --verify-media bypass re-tries."""
    import httpx

    URL = "https://api.asm.skype.com/v1/objects/dead/views/imgo"
    IMG = f'<img itemtype="http://schema.skype.com/AMSImage" src="{URL}">'
    store = ChatStore(tmp_path / "data", C1)
    store.insert_page(
        [{"id": "m1", "composetime": "2026-07-01T09:00:00Z", "content": IMG, "messagetype": "RichText/Html"}]
    )

    calls: list[str] = []

    async def fake_process(
        content,
        msgtype,
        token,
        media_dir,
        download,
        client=None,
        sp_token=None,
        skip_url=None,
        on_fail=None,
        media=None,
        files=None,
        store=None,
    ):  # noqa: ANN001
        if skip_url and skip_url(URL):
            calls.append("skipped")
        else:
            calls.append("attempted")
            req = httpx.Request("GET", URL)
            on_fail(URL, httpx.HTTPStatusError("403", request=req, response=httpx.Response(403, request=req)))
        return [f"[image: {URL}]"]

    monkeypatch.setattr(AR.attachments, "process", fake_process)

    await AR._download_media(store, "sk", "label")
    assert calls == ["attempted"] and store.denied_urls(AR._now_iso()) == {URL}  # 403 recorded
    await AR._download_media(store, "sk", "label")
    assert calls == ["attempted", "skipped"]  # negative cache honored
    await AR._download_media(store, "sk", "label", ignore_denied=True)
    assert calls == ["attempted", "skipped", "attempted"]  # asset flag bypasses
    store.close()


def test_backoff_ladder_climbs_then_caps() -> None:
    """A dead URL must decay to the cap, never to silence and never staying at one hour."""
    from datetime import datetime

    def hours(n: int) -> float:
        delta = datetime.fromisoformat(AR._retry_after(n)) - datetime.now(AR.UTC)
        return round(delta.total_seconds() / 3600)

    assert [hours(n) for n in (1, 2, 3, 4, 5)] == [1, 6, 12, 24, 48]
    assert hours(9) == 48  # standing cap, not a terminal state


async def test_media_404_backs_off_while_403_stays_permanent(
    settings: Settings, tmp_path, monkeypatch
) -> None:
    """The split that makes a still-generating transcript recoverable and a dead one cheap."""
    import httpx

    URL = "https://c-my.sharepoint.com/personal/x/_api/v2.1/drives/b!a/items/01A"
    REC = (
        '<URIObject type="Video.2/CallRecording.1" uri="">'
        f'<item type="onedriveForBusinessTranscript" uri="{URL}" /></URIObject>'
    )
    store = ChatStore(tmp_path / "data", C1)
    store.insert_page(
        [
            {
                "id": "m1",
                "composetime": "2026-07-01T09:00:00Z",
                "content": REC,
                "messagetype": "RichText/Media_CallRecording",
            }
        ]
    )

    async def fake_process(content, msgtype, token, media_dir, download, **kw):  # noqa: ANN001
        req = httpx.Request("GET", URL)
        for key, code in ((f"{URL}#.json", 404), (f"{URL}#.vtt", 403)):
            if kw.get("skip_url") and kw["skip_url"](key):
                continue
            kw["on_fail"](
                key, httpx.HTTPStatusError(str(code), request=req, response=httpx.Response(code, request=req))
            )
        return [f"[transcript(sp): {URL}]"]

    monkeypatch.setattr(AR.attachments, "process", fake_process)
    await AR._download_media(store, "sk", "label")

    rows = {
        r[0]: r for r in store._db.execute("SELECT url, status, attempts, retry_after FROM denied_assets")
    }
    assert rows[f"{URL}#.vtt"][3] == ""  # 403 → permanent
    assert rows[f"{URL}#.json"][1] == 404 and rows[f"{URL}#.json"][3] != ""  # 404 → dated retry
    assert rows[f"{URL}#.json"][2] == 1

    # Second pass while the delay stands: the 404 key is suppressed, so attempts must NOT climb.
    await AR._download_media(store, "sk", "label")
    assert store.denied_attempts(f"{URL}#.json") == 1
    store.close()


async def test_label_falls_back_to_enumeration_topic(settings: Settings, tmp_path, monkeypatch) -> None:
    """Directory denied (403 meeting) → label/topic come from the enumeration raw, not the bare id."""
    conv = {"id": C1, "threadProperties": {"topic": "[Acme] DNS"}}
    api = _FakeApi({C1: [_msg("m1", "2026-07-01T09:00:00Z")]})
    monkeypatch.setattr(AR, "iter_history_pages", api.iter_pages)
    monkeypatch.setattr(AR, "fetch_conversations", lambda s, tok: iter([[conv]]))

    async def denied_thread(self, thread_id):  # noqa: ANN001 — directory fetch fails (403)
        return None

    async def denied_label(self, thread_id):  # noqa: ANN001 — falls back to the bare id
        return thread_id

    monkeypatch.setattr(AR.Directory, "thread", denied_thread)
    monkeypatch.setattr(AR.Directory, "label", denied_label)
    data = tmp_path / "data"
    await _run(settings, data, download_media=False)

    index = Index(data)
    chat = {c["id"]: c for c in index.chats()}[C1]
    index.close()
    assert chat["label"] == "[Acme] DNS" and chat["topic"] == "[Acme] DNS"


async def test_history_403_counted_denied_not_failed(settings: Settings, tmp_path, monkeypatch) -> None:
    """History 403 on one chat: warning + denied counter, other chats still archived."""
    import httpx

    api = _FakeApi({C2: [_msg("b1", "2026-07-03T09:00:00Z")]})
    original = api.iter_pages

    def forbidden(settings, token, thread_id, page_size, max_pages, end_before=None):  # noqa: ANN001
        if thread_id == C1:
            req = httpx.Request("GET", "https://msg.example/messages")
            raise httpx.HTTPStatusError("403", request=req, response=httpx.Response(403, request=req))
        return original(settings, token, thread_id, page_size, max_pages, end_before)

    _wire(monkeypatch, api, [C1, C2])
    monkeypatch.setattr(AR, "iter_history_pages", forbidden)

    events: list[tuple[str, str, dict]] = []

    class _Log:
        def __getattr__(self, level):  # noqa: ANN001
            return lambda event, **kw: events.append((level, event, kw))

    monkeypatch.setattr(AR, "log", _Log())
    await _run(settings, tmp_path / "data", download_media=False)

    assert _stored_ids(tmp_path / "data", C2) == {"b1"}  # isolation preserved
    assert ("warning", "chat_history_denied", {"thread": C1}) in events
    assert not any(e[1] == "chat_archive_failed" for e in events)
    recap = next(kw for _, event, kw in events if event == "archive_recap")
    assert recap["denied"] == 1 and recap["failed"] == 0 and recap["chats"] == 2


async def test_history_denied_skipped_next_run_and_retry_flag(
    settings: Settings, tmp_path, monkeypatch
) -> None:
    """403 persisted → next run makes zero calls for that chat; --retry-denied re-attempts and a
    successful retry clears the denial."""
    import httpx

    data = tmp_path / "data"
    api = _FakeApi({C1: [_msg("a1", "2026-07-01T09:00:00Z")]})
    original = api.iter_pages
    allow = False

    def forbidden(settings, token, thread_id, page_size, max_pages, end_before=None):  # noqa: ANN001
        if not allow:
            req = httpx.Request("GET", "https://msg.example/messages")
            raise httpx.HTTPStatusError("403", request=req, response=httpx.Response(403, request=req))
        return original(settings, token, thread_id, page_size, max_pages, end_before)

    _wire(monkeypatch, api, [C1])
    monkeypatch.setattr(AR, "iter_history_pages", forbidden)

    await _run(settings, data, download_media=False)  # run 1: 403 → marked denied
    index = Index(data)
    assert index.history_denied_ids() == {C1}
    index.close()

    calls: list[str] = []
    monkeypatch.setattr(AR, "iter_history_pages", lambda *a, **kw: calls.append("hit") or original(*a, **kw))
    await _run(settings, data, download_media=False)  # run 2: skipped, no history call
    assert calls == []

    allow = True
    monkeypatch.setattr(AR, "iter_history_pages", forbidden)
    await _run(settings, data, download_media=False, retry_denied=True)  # run 3: forced, succeeds
    assert _stored_ids(data, C1) == {"a1"}
    index = Index(data)
    assert index.history_denied_ids() == set()  # denial lifted after success
    index.close()


async def test_empty_backfilled_chat_refetches_when_messages_arrive(
    settings: Settings, tmp_path, monkeypatch
) -> None:
    """Meeting chat archived before the meeting: empty history drained, backfill_done set. Once
    messages exist, the next run must fetch them (regression: top-up had no cursor and backfill
    was skipped → chat frozen empty forever)."""
    data = tmp_path / "data"
    _wire(monkeypatch, _FakeApi({C1: []}), [C1])
    await _run(settings, data, download_media=False)  # pre-meeting: empty, marked done
    index = Index(data)
    assert index.backfill_done(C1)
    index.close()
    assert _stored_ids(data, C1) == set()

    _wire(monkeypatch, _FakeApi({C1: [_msg("m1", "2026-07-24T11:30:00Z")]}), [C1])
    await _run(settings, data, download_media=False)  # meeting happened
    assert _stored_ids(data, C1) == {"m1"}


async def test_media_pass_writes_recordings_manifest(settings: Settings, tmp_path, monkeypatch) -> None:
    """media/recordings.json pairs each Media_CallRecording with its on-disk files."""
    import json as _json

    OBJ = "https://fr-prod.asyncgw.teams.microsoft.com/v1/objects/rec1/views"
    REC = (
        '<URIObject type="Video.2/CallRecording.1" uri="">'
        "<Title>CIR OPS</Title>"
        f'<item type="amsVideo" uri="{OBJ}/video" />'
        "</URIObject>"
    )
    data = tmp_path / "data"
    pre = ChatStore(data, C1)
    pre.insert_page(
        [
            {
                "id": "r1",
                "composetime": "2026-08-18T10:00:00Z",
                "content": REC,
                "messagetype": "RichText/Media_CallRecording",
            }
        ]
    )
    pre.media_dir.mkdir(parents=True, exist_ok=True)
    (pre.media_dir / "rec1.video.mp4").write_bytes(b"MP4")
    pre.close()
    index = Index(data)
    index.upsert_chat(C1)
    index.mark_backfill_done(C1)
    index.close()

    async def fake_process(*a, **k):  # noqa: ANN002, ANN003 — downloads themselves are not under test
        return []

    monkeypatch.setattr(AR.attachments, "process", fake_process)
    api = _FakeApi({C1: []})
    _wire(monkeypatch, api, [C1])
    await _run(settings, data, download_media=True)

    manifest_path = ChatStore(data, C1).media_dir / "recordings.json"
    assert manifest_path.stat().st_mode & 0o777 == 0o600  # owner-only, like the transcripts
    manifest = _json.loads(manifest_path.read_text())
    assert manifest == [
        {
            "title": "CIR OPS",
            "duration": "",
            "videos": ["rec1.video.mp4"],
            "transcripts": [],
            "message_id": "r1",
            "composetime": "2026-08-18T10:00:00Z",
        }
    ]


async def test_force_tops_up_an_unchanged_looking_chat(settings: Settings, tmp_path, monkeypatch) -> None:
    """Live gap (spec 005): a message stored past a lost one makes the chat look unchanged; a
    forced chat is topped up anyway and the hole below `newest()` is fetched."""
    data = tmp_path / "data"
    pre = ChatStore(data, C1)
    pre.insert_page([_msg("a1", "2026-07-01T09:00:00Z"), _msg("a3", "2026-07-01T11:00:00Z")])
    pre.close()
    index = Index(data)
    for thread in (C1, C2):
        index.upsert_chat(thread)
        index.mark_backfill_done(thread)
    index.close()
    pre2 = ChatStore(data, C2)
    pre2.insert_page([_msg("b1", "2026-07-01T09:00:00Z")])
    pre2.close()

    convs = [
        {"id": C1, "lastMessage": {"composetime": "2026-07-01T11:00:00Z"}},  # == stored newest
        {"id": C2, "lastMessage": {"composetime": "2026-07-01T09:00:00Z"}},  # unchanged, not forced
    ]
    monkeypatch.setattr(AR, "fetch_conversations", lambda s, tok: iter([convs]))
    api = _FakeApi(
        {
            C1: [_msg("a3", "2026-07-01T11:00:00Z"), _msg("a2", "2026-07-01T10:00:00Z")],
            C2: [_msg("b2", "2026-07-01T10:00:00Z")],
        }
    )
    monkeypatch.setattr(AR, "iter_history_pages", api.iter_pages)

    async def fake_thread(self, tid):  # noqa: ANN001
        return {"topic": "", "members": [], "picture": None}

    async def fake_label(self, tid):  # noqa: ANN001
        return "L"

    monkeypatch.setattr(AR.Directory, "thread", fake_thread)
    monkeypatch.setattr(AR.Directory, "label", fake_label)
    await _run(settings, data, download_media=False, force=frozenset({C1}))
    assert _stored_ids(data, C1) == {"a1", "a2", "a3"}  # hole filled
    assert _stored_ids(data, C2) == {"b1"}  # not forced: fast-skipped as before


def test_refreshing_token_mints_once_across_threads(settings: Settings, monkeypatch) -> None:
    """`archive --live` asks from the pass thread and the event loop at once: one re-mint."""
    import threading

    exchanges: list[int] = []
    gate = threading.Barrier(8)

    class _Src:
        def refresh(self):
            return {"access_token": "a", "id_token": "b"}

    def fake_exchange(s, at):  # noqa: ANN001
        time.sleep(0.05)  # widen the race window
        exchanges.append(1)
        return {"skype_token": f"sk{len(exchanges)}", "expires_in": 3600}

    monkeypatch.setattr(AR, "exchange_skype_token", fake_exchange)
    prov = AR.RefreshingToken(settings, _Src())
    got: list[str] = []

    def worker() -> None:
        gate.wait()
        got.append(prov.token())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert exchanges == [1] and set(got) == {"sk1"}


async def test_force_also_overrides_the_version_skip(settings: Settings, tmp_path, monkeypatch) -> None:
    """A forced chat is topped up even when its conversation version says it is synced."""
    data = tmp_path / "data"
    pre = ChatStore(data, C1)
    pre.insert_page([_msg("a1", "2026-07-01T09:00:00Z")])
    pre.close()
    index = Index(data)
    index.upsert_chat(C1)
    index.mark_backfill_done(C1)
    index.mark_synced(C1, 42)
    index.close()

    conv = {"id": C1, "version": 42}  # no lastMessage: only the version could skip it
    monkeypatch.setattr(AR, "fetch_conversations", lambda s, tok: iter([[conv]]))
    api = _FakeApi({C1: [_msg("a1", "2026-07-01T09:00:00Z"), _msg("a2", "2026-07-01T10:00:00Z")]})
    monkeypatch.setattr(AR, "iter_history_pages", api.iter_pages)

    async def fake_thread(self, tid):  # noqa: ANN001
        return {"topic": "", "members": [], "picture": None}

    async def fake_label(self, tid):  # noqa: ANN001
        return "L"

    monkeypatch.setattr(AR.Directory, "thread", fake_thread)
    monkeypatch.setattr(AR.Directory, "label", fake_label)
    await _run(settings, data, download_media=False)
    assert _stored_ids(data, C1) == {"a1"}  # unforced: skipped by version
    await _run(settings, data, download_media=False, force=frozenset({C1}))
    assert _stored_ids(data, C1) == {"a1", "a2"}


def _backfilled(data: Path, thread_id: str) -> None:
    index = Index(data)
    index.upsert_chat(thread_id)
    index.mark_backfill_done(thread_id)
    index.close()


async def test_a_live_write_does_not_hide_a_message_the_stream_missed(
    settings: Settings, tmp_path, monkeypatch
) -> None:
    """Trouter lost m2 without a gap signal, then delivered m3 live: the next pass still fetches m2."""
    full = [_msg(f"m{i}", f"2026-07-0{i}T09:00:00Z") for i in (1, 2, 3)]
    data = tmp_path / "data"
    pre = ChatStore(data, C1)
    pre.insert_page([full[0]])
    pre.apply_message(full[2], "2026-07-03T09:00:01Z")
    pre.close()
    _backfilled(data, C1)
    _wire(monkeypatch, _FakeApi({C1: full}), [C1])
    conv = {"id": C1, "lastMessage": {"composetime": full[2]["composetime"]}}
    monkeypatch.setattr(AR, "fetch_conversations", lambda s, tok: iter([[conv]]))
    monkeypatch.setattr(AR, "_PAGE_SIZE", 1)
    await _run(settings, data)

    assert _stored_ids(data, C1) == {"m1", "m2", "m3"}


async def test_an_interrupted_top_up_leaves_no_gap(settings: Settings, tmp_path, monkeypatch) -> None:
    full = [_msg(f"m{i}", f"2026-07-0{i}T09:00:00Z") for i in range(1, 6)]
    data = tmp_path / "data"
    pre = ChatStore(data, C1)
    pre.insert_page([full[0]])
    pre.close()
    _backfilled(data, C1)
    api = _FakeApi({C1: full})
    _wire(monkeypatch, api, [C1])
    monkeypatch.setattr(AR, "_PAGE_SIZE", 1)

    def first_page_then_reset(*a, **k):  # noqa: ANN002, ANN003
        pages = api.iter_pages(*a, **k)
        yield next(pages)
        raise RuntimeError("connection reset")

    monkeypatch.setattr(AR, "iter_history_pages", first_page_then_reset)
    await _run(settings, data)
    assert _stored_ids(data, C1) == {"m1", "m5"}

    monkeypatch.setattr(AR, "iter_history_pages", api.iter_pages)
    await _run(settings, data)
    assert _stored_ids(data, C1) == {f"m{i}" for i in range(1, 6)}


async def test_recheck_since_walks_back_to_the_date_on_chats_active_since(
    settings: Settings, tmp_path, monkeypatch
) -> None:
    """An archive already holding a hole under its newest message (rows written live before the
    watermark existed) gets it filled; chats idle since the date keep their fast-skip."""
    full = [_msg(f"m{i}", f"2026-07-0{i}T09:00:00Z") for i in (1, 2, 3)]
    old = _msg("b1", "2026-06-01T09:00:00Z")
    data = tmp_path / "data"
    pre = ChatStore(data, C1)
    pre.insert_page([full[2], full[0]])
    pre.close()
    pre = ChatStore(data, C2)
    pre.insert_page([old])
    pre.close()
    _backfilled(data, C1)
    _backfilled(data, C2)
    api = _FakeApi({C1: full, C2: [old]})
    _wire(monkeypatch, api, [C1, C2])
    convs = [
        {"id": C1, "lastMessage": {"composetime": full[2]["composetime"]}},
        {"id": C2, "lastMessage": {"composetime": old["composetime"]}},
    ]
    monkeypatch.setattr(AR, "fetch_conversations", lambda s, tok: iter([convs]))
    monkeypatch.setattr(AR, "_PAGE_SIZE", 1)

    await _run(settings, data)
    assert _stored_ids(data, C1) == {"m1", "m3"}  # fast-skipped: the hole is invisible

    await _run(settings, data, recheck_since="2026-07-01T00:00:00")
    assert _stored_ids(data, C1) == {"m1", "m2", "m3"}
    assert {thread for thread, _ in api.calls} == {C1}


async def test_a_chat_holding_only_live_rows_is_walked_in_full(
    settings: Settings, tmp_path, monkeypatch
) -> None:
    """A meeting chat backfilled while still empty, then written live: nothing bounds its top-up."""
    full = [_msg(f"m{i}", f"2026-07-0{i}T09:00:00Z") for i in (1, 2)]
    data = tmp_path / "data"
    pre = ChatStore(data, C1)
    pre.apply_message(full[1], "2026-07-02T09:00:01Z")
    pre.close()
    _backfilled(data, C1)
    _wire(monkeypatch, _FakeApi({C1: full}), [C1])
    monkeypatch.setattr(AR, "_PAGE_SIZE", 1)
    await _run(settings, data)

    assert _stored_ids(data, C1) == {"m1", "m2"}
    store = ChatStore(data, C1)
    assert store.newest() == full[1]["composetime"]  # the walk confirmed the live row
    store.close()


async def test_recheck_since_applies_to_a_named_thread(settings: Settings, tmp_path, monkeypatch) -> None:
    """--thread carries no activity to compare: naming the chat is the request to recheck it."""
    full = [_msg(f"m{i}", f"2026-07-0{i}T09:00:00Z") for i in (1, 2, 3)]
    data = tmp_path / "data"
    pre = ChatStore(data, C1)
    pre.insert_page([full[2], full[0]])
    pre.close()
    _backfilled(data, C1)
    _wire(monkeypatch, _FakeApi({C1: full}), [C1])
    monkeypatch.setattr(AR, "_PAGE_SIZE", 1)
    await _run(settings, data, thread=C1, recheck_since="2026-07-01T00:00:00")

    assert _stored_ids(data, C1) == {"m1", "m2", "m3"}


async def test_media_scan_lets_the_loop_run_mid_chat(tmp_path) -> None:
    """`archive --live` sweeps on the stream's loop: a long scan must hand it back as it goes."""
    import asyncio

    store = ChatStore(tmp_path / "data", C1)
    n = 1200
    store.insert_page(
        [{"id": f"m{i}", "composetime": f"2026-07-01T{i:06d}", "content": "text"} for i in range(n)]
    )
    scanned = 0
    rows = store.iter_messages

    def counting():  # noqa: ANN202
        nonlocal scanned
        for message in rows():
            scanned += 1
            yield message

    store.iter_messages = counting  # type: ignore[method-assign]
    seen: list[int] = []

    async def tick() -> None:
        while True:
            seen.append(scanned)
            await asyncio.sleep(0)

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0)
    assert await AR._download_media(store, "sk", "label") == 0
    ticker.cancel()
    store.close()
    assert any(0 < s < n for s in seen)  # the loop ran while the scan was mid-chat


async def test_media_pass_opens_no_client_for_a_chat_without_media(tmp_path, monkeypatch) -> None:
    """The live sweep visits every chat: one without attachments must cost no HTTP client."""

    def no_client(*a: Any, **kw: Any) -> Any:
        raise AssertionError("httpx client built for a chat without media")

    monkeypatch.setattr(AR.httpx, "AsyncClient", no_client)
    store = ChatStore(tmp_path / "data", C1)
    store.insert_page([{"id": "m1", "composetime": "2026-07-01T09:00:00Z", "content": "plain text"}])
    store.insert_page(
        [{"id": "m2", "composetime": "2026-07-01T09:01:00Z", "content": "", "messagetype": "Text"}]
    )
    assert await AR._download_media(store, "sk", "label") == 0
    store.close()


async def test_media_scan_holds_no_snapshot_across_downloads(tmp_path, monkeypatch) -> None:
    """A live write to the chat mid-scan must not lock out the scan's own writes (stale WAL snapshot)."""
    import asyncio

    data = tmp_path / "data"
    store = ChatStore(data, C1)
    img = '<img itemtype="http://schema.skype.com/AMSImage" src="https://api.asm.skype.com/v1/objects/o/views/imgo">'
    store.insert_page(
        [{"id": f"m{i}", "composetime": f"2026-07-01T09:{i:02d}:00Z", "content": img} for i in range(30)]
    )
    store._db.execute("PRAGMA busy_timeout = 100")  # fail fast instead of waiting 5 s
    errors: list[str] = []
    live_wrote = asyncio.Event()

    async def fake_process(content, msgtype, token, media_dir, download, **kw):  # noqa: ANN001
        if not live_wrote.is_set():
            other = ChatStore(data, C1)  # `archive --live` writing the same chat
            other.apply_message({"id": "live", "composetime": "2026-07-02T00:00:00Z", "content": "hi"}, "now")
            other.close()
            live_wrote.set()
        try:
            store.mark_denied(f"k{len(errors)}-{id(content)}", 403, "2026-07-02T00:00:00Z")
            store.save_file([f"u-{id(content)}"], "f.bin", "")
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
        return []

    monkeypatch.setattr(AR.attachments, "process", fake_process)
    await AR._download_media(store, "sk", "label")
    store.close()
    assert live_wrote.is_set() and errors == []


IMG = '<img itemtype="http://schema.skype.com/AMSImage" src="https://api.asm.skype.com/v1/objects/o/views/imgo">'


def _image_chat(tmp_path: Path, n: int) -> ChatStore:
    store = ChatStore(tmp_path / "data", C1)
    store.insert_page(
        [{"id": f"m{i:04d}", "composetime": f"2026-07-01T{i:06d}", "content": IMG} for i in range(n)]
    )
    return store


async def test_media_scan_reads_ahead_only_a_bounded_queue(tmp_path, monkeypatch) -> None:
    import asyncio

    store = _image_chat(tmp_path, 1000)
    scanned = 0
    rows = store.iter_messages

    def counting():  # noqa: ANN202
        nonlocal scanned
        for message in rows():
            scanned += 1
            yield message

    store.iter_messages = counting  # type: ignore[method-assign]
    entered = 0

    async def blocked(*a: Any, **kw: Any) -> list[str]:
        nonlocal entered
        entered += 1
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(AR.attachments, "process", blocked)
    task = asyncio.create_task(AR._download_media(store, "sk", "label"))
    async with asyncio.timeout(5):
        while entered < AR._MEDIA_CONCURRENCY:
            await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.wait({task})
    store.close()
    # in the workers' hands, in the queue, and the one the scan waits to put
    assert scanned <= 2 * AR._MEDIA_CONCURRENCY + 1


class _Client:
    """Stands in for httpx.AsyncClient: records its closes."""

    closed = 0

    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    async def aclose(self) -> None:
        _Client.closed += 1


async def test_media_client_is_closed_on_success_and_on_cancel(tmp_path, monkeypatch) -> None:
    import asyncio

    monkeypatch.setattr(AR.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(_Client, "closed", 0)
    gate = asyncio.Event()
    gate.set()
    entered = asyncio.Event()

    async def process(*a: Any, **kw: Any) -> list[str]:
        entered.set()
        await gate.wait()
        return []

    monkeypatch.setattr(AR.attachments, "process", process)
    store = _image_chat(tmp_path, 3)
    await AR._download_media(store, "sk", "label")
    assert _Client.closed == 1
    gate.clear()
    entered.clear()
    task = asyncio.create_task(AR._download_media(store, "sk", "label"))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.wait({task})
    store.close()
    assert _Client.closed == 2


async def test_one_shot_run_clears_a_stale_sweep_heartbeat_but_a_live_pass_does_not(
    settings: Settings, tmp_path
) -> None:
    from miniteams.archive_store import read_meta

    data = tmp_path / "data"
    for live_pass, left in ((True, "running"), (False, "")):
        index = Index(data)
        index.set_meta("media_sweep", "running")
        index.close()
        await AR.run_archive(
            settings, data, token_provider=AR.StaticToken("sk"), assets_only=True, live_pass=live_pass
        )
        assert read_meta(data)["media_sweep"] == left
