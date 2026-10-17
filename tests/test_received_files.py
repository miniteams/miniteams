"""Spec 027 phase 2: `properties.files` entries ride the attachment pipeline (SharePoint faked end to end)."""

import asyncio
import base64
import json
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
    is never recorded as denied; the body link, which has no fallback, denied under the sharing link, does not
    mute the entry on the next sweep."""
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
        assert store.denied_urls(AR._now_iso()) == (set() if non_file else {SHARE})  # the body link's
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
