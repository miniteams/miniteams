"""Identity + reaction-state caches (pure)."""

import asyncio
import json
import sqlite3
from contextlib import closing
from typing import Any

import httpx

from miniteams import directory as directory_mod
from miniteams.config import Settings
from miniteams.directory import Directory, _mri_from_userlink, is_app


def test_reaction_diff_tracks_add_and_remove(directory: Directory) -> None:
    added, removed = directory.reaction_diff("m", "like", ["u1", "u2"])
    assert added == {"u1", "u2"}
    assert removed == set()
    added, removed = directory.reaction_diff("m", "like", ["u2", "u3"])
    assert added == {"u3"}
    assert removed == {"u1"}


def test_name_for_uses_cache_then_strips_prefix(directory: Directory) -> None:
    directory.note_name("8:orgid:guid", "Alice")
    assert directory.name_for("8:orgid:guid") == "Alice"
    assert directory.name_for("8:orgid:unknown") == "unknown"


def test_contact_url_shares_the_mri_cache(directory: Directory) -> None:
    contact = "https://{}.ng.msg.teams.microsoft.com/v1/users/ME/contacts/8:orgid:guid"
    directory.note_name(contact.format("fr"), "Alice")
    assert directory.name_for("8:orgid:guid") == "Alice"
    assert directory.name_for(contact.format("apac")) == "Alice"  # another region's host


def test_import_drops_contact_url_keys(settings: Settings) -> None:
    contact = "https://fr.ng.msg.teams.microsoft.com/v1/users/ME/contacts/8:orgid:guid"
    settings.cache_dir.mkdir(parents=True)
    (settings.cache_dir / "names-v2.json").write_text(json.dumps({"8:orgid:guid": "Alice", contact: "Alice"}))
    assert Directory(settings).known_name("8:orgid:guid") == "Alice"
    with closing(sqlite3.connect(settings.config_dir / "names.db")) as db:
        assert db.execute("SELECT mri FROM names").fetchall() == [("8:orgid:guid",)]


def test_note_name_ignores_empty(directory: Directory) -> None:
    directory.note_name("", "x")
    directory.note_name("8:o:u", None)
    assert directory.name_for("8:o:u") == "u"


def test_mri_from_userlink() -> None:
    assert _mri_from_userlink("https://h/v1/users/8:orgid:g") == "8:orgid:g"
    assert _mri_from_userlink(None) == ""


async def test_label_special_thread_no_network(directory: Directory) -> None:
    assert await directory.label("48:notes") == "Notes to self"


async def test_forget_makes_next_thread_lookup_refetch(directory: Directory, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_fetch(self: Directory, thread_id: str) -> dict[str, Any]:
        calls.append(thread_id)
        return {"topic": "T", "members": [], "picture": None, "created_at": None}

    monkeypatch.setattr(Directory, "_fetch_thread", fake_fetch)
    await directory.thread("19:x@thread.v2")
    await directory.thread("19:x@thread.v2")  # cached
    directory.forget("19:x@thread.v2")
    directory.forget("19:never@thread.v2")  # unknown: no-op
    await directory.thread("19:x@thread.v2")
    assert calls == ["19:x@thread.v2", "19:x@thread.v2"]


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://h/t")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))


async def test_thread_caches_404_but_retries_transient_failures(directory: Directory, monkeypatch) -> None:
    outcomes: list[Any] = [_status_error(429), httpx.ConnectError("down"), _status_error(404)]
    calls: list[str] = []

    async def fake_fetch(self: Directory, thread_id: str) -> dict[str, Any]:
        calls.append(thread_id)
        raise outcomes.pop(0)

    monkeypatch.setattr(Directory, "_fetch_thread", fake_fetch)
    clock = {"now": 1000.0}
    monkeypatch.setattr(directory_mod.time, "monotonic", lambda: clock["now"])
    for _ in range(3):  # 429 → retried, connect error → retried (after the window), 404 → remembered
        assert await directory.thread("19:t@thread.v2") is None
        clock["now"] += directory_mod._THREAD_RETRY_AFTER
    assert await directory.thread("19:t@thread.v2") is None  # no fourth fetch
    assert calls == ["19:t@thread.v2"] * 3
    assert await directory.label("19:t@thread.v2") == "19:t@thread.v2"


async def test_thread_older_than_max_age_is_refetched(directory: Directory, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_fetch(self: Directory, thread_id: str) -> dict[str, Any]:
        calls.append(thread_id)
        return {"topic": None, "members": [], "picture": None, "created_at": None}

    monkeypatch.setattr(Directory, "_fetch_thread", fake_fetch)
    clock = {"now": 1000.0}
    monkeypatch.setattr(directory_mod.time, "monotonic", lambda: clock["now"])
    await directory.thread("19:x@thread.v2")
    clock["now"] += 600
    await directory.thread("19:x@thread.v2", max_age=600)  # at the limit: still trusted
    await directory.thread("19:x@thread.v2")  # no max_age: cached for good
    assert calls == ["19:x@thread.v2"]
    clock["now"] += 1
    await directory.thread("19:x@thread.v2", max_age=600)
    await _settled(directory)
    assert calls == ["19:x@thread.v2"] * 2


async def _settled(directory: Directory) -> None:
    """Let the background refetches finish."""
    while directory._thread_fetch:
        await asyncio.sleep(0)


async def test_a_stale_roster_answers_at_once_while_it_is_refetched(
    directory: Directory, monkeypatch
) -> None:
    """M9: past max_age the cached copy is returned without waiting; one shared refetch runs behind it."""
    release = asyncio.Event()
    calls: list[str] = []

    async def slow_fetch(self: Directory, thread_id: str) -> dict[str, Any]:
        calls.append(thread_id)
        if len(calls) > 1:
            await asyncio.wait_for(release.wait(), 3)
        return {"topic": f"v{len(calls)}", "members": [], "picture": None, "created_at": None}

    monkeypatch.setattr(Directory, "_fetch_thread", slow_fetch)
    t = "19:x@thread.v2"
    assert (await directory.thread(t))["topic"] == "v1"  # type: ignore[index]
    at, info = directory._threads[t]
    directory._threads[t] = (at - 601, info)  # aged, not a frozen clock: the deadlines below need one
    for _ in range(3):  # the fetch is hung: each caller still answers at once, from the cache
        got = await asyncio.wait_for(directory.thread(t, max_age=600), 0.5)
        assert got is not None and got["topic"] == "v1"
    await asyncio.sleep(0)
    assert calls == [t, t] and t in directory._thread_fetch
    release.set()
    await _settled(directory)
    assert (await directory.thread(t, max_age=600))["topic"] == "v2"  # type: ignore[index]


async def test_parallel_thread_lookups_share_one_fetch(directory: Directory, monkeypatch) -> None:
    release = asyncio.Event()
    calls: list[str] = []

    async def slow_fetch(self: Directory, thread_id: str) -> dict[str, Any]:
        calls.append(thread_id)
        await asyncio.wait_for(release.wait(), 3)
        return {"topic": "T", "members": [], "picture": None, "created_at": None}

    monkeypatch.setattr(Directory, "_fetch_thread", slow_fetch)
    first = asyncio.create_task(directory.thread("19:x@thread.v2"))
    others = [asyncio.create_task(directory.thread("19:x@thread.v2")) for _ in range(3)]
    await asyncio.sleep(0)
    first.cancel()  # a page closing mid-lookup
    release.set()
    assert [(await t) or {} for t in others] == [
        {"topic": "T", "members": [], "picture": None, "created_at": None}
    ] * 3
    assert calls == ["19:x@thread.v2"]
    assert directory._thread_fetch == {}  # removed once done


async def test_thread_cache_is_bounded(directory: Directory, monkeypatch) -> None:
    async def fake_fetch(self: Directory, thread_id: str) -> dict[str, Any]:
        return {"topic": thread_id, "members": [], "picture": None, "created_at": None}

    monkeypatch.setattr(Directory, "_fetch_thread", fake_fetch)
    monkeypatch.setattr(directory_mod, "_THREADS_MAX", 2)
    for thread in ("19:a@thread.v2", "19:b@thread.v2", "19:a@thread.v2", "19:c@thread.v2"):
        await directory.thread(thread)
    assert list(directory._threads) == ["19:a@thread.v2", "19:c@thread.v2"]  # least recently used went


def test_is_app_reads_the_mri_of_a_contact_url() -> None:
    assert is_app("28:bot") and is_app("https://h/v1/users/ME/contacts/28:bot")
    assert not is_app("8:orgid:ann") and not is_app("https://h/v1/users/8:orgid:28")


async def test_a_failed_refresh_keeps_the_stale_roster(directory: Directory, monkeypatch) -> None:
    """M3: past max_age a 429, a 5xx or a network error keeps (and returns) the cached roster and opens the
    retry window; only a success or a 404 replaces it."""
    request = httpx.Request("GET", "https://h/v1/threads/x")
    old = {
        "topic": None,
        "members": [{"mri": "8:orgid:a", "name": "Ann"}],
        "picture": None,
        "created_at": None,
    }
    new = {**old, "members": []}
    outcomes: list[Any] = [
        old,
        httpx.HTTPStatusError("429", request=request, response=httpx.Response(429)),
        httpx.HTTPStatusError("503", request=request, response=httpx.Response(503)),
        httpx.ConnectError("down", request=request),
        new,
        httpx.HTTPStatusError("404", request=request, response=httpx.Response(404)),
    ]
    calls: list[str] = []

    async def fake_fetch(self: Directory, thread_id: str) -> dict[str, Any]:
        calls.append(thread_id)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)

    monkeypatch.setattr(Directory, "_fetch_thread", fake_fetch)
    clock = {"now": 1000.0}
    monkeypatch.setattr(directory_mod.time, "monotonic", lambda: clock["now"])
    t = "19:x@thread.v2"
    assert await directory.thread(t) == old
    for _ in range(3):  # 429, 5xx, network: the stale copy each time, the failure recorded
        clock["now"] += directory_mod._THREAD_RETRY_AFTER + 1
        assert await directory.thread(t, max_age=1) == old
        await _settled(directory)
        assert t in directory._thread_failed
        assert await directory.thread(t, max_age=1) == old  # inside the window: no refetch
    assert len(calls) == 4
    clock["now"] += directory_mod._THREAD_RETRY_AFTER + 1
    assert await directory.thread(t, max_age=1) == old  # stale while revalidating
    await _settled(directory)
    assert await directory.thread(t, max_age=1) == new and t not in directory._thread_failed
    clock["now"] += 2
    assert await directory.thread(t, max_age=1) == new
    await _settled(directory)
    assert await directory.thread(t, max_age=1) is None  # a 404 is an answer: the chat is gone
    assert await directory.label(t) == t


def test_roster_label_is_one_rule_for_live_and_archived_rosters() -> None:
    """M10: topic · N, else the people but me, else the apps, else anyone named; "" for nobody."""
    me, ann, bot = "8:orgid:me", "8:orgid:ann", "28:bot"
    people = [{"mri": me, "name": "Me"}, {"mri": ann, "name": "Ann"}, {"mri": bot, "name": "Jira"}]
    assert directory_mod.roster_label("Plan", people, me) == "Plan · 3p"
    assert directory_mod.roster_label(None, people, me) == "Ann"
    assert directory_mod.roster_label("", [people[0], people[2]], me) == "Jira"
    assert directory_mod.roster_label(None, [people[0]], me) == "Me"
    assert directory_mod.roster_label(None, [{"mri": ann, "name": ""}, {"name": "No mri"}], me) == "No mri"
    assert directory_mod.roster_label(None, [], me) == ""
