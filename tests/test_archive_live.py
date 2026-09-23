"""Live follower (spec 005): buffer during the catch-up pass, drain, follow, re-catch-up on gaps."""

import asyncio
import json
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


async def test_conversation_update_merges_into_the_index(live: AL.LiveArchive) -> None:
    live.index.upsert_chat(C1, label="Alice", raw={"properties": {"consumptionhorizon": "1", "a": 1}})
    update = {"id": C1, "properties": {"consumptionhorizon": "2"}}
    await live.on_event({"type": "EventMessage", "resourceType": "ConversationUpdate", "resource": update})
    assert live.index.chats()[0]["raw"]["properties"] == {"consumptionhorizon": "2", "a": 1}


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
