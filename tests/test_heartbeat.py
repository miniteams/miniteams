"""Archiver liveness for readers (spec 008): the `meta` rows, the heartbeat, the state."""

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from test_archive import _FakeApi, _msg, _run, _spy_log, _wire

from miniteams import archive as AR
from miniteams import archive_live as AL
from miniteams import stream as ST
from miniteams.archive_store import Index, read_meta
from miniteams.config import Settings
from miniteams.directory import Directory

C1 = "19:alice@unq.gbl.spaces"


def _meta(data: Path) -> dict[str, str]:
    return read_meta(data)


def test_meta_rows_survive_reopen_and_replace(tmp_path: Path) -> None:
    index = Index(tmp_path)
    index.set_meta("archiver_state", "syncing")
    index.set_meta("archiver_state", "idle")
    index.close()
    assert _meta(tmp_path) == {"archiver_state": "idle"}
    assert Index(tmp_path).get_meta("nothing") == ""


def test_an_index_from_before_the_table_gets_it_and_keeps_its_rows(tmp_path: Path) -> None:
    index = Index(tmp_path)
    index.upsert_chat(C1, label="Alice")
    index.close()
    db = sqlite3.connect(tmp_path / "index.db")
    db.execute("DROP TABLE meta")
    db.commit()
    db.close()
    assert _meta(tmp_path) == {}  # a reader never adds the table
    index = Index(tmp_path)
    index.set_meta("archiver_seen", "2026-09-30T00:00:00Z")
    assert [c["label"] for c in index.chats()] == ["Alice"]
    index.close()
    assert _meta(tmp_path) == {"archiver_seen": "2026-09-30T00:00:00Z"}


def test_read_meta_never_creates_an_archive(tmp_path: Path) -> None:
    try:
        read_meta(tmp_path / "absent")
    except FileNotFoundError:
        pass
    assert not (tmp_path / "absent").exists()


async def test_a_pass_marks_syncing_then_seen_and_idle(settings: Settings, tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    seen_during: dict[str, str] = {}
    original = AR.archive_chat

    async def spy(*args: Any, **kw: Any) -> dict[str, int]:
        seen_during.update(read_meta(data))
        return await original(*args, **kw)

    monkeypatch.setattr(AR, "archive_chat", spy)
    _wire(monkeypatch, _FakeApi({C1: [_msg("a1", "2026-07-01T09:00:00Z")]}), [C1])
    await _run(settings, data)

    assert seen_during["archiver_state"] == "syncing" and seen_during["sync_started_at"]
    assert "archiver_seen" not in seen_during
    after = read_meta(data)
    assert after["archiver_state"] == "idle" and after["archiver_seen"] >= after["sync_started_at"]


async def test_a_pass_stopped_by_a_dead_credential_leaves_no_heartbeat(settings: Settings, tmp_path) -> None:
    from miniteams.auth import AuthExpired

    class _Dead:
        def token(self) -> str:
            raise AuthExpired("dead")

        bearer = token

        def sharepoint_token(self, host: str) -> None:
            return None

        def graph_token(self) -> None:
            return None

    data = tmp_path / "data"
    assert await AR.run_archive(settings, data, token_provider=_Dead()) is True
    meta = read_meta(data)
    assert meta["archiver_state"] == "syncing" and "archiver_seen" not in meta


async def test_a_locked_index_does_not_stop_the_pass(settings: Settings, tmp_path, monkeypatch) -> None:
    def locked(self: Index, key: str, value: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Index, "set_meta", locked)
    _wire(monkeypatch, _FakeApi({C1: [_msg("a1", "2026-07-01T09:00:00Z")]}), [C1])
    events = _spy_log(monkeypatch)
    await _run(settings, tmp_path / "data")

    recap = next(kw for _, event, kw in events if event == "archive_recap")
    assert recap["failed"] == 0
    assert [kw["key"] for _, event, kw in events if event == "meta_write_failed"] == [
        "archiver_state",
        "sync_started_at",
        "archiver_seen",
        "archiver_state",
    ]


async def test_live_writes_a_heartbeat_when_alive_and_following_once_drained(
    settings: Settings, tmp_path
) -> None:
    live = AL.LiveArchive(settings, tmp_path / "data", AR.StaticToken("sk"))
    try:
        await live.on_alive()
        assert set(read_meta(live.data_dir)) == {"archiver_seen"}
        live._buffering = True
        assert await live._drain() is True
        assert read_meta(live.data_dir)["archiver_state"] == "following"
    finally:
        live.close()


async def test_the_socket_reports_alive_at_connect_and_on_every_ping(
    settings: Settings, directory: Directory, monkeypatch
) -> None:
    beats: list[str] = []

    async def alive() -> None:
        beats.append("beat")

    monkeypatch.setattr(ST, "_PING_INTERVAL", 0.01)
    client = ST.TrouterClient(
        settings, {"access_token": "a"}, "sk", {}, "sid", "epid", directory, on_alive=alive
    )

    async def sent(payload: dict[str, Any]) -> None:
        pass

    monkeypatch.setattr(client, "_send_regular", sent)
    monkeypatch.setattr(client, "_authenticate", _noop)
    monkeypatch.setattr(client, "_register", _noop)
    await client._on_connected()
    assert beats == ["beat"]  # once at connect, before any ping
    await asyncio.sleep(0.05)
    for task in client._tasks:
        task.cancel()
    assert len(beats) >= 3  # then one per ping


async def _noop(*_: Any) -> None:
    pass


async def test_a_failing_alive_hook_keeps_the_socket(settings: Settings, directory: Directory) -> None:
    async def boom() -> None:
        raise RuntimeError("disk full")

    client = ST.TrouterClient(
        settings, {"access_token": "a"}, "sk", {}, "sid", "epid", directory, on_alive=boom
    )
    await client._alive()  # raises nothing


async def test_run_live_passes_the_alive_hook_to_the_stream(
    settings: Settings, tmp_path, monkeypatch
) -> None:
    hooks: dict[str, Any] = {}

    async def fake_forever(s, **kw):  # noqa: ANN001
        hooks.update(kw)
        await kw["on_alive"]()

    monkeypatch.setattr(AL, "run_forever", fake_forever)
    monkeypatch.setattr(AL.LiveArchive, "_pass", lambda self, force: False)
    await AL.run_live(settings, tmp_path / "data", token_provider=AR.StaticToken("sk"), reconcile=60)
    assert "archiver_seen" in read_meta(tmp_path / "data")
