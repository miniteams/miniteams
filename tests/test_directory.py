"""Identity + reaction-state caches (pure)."""

from typing import Any

import httpx

from miniteams.directory import Directory, _mri_from_userlink


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
    for _ in range(3):  # 429 → retried, connect error → retried, 404 → remembered
        assert await directory.thread("19:t@thread.v2") is None
    assert await directory.thread("19:t@thread.v2") is None  # no fourth fetch
    assert calls == ["19:t@thread.v2"] * 3
    assert await directory.label("19:t@thread.v2") == "19:t@thread.v2"
