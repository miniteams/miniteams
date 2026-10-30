"""Sender names: stamped from the cached ID token, shown from the directory before the declared name."""

import argparse
import asyncio
import base64
import functools
import io
import json
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from test_auth import _cache_doc, _CacheBackedApp
from test_mcp import OTHER as MCP_OTHER
from test_mcp import PRIVATE as MCP_PRIVATE
from test_mcp import _api, _call, _gone, _scheduling, _server, data  # noqa: F401
from test_mcp import _msg as _mcp_msg
from test_web_chat import ANN, ME, T, _archive, _at, _m, _search, quiet_directory  # noqa: F401

from miniteams import auth as A
from miniteams import chat_verbs as CV
from miniteams import cli, messages, send
from miniteams import directory as D
from miniteams import mcp as M
from miniteams import watch as WA
from miniteams import web as W
from miniteams.archive_store import Index
from miniteams.config import Settings
from miniteams.directory import Directory, sender_name
from miniteams.names_store import DECLARED, LIVE, Row

ID = "uid.utid"  # the cached account, see test_auth._cache_doc
BOGUS = "Zorglub Testeur"  # free text a sending client may declare
CONTACT = f"https://h/v1/users/ME/contacts/{ANN}"


def _jwt(claims: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


def _signed_in(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, *id_tokens: tuple[str, str, dict[str, Any]]
) -> None:
    """A token cache on disk: my account plus the given (home account, client id, claims) ID tokens.

    Its msal app answers a silent call from the access-token cache: no `id_token_claims`, as msal does."""
    doc = json.loads(_cache_doc("rt"))
    doc["IdToken"] = {
        f"{home}-login.windows.net-idtoken-{client}-tenant-": {
            "credential_type": "IdToken",
            "secret": _jwt(claims),
            "home_account_id": home,
            "environment": "login.windows.net",
            "realm": "tenant",
            "client_id": client,
        }
        for home, client, claims in id_tokens
    }
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    (settings.config_dir / "msal_cache.json").write_text(json.dumps(doc))
    monkeypatch.setattr(A, "_app", lambda cid, s, cache: _CacheBackedApp(cache))


# --- send side: the name comes from the cached ID token ---


def test_my_name_comes_from_my_cached_id_token(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _signed_in(settings, monkeypatch, (ID, settings.client_id, {"name": "Ann Lee"}))
    source = A.TokenSource(settings)
    assert "id_token_claims" not in source.refresh()  # the cache hit this guards against
    assert source.display_name() == "Ann Lee"


@pytest.mark.parametrize(
    "id_tokens",
    [
        [],  # signed in, no ID token cached
        [("other.utid", "1fec8e78", {"name": "Bob Roe"})],  # someone else's account
        [(ID, "1fec8e78", {"preferred_username": "ann@example.test"})],  # mine, no name claim
        [(ID, "1fec8e78", {"name": ""})],
    ],
)
def test_no_name_when_no_id_token_of_mine_carries_one(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, id_tokens: list[tuple[str, str, dict[str, Any]]]
) -> None:
    _signed_in(settings, monkeypatch, *id_tokens)
    assert A.TokenSource(settings).display_name() == ""


def test_no_account_no_name(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(A, "_app", lambda cid, s, cache: _CacheBackedApp(cache))
    assert A.TokenSource(settings).display_name() == ""


def test_an_mcp_send_and_schedule_stamp_my_name(
    settings: Settings,
    data: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, drafts, calls = _scheduling(settings, data, monkeypatch)
    _signed_in(settings, monkeypatch, (ID, settings.client_id, {"name": "Ann Lee"}))
    real = A.TokenSource(settings)  # over the cache above; the drafts store keeps its fake ic3 token
    monkeypatch.setattr(server.session.source, "refresh", real.refresh)
    monkeypatch.setattr(server.session.source, "display_name", real.display_name)
    assert not _call(server, "send_message", thread_id="48:notes", text="now")["error"]
    out = _call(server, "send_message", thread_id="48:notes", text="later", send_at=_in_a_day())
    assert not out["error"]
    assert calls[0][3] == "Ann Lee"
    (row,) = drafts.rows.values()
    assert row["imdisplayname"] == "Ann Lee"


def _in_a_day() -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_cli_send_stamps_my_name(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _signed_in(settings, monkeypatch, (ID, settings.client_id, {"name": "Ann Lee"}))
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: ({"access_token": "a"}, "sk"))  # no claims
    stamped: list[str] = []
    monkeypatch.setattr(send, "send_message", lambda s, tok, thread, text, name, **k: stamped.append(name))
    args = {"text": "hi", "file": None, "thread": "48:notes", "html": True, "image": None, "attach": None}
    assert cli.cmd_send(settings, argparse.Namespace(**args)) == 0
    assert stamped == ["Ann Lee"]


def test_web_starts_with_my_name(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _signed_in(settings, monkeypatch, (ID, settings.client_id, {"name": "Ann Lee"}))
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: ({"access_token": "a"}, "sk"))
    got: dict[str, Any] = {}

    async def fake_run(*a: Any, **kw: Any) -> None:
        got.update(kw)

    monkeypatch.setattr(W, "run", fake_run)
    args = argparse.Namespace(
        limit=None, bind=None, reactions=False, opener="", open_scheme="", browser="", data_dir="d"
    )
    assert cli.cmd_web(settings, args) == 0
    assert got["me_name"] == "Ann Lee"


@pytest.mark.parametrize(
    ("cached", "given", "stamped"),
    [
        (None, "Ann Lee", "Ann Lee"),
        ("Old Name", "Ann Lee", "Ann Lee"),
        ("Ann Lee", "", "Ann Lee"),
        (None, "", ""),  # nobody named me: stamp nothing rather than my bare id
        ("marker", "", ""),
    ],
)
async def test_the_web_stamps_the_name_it_starts_with(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cached: str | None,
    given: str,
    stamped: str,
) -> None:
    if cached is not None:
        settings.cache_dir.mkdir(parents=True, exist_ok=True)
        (settings.cache_dir / "names-v2.json").write_text(
            json.dumps({ME: ME.split(":")[-1] if cached == "marker" else cached})
        )
    seen: dict[str, str] = {}

    async def stop(board: W.Board, *_: Any) -> None:
        seen.update(board=board.me_name, chat=board.chat.me_name)
        raise RuntimeError("stop")

    async def idle(*_: Any, **__: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(W, "serve_board", stop)
    monkeypatch.setattr(W, "fill_from_listing", idle)
    monkeypatch.setattr(W, "run_forever", idle)
    monkeypatch.setattr(W.EmojiCache, "refresh", idle)
    with pytest.raises(RuntimeError, match="stop"):
        await asyncio.wait_for(
            W.run(settings, "sk", "", None, None, me=ME, me_name=given, data_dir=tmp_path), 5
        )
    assert seen == {"board": stamped, "chat": stamped}


# --- display side: the directory's name for `from`, else this message's declared name ---

TAIL = ANN.split(":")[-1]  # an unresolved MRI's label


def _named(directory: Directory, name: str) -> None:
    """`marker`: a lookup named nobody; else a live name."""
    if name == "marker":
        directory._note_missed([ANN])
    else:
        directory.note_name(ANN, name)


REAL = "Real Name"  # what a second message from the same unknown sender declares


def test_sender_name_rule(directory: Directory) -> None:
    directory.note_name(ANN, "Ann Lee")
    directory._note_missed(["8:orgid:gone"])  # a lookup named nobody
    directory.note_declared("8:orgid:new", BOGUS)  # a label, not a name
    known = directory.known_name
    assert sender_name(ANN, BOGUS, known) == "Ann Lee"
    assert sender_name(ANN, "", known) == "Ann Lee"
    assert sender_name(CONTACT, None, known) == "Ann Lee"  # a contact URL names the same MRI
    assert sender_name("8:orgid:gone", REAL, known) == REAL  # never the marker
    assert sender_name("8:orgid:gone", "", known) == ""
    assert sender_name("8:orgid:new", REAL, known) == REAL  # this message's name, not an earlier one
    assert sender_name("8:orgid:new", None, known) == ""
    assert sender_name(ANN, BOGUS, None) == BOGUS  # no directory: an archive read
    assert sender_name("", BOGUS, known) == BOGUS


type Site = Callable[[dict[str, Any], Directory, Path, pytest.MonkeyPatch], Awaitable[str]]


async def _record(message: dict[str, Any], directory: Directory, *_: Any) -> str:
    return str(
        CV.chat_record(message, T, ME, name_for=directory.name_for, known_name=directory.known_name)["name"]
    )


async def _quote(
    message: dict[str, Any], directory: Directory, _: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    monkeypatch.setattr(CV, "reply_quote", lambda message, name: name)
    return str(W.Board({}, directory, me=ME).chat._reply_quote(message))


async def _hit(
    message: dict[str, Any], directory: Directory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    monkeypatch.setattr(CV, "fetch_history", lambda *a: pytest.fail("no API call expected"))
    root = _archive(tmp_path / "d", T, [message])
    (hit,) = (await _search(W.Board({}, directory, me=ME, data_dir=root), T, "m1"))["hits"]
    return str(hit["name"])


async def _row(message: dict[str, Any], directory: Directory, *_: Any) -> str:
    return await W.sender_of(message, directory)


async def _watch_line(message: dict[str, Any], directory: Directory, *_: Any) -> str:
    watcher = WA.Watcher(directory.settings, WA.Watch(), ME, 0)
    watcher.directory = directory
    return str((await watcher.line({"resource": message, "reactor": "", "reaction": None}))["sender"])


async def _jsonl(message: dict[str, Any], directory: Directory, *_: Any) -> str:
    return str((await messages.resource_to_record(message, directory))["sender"])


async def _printed(message: dict[str, Any], directory: Directory, *_: Any) -> str:
    lines: list[str] = []

    async def emit(text: str) -> None:
        lines.append(text)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(messages, "emit", emit)
        await messages.print_resource(message, directory)
    (line,) = lines
    return line.split(") ", 1)[1].split(": ", 1)[0]


async def _relayed(
    message: dict[str, Any],
    directory: Directory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    media: bool = False,
) -> str:
    board = W.Board({}, directory, me=ME)
    frames: list[dict[str, Any]] = []
    monkeypatch.setattr(board, "_relay_now", lambda thread, frame: frames.append(frame))
    folder = tmp_path / "media" if media else None  # with a folder the record is built on the archive pool
    if folder is not None:
        folder.mkdir(parents=True)
    board._follows[object()] = (T, folder)  # type: ignore[index]
    link = {"conversationLink": f"https://h/v1/users/ME/conversations/{T}"}
    try:
        await _feed(board, {"type": "EventMessage", "resourceType": "NewMessage", "resource": message | link})
    finally:
        board.close()
    (frame,) = [f for f in frames if "msg" in f]
    return str(frame["msg"]["name"])


# The name each site shows when neither the directory nor the message names the sender.
SITES: dict[str, tuple[Site, str]] = {
    "chat record": (_record, ""),  # the page shows "?"
    "live relay": (_relayed, ""),
    "live relay, media": (functools.partial(_relayed, media=True), ""),
    "reply quote": (_quote, TAIL),
    "search hit": (_hit, ""),
    "list row": (_row, f"name:{CONTACT}"),
    "watch line": (_watch_line, f"name:{CONTACT}"),
    "stream jsonl": (_jsonl, f"name:{CONTACT}"),
    "stream text": (_printed, f"name:{CONTACT}"),
}


@pytest.mark.parametrize("site", list(SITES))
@pytest.mark.parametrize(
    ("cached", "declared", "shown"),
    [
        ("Ann Lee", "", "Ann Lee"),
        ("Ann Lee", BOGUS, "Ann Lee"),
        (None, BOGUS, BOGUS),
        ("marker", BOGUS, BOGUS),
        (None, "", None),
        ("marker", "", None),
    ],
)
async def test_each_site_shows_the_directory_name_first(
    quiet_directory: Directory,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    cached: str | None,
    declared: str,
    shown: str | None,
) -> None:
    if cached is not None:
        _named(quiet_directory, cached)
    show, fallback = SITES[site]
    message = _m("1", _at(1), imdisplayname=declared)
    assert await show(message, quiet_directory, tmp_path, monkeypatch) == (
        fallback if shown is None else shown
    )
    if cached == "Ann Lee":
        assert quiet_directory.known_name(ANN) == "Ann Lee"  # the declared name did not replace it


@pytest.mark.parametrize("site", list(SITES))
@pytest.mark.parametrize("negative", [False, True])
async def test_each_message_of_an_unknown_sender_shows_its_own_name(
    quiet_directory: Directory,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    negative: bool,
) -> None:
    if negative:
        quiet_directory._note_missed([ANN])
    show, _ = SITES[site]
    for n, declared in enumerate([BOGUS, REAL, BOGUS]):
        message = _m("1", _at(1), imdisplayname=declared)
        assert await show(message, quiet_directory, tmp_path / str(n), monkeypatch) == declared
    assert quiet_directory.known_name(ANN) == ""
    assert quiet_directory._store is not None
    row = quiet_directory._store.get([ANN]).get(ANN, Row("", 0, False))
    assert row.rank <= DECLARED and row.looked_up == negative  # a declared label at most, never a known name
    quiet_directory.note_name(ANN, "Ann Lee")  # a profile lookup or a roster names them later
    assert await show(_m("1", _at(1), imdisplayname=BOGUS), quiet_directory, tmp_path / "9", monkeypatch) == (
        "Ann Lee"
    )


# --- the names store: declared names are labels, never known names ---


@pytest.mark.parametrize("mri", [ANN, CONTACT])
@pytest.mark.parametrize("before", [None, "marker", "Ann Lee"])
def test_a_declared_name_never_becomes_a_known_name(
    directory: Directory, mri: str, before: str | None
) -> None:
    """038-AC7: a declared name is stored at the lowest rank and never over a live one."""
    if before is not None:
        _named(directory, before)
    directory.note_declared(mri, BOGUS)
    assert directory.known_name(ANN) == ("Ann Lee" if before == "Ann Lee" else "")
    assert directory._names.get(ANN) == (("Ann Lee", LIVE) if before == "Ann Lee" else (BOGUS, DECLARED))


@pytest.mark.parametrize("negative", [False, True])
def test_a_declared_name_is_a_last_resort_label(directory: Directory, negative: bool) -> None:
    if negative:
        directory._note_missed([ANN])
    assert directory.name_for(ANN) == TAIL
    directory.note_declared(CONTACT, BOGUS)
    directory.note_declared(ANN, "")  # an empty name keeps the last one
    assert directory.name_for(ANN) == BOGUS  # reaction people, typing: some label beats a guid
    directory.note_name(ANN, "Ann Lee")
    assert directory.name_for(ANN) == "Ann Lee"


def test_a_declared_label_serves_every_process(directory: Directory) -> None:
    """038: declared names live in names.db, not in one process's memory."""
    directory.note_declared("8:orgid:p1", "P 1")
    other = Directory(directory.settings)
    assert (other.name_for("8:orgid:p1"), other.known_name("8:orgid:p1")) == ("P 1", "")


async def test_a_declared_name_does_not_spare_a_profile_lookup(
    directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[list[str]] = []

    async def resolve(mris: list[str], *, refresh: bool = False) -> None:
        asked.append(mris)
        directory.note_name(ANN, "Ann Lee")

    directory.set_token("sk", "bearer")
    monkeypatch.setattr(directory, "resolve", resolve)
    directory.note_declared(ANN, BOGUS)
    assert await directory.display(CONTACT) == "Ann Lee"
    assert asked == [[ANN]]


async def test_a_roster_member_is_never_named_by_a_declared_name(
    directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The roster is archived as `participants` and drives mentions: a declared name must not reach it."""
    from test_directory_fetch import _patch, _Resp

    _patch(
        monkeypatch,
        lambda url: _Resp({"members": [{"mri": ANN, "friendlyName": ""}, {"mri": ME, "friendlyName": "Me"}]}),
    )

    async def nothing(mris: list[str], *, refresh: bool = False) -> None:
        return None

    monkeypatch.setattr(directory, "resolve", nothing)
    directory.note_declared(ANN, BOGUS)
    info = await directory.thread(T)
    assert info is not None
    assert {m["mri"]: m["name"] for m in info["members"]} == {ANN: TAIL, ME: "Me"}


def test_a_mention_title_is_never_a_declared_name(directory: Directory) -> None:
    content = '<p><span itemtype="http://schema.skype.com/Mention" itemscope itemid="0">Ann</span> hi</p>'
    props = {"mentions": json.dumps([{"mentionType": "person", "itemid": 0, "mri": ANN}])}
    message = _m("7", _at(7), content, properties=props)

    def html() -> str:
        record = CV.chat_record(message, T, ME, name_for=directory.name_for, known_name=directory.known_name)
        return str(record["html"])

    directory.note_declared(ANN, BOGUS)
    assert "title=" not in html()
    directory._note_missed([ANN])
    assert "title=" not in html()
    directory.note_name(ANN, "Ann Lee")
    assert 'title="Ann Lee"' in html()


@pytest.mark.parametrize("before", [None, "marker"])
async def test_a_reader_known_by_a_declared_name_only_is_still_looked_up(
    directory: Directory, monkeypatch: pytest.MonkeyPatch, before: str | None
) -> None:
    from test_receipts import _live

    from miniteams import receipts as R

    asked: list[str] = []

    async def lookup(thread_id: str, member: str) -> None:
        asked.append(member)

    if before is not None:
        directory._note_missed([ANN])
    directory.note_declared(ANN, BOGUS)
    receipts = R.Receipts(directory, ME, lambda t, f: None, None, None)
    monkeypatch.setattr(receipts, "_name", lookup)
    receipts.shown[T] = {}
    receipts.on_event(T, _live(ANN, "5;1;1"))
    await asyncio.gather(*receipts._tasks)
    assert asked == [ANN]
    await receipts.close()


# --- MCP: the archive and the live API name senders like the pages do ---


@pytest.mark.parametrize("names", [{MCP_OTHER: "Ann Lee"}, {MCP_OTHER: "other"}, {}])
def test_mcp_archive_reads_name_senders_from_the_names_cache(
    settings: Settings,
    data: Path,  # noqa: F811
    names: dict[str, str],
) -> None:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    (settings.cache_dir / "names-v2.json").write_text(json.dumps(names))
    server = _server(settings, data)
    shown = "Ann Lee" if names.get(MCP_OTHER) == "Ann Lee" else "Somebody"  # else the declared name
    read = _call(server, "read_messages", thread_id=MCP_PRIVATE, since="2026-07-01")["messages"]
    assert read and {m["sender"] for m in read} == {shown}
    found = _call(server, "search_messages", query="there", since="2026-07-01")["messages"]
    assert found and {m["sender"] for m in found} == {shown}


def test_mcp_calls_share_one_store_and_see_names_improved_between_them(
    settings: Settings,
    data: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One names.db open per MCP process; a name another process improved shows on the next call."""
    opens: list[Path] = []
    real = D._open_store

    def counted(path: Path) -> Any:
        opens.append(path)
        return real(path)

    monkeypatch.setattr(D, "_open_store", counted)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    (settings.cache_dir / "names-v2.json").write_text(json.dumps({MCP_OTHER: "Ann Old"}))
    server = _server(settings, data)

    def senders() -> set[str]:
        read = _call(server, "read_messages", thread_id=MCP_PRIVATE, since="2026-07-01")["messages"]
        return {m["sender"] for m in read}

    assert senders() == {"Ann Old"}
    Directory(settings).note_name(MCP_OTHER, "Ann Lee")  # another process, a live name
    assert senders() == {"Ann Lee"}
    assert len(opens) == 2  # this test's other Directory, and the server's one


def test_a_failed_store_call_does_not_blank_the_next_mcp_call(
    settings: Settings,
    data: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pause after a failure spans no MCP call: each call gives names.db one fresh try."""
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    (settings.cache_dir / "names-v2.json").write_text(json.dumps({MCP_OTHER: "Ann Lee"}))
    server = _server(settings, data)
    directory = server.archive.directory()
    assert directory is not None and directory._store is not None
    directory._store._db.execute("PRAGMA busy_timeout = 50")
    roster = tmp_path / "rosters"
    index = Index(roster)
    index.upsert_chat("19:r@thread.v2", participants=[{"mri": "8:orgid:dee-test", "name": "Dee Ray"}])
    index.close()

    class _Stop(Exception):
        pass

    def stop(seconds: float) -> None:
        raise _Stop

    holder = sqlite3.connect(settings.config_dir / "names.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(_Stop):
            directory.import_rosters(roster, stop)  # its write fails: the store pauses
    finally:
        holder.close()
    assert directory._paused()
    known = server.archive._known_name()
    assert known is not None and known(MCP_OTHER) == "Ann Lee"


def test_mcp_api_calls_share_the_process_store(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API fallback reuses the server's Directory: no names.db open per call."""
    opens: list[Path] = []
    real = D._open_store

    def counted(path: Path) -> Any:
        opens.append(path)
        return real(path)

    monkeypatch.setattr(D, "_open_store", counted)
    convs = [{"id": MCP_PRIVATE, "lastMessage": {"composetime": "2026-07-03T09:00:00Z"}}]
    _api(monkeypatch, convs, {MCP_PRIVATE: [_mcp_msg("a", "2026-07-02T09:00:00Z")]})
    server = _gone(settings, tmp_path, monkeypatch)
    for _ in range(3):
        assert _call(server, "read_messages", thread_id=MCP_PRIVATE, since="2026-07-01")["source"] == "api"
        assert _call(server, "find_chats", query="", since="2026-07-01")["source"] == "api"
    assert len(opens) == 1
    assert server.api().directory is server.archive.directory()


def test_each_mcp_api_call_reads_rosters_afresh(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared Directory must not pin a roster for the MCP process's life: members join between sends."""
    server = _gone(settings, tmp_path, monkeypatch)
    rosters = [
        {"topic": None, "members": [{"mri": n, "name": n, "role": "User"}], "picture": None} for n in "ab"
    ]
    fetched: list[str] = []

    async def fetch(self: Directory, thread_id: str) -> dict[str, Any]:
        fetched.append(thread_id)
        return rosters[len(fetched) - 1]

    monkeypatch.setattr(Directory, "_fetch_thread", fetch)
    assert asyncio.run(server.api().directory.thread(MCP_PRIVATE)) == rosters[0]
    assert asyncio.run(server.api().directory.thread(MCP_PRIVATE)) == rosters[1]
    assert fetched == [MCP_PRIVATE] * 2


def test_each_mcp_api_call_reads_names_afresh(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name another process improved after a call is the one the next call shows."""
    server = _gone(settings, tmp_path, monkeypatch)
    first = server.api().directory
    assert first._store is not None
    first._store.put([(MCP_OTHER, "Ann Old", 2, "test", 0)])
    assert first.known_name(MCP_OTHER) == "Ann Old"
    Directory(settings).note_name(MCP_OTHER, "Ann Lee")  # another process, a live name
    assert server.api().directory.known_name(MCP_OTHER) == "Ann Lee"


def test_without_a_store_each_mcp_api_call_asks_again_for_a_missed_person(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With names.db unusable a lookup miss lives in memory only: it must not outlive the call."""
    (settings.config_dir / "names.db").mkdir(parents=True)  # not a database
    server = _gone(settings, tmp_path, monkeypatch)
    profiles = _Profiles({})
    monkeypatch.setattr(D.httpx, "AsyncClient", profiles.client)
    asyncio.run(server.api().directory.resolve([MCP_OTHER]))
    profiles.known[MCP_OTHER] = "Ann Lee"  # the directory has a name for them now
    directory = server.api().directory
    asyncio.run(directory.resolve([MCP_OTHER]))
    assert profiles.batches == [[MCP_OTHER]] * 2 and directory.known_name(MCP_OTHER) == "Ann Lee"


def test_serve_imports_the_rosters_on_the_server_s_directory(
    settings: Settings,
    data: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """038-AC4 for MCP: the import runs on the Directory the tools read through, at start."""
    started: list[tuple[Directory, Path]] = []
    monkeypatch.setattr(
        Directory, "start_roster_import", lambda self, data_dir: started.append((self, data_dir))
    )
    server = M.Server(settings, data, stdin=io.StringIO(""), stdout=io.StringIO())
    assert server.serve() == 0
    assert started == [(server.archive.directory(), data.resolve())]


@pytest.mark.parametrize("names", [{MCP_OTHER: "Ann Lee"}, {MCP_OTHER: "other"}, {}])
def test_mcp_api_reads_name_senders_from_the_directory(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: dict[str, str]
) -> None:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    (settings.cache_dir / "names-v2.json").write_text(json.dumps(names))
    convs = [{"id": MCP_PRIVATE, "lastMessage": {"composetime": "2026-07-03T09:00:00Z"}}]
    _api(monkeypatch, convs, {MCP_PRIVATE: [_mcp_msg("a", "2026-07-02T09:00:00Z")]})
    server = _gone(settings, tmp_path, monkeypatch)
    shown = "Ann Lee" if names.get(MCP_OTHER) == "Ann Lee" else "Somebody"
    read = _call(server, "read_messages", thread_id=MCP_PRIVATE, since="2026-07-01")
    assert read["source"] == "api" and [m["sender"] for m in read["messages"]] == [shown]
    found = _call(server, "search_messages", query="there", since="2026-07-01")
    assert found["source"] == "api" and [m["sender"] for m in found["messages"]] == [shown]


# --- typing: keyed by who types, so a name learned meanwhile cannot strand it ---


CHANNEL = "19:c@thread.tacv2"  # followable, never on the board: only the relay tracks its typing


def _event(msgtype: str, thread: str = T, **extra: Any) -> dict[str, Any]:
    resource = {
        "conversationLink": f"https://h/v1/users/ME/conversations/{thread}",
        "from": CONTACT,
        "imdisplayname": "",
    }
    return {
        "type": "EventMessage",
        "resourceType": "NewMessage",
        "resource": {**resource, "messagetype": msgtype, **extra},
    }


async def _feed(board: W.Board, event: dict[str, Any]) -> None:
    """The event, then every relay it queued: relays run in order, so a marker queued last ends them."""
    await board.on_event(event)
    done = asyncio.Event()

    async def mark() -> None:
        done.set()

    board._enqueue_relay(T, mark)
    await asyncio.wait_for(done.wait(), 2)


@pytest.mark.parametrize(
    ("thread", "followed"),
    [(T, True), (T, False), (CHANNEL, True)],  # board and relay, the board alone, the relay alone
)
@pytest.mark.parametrize(
    "end",
    [
        {"messagetype": "Text", "id": "9", "composetime": "2026-09-15T12:00:00Z", "content": "hi"},
        {"messagetype": "Control/ClearTyping"},
    ],
)
async def test_typing_ends_after_the_typist_got_a_name(
    quiet_directory: Directory,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    thread: str,
    followed: bool,
    end: dict[str, Any],
) -> None:
    row = {"id": T, "label": "T", "last_activity": "2026-09-15T10:00:00Z", "last_id": "1", "typing": []}
    board = W.Board({T: row}, quiet_directory, me=ME, typing_ttl=60)
    frames: list[dict[str, Any]] = []
    monkeypatch.setattr(board, "_relay_now", lambda thread, frame: frames.append(frame))
    monkeypatch.setattr(board, "_relay_queued", lambda thread, frame: frames.append(frame))
    if followed:
        board._follows[object()] = (thread, None)  # type: ignore[index]
    await _feed(board, _event("Control/Typing", thread))
    shown = f"name:{CONTACT}"  # no name known yet: the lookup's answer
    quiet_directory.note_name(ANN, "Ann Lee")  # the roster names them while they type
    await _feed(board, _event("Control/Typing", thread))  # still typing: announced under the same name
    assert row["typing"] == ([shown] if thread == T else [])
    await _feed(
        board, _event(end["messagetype"], thread, **{k: v for k, v in end.items() if k != "messagetype"})
    )
    assert row["typing"] == []
    assert board._typing_timers == {}
    typing = [f for f in frames if "typing" in f]
    assert typing[-1] == {"typing": shown, "on": False}
    assert {f["typing"] for f in typing} == {shown}
    board.close()


@pytest.mark.parametrize("before", [None, "marker"])
async def test_a_lookup_that_names_nobody_repaints_no_receipts(
    directory: Directory, monkeypatch: pytest.MonkeyPatch, before: str | None
) -> None:
    from miniteams import receipts as R

    sent: list[dict[str, Any]] = []

    async def resolve(mris: list[str], *, refresh: bool = False) -> None:
        if refresh:
            directory.note_name(ANN, "Ann Lee")

    if before is not None:
        directory._note_missed([ANN])
    directory.note_declared(ANN, BOGUS)
    monkeypatch.setattr(directory, "resolve", resolve)
    receipts = R.Receipts(directory, ME, lambda t, f: sent.append(f), None, None)
    receipts.shown[T] = {ANN: 5}
    await receipts._name(T, ANN)
    assert sent == []  # a declared label is no news
    monkeypatch.setattr(directory, "resolve", lambda mris, refresh=False: resolve(mris, refresh=True))
    await receipts._name(T, ANN)
    assert sent == [{"receipts": {ANN: {"id": "5", "name": "Ann Lee"}}}]
    await receipts.close()


# --- the old names cache files: imported once, left on disk ---


def _old_cache(settings: Settings, names: dict[str, str]) -> Path:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    old = settings.cache_dir / "names.json"  # every earlier version also wrote declared names here
    old.write_text(json.dumps(names))
    return old


@pytest.mark.parametrize("v2", [None, {ANN: "Ann Lee"}])
def test_the_old_names_files_are_imported_and_left_alone(
    settings: Settings, v2: dict[str, str] | None
) -> None:
    """038-AC4: names-v2.json gives archived names, the unversioned names.json declared labels only."""
    old = _old_cache(settings, {ANN: BOGUS, "8:orgid:bob": "Bob Roe"})
    before = old.read_bytes()
    v2_path = settings.cache_dir / "names-v2.json"
    if v2 is not None:
        v2_path.write_text(json.dumps(v2))
    v2_before = v2_path.read_bytes() if v2 else None
    directory = Directory(settings)
    assert directory.known_name(ANN) == ("Ann Lee" if v2 else "")
    assert directory.known_name("8:orgid:bob") == ""
    assert directory.name_for("8:orgid:bob") == "Bob Roe"
    assert sender_name(ANN, REAL, directory.known_name) == ("Ann Lee" if v2 else REAL)
    directory.note_name("8:orgid:cy", "Cy Ray")
    assert old.read_bytes() == before  # the user's files are never rewritten nor deleted
    assert (v2_path.read_bytes() if v2 else None) == v2_before
    assert v2_path.exists() == bool(v2)


class _Profiles:
    """The profile lookup: records each batch, names the MRIs it knows; a thread GET answers `thread`."""

    def __init__(self, known: dict[str, str], thread: dict[str, Any] | None = None) -> None:
        self.known = known
        self.thread = thread
        self.batches: list[list[str]] = []

    def client(self, *_: Any, **__: Any) -> Any:
        profiles = self

        class _Client:
            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *_: Any) -> bool:
                return False

            async def get(self, url: str, headers: Any = None) -> Any:
                from test_directory_fetch import _Resp

                return _Resp(profiles.thread)

            async def post(self, url: str, headers: Any = None, json: Any = None) -> Any:
                from test_directory_fetch import _Resp

                profiles.batches.append(list(json))
                names = [{"mri": m, "displayName": profiles.known[m]} for m in json if m in profiles.known]
                return _Resp({"value": names})

        return _Client()


async def test_after_the_upgrade_a_roster_is_resolved_in_one_batch(
    directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _old_cache(directory.settings, {ANN: BOGUS, "8:orgid:bob": "Bob Roe", "8:orgid:cy": "Cy Ray"})
    directory = Directory(directory.settings)
    directory.set_token("sk", "bearer")
    members = [{"mri": m, "friendlyName": ""} for m in (ANN, "8:orgid:bob", "8:orgid:cy")]
    profiles = _Profiles({ANN: "Ann Lee", "8:orgid:bob": "Bob Roe"}, {"members": members})
    monkeypatch.setattr(D.httpx, "AsyncClient", profiles.client)
    info = await directory.thread(T)
    assert info is not None
    assert profiles.batches == [sorted([ANN, "8:orgid:bob", "8:orgid:cy"])]  # one lookup, not one per member
    assert [m["name"] for m in info["members"]] == ["Ann Lee", "Bob Roe", "cy"]


@pytest.mark.parametrize("followed", [False, True])  # the board alone, and board plus relay
async def test_a_thread_activity_from_the_typist_does_not_end_their_typing(
    quiet_directory: Directory,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    followed: bool,
) -> None:
    row = {"id": T, "label": "T", "last_activity": "2026-09-15T10:00:00Z", "last_id": "1", "typing": []}
    board = W.Board({T: row}, quiet_directory, me=ME, typing_ttl=60)
    monkeypatch.setattr(board, "_relay_now", lambda thread, frame: None)
    monkeypatch.setattr(board, "_relay_queued", lambda thread, frame: None)
    if followed:
        board._follows[object()] = (T, None)  # type: ignore[index]
    board._typing_arm(T, ANN, "Ann Lee")
    row["typing"].append("Ann Lee")
    await _feed(
        board,
        _event("ThreadActivity/TopicUpdate", id="9", composetime="2026-09-15T12:00:00Z", content="<x/>"),
    )
    assert (T, ANN) in board._typing_timers
    assert row["typing"] == ["Ann Lee"]
    board.close()
