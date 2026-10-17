"""Spec 027 phase 2: `properties.files` entries ride the attachment pipeline (SharePoint faked end to end)."""

import asyncio
import base64
import json
import os
import sqlite3
from typing import Any

import pytest
import test_media_types as TM
from test_archive import _FakeApi, _wire
from test_media_types import C1, SP, _archive, _on_disk, _Tokens, fetched  # noqa: F401 — fixture

from miniteams import archive as AR
from miniteams import archive_live as AL
from miniteams import attachments as A
from miniteams import auth, messages
from miniteams.archive_store import ChatStore
from miniteams.config import Settings
from miniteams.directory import Directory

SHARE = f"{SP}/:w:/g/personal/x/share1"
DOC_URL = f"{SP}/personal/x/Documents/Microsoft%20Teams%20Chat%20Files/report.docx"
DOC = {"fileName": "report.docx", "state": "active", "objectUrl": DOC_URL, "fileInfo": {"shareUrl": SHARE}}
# A video behind a share link that does not look like one: the name says what it is.
MOVIE = {
    "fileName": "movie.mp4",
    "state": "active",
    "objectUrl": f"{SP}/personal/x/Documents/movie.mp4",
    "fileInfo": {"shareUrl": f"{SP}/:u:/g/personal/x/share2"},
}
REF = {
    "fileName": "budget.xlsx",
    "state": "reference",
    "fileInfo": {"shareUrl": f"{SP}/:x:/g/personal/x/share3"},
}
BODY_LINK = f'<a itemtype="{A._FILE_LINK}" href="{SHARE}">report.docx</a>'
NAMES = {
    "share1": "report.docx",
    "share2": "movie.mp4",
    "share3": "budget.xlsx",
    "report.docx": "report.docx",
}
SHARE_ID = base64.urlsafe_b64encode(SHARE.encode()).decode().rstrip("=")  # its /shares API key
DOC_ID = base64.urlsafe_b64encode(DOC_URL.encode()).decode().rstrip("=")


def _msg(mid: str, i: int, content: str, files: list[Any]) -> dict[str, Any]:
    return {
        "id": mid,
        "composetime": f"2026-07-01T09:00:0{i}Z",
        "content": content,
        "messagetype": "RichText/Html",
        "properties": {"files": json.dumps(files)},
    }


MESSAGES = [
    _msg("f1", 1, "<p>see</p>", [DOC, MOVIE, REF]),
    _msg("f2", 2, f"<p>again {BODY_LINK}</p>", [DOC]),  # the body links the same file
]


@pytest.fixture
def wired(monkeypatch) -> None:
    monkeypatch.setattr(auth.TokenSource, "acquire", lambda self: {"access_token": "x"})
    monkeypatch.setattr(auth, "_app", lambda *a: None)
    monkeypatch.setattr(AR, "RefreshingToken", lambda settings, source: _Tokens("sk"))
    monkeypatch.setattr(AR, "_INTER_PAGE_DELAY", 0)
    for key, name in NAMES.items():
        monkeypatch.setitem(TM._SP_NAMES, key, name)
    _wire(monkeypatch, _FakeApi({C1: MESSAGES}), [C1])


def _asks_share(url: str) -> bool:
    """A SharePoint call for the document by its sharing link: REST, or the /shares fallback."""
    return "share1" in TM._shared(url) or SHARE_ID in url


def _asks_doc(url: str) -> bool:
    """A SharePoint call for the document, by its sharing link or its objectUrl."""
    return _asks_share(url) or TM._shared(url) == DOC_URL or DOC_ID in url


def _contents(urls: list[str]) -> list[str]:
    """The files whose bytes were fetched (a GetFileByUrl `$value` call), by name."""
    return [NAMES[TM._shared(u).rsplit("/", 1)[1]] for u in urls if TM._SP_REST in u and "/$value" in u]


# --- extraction ---


def test_entries_become_sharepoint_items_reference_excluded() -> None:
    """027-AC3: each entry but a pasted `reference` is an sp_file, by its sharing link else its objectUrl."""
    bare = {"fileName": "a.pdf", "objectUrl": f"{SP}/personal/x/a.pdf"}
    assert A.extract("", "RichText/Html", files=[DOC, REF, bare]) == [
        {"kind": "sp_file", "url": SHARE, "fallback": DOC_URL},
        {"kind": "sp_file", "url": bare["objectUrl"]},
    ]
    same = {"objectUrl": SHARE, "fileInfo": {"shareUrl": SHARE}}
    assert A.extract("", "", files=[same]) == [{"kind": "sp_file", "url": SHARE}]


@pytest.mark.parametrize(
    ("entry", "items"),
    [
        ({"objectUrl": "https://example.com/a.pdf"}, []),
        ({"objectUrl": "http://example.sharepoint.com/a.pdf"}, []),
        (
            {
                "objectUrl": DOC_URL,
                "fileInfo": {"shareUrl": "https://evil.example/@example.sharepoint.com/x"},
            },
            [DOC_URL],
        ),
        ({"objectUrl": "https://example.com/a.pdf", "fileInfo": {"shareUrl": SHARE}}, [SHARE]),
    ],
)
def test_only_sharepoint_urls_are_kept(entry: dict[str, Any], items: list[str]) -> None:
    """027-AC3: an entry URL off a SharePoint tenant host is neither fetched nor a fallback."""
    assert A.extract("", "", files=[entry]) == [{"kind": "sp_file", "url": u} for u in items]
    assert A.extract("", "", files=[REF]) == []


@pytest.mark.parametrize(
    "entry",
    [
        None,
        "x",
        5,
        {"fileName": "a.pdf"},
        {"objectUrl": 5, "fileInfo": "x"},
        {"objectUrl": "", "fileInfo": None},
    ],
)
def test_malformed_entries_are_skipped(entry: Any) -> None:
    """027-AC3: an entry without a usable URL adds nothing and keeps the message's other attachments."""
    assert A.extract(BODY_LINK, "RichText/Html", files=[entry]) == [{"kind": "sp_file", "url": SHARE}]


@pytest.mark.parametrize("raw", [f"{SP}/personal/x/a&amp;b.pdf", f"{SP}/personal/x/a.pdf?x=1&lt=2"])
def test_entry_urls_are_taken_verbatim(raw: str) -> None:
    """027-AC3: entry URLs are JSON, not HTML: no entity is decoded (the body's links still are)."""
    assert A.extract("", "", files=[{"objectUrl": raw}]) == [{"kind": "sp_file", "url": raw}]


@pytest.mark.parametrize(
    "entry",
    [
        MOVIE,  # by its name
        {"objectUrl": f"{SP}/personal/x/clip.MOV"},  # by its objectUrl
        {"fileName": "f", "fileInfo": {"shareUrl": f"{SP}/:v:/g/personal/x/v"}},  # by its sharing link
    ],
)
def test_a_video_entry_follows_the_videos_type(entry: dict[str, Any]) -> None:
    """027-AC4: a video entry is `videos` (off by default), everything else `files`."""
    assert A.extract("", "", files=[entry]) == []
    assert A.extract("", "", frozenset({"files"}), [entry]) == []
    assert len(A.extract("", "", frozenset({"videos"}), [entry])) == 1
    assert A.extract("", "", frozenset({"videos"}), [DOC]) == []


@pytest.mark.parametrize("href", [SHARE, DOC_URL])
def test_an_entry_the_body_already_links_is_one_item(href: str) -> None:
    """027-AC3: the body's link to the file, by its sharing link or its objectUrl, and the entry are one."""
    body = f'<a itemtype="{A._FILE_LINK}" href="{href}">r</a>'
    assert A.extract(body, "RichText/Html", files=[DOC]) == [{"kind": "sp_file", "url": href}]
    assert A.extract(body, "RichText/Html", files=[DOC, DOC]) == [{"kind": "sp_file", "url": href}]


def test_message_files_decodes_the_property() -> None:
    assert messages.message_files({"properties": {"files": json.dumps([DOC])}}) == [DOC]
    assert messages.message_files({"properties": {"files": [DOC]}}) == [DOC]
    assert messages.message_files({"properties": "x"}) == messages.message_files({}) == []


async def test_the_stream_names_the_entries(directory: Directory, monkeypatch, fetched, capsys) -> None:  # noqa: F811
    """027-AC3: the stream's line and JSONL record name the entries among the message's attachments; the
    stream holds no SharePoint bearer, so nothing is fetched."""

    async def label(thread_id: str) -> str:
        return "L"

    monkeypatch.setattr(directory, "label", label)
    resource = MESSAGES[0] | {"from": "8:orgid:x", "imdisplayname": "X"}
    await messages.print_resource(resource, directory)
    assert f"[file(sp): {SHARE}]" in capsys.readouterr().out and fetched == []
    record = await messages.resource_to_record(resource, directory)
    assert record["attachments"] == [{"kind": "sp_file", "url": SHARE, "fallback": DOC_URL}]


# --- archive one-shot, --verify-media, live ---


def test_archive_downloads_each_entry_once(wired, fetched, tmp_path) -> None:  # noqa: F811
    """027-AC3: the one-shot downloads the entry to the chat's media folder, once though two messages
    (one also linking it in its body) carry it; a re-run fetches nothing; the reference is never fetched."""
    data = tmp_path / "data"
    assert _archive(data) == 0
    assert _on_disk(data) == {"report.docx": b"bytes of report.docx"}
    assert _contents(fetched) == ["report.docx"]
    assert not [u for u in fetched if "share3" in TM._shared(u)]
    fetched.clear()
    assert _archive(data, "--verify-media") == 0
    assert _contents(fetched) == []


def test_videos_flag_adds_the_video_entry(wired, fetched, tmp_path) -> None:  # noqa: F811
    """027-AC4: a video entry downloads only with videos selected."""
    data = tmp_path / "data"
    assert _archive(data, "--videos") == 0
    assert set(_on_disk(data)) == {"report.docx", "movie.mp4"}


@pytest.mark.parametrize("flags", [("--no-media",), ("--media", "images,transcripts")])
def test_no_media_or_files_off_downloads_none(wired, fetched, tmp_path, flags) -> None:  # noqa: F811
    """027-AC4: --no-media, or a selection without files, fetches no entry."""
    data = tmp_path / "data"
    assert _archive(data, *flags) == 0
    assert _on_disk(data) == {} and fetched == []


def test_verify_media_catches_up_on_archived_messages(wired, fetched, tmp_path) -> None:  # noqa: F811
    """027-AC3: messages archived without media get their entries on the --verify-media sweep."""
    data = tmp_path / "data"
    assert _archive(data, "--no-media") == 0
    assert _on_disk(data) == {}
    assert _archive(data, "--verify-media") == 0
    assert _on_disk(data) == {"report.docx": b"bytes of report.docx"}


@pytest.mark.parametrize("status", [403, 404])
def test_a_denied_entry_backs_off_and_retry_assets_forces_it(
    wired,
    fetched,  # noqa: F811
    tmp_path,
    monkeypatch,
    status: int,
) -> None:
    """027-AC4: an entry denied by both its URLs is recorded, skipped on the next sweep, re-tried with
    --retry-assets."""
    serve = TM._serve
    monkeypatch.setattr(TM, "_serve", lambda u, p: TM._Resp(status=status) if _asks_doc(u) else serve(u, p))
    data = tmp_path / "data"
    assert _archive(data) == 0
    assert _on_disk(data) == {}
    store = ChatStore(data, C1)
    try:
        assert SHARE in store.denied_urls(AR._now_iso())
    finally:
        store.close()
    assert any(SHARE_ID in u for u in fetched) and any(DOC_ID in u for u in fetched)  # every route asked
    fetched.clear()
    assert _archive(data, "--verify-media") == 0
    assert not [u for u in fetched if _asks_doc(u)]
    assert _archive(data, "--verify-media", "--retry-assets") == 0
    assert [u for u in fetched if _asks_doc(u)]


@pytest.mark.parametrize("status", [403, 404])
@pytest.mark.parametrize("non_file", [False, True])
def test_a_failed_sharing_link_falls_back_to_the_object_url(
    wired,
    fetched,  # noqa: F811
    tmp_path,
    monkeypatch,
    status: int,
    non_file: bool,
) -> None:
    """027-AC3: when the sharing link is denied, or resolves to no file, the objectUrl downloads the entry and
    is never recorded as denied; the body link to the same sharing link then finds the entry's copy (027-AC7)
    and is not denied either; a copy gone from disk is fetched again on the next sweep."""
    serve = TM._serve

    def fake(url: str, params: Any) -> TM._Resp:
        if non_file and SHARE_ID in url:
            return TM._Resp(data={"name": "a folder"})  # /shares: no `file` facet
        return TM._Resp(status=status) if _asks_share(url) else serve(url, params)

    monkeypatch.setattr(TM, "_serve", fake)
    data = tmp_path / "data"
    assert _archive(data) == 0
    assert _on_disk(data) == {"report.docx": b"bytes of report.docx"}
    store = ChatStore(data, C1)
    try:
        assert store.denied_urls(AR._now_iso()) == set()
    finally:
        store.close()
    (TM._media_dir(data) / "report.docx").unlink()
    assert _archive(data, "--verify-media") == 0
    assert _on_disk(data) == {"report.docx": b"bytes of report.docx"}


async def test_a_denied_fallback_is_not_asked_again(settings: Settings, fetched, monkeypatch) -> None:  # noqa: F811
    """027-AC4: an entry whose objectUrl is denied asks only its sharing link; when that fails too, only the
    sharing link's failure is recorded (the objectUrl's stands)."""
    monkeypatch.setattr(TM, "_serve", lambda u, p: TM._Resp(status=403))
    failed: list[str] = []
    notes = await A.process(
        "",
        "RichText/Html",
        "sk",
        settings.media_dir,
        download=True,
        sp_token=lambda host: "SPTOK",
        skip_url={DOC_URL}.__contains__,
        on_fail=lambda url, exc: failed.append(url),
        files=[DOC],
    )
    assert notes == [f"[file(sp): {SHARE}]"] and failed == [SHARE]
    assert fetched and all(_asks_share(u) for u in fetched)


async def test_live_archiver_downloads_the_entry(
    settings: Settings,
    tmp_path,
    fetched,  # noqa: F811
    monkeypatch,
) -> None:
    """027-AC3: a live message's entry lands in the chat's media folder; a reference-only one spawns no
    download."""
    monkeypatch.setitem(TM._SP_NAMES, "share1", "report.docx")
    live = AL.LiveArchive(settings, tmp_path / "data", _Tokens("sk"))
    try:
        for mid, files in (("m1", [DOC]), ("m2", [REF])):
            resource = MESSAGES[0] | {"id": mid, "version": "1", "properties": {"files": files}}
            resource["conversationLink"] = f"https://x/v1/users/ME/conversations/{C1}"
            await live.on_event({"type": "EventMessage", "resourceType": "NewMessage", "resource": resource})
            if mid == "m2":
                assert not live._media_tasks
            await asyncio.wait_for(asyncio.gather(*live._media_tasks), 5)
    finally:
        live.close()
    assert _on_disk(live.data_dir) == {"report.docx": b"bytes of report.docx"}


# --- phase 3: local copies by URL, name collisions (027-AC7, AC8) ---

OTHER_SHARE = f"{SP}/:w:/g/personal/y/share9"
OTHER_URL = f"{SP}/personal/y/Documents/Microsoft%20Teams%20Chat%20Files/report.docx"
# Another file of the same name, in someone else's OneDrive.
OTHER = {
    "fileName": "report.docx",
    "state": "active",
    "objectUrl": OTHER_URL,
    "fileInfo": {"shareUrl": OTHER_SHARE},
}
UID, UID2 = "0a0a0a0a-0000-4000-8000-000000000001", "0b0b0b0b-0000-4000-8000-000000000002"
MODIFIED = "2026-07-01T10:00:00Z"  # the last change SharePoint reports
MODIFIED_S = 1782900000.0


@pytest.fixture
def sharepoint(monkeypatch) -> dict[str, tuple[str, str]]:
    """SharePoint REST faked per file URL: {url: (name, unique id)}; the bytes name both."""
    files = {SHARE: ("report.docx", UID), DOC_URL: ("report.docx", UID)}

    def serve(url: str, params: Any) -> TM._Resp:
        if TM._SP_REST not in url or TM._shared(url) not in files:
            return TM._Resp(status=404)
        name, uid = files[TM._shared(url)]
        body = f"{uid} {name}".encode()
        if "/$value" in url:
            return TM._Resp(body)
        meta = {"Name": name, "UniqueId": uid.upper(), "Length": str(len(body)), "TimeLastModified": MODIFIED}
        return TM._Resp(data=meta)

    monkeypatch.setattr(TM, "_serve", serve)
    return files


def _rearchive(monkeypatch, messages: list[dict[str, Any]]) -> None:
    _wire(monkeypatch, _FakeApi({C1: messages}), [C1])


def _saved(data, urls: list[str]) -> dict[str, str]:
    store = ChatStore(data, C1)
    try:
        return store.saved_files(urls)
    finally:
        store.close()


def test_the_table_is_added_to_an_old_archive(tmp_path) -> None:
    """027 data model: opening a chat archived before phase 3 adds the table and keeps every row."""
    store = ChatStore(tmp_path, C1)
    store.insert_page([_msg("f1", 1, "<p>x</p>", [DOC])])
    store._db.execute("DROP TABLE sp_files")
    store._db.commit()
    store.close()
    store = ChatStore(tmp_path, C1)
    try:
        assert store.count() == 1 and store.saved_files([SHARE]) == {} and store.file_owners("x") == set()
        store.save_file([SHARE, DOC_URL], "report.docx", UID)
        store.save_file([DOC_URL], "report (2).docx", "")  # a later save of a URL replaces its row
        assert store.saved_files([SHARE, DOC_URL, OTHER_URL]) == {
            SHARE: "report.docx",
            DOC_URL: "report (2).docx",
        }
        assert store.file_owners("report.docx") == {UID} and store.file_owners("report (2).docx") == {""}
    finally:
        store.close()


def test_a_held_file_costs_no_sharepoint_call(wired, sharepoint, fetched, tmp_path) -> None:  # noqa: F811
    """027-AC7: the download records the copy under the entry's sharing link and objectUrl; the next passes
    make no SharePoint call at all, until the copy leaves the disk."""
    data = tmp_path / "data"
    assert _archive(data) == 0
    assert _on_disk(data) == {"report.docx": f"{UID} report.docx".encode()}
    assert _saved(data, [SHARE, DOC_URL]) == {SHARE: "report.docx", DOC_URL: "report.docx"}
    for _ in range(2):
        fetched.clear()
        assert _archive(data, "--verify-media") == 0
        assert fetched == []
    (TM._media_dir(data) / "report.docx").unlink()
    assert _archive(data, "--verify-media") == 0
    assert _on_disk(data) == {"report.docx": f"{UID} report.docx".encode()} and fetched


def test_the_objecturl_alone_finds_the_copy(wired, sharepoint, fetched, tmp_path, monkeypatch) -> None:  # noqa: F811
    """027-AC7: a later entry that knows the file by its objectUrl only (no sharing link) finds the copy."""
    data = tmp_path / "data"
    assert _archive(data) == 0
    fetched.clear()
    _rearchive(monkeypatch, [*MESSAGES, _msg("f3", 3, "<p>x</p>", [{**DOC, "fileInfo": {}}])])
    assert _archive(data, "--verify-media") == 0
    assert fetched == [] and _stored_ids(data) >= {"f3"}


def _stored_ids(data) -> set[str]:
    store = ChatStore(data, C1)
    try:
        return {m["id"] for m in store.iter_messages()}
    finally:
        store.close()


def test_two_files_with_one_name_are_both_kept(wired, sharepoint, fetched, tmp_path, monkeypatch) -> None:  # noqa: F811
    """027-AC8: a different file under a taken name is saved as `name (2).ext`, recorded under its own URLs;
    each is fetched once."""
    sharepoint |= {OTHER_SHARE: ("report.docx", UID2), OTHER_URL: ("report.docx", UID2)}
    _rearchive(monkeypatch, [*MESSAGES, _msg("f3", 3, "<p>x</p>", [OTHER])])
    data = tmp_path / "data"
    assert _archive(data) == 0
    assert _on_disk(data) == {
        "report.docx": f"{UID} report.docx".encode(),
        "report (2).docx": f"{UID2} report.docx".encode(),
    }
    assert _saved(data, [SHARE, DOC_URL, OTHER_SHARE, OTHER_URL]) == {
        SHARE: "report.docx",
        DOC_URL: "report.docx",
        OTHER_SHARE: "report (2).docx",
        OTHER_URL: "report (2).docx",
    }
    assert len([u for u in fetched if "/$value" in u]) == 2
    fetched.clear()
    assert _archive(data, "--verify-media") == 0
    assert fetched == []


def test_one_file_under_two_urls_is_one_copy(wired, sharepoint, fetched, tmp_path, monkeypatch) -> None:  # noqa: F811
    """027-AC8: the same file met through another link (same SharePoint id) is not fetched again: its link
    is recorded on the copy held."""
    again = f"{SP}/:w:/g/personal/x/share1b"
    sharepoint[again] = ("report.docx", UID)
    _rearchive(
        monkeypatch,
        [*MESSAGES, _msg("f3", 3, "<p>x</p>", [{**DOC, "objectUrl": "", "fileInfo": {"shareUrl": again}}])],
    )
    data = tmp_path / "data"
    assert _archive(data) == 0
    assert _on_disk(data) == {"report.docx": f"{UID} report.docx".encode()}
    assert len([u for u in fetched if "/$value" in u]) == 1
    assert _saved(data, [again]) == {again: "report.docx"}


class _Events:
    """The events a module logs, whatever level the CLI configures."""

    def __init__(self, monkeypatch, module: Any) -> None:
        self.events: list[dict[str, Any]] = []

        def record(event: str, **kw: Any) -> None:
            self.events.append({"event": event, **kw})

        fake = type("_Log", (), {lvl: staticmethod(record) for lvl in ("debug", "info", "warning", "error")})
        monkeypatch.setattr(module, "log", fake)

    def named(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]


LEGACY = f"{UID} report.docx".encode()  # what SharePoint serves for DOC: same size, same bytes


@pytest.mark.parametrize(
    ("on_disk", "known", "age", "adopted"),
    [
        (LEGACY, {"Length", "TimeLastModified"}, -1, True),  # SharePoint's size, an older copy
        (LEGACY, {"Length", "TimeLastModified"}, 1, True),  # SharePoint's size, a newer copy
        (b"x" * len(LEGACY), {"Length"}, -1, True),  # the size is the test, not the bytes
        # A newer copy of another size may be another file of the same name: a duplicate beats a wrong file.
        (b"rewritten by Office", {"Length", "TimeLastModified"}, 0, False),
        (b"rewritten by Office", {"TimeLastModified"}, 1, False),
        (b"another file", {"Length", "TimeLastModified"}, -1, False),
        (LEGACY + b"!", {"Length"}, 1, False),
        (LEGACY, set(), 1, False),  # SharePoint gives no size: never bind a guess
    ],
)
def test_a_copy_from_before_the_record_is_adopted_when_it_matches(
    wired,
    sharepoint,
    fetched,  # noqa: F811
    tmp_path,
    monkeypatch,
    on_disk: bytes,
    known: set[str],
    age: int,
    adopted: bool,
) -> None:
    """027-AC7, AC8: a file saved by name before the record (no row) costs one metadata call on the next
    sweep; it is recorded as this file only when its size is SharePoint's, whatever its age; else it stays,
    the mismatch is logged and the file is downloaded as `name (2).ext`. Either way, the sweep after makes
    no call."""
    serve = TM._serve

    def partial(url: str, params: Any) -> TM._Resp:
        resp = serve(url, params)
        if isinstance(resp._data, dict):
            resp._data = {
                k: v for k, v in resp._data.items() if k in known or k not in ("Length", "TimeLastModified")
            }
        return resp

    monkeypatch.setattr(TM, "_serve", partial)
    data = tmp_path / "data"
    assert _archive(data, "--no-media") == 0
    media = TM._media_dir(data)
    media.mkdir(parents=True)
    copy = media / "report.docx"
    copy.write_bytes(on_disk)
    os.utime(copy, (MODIFIED_S + age * 60, MODIFIED_S + age * 60))
    logs = _Events(monkeypatch, A)
    assert _archive(data, "--verify-media") == 0
    logged = logs.named("sp_file_legacy_mismatch")
    name = "report.docx" if adopted else "report (2).docx"
    assert _on_disk(data) == {"report.docx": on_disk} | ({} if adopted else {name: LEGACY})
    assert len([u for u in fetched if "/$value" in u]) == (0 if adopted else 1)
    assert _saved(data, [SHARE, DOC_URL]) == {SHARE: name, DOC_URL: name}
    if adopted:
        assert logged == []
    else:
        assert len(logged) == 1
        assert {k: logged[0][k] for k in ("name", "local_size", "remote_size", "remote_modified")} == {
            "name": "report.docx",
            "local_size": len(on_disk),
            "remote_size": len(LEGACY) if "Length" in known else None,
            "remote_modified": MODIFIED_S if "TimeLastModified" in known else None,
        }
        assert logged[0]["local_mtime"] == MODIFIED_S + age * 60
    fetched.clear()
    assert _archive(data, "--verify-media") == 0
    assert fetched == []


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        (MODIFIED, MODIFIED_S),  # REST TimeLastModified
        ("2026-07-01T10:00:00.5Z", MODIFIED_S + 0.5),  # driveItem lastModifiedDateTime
        ("2026-07-01T12:00:00+02:00", MODIFIED_S),
        ("2026-07-01T10:00:00", None),  # no zone: no time to compare
        ("yesterday", None),
        (None, None),
    ],
)
def test_the_sharepoint_time_is_read_as_epoch_seconds(value: Any, seconds: float | None) -> None:
    """027-AC8: SharePoint's last change, in epoch seconds like st_mtime; unknown when unreadable."""
    assert A._sp_time(value) == seconds


@pytest.mark.parametrize(
    ("meta", "size"),
    [({"Length": "42"}, 42), ({"size": 42}, 42), ({"Length": ""}, None), ({"size": -1}, None), ({}, None)],
)
def test_the_sharepoint_size_is_read_from_rest_or_drive_item(meta: dict[str, Any], size: int | None) -> None:
    """027-AC8: REST says `Length` (a string), a drive item `size` (a number); anything else is unknown."""
    assert A._sp_size(meta.get("Length", meta.get("size"))) == size


def test_an_unrecorded_numbered_name_is_never_adopted(tmp_path) -> None:
    """027-AC8: `name (n).ext` on disk without a record is not this file: the next free number is used."""
    store = ChatStore(tmp_path, C1)
    media = store.media_dir
    media.mkdir()
    (media / "a.pdf").write_bytes(b"1")
    (media / "a (2).pdf").write_bytes(b"stray")
    store.save_file(["https://x/1"], "a.pdf", UID)
    try:
        assert asyncio.run(_one_download(store, "a.pdf", UID2)) == str(media / "a (3).pdf")
        assert (media / "a (2).pdf").read_bytes() == b"stray"
        assert store.saved_files([f"{SP}/new"]) == {f"{SP}/new": "a (3).pdf"}
    finally:
        store.close()


async def _one_download(store: ChatStore, name: str, uid: str, url: str = f"{SP}/new") -> str | None:
    class _C:
        async def get(self, u: str, **_: Any) -> TM._Resp:
            return TM._Resp(data={"Name": name, "eTag": f'"{{{uid}}},4"'})

        def stream(self, method: str, u: str, **_: Any) -> TM._Resp:
            return TM._Resp(b"fresh")

    return await A._download_sp_file(_C(), url, "SPTOK", store.media_dir, store)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "second", "third"),
    [
        ("report.docx", "report (2).docx", "report (3).docx"),
        ("notes", "notes (2)", "notes (3)"),
        (".env", ".env (2)", ".env (3)"),
        ("a.tar.gz", "a.tar (2).gz", "a.tar (3).gz"),
    ],
)
def test_numbered_names(name: str, second: str, third: str) -> None:
    """027-AC8: `name (n).ext`, extension kept; a dotfile or a name without one gets the number last."""
    assert [A._numbered(name, n) for n in (1, 2, 3)] == [name, second, third]


@pytest.mark.parametrize("meta", [{"UniqueId": UID.upper()}, {"eTag": f'"{{{UID.upper()}}},3"'}])
def test_the_sharepoint_id_is_read_from_rest_or_drive_item(meta: dict[str, str]) -> None:
    """027-AC8: REST metadata carries `UniqueId`, a drive item its `eTag`: one id for one file."""
    assert A._sp_id(meta) == UID
    assert A._sp_id({}) == A._sp_id({"eTag": "opaque"}) == ""


class _Slow(TM._Resp):
    async def aiter_bytes(self, size: int = 65536) -> Any:
        await asyncio.sleep(0)  # the other task runs into the lock meanwhile
        yield self.content


async def test_concurrent_downloads_of_one_entry_fetch_it_once(tmp_path) -> None:
    """027-AC8: two messages carrying one entry at once: one content fetch, the waiter takes the copy."""
    store = ChatStore(tmp_path, C1)
    store.media_dir.mkdir()
    streams: list[str] = []

    class _C:
        async def get(self, u: str, **_: Any) -> TM._Resp:
            return TM._Resp(data={"Name": "r.docx"})  # no id: only the record tells it is this file

        def stream(self, method: str, u: str, **_: Any) -> TM._Resp:
            streams.append(u)
            return _Slow(b"r")

    try:
        got = await asyncio.gather(
            *(
                A._download_sp_file(_C(), SHARE, "SPTOK", store.media_dir, store, [SHARE, DOC_URL])
                for _ in range(2)
            )  # type: ignore[arg-type]
        )
        assert len(streams) == 1 and got[0] == got[1] == str(store.media_dir / "r.docx")
        assert sorted(p.name for p in store.media_dir.iterdir()) == ["r.docx"]
    finally:
        store.close()


# --- review fixes: denied sharing link, unrecordable copies, numbered-name lock, recordings ---


async def test_a_denied_sharing_link_is_not_asked_while_the_objecturl_serves(
    settings: Settings,
    sharepoint,
    fetched,  # noqa: F811
) -> None:
    """027-AC4: a denied sharing link is skipped though first; the objectUrl gives the file."""
    store = ChatStore(settings.media_dir.parent, C1)
    try:
        notes = await A.process(
            "",
            "RichText/Html",
            "sk",
            store.media_dir,
            download=True,
            sp_token=lambda host: "SPTOK",
            skip_url={SHARE}.__contains__,
            files=[DOC],
            store=store,
        )
        assert notes and "→" in notes[0]
        assert fetched and not [u for u in fetched if _asks_share(u)]
        assert store.saved_files([SHARE, DOC_URL]) == {SHARE: "report.docx", DOC_URL: "report.docx"}
    finally:
        store.close()


def test_a_denied_sharing_link_stays_unasked_on_every_sweep(wired, sharepoint, fetched, tmp_path) -> None:  # noqa: F811
    """027-AC4: the archive's denied sharing link is never asked again while the objectUrl downloads the
    entry; the body link to it alone is a ref."""
    data = tmp_path / "data"
    assert _archive(data, "--no-media") == 0
    store = ChatStore(data, C1)
    store.mark_denied(SHARE, 403, AR._now_iso())
    store.close()
    assert _archive(data, "--verify-media") == 0
    assert _on_disk(data) == {"report.docx": LEGACY}
    assert fetched and not [u for u in fetched if _asks_share(u)]


def test_a_copy_the_archive_cannot_record_is_kept_then_recorded(
    wired,
    sharepoint,
    fetched,  # noqa: F811
    tmp_path,
    monkeypatch,
) -> None:
    """027-AC7: a locked archive at record time keeps the copy under its own name (no denial, no failure);
    the next pass records it from its size without downloading it again."""
    real = ChatStore.save_file

    def locked(self: ChatStore, *a: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ChatStore, "save_file", locked)
    data = tmp_path / "data"
    assert _archive(data) == 0
    assert _on_disk(data) == {"report.docx": LEGACY} and _saved(data, [SHARE]) == {}
    monkeypatch.setattr(ChatStore, "save_file", real)
    fetched.clear()
    assert _archive(data, "--verify-media") == 0
    assert not [u for u in fetched if "/$value" in u]
    assert _saved(data, [SHARE, DOC_URL]) == {SHARE: "report.docx", DOC_URL: "report.docx"}


async def test_an_unrecorded_numbered_copy_is_dropped_and_its_number_reused(tmp_path, monkeypatch) -> None:
    """027-AC8: a numbered copy the archive cannot record is removed and the item fails, its other URL not
    downloaded and dropped again; the next pass takes the same number instead of the one after."""
    store = ChatStore(tmp_path, C1)
    media = store.media_dir
    media.mkdir()
    (media / "a.pdf").write_bytes(b"1")
    store.save_file(["https://x/1"], "a.pdf", UID)
    real = ChatStore.save_file
    streams: list[str] = []

    class _C:
        async def get(self, u: str, **_: Any) -> TM._Resp:
            return TM._Resp(data={"Name": "a.pdf", "UniqueId": UID2})

        def stream(self, method: str, u: str, **_: Any) -> TM._Resp:
            streams.append(u)
            return TM._Resp(b"fresh")

    def locked(self: ChatStore, *a: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    item = {"kind": "sp_file", "url": SHARE, "fallback": DOC_URL}

    async def once() -> tuple[str | None, list[tuple[str, Exception]]]:
        return await A._sp_file_or_fallback(_C(), item, media, True, lambda h: "SPTOK", None, store)  # type: ignore[arg-type]

    try:
        monkeypatch.setattr(ChatStore, "save_file", locked)
        path, failures = await once()
        assert path is None and [(u, type(e)) for u, e in failures] == [(SHARE, A._Unrecorded)]
        assert len(streams) == 1
        assert sorted(p.name for p in media.iterdir()) == ["a.pdf"]
        monkeypatch.setattr(ChatStore, "save_file", real)
        assert await once() == (str(media / "a (2).pdf"), [])
    finally:
        store.close()


async def test_a_numbered_name_is_locked_against_the_file_that_owns_it(tmp_path) -> None:
    """027-AC8: the second `x.pdf` numbered `x (2).pdf` while a file really named `x (2).pdf` downloads: they
    never share a destination; each URL gets a copy of its own bytes."""
    store = ChatStore(tmp_path, C1)
    media = store.media_dir
    media.mkdir()
    (media / "x.pdf").write_bytes(b"first")
    store.save_file([f"{SP}/x1"], "x.pdf", UID)
    meta = {f"{SP}/x2": ("x.pdf", UID2), f"{SP}/x3": ("x (2).pdf", "0c0c0c0c-0000-4000-8000-000000000003")}

    class _C:
        async def get(self, u: str, **_: Any) -> TM._Resp:
            await asyncio.sleep(0)  # both resolve their names before either downloads
            name, uid = meta[TM._shared(u)]
            return TM._Resp(data={"Name": name, "UniqueId": uid})

        def stream(self, method: str, u: str, **_: Any) -> TM._Resp:
            return _Slow(TM._shared(u).encode())

    try:
        await asyncio.gather(
            *(A._download_sp_file(_C(), url, "SPTOK", media, store) for url in meta)  # type: ignore[arg-type]
        )
        saved = store.saved_files(list(meta))
        assert len(set(saved.values())) == 2
        for url, name in saved.items():
            assert (media / name).read_bytes() == url.encode(), name
        assert not A._dl_locks and not A._dl_refs
    finally:
        store.close()


_REC_URL = f"{SP}/:v:/g/personal/x/rec1?a=1&b=2"
_REC_HTML = _REC_URL.replace("&", "&amp;")  # as the message HTML writes it
_REC = (
    f'<URIObject type="Video.2/CallRecording.1"><item type="onedriveForBusinessVideo" uri="{_REC_HTML}" />'
    '<OriginalName v="rec.mp4" /></URIObject>'
)
_PLAY_ONLY = f'<URIObject><a href="{_REC_HTML}">Play</a><OriginalName v="rec.mp4" /></URIObject>'


@pytest.mark.parametrize("content", [_REC, _PLAY_ONLY])
def test_the_recordings_manifest_finds_a_numbered_recording(tmp_path, content: str) -> None:
    """027-AC8: a recording saved as `rec (2).mp4` (another recording held `rec.mp4`) is the one the manifest
    lists, by its OneDrive item or its Play link; without the store, the original name as before."""
    store = ChatStore(tmp_path, C1)
    media = store.media_dir
    media.mkdir()
    (media / "rec.mp4").write_bytes(b"other")
    (media / "rec (2).mp4").write_bytes(b"this")
    store.save_file([_REC_URL], "rec (2).mp4", UID)
    try:
        assert A.recording_files(content, media, store)["videos"] == ["rec (2).mp4"]
        assert A.recording_files(content, media)["videos"] == ["rec.mp4"]
        store.save_file([_REC_URL], "gone.mp4", UID)  # recorded but no longer on disk: the original name
        assert A.recording_files(content, media, store)["videos"] == ["rec.mp4"]
    finally:
        store.close()


# --- long names, denied counts ---


@pytest.mark.parametrize(
    "name",
    [
        "é" * 200 + ".docx",  # a multibyte stem
        "a" + "é" * 200 + ".docx",  # the cut lands inside a character
        "x." + "é" * 200,  # a suffix too long to keep
        "😀" * 100,  # no suffix, 4-byte characters
        "a" * 246 + ".docx",  # 251 bytes: one past what `.part` leaves
    ],
)
def test_a_long_name_is_cut_and_numbered_within_the_limit(tmp_path, name: str) -> None:
    """027-AC8: a name past 250 bytes is cut on a character boundary, its extension kept when short; its
    numbered forms are cut too, so `name (n).ext.part` can always be created."""
    safe = A._safe_name(name)
    assert len(safe.encode()) == len(safe.encode().decode().encode()) and len(safe.encode()) <= 250
    assert len(safe.encode()) >= 247  # cut, not emptied: at most one multibyte character short
    if name.endswith(".docx"):
        assert safe.endswith(".docx") and safe.startswith(name[0])
    for n in (2, 99999999):
        numbered = A._numbered(safe, n)
        assert numbered.endswith(f" ({n}).docx" if name.endswith(".docx") else f" ({n})")
        (tmp_path / f"{numbered}.part").write_bytes(b"x")


@pytest.mark.parametrize("size", [6, 244, 250])
def test_a_name_that_was_writable_is_kept(tmp_path, size: int) -> None:
    """027-AC8: a name of up to 250 bytes was always written (with `.part`, 255): it is kept whole, so a copy
    saved under it before the cut existed is still found; its numbered form is cut to fit."""
    name = "é" * ((size - 5) // 2) + "a" * ((size - 5) % 2) + ".docx"
    assert len(name.encode()) == size and A._safe_name(name) == name
    numbered = A._numbered(name, 12)
    assert numbered.endswith(" (12).docx") and len(numbered.encode()) <= 250
    (tmp_path / f"{numbered}.part").write_bytes(b"x")


def test_a_long_named_legacy_copy_is_adopted(wired, sharepoint, fetched, tmp_path) -> None:  # noqa: F811
    """027-AC7: a 244-byte name saved before the record is found under that very name and adopted, not
    downloaded again."""
    name = "é" * 118 + "abc.docx"
    assert len(name.encode()) == 244
    sharepoint |= {SHARE: (name, UID), DOC_URL: (name, UID)}
    data = tmp_path / "data"
    assert _archive(data, "--no-media") == 0
    media = TM._media_dir(data)
    media.mkdir(parents=True)
    (media / name).write_bytes(f"{UID} {name}".encode())  # SharePoint's size
    assert _archive(data, "--verify-media") == 0
    assert fetched and not [u for u in fetched if "/$value" in u]
    assert list(_on_disk(data)) == [name]
    assert _saved(data, [SHARE, DOC_URL]) == {SHARE: name, DOC_URL: name}


def test_each_denied_url_is_counted_once(wired, sharepoint, fetched, tmp_path, monkeypatch) -> None:  # noqa: F811
    """027-AC4: an entry whose sharing link is denied counts that link once in media_denied_skipped, though
    its objectUrl downloads it."""
    _rearchive(monkeypatch, [_msg("f1", 1, "<p>x</p>", [DOC])])
    data = tmp_path / "data"
    assert _archive(data, "--no-media") == 0
    store = ChatStore(data, C1)
    store.mark_denied(SHARE, 403, AR._now_iso())
    store.close()
    logs = _Events(monkeypatch, AR)
    assert _archive(data, "--verify-media") == 0
    lines = logs.named("media_denied_skipped")
    assert [line["skipped"] for line in lines] == [1]
    assert _on_disk(data) == {"report.docx": LEGACY}
