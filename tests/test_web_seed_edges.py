"""Spec 004 phase 3: the archive as a fallback name, its re-read, and a writer working next to us."""

import asyncio
import threading
import time
from typing import Any

import pytest
import structlog.testing
from conftest import wait

from miniteams import web as W
from miniteams.archive_store import Index
from miniteams.directory import Directory

ME = "8:orgid:me"


@pytest.fixture
def board_directory(directory: Directory, monkeypatch: pytest.MonkeyPatch) -> Directory:
    """A Directory whose label lookup always fails, the way a 429 or a removal does."""
    directory.me = ME

    async def failing_label(self: Directory, thread_id: str) -> str:
        return thread_id  # the contract for "could not name it"

    async def fake_display(self: Directory, mri: str) -> str:
        return f"name:{mri}"

    monkeypatch.setattr(Directory, "label", failing_label)
    monkeypatch.setattr(Directory, "display", fake_display)
    return directory


def _write_index(data_dir: Any, thread_id: str, name: str) -> None:
    index = Index(data_dir)
    index.upsert_chat(
        thread_id,
        participants=[{"mri": "8:orgid:x", "name": name}],
        raw={"id": thread_id, "lastMessage": {"composetime": "2026-09-20T10:00:00Z", "id": "1"}},
    )
    index.close()


def _seeded_board(directory: Directory, data_dir: Any) -> W.Board:
    """A board seeded the way run() seeds it: the index and its stamp from one read."""
    stamp, seed = W._reseed(data_dir, ME, None)
    return W.Board({}, directory, seed=seed, data_dir=data_dir, me=ME, seed_stamp=stamp)


def _record_stamp_threads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The names of the threads `_index_stamp` runs on, in call order."""
    stats: list[str] = []
    real = W._index_stamp

    def stamp(data_dir: Any) -> Any:
        stats.append(threading.current_thread().name)
        return real(data_dir)

    monkeypatch.setattr(W, "_index_stamp", stamp)
    return stats


def _message(thread_id: str, text: str = "hi") -> dict[str, Any]:
    return {
        "resourceType": "NewMessage",
        "resource": {
            "id": "1790000000000",
            "messagetype": "Text",
            "content": text,
            "composetime": "2026-09-23T10:00:00Z",
            "conversationLink": f"https://x/conversations/{thread_id}",
            "from": "https://x/contacts/8:orgid:x",
            "imdisplayname": "Jean Martin",
        },
    }


async def test_a_chat_the_lookup_cannot_name_takes_the_archive_s_name(
    board_directory: Directory, tmp_path: Any
) -> None:
    _write_index(tmp_path, "19:known@thread.v2", "Jean Martin")
    board = _seeded_board(board_directory, tmp_path)

    await board.on_event(_message("19:known@thread.v2"))

    assert board.rows["19:known@thread.v2"]["label"] == "Jean Martin"


async def test_the_index_is_re_read_when_it_changed_since_the_seed(
    board_directory: Directory, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The archive learns about a chat after the widget started: its name must still reach the page.
    _write_index(tmp_path, "19:early@thread.v2", "Jean Martin")
    board = _seeded_board(board_directory, tmp_path)
    monkeypatch.setattr(W, "_SEED_REREAD", 0.0)  # the 30s floor is not what this test is about
    _write_index(tmp_path, "19:late@thread.v2", "Jean Dupont")

    assert await board.archived_label("19:late@thread.v2") == "Jean Dupont"


async def test_a_live_archiver_s_commit_is_re_read(
    board_directory: Directory, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R11: `archive --live` keeps its index open, so commits sit in index.db-wal, not index.db; after a
    checkpoint the next commit rewrites the -wal in place, same size: only its mtime moves."""
    _write_index(tmp_path, "19:early@thread.v2", "Jean Martin")
    board = _seeded_board(board_directory, tmp_path)
    monkeypatch.setattr(W, "_SEED_REREAD", 0.0)
    live = Index(tmp_path)

    def commit(thread_id: str, name: str) -> None:
        live.upsert_chat(
            thread_id,
            participants=[{"mri": "8:orgid:x", "name": name}],
            raw={"id": thread_id, "lastMessage": {"composetime": "2026-09-20T10:00Z", "id": "1"}},
        )

    try:
        commit("19:late@thread.v2", "Jean Dupont")
        assert await board.archived_label("19:late@thread.v2") == "Jean Dupont"
        live._db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        assert await board.archived_label("19:nobody@thread.v2") == ""  # takes the checkpoint's stamp
        commit("19:later@thread.v2", "Paul Durand")
        assert await board.archived_label("19:later@thread.v2") == "Paul Durand"
    finally:
        live.close()


async def test_the_stream_never_reads_the_index(
    board_directory: Directory, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M7: an unnamed chat's event takes the seed's name or keeps the bare id: the index re-read may
    wait on a hung mount, and on_event must not."""
    _write_index(tmp_path, "19:early@thread.v2", "Jean Martin")
    board = _seeded_board(board_directory, tmp_path)
    monkeypatch.setattr(W, "_SEED_REREAD", 0.0)
    monkeypatch.setattr(W, "_reseed", lambda *a: pytest.fail("the stream read the index"))
    await asyncio.wait_for(board.on_event(_message("19:late@thread.v2")), 0.5)
    await asyncio.wait_for(board.on_event(_message("19:early@thread.v2")), 0.5)
    assert board.rows["19:late@thread.v2"]["label"] == "19:late@thread.v2"
    assert board.rows["19:early@thread.v2"]["label"] == "Jean Martin"


async def test_an_index_timeout_names_nothing(
    board_directory: Directory, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M13: past the index deadline the archive name lookup is "" and the caller moves on."""
    from miniteams import chat_verbs as CV

    _write_index(tmp_path, "19:early@thread.v2", "Jean Martin")
    board = _seeded_board(board_directory, tmp_path)
    monkeypatch.setattr(W, "_SEED_REREAD", 0.0)
    monkeypatch.setitem(CV._DEADLINES, "index", 0.05)
    release = threading.Event()
    monkeypatch.setattr(W, "_reseed", lambda *a: release.wait(5))
    try:
        assert await asyncio.wait_for(board.archived_label("19:late@thread.v2"), 1) == ""
    finally:
        release.set()


async def test_an_unchanged_index_is_not_re_read(
    board_directory: Directory, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_index(tmp_path, "19:known@thread.v2", "Jean Martin")
    board = _seeded_board(board_directory, tmp_path)
    monkeypatch.setattr(W, "_SEED_REREAD", 0.0)
    reads = []
    monkeypatch.setattr(W, "seed_from_archive", lambda *a: reads.append(a) or {})

    assert await board.archived_label("19:unknown@thread.v2") == ""
    assert reads == []  # same file: nothing new to learn


async def test_the_re_read_waits_out_its_floor(
    board_directory: Directory, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every unnamed message must not turn into a 12 MiB read.
    _write_index(tmp_path, "19:early@thread.v2", "Jean Martin")
    board = _seeded_board(board_directory, tmp_path)
    _write_index(tmp_path, "19:late@thread.v2", "Jean Dupont")  # the file did move
    reads = []
    monkeypatch.setattr(W, "seed_from_archive", lambda *a: reads.append(a) or {})

    assert await board.archived_label("19:late@thread.v2") == ""
    assert reads == []  # too soon after the first read


async def test_concurrent_misses_read_the_index_once(
    board_directory: Directory, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A reconnect replays what the stream dropped: several unnamed chats land together, and each
    # one starting its own read of a 12 MiB index is how a fallback becomes an outage.
    _write_index(tmp_path, "19:early@thread.v2", "Jean Martin")
    board = _seeded_board(board_directory, tmp_path)
    monkeypatch.setattr(W, "_SEED_REREAD", 0.0)
    _write_index(tmp_path, "19:late@thread.v2", "Jean Dupont")  # the file moved: a re-read is due
    reads: list[Any] = []

    def slow_read(data_dir: Any, me: str = "") -> dict[str, Any]:
        reads.append(data_dir)
        time.sleep(0.05)  # wide enough for the others to barge in if nothing holds them
        return {}

    monkeypatch.setattr(W, "seed_from_archive", slow_read)

    await asyncio.gather(*(board.archived_label(f"19:miss{i}@thread.v2") for i in range(4)))

    assert len(reads) == 1


async def test_no_archive_means_no_fallback_and_no_crash(board_directory: Directory, tmp_path: Any) -> None:
    board = W.Board({}, board_directory, data_dir=tmp_path / "nowhere", me=ME)
    await board.on_event(_message("19:orphan@thread.v2"))
    assert board.rows["19:orphan@thread.v2"]["label"] == "19:orphan@thread.v2"


def test_the_seed_reads_while_the_archive_writes(tmp_path: Any) -> None:
    # `archive --loop` runs next to the widget: WAL plus a read-only open means no lock, no wait.
    _write_index(tmp_path, "19:a@thread.v2", "Jean Martin")
    failed: list[str] = []

    def writer() -> None:
        index = Index(tmp_path)  # sqlite connections belong to the thread that made them
        try:
            for i in range(30):
                index.upsert_chat(f"19:w{i}@thread.v2", participants=[{"mri": "8:orgid:y", "name": "W"}])
        except Exception as exc:  # noqa: BLE001 — the reader must not be why a write fails
            failed.append(str(exc))
        finally:
            index.close()

    thread = threading.Thread(target=writer)
    thread.start()
    seen = [len(W.seed_from_archive(tmp_path, ME)) for _ in range(30)]
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert failed == []  # the writer was never blocked by us
    assert min(seen) >= 1  # and every read returned rows, none raised


async def test_the_index_stat_runs_off_the_loop_and_only_past_the_floor(
    board_directory: Directory, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4: an unnamed event inside the floor costs no stat at all; past it, the stat runs on an archive
    thread with the re-read, never on the loop."""
    _write_index(tmp_path, "19:early@thread.v2", "Jean Martin")
    board = _seeded_board(board_directory, tmp_path)
    stats = _record_stamp_threads(monkeypatch)
    assert await board.archived_label("19:late@thread.v2") == ""
    assert stats == []
    monkeypatch.setattr(W, "_SEED_REREAD", 0.0)
    _write_index(tmp_path, "19:late@thread.v2", "Jean Dupont")
    assert await board.archived_label("19:late@thread.v2") == "Jean Dupont"
    assert len(stats) == 1 and stats[0].startswith("archive")


async def test_a_seed_past_its_deadline_starts_an_empty_board(
    settings: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M13: a hung index at start: the board starts empty, the listing fills it, the log says why."""
    from miniteams import chat_verbs as CV

    monkeypatch.setitem(CV._DEADLINES, "index", 0.05)
    release = threading.Event()
    monkeypatch.setattr(W, "seed_from_archive", lambda *a: release.wait(5) and {"x": {}})
    boards: list[W.Board] = []

    async def serve(board: W.Board, *a: Any) -> None:
        boards.append(board)
        await asyncio.Event().wait()

    async def fill(board: W.Board, *a: Any) -> None:
        release.set()  # the hung read ends before run() checks for stuck threads
        await asyncio.Event().wait()

    async def ended(*a: Any, **kw: Any) -> None:
        await wait(lambda: bool(boards), "served")

    monkeypatch.setattr(W, "serve_board", serve)
    monkeypatch.setattr(W, "fill_from_listing", fill)
    monkeypatch.setattr(W, "run_forever", ended)
    monkeypatch.setattr(W.EmojiCache, "refresh", fill)  # no token fetch
    with structlog.testing.capture_logs() as logs, pytest.raises(RuntimeError, match="live stream ended"):
        await W.run(settings, "sk", "", None, None, data_dir=tmp_path)
    assert [board.rows for board in boards] == [{}]
    assert [e["event"] for e in logs if e["event"].startswith("archive_index")] == ["archive_index_timeout"]


async def test_an_interrupted_seed_still_leaves_when_its_read_hangs(
    settings: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R9: Ctrl-C during a hung index read exits at once, or the process would keep the run-dir lock."""

    async def interrupted(*a: Any) -> None:
        raise asyncio.CancelledError  # what asyncio.run turns a Ctrl-C into inside run()

    exits = _record_exits(monkeypatch)
    monkeypatch.setattr(W, "io", interrupted)
    monkeypatch.setattr(W, "stuck_archive_threads", lambda *a: 1)
    with pytest.raises(SystemExit):
        await W.run(settings, "sk", "", None, None, data_dir=tmp_path)
    assert exits == [0]


def _record_exits(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    exits: list[int] = []

    def leave(code: int) -> None:
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(W.os, "_exit", leave)
    return exits


def _stream_ends_once_served(monkeypatch: pytest.MonkeyPatch) -> None:
    served = asyncio.Event()

    async def serve(*a: Any) -> None:
        served.set()
        await asyncio.Event().wait()

    async def idle(*a: Any, **kw: Any) -> None:
        await asyncio.Event().wait()

    async def ended(*a: Any, **kw: Any) -> None:
        await served.wait()

    monkeypatch.setattr(W, "serve_board", serve)
    monkeypatch.setattr(W, "fill_from_listing", idle)
    monkeypatch.setattr(W, "run_forever", ended)
    monkeypatch.setattr(W.EmojiCache, "refresh", idle)  # no token fetch


async def test_stuck_archive_threads_force_the_exit_when_the_stream_ends(
    settings: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream's end is a failure: with a read still hung, run() leaves at once with code 1."""
    _stream_ends_once_served(monkeypatch)
    exits = _record_exits(monkeypatch)
    monkeypatch.setattr(W, "stuck_archive_threads", lambda *a: 1)
    with pytest.raises(SystemExit):
        await W.run(settings, "sk", "", None, None, data_dir=tmp_path)
    assert exits == [1]


async def test_run_stats_the_index_off_the_loop(
    settings: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: a stat on the loop hangs it on a dead mount, where Ctrl-C can no longer get out."""
    _write_index(tmp_path, "19:early@thread.v2", "Jean Martin")
    _stream_ends_once_served(monkeypatch)
    stats = _record_stamp_threads(monkeypatch)
    with pytest.raises(RuntimeError, match="live stream ended"):
        await W.run(settings, "sk", "", None, None, data_dir=tmp_path)
    assert stats and all(name.startswith("archive") for name in stats)
