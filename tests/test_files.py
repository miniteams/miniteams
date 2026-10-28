"""Send any file (spec 025 phase 1): OneDrive discovery, upload, share, the card (SharePoint mocked)."""

import argparse
import base64
import io
import json
import logging
import re
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
import structlog
from test_images import png

from miniteams import cli, files, send
from miniteams.config import Settings

HOST = "contoso-my.sharepoint.com"
SITE = f"https://{HOST}/personal/me_contoso_com"
DRIVE = f"{SITE}/_api/v2.0/drive"
ME, BOB, CAROL = "8:orgid:aaaaaaaa-0000-0000-0000-000000000001", "b0b", "ca401"
THREAD = "19:chat@thread.v2"
TID = "0f1e2d3c-aaaa-bbbb-cccc-000000000123"


def _jwt(**claims: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{body}.s"


AAD = _jwt(oid="aaaaaaaa-0000-0000-0000-000000000001", tid=TID)


class FakeSharePoint:
    """Teams' middle tier, the chat roster, a OneDrive and the chat service, in memory."""

    def __init__(self, folders: dict[str, str] | None = None) -> None:
        self.folders = {"Microsoft Teams Chat Files": "2026-10-01T00:00:00Z"} if folders is None else folders
        self.items: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.posted: list[dict[str, Any]] = []
        self.fail: dict[str, int] = {}  # action → status
        self.domain = "contoso.onmicrosoft.com"
        self.tid = TID
        self.personal = SITE + "/"
        self.hook: Any = lambda req: None  # a test's own answer, before the fake's

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def _error(self, status: int) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": "x", "message": f"refused {status}"}})

    def handle(self, req: httpx.Request) -> httpx.Response:  # noqa: C901 — one fake server
        self.requests.append(req)
        if (own := self.hook(req)) is not None:
            return own
        url, path = str(req.url), unquote(req.url.path)
        if url.startswith(Settings.model_fields["tenants_url"].default):
            return httpx.Response(
                200,
                json=[  # a guest tenant first: the token's own one is the one to take
                    {
                        "tenantId": "99999999-0000-0000-0000-000000000000",
                        "tenantPrimaryDomainName": "guest.onmicrosoft.com",
                    },
                    {"tenantId": self.tid, "tenantPrimaryDomainName": self.domain},
                ],
            )
        if "/v1/threads/" in path:
            members = [{"id": ME}, {"id": f"8:orgid:{BOB}"}, {"id": f"8:orgid:{CAROL}"}, {"id": "28:bot"}]
            return httpx.Response(200, json={"members": members})
        if path.endswith("/messages") and req.method == "POST":
            if "post" in self.fail:
                return self._error(self.fail["post"])
            self.posted.append(json.loads(req.content))
            return httpx.Response(201, json={})
        if path.endswith("/GetMyProperties"):
            return httpx.Response(200, json={"PersonalUrl": self.personal})
        if url.startswith(f"{SITE}/_upload/"):
            assert "authorization" not in req.headers  # the session URL is pre-authorised
            if req.method == "DELETE":
                del self.sessions[req.url.path]
                return httpx.Response(204)
            session = self.sessions[req.url.path]
            start, end, total = map(
                int, re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", req.headers["content-range"]).groups()
            )  # type: ignore[union-attr]
            assert start == len(session["data"]) and end - start + 1 == len(req.content)
            session["data"] += req.content
            if len(session["data"]) < total:
                return httpx.Response(202, json={"nextExpectedRanges": [f"{len(session['data'])}-"]})
            return httpx.Response(201, json=self._put(session["folder"], session["name"], session["data"]))
        assert req.headers["authorization"] == "Bearer sp-token"
        m = re.fullmatch(r"/personal/me_contoso_com/_api/v2\.0/drive/root:/([^/]+)", path)
        if m and req.method == "GET":
            when = self.folders.get(m[1])
            return (
                httpx.Response(200, json={"name": m[1], "folder": {}, "lastModifiedDateTime": when})
                if when
                else httpx.Response(404)
            )
        if path.endswith("/drive/root/children"):
            self.folders[json.loads(req.content)["name"]] = "now"
            return httpx.Response(201, json={})
        m = re.fullmatch(r".*/drive/root:/([^/]+)/([^/]+):/(content|createUploadSession)", path)
        if m:
            assert req.url.params.get("@microsoft.graph.conflictBehavior") == "rename" or m[3] != "content"
            if "upload" in self.fail:
                return self._error(self.fail["upload"])
            if m[3] == "content":
                return httpx.Response(201, json=self._put(m[1], m[2], req.content))
            key = f"/personal/me_contoso_com/_upload/{len(self.sessions)}"
            self.sessions[key] = {"folder": m[1], "name": m[2], "data": b""}
            return httpx.Response(200, json={"uploadUrl": f"https://{HOST}{key}"})
        m = re.fullmatch(r".*/drive/items/([^/]+)(/createLink)?", path)
        if m and m[2]:
            if "share" in self.fail:
                return self._error(self.fail["share"])
            self.items[m[1]]["shared"] = json.loads(req.content)
            return httpx.Response(
                200,
                json={
                    "id": "perm-1",
                    "link": {"webUrl": f"https://{HOST}/:t:/g/personal/me/TOKEN?email=b%40c"},
                },
            )
        if m and req.method == "DELETE":
            del self.items[m[1]]
            return httpx.Response(204)
        if m:
            item = self.items[m[1]]
            return httpx.Response(
                200,
                json={
                    "id": m[1],
                    "name": item["name"],
                    "webUrl": item["webUrl"],
                    "sharepointIds": {"listItemUniqueId": f"guid-{m[1]}"},
                },
            )
        raise AssertionError(f"unexpected {req.method} {url}")

    def _put(self, folder: str, name: str, data: bytes) -> dict[str, Any]:
        taken = {i["name"] for i in self.items.values()}
        stem, dot, ext = name.rpartition(".")
        n = 1
        while name in taken:  # OneDrive's own rename: "x 1.txt"
            name, n = f"{stem} {n}{dot}{ext}", n + 1
        item_id = f"item{len(self.items) + len(self.posted)}"
        self.items[item_id] = {
            "name": name,
            "data": data,
            "folder": folder,
            "webUrl": f"{SITE}/Documents/{folder}/{name}",
        }
        return {"id": item_id, "name": name}


@pytest.fixture(autouse=True)
def _all_logs(_quiet_logs: None) -> None:
    # conftest keeps WARNING and up: the info events asserted on here would never reach capture_logs.
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))


@pytest.fixture
def sp(monkeypatch: pytest.MonkeyPatch) -> FakeSharePoint:
    fake = FakeSharePoint()
    client = fake.client()
    monkeypatch.setattr(files, "_CLIENT", client)
    monkeypatch.setattr(send, "_CLIENT", client)
    return fake


def _drive(settings: Settings, sp: FakeSharePoint) -> files.OneDrive:
    return files.onedrive(settings, AAD, lambda host: "sp-token" if host == HOST else None)


def _send(
    settings: Settings, sp: FakeSharePoint, batch: list[tuple[str, bytes]], text: str = "hi"
) -> dict[str, Any]:
    return files.send_files(settings, "sk", _drive(settings, sp), ME, THREAD, text, _open(batch), "Me")


def _open(batch: list[tuple[str, bytes]]) -> list[tuple[str, Any, int]]:
    return [(name, io.BytesIO(data), len(data)) for name, data in batch]


def test_onedrive_is_the_personal_site_sharepoint_names(settings: Settings, sp: FakeSharePoint) -> None:
    drive = _drive(settings, sp)
    assert drive == files.OneDrive(SITE, "sp-token")
    assert drive.drive == DRIVE


@pytest.mark.parametrize(
    "domain",
    [
        "straße.onmicrosoft.com",
        "contoso.com",
        "contoso.onmicrosoft.com.evil.example",
        "con/toso.onmicrosoft.com",
        "",
    ],
)
def test_onedrive_refuses_a_domain_that_names_no_tenant_host(
    settings: Settings, sp: FakeSharePoint, domain: str
) -> None:
    sp.domain = domain
    with pytest.raises(RuntimeError, match="no OneDrive host"):
        _drive(settings, sp)


@pytest.mark.parametrize("personal", ["https://evil.example/personal/me/", f"http://{HOST}/personal/me/", ""])
def test_onedrive_refuses_a_personal_site_off_its_host(
    settings: Settings, sp: FakeSharePoint, personal: str
) -> None:
    sp.personal = personal
    with pytest.raises(RuntimeError, match="no personal site"):
        _drive(settings, sp)


@pytest.mark.parametrize("configured", [TID.upper(), "contoso.com", TID])
@pytest.mark.parametrize("listed", [TID, TID.upper()])
def test_onedrive_follows_the_tokens_tenant_whatever_the_setting(
    settings: Settings, sp: FakeSharePoint, configured: str, listed: str
) -> None:
    settings.tenant_id, sp.tid = configured, listed  # .env takes a GUID in any case, or a verified domain
    assert _drive(settings, sp).site == SITE


def test_onedrive_matches_an_uppercase_tid_in_the_token(settings: Settings, sp: FakeSharePoint) -> None:
    assert files.onedrive(settings, _jwt(tid=TID.upper()), lambda host: "sp-token").site == SITE


def test_a_token_without_tid_never_matches_an_entry_without_tenant_id(
    settings: Settings, sp: FakeSharePoint
) -> None:
    sp.tid = ""
    with pytest.raises(RuntimeError, match="no OneDrive host"):
        files.onedrive(settings, _jwt(oid="x"), lambda host: "sp-token")


@pytest.mark.parametrize("token", [_jwt(tid="11111111-0000-0000-0000-000000000000"), _jwt(oid="x"), "opaque"])
def test_onedrive_refuses_a_token_whose_tenant_is_not_listed(
    settings: Settings, sp: FakeSharePoint, token: str
) -> None:
    with pytest.raises(RuntimeError, match="no OneDrive host for primary domain ''"):
        files.onedrive(settings, token, lambda host: "sp-token")


def test_onedrive_without_a_sharepoint_token_says_so(settings: Settings, sp: FakeSharePoint) -> None:
    with pytest.raises(RuntimeError, match="no SharePoint token"):
        files.onedrive(settings, AAD, lambda host: None)


@pytest.mark.parametrize(
    ("folders", "expected"),
    [
        (
            {"Fichiers de conversation Microsoft Teams": "2024-01-01T00:00:00Z"},
            "Fichiers de conversation Microsoft Teams",
        ),
        (
            {
                "Microsoft Teams Chat Files": "2026-10-08T00:00:00Z",
                "Fichiers de conversation Microsoft Teams": "2025-09-05T00:00:00Z",
            },
            "Microsoft Teams Chat Files",
        ),
        (
            {
                "Microsoft Teams Chat Files": "2024-01-01T00:00:00Z",
                "Fichiers de conversation Microsoft Teams": "2025-09-05T00:00:00Z",
            },
            "Fichiers de conversation Microsoft Teams",
        ),
    ],
)
def test_chat_folder_is_the_most_recently_used_localized_one(
    settings: Settings, sp: FakeSharePoint, folders: dict[str, str], expected: str
) -> None:
    sp.folders = folders
    assert files.chat_folder(_drive(settings, sp)) == expected
    assert not [r for r in sp.requests if r.method == "POST"]  # nothing created


def test_chat_folder_is_created_only_when_none_exists(settings: Settings, sp: FakeSharePoint) -> None:
    sp.folders = {}
    with structlog.testing.capture_logs() as logs:
        assert files.chat_folder(_drive(settings, sp)) == "Microsoft Teams Chat Files"
    assert [e["name"] for e in logs if e["event"] == "chat_folder_created"] == ["Microsoft Teams Chat Files"]
    assert list(sp.folders) == ["Microsoft Teams Chat Files"]


def test_send_posts_one_message_whose_cards_are_teams_shape(settings: Settings, sp: FakeSharePoint) -> None:
    _send(settings, sp, [("a.pdf", b"%PDF"), ("notes.TXT", b"x")])
    (body,) = sp.posted
    assert body["content"] == "hi"
    assert isinstance(body["properties"]["files"], str)  # Teams sends it as a JSON string
    first, second = json.loads(body["properties"]["files"])
    url = f"{SITE}/Documents/Microsoft Teams Chat Files/a.pdf"
    assert first["@type"] == "http://schema.skype.com/File"
    assert (first["fileName"], first["title"], first["type"], first["fileType"]) == ("a.pdf",) * 2 + (
        "pdf",
    ) * 2
    assert first["id"] == first["itemid"] == first["sharepointIds"]["listItemUniqueId"] == "guid-item0"
    assert first["objectUrl"] == first["fileInfo"]["fileUrl"] == url
    assert first["baseUrl"] == first["fileInfo"]["siteUrl"] == f"{SITE}/"
    assert (
        first["fileInfo"]["shareUrl"] == f"https://{HOST}/:t:/g/personal/me/TOKEN"
    )  # one member's email dropped
    assert first["fileInfo"]["shareId"] == "perm-1"
    assert (first["state"], first["fileChicletState"]) == (
        "active",
        {"serviceName": "p2p", "state": "active"},
    )
    assert first["permissionScope"] == "SpecificPeople"
    assert second["type"] == "txt"


def test_send_shares_an_edit_link_with_the_chat_people_but_me(settings: Settings, sp: FakeSharePoint) -> None:
    _send(settings, sp, [("a.pdf", b"%PDF")])
    (item,) = sp.items.values()
    assert item["shared"] == {
        "type": "edit",
        "scope": "users",
        "recipients": [{"objectId": BOB}, {"objectId": CAROL}],
    }


def test_send_to_notes_shares_with_nobody(settings: Settings, sp: FakeSharePoint) -> None:
    files.send_files(
        settings,
        "sk",
        _drive(settings, sp),
        ME,
        send.NOTES_THREAD,
        "",
        _open([("a.pdf", b"1")]),
        "Me",
    )
    assert not [r for r in sp.requests if "/threads/" in r.url.path or r.url.path.endswith("/createLink")]
    (entry,) = json.loads(sp.posted[0]["properties"]["files"])
    assert entry["fileInfo"]["shareUrl"] == ""


def test_a_name_clash_takes_onedrives_new_name(settings: Settings, sp: FakeSharePoint) -> None:
    _send(settings, sp, [("a.txt", b"1"), ("a.txt", b"2")])
    names = [e["fileName"] for e in json.loads(sp.posted[0]["properties"]["files"])]
    assert names == ["a.txt", "a 1.txt"]
    assert sorted(i["data"] for i in sp.items.values()) == [b"1", b"2"]


def test_a_large_file_goes_up_in_chunks_and_arrives_whole(settings: Settings, sp: FakeSharePoint) -> None:
    data = bytes(range(256)) * ((files.SIMPLE_MAX + 2 * files.CHUNK) // 256 + 7)
    reads: list[int] = []

    class Spy(io.BytesIO):
        def read(self, n: int | None = -1) -> bytes:
            reads.append(-1 if n is None else n)
            return super().read(n)

    drive = _drive(settings, sp)
    files.send_files(settings, "sk", drive, ME, THREAD, "", [("big.bin", Spy(data), len(data))], "Me")
    assert reads and max(reads) == files.CHUNK and min(reads) > 0  # never the whole file at once
    (item,) = sp.items.values()
    assert item["data"] == data
    chunks = [r for r in sp.requests if "/_upload/" in r.url.path]
    assert len(chunks) == -(-len(data) // files.CHUNK) > 1
    assert not [r for r in sp.requests if r.url.path.endswith(":/content")]


def test_a_file_at_the_simple_limit_goes_in_one_request(settings: Settings, sp: FakeSharePoint) -> None:
    _send(settings, sp, [("edge.bin", b"x" * files.SIMPLE_MAX)])
    assert not sp.sessions


@pytest.mark.parametrize(
    "upload_url",
    [
        "https://evil.example/_upload/0",
        f"http://{HOST}/personal/me_contoso_com/_upload/0",
        f"https://{HOST}:8443/personal/me_contoso_com/_upload/0",
        f"https://user@{HOST}/personal/me_contoso_com/_upload/0",
        "",
    ],
)
def test_an_upload_session_off_the_onedrive_host_gets_no_bytes(
    settings: Settings, sp: FakeSharePoint, upload_url: str
) -> None:
    def elsewhere(req: httpx.Request) -> httpx.Response | None:
        if req.url.path.endswith("/createUploadSession"):
            return httpx.Response(200, json={"uploadUrl": upload_url})
        return None

    sp.hook = elsewhere
    big = files.SIMPLE_MAX + 1
    with pytest.raises(RuntimeError, match="not on the OneDrive host"):
        files.upload(_drive(settings, sp), "f", "big.bin", io.BytesIO(b"x" * big), big)
    assert not [r for r in sp.requests if r.method in ("PUT", "DELETE")]


def _chunk_hook(answer: Any) -> Any:
    """Answer the second chunk PUT with `answer` (a response, or an exception to raise)."""
    puts: list[httpx.Request] = []

    def hook(req: httpx.Request) -> httpx.Response | None:
        if "/_upload/" in req.url.path and req.method == "PUT":
            puts.append(req)
            if len(puts) == 2:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return None

    return hook


def _big() -> bytes:
    return b"x" * (files.SIMPLE_MAX + files.CHUNK)


def test_a_failed_chunk_cancels_the_session_and_posts_nothing(settings: Settings, sp: FakeSharePoint) -> None:
    sp.hook = _chunk_hook(httpx.Response(500, json={"error": {"message": "chunk lost"}}))
    with pytest.raises(httpx.HTTPStatusError, match="upload chunk failed: HTTP 500 chunk lost"):
        _send(settings, sp, [("big.bin", _big())])
    assert [r.method for r in sp.requests if "/_upload/" in r.url.path][-1] == "DELETE"
    assert sp.sessions == {} and sp.items == {} and sp.posted == []


def test_a_failed_session_cancel_does_not_hide_the_real_failure(
    settings: Settings, sp: FakeSharePoint
) -> None:
    chunk = _chunk_hook(httpx.Response(500, json={"error": {"message": "chunk lost"}}))

    def hook(req: httpx.Request) -> httpx.Response | None:
        if "/_upload/" in req.url.path and req.method == "DELETE":
            raise httpx.ConnectError("gone", request=req)
        return chunk(req)  # type: ignore[no-any-return]

    sp.hook = hook
    with pytest.raises(httpx.HTTPStatusError, match="chunk lost"):
        _send(settings, sp, [("big.bin", _big())])


def test_a_session_that_ends_without_an_item_is_a_failure(settings: Settings, sp: FakeSharePoint) -> None:
    sp.hook = lambda req: (
        httpx.Response(202, json={}) if "/_upload/" in req.url.path and req.method == "PUT" else None
    )
    with pytest.raises(RuntimeError, match="upload session ended without an item"):
        _send(settings, sp, [("big.bin", _big())])
    assert sp.sessions == {} and sp.posted == []


@pytest.mark.parametrize("size", [3, files.SIMPLE_MAX + files.CHUNK])
def test_a_file_shorter_than_its_size_is_refused(settings: Settings, sp: FakeSharePoint, size: int) -> None:
    with pytest.raises(RuntimeError, match="file changed while uploading"):
        files.send_files(
            settings, "sk", _drive(settings, sp), ME, THREAD, "", [("a", io.BytesIO(b"12"), size)], "Me"
        )
    assert sp.items == {} and sp.posted == []


@pytest.mark.parametrize(("step", "status"), [("share", 400), ("post", 400), ("post", 499), ("share", 503)])
def test_a_failure_posts_nothing_and_deletes_every_upload(
    settings: Settings, sp: FakeSharePoint, step: str, status: int
) -> None:
    sp.fail[step] = status
    with pytest.raises(
        httpx.HTTPStatusError, match=f"refused {status}" if step == "share" else "send failed"
    ):
        _send(settings, sp, [("a.pdf", b"1"), ("b.pdf", b"2")])
    assert sp.items == {}
    assert sp.posted == []


def test_an_upload_failure_after_a_first_file_deletes_the_first(
    settings: Settings, sp: FakeSharePoint
) -> None:
    seen: list[httpx.Request] = []

    def second_fails(req: httpx.Request) -> httpx.Response | None:
        if req.url.path.endswith(":/content"):
            seen.append(req)
            if len(seen) == 2:
                return httpx.Response(507, json={"error": {"message": "quota exceeded"}})
        return None

    sp.hook = second_fails
    with pytest.raises(httpx.HTTPStatusError, match="upload failed: HTTP 507 quota exceeded"):
        _send(settings, sp, [("a", b"1"), ("b", b"2")])
    assert sp.items == {} and sp.posted == []


@pytest.mark.parametrize(
    "answer",
    [httpx.ReadTimeout("slow"), httpx.Response(504), httpx.Response(500)],
    ids=["timeout", "504", "500"],
)
def test_a_post_with_an_unknown_outcome_keeps_the_files(
    settings: Settings, sp: FakeSharePoint, answer: Any
) -> None:
    def hook(req: httpx.Request) -> httpx.Response | None:
        if req.url.path.endswith("/messages"):
            if isinstance(answer, Exception):
                raise answer
            return answer
        return None

    sp.hook = hook
    with structlog.testing.capture_logs() as logs, pytest.raises(httpx.HTTPError):
        _send(settings, sp, [("a.pdf", b"1"), ("b.pdf", b"2")])
    assert sorted(sp.items) == ["item0", "item1"]  # a message that landed keeps working cards
    (kept,) = (e for e in logs if e["event"] == "files_kept_post_unknown")
    assert (kept["log_level"], kept["items"], kept["thread"]) == ("warning", ["item0", "item1"], THREAD)


@pytest.mark.parametrize(
    ("content", "size"),
    [
        (b"/proc says 0", 0),  # a size from fstat that is plain wrong
        (b"12345", 3),  # grew after the stat: simple upload
        (b"x" * (files.SIMPLE_MAX + files.CHUNK + 5), files.SIMPLE_MAX + files.CHUNK),  # grew: chunked
        (b"x" * (2 * files.CHUNK + files.SIMPLE_MAX + 1), files.SIMPLE_MAX + 1),  # several chunks behind
        (b"x" * (3 * files.CHUNK + 1), 3 * files.CHUNK),  # last chunk exactly full
    ],
)
def test_a_file_longer_than_its_size_is_refused(
    settings: Settings, sp: FakeSharePoint, content: bytes, size: int
) -> None:
    with pytest.raises(RuntimeError, match="file changed while uploading"):
        files.send_files(
            settings, "sk", _drive(settings, sp), ME, THREAD, "", [("a", io.BytesIO(content), size)], "Me"
        )
    assert sp.items == {} and sp.posted == [] and sp.sessions == {}


def test_a_file_that_grows_during_the_upload_is_refused(settings: Settings, sp: FakeSharePoint) -> None:
    size = files.SIMPLE_MAX + files.CHUNK

    class Growing(io.BytesIO):
        def read(self, n: int | None = -1) -> bytes:
            data = super().read(n)
            if self.tell() >= size:  # a writer appends once the uploader reached the stat size
                self.write(b"more")
                self.seek(size)
            return data

    with pytest.raises(RuntimeError, match="file changed while uploading"):
        files.send_files(
            settings, "sk", _drive(settings, sp), ME, THREAD, "", [("a", Growing(b"x" * size), size)], "Me"
        )
    assert sp.items == {} and sp.sessions == {}


@pytest.mark.parametrize(
    ("size", "failing_put", "unknown", "error"),
    [
        (10, 1, True, httpx.ReadTimeout),
        (10, 1, True, httpx.RemoteProtocolError),  # connection dropped before the answer
        (files.SIMPLE_MAX + files.CHUNK, 4, True, httpx.ReadTimeout),
        (files.SIMPLE_MAX + files.CHUNK, 2, False, httpx.ReadTimeout),
    ],
    ids=["simple", "simple-dropped", "last-chunk", "earlier-chunk"],
)
def test_a_transport_failure_on_the_storing_request_is_logged(
    settings: Settings, sp: FakeSharePoint, size: int, failing_put: int, unknown: bool, error: Any
) -> None:
    puts: list[httpx.Request] = []

    def hook(req: httpx.Request) -> httpx.Response | None:
        if req.method == "PUT":
            puts.append(req)
            if len(puts) == failing_put:
                raise error("lost", request=req)
        return None

    sp.hook = hook
    with structlog.testing.capture_logs() as logs, pytest.raises(error):
        _send(settings, sp, [("r é.bin", b"x" * size)])
    warned = [
        (e["log_level"], e["folder"], e["name"]) for e in logs if e["event"] == "upload_outcome_unknown"
    ]
    assert warned == ([("warning", "Microsoft Teams Chat Files", "r é.bin")] if unknown else [])
    assert sp.posted == []


@pytest.mark.parametrize(
    ("size", "failing_put", "status", "unknown"),
    [
        (10, 1, 504, True),
        (10, 1, 500, True),
        (10, 1, 499, False),
        (10, 1, 409, False),
        (files.SIMPLE_MAX + files.CHUNK, 4, 504, True),
        (files.SIMPLE_MAX + files.CHUNK, 4, 400, False),
        (files.SIMPLE_MAX + files.CHUNK, 2, 504, False),
    ],
    ids=[
        "simple-504",
        "simple-500",
        "simple-499",
        "simple-409",
        "last-chunk-504",
        "last-chunk-400",
        "earlier-chunk-504",
    ],
)
def test_a_5xx_on_the_storing_request_is_logged(
    settings: Settings, sp: FakeSharePoint, size: int, failing_put: int, status: int, unknown: bool
) -> None:
    puts: list[httpx.Request] = []

    def hook(req: httpx.Request) -> httpx.Response | None:
        if req.method == "PUT":
            puts.append(req)
            if len(puts) == failing_put:
                return httpx.Response(status, json={"error": {"message": "gateway"}})
        return None

    sp.hook = hook
    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(httpx.HTTPStatusError, match=f"HTTP {status}"),
    ):
        _send(settings, sp, [("r.bin", b"x" * size)])
    warned = [(e["folder"], e["name"]) for e in logs if e["event"] == "upload_outcome_unknown"]
    assert warned == ([("Microsoft Teams Chat Files", "r.bin")] if unknown else [])
    assert sp.posted == []


@pytest.mark.parametrize(
    ("size", "failing_put"), [(10, 1), (files.SIMPLE_MAX + files.CHUNK, 4)], ids=["simple", "last-chunk"]
)
def test_an_unreadable_2xx_on_the_storing_request_is_logged(
    settings: Settings, sp: FakeSharePoint, size: int, failing_put: int
) -> None:
    puts: list[httpx.Request] = []

    def hook(req: httpx.Request) -> httpx.Response | None:
        if req.method == "PUT":
            puts.append(req)
            if len(puts) == failing_put:
                return httpx.Response(201, content=b"<html>stored, maybe</html>")
        return None

    sp.hook = hook
    with structlog.testing.capture_logs() as logs, pytest.raises(ValueError):
        _send(settings, sp, [("r.bin", b"x" * size)])
    warned = [(e["folder"], e["name"]) for e in logs if e["event"] == "upload_outcome_unknown"]
    assert warned == [("Microsoft Teams Chat Files", "r.bin")]
    assert sp.posted == []


@pytest.mark.parametrize("body", [b"", b'{"name": "r.bin"}', b'["id"]'], ids=["empty", "no-id", "not-a-dict"])
def test_a_simple_upload_answered_without_an_item_is_logged(
    settings: Settings, sp: FakeSharePoint, body: bytes
) -> None:
    sp.hook = lambda req: httpx.Response(201, content=body) if req.method == "PUT" else None
    with structlog.testing.capture_logs() as logs, pytest.raises(ValueError, match="upload returned no item"):
        _send(settings, sp, [("r.bin", b"x")])
    warned = [(e["folder"], e["name"]) for e in logs if e["event"] == "upload_outcome_unknown"]
    assert warned == [("Microsoft Teams Chat Files", "r.bin")]
    assert sp.posted == []


def test_a_post_rejected_in_a_2xx_envelope_deletes_the_files(settings: Settings, sp: FakeSharePoint) -> None:
    def hook(req: httpx.Request) -> httpx.Response | None:
        if req.url.path.endswith("/messages"):
            return httpx.Response(201, json={"errorCode": "Forbidden", "message": "no"})
        return None

    sp.hook = hook
    with pytest.raises(RuntimeError, match="send rejected: Forbidden"):
        _send(settings, sp, [("a.pdf", b"1")])
    assert sp.items == {}


def test_a_failed_orphan_delete_is_logged_and_the_real_failure_raised(
    settings: Settings, sp: FakeSharePoint
) -> None:
    sp.fail["share"] = 403

    def hook(req: httpx.Request) -> httpx.Response | None:
        if req.method == "DELETE" and "/drive/items/" in req.url.path:
            return httpx.Response(503, json={"error": {"message": "busy"}})
        return None

    sp.hook = hook
    with structlog.testing.capture_logs() as logs, pytest.raises(httpx.HTTPStatusError, match="share failed"):
        _send(settings, sp, [("a.pdf", b"1")])
    (orphan,) = (e for e in logs if e["event"] == "orphan_delete_failed")
    assert orphan["item"] == "item0" and "busy" in orphan["error"]


def test_a_folder_created_meanwhile_is_used_without_claiming_it(
    settings: Settings, sp: FakeSharePoint
) -> None:
    sp.folders = {}
    sp.hook = lambda req: httpx.Response(409) if req.url.path.endswith("/root/children") else None
    with structlog.testing.capture_logs() as logs:
        assert files.chat_folder(_drive(settings, sp)) == "Microsoft Teams Chat Files"
    assert not [e for e in logs if e["event"] == "chat_folder_created"]


def test_a_folder_create_failure_is_reported(settings: Settings, sp: FakeSharePoint) -> None:
    sp.folders = {}
    sp.hook = lambda req: (
        httpx.Response(403, json={"error": {"message": "denied"}})
        if req.url.path.endswith("/root/children")
        else None
    )
    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(httpx.HTTPStatusError, match="chat folder create failed: HTTP 403 denied"),
    ):
        files.chat_folder(_drive(settings, sp))
    assert not [e for e in logs if e["event"] == "chat_folder_created"]


def test_limits(settings: Settings) -> None:
    files.check_files([("a", files.MAX_FILE_BYTES)] * files.MAX_FILES)
    with pytest.raises(ValueError, match="11 files: at most 10"):
        files.check_files([("a", 1)] * 11)
    with pytest.raises(ValueError, match="big.iso: over 100 MiB"):
        files.check_files([("a", 1), ("big.iso", files.MAX_FILE_BYTES + 1)])
    files.check_files([("rapport final (v2).pdf", 1), ("été.txt", 1), (".env", 1), ("a.b.c", 1)])


@pytest.mark.parametrize(
    "bad",
    ["", ".", "..", "../x", "a/b", " a.txt", "a.txt ", "a\tb", "a\nb", "a\x00b", "a\x1fb", "a\x7fb"]
    + [f"a{c}b" for c in '"*:<>?\\|'],
)
def test_a_name_onedrive_refuses_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="not a file name OneDrive takes"):
        files.check_files([(bad, 1)])


def test_over_the_limits_nothing_is_requested(settings: Settings, sp: FakeSharePoint) -> None:
    with pytest.raises(ValueError):
        _send(settings, sp, [("a", b"1")] * 11)
    assert all("/v1/threads/" not in r.url.path and r.method == "GET" for r in sp.requests)  # discovery only


def test_the_whole_send_never_talks_to_graph(settings: Settings, sp: FakeSharePoint) -> None:
    _send(settings, sp, [("a.pdf", b"1"), ("big.bin", b"x" * (files.SIMPLE_MAX + 1))])
    hosts = {r.url.host for r in sp.requests}
    assert hosts == {"teams.microsoft.com", HOST, settings.contacts_host}
    assert not [r for r in sp.requests if "graph.microsoft" in str(r.url)]


def _args(tmp_path: Any, *attach: str, image: list[str] | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        text="t", file=None, thread=THREAD, html=False, image=image, attach=list(attach)
    )


def test_cli_refuses_too_many_or_too_big_files_before_auth(settings: Settings, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: pytest.fail("no auth expected"))
    small = tmp_path / "a.txt"
    small.write_bytes(b"1")
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 11)
    odd = tmp_path / "a:b.txt"
    odd.write_bytes(b"1")
    monkeypatch.setattr(files, "MAX_FILE_BYTES", 10)
    cases = [
        (_args(tmp_path, *[str(small)] * 11), "11 files: at most 10"),
        (_args(tmp_path, str(big)), "big.bin: over 0 MiB"),
        (_args(tmp_path, str(tmp_path / "missing")), "missing: No such file"),
        (_args(tmp_path, str(tmp_path)), "not a regular file"),
        (_args(tmp_path, str(odd)), "not a file name OneDrive takes"),
    ]
    real_open = cli._open_regular
    # A stub, not capture_logs: once cli.main() ran setup_logging, cli.log is cached past its reach.
    logs: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(cli, "log", SimpleNamespace(error=lambda event, **kw: logs.append((event, kw))))
    for args, reason in cases:
        logs.clear()
        # Too many files are refused before a single one is opened.
        too_many = len(args.attach) > files.MAX_FILES
        monkeypatch.setattr(
            cli, "_open_regular", (lambda p: pytest.fail("opened")) if too_many else real_open
        )
        assert cli.cmd_send(settings, args) == 2
        assert [event for event, _ in logs] == ["send_bad_file"]
        assert reason in logs[0][1]["error"], (reason, logs[0][1]["error"])


@pytest.mark.parametrize("is_html", [False, True])
def test_cli_refuses_too_long_text_before_auth_and_uploads(
    settings: Settings, tmp_path, monkeypatch, is_html: bool
) -> None:
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: pytest.fail("no auth expected"))
    monkeypatch.setattr(cli, "_open_regular", lambda p: pytest.fail("no file opened"))
    logs: list[str] = []
    monkeypatch.setattr(cli, "log", SimpleNamespace(error=lambda event, **kw: logs.append(event)))
    # Plain text grows when escaped: 10 000 "<" are 40 000 bytes of HTML, raw HTML stays as typed.
    text = "<" * 10_000 if not is_html else "x" * (send.MAX_HTML_BYTES + 1)
    args = argparse.Namespace(text=text, file=None, thread=THREAD, html=is_html, image=None, attach=["a.pdf"])
    assert cli.cmd_send(settings, args) == 2
    assert logs == ["send_too_long" if is_html else "send_refused"]  # 029-AC10: plain text is rendered


@pytest.mark.parametrize(
    ("text", "is_html"), [("x" * send.MAX_HTML_BYTES, False), ("&" * send.MAX_HTML_BYTES, True)]
)
def test_cli_sends_text_right_at_the_limit(
    settings: Settings, sp: FakeSharePoint, tmp_path, monkeypatch, text: str, is_html: bool
) -> None:
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"1")
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: ({"access_token": AAD}, "sk"))
    monkeypatch.setattr(
        "miniteams.auth.token_source",
        lambda s: SimpleNamespace(sharepoint_token=lambda h: "sp-token", display_name=lambda: ""),
    )
    args = argparse.Namespace(
        text=text, file=None, thread=THREAD, html=is_html, image=None, attach=[str(doc)]
    )
    assert cli.cmd_send(settings, args) == 0
    assert len(sp.posted[0]["content"]) == send.MAX_HTML_BYTES


def test_cli_attach_sends_the_files(settings: Settings, sp: FakeSharePoint, tmp_path, monkeypatch) -> None:
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.7")
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: ({"access_token": AAD}, "sk"))
    monkeypatch.setattr("miniteams.auth.self_mri", lambda token: ME)

    class Source:
        def sharepoint_token(self, host: str) -> str | None:
            return "sp-token" if host == HOST else None

        def display_name(self) -> str:
            return ""

    monkeypatch.setattr("miniteams.auth.token_source", lambda s: Source())
    assert cli.cmd_send(settings, _args(tmp_path, str(doc))) == 0
    (body,) = sp.posted
    assert [e["fileName"] for e in json.loads(body["properties"]["files"])] == ["report.pdf"]
    assert next(iter(sp.items.values()))["data"] == b"%PDF-1.7"


def test_the_cards_join_the_messages_own_properties(settings: Settings, sp: FakeSharePoint) -> None:
    """025-AC5: a composer message's mentions and its file cards travel together, with its id."""
    drive = _drive(settings, sp)
    entries, uploaded = files.upload_files(settings, "sk", drive, ME, THREAD, _open([("a.pdf", b"1")]))
    files.post_files(
        settings,
        "sk",
        THREAD,
        "<p>hi</p>",
        "Me",
        drive=drive,
        entries=entries,
        uploaded=uploaded,
        properties={"mentions": "[]"},
        is_html=True,
        client_message_id="99",
    )
    (body,) = sp.posted
    assert body["properties"]["mentions"] == "[]" and _cards_of(body) == ["a.pdf"]
    assert body["clientmessageid"] == "99" and body["content"] == "<p>hi</p>"


def _cards_of(body: dict[str, Any]) -> list[str]:
    return [e["fileName"] for e in json.loads(body["properties"]["files"])]


def test_cli_sends_images_and_files_in_one_message(
    settings: Settings, sp: FakeSharePoint, tmp_path, monkeypatch
) -> None:
    """025: `--image` with `--attach`: one message, the text, the inline images, the file cards."""
    doc, picture = tmp_path / "report.pdf", tmp_path / "p.png"
    doc.write_bytes(b"%PDF-1.7")
    picture.write_bytes(png())
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: ({"access_token": AAD}, "sk"))
    monkeypatch.setattr("miniteams.auth.self_mri", lambda token: ME)
    monkeypatch.setattr(
        "miniteams.auth.token_source",
        lambda s: SimpleNamespace(sharepoint_token=lambda h: "sp-token", display_name=lambda: ""),
    )
    monkeypatch.setattr(send, "upload_image", lambda *a: "0-weu-d1-" + "1" * 32)
    args = _args(tmp_path, str(doc), image=[str(picture)])
    args.text = "a < b"
    assert cli.cmd_send(settings, args) == 0
    (body,) = sp.posted
    assert body["content"].startswith("<p>a &lt; b</p><p><img ") and "1" * 32 in body["content"]
    assert [e["fileName"] for e in json.loads(body["properties"]["files"])] == ["report.pdf"]


def test_parser_takes_repeated_attach(monkeypatch) -> None:
    seen: list[argparse.Namespace] = []
    monkeypatch.setattr(cli, "cmd_send", lambda s, a: seen.append(a) or 0)
    assert cli.main(["send", "--thread", THREAD, "--attach", "a.pdf", "--attach", "b.zip"]) == 0
    assert seen[0].attach == ["a.pdf", "b.zip"]
