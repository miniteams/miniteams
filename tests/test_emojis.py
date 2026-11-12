"""Custom emoji listing (CSA metadata → rows → table/JSONL/images), network mocked."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from miniteams import emojis
from miniteams.config import Settings
from miniteams.directory import Directory

_KEY = "ah-denis;0-frc-d4-2de7f26db42058a360d77508b203b9fc"
_META = {
    "categories": [
        {
            "id": "customEmoji",
            "emoticons": [
                {
                    "id": _KEY,
                    "documentId": "0-frc-d4-2de7f26db42058a360d77508b203b9fc",
                    "shortcuts": ["ah-denis"],
                    "createdOn": 1722640818713,
                    "creator": "b0a2411a",
                    "isDeleted": False,
                },
                {"id": "gone;0-frs-d1-x", "shortcuts": ["gone"], "createdOn": 1, "isDeleted": True},
                {"id": "bare;0-frs-d1-y", "createdOn": 1600000000000},
                {"id": "newer;0-frs-d1-z", "createdOn": 1790000000000, "creator": "8:orgid:c0ffee"},
            ],
        }
    ]
}


def test_rows_drop_deleted_and_sort_oldest_first() -> None:
    got = emojis.rows(_META)
    assert [r["name"] for r in got] == ["bare", "ah-denis", "newer"]  # oldest first; `gone` deleted
    assert got[0] == {
        "name": "bare",  # no shortcut: the key's name part
        "key": "bare;0-frs-d1-y",
        "created": "2020-09-13T12:26:40+00:00",
        "creator_mri": "",
        "document_id": "",
    }
    assert got[1]["key"] == _KEY
    assert got[1]["creator_mri"] == "8:orgid:b0a2411a"
    assert got[1]["created"] == "2024-08-02T23:20:18+00:00"
    assert got[2]["creator_mri"] == "8:orgid:c0ffee"  # already an MRI: not prefixed twice


def test_rows_tolerate_a_missing_date_and_empty_payload() -> None:
    assert emojis.rows({}) == []
    got = emojis.rows({"categories": [{"emoticons": [{"id": "x;1", "createdOn": None}]}]})
    assert got[0]["created"] == ""


def test_fetch_metadata_sends_both_tokens(settings: Settings, monkeypatch) -> None:
    import httpx

    seen: dict[str, Any] = {}

    def fake_get(url: str, **kw: Any) -> httpx.Response:
        seen.update(kw, url=url)
        return httpx.Response(200, json=_META, request=httpx.Request("GET", url))

    monkeypatch.setattr(emojis.httpx, "get", fake_get)
    assert emojis.fetch_metadata(settings, "SK", "CSA") == _META
    assert seen["url"] == f"{settings.csa_url}/api/v1/customemoji/metadata"
    assert seen["headers"]["Authentication"] == "skypetoken=SK"
    assert seen["headers"]["Authorization"] == "Bearer CSA"


@pytest.mark.parametrize("jsonl", [True, False])
async def test_list_emojis_resolves_creators_and_downloads(
    settings: Settings, monkeypatch, capsys, jsonl: bool
) -> None:
    monkeypatch.setattr(emojis, "fetch_metadata", lambda *a: _META)
    resolved: list[list[str]] = []

    async def fake_resolve(self: Directory, mris: list[str], *, refresh: bool = False) -> None:
        assert refresh  # creators unknown to an old lookup must be retried
        resolved.append(mris)
        self.note_name("8:orgid:b0a2411a", "Alice")

    async def fake_fetch(client: Any, token: str, url: str, dest: Path, suffix: str) -> str:
        if "2de7f" not in url:
            raise RuntimeError("403")
        return str(dest / "ah.png")

    monkeypatch.setattr(Directory, "resolve", fake_resolve)
    monkeypatch.setattr(emojis, "fetch_image", fake_fetch)
    await emojis.list_emojis(settings, "SK", "B", "CSA", jsonl=jsonl, download=True)

    assert len(resolved) == 1  # one batched lookup, not one per emoji
    out = capsys.readouterr().out.splitlines()
    image = str(settings.cache_dir / "emojis" / "ah.png")
    if jsonl:
        records = [json.loads(line) for line in out]
        assert records[1]["creator"] == "Alice" and records[1]["image"] == image
        assert "image" not in records[0]  # no documentId: nothing to fetch
    else:
        assert out == [
            "2020-09-13  bare  ()",
            f"2024-08-02  ah-denis  (Alice)  {image}",
            "2026-09-21  newer  (Unknown user)",  # unresolved (038-AC6), and its image failed
        ]


async def test_a_failed_image_does_not_stop_the_listing(settings: Settings, monkeypatch, capsys) -> None:
    meta = {"categories": [{"emoticons": [dict(_META["categories"][0]["emoticons"][0], creator=None)]}]}
    monkeypatch.setattr(emojis, "fetch_metadata", lambda *a: meta)

    async def boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("403")

    monkeypatch.setattr(emojis, "fetch_image", boom)
    await emojis.list_emojis(settings, "SK", "B", "CSA", jsonl=True, download=True)
    record = json.loads(capsys.readouterr().out)
    assert record["image"] == "" and record["name"] == "ah-denis"


@pytest.mark.parametrize(
    ("creator", "mri"),
    [
        ("b0a2411a", "8:orgid:b0a2411a"),
        ("8:orgid:b0a2411a", "8:orgid:b0a2411a"),
        ("28:bot-id", "28:bot-id"),
        ("8:teamsvisitor:x", "8:teamsvisitor:x"),
        ("", ""),
    ],
)
def test_creator_is_prefixed_only_when_bare(creator: str, mri: str) -> None:
    got = emojis.rows({"categories": [{"emoticons": [{"id": "e;1", "creator": creator}]}]})
    assert got[0]["creator_mri"] == mri


async def test_list_emojis_makes_stdout_blocking(settings: Settings, monkeypatch, capsys) -> None:
    """Without it, `emojis --jsonl | slow-reader` stopped at 226 of 312 lines (BlockingIOError)."""
    calls: list[bool] = []
    monkeypatch.setattr(emojis, "force_blocking_stdout", lambda: calls.append(True))
    monkeypatch.setattr(emojis, "fetch_metadata", lambda *a: {})
    await emojis.list_emojis(settings, "SK", "B", "CSA")
    assert calls == [True]


@pytest.mark.parametrize("document_id", ["0-frc-d4-a/../../x", "0-frc-d4-a?view=y", "0-frc-d4-a#z"])
async def test_image_url_escapes_the_document_id(settings: Settings, monkeypatch, document_id: str) -> None:
    urls: list[str] = []

    async def fake_fetch(client: Any, token: str, url: str, dest: Path, suffix: str) -> str:
        urls.append(url)
        return "img"

    monkeypatch.setattr(emojis, "fetch_image", fake_fetch)
    await emojis._image(None, "SK", document_id, settings.cache_dir)  # type: ignore[arg-type]
    from urllib.parse import urlsplit

    parts = urlsplit(urls[0])
    assert parts.path.count("/") == 5  # /v1/objects/<id>/views/imgpsh_fullsize, nothing injected
    assert parts.path.endswith("/views/imgpsh_fullsize") and not parts.query and not parts.fragment


def test_ids_by_name_falls_back_to_the_key_tail() -> None:
    """012: the composer's custom set, name → AMS object id; first name wins, id-less entries dropped."""
    got = emojis.ids_by_name(
        [
            {"name": "party", "key": "party;0-weu-d1-key", "document_id": "0-weu-d1-doc"},
            {"name": "wave", "key": "wave;0-weu-d1-tail", "document_id": ""},
            {"name": "party", "key": "party;0-weu-d1-other", "document_id": ""},
            {"name": "bare", "key": "bare", "document_id": ""},
        ]
    )
    assert got == {"party": "0-weu-d1-doc", "wave": "0-weu-d1-tail"}


async def test_cached_image_downloads_once_into_the_cache(settings: Settings, monkeypatch) -> None:
    """012-AC9: the image lands under cache_dir/emojis; a failed download is None."""
    seen: list[tuple[str, str, Path]] = []

    async def fake_image(client: Any, token: str, document_id: str, dest: Path) -> str:
        seen.append((token, document_id, dest))
        return str(dest / f"{document_id}.png") if document_id == "ok" else ""

    monkeypatch.setattr(emojis, "_image", fake_image)
    dest = settings.cache_dir / "emojis"
    assert await emojis.cached_image(settings, "SK", "ok") == dest / "ok.png"
    assert dest.is_dir()
    assert await emojis.cached_image(settings, "SK", "bad") is None
    assert seen == [("SK", "ok", dest), ("SK", "bad", dest)]


# --- Teams' art for themed emoticons (spec 042), CDN mocked at the transport ---

_PNG = b"\x89PNG-rock"


class _Cdn:
    def __init__(self) -> None:
        self.asked: list[httpx.Request] = []
        self.reply = httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request)
        return self.reply


@pytest.fixture
def cdn(monkeypatch: pytest.MonkeyPatch) -> _Cdn:
    fake, real = _Cdn(), httpx.AsyncClient
    monkeypatch.setattr(
        emojis.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(fake.handler), **kw)
    )
    return fake


async def test_teams_art_is_fetched_once_then_read_from_disk(tmp_path: Path, cdn: _Cdn) -> None:
    """042-AC3: one tokenless CDN request, then the cache, also for a new instance (a restart)."""
    cache = tmp_path / "teams-emojis"
    art = emojis.TeamsArt(cache)
    assert await art.image("rock") == _PNG
    assert await art.image("rock") == _PNG
    assert [str(r.url) for r in cdn.asked] == [emojis._ART_URL.format("rock")]
    assert not {"authorization", "authentication", "cookie"} & set(cdn.asked[0].headers)
    assert sorted(p.name for p in cache.iterdir()) == ["rock.png"]  # no .part left behind
    assert await emojis.TeamsArt(cache).image("rock") == _PNG
    assert len(cdn.asked) == 1


@pytest.mark.parametrize(
    "emoticon", ["like", "1f918_signofthehorns", "../cfg", "rock/x", "rock%2f", "x" * 300, ""]
)
async def test_teams_art_serves_themed_ids_only(tmp_path: Path, cdn: _Cdn, emoticon: str) -> None:
    """042-AC2: any other id is None before the disk or the network."""
    assert await emojis.TeamsArt(tmp_path / "teams-emojis").image(emoticon) is None
    assert cdn.asked == [] and not (tmp_path / "teams-emojis").exists()


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(404),
        httpx.Response(200, content=b"<svg/>", headers={"content-type": "image/svg+xml"}),
        httpx.Response(200, content=b"x" * (emojis._ART_MAX + 1), headers={"content-type": "image/png"}),
    ],
)
async def test_failed_teams_art_is_not_refetched_before_the_ttl(
    tmp_path: Path, cdn: _Cdn, monkeypatch: pytest.MonkeyPatch, reply: httpx.Response
) -> None:
    """042-AC4: a failure (status, type, size) is None, nothing is cached, no new request until the TTL."""
    cdn.reply = reply
    cache = tmp_path / "teams-emojis"
    art = emojis.TeamsArt(cache)
    assert await art.image("rock") is None
    assert await art.image("rock") is None
    assert len(cdn.asked) == 1 and not cache.exists()
    monkeypatch.setattr(art._downloads, "ttl", 0.0)  # the TTL over
    assert await art.image("rock") is None
    assert len(cdn.asked) == 2


async def test_teams_art_not_stored_is_still_served(
    tmp_path: Path, cdn: _Cdn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """042-AC3: a cache write that fails (disk full) still serves the art and leaves no `.part` behind."""
    cache = tmp_path / "teams-emojis"
    real_replace = Path.replace

    def full(self: Path, target: Path) -> Path:
        if self.suffix == ".part":
            raise OSError(28, "No space left on device")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", full)
    assert await emojis.TeamsArt(cache).image("rock") == _PNG
    assert list(cache.iterdir()) == []


async def test_no_cache_dir_serves_no_art(cdn: _Cdn) -> None:
    assert await emojis.TeamsArt(None).image("rock") is None
    assert cdn.asked == []
