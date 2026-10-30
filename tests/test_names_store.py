"""Names store (spec 038 phase 1): ranked names in names.db, shared by every miniteams process."""

import itertools
import json
import os
import sqlite3
import stat
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import structlog
from test_directory_fetch import _AsyncClientPost, _Resp

from miniteams import directory as directory_mod
from miniteams import names_store as NS
from miniteams.archive_store import Index
from miniteams.config import Settings
from miniteams.directory import Directory, sender_name
from miniteams.names_store import ARCHIVED, DECLARED, LIVE, NameStore, Row

ANN, BOB, CY, DEE, GONE = (f"8:orgid:{p}-0000-test" for p in ("ann", "bob", "cy", "dee", "gone"))
TAIL = {mri: mri.split(":")[-1] for mri in (ANN, BOB, CY, GONE)}  # what an unresolved MRI shows
RANKS = [LIVE, ARCHIVED, DECLARED]


def _rows(settings: Settings) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(settings.config_dir / "names.db")) as db:
        return db.execute("SELECT * FROM names ORDER BY mri").fetchall()


@pytest.fixture
def store(tmp_path: Path) -> Any:
    s = NameStore(tmp_path / "names.db")
    yield s
    s.close()


# --- 038-AC2: the ranking table ---


@pytest.mark.parametrize("first", RANKS)
@pytest.mark.parametrize("second", RANKS)
def test_a_name_gives_way_only_to_a_rank_as_high(store: NameStore, first: int, second: int) -> None:
    """038-AC2: declared < archived < live; a later name of the same rank wins."""
    store.put([(ANN, "First", first, "a", 10)])
    store.put([(ANN, "Second", second, "b", 20)])
    won = ("Second", second, False, 20) if second >= first else ("First", first, False, 10)
    assert store.get([ANN]) == {ANN: Row(*won)}


@pytest.mark.parametrize("rank", RANKS)
def test_within_a_rank_the_latest_seen_wins(store: NameStore, rank: int) -> None:
    """038-AC2: an older observation never replaces a newer one; the same instant does."""
    store.put([(ANN, "New", rank, "a", 20)])
    store.put([(ANN, "Old", rank, "b", 10)])
    assert store.get([ANN])[ANN].name == "New"
    store.put([(ANN, "Same time", rank, "c", 20)])
    assert store.get([ANN])[ANN].name == "Same time"


def test_an_empty_name_is_never_written(store: NameStore, directory: Directory) -> None:
    """038-AC2."""
    assert store.put([(ANN, "", LIVE, "profile", 10)]) == {}
    assert store.get([ANN]) == {}
    directory.note_name(BOB, "")
    directory.note_name(BOB, None)
    directory.note_declared(BOB, None, directory_mod.now_ms())
    directory.note_name(BOB, TAIL[BOB])  # the guid is no name
    assert _rows(directory.settings) == []


@pytest.mark.parametrize("rank", RANKS)
def test_a_lookup_that_names_nobody_never_erases_a_name(store: NameStore, rank: int) -> None:
    """038-AC2: the negative is noted beside the name."""
    store.put([(ANN, "Ann Lee", rank, "a", 10)])
    store.missed([ANN], 99)
    assert store.get([ANN]) == {ANN: Row("Ann Lee", rank, True, 10)}


@pytest.mark.parametrize("rank", RANKS)
def test_a_name_after_a_negative_fills_its_row(store: NameStore, rank: int) -> None:
    """038-AC2: the rank-0 row of a lookup miss takes any real name, and keeps its lookup mark."""
    store.missed([ANN], 99)
    assert store.get([ANN]) == {ANN: Row("", 0, True, 0)}
    store.put([(ANN, "Ann Lee", rank, "a", 0)])
    assert store.get([ANN]) == {ANN: Row("Ann Lee", rank, True, 0)}


async def test_an_empty_profile_answer_keeps_an_archived_name(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """038-AC2: a successful lookup with no name marks the row, the archived name stays."""
    settings.cache_dir.mkdir(parents=True)
    (settings.cache_dir / "names-v2.json").write_text(json.dumps({ANN: "Ann Lee"}))
    directory = Directory(settings)
    directory.set_token("sk", bearer="b")
    monkeypatch.setattr(
        directory_mod.httpx, "AsyncClient", lambda *a, **k: _AsyncClientPost(lambda url: _Resp({"value": []}))
    )
    await directory.resolve([ANN])
    assert directory.known_name(ANN) == "Ann Lee"
    assert directory._store is not None and directory._store.get([ANN]) == {
        ANN: Row("Ann Lee", ARCHIVED, True, 0)
    }


# --- resolve(): who the profile lookup is asked about ---


def _state(directory: Directory, state: str) -> None:
    if state in ("archived", "archived+missed"):
        assert directory._store is not None
        directory._store.put([(ANN, "Ann Archived", ARCHIVED, "test", 0)])
    if state in ("missed", "archived+missed"):
        directory._note_missed([ANN])
    if state == "declared":
        directory.note_declared(ANN, "Ann Declared", directory_mod.now_ms())
    if state == "live":
        directory.note_name(ANN, "Ann Lee")


@pytest.mark.parametrize(
    ("state", "asked", "asked_on_refresh"),
    [
        ("none", True, True),
        ("declared", True, True),
        ("archived", True, True),
        ("missed", False, True),
        ("archived+missed", False, True),
        ("live", False, False),
    ],
)
@pytest.mark.parametrize("fresh", [False, True])  # this process, or another one, wrote the state
async def test_resolve_asks_for_who_has_no_live_name_and_no_lookup_mark(
    directory: Directory,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    asked: bool,
    asked_on_refresh: bool,
    fresh: bool,
) -> None:
    _state(directory, state)
    if fresh:
        directory = Directory(directory.settings)
    directory.set_token("sk", bearer="b")
    batches: list[Any] = []

    class _Client(_AsyncClientPost):
        async def post(self, url: str, headers: Any = None, json: Any = None) -> _Resp:
            batches.append(json)
            return _Resp({"value": []})

    monkeypatch.setattr(directory_mod.httpx, "AsyncClient", lambda *a, **k: _Client(None))
    await directory.resolve([ANN])
    await directory.resolve([ANN], refresh=True)
    assert batches == [[ANN]] * asked + [[ANN]] * asked_on_refresh


async def test_refresh_names_a_person_known_only_from_the_archive(
    directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state(directory, "archived+missed")
    directory.set_token("sk", bearer="b")
    answer = {"value": [{"mri": ANN, "displayName": "Ann Lee"}]}
    monkeypatch.setattr(
        directory_mod.httpx, "AsyncClient", lambda *a, **k: _AsyncClientPost(lambda u: _Resp(answer))
    )
    await directory.resolve([ANN], refresh=True)
    assert directory.known_name(ANN) == "Ann Lee"
    assert Directory(directory.settings).known_name(ANN) == "Ann Lee"


# --- 038-AC3: processes sharing names.db ---


def test_two_processes_learning_at_once_both_keep_their_names(settings: Settings) -> None:
    """038-AC3: per-row writes; a stale instance's later write drops nobody."""
    stale = Directory(settings)
    first, second = Directory(settings), Directory(settings)
    go = threading.Barrier(2)

    def learn(directory: Directory, who: str) -> None:
        go.wait()
        for n in range(100):
            directory.note_name(f"8:orgid:{who}{n}", f"{who} {n}")

    threads = [threading.Thread(target=learn, args=(d, w)) for d, w in ((first, "Ann"), (second, "Bob"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    stale.note_name(CY, "Cy Ray")
    stale.note_declared(GONE, "Gone Declared", directory_mod.now_ms())
    third = Directory(settings)
    for who in ("Ann", "Bob"):
        assert [third.known_name(f"8:orgid:{who}{n}") for n in range(100)] == [
            f"{who} {n}" for n in range(100)
        ]
    assert stale.known_name("8:orgid:Ann7") == "Ann 7"  # a miss reads what others wrote since
    assert (third.known_name(CY), third.name_for(GONE)) == ("Cy Ray", "Gone Declared")
    assert len(_rows(settings)) == 202


def test_a_name_another_process_adds_later_is_seen(settings: Settings) -> None:
    """038-AC3: an absence is not cached."""
    reader, writer = Directory(settings), Directory(settings)
    assert reader.known_name(ANN) == "" and reader.name_for(ANN) == TAIL[ANN]
    writer.note_declared(ANN, "Ann Declared", directory_mod.now_ms())
    assert reader.name_for(ANN) == "Ann Declared"
    writer.note_name(ANN, "Ann Lee")
    assert reader.known_name(ANN) == "Ann Lee"


def test_a_lower_rank_noted_here_never_hides_a_better_name_written_elsewhere(settings: Settings) -> None:
    """038-AC2 across processes: the store's answer wins over this process's guess."""
    here, there = Directory(settings), Directory(settings)
    there.note_name(ANN, "Ann Lee")
    here.note_declared(ANN, "Zorglub", directory_mod.now_ms())
    assert here.known_name(ANN) == "Ann Lee"
    assert here.name_for(ANN) == "Ann Lee"


# --- 038-AC4: imports on first start ---


def _old_caches(settings: Settings) -> None:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    (settings.cache_dir / "names-v2.json").write_text(json.dumps({ANN: "Ann Lee", GONE: TAIL[GONE]}))
    contact = f"https://h/v1/users/ME/contacts/{CY}"
    old = {ANN: "Zorglub", BOB: "Bob Roe", CY: TAIL[CY], contact: "Cy Ray"}
    (settings.cache_dir / "names.json").write_text(json.dumps(old))


def test_first_start_imports_the_old_caches_once(settings: Settings) -> None:
    """038-AC4: names-v2.json as archived (its markers as lookup marks), names.json as declared; a second
    start imports nothing more and changes no row."""
    _old_caches(settings)
    Directory(settings)
    rows = _rows(settings)
    assert [r[:5] for r in rows] == [
        (ANN, "Ann Lee", ARCHIVED, "names-v2", 0),
        (BOB, "Bob Roe", DECLARED, "names-json", 0),
        (GONE, "", 0, "", 0),
    ]
    assert [r[5] is not None for r in rows] == [False, False, True]
    (settings.cache_dir / "names-v2.json").write_text(json.dumps({CY: "Cy Ray"}))
    second = Directory(settings)
    assert _rows(settings) == rows
    assert second.known_name(CY) == ""


@pytest.mark.parametrize("corrupt", ["names-v2.json", "names.json"])
def test_a_corrupt_old_cache_is_skipped(settings: Settings, corrupt: str) -> None:
    _old_caches(settings)
    (settings.cache_dir / corrupt).write_text("{not json")
    directory = Directory(settings)
    assert directory.known_name(ANN) == ("" if corrupt == "names-v2.json" else "Ann Lee")
    assert directory.name_for(BOB) == (TAIL[BOB] if corrupt == "names.json" else "Bob Roe")


def test_two_processes_starting_together_import_once(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """038-AC4: the import and its marker share one transaction."""
    _old_caches(settings)
    real = NameStore.import_once
    done: list[bool] = []

    def counted(self: NameStore, *args: Any) -> bool:
        done.append(real(self, *args))
        return done[-1]

    monkeypatch.setattr(NameStore, "import_once", counted)
    monkeypatch.setattr(NameStore, "imported", lambda self, key: False)  # all of them pass the cheap check
    go = threading.Barrier(4)

    def start() -> None:
        go.wait()
        Directory(settings)

    threads = [threading.Thread(target=start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(done) == [False, False, False, True]  # every start ran it, without an error
    assert len(_rows(settings)) == 3


def test_a_start_waits_for_a_writer_holding_the_store(settings: Settings) -> None:
    """038-AC3/AC4: an import behind another process's write lands once the lock is released."""
    _old_caches(settings)
    settings.config_dir.mkdir(parents=True)
    holder = sqlite3.connect(settings.config_dir / "names.db", isolation_level=None, check_same_thread=False)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.3, holder.execute, ("COMMIT",))
    release.start()
    try:
        assert Directory(settings).known_name(ANN) == "Ann Lee"
    finally:
        release.join()
        holder.close()


def _index(data_dir: Path) -> Path:
    index = Index(data_dir)
    index.upsert_chat(
        "19:a@thread.v2",
        participants=[
            {"mri": ANN, "name": "Ann Lee", "role": "Admin"},
            {"mri": GONE, "name": TAIL[GONE], "role": "User"},  # an unresolved member
            {"mri": f"https://h/v1/users/ME/contacts/{BOB}", "name": "Bob Roe"},
            "not a member",
        ],
    )
    index.upsert_chat("19:b@thread.v2", participants=[{"mri": CY, "name": "Cy Ray"}])
    index.close()
    with sqlite3.connect(data_dir / "index.db") as db:  # one damaged roster must not cost the others
        db.execute("UPDATE chats SET participants = '[' WHERE id = '19:b@thread.v2'")
    db.close()
    return data_dir / "index.db"


def test_stored_rosters_are_imported_once_read_only(settings: Settings, tmp_path: Path) -> None:
    """038-AC4: index.db rosters give archived names; the archive is left byte-identical."""
    data = tmp_path / "data"
    _index(data)
    before = (data / "index.db").read_bytes()
    directory = Directory(settings)
    directory.note_name(BOB, "Bob Live")  # a live name is never replaced by a roster's
    directory.import_rosters(data)
    assert (data / "index.db").read_bytes() == before  # SQLite's own -wal/-shm aside, as for every reader
    rows = _rows(settings)
    assert [r[:4] for r in rows] == [
        (ANN, "Ann Lee", ARCHIVED, "index-roster"),
        (BOB, "Bob Live", LIVE, "profile"),
    ]
    index = Index(data)
    index.upsert_chat("19:c@thread.v2", participants=[{"mri": CY, "name": "Cy Ray"}])
    index.close()
    directory.import_rosters(data)
    Directory(settings).import_rosters(data)
    assert _rows(settings) == rows


def test_without_an_archive_the_roster_import_waits_for_one(settings: Settings, tmp_path: Path) -> None:
    directory = Directory(settings)
    directory.import_rosters(tmp_path / "data")
    assert not (tmp_path / "data").exists()  # never brought into existence
    _index(tmp_path / "data")
    directory.import_rosters(tmp_path / "data")
    assert directory.known_name(ANN) == "Ann Lee"


# --- 038-AC7: who a message is attributed to ---


@pytest.mark.parametrize("rank", RANKS)
def test_a_known_name_beats_the_message_s_declared_name(directory: Directory, rank: int) -> None:
    """038-AC7: a live or archived name wins over imdisplayname; a declared one never does."""
    assert directory._store is not None
    directory._store.put([(ANN, "Ann Lee", rank, "test", 0)])
    shown = "Ann Lee" if rank >= ARCHIVED else "Zorglub"
    assert sender_name(ANN, "Zorglub", directory.known_name) == shown
    assert sender_name(ANN, "Zorglub", Directory(directory.settings).known_name) == shown


# --- the store failing ---


def test_an_unusable_store_leaves_names_in_memory(settings: Settings) -> None:
    (settings.config_dir / "names.db").mkdir(parents=True)  # not a database
    directory = Directory(settings)
    assert directory._store is None
    directory.note_name(ANN, "Ann Lee")
    directory.note_declared(BOB, "Bob Roe", directory_mod.now_ms())
    assert (directory.known_name(ANN), directory.name_for(BOB)) == ("Ann Lee", "Bob Roe")


def _hold(settings: Settings) -> sqlite3.Connection:
    """Another process holding names.db's write lock."""
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    holder = sqlite3.connect(settings.config_dir / "names.db", isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def _clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock = [1000.0]
    monkeypatch.setattr(directory_mod.time, "monotonic", lambda: clock[0])
    return clock


def _locked_once(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> tuple[Directory, list[float]]:
    """A Directory whose store write lock another process holds; see _hold."""
    clock = _clock(monkeypatch)
    directory = Directory(settings)
    assert directory._store is not None
    directory._store._db.execute("PRAGMA busy_timeout = 50")
    return directory, clock


def test_a_locked_store_is_logged_and_the_name_written_later(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write is queued, never lost, and lands once past the retry window."""
    directory, clock = _locked_once(settings, monkeypatch)
    holder = _hold(settings)
    try:
        with structlog.testing.capture_logs() as logs:
            directory.note_name(ANN, "Ann Lee")
            directory.note_declared(
                ANN, "Zorglub", directory_mod.now_ms()
            )  # a lower rank while ANN is queued: never wins
    finally:
        holder.close()
    assert directory.known_name(ANN) == "Ann Lee"
    assert [e["event"] for e in logs] == ["names_store_failed"]
    directory.note_name(BOB, "Bob Roe")  # inside the window: queued too
    assert _rows(settings) == []
    clock[0] += directory_mod._STORE_RETRY_AFTER
    directory.note_declared(CY, None, directory_mod.now_ms())  # any note past the window writes the queue
    fresh = Directory(settings)
    assert (fresh.known_name(ANN), fresh.known_name(BOB)) == ("Ann Lee", "Bob Roe")
    assert [r[:3] for r in _rows(settings)] == [(ANN, "Ann Lee", LIVE), (BOB, "Bob Roe", LIVE)]
    assert directory._pending == {}


def test_notes_under_a_held_lock_wait_on_it_once(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """After one failed write, notes only queue for the window: no busy-timeout stall per message."""
    directory, _ = _locked_once(settings, monkeypatch)
    holder = _hold(settings)
    try:
        with structlog.testing.capture_logs() as logs:
            directory.note_name(ANN, "Ann Lee")
            assert directory._store is not None
            directory._store._db.execute("PRAGMA busy_timeout = 2000")  # a stall per note would show
            started = time.perf_counter()
            for n in range(5):
                directory.note_declared(f"8:orgid:p{n}-test", f"P {n}", directory_mod.now_ms())
            directory._note_missed([GONE])
            elapsed = time.perf_counter() - started
    finally:
        holder.close()
    assert elapsed < 1.0
    assert [e["event"] for e in logs] == ["names_store_failed"]
    assert GONE in directory._missed and len(directory._pending) == 6


def test_the_latest_of_two_names_noted_while_the_store_fails_wins(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer name noted while an older one is being written survives that write failing."""
    directory, clock = _locked_once(settings, monkeypatch)
    real_put = NameStore.put
    calls: list[int] = []
    monkeypatch.setattr(directory_mod, "now_ms", lambda: 10)  # the same instant: the later note wins

    def put(self: NameStore, entries: Any) -> dict[str, Row]:
        calls.append(1)
        if len(calls) == 1:  # another thread notes a newer name while this write runs
            directory.note_name(ANN, "Ann New")
        if len(calls) <= 2:  # and both writes fail, the older one last
            raise sqlite3.OperationalError("database is locked")
        return real_put(self, entries)

    monkeypatch.setattr(NameStore, "put", put)
    directory.note_name(ANN, "Ann Old")
    assert directory._pending[ANN, LIVE][1] == "Ann New"
    clock[0] += directory_mod._STORE_RETRY_AFTER
    directory.note_declared(CY, None, directory_mod.now_ms())
    assert [r[:2] for r in _rows(settings)] == [(ANN, "Ann New")]
    assert directory.known_name(ANN) == "Ann New"


def test_two_names_noted_under_a_held_lock_keep_the_later(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, clock = _locked_once(settings, monkeypatch)
    stamps = itertools.count(10, 10)
    monkeypatch.setattr(directory_mod, "now_ms", lambda: next(stamps))
    holder = _hold(settings)
    try:
        directory.note_name(ANN, "Ann Old")
        directory.note_name(ANN, "Ann New")
    finally:
        holder.close()
    clock[0] += directory_mod._STORE_RETRY_AFTER
    directory._put([])
    assert [r[:2] for r in _rows(settings)] == [(ANN, "Ann New")]
    assert directory.known_name(ANN) == "Ann New"


def test_a_queued_name_never_beats_a_better_one_written_meanwhile(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """038-AC2: the queue goes through the store's rank rule, not this process's memory."""
    directory, clock = _locked_once(settings, monkeypatch)
    holder = _hold(settings)
    try:
        directory.note_declared(ANN, "Zorglub", directory_mod.now_ms())
    finally:
        holder.close()
    Directory(settings).note_name(ANN, "Ann Lee")
    clock[0] += directory_mod._STORE_RETRY_AFTER
    directory.note_name(BOB, "Bob Roe")
    assert [r[:3] for r in _rows(settings)] == [(ANN, "Ann Lee", LIVE), (BOB, "Bob Roe", LIVE)]
    assert directory.known_name(ANN) == "Ann Lee"


def test_a_store_unavailable_at_start_is_reopened_later(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A days-long process gets names.db back: at most one open per window; pending names land once."""
    holder = _hold(settings)
    opens: list[Path] = []
    real = directory_mod._open_store

    def counted(path: Path) -> NameStore | None:
        opens.append(path)
        return real(path)

    monkeypatch.setattr(directory_mod, "_open_store", counted)
    monkeypatch.setattr(NS, "_BUSY_TIMEOUT", 0.05)
    written: list[str] = []
    real_put = NameStore.put

    def put(self: NameStore, entries: Any) -> dict[str, Row]:
        entries = list(entries)
        written.extend(e[0] for e in entries)
        return real_put(self, entries)

    monkeypatch.setattr(NameStore, "put", put)
    clock = [1000.0]
    monkeypatch.setattr(directory_mod.time, "monotonic", lambda: clock[0])
    try:
        with structlog.testing.capture_logs() as logs:
            directory = Directory(settings)
            directory.note_name(ANN, "Ann Lee")
    finally:
        holder.close()
    assert directory._store is None and len(opens) == 1
    assert [e["event"] for e in logs] == ["names_store_unavailable"]
    clock[0] += directory_mod._STORE_RETRY_AFTER - 1
    directory.note_name(BOB, "Bob Roe")  # inside the window: no second open
    assert directory.known_name(BOB) == "Bob Roe" and len(opens) == 1
    clock[0] += 1
    directory.note_name(CY, "Cy Ray")
    assert len(opens) == 2 and directory._store is not None
    assert [r[:2] for r in _rows(settings)] == [(ANN, "Ann Lee"), (BOB, "Bob Roe"), (CY, "Cy Ray")]
    assert directory._pending == {} and sorted(written) == [ANN, BOB, CY]  # each written once


def _counted_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    reads: list[Path] = []
    real_entries = directory_mod.roster_entries

    def read(index: Path) -> list[Any]:
        reads.append(index)
        return real_entries(index)

    monkeypatch.setattr(directory_mod, "roster_entries", read)
    return reads


def test_a_roster_import_without_a_store_waits_for_it(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """038-AC4: the import retries on its own until names.db opens, and reads the archive once."""
    data = tmp_path / "data"
    _index(data)
    reads = _counted_reads(monkeypatch)
    monkeypatch.setattr(NS, "_BUSY_TIMEOUT", 0.05)
    clock = _clock(monkeypatch)
    holder = _hold(settings)
    try:
        directory = Directory(settings)
    finally:
        holder.close()
    assert directory._store is None
    waited: list[float] = []

    def wait(seconds: float) -> None:
        waited.append(seconds)
        clock[0] += seconds

    directory.import_rosters(data, wait)
    assert waited == [directory_mod._STORE_RETRY_AFTER] and reads == [data / "index.db"]
    key = f"{directory_mod._ROSTER_IMPORT}:{(data / 'index.db').resolve()}"
    assert directory._store is not None and directory._store.imported(key)
    assert Directory(settings).known_name(ANN) == "Ann Lee"
    directory.import_rosters(data, wait)
    assert len(reads) == 1  # done: never read again


def test_a_read_reopens_the_store_too(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    holder = _hold(settings)
    monkeypatch.setattr(NS, "_BUSY_TIMEOUT", 0.05)
    clock = [1000.0]
    monkeypatch.setattr(directory_mod.time, "monotonic", lambda: clock[0])
    try:
        directory = Directory(settings)
    finally:
        holder.close()
    assert directory._store is None
    Directory(settings).note_name(ANN, "Ann Lee")
    clock[0] += directory_mod._STORE_RETRY_AFTER / 2
    directory.note_name(BOB, "Bob Roe")  # no store yet: queued, and writes pause
    assert directory.known_name(ANN) == ""  # inside the window
    clock[0] += directory_mod._STORE_RETRY_AFTER
    assert directory.known_name(ANN) == "Ann Lee"  # a read reopens it, and the queue is written then
    assert Directory(settings).known_name(BOB) == "Bob Roe"


def test_the_store_is_owner_only(settings: Settings) -> None:
    old = os.umask(0o022)
    try:
        directory = Directory(settings)
        directory.note_name(ANN, "Ann Lee")
    finally:
        os.umask(old)
    assert stat.S_IMODE(settings.config_dir.stat().st_mode) == 0o700
    for name in ("names.db", "names.db-wal", "names.db-shm"):
        assert stat.S_IMODE((settings.config_dir / name).stat().st_mode) == 0o600, name


def test_now_is_epoch_milliseconds() -> None:
    assert 1_700_000_000_000 < NS.now_ms() < 10_000_000_000_000


# --- concurrency at the SQL level, and the failure paths ---


def _commit_later(holder: sqlite3.Connection) -> threading.Timer:
    timer = threading.Timer(0.3, holder.execute, ("COMMIT",))
    timer.start()
    return timer


def test_an_import_another_process_is_committing_is_not_done_twice(store: NameStore, tmp_path: Path) -> None:
    """038-AC4: the marker check waits for the writer (BEGIN IMMEDIATE); a deferred read would race it."""
    with closing(
        sqlite3.connect(tmp_path / "names.db", isolation_level=None, check_same_thread=False)
    ) as holder:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO meta (key, value) VALUES ('import:x', '1')")
        timer = _commit_later(holder)
        try:
            assert store.import_once("import:x", [(ANN, "Ann Lee", ARCHIVED, "x", 0)], [BOB], 5) is False
        finally:
            timer.join()
    assert store.get([ANN, BOB]) == {}


def test_a_store_opened_while_another_creates_it_opens(tmp_path: Path) -> None:
    """Schema creation waits for the other process's (BEGIN IMMEDIATE), then finds its tables."""
    path = tmp_path / "names.db"
    with closing(sqlite3.connect(path, isolation_level=None, check_same_thread=False)) as holder:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("BEGIN IMMEDIATE")
        for statement in NS._SCHEMA.split(";")[:2]:
            holder.execute(statement)
        holder.execute("INSERT INTO names (mri, name, rank) VALUES (?, 'Ann Lee', 3)", (ANN,))
        timer = _commit_later(holder)
        try:
            store = NameStore(path)
        finally:
            timer.join()
    assert store.get([ANN]) == {ANN: Row("Ann Lee", LIVE, False, 0)}
    store.close()


class _Wrapped:
    """A connection whose WAL switch answers `mode`, or fails with `error`."""

    def __init__(self, db: sqlite3.Connection, mode: str, error: bool) -> None:
        self._db, self._mode, self._error = db, mode, error

    def execute(self, sql: str, *args: Any) -> Any:
        if sql == "PRAGMA journal_mode=WAL":
            if self._error:
                raise sqlite3.OperationalError("database is locked")
            return self._db.execute("SELECT ?", (self._mode,))
        return self._db.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


def _connections(monkeypatch: pytest.MonkeyPatch, mode: str = "wal", error: bool = False) -> list[Any]:
    made: list[Any] = []
    real = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> Any:
        made.append(_Wrapped(real(*args, **kwargs), mode, error))
        return made[-1]

    monkeypatch.setattr(NS.sqlite3, "connect", connect)
    monkeypatch.setattr(NS.time, "sleep", lambda _: None)
    return made


@pytest.mark.parametrize(("mode", "error"), [("delete", False), ("wal", True)])
def test_a_store_that_never_reaches_wal_is_unavailable_and_closed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, mode: str, error: bool
) -> None:
    made = _connections(monkeypatch, mode, error)
    with structlog.testing.capture_logs() as logs:
        directory = Directory(settings)
    assert directory._store is None and len(made) == 1
    assert [e["event"] for e in logs] == ["names_store_unavailable"]
    with pytest.raises(sqlite3.ProgrammingError):  # the failed open closed its connection
        made[0].execute("SELECT 1")
    directory.note_name(ANN, "Ann Lee")
    assert directory.known_name(ANN) == "Ann Lee"


def test_the_wal_switch_is_retried_until_it_takes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["delete", "delete", "wal"])
    made = _connections(monkeypatch)
    real_execute = _Wrapped.execute

    def execute(self: _Wrapped, sql: str, *args: Any) -> Any:
        if sql == "PRAGMA journal_mode=WAL":
            return self._db.execute("SELECT ?", (next(answers),))
        return real_execute(self, sql, *args)

    monkeypatch.setattr(_Wrapped, "execute", execute)
    NameStore(tmp_path / "names.db").close()
    assert len(made) == 1 and next(answers, None) is None


def test_a_failed_roster_import_is_retried(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """038-AC4: an unreadable index imports nothing and marks nothing; the next start imports."""
    _index(tmp_path / "data")
    real = directory_mod.roster_entries
    calls: list[Path] = []

    def flaky(index: Path) -> list[Any]:
        calls.append(index)
        if len(calls) == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real(index)

    monkeypatch.setattr(directory_mod, "roster_entries", flaky)
    directory = Directory(settings)
    with structlog.testing.capture_logs() as logs:
        directory.import_rosters(tmp_path / "data")
    assert [e["event"] for e in logs] == ["names_roster_import_failed"] and _rows(settings) == []
    directory.import_rosters(tmp_path / "data")
    assert Directory(settings).known_name(ANN) == "Ann Lee" and len(calls) == 2


async def test_a_failed_lookup_records_nothing(directory: Directory, monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient failure is not a lookup that named nobody: the MRI stays due."""
    directory.set_token("sk", bearer="b")
    monkeypatch.setattr(
        directory_mod.httpx, "AsyncClient", lambda *a, **k: _AsyncClientPost(lambda url: _Resp(fail=True))
    )
    await directory.resolve([ANN])
    assert _rows(directory.settings) == [] and directory._lookup_due(ANN, False)
    assert directory._store is not None and directory._store.get([]) == {}


def test_a_name_noted_again_is_not_written_again(
    directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One write per new name, not one per message."""
    writes: list[int] = []
    real_put = NameStore.put

    def put(self: NameStore, entries: Any) -> dict[str, Row]:
        writes.append(1)
        return real_put(self, entries)

    monkeypatch.setattr(NameStore, "put", put)
    for _ in range(3):
        directory.note_declared(ANN, "Ann Declared", directory_mod.now_ms())
        directory.note_name(BOB, "Bob Roe")
    assert len(writes) == 2


def test_a_failed_lookup_mark_pauses_writes_too(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    directory, _ = _locked_once(settings, monkeypatch)
    holder = _hold(settings)
    try:
        with structlog.testing.capture_logs() as logs:
            directory._note_missed([GONE])
            directory.note_name(ANN, "Ann Lee")  # inside the window: queued without waiting on the lock
    finally:
        holder.close()
    assert [e["event"] for e in logs] == ["names_store_failed"]
    assert GONE in directory._missed and (ANN, LIVE) in directory._pending


def test_each_archive_s_rosters_are_imported(settings: Settings, tmp_path: Path) -> None:
    """038-AC4: one import marker per index.db: a scratch archive does not mark the real one done."""
    _index(tmp_path / "scratch")
    real = tmp_path / "real"
    index = Index(real)
    index.upsert_chat("19:r@thread.v2", participants=[{"mri": DEE, "name": "Dee Ray"}])
    index.close()
    directory = Directory(settings)
    directory.import_rosters(tmp_path / "scratch")
    directory.import_rosters(real)
    fresh = Directory(settings)
    assert (fresh.known_name(ANN), fresh.known_name(DEE)) == ("Ann Lee", "Dee Ray")


def test_a_broken_store_is_left_alone_for_the_window(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reads and writes stop hitting a failing names.db: one failure logged, not one per message."""
    clock = _clock(monkeypatch)
    directory = Directory(settings)
    assert directory._store is not None
    directory._store._db.close()
    with structlog.testing.capture_logs() as logs:
        for n in range(100):
            directory.name_for(f"8:orgid:p{n}-test")
            directory.note_declared(f"8:orgid:p{n}-test", f"P {n}", directory_mod.now_ms())
    assert [e["event"] for e in logs] == ["names_store_failed"]
    assert directory.name_for("8:orgid:p7-test") == "P 7"  # memory still answers
    clock[0] += directory_mod._STORE_RETRY_AFTER
    with structlog.testing.capture_logs() as logs:
        directory.name_for(GONE)
    assert [e["event"] for e in logs] == ["names_store_failed"]  # tried again past the window


def test_a_roster_import_during_a_pause_runs_after_it(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    _index(data)
    directory, clock = _locked_once(settings, monkeypatch)
    holder = _hold(settings)
    try:
        directory.note_name(BOB, "Bob Live")  # fails: the store pauses
    finally:
        holder.close()
    directory.import_rosters(data, lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert [r[:3] for r in _rows(settings)] == [(ANN, "Ann Lee", ARCHIVED), (BOB, "Bob Roe", ARCHIVED)]
    directory.note_declared(CY, None, directory_mod.now_ms())  # the queued live name lands over the roster's
    assert [r[:3] for r in _rows(settings)] == [(ANN, "Ann Lee", ARCHIVED), (BOB, "Bob Live", LIVE)]


def test_a_roster_import_failing_after_the_read_reads_again_once_resumed(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    _index(data)
    clock = _clock(monkeypatch)
    directory = Directory(settings)
    reads = _counted_reads(monkeypatch)
    real = NameStore.import_once
    fails = [1]

    def import_once(self: NameStore, *args: Any) -> bool:
        if fails:
            fails.pop()
            raise sqlite3.OperationalError("database is locked")
        return real(self, *args)

    monkeypatch.setattr(NameStore, "import_once", import_once)
    directory.import_rosters(data, lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert len(reads) == 2 and Directory(settings).known_name(ANN) == "Ann Lee"


def test_a_throwaway_directory_s_roster_import_lands(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archiver and MCP start it on a Directory nobody keeps: its thread retries by itself."""
    data = tmp_path / "data"
    _index(data)
    monkeypatch.setattr(NS, "_BUSY_TIMEOUT", 0.02)
    monkeypatch.setattr(directory_mod, "_STORE_RETRY_AFTER", 0.05)
    holder = _hold(settings)
    try:
        Directory(settings).start_roster_import(data)
        time.sleep(0.2)  # a few failed attempts while the lock is held
    finally:
        holder.close()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with closing(sqlite3.connect(settings.config_dir / "names.db")) as db:
            if db.execute("SELECT name FROM sqlite_master WHERE name = 'names'").fetchone() and _rows(
                settings
            ):
                break
        time.sleep(0.02)
    assert [r[:2] for r in _rows(settings)] == [(ANN, "Ann Lee"), (BOB, "Bob Roe")]


def test_a_cache_import_that_failed_runs_once_the_store_answers(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """038-AC4: a failed first-start import is not skipped for the life of the process."""
    _old_caches(settings)
    clock = _clock(monkeypatch)
    real = NameStore.import_once
    fails = [1]

    def import_once(self: NameStore, *args: Any) -> bool:
        if fails:
            fails.pop()
            raise sqlite3.OperationalError("database is locked")
        return real(self, *args)

    monkeypatch.setattr(NameStore, "import_once", import_once)
    directory = Directory(settings)
    assert directory.known_name(ANN) == ""  # paused: nothing imported, nothing read
    clock[0] += directory_mod._STORE_RETRY_AFTER
    assert directory.known_name(ANN) == "Ann Lee"
    assert Directory(settings).name_for(BOB) == "Bob Roe"


def test_a_due_import_that_fails_again_spares_the_call_behind_it(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One failure per window: the read that ran the failing import does not touch the store too."""
    _old_caches(settings)
    clock = _clock(monkeypatch)
    reads: list[int] = []
    real_get = NameStore.get

    def import_once(self: NameStore, *args: Any) -> bool:
        raise sqlite3.OperationalError("database is locked")

    def get(self: NameStore, mris: Any) -> dict[str, Row]:
        reads.append(1)
        return real_get(self, mris)

    monkeypatch.setattr(NameStore, "import_once", import_once)
    monkeypatch.setattr(NameStore, "get", get)
    directory = Directory(settings)
    clock[0] += directory_mod._STORE_RETRY_AFTER
    with structlog.testing.capture_logs() as logs:
        assert directory.known_name(ANN) == ""
    assert [e["event"] for e in logs] == ["names_store_failed"] and reads == []


def test_a_cache_import_whose_marker_read_failed_lands_after_the_window(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _old_caches(settings)
    clock = _clock(monkeypatch)
    real = NameStore.imported
    fails = [1]

    def imported(self: NameStore, key: str) -> bool:
        if fails:
            fails.pop()
            raise sqlite3.OperationalError("database is locked")
        return real(self, key)

    monkeypatch.setattr(NameStore, "imported", imported)
    directory = Directory(settings)
    assert directory.known_name(ANN) == ""  # paused
    clock[0] += directory_mod._STORE_RETRY_AFTER
    assert directory.known_name(ANN) == "Ann Lee"
