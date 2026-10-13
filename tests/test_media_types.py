"""Spec 014: `--media` chooses which attachment types are downloaded (network faked end to end)."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_archive import _FakeApi, _wire

from miniteams import archive as AR
from miniteams import archive_live as AL
from miniteams import attachments as A
from miniteams import auth, cli, messages
from miniteams.archive_store import ChatStore, Index, chat_dir_name
from miniteams.config import Settings
from miniteams.directory import Directory

C1 = "19:alice@unq.gbl.spaces"
AMS = "https://eu-prod.asyncgw.teams.microsoft.com/v1/objects"
SP = "https://c-my.sharepoint.com"
SP_TR = f"{SP}/personal/x/_api/v2.1/drives/b/items/01/media/transcripts/t1/content"
IMG = f'<img itemtype="http://schema.skype.com/AMSImage" src="{AMS}/img1/views/imgo">'
PIC = f'<URIObject type="Picture.1" uri="{AMS}/pic1"></URIObject>'
DOC = f'<a itemtype="http://schema.skype.com/HyperLink/Files" href="{SP}/:w:/g/personal/x/doc1">doc</a>'
CLIP = f'<a href="{SP}/:v:/g/personal/x/clip1">clip</a>'
REC = (
    f'<URIObject type="Video.2/CallRecording.1" uri="" url_thumbnail="{AMS}/rec1/views/thumbnail_small">'
    f'<a href="{SP}/:v:/g/personal/x/rec1">Play</a>'
    f'<item type="amsTranscript" uri="{AMS}/tr1/views/transcript" />'
    f'<item type="onedriveForBusinessTranscript" uri="{SP_TR}" />'
    f'<item type="amsVideo" uri="{AMS}/rec1/views/video" />'
    f'<item type="onedriveForBusinessVideo" uri="{SP}/:v:/g/personal/x/rec1" />'
    '<OriginalName v="rec.mp4" /></URIObject>'
)
MESSAGES = [
    ("m1", IMG, "RichText/Html"),
    ("m2", PIC, "RichText/Media_GenericFile"),
    ("m3", DOC, "RichText/Html"),
    ("m4", REC, "RichText/Media_CallRecording"),
    ("m5", CLIP, "RichText/Html"),
    ("m6", IMG.replace("img1", "img2") + DOC.replace("doc1", "doc2"), "RichText/Html"),  # mixed types
]
SP_STEM = f"sp-{hashlib.sha1(SP_TR.encode()).hexdigest()[:16]}.transcript"
IMAGES = {"img1.png", "img1.full.png", "img2.png", "img2.full.png", "pic.png"}
FILES = {"doc.docx", "doc2.docx"}
TRANSCRIPTS = {"tr1.transcript.vtt", f"{SP_STEM}.json", f"{SP_STEM}.vtt"}
VIDEOS = {"rec.mp4", "clip.mp4"}  # the AMS copy is a fallback: the SharePoint one served
VALID = "valid types: images, files, transcripts, videos"


class _Resp:
    def __init__(self, content: bytes = b"", ctype: str = "", data: Any = None, status: int = 200) -> None:
        self.content, self._data, self.status_code = content, data, status
        self.headers = {"content-type": ctype} if ctype else {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(str(self.status_code), request=None, response=self)  # type: ignore[arg-type]

    def json(self) -> Any:
        return self._data

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def aiter_bytes(self, size: int = 65536) -> Any:
        yield self.content


_GRAPH_NAMES = {"doc2": "doc2.docx", "doc1": "doc.docx", "rec1": "rec.mp4", "clip1": "clip.mp4"}


def _shared(url: str) -> str:
    """The sharing URL behind a Graph /shares call, else the URL itself."""
    if not url.startswith(A._GRAPH):
        return url
    share = url.split("/shares/u!")[1].split("/")[0]
    return base64.urlsafe_b64decode(share + "=" * (-len(share) % 4)).decode()


def _serve(url: str, params: dict[str, str] | None) -> _Resp:
    if url.startswith(A._GRAPH):
        name = _GRAPH_NAMES[_shared(url).rsplit("/", 1)[1]]
        if url.endswith("/driveItem"):
            return _Resp(data={"name": name, "file": {}})
        return _Resp(f"bytes of {name}".encode())
    if url == SP_TR:
        fmt = (params or {}).get("format")
        return _Resp(b"[]", "application/json") if fmt == "json" else _Resp(b"WEBVTT", "text/vtt")
    if url.endswith(("/views/imgo", "/views/imgpsh_fullsize")):
        return _Resp(b"PNG " + url.encode(), "image/png")
    if url == f"{AMS}/pic1/views/original/status":
        view = f"{AMS}/pic1/views/imgpsh_fullsize"
        return _Resp(data={"original_filename": "pic.png", "content_state": "ready", "view_location": view})
    if url.endswith("/views/transcript"):
        return _Resp(b"WEBVTT ams", "text/vtt")
    if url.endswith("/views/video"):
        return _Resp(b"MP4 ams", "video/mp4")
    return _Resp(status=404)


class _Client:
    def __init__(self, fetched: list[str]) -> None:
        self.fetched = fetched

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def aclose(self) -> None:
        return None

    async def get(self, url: str, params: dict[str, str] | None = None, **_: Any) -> _Resp:
        self.fetched.append(url)
        return _serve(url, params)

    def stream(self, method: str, url: str, **_: Any) -> _Resp:
        self.fetched.append(url)
        return _serve(url, None)


class _Tokens(AR.StaticToken):
    def sharepoint_token(self, host: str) -> str | None:
        return "SPTOK"

    def graph_token(self) -> str | None:
        return "GTOK"


@pytest.fixture
def fetched(monkeypatch) -> list[str]:
    urls: list[str] = []
    monkeypatch.setattr(A.httpx, "AsyncClient", lambda *a, **k: _Client(urls))
    return urls


@pytest.fixture
def wired(monkeypatch) -> None:
    """The archive command with every network edge faked: auth, history, directory, downloads."""
    monkeypatch.setattr(auth.TokenSource, "acquire", lambda self: {"access_token": "x"})
    monkeypatch.setattr(auth, "_app", lambda *a: None)
    monkeypatch.setattr(AR, "RefreshingToken", lambda settings, source: _Tokens("sk"))
    monkeypatch.setattr(AR, "_INTER_PAGE_DELAY", 0)
    msgs = [
        {"id": mid, "composetime": f"2026-07-01T09:00:0{i}Z", "content": c, "messagetype": t}
        for i, (mid, c, t) in enumerate(MESSAGES)
    ]
    _wire(monkeypatch, _FakeApi({C1: msgs}), [C1])


def _archive(data: Path, *flags: str) -> int:
    return cli.main(["archive", "--thread", C1, "--data-dir", str(data), "--no-avatars", *flags])


def _media_dir(data: Path) -> Path:
    return data / chat_dir_name(C1) / "media"


def _on_disk(data: Path) -> dict[str, bytes]:
    media = _media_dir(data)
    if not media.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in media.iterdir() if p.name != "recordings.json"}


def _stored_ids(data: Path) -> set[str]:
    store = ChatStore(data, C1)
    try:
        return {r[0] for r in store._db.execute("SELECT id FROM messages")}
    finally:
        store.close()


def test_every_kind_maps_to_exactly_one_type() -> None:
    """014-AC1: one type per kind, URIObject picture vs document, sp_file document vs video."""
    assert A.media_type("image", f"{AMS}/x/views/imgo") == "images"
    assert A.media_type("file", f"{AMS}/x", "Picture.1") == "images"
    assert A.media_type("file", f"{AMS}/x", "File.1") == "files"
    assert A.media_type("file", "https://urlp.asm.skype.com/card.png", "SWIFT.1") == "files"
    assert A.media_type("file", f"{AMS}/x/views/thumbnail_small", "Video.2/CallRecording.1") == "files"
    assert A.media_type("sp_file", f"{SP}/:x:/g/personal/x/sheet") == "files"
    assert A.media_type("sp_file", f"{SP}/sites/x/clip.MKV?web=1") == "videos"
    assert A.media_type("sp_file", f"{SP}/:v:/g/personal/x/rec") == "videos"
    assert A.media_type("sp_file", f"{SP}/personal/x/rec", "onedriveForBusinessVideo") == "videos"
    assert A.media_type("video", f"{AMS}/x/views/video") == "videos"
    assert A.media_type("transcript", f"{AMS}/x/views/transcript") == "transcripts"
    assert A.media_type("sp_transcript", SP_TR) == "transcripts"
    content = "".join(c for _, c, _ in MESSAGES)
    every = A.extract(content, "", frozenset(A.MEDIA_TYPES))
    per_type = [A.extract(content, "", frozenset({t})) for t in A.MEDIA_TYPES]
    assert sorted(map(str, every)) == sorted(str(i) for items in per_type for i in items)  # a partition
    assert all(per_type)
    # A OneDrive recording stays a video even behind a non-:v: link that is also posted as a file.
    odv = f"{SP}/personal/x/rec.bin"
    rec = (
        f'<item type="onedriveForBusinessVideo" uri="{odv}" /><a itemtype="{A._FILE_LINK}" href="{odv}">r</a>'
    )
    assert A.extract(rec, "", frozenset({"files"})) == []
    assert A.extract(rec, "", frozenset({"videos"})) == [{"kind": "sp_file", "url": odv}]


def test_media_transcripts_downloads_every_transcript_format_only(wired, fetched, tmp_path) -> None:
    """014-AC2: only the transcript, every rendition (both SharePoint ones, AMS by content type)."""
    data = tmp_path / "data"
    assert _archive(data, "--media", "transcripts") == 0
    assert set(_on_disk(data)) == TRANSCRIPTS
    assert _stored_ids(data) == {m[0] for m in MESSAGES}  # messages complete
    index = Index(data)
    try:
        assert [c["id"] for c in index.chats()] == [C1]
    finally:
        index.close()
    assert not [u for u in fetched if "/views/transcript" not in u and u != SP_TR]


def test_default_set_unchanged_and_videos_shorthand_equals_all(wired, fetched, tmp_path) -> None:
    """014-AC3: no --media = today's default; --videos = --media with all four types."""
    runs = {}
    for name, flags in (
        ("default", ()),
        ("videos", ("--videos",)),
        ("all", ("--media", "images,files,transcripts,videos")),
    ):
        assert _archive(tmp_path / name, *flags) == 0
        runs[name] = _on_disk(tmp_path / name)
    assert set(runs["default"]) == IMAGES | FILES | TRANSCRIPTS
    assert runs["videos"] == runs["all"]
    assert set(runs["all"]) == IMAGES | FILES | TRANSCRIPTS | VIDEOS
    assert {k: v for k, v in runs["all"].items() if k in runs["default"]} == runs["default"]


def test_no_media_downloads_nothing(wired, fetched, tmp_path) -> None:
    """014-AC4: --no-media is an empty selection."""
    data = tmp_path / "data"
    assert _archive(data, "--no-media") == 0
    assert _on_disk(data) == {} and fetched == []
    assert _stored_ids(data) == {m[0] for m in MESSAGES}


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["archive", "--media", "images", "--no-media"], "--media cannot be combined with --no-media"),
        (["archive", "--videos", "--media", "images"], "--media cannot be combined with --videos"),
        (["archive", "--media", "pdf"], VALID),
        (["archive", "--media", ""], VALID),
        (["archive", "--media", "images,images"], VALID),
        (["archive", "--media", "images,"], VALID),
        (["archive", "--live", "--media", "Images"], VALID),
        (["stream", "--media", "pdf"], VALID),
    ],
)
def test_media_usage_errors(argv, error, capsys, monkeypatch) -> None:
    """014-AC4: clashing shorthands and unknown, empty or duplicate names are usage errors."""

    async def never(*a: Any, **k: Any) -> bool:
        raise AssertionError("guard bypassed: no run may start")

    monkeypatch.setattr(AR, "run_archive", never)
    monkeypatch.setattr(AL, "run_live", never)
    monkeypatch.setattr(auth.TokenSource, "acquire", lambda self: {"access_token": "x"})
    monkeypatch.setattr(auth, "_app", lambda *a: None)
    # A bypass would cache loggers on capsys' stream, closed after this test, failing later ones.
    monkeypatch.setattr(cli, "setup_logging", lambda level: None)
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert error in capsys.readouterr().err


def test_live_and_stream_options_reach_the_run(monkeypatch) -> None:
    """014-AC5: `archive --live --media` and `stream --media` hand the selection to the run."""
    monkeypatch.setattr(auth.TokenSource, "acquire", lambda self: {"access_token": "x"})
    monkeypatch.setattr(auth, "_app", lambda *a: None)
    live: list[dict[str, Any]] = []

    async def fake_live(settings: Settings, data_dir: Path, **kw: Any) -> bool:
        live.append(kw)
        return True

    monkeypatch.setattr(AL, "run_live", fake_live)
    cli.main(["archive", "--live", "--media", "transcripts"])
    cli.main(["archive", "--live"])
    cli.main(["archive", "--live", "--no-media"])
    assert [kw["media_types"] for kw in live] == [{"transcripts"}, A.DEFAULT_MEDIA, frozenset()]
    streamed: list[frozenset[str]] = []

    async def fake_forever(settings: Settings, *a: Any, directory: Directory, **k: Any) -> None:
        streamed.append(directory.media)

    from miniteams import stream

    monkeypatch.setattr(stream, "run_forever", fake_forever)
    assert cli.main(["stream", "--media", "images,videos"]) == 0
    assert cli.main(["stream"]) == 0
    assert streamed == [{"images", "videos"}, A.DEFAULT_MEDIA]


@pytest.mark.parametrize(
    ("selection", "content", "expected"),
    [("transcripts", "".join(c for _, c, _ in MESSAGES), TRANSCRIPTS), ("videos", CLIP, {"clip.mp4"})],
)
async def test_live_archive_honours_the_selection(
    selection, content, expected, settings: Settings, tmp_path, fetched
) -> None:
    """014-AC5: a live message downloads its selected types only, including a video-only one."""
    live = AL.LiveArchive(settings, tmp_path / "data", _Tokens("sk"), media_types=frozenset({selection}))
    try:
        resource = {
            "id": "m4",
            "version": "1",
            "composetime": "2026-09-23T10:00:01Z",
            "messagetype": "RichText/Media_CallRecording",
            "content": content,
            "conversationLink": f"https://x/v1/users/ME/conversations/{C1}",
        }
        await live.on_event({"type": "EventMessage", "resourceType": "NewMessage", "resource": resource})
        await asyncio.wait_for(asyncio.gather(*live._media_tasks), 5)
    finally:
        live.close()
    assert set(_on_disk(live.data_dir)) == expected


async def test_stream_honours_the_selection(directory: Directory, fetched, capsys) -> None:
    """014-AC5: the stream prints and downloads selected types only."""
    directory.media = frozenset({"transcripts"})
    resource = {"messagetype": "RichText/Media_CallRecording", "content": REC, "from": "8:x", "to": C1}
    resource["imdisplayname"] = "X"

    async def label(thread_id: str) -> str:
        return "chat"

    directory.label = label  # type: ignore[method-assign]
    await messages.print_resource(resource, directory)
    line = capsys.readouterr().out
    assert "[transcript: " in line and "[transcript(sp): " in line
    assert "image" not in line and "[file" not in line and "video" not in line
    assert fetched == [f"{AMS}/tr1/views/transcript"]  # the SharePoint one needs a bearer the stream lacks
    assert {p.name for p in directory.settings.media_dir.iterdir()} == {"tr1.transcript.vtt"}
    record = await messages.resource_to_record(resource | {"content": REC + IMG + DOC}, directory)
    assert {i["kind"] for i in record["attachments"]} == {"transcript", "sp_transcript"}  # --jsonl agrees


@pytest.mark.parametrize("flag", ["--assets-only", "--verify-media"])
def test_verify_media_fetches_selected_types_only(flag, wired, fetched, tmp_path) -> None:
    """014-AC6: missing images are fetched; missing other types stay missing and unreported."""
    data = tmp_path / "data"
    assert _archive(data, "--no-media") == 0
    if flag == "--assets-only":
        assert cli.main(["archive", "--data-dir", str(data), "--assets-only", "--media", "images"]) == 0
    else:
        assert _archive(data, "--verify-media", "--media", "images") == 0
    assert set(_on_disk(data)) == IMAGES
    assert all("/views/imgo" in u or "imgpsh_fullsize" in u or "/pic1/" in u for u in fetched)
    store = ChatStore(data, C1)
    try:
        assert store.denied_urls(AR._now_iso()) == set()  # nothing deselected recorded as a failure
    finally:
        store.close()


def test_deselected_files_on_disk_are_untouched(wired, fetched, tmp_path) -> None:
    """014-AC7: an excluded type already archived keeps its bytes, mtime and manifest entry."""
    data = tmp_path / "data"
    assert _archive(data, "--videos") == 0
    media = _media_dir(data)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in media.iterdir()}
    manifest = json.loads((media / "recordings.json").read_text())
    fetched.clear()
    for selection in ("images", "transcripts", "files"):
        assert _archive(data, "--media", selection) == 0
    assert _archive(data, "--no-media") == 0
    assert not [u for u in map(_shared, fetched) if "/views/video" in u or ":v:" in u]  # no video asked
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in media.iterdir()}
    assert {k: v for k, v in after.items() if k != "recordings.json"} == {
        k: v for k, v in before.items() if k != "recordings.json"
    }
    assert json.loads((media / "recordings.json").read_text()) == manifest
    assert manifest[0]["videos"] and manifest[0]["transcripts"]
