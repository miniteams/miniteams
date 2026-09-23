"""Rows seeded from the archive index (spec 004): what the per-row API fetches used to buy."""

from typing import Any

import pytest

from miniteams import web as W
from miniteams.archive_store import Index
from miniteams.directory import Directory

ME = "8:orgid:me"


@pytest.fixture
def quiet_directory(directory: Directory, monkeypatch: pytest.MonkeyPatch) -> Directory:
    """A Directory whose lookups are local and counted — a seeded row must not reach for them."""
    directory.me = ME
    directory.fetched = []  # type: ignore[attr-defined]

    async def fake_label(self: Directory, thread_id: str) -> str:
        self.fetched.append(thread_id)  # type: ignore[attr-defined]
        return f"fetched:{thread_id}"

    async def fake_display(self: Directory, mri: str) -> str:
        return f"name:{mri}"

    monkeypatch.setattr(Directory, "label", fake_label)
    monkeypatch.setattr(Directory, "display", fake_display)
    return directory


def _index_with(tmp_path: Any, chats: list[dict[str, Any]]) -> Any:
    """Build index.db through the archive's own writer, so the schema cannot drift from the reader."""
    index = Index(tmp_path)
    for chat in chats:
        index.upsert_chat(
            chat["id"],
            label=chat.get("label", ""),
            topic=chat.get("topic", ""),
            participants=chat.get("participants", []),
            raw=chat.get("raw", {}),
        )
    index.close()
    return tmp_path


def _chat(thread_id: str, **kw: Any) -> dict[str, Any]:
    raw = {"id": thread_id, "lastMessage": {"composetime": "2026-09-01T10:00:00Z"}, **kw.pop("raw", {})}
    return {"id": thread_id, "raw": raw, **kw}


def _conv(thread_id: str, when: str, **last: Any) -> dict[str, Any]:
    return {"id": thread_id, "lastMessage": {"composetime": when, **last}}


# --- the seed itself ---


def test_seed_label_drops_me_and_prefers_people_over_apps(tmp_path: Any) -> None:
    data_dir = _index_with(
        tmp_path,
        [
            _chat(
                "19:a_b@unq.gbl.spaces",
                participants=[{"mri": ME, "name": "Me"}, {"mri": "8:orgid:x", "name": "Jean Martin"}],
            ),
            _chat(
                "19:topic@thread.v2",
                topic="Weekly sync",
                participants=[{"mri": ME, "name": "Me"}, {"mri": "8:orgid:x", "name": "Jean Martin"}],
            ),
            _chat(
                "19:bot@thread.v2",
                participants=[{"mri": ME, "name": "Me"}, {"mri": "28:app", "name": "Jira Cloud"}],
            ),
        ],
    )
    seed = W.seed_from_archive(data_dir, ME)
    assert seed["19:a_b@unq.gbl.spaces"]["label"] == "Jean Martin"  # never "Jean Martin, Me"
    assert seed["19:topic@thread.v2"]["label"] == "Weekly sync · 2p"
    assert seed["19:bot@thread.v2"]["label"] == "Jira Cloud"  # nobody else to name it after


def test_a_topic_naming_me_is_kept_as_is(tmp_path: Any) -> None:
    # "Meeting with <me> · 1p" is the chat's real name, not a roster artefact: only roster-built
    # labels drop me. Three such chats exist in the corpus.
    data_dir = _index_with(
        tmp_path,
        [
            _chat(
                "19:booked@thread.v2",
                topic="Meeting with Jean Martin",
                participants=[{"mri": ME, "name": "Jean Martin"}],
            )
        ],
    )
    label = W.seed_from_archive(data_dir, ME)["19:booked@thread.v2"]["label"]
    assert label == "Meeting with Jean Martin · 1p"


def test_seed_carries_snippet_sender_and_read_horizon(tmp_path: Any) -> None:
    data_dir = _index_with(
        tmp_path,
        [
            _chat(
                "19:a_b@unq.gbl.spaces",
                participants=[{"mri": "8:orgid:x", "name": "Jean Martin"}],
                raw={
                    "properties": {"consumptionhorizon": "1790000000000;1790000000001;0"},
                    "lastMessage": {
                        "id": "1790000000000",
                        "composetime": "2026-09-01T10:00:00Z",
                        "messagetype": "RichText/Html",
                        "content": "<p>salut</p>",
                        "imdisplayname": "Jean Martin",
                    },
                },
            )
        ],
    )
    row = W.seed_from_archive(data_dir, ME)["19:a_b@unq.gbl.spaces"]
    assert row["text"] == "salut"
    assert row["sender"] == "Jean Martin"
    assert row["read_id"] == "1790000000000"


def test_seed_skips_out_of_scope_and_degrades_on_a_missing_or_corrupt_index(tmp_path: Any) -> None:
    data_dir = _index_with(tmp_path / "ok", [_chat("19:channel@thread.tacv2", topic="Team")])
    assert W.seed_from_archive(data_dir, ME) == {}  # channels are not widget material
    assert W.seed_from_archive(tmp_path / "nowhere", ME) == {}  # no archive is not an error
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "index.db").write_text("this is not sqlite")
    assert W.seed_from_archive(broken, ME) == {}


def test_seed_never_creates_an_archive(tmp_path: Any) -> None:
    # A reader that brings `data/` into existence would have the widget seed the archive's own dir.
    W.seed_from_archive(tmp_path / "absent", ME)
    assert not (tmp_path / "absent").exists()


# --- how bootstrap uses it ---


async def test_seeded_label_spares_the_thread_fetch(quiet_directory: Directory, tmp_path: Any) -> None:
    data_dir = _index_with(
        tmp_path,
        [
            _chat(
                "19:seeded@thread.v2",
                participants=[{"mri": "8:orgid:x", "name": "Jean Martin"}],
                raw={
                    "lastMessage": {
                        "id": "1",
                        "composetime": "2026-09-15T10:00:00Z",
                        "imdisplayname": "Jean Martin",
                        "messagetype": "Text",
                        "content": "hi",
                    }
                },
            )
        ],
    )
    seed = W.seed_from_archive(data_dir, ME)
    pages = [
        [
            _conv("19:seeded@thread.v2", "2026-09-15T10:00:00Z", id="1", messagetype="Text", content="hi"),
            _conv("19:unknown@thread.v2", "2026-09-15T09:00:00Z", id="2", messagetype="Text", content="ho"),
        ]
    ]
    rows = await W.bootstrap(pages, quiet_directory, limit=0, seed=seed)
    assert rows["19:seeded@thread.v2"]["label"] == "Jean Martin"
    # The listing carried no imdisplayname, so the sender comes from the archive, not a lookup.
    assert rows["19:seeded@thread.v2"]["sender"] == "Jean Martin"
    assert rows["19:unknown@thread.v2"]["label"] == "fetched:19:unknown@thread.v2"
    assert quiet_directory.fetched == ["19:unknown@thread.v2"]  # type: ignore[attr-defined]


async def test_listing_wins_over_the_archive(quiet_directory: Directory, tmp_path: Any) -> None:
    data_dir = _index_with(
        tmp_path,
        [
            _chat(
                "19:moved@thread.v2",
                participants=[{"mri": "8:orgid:x", "name": "Jean Martin"}],
                raw={
                    "properties": {"consumptionhorizon": "1;1;0"},
                    "lastMessage": {"id": "1", "composetime": "2026-01-01T00:00:00Z", "content": "stale"},
                },
            )
        ],
    )
    seed = W.seed_from_archive(data_dir, ME)
    conv = _conv("19:moved@thread.v2", "2026-09-15T10:00:00Z", id="9", messagetype="Text", content="fresh")
    conv["properties"] = {"consumptionhorizon": "9;9;0"}
    rows = await W.bootstrap([[conv]], quiet_directory, limit=0, seed=seed)
    row = rows["19:moved@thread.v2"]
    assert row["text"] == "fresh"  # the archive's older snippet never overwrites the listing's
    assert row["read_id"] == "9"
    assert row["last_activity"] == "2026-09-15T10:00:00Z"


async def test_the_archive_supplies_a_read_horizon_the_listing_dropped(
    quiet_directory: Directory, tmp_path: Any
) -> None:
    # A quiet chat comes back from the listing without its consumptionhorizon: with nothing to
    # compare the last message against, every such row would claim to be read.
    data_dir = _index_with(
        tmp_path,
        [
            _chat(
                "19:quiet@thread.v2",
                participants=[{"mri": "8:orgid:x", "name": "Jean Martin"}],
                raw={
                    "properties": {"consumptionhorizon": "5;5;0"},
                    "lastMessage": {"id": "5", "composetime": "2026-09-15T09:00:00Z"},
                },
            )
        ],
    )
    seed = W.seed_from_archive(data_dir, ME)
    conv = _conv("19:quiet@thread.v2", "2026-09-15T10:00:00Z", id="9", messagetype="Text", content="hi")

    row = (await W.bootstrap([[conv]], quiet_directory, limit=0, seed=seed))["19:quiet@thread.v2"]

    assert row["read_id"] == "5"  # from the archive, since the listing carried none
    assert row["unread"] is True  # message 9 is newer than the horizon at 5


async def test_archive_resolves_a_stub_without_a_history_call(
    quiet_directory: Directory, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The listing drops `properties`, so a file post reads as a deleted message; the archived copy
    # kept them. Trusting it is what removes the per-row history page.
    fetched: list[str] = []

    def fake_history(s: Any, token: str, thread_id: str, page_size: int, max_pages: int) -> list[Any]:
        fetched.append(thread_id)
        return []

    monkeypatch.setattr(W, "fetch_history", fake_history)
    data_dir = _index_with(
        tmp_path,
        [
            _chat(
                "19:file@thread.v2",
                participants=[{"mri": "8:orgid:x", "name": "Jean Martin"}],
                raw={
                    "lastMessage": {
                        "id": "1",
                        "composetime": "2026-09-15T10:00:00Z",
                        "messagetype": "RichText/Html",
                        "content": "",
                        "properties": {"files": '[{"fileName": "Facture.pdf"}]'},
                    }
                },
            )
        ],
    )
    seed = W.seed_from_archive(data_dir, ME)
    stub = _conv("19:file@thread.v2", "2026-09-15T10:00:00Z", id="1", messagetype="RichText/Html", content="")
    rows = await W.bootstrap([[stub]], quiet_directory, limit=0, seed=seed)
    assert rows["19:file@thread.v2"]["text"] == "📎 Facture.pdf"
    assert fetched == []  # the whole point: no API call for a row the archive already describes


async def test_unresolved_stub_still_pays_for_history(
    quiet_directory: Directory, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetched: list[str] = []

    def fake_history(settings: Any, token: str, thread_id: str, page_size: int, max_pages: int) -> list[Any]:
        fetched.append(thread_id)
        return [{"id": "1", "messagetype": "RichText/Html", "content": "", "properties": {"deletetime": "1"}}]

    monkeypatch.setattr(W, "fetch_history", fake_history)
    seed = W.seed_from_archive(_index_with(tmp_path, []), ME)
    stub = _conv("19:gone@thread.v2", "2026-09-15T10:00:00Z", id="1", messagetype="RichText/Html", content="")
    rows = await W.bootstrap([[stub]], quiet_directory, limit=0, seed=seed)
    assert rows["19:gone@thread.v2"]["text"] == "🗑 deleted"
    assert fetched == ["19:gone@thread.v2"]


async def test_mention_scan_is_bounded_to_the_head_of_the_list(
    quiet_directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mentions cost one history page each; beyond the head they wait for the chat to move again.
    fetched: list[str] = []

    def fake_history(s: Any, token: str, thread_id: str, page_size: int, max_pages: int) -> list[Any]:
        fetched.append(thread_id)
        return []

    monkeypatch.setattr(W, "fetch_history", fake_history)
    monkeypatch.setattr(W, "_MENTION_SCAN", 3)
    pages = [
        [
            _conv(
                f"19:c{i}@thread.v2",
                f"2026-09-{15 - i:02d}T10:00:00Z",
                id=str(i),
                messagetype="Text",
                content="hi",
            )
            for i in range(6)
        ]
    ]
    await W.bootstrap(pages, quiet_directory, limit=0, me=ME)
    # A set: the rows are built concurrently under the fan-out gate, so completion order varies.
    assert set(fetched) == {"19:c0@thread.v2", "19:c1@thread.v2", "19:c2@thread.v2"}
    assert len(fetched) == 3  # and nothing beyond the head is scanned
