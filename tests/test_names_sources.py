"""Names store (spec 038 phase 2): the names call records, member events and senders give, archived
live, on backfill and by `miniteams names --sweep`."""

import hashlib
import html
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import structlog
from test_archive import _FakeApi, _run, _wire
from test_directory_fetch import _AsyncClientPost, _Resp
from test_names_store import _clock, _hold

from miniteams import archive as AR
from miniteams import archive_live as AL
from miniteams import archive_store as AS
from miniteams import cli
from miniteams import directory as directory_mod
from miniteams import names_store as NS
from miniteams import web as W
from miniteams.archive_store import ChatStore, Index, name_fields
from miniteams.config import Settings
from miniteams.directory import Directory
from miniteams.messages import message_names
from miniteams.names_store import ARCHIVED, DECLARED, LIVE, NameStore

ANN, BOB, CY, DEE, EVE, FAY, GONE = (
    f"8:orgid:{p}-0000-test" for p in ("ann", "bob", "cy", "dee", "eve", "fay", "gone")
)
C1, C2, C3, C4 = (f"19:{c}@thread.v2" for c in ("calls", "events", "corrupt", "missing"))
T1, T2, T3 = "2025-03-01T10:00:00.000Z", "2025-06-01T10:00:00.000Z", "2025-09-01T10:00:00.000Z"
MS1, MS2, MS3 = 1740823200000, 1748772000000, 1756720800000
DAY = 24 * 3600 * 1000


def _call(parts: list[tuple[str, str]], when: str = T1, msg_id: str = "call") -> dict[str, Any]:
    body = "".join(
        f'<part identity="{m}"><name>{m}</name><displayName>{n}</displayName><duration>60</duration></part>'
        for m, n in parts
    )
    return {
        "id": msg_id,
        "messagetype": "Event/Call",
        "from": f"https://h/v1/users/ME/contacts/{EVE}",
        "imdisplayname": "",
        "composetime": when,
        "content": f'<ended/><partlist type="ended" alt="" count="{len(parts)}">{body}</partlist>',
    }


def _said(mri: str, name: str, when: str = T1, msg_id: str = "said") -> dict[str, Any]:
    return {
        "id": msg_id,
        "messagetype": "RichText/Html",
        "from": f"https://h/v1/users/ME/contacts/{mri}",
        "imdisplayname": name,
        "composetime": when,
        "content": "<p>hi</p>",
    }


def _added(when: str = T1, msg_id: str = "added") -> dict[str, Any]:
    return {
        "id": msg_id,
        "messagetype": "ThreadActivity/AddMember",
        "from": "19:x@thread.v2",
        "composetime": when,
        "content": f"<addmember><eventtime>1</eventtime><initiator>{ANN}</initiator>"
        f"<friendlyname>Ann Lee</friendlyname><target>{BOB}</target><friendlyname>Bob Roe</friendlyname>"
        f"<target>{GONE}</target></addmember>",
    }


def _joined(when: str = T1, msg_id: str = "joined") -> dict[str, Any]:
    members = [{"id": CY, "friendlyname": "Cy Ray"}, {"id": DEE}]
    return {
        "id": msg_id,
        "messagetype": "ThreadActivity/MemberJoined",
        "composetime": when,
        "content": json.dumps({"members": members}),
    }


def _rows(settings: Settings) -> dict[str, tuple[str, int, str, int]]:
    with closing(sqlite3.connect(settings.config_dir / "names.db")) as db:
        return {
            m: (n, r, s, t)
            for m, n, r, s, t in db.execute("SELECT mri, name, rank, source, seen_at FROM names")
        }


# --- the parser ---


def test_a_call_record_names_its_participants_at_the_call_s_time() -> None:
    """038-AC1: empty names and guid-only names are skipped; the organizer's empty imdisplayname too."""
    call = _call([(ANN, "Ann Lee"), (BOB, ""), (CY, CY.split(":")[-1]), (DEE, "Dee &amp; Co")])
    assert message_names(call) == [
        (ANN, "Ann Lee", ARCHIVED, "call", MS1),
        (DEE, "Dee & Co", ARCHIVED, "call", MS1),
    ]


def test_an_escaped_call_record_is_read_the_same() -> None:
    call = _call([(ANN, "Ann Lee"), (DEE, "Dee &amp; Co")])
    call["content"] = html.escape(call["content"])
    assert [e[:2] for e in message_names(call)] == [(ANN, "Ann Lee"), (DEE, "Dee & Co")]


def test_an_unclosed_part_never_takes_the_next_one_s_name() -> None:
    call = _call([(BOB, "Bob Roe")])
    call["content"] = call["content"].replace(
        "<partlist", f'<partlist><part identity="{ANN}"><name>x</name>', 1
    )
    assert [e[:2] for e in message_names(call)] == [(BOB, "Bob Roe")]


def test_a_long_unclosed_call_record_is_read_in_bounded_time() -> None:
    import time

    body = f'<part identity="{ANN}"><name>{"x" * 50}</name>' * 5000 + "<displayName>Ann</displayName>"
    started = time.perf_counter()
    assert message_names(_call([]) | {"content": f"<partlist>{body}"}) == []
    assert time.perf_counter() - started < 1.0


def test_a_started_call_without_names_names_nobody() -> None:
    call = _call([])
    call["content"] = (
        f'<partlist type="started" alt=""><part identity="{ANN}"><name>{ANN}</name></part></partlist>'
    )
    assert message_names(call) == []


def test_member_events_name_whom_their_friendlyname_follows() -> None:
    """038-AC8: XML (AddMember) and JSON (MemberJoined) shapes, through the existing activity parser."""
    assert message_names(_added()) == [
        (ANN, "Ann Lee", ARCHIVED, "event", MS1),
        (BOB, "Bob Roe", ARCHIVED, "event", MS1),
    ]
    assert message_names(_joined()) == [(CY, "Cy Ray", ARCHIVED, "event", MS1)]


def test_a_sender_s_imdisplayname_is_declared() -> None:
    assert message_names(_said(ANN, "Annie")) == [(ANN, "Annie", DECLARED, "declared", MS1)]
    late = _said(ANN, "Annie") | {"composetime": "", "originalarrivaltime": T2}
    assert message_names(late)[0][4] == MS2


@pytest.mark.parametrize(
    "message",
    [
        {},
        {"messagetype": "RichText/Html", "from": ANN, "content": "x"},
        {"messagetype": "Event/Call", "content": "<partlist"},
        {"messagetype": "ThreadActivity/MemberJoined", "content": "not json"},
        _said(ANN, ANN.split(":")[-1]),
    ],
)
def test_a_message_naming_nobody_gives_nothing(message: dict[str, Any]) -> None:
    assert message_names(message) == []


# --- 038-AC1 ---


async def test_a_call_record_names_whom_the_profile_lookup_does_not(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """038-AC1: the archive's call name is the label of a person the lookup answers nothing for."""
    Directory(settings).note_entries(message_names(_call([(GONE, "Gail Gone")])))
    directory = Directory(settings)
    directory.set_token("sk", bearer="b")
    monkeypatch.setattr(
        directory_mod.httpx, "AsyncClient", lambda *a, **k: _AsyncClientPost(lambda url: _Resp({"value": []}))
    )
    await directory.resolve([GONE], refresh=True)
    assert (directory.known_name(GONE), directory.name_for(GONE)) == ("Gail Gone", "Gail Gone")
    assert await directory.display(GONE) == "Gail Gone"


# --- ranking through note_entries ---


def test_an_archived_name_never_replaces_a_live_one_nor_a_declared_one_an_archived(
    settings: Settings,
) -> None:
    """038-AC2: whatever the order the archive is read in."""
    directory = Directory(settings)
    directory.note_name(ANN, "Ann Live")
    directory.note_entries(message_names(_call([(ANN, "Ann Call"), (BOB, "Bob Call")], when=T1)))
    directory.note_entries(message_names(_said(BOB, "Bobby", when=T2)))
    assert {m: r[:2] for m, r in _rows(settings).items()} == {
        ANN: ("Ann Live", LIVE),
        BOB: ("Bob Call", ARCHIVED),
    }
    fresh = Directory(settings)
    assert (fresh.known_name(ANN), fresh.known_name(BOB)) == ("Ann Live", "Bob Call")


@pytest.mark.parametrize("unit", [1, DAY])  # within one write window, and days apart
@pytest.mark.parametrize("order", [(1, 3, 2), (2, 3, 1), (3, 1, 2), (2, 1, 3)])
def test_within_a_rank_the_newest_sighting_wins_whatever_the_order(
    settings: Settings, order: tuple[int, ...], unit: int
) -> None:
    """038-AC2: Ann at 1 and 3, Bea at 2: Ann, however they are read, one by one or in one batch."""
    names = {1: "Ann", 2: "Bea", 3: "Ann"}
    entries = [(ANN, names[t], ARCHIVED, "call", 1000 * t * unit) for t in order]
    for entry in entries:  # one process each: the store alone decides
        Directory(settings).note_entries([entry])
    assert _rows(settings)[ANN][0] == "Ann"
    with closing(sqlite3.connect(settings.config_dir / "names.db")) as db:
        db.execute("DELETE FROM names")
        db.commit()
    one = Directory(settings)
    for entry in entries:
        one.note_entries([entry])
    assert (_rows(settings)[ANN][0], one.known_name(ANN)) == ("Ann", "Ann")
    with closing(sqlite3.connect(settings.config_dir / "names.db")) as db:
        db.execute("DELETE FROM names")
        db.commit()
    batch = Directory(settings)
    batch.note_entries(entries)
    assert (_rows(settings)[ANN][0], batch.known_name(ANN)) == ("Ann", "Ann")


def test_a_store_read_between_sightings_keeps_the_newest(settings: Settings) -> None:
    """A declared name is read back from the store per label: that read must not forget a newer sighting."""
    directory = Directory(settings)
    directory.note_entries([(ANN, "Ann", DECLARED, "declared", 1000)])
    directory.note_entries([(ANN, "Ann", DECLARED, "declared", 3000)])  # not written: same name, same day
    assert directory.known_name(ANN) == ""  # declared: re-read from the store
    directory.note_entries([(ANN, "Bea", DECLARED, "declared", 2000)])
    assert (_rows(settings)[ANN][0], directory.name_for(ANN)) == ("Ann", "Ann")


def test_a_name_seen_again_is_rewritten_once_a_day_at_most(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    puts = _counted_puts(monkeypatch)
    directory = Directory(settings)
    for t in (0, 1000, DAY, DAY + 1, DAY + 2):
        directory.note_entries([(ANN, "Ann Lee", LIVE, "profile", t)])
    assert puts == [1, 1]
    assert _rows(settings)[ANN][3] == DAY + 1


def test_an_older_record_read_last_never_wins(settings: Settings) -> None:
    """Backfill reads newest first: the newest name of a rank stays, here and in the store."""
    directory = Directory(settings)
    directory.note_entries(message_names(_call([(ANN, "Ann New")], when=T2)))
    directory.note_entries(message_names(_call([(ANN, "Ann Old")], when=T1)))
    assert _rows(settings)[ANN] == ("Ann New", ARCHIVED, "call", MS2)
    assert directory.known_name(ANN) == "Ann New"


def test_an_older_record_never_wins_in_memory_while_the_store_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clock(monkeypatch)
    directory = Directory(settings)
    assert directory._store is not None
    directory._store._db.execute("PRAGMA busy_timeout = 50")
    holder = _hold(settings)
    try:
        directory.note_entries(message_names(_call([(ANN, "Ann New")], when=T2)))
        directory.note_entries(message_names(_call([(ANN, "Ann Old")], when=T1)))
        assert directory.known_name(ANN) == "Ann New"
    finally:
        holder.close()


def test_entries_noted_while_the_store_fails_are_written_by_flush(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write keeps the entries queued; flush waits out the window and lands them."""
    clock = _clock(monkeypatch)
    directory = Directory(settings)
    assert directory._store is not None
    directory._store._db.execute("PRAGMA busy_timeout = 50")
    holder = _hold(settings)
    directory.note_entries(message_names(_call([(ANN, "Ann Lee"), (BOB, "Bob Roe")])))
    assert len(directory._pending) == 2
    waits: list[float] = []

    def wait(seconds: float) -> None:
        waits.append(seconds)
        holder.close()
        clock[0] += seconds

    assert directory.flush(wait=wait)
    assert waits == [directory_mod._STORE_RETRY_AFTER]
    assert {m: r[:2] for m, r in _rows(settings).items()} == {
        ANN: ("Ann Lee", ARCHIVED),
        BOB: ("Bob Roe", ARCHIVED),
    }


# --- 038-AC8: the archiver records names as it stores messages ---


def _counted_puts(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    puts: list[int] = []
    real = NameStore.put

    def put(self: NameStore, entries: Any) -> Any:
        entries = list(entries)
        puts.append(len(entries))
        return real(self, entries)

    monkeypatch.setattr(NameStore, "put", put)
    return puts


def test_backfill_and_topup_record_the_names_of_each_page_in_one_write(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """038-AC8: call records, member events and senders; one names.db write per stored page."""
    pages = [
        [_said(DEE, "Dee Dee", T2, "m4"), _call([(ANN, "Ann Lee"), (EVE, "Eve Ito")], T2, "m3")],
        [_added(T1, "m2"), _joined(T1, "m1")],
    ]
    monkeypatch.setattr(AR, "iter_history_pages", lambda *a, **k: iter(pages))
    monkeypatch.setattr(AR, "_INTER_PAGE_DELAY", 0)
    data = tmp_path / "data"
    index, store, directory = Index(data), ChatStore(data, C1), Directory(settings)
    puts = _counted_puts(monkeypatch)
    try:
        AR._backfill(settings, "sk", C1, store, index, "label", directory)
        assert puts == [3, 2]  # one per page; Ann named again the same: not written again
        AR._topup(settings, "sk", C1, store, "label", directory)
        assert len(puts) == 2  # the same names again: nothing to write
        newer = [[_call([(FAY, "Fay Ito")], T3, "m5")]]
        monkeypatch.setattr(AR, "iter_history_pages", lambda *a, **k: iter(newer))
        AR._topup(settings, "sk", C1, store, "label", directory)
        assert puts == [3, 2, 1]
    finally:
        store.close()
        index.close()
    assert _rows(settings) == {
        ANN: ("Ann Lee", ARCHIVED, "call", MS2),
        EVE: ("Eve Ito", ARCHIVED, "call", MS2),
        DEE: ("Dee Dee", DECLARED, "declared", MS2),
        BOB: ("Bob Roe", ARCHIVED, "event", MS1),
        CY: ("Cy Ray", ARCHIVED, "event", MS1),
        FAY: ("Fay Ito", ARCHIVED, "call", MS3),
    }


async def test_the_live_archive_records_the_names_of_what_it_stores(
    settings: Settings, tmp_path: Path
) -> None:
    """038-AC8: from the message it is storing, no second pass; a stale redelivery records nothing new."""
    live = AL.LiveArchive(settings, tmp_path / "data", AR.StaticToken("sk"))
    try:
        assert live._apply("NewMessage", C1, _call([(ANN, "Ann Lee")], msg_id="m1")) == "new"
        assert live._apply("NewMessage", C1, _added(msg_id="m2")) == "new"
        assert live._apply("NewMessage", C1, _said(DEE, "Dee Dee", msg_id="m3")) == "new"
        stale = _said(DEE, "Dee Changed", msg_id="m3")  # redelivered, same version
        assert live._apply("NewMessage", C1, stale) == "stale"
    finally:
        live.close()
    assert {m: r[:3] for m, r in _rows(settings).items()} == {
        ANN: ("Ann Lee", ARCHIVED, "call"),
        BOB: ("Bob Roe", ARCHIVED, "event"),
        DEE: ("Dee Dee", DECLARED, "declared"),
    }


# --- 038-AC5: the sweep ---


def _archive(data: Path) -> None:
    """C1 calls and senders, C2 member events, C3 a corrupt messages.db, C4 indexed but never stored."""
    index = Index(data)
    index.upsert_chat(C1, participants=[{"mri": GONE, "name": "Gail Roster"}])
    for chat in (C2, C3, C4):
        index.upsert_chat(chat)
    index.close()
    calls = ChatStore(data, C1)  # the newer call first: the read order must not matter
    calls.insert_page(
        [
            _call([(DEE, "Dee New")], T2, "c2"),
            _call([(DEE, "Dee Old"), (ANN, "Ann Call")], T1, "c1"),
            _said(ANN, "Annie", T2, "s1"),  # newer, but declared
            _said(EVE, "Eve Ito", T1, "s2"),
        ]
    )
    with calls._db:
        calls._db.execute("INSERT INTO messages (id, composetime, raw) VALUES ('bad', ?, '{not json')", (T1,))
    calls.close()
    events = ChatStore(data, C2)
    events.insert_page([_added(T2, "e1"), _joined(T2, "e2")])
    events.close()
    ChatStore(data, C3).close()
    (data / C3 / "messages.db").write_bytes(b"not a database at all" * 100)


@pytest.fixture
def sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    # setup_logging caches cli.log on first use, past the reach of a later capture_logs.
    monkeypatch.setattr(cli, "setup_logging", lambda level: None)
    monkeypatch.setattr(cli, "log", structlog.get_logger())  # an earlier test may have cached it already


def _tree(root: Path) -> dict[str, str]:
    """Every file and folder under `root`: a file's sha256, "dir" for a folder."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
        for p in root.rglob("*")
    }


@pytest.mark.usefixtures("sweep")
def test_the_sweep_names_everyone_the_archive_names_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """038-AC5: every source, ranked; the archive byte-identical; unreadable chats skipped."""
    _archive(tmp_path / "fixture")
    data = tmp_path / "data"
    shutil.copytree(tmp_path / "fixture", data)
    before = _tree(data)
    assert not [k for k in before if k.endswith(("-wal", "-shm"))]
    settings = Settings(tenant_id="t")
    Directory(settings).note_name(CY, "Cy Live")
    with structlog.testing.capture_logs() as logs:
        assert cli.main(["names", "--sweep", "--data-dir", str(data)]) == 0
    skipped = [Path(e["chat_dir"]).name for e in logs if e["event"] == "names_sweep_chat_skipped"]
    assert sorted(skipped) == [C3, C4]
    assert _tree(data) == before  # no -wal or -shm either: a plain `mode=ro` open would leave both
    assert {m: r[:3] for m, r in _rows(settings).items()} == {
        GONE: ("Gail Roster", ARCHIVED, "index-roster"),
        DEE: ("Dee New", ARCHIVED, "call"),
        ANN: ("Ann Lee", ARCHIVED, "event"),  # a newer archived name than the call's
        EVE: ("Eve Ito", DECLARED, "declared"),
        BOB: ("Bob Roe", ARCHIVED, "event"),
        CY: ("Cy Live", LIVE, "profile"),
    }
    assert capsys.readouterr().out == "2/4 chats read, 6 people named\n"


@pytest.mark.usefixtures("sweep")
def test_a_sweep_without_an_index_fails_and_creates_nothing(tmp_path: Path) -> None:
    with structlog.testing.capture_logs() as logs:
        assert cli.main(["names", "--sweep", "--data-dir", str(tmp_path / "none")]) == 1
    assert [e["event"] for e in logs] == ["names_sweep_no_archive"]
    assert not (tmp_path / "none").exists()


def test_a_chat_is_read_once_page_by_page(tmp_path: Path) -> None:
    store = ChatStore(tmp_path, C1)
    store.insert_page([_said(ANN, f"Ann {n}", T1, f"m{n}") for n in range(5)])
    store.close()
    pages = list(name_fields(tmp_path / C1, page=2))
    assert [len(p) for p in pages] == [2, 2, 1]
    assert sorted(m["imdisplayname"] for p in pages for m in p) == [f"Ann {n}" for n in range(5)]
    assert "content" not in pages[0][0]  # a sender's message body is never read


@pytest.mark.usefixtures("sweep")
def test_a_chat_a_writer_holds_open_is_read_with_its_log(tmp_path: Path) -> None:
    """038-AC5: rows only in the -wal are named; the file set stays, the database and its log untouched."""
    _archive(tmp_path / "data")
    db = tmp_path / "data" / C1 / "messages.db"
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")  # the new row stays in the log
        with writer:
            row = json.dumps(_call([(FAY, "Fay Wal")], T2, "w1"))
            writer.execute("INSERT INTO messages (id, composetime, raw) VALUES ('w1', ?, ?)", (T2, row))
        before = _tree(tmp_path / "data")
        assert f"{C1}/messages.db-wal" in before
        assert cli.main(["names", "--sweep", "--data-dir", str(tmp_path / "data")]) == 0
        after = _tree(tmp_path / "data")
    finally:
        writer.close()
    shm = f"{C1}/messages.db-shm"  # readers take their read marks in it
    assert set(after) == set(before)
    assert {k: v for k, v in after.items() if k != shm} == {k: v for k, v in before.items() if k != shm}
    assert _rows(Settings(tenant_id="t"))[FAY][:3] == ("Fay Wal", ARCHIVED, "call")


@pytest.mark.usefixtures("sweep")
def test_a_chat_failing_mid_read_is_skipped_and_the_sweep_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page torn by a writer's checkpoint under an immutable read: that chat is logged and skipped."""
    _archive(tmp_path / "data")
    real = AS.name_fields

    def torn(chat_dir: Path, page: int = 500) -> Any:
        if chat_dir.name == C1:
            yield [_said(EVE, "Eve Ito")]
            raise sqlite3.DatabaseError("database disk image is malformed")
        yield from real(chat_dir, page)

    monkeypatch.setattr(AS, "name_fields", torn)
    with structlog.testing.capture_logs() as logs:
        assert cli.main(["names", "--sweep", "--data-dir", str(tmp_path / "data")]) == 0
    skipped = sorted(Path(e["chat_dir"]).name for e in logs if e["event"] == "names_sweep_chat_skipped")
    assert skipped == sorted([C1, C3, C4])
    assert _rows(Settings(tenant_id="t"))[BOB][:2] == ("Bob Roe", ARCHIVED)  # C2, read after C1


@pytest.mark.usefixtures("sweep")
def test_each_sweep_reads_the_rosters_again(tmp_path: Path) -> None:
    """The one-off roster import of the long-running processes does not stop a sweep reading rosters."""
    _archive(tmp_path / "data")
    assert cli.main(["names", "--sweep", "--data-dir", str(tmp_path / "data")]) == 0
    index = Index(tmp_path / "data")
    index.upsert_chat(C2, participants=[{"mri": FAY, "name": "Fay Roster"}])
    index.close()
    assert cli.main(["names", "--sweep", "--data-dir", str(tmp_path / "data")]) == 0
    assert _rows(Settings(tenant_id="t"))[FAY][:3] == ("Fay Roster", ARCHIVED, "index-roster")


@pytest.mark.usefixtures("sweep")
def test_a_sweep_whose_names_db_never_answers_fails_without_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep tries names.db a few times, then exits 1 saying how many names stayed unwritten."""
    _archive(tmp_path / "data")
    config = Settings(tenant_id="t").config_dir
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("not a folder")
    clock = _clock(monkeypatch)
    waits: list[float] = []
    monkeypatch.setattr(
        directory_mod.time, "sleep", lambda s: waits.append(s) or clock.__setitem__(0, clock[0] + s)
    )
    with structlog.testing.capture_logs() as logs:
        assert cli.main(["names", "--sweep", "--data-dir", str(tmp_path / "data")]) == 1
    assert waits == [directory_mod._STORE_RETRY_AFTER] * 2
    assert [e["count"] for e in logs if e["event"] == "names_unwritten"] == [6]


@pytest.mark.usefixtures("sweep")
def test_a_sweep_saves_what_a_busy_names_db_held_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Names queued while another process held names.db are written before the sweep ends."""
    _archive(tmp_path / "data")
    settings = Settings(tenant_id="t")
    Directory(settings)
    monkeypatch.setattr(NS, "_BUSY_TIMEOUT", 0.05)
    clock = _clock(monkeypatch)
    holder = _hold(settings)

    def sleep(seconds: float) -> None:
        holder.close()
        clock[0] += seconds

    monkeypatch.setattr(directory_mod.time, "sleep", sleep)
    try:
        assert cli.main(["names", "--sweep", "--data-dir", str(tmp_path / "data")]) == 0
    finally:
        holder.close()
    assert len(_rows(settings)) == 6


def test_the_roster_import_creates_nothing_in_the_archive(settings: Settings, tmp_path: Path) -> None:
    """038-AC4 read path: an index.db without a -wal gets no sidecar."""
    _archive(tmp_path / "data")
    before = _tree(tmp_path / "data")
    Directory(settings).import_rosters(tmp_path / "data")
    assert _tree(tmp_path / "data") == before
    assert _rows(settings)[GONE][:3] == ("Gail Roster", ARCHIVED, "index-roster")


# --- declared names carry their message's time ---


def _at_minute(n: int) -> str:
    return f"2026-01-01T10:{n:02d}:00.000Z"


async def test_an_old_message_shown_again_never_beats_a_newer_declared_name(settings: Settings) -> None:
    """038-AC2: the sighting is the message's time, not the render's."""
    directory = Directory(settings)
    directory.note_entries(message_names(_said(ANN, "Ann 2026", "2026-02-01T10:00:00.000Z", "new")))
    assert (
        await W.sender_of(_said(ANN, "Ann 2024", "2024-02-01T10:00:00.000Z", "old"), directory) == "Ann 2024"
    )
    assert await W.sender_of(_said(ANN, "Ann 2024", "2024-02-01T10:00:00.000Z", "old"), Directory(settings))
    assert _rows(settings)[ANN][:2] == ("Ann 2026", DECLARED)
    with closing(sqlite3.connect(settings.config_dir / "names.db")) as db:
        db.execute("DELETE FROM names")
        db.commit()
    other = Directory(settings)  # the other order
    await W.sender_of(_said(ANN, "Ann 2024", "2024-02-01T10:00:00.000Z", "old"), other)
    other.note_entries(message_names(_said(ANN, "Ann 2026", "2026-02-01T10:00:00.000Z", "new")))
    assert _rows(settings)[ANN][:2] == ("Ann 2026", DECLARED)


async def test_rendering_a_page_alternating_two_declared_names_writes_once(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Newest first, then again oldest first in another process: one write each at most."""
    page = [_said(ANN, ("Ann", "Annie")[n % 2], _at_minute(n), f"m{n}") for n in range(40)]
    puts = _counted_puts(monkeypatch)
    directory = Directory(settings)
    for message in reversed(page):
        await W.sender_of(message, directory)
    other = Directory(settings)
    for message in page:
        await W.sender_of(message, directory)
        await W.sender_of(message, other)
    assert sum(puts) <= 2
    assert _rows(settings)[ANN][:2] == ("Annie", DECLARED)


# --- flushing at the end of a run ---


@pytest.mark.parametrize(("live_pass", "tries"), [(False, 3), (True, 1)])
async def test_an_archive_pass_flushes_its_names_without_holding_a_live_pass(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_pass: bool, tries: int
) -> None:
    """A live pass gives names.db one last try, no wait: new live messages are buffered meanwhile."""
    _wire(monkeypatch, _FakeApi({C1: [_said(ANN, "Ann Lee", T1, "m1")]}), [C1])
    calls: list[int] = []
    real = Directory.flush

    def flush(self: Directory, tries: int = 3, wait: Any = None) -> bool:
        calls.append(tries)
        return real(self, tries, wait)

    monkeypatch.setattr(Directory, "flush", flush)
    await _run(settings, tmp_path / "data", live_pass=live_pass)
    assert calls == [tries]
    assert _rows(settings)[ANN][:2] == ("Ann Lee", DECLARED)


def test_a_last_flush_tries_even_while_paused(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _clock(monkeypatch)  # the pause never runs out on its own here
    directory = Directory(settings)
    assert directory._store is not None
    directory._store._db.execute("PRAGMA busy_timeout = 50")
    holder = _hold(settings)
    directory.note_entries(message_names(_call([(ANN, "Ann Lee")])))
    holder.close()
    assert directory._pending and directory._paused()
    assert directory.flush(tries=1)
    assert _rows(settings)[ANN][:2] == ("Ann Lee", ARCHIVED)


async def test_a_name_the_live_archive_could_not_write_lands_on_close(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clock(monkeypatch)
    monkeypatch.setattr(NS, "_BUSY_TIMEOUT", 0.05)
    live = AL.LiveArchive(settings, tmp_path / "data", AR.StaticToken("sk"))
    holder = _hold(settings)
    try:
        assert live._apply("NewMessage", C1, _call([(ANN, "Ann Lee")], msg_id="m1")) == "new"
    finally:
        holder.close()
    assert _rows(settings) == {}
    live.close()
    assert _rows(settings)[ANN][:2] == ("Ann Lee", ARCHIVED)


@pytest.mark.usefixtures("sweep")
def test_a_roster_is_as_recent_as_the_conversation_stored_with_it(tmp_path: Path) -> None:
    """Its conversation's version, not `last_fetch_at` (moved by passes that keep the roster): newer than
    a 2023 call, older than a call after that version even once the chat is touched after the call."""
    data = tmp_path / "data"
    index = Index(data)
    people = [{"mri": GONE, "name": "Gail Roster"}, {"mri": DEE, "name": "Dee Roster"}]
    index.upsert_chat(C1, participants=people, raw={"id": C1, "version": MS2})
    index.touch(C1, "2026-01-01T00:00:00.000Z")  # a later pass found the chat unchanged
    index.upsert_chat(C3, participants=[{"mri": EVE, "name": "Eve Roster"}], raw={"id": C3, "version": "x"})
    index.upsert_chat(C2)
    index.close()
    ChatStore(data, C1).close()
    calls = ChatStore(data, C2)
    calls.insert_page(
        [_call([(GONE, "Gail Call")], "2023-05-01T10:00:00.000Z", "c1"), _call([(DEE, "Dee Call")], T3, "c2")]
    )
    calls.close()
    assert cli.main(["names", "--sweep", "--data-dir", str(data)]) == 0
    rows = _rows(Settings(tenant_id="t"))
    assert (rows[GONE][:3], rows[DEE][:3]) == (
        ("Gail Roster", ARCHIVED, "index-roster"),
        ("Dee Call", ARCHIVED, "call"),
    )
    assert (rows[GONE][3], rows[EVE][3]) == (MS2, 0)  # an unreadable version dates nothing


async def test_a_typing_event_without_a_time_is_current(settings: Settings) -> None:
    """Typing is happening now; any other message without a time is dated nothing, never now."""
    directory = Directory(settings)
    directory.note_entries([(ANN, "Ann Old", DECLARED, "declared", MS1)])
    timeless = {"from": f"https://h/v1/users/ME/contacts/{ANN}", "imdisplayname": "Ann Timeless"}
    await W.sender_of(timeless | {"messagetype": "RichText/Html"}, directory)
    assert _rows(settings)[ANN][:2] == ("Ann Old", DECLARED)
    await W.sender_of(timeless | {"messagetype": "Control/Typing", "imdisplayname": "Ann Typing"}, directory)
    assert _rows(settings)[ANN][:2] == ("Ann Typing", DECLARED)
