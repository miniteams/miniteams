"""Spec 004 phase 3: the archive as a fallback name, its re-read, and a writer working next to us."""

import threading
from typing import Any

import pytest

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

    await board.on_event(_message("19:late@thread.v2"))

    assert board.rows["19:late@thread.v2"]["label"] == "Jean Dupont"


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
