"""Live follower (spec 005): buffer during the catch-up pass, drain, follow, re-catch-up on gaps."""

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from miniteams import archive_live as AL
from miniteams.archive import StaticToken
from miniteams.archive_store import ChatStore, Index
from miniteams.config import Settings

C1 = "19:alice@unq.gbl.spaces"
C2 = "19:bob@unq.gbl.spaces"
LINK = "https://h/v1/users/ME/conversations/"


def _new(thread: str, mid: str, version: int = 1, **extra: Any) -> dict[str, Any]:
    resource = {
        "id": mid,
        "version": str(version),
        "composetime": f"2026-09-23T10:00:{version:02d}Z",
        "messagetype": "RichText/Html",
        "content": f"{mid} v{version}",
        "conversationLink": LINK + thread,
        **extra,
    }
    return {"type": "EventMessage", "resourceType": "NewMessage", "resource": resource}


def _update(thread: str, mid: str, version: int, content: str) -> dict[str, Any]:
    event = _new(thread, mid, version, content=content)
    event["resourceType"] = "MessageUpdate"
    return event


class _Passes:
    """Fake catch-up pass: records its `force` set; blocks while `hold` is clear."""

    def __init__(self, *outcomes: Any) -> None:
        self.forces: list[frozenset[str]] = []
        self.outcomes = list(outcomes)
        self.hold = threading.Event()
        self.hold.set()
        self.started = threading.Event()

    def __call__(self, force: frozenset[str]) -> bool:
        self.forces.append(force)
        self.started.set()
        self.hold.wait(5)
        outcome = self.outcomes.pop(0) if self.outcomes else False
        if isinstance(outcome, BaseException):
            raise outcome
        return bool(outcome)


@pytest.fixture
def live(settings: Settings, tmp_path: Path, monkeypatch) -> Any:
    arch = AL.LiveArchive(settings, tmp_path / "data", StaticToken("sk"))
    passes = _Passes()
    monkeypatch.setattr(arch, "_pass", passes)
    arch.passes = passes  # type: ignore[attr-defined]
    yield arch
    arch.close()


def _stored(live: AL.LiveArchive, thread: str) -> dict[str, dict[str, Any]]:
    store = ChatStore(live.data_dir, thread)
    try:
        return {r[0]: json.loads(r[1]) for r in store._db.execute("SELECT id, raw FROM messages")}
    finally:
        store.close()


def _versions(live: AL.LiveArchive, thread: str) -> list[str]:
    store = ChatStore(live.data_dir, thread)
    try:
        return [r[0] for r in store._db.execute("SELECT version FROM message_versions ORDER BY version")]
    finally:
        store.close()


async def _settle(live: AL.LiveArchive) -> None:
    assert live._catchup is not None
    await asyncio.wait_for(live._catchup, 5)


async def test_events_during_the_pass_are_written_only_after_it(live: AL.LiveArchive) -> None:
    live.passes.hold.clear()
    await live.on_gap("connected")
    await asyncio.to_thread(live.passes.started.wait, 5)
    await live.on_event(_new(C1, "m1"))
    assert _stored(live, C1) == {}  # buffered: newest() must not move while the pass runs
    live.passes.hold.set()
    await _settle(live)
    assert set(_stored(live, C1)) == {"m1"}
    await live.on_event(_new(C1, "m2"))  # following: written at once
    assert set(_stored(live, C1)) == {"m1", "m2"}
    assert live.passes.forces == [frozenset()]


async def test_buffered_versions_all_reach_history(live: AL.LiveArchive) -> None:
    live._buffering = True
    await live.on_event(_new(C1, "m1", 1))
    await live.on_event(_update(C1, "m1", 3, "deleted"))
    await live.on_event(_update(C1, "m1", 2, "edited"))  # out of order
    await live.on_event(_update(C1, "m1", 2, "edited"))  # exact redelivery collapses
    assert len(live._buffer) == 3
    await live.on_gap("connected")
    await _settle(live)
    assert _stored(live, C1)["m1"]["content"] == "deleted"
    assert _versions(live, C1) == ["1", "2"]


async def test_ignores_what_the_archive_does_not_keep(live: AL.LiveArchive) -> None:
    typing = _new(C1, "t", messagetype="Control/Typing")
    horizon = _new(C1, "", messagetype="ThreadActivity/MemberConsumptionHorizonUpdate")
    outside = _new("19:x@thread.tacv2", "c1")  # channel, not archived without --all
    presence = {"type": "EventMessage", "resourceType": "UserPresence", "resource": {"id": "p"}}
    for event in (typing, horizon, outside, presence):
        await live.on_event(event)
    assert _stored(live, C1) == {} and live.index.chats() == []


class _Log:
    """Stands in for the module logger: conftest filters info out before any capture."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __getattr__(self, level: str) -> Any:
        return lambda event, **kw: self.events.append({"event": event, **kw})


async def test_conversation_update_merges_into_the_index(live: AL.LiveArchive, monkeypatch) -> None:
    live.index.upsert_chat(C1, label="Alice", raw={"properties": {"consumptionhorizon": "1", "a": 1}})
    update = {"id": C1, "properties": {"consumptionhorizon": "2"}}
    monkeypatch.setattr(AL, "log", logs := _Log())
    await live.on_event({"type": "EventMessage", "resourceType": "ConversationUpdate", "resource": update})
    assert live.index.chats()[0]["raw"]["properties"] == {"consumptionhorizon": "2", "a": 1}
    assert [e["outcome"] for e in logs.events if e["event"] == "live_event_stored"] == ["merged"]


async def test_new_message_moves_the_index_last_message(live: AL.LiveArchive) -> None:
    await live.on_event(_new(C1, "m1", 5))
    (chat,) = live.index.chats()
    assert chat["id"] == C1 and chat["raw"]["lastMessage"]["id"] == "m1"


async def test_gap_during_the_pass_runs_one_more(live: AL.LiveArchive) -> None:
    live.passes.hold.clear()
    await live.on_gap("connected")
    await asyncio.to_thread(live.passes.started.wait, 5)
    await live.on_event(_new(C1, "m1"))
    await live.on_gap("connected")
    await live.on_gap("connected")  # several signals collapse into one extra pass
    live.passes.hold.set()
    await _settle(live)
    assert len(live.passes.forces) == 2
    assert set(_stored(live, C1)) == {"m1"} and not live._buffering


async def test_message_loss_forces_recently_written_chats(live: AL.LiveArchive) -> None:
    await live.on_event(_new(C1, "m1"))
    await live.on_event(_new(C2, "m2"))
    live._recent[C2] -= AL._LOSS_WINDOW + 1  # written before the window
    await live.on_gap("message_loss")
    await _settle(live)
    assert live.passes.forces == [frozenset({C1})]


async def test_overflow_keeps_memory_flat_and_tops_up_before_draining(
    live: AL.LiveArchive, monkeypatch
) -> None:
    monkeypatch.setattr(AL, "_BUFFER_MAX", 2)
    live.passes.hold.clear()
    await live.on_gap("connected")
    await asyncio.to_thread(live.passes.started.wait, 5)
    await live.on_event(_new(C1, "m1"))  # buffered before the overflow
    await live.on_event(_new(C2, "b1"))
    await live.on_event(_new(C1, "m2"))  # dropped: C1 now has a hole past m1
    await live.on_event(_new(C1, "m3"))
    assert len(live._buffer) == 2 and live._dropped == 2
    drained_before_second_pass: list[set[str]] = []
    original = live.passes.__call__

    def second(force: frozenset[str]) -> bool:
        drained_before_second_pass.append(set(_stored(live, C1)))
        return original(force)

    live._pass = second  # type: ignore[method-assign]
    live.passes.hold.set()
    await _settle(live)
    assert live.passes.forces == [frozenset(), frozenset({C1})]
    assert drained_before_second_pass == [set()]  # nothing drained before the forced top-up
    assert set(_stored(live, C1)) == {"m1"} and not live._buffer


async def test_failed_pass_retries_with_its_force_set(live: AL.LiveArchive, monkeypatch) -> None:
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(AL.asyncio, "sleep", fake_sleep)
    live.passes.outcomes = [OSError("reset"), OSError("reset"), False]
    live._force = {C1}
    await live.on_gap("connected")
    await _settle(live)
    assert live.passes.forces == [frozenset({C1})] * 3
    assert slept[:2] == [2.0, 4.0]


async def test_dead_refresh_token_stops(live: AL.LiveArchive) -> None:
    live.passes.outcomes = [True]
    await live.on_gap("connected")
    await _settle(live)
    assert live.stopped.is_set()


async def test_run_live_returns_when_the_stream_stops(settings: Settings, tmp_path, monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_forever(s, **kw):  # noqa: ANN001
        seen.update(kw)

    monkeypatch.setattr(AL, "run_forever", fake_forever)
    assert await AL.run_live(settings, tmp_path / "data", token_provider=StaticToken("sk")) is True
    assert seen["epid_name"] == "endpoint_id-archive" and seen["on_gap"] and seen["on_event"]
    assert Index(tmp_path / "data").chats() == []


async def test_gap_during_the_pass_reruns_even_with_nothing_buffered(live: AL.LiveArchive) -> None:
    live.passes.hold.clear()
    await live.on_gap("connected")
    await asyncio.to_thread(live.passes.started.wait, 5)
    await live.on_gap("connected")  # a reconnect with no event: the gap is still unknown
    live.passes.hold.set()
    await _settle(live)
    assert len(live.passes.forces) == 2 and not live._buffering


def _flaky(live: AL.LiveArchive, monkeypatch, fails: int) -> list[str]:
    """`_apply` raising `database is locked` `fails` times, then working; returns the call log."""
    import sqlite3

    real, calls = live._apply, []

    def apply(kind: str, thread: str, resource: dict[str, Any]) -> str:
        calls.append(str(resource.get("id")))
        if len(calls) <= fails:
            raise sqlite3.OperationalError("database is locked")
        return real(kind, thread, resource)

    monkeypatch.setattr(live, "_apply", apply)
    return calls


async def test_failed_write_during_the_drain_is_retried_not_lost(live: AL.LiveArchive, monkeypatch) -> None:
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(AL.asyncio, "sleep", fake_sleep)
    _flaky(live, monkeypatch, fails=2)
    live._buffering = True
    await live.on_event(_new(C1, "m1", 1))
    await live.on_event(_update(C1, "m1", 2, "edited"))  # an edit a top-up could never restore
    await live.on_gap("connected")
    await _settle(live)
    assert _stored(live, C1)["m1"]["content"] == "edited" and _versions(live, C1) == ["1"]
    assert not live._buffering and len(live.passes.forces) == 3
    assert [d for d in slept if d] == [2.0, 4.0]  # backoff grows across good passes


async def test_failed_write_while_following_triggers_a_catch_up(live: AL.LiveArchive, monkeypatch) -> None:
    calls = _flaky(live, monkeypatch, fails=1)
    await live.on_event(_new(C1, "m1"))
    assert live._buffering and live._catchup is not None  # not dropped: buffered, pass scheduled
    await _settle(live)
    assert set(_stored(live, C1)) == {"m1"} and calls == ["m1", "m1"]


# --- live media (phase 3) ---

IMG = '<img itemtype="http://schema.skype.com/AMSImage" src="https://api.asm.skype.com/v1/objects/o1/views/imgo">'


async def _media_done(live: AL.LiveArchive) -> None:
    await asyncio.wait_for(asyncio.gather(*live._media_tasks), 5)


async def test_live_media_downloads_into_the_chat_media_dir(live: AL.LiveArchive, monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_process(content, msgtype, token, media_dir, download, **kw):  # noqa: ANN001
        calls.append({"media_dir": media_dir, "download": download, "token": token, **kw})
        kw["on_fail"](IMG, _http_error(403))  # one asset denied: must be remembered
        return ["→ file"]

    monkeypatch.setattr(AL.attachments, "process", fake_process)
    await live.on_event(_new(C1, "m1", content=IMG))
    await live.on_event(_new(C1, "m2"))  # no attachment: no download
    await _media_done(live)
    (call,) = calls
    assert call["media_dir"] == live.data_dir / C1 / "media" and call["download"] is True
    assert call["token"] == "sk"
    store = ChatStore(live.data_dir, C1)
    assert IMG in store.denied_urls("9999")
    store.close()


async def test_live_media_does_not_hold_the_stream(live: AL.LiveArchive, monkeypatch) -> None:
    release = asyncio.Event()

    async def slow_process(*a: Any, **kw: Any) -> list[str]:
        await release.wait()
        raise OSError("sharepoint down")

    monkeypatch.setattr(AL.attachments, "process", slow_process)
    await asyncio.wait_for(live.on_event(_new(C1, "m1", content=IMG)), 1)  # returns before the download
    assert set(_stored(live, C1)) == {"m1"} and len(live._media_tasks) == 1
    release.set()
    await _media_done(live)  # the failure is logged, not raised
    assert not live.stopped.is_set()


async def test_live_media_skips_denied_and_honours_no_media(live: AL.LiveArchive, monkeypatch) -> None:
    seen: list[bool] = []

    async def fake_process(content, msgtype, token, media_dir, download, **kw):  # noqa: ANN001
        seen.append(kw["skip_url"]("denied-key"))
        return []

    monkeypatch.setattr(AL.attachments, "process", fake_process)
    store = ChatStore(live.data_dir, C1)
    store.mark_denied("denied-key", 403, "2026-01-01T00:00:00Z")
    store.close()
    await live.on_event(_new(C1, "m1", content=IMG))
    await _media_done(live)
    assert seen == [True]
    live.download_media = False
    await live.on_event(_new(C1, "m2", content=IMG))
    assert not live._media_tasks and seen == [True]


async def test_live_media_stops_on_a_dead_refresh_token(live: AL.LiveArchive, monkeypatch) -> None:
    from miniteams.auth import AuthExpired

    def dead() -> str:
        raise AuthExpired("refresh token revoked")

    monkeypatch.setattr(live.provider, "token", dead)
    await live.on_event(_new(C1, "m1", content=IMG))
    await _media_done(live)
    assert live.stopped.is_set()


def _http_error(status: int) -> Exception:
    import httpx

    request = httpx.Request("GET", "https://x")
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))


async def test_run_live_reconciles_on_a_timer(settings: Settings, tmp_path, monkeypatch) -> None:
    """The safety net for events Trouter drops without saying so: a pass runs every interval."""
    passes: list[frozenset[str]] = []
    monkeypatch.setattr(AL.LiveArchive, "_pass", lambda self, force: passes.append(force) or False)

    async def fake_forever(s, **kw):  # noqa: ANN001
        await asyncio.sleep(0.5)

    monkeypatch.setattr(AL, "run_forever", fake_forever)
    await AL.run_live(settings, tmp_path / "data", token_provider=StaticToken("sk"), reconcile=0.1)
    assert len(passes) >= 2


# --- media sweep (spec 032) ---

C3 = "19:carol@unq.gbl.spaces"


def _archived(live: AL.LiveArchive, *threads: str) -> None:
    """Chats already archived, each with one image message; the sweep runs without pauses."""
    live.settings.sweep_pause = 0.0
    for thread in threads:
        live.index.ensure_chat(thread)
        store = ChatStore(live.data_dir, thread)
        store.insert_page([{"id": f"{thread}-m", "composetime": "2026-09-01T00:00:00Z", "content": IMG}])
        store.close()


async def _until(cond: Any) -> None:
    while not cond():
        await asyncio.sleep(0.01)


async def _first_pass(live: AL.LiveArchive) -> None:
    await live.on_gap("connected")
    await _settle(live)


class _Media:
    """Fake `attachments.process`: records the chat of each call; blocks while `gate` is clear."""

    def __init__(self) -> None:
        self.chats: list[str] = []
        self.kw: list[dict[str, Any]] = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.entered = asyncio.Event()
        self.active = 0

    async def __call__(self, content, msgtype, token, media_dir, download, **kw):  # noqa: ANN001
        self.chats.append(kw["store"].thread_id)
        self.kw.append(kw)
        self.active += 1
        self.entered.set()
        try:
            await self.gate.wait()
        finally:
            self.active -= 1
        return ["[image: u → local]"]


@pytest.fixture
def media(monkeypatch) -> _Media:
    fake = _Media()
    monkeypatch.setattr(AL.attachments, "process", fake)
    return fake


async def test_sweep_waits_for_the_first_pass_then_downloads_each_chat_once(
    live: AL.LiveArchive, media: _Media, monkeypatch
) -> None:
    monkeypatch.setattr(AL, "log", logs := _Log())
    _archived(live, C1, C2, C3)
    sweep = asyncio.create_task(live.sweep())
    await asyncio.sleep(0.05)
    assert media.chats == []  # not before the first catch-up pass
    await live.on_gap("connected")
    await _settle(live)
    await asyncio.wait_for(sweep, 5)
    assert media.chats == [C1, C2, C3]
    done = [e for e in logs.events if e["event"] == "media_sweep_done"]
    assert len(done) == 1 and done[0]["chats"] == 3 and done[0]["files"] == 3 and done[0]["failures"] == 0
    assert live.index.get_meta("media_sweep") == "" and live.index.get_meta("media_sweep_after") == ""


async def test_sweep_counts_a_failing_chat_goes_on_and_resumes_at_it(
    live: AL.LiveArchive, media: _Media, monkeypatch
) -> None:
    monkeypatch.setattr(AL, "log", logs := _Log())
    _archived(live, C1, C2, C3)
    real = AL.archive_chat

    async def flaky(settings, token, bearer, thread_id, *a, **kw):  # noqa: ANN001
        if thread_id == C2:
            raise OSError("NAS gone")
        return await real(settings, token, bearer, thread_id, *a, **kw)

    monkeypatch.setattr(AL, "archive_chat", flaky)
    await _first_pass(live)
    await asyncio.wait_for(live.sweep(), 5)
    done = [e for e in logs.events if e["event"] == "media_sweep_done"][0]
    assert media.chats == [C1, C3] and done["failures"] == 1 and done["files"] == 2
    assert live.index.get_meta("media_sweep_after") == C1  # not past the failed chat
    monkeypatch.setattr(AL, "archive_chat", real)
    media.chats.clear()
    await asyncio.wait_for(live.sweep(), 5)
    assert media.chats == [C2, C3] and live.index.get_meta("media_sweep_after") == ""


async def test_sweep_crash_is_logged_and_clears_the_heartbeat(live: AL.LiveArchive, monkeypatch) -> None:
    await _first_pass(live)
    monkeypatch.setattr(AL, "log", logs := _Log())
    live.index.set_meta("media_sweep", "running")

    def broken(after: str = "") -> list[str]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(live.index, "chat_ids", broken)
    await asyncio.wait_for(live.sweep(), 5)  # does not raise into run_live
    assert [e["event"] for e in logs.events] == ["media_sweep_failed"]
    assert live.index.get_meta("media_sweep") == ""


async def test_live_event_is_written_at_once_during_the_sweep(live: AL.LiveArchive, media: _Media) -> None:
    _archived(live, C1, C2)
    await _first_pass(live)
    media.gate.clear()
    sweep = asyncio.create_task(live.sweep())
    await asyncio.wait_for(media.entered.wait(), 5)  # the sweep is mid-chat on C1
    await live.on_event(_new(C1, "live1"))
    assert "live1" in _stored(live, C1) and not live._buffering and not live._buffer
    media.gate.set()
    await asyncio.wait_for(sweep, 5)


async def test_sweep_never_overlaps_a_pass(live: AL.LiveArchive, media: _Media, monkeypatch) -> None:
    """A pass starting mid-chat cancels it; the sweep waits for the pass, then redoes the chat."""
    _archived(live, C1, C2)
    await _first_pass(live)
    overlaps: list[str] = []
    in_pass = threading.Event()
    release = threading.Event()

    def fake_pass(force: frozenset[str]) -> bool:
        if media.active:
            overlaps.append("pass started beside a sweep download")
        in_pass.set()
        release.wait(5)
        in_pass.clear()
        return False

    monkeypatch.setattr(live, "_pass", fake_pass)
    real_call = media.__call__

    async def guarded(*a: Any, **kw: Any) -> list[str]:
        if in_pass.is_set():
            overlaps.append("sweep download during a pass")
        return await real_call(*a, **kw)

    monkeypatch.setattr(AL.attachments, "process", guarded)
    media.gate.clear()
    sweep = asyncio.create_task(live.sweep())
    await asyncio.wait_for(media.entered.wait(), 5)
    await live.on_gap("reconcile")  # a pass starts while the sweep holds C1
    await asyncio.to_thread(in_pass.wait, 5)
    assert live._sweep_chat is not None and live._sweep_chat.cancelled()
    media.gate.set()
    await asyncio.sleep(0.05)
    assert media.chats == [C1]  # paused while the pass runs
    release.set()
    await _settle(live)
    await asyncio.wait_for(sweep, 5)
    assert media.chats == [C1, C1, C2] and overlaps == []


async def test_sweep_honours_media_selection_and_denied_backoff(live: AL.LiveArchive, media: _Media) -> None:
    _archived(live, C1)
    store = ChatStore(live.data_dir, C1)
    store.mark_denied("denied-key", 403, "2026-01-01T00:00:00Z")
    store.close()
    live.media_types = frozenset({"images", "videos"})
    await _first_pass(live)
    await asyncio.wait_for(live.sweep(), 5)
    assert media.kw[0]["media"] == frozenset({"images", "videos"})
    assert media.kw[0]["skip_url"]("denied-key")  # backoff kept: no --retry-assets in live mode
    live.media_types = frozenset()  # --no-media
    await asyncio.wait_for(live.sweep(), 5)
    assert media.chats == [C1]


async def test_sweep_shares_the_live_media_limit(live: AL.LiveArchive, media: _Media) -> None:
    _archived(live, C1)
    await _first_pass(live)
    media.gate.clear()
    sweep = asyncio.create_task(live.sweep())
    await asyncio.wait_for(media.entered.wait(), 5)
    assert live._media_sem._value == AL._MEDIA_CONCURRENCY - 1  # the download holds a live slot
    media.gate.set()
    await asyncio.wait_for(sweep, 5)


async def test_sweep_resumes_after_the_last_chat_done(
    live: AL.LiveArchive, media: _Media, monkeypatch
) -> None:
    _archived(live, C1, C2, C3)
    real_call = media.__call__
    held = asyncio.Event()

    async def hold_c2(*a: Any, **kw: Any) -> list[str]:
        if kw["store"].thread_id == C2:
            held.set()
            await asyncio.Event().wait()  # stopped mid-C2
        return await real_call(*a, **kw)

    monkeypatch.setattr(AL.attachments, "process", hold_c2)
    await _first_pass(live)
    sweep = asyncio.create_task(live.sweep())
    await asyncio.wait_for(held.wait(), 5)
    assert live.index.get_meta("media_sweep") == "running"
    sweep.cancel()
    await asyncio.wait({sweep})
    assert live.index.get_meta("media_sweep_after") == C1
    media.chats.clear()
    monkeypatch.setattr(AL.attachments, "process", media)
    await asyncio.wait_for(live.sweep(), 5)
    assert media.chats == [C2, C3]


async def test_run_live_sweeps_only_when_asked_and_drops_progress_otherwise(
    settings: Settings, tmp_path, media: _Media, monkeypatch
) -> None:
    from miniteams.archive_store import read_meta

    monkeypatch.setattr(AL.LiveArchive, "_pass", lambda self, force: False)
    settings.sweep_pause = 0.0
    data = tmp_path / "data"
    index = Index(data)
    for thread in (C1, C2):
        index.ensure_chat(thread)
        store = ChatStore(data, thread)
        store.insert_page([{"id": "m", "composetime": "2026-09-01T00:00:00Z", "content": IMG}])
        store.close()
    index.set_meta("media_sweep_after", C1)  # a sweep stopped after C1
    index.close()
    seen: dict[str, str] = {}

    async def connected(s, *, on_gap, **kw):  # noqa: ANN001
        await on_gap("connected")
        await asyncio.wait_for(media.entered.wait(), 5)
        seen.update(read_meta(data))  # mid-sweep heartbeat
        media.gate.set()
        await asyncio.wait_for(_until(lambda: read_meta(data)["media_sweep"] == ""), 5)  # sweep done

    media.gate.clear()

    monkeypatch.setattr(AL, "run_forever", connected)
    await AL.run_live(settings, data, token_provider=StaticToken("sk"), verify_media=True)
    assert media.chats == [C2] and seen["media_sweep"] == "running"
    assert read_meta(data)["media_sweep"] == "" and read_meta(data)["media_sweep_after"] == ""

    index = Index(data)
    index.set_meta("media_sweep_after", C1)
    index.close()
    media.chats.clear()
    await AL.run_live(settings, data, token_provider=StaticToken("sk"))
    assert media.chats == [] and read_meta(data)["media_sweep_after"] == ""  # dropped, not resumed


async def test_sweep_waits_for_a_pass_started_before_the_last_one_settled(
    live: AL.LiveArchive, media: _Media
) -> None:
    """A gap handled after pass 1 ended but before its done-callback ran: pass 2 still holds the archive."""
    _archived(live, C1)
    await live.on_gap("connected")
    first = live._catchup
    assert first is not None
    async with asyncio.timeout(5):
        while not first.done():
            await asyncio.sleep(0)  # one loop turn at a time: stop before its done-callbacks run
    live.passes.hold.clear()
    live.passes.started.clear()
    await live.on_gap("reconcile")  # pass 2, while pass 1's callbacks are still queued
    await asyncio.to_thread(live.passes.started.wait, 5)
    sweep = asyncio.create_task(live.sweep())
    await asyncio.sleep(0.05)
    assert media.chats == []  # nothing swept beside pass 2
    live.passes.hold.set()
    await _settle(live)
    await asyncio.wait_for(sweep, 5)
    assert media.chats == [C1]


async def test_live_download_gets_the_next_slot_during_a_big_sweep_chat(
    live: AL.LiveArchive, monkeypatch
) -> None:
    """The sweep shares the live limit, but a live image must not wait for a whole chat of downloads."""
    live.settings.sweep_pause = 0.0
    live.index.ensure_chat(C1)
    store = ChatStore(live.data_dir, C1)
    page = [{"id": f"s{n}", "composetime": f"2026-09-01T00:00:{n:02d}Z", "content": IMG} for n in range(20)]
    store.insert_page(page)
    store.close()
    calls: list[str] = []
    slots = asyncio.Semaphore(0)

    async def fake_process(content, msgtype, token, media_dir, download, **kw):  # noqa: ANN001
        if "LIVE" in content:
            calls.append("live")
            return []
        calls.append("sweep")
        await slots.acquire()  # a slow download: one ends per release
        return ["[image: u → local]"]

    monkeypatch.setattr(AL.attachments, "process", fake_process)
    await _first_pass(live)
    sweep = asyncio.create_task(live.sweep())
    await asyncio.wait_for(_until(lambda: len(calls) == AL._MEDIA_CONCURRENCY), 5)
    await live.on_event(_new(C2, "l1", content=IMG + " LIVE"))
    await asyncio.sleep(0.05)
    assert calls.count("sweep") == AL._MEDIA_CONCURRENCY and "live" not in calls  # every slot is busy
    slots.release()  # one sweep download ends
    await asyncio.wait_for(_until(lambda: len(calls) > AL._MEDIA_CONCURRENCY), 5)
    assert calls[AL._MEDIA_CONCURRENCY] == "live"  # the freed slot goes to the live image
    for _ in range(20):
        slots.release()
    await asyncio.wait_for(sweep, 5)
    await _media_done(live)


async def test_sweep_covers_chats_the_first_pass_adds(
    live: AL.LiveArchive, media: _Media, monkeypatch
) -> None:
    """Started on an empty index, the sweep reads the chat list only once the first pass is done."""
    monkeypatch.setattr(AL, "log", logs := _Log())
    live.settings.sweep_pause = 0.0

    def first_pass(force: frozenset[str]) -> bool:  # the pass thread has its own connections
        index = Index(live.data_dir)
        index.ensure_chat(C1)
        index.close()
        store = ChatStore(live.data_dir, C1)
        store.insert_page([{"id": "m", "composetime": "2026-09-01T00:00:00Z", "content": IMG}])
        store.close()
        return False

    monkeypatch.setattr(live, "_pass", first_pass)
    sweep = asyncio.create_task(live.sweep())
    await asyncio.sleep(0.05)
    assert logs.events == [] and live.index.get_meta("media_sweep") == ""  # nothing before the pass
    await live.on_gap("connected")
    await _settle(live)
    await asyncio.wait_for(sweep, 5)
    assert media.chats == [C1]


async def test_stop_during_a_sweep_clears_its_heartbeat_cleanly(
    settings: Settings, tmp_path, media: _Media, monkeypatch
) -> None:
    from miniteams import archive as AR
    from miniteams.archive_store import read_meta

    monkeypatch.setattr(AR, "log", logs := _Log())
    monkeypatch.setattr(AL.LiveArchive, "_pass", lambda self, force: False)
    data = tmp_path / "data"
    index = Index(data)
    index.ensure_chat(C1)
    index.close()
    store = ChatStore(data, C1)
    store.insert_page([{"id": "m", "composetime": "2026-09-01T00:00:00Z", "content": IMG}])
    store.close()
    media.gate.clear()

    async def stops_mid_sweep(s, *, on_gap, **kw):  # noqa: ANN001
        await on_gap("connected")
        await asyncio.wait_for(media.entered.wait(), 5)  # dead token while C1 downloads

    monkeypatch.setattr(AL, "run_forever", stops_mid_sweep)
    await AL.run_live(settings, data, token_provider=StaticToken("sk"), verify_media=True)
    await asyncio.sleep(0.05)  # anything still running past run_live would log now
    assert [e for e in logs.events if e["event"] == "meta_write_failed"] == []
    assert read_meta(data)["media_sweep"] == "" and media.active == 0


async def test_run_live_clears_a_killed_sweeps_heartbeat(settings: Settings, tmp_path, monkeypatch) -> None:
    from miniteams.archive_store import read_meta

    async def dead(s, **kw):  # noqa: ANN001
        return None

    monkeypatch.setattr(AL, "run_forever", dead)
    data = tmp_path / "data"
    for verify, after in ((True, C1), (False, "")):
        index = Index(data)
        index.set_meta("media_sweep", "running")  # left by a kill -9
        index.set_meta("media_sweep_after", C1)
        index.close()
        await AL.run_live(settings, data, token_provider=StaticToken("sk"), verify_media=verify)
        meta = read_meta(data)
        assert meta["media_sweep"] == "" and meta["media_sweep_after"] == after


def test_live_pass_leaves_the_sweep_heartbeat_alone(settings: Settings, tmp_path, monkeypatch) -> None:
    """A pass must not clear `media_sweep`: the live sweep owns it."""
    seen: list[dict[str, Any]] = []

    async def fake_run_archive(settings, data_dir, **kw):  # noqa: ANN001
        seen.append(kw)
        return False

    monkeypatch.setattr(AL, "run_archive", fake_run_archive)
    arch = AL.LiveArchive(settings, tmp_path / "data", StaticToken("sk"))
    try:
        assert arch._pass(frozenset()) is False
    finally:
        arch.close()
    assert seen and seen[0]["live_pass"] is True
