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
    seed = W.seed_from_archive(tmp_path, ME)
    board = W.Board({}, board_directory, seed=seed, data_dir=tmp_path, me=ME)

    await board.on_event(_message("19:known@thread.v2"))

    assert board.rows["19:known@thread.v2"]["label"] == "Jean Martin"


async def test_the_index_is_re_read_when_it_changed_since_the_seed(
    board_directory: Directory, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The archive learns about a chat after the widget started: its name must still reach the page.
    _write_index(tmp_path, "19:early@thread.v2", "Jean Martin")
    board = W.Board({}, board_directory, seed=W.seed_from_archive(tmp_path, ME), data_dir=tmp_path, me=ME)
    monkeypatch.setattr(W, "_SEED_REREAD", 0.0)  # the 30s floor is not what this test is about
    _write_index(tmp_path, "19:late@thread.v2", "Jean Dupont")

    assert await board.archived_label("19:late@thread.v2") == "Jean Dupont"


async def test_the_stream_never_reads_the_index(
    board_directory: Directory, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M7: an unnamed chat's event takes the seed's name or keeps the bare id: the index re-read may
    wait on a hung mount, and on_event must not."""
    _write_index(tmp_path, "19:early@thread.v2", "Jean Martin")
    board = W.Board({}, board_directory, seed=W.seed_from_archive(tmp_path, ME), data_dir=tmp_path, me=ME)
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
    board = W.Board({}, board_directory, seed=W.seed_from_archive(tmp_path, ME), data_dir=tmp_path, me=ME)
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
    board = W.Board({}, board_directory, seed=W.seed_from_archive(tmp_path, ME), data_dir=tmp_path, me=ME)
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
    board = W.Board({}, board_directory, seed=W.seed_from_archive(tmp_path, ME), data_dir=tmp_path, me=ME)
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
    board = W.Board({}, board_directory, seed=W.seed_from_archive(tmp_path, ME), data_dir=tmp_path, me=ME)
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
    board = W.Board({}, board_directory, seed=W.seed_from_archive(tmp_path, ME), data_dir=tmp_path, me=ME)
    stats: list[str] = []
    real = W._index_stamp

    def stamp(data_dir: Any) -> Any:
        stats.append(threading.current_thread().name)
        return real(data_dir)

    monkeypatch.setattr(W, "_index_stamp", stamp)
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
