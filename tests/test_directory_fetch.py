"""Thread roster/topic fetch + label formatting with a fake async client."""

from typing import Any

from miniteams import directory as directory_mod
from miniteams.directory import Directory


class _Resp:
    def __init__(self, data: Any = None, fail: bool = False) -> None:
        self._data = data
        self._fail = fail

    def raise_for_status(self) -> None:
        if self._fail:
            raise RuntimeError("boom")

    def json(self) -> Any:
        return self._data


class _AsyncClient:
    def __init__(self, handler: Any) -> None:
        self._handler = handler

    async def __aenter__(self) -> _AsyncClient:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def get(self, url: str, headers: Any = None) -> _Resp:
        return self._handler(url)


class _AsyncClientPost:
    def __init__(self, handler: Any) -> None:
        self._handler = handler

    async def __aenter__(self) -> _AsyncClientPost:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def post(self, url: str, headers: Any = None, json: Any = None) -> _Resp:
        return self._handler(url)


def _patch(monkeypatch, handler: Any) -> None:
    monkeypatch.setattr(directory_mod.httpx, "AsyncClient", lambda *a, **k: _AsyncClient(handler))


async def test_label_uses_topic_and_caches_names(directory: Directory, monkeypatch) -> None:
    data = {
        "members": [
            {"friendlyName": "Alice", "linkedMri": "8:o:a", "role": "Admin"},
            {"friendlyName": "Bob", "userLink": "https://h/v1/users/8:o:b"},
        ],
        "properties": {"topic": "Project X"},
    }
    _patch(monkeypatch, lambda url: _Resp(data))
    label = await directory.label("19:t@thread.v2")
    assert label == "Project X · 2p"
    assert directory.name_for("8:o:a") == "Alice"
    assert directory.name_for("8:o:b") == "Bob"


async def test_label_falls_back_to_roster_without_topic(directory: Directory, monkeypatch) -> None:
    data = {"members": [{"friendlyName": "Al", "linkedMri": "8:o:a"}], "properties": {}}
    _patch(monkeypatch, lambda url: _Resp(data))
    assert await directory.label("19:x@thread.v2") == "Al"


async def test_label_degrades_to_id_on_fetch_failure(directory: Directory, monkeypatch) -> None:
    _patch(monkeypatch, lambda url: _Resp(fail=True))
    assert await directory.label("19:bad@thread.v2") == "19:bad@thread.v2"


async def test_display_resolves_via_profile_and_caches(directory: Directory, monkeypatch) -> None:
    directory.set_token("sk", bearer="id-tok")
    calls = {"n": 0}

    def handler(url: str) -> _Resp:
        calls["n"] += 1
        return _Resp({"value": [{"mri": "8:orgid:guid", "displayName": "Alice"}]})

    monkeypatch.setattr(directory_mod.httpx, "AsyncClient", lambda *a, **k: _AsyncClientPost(handler))
    assert await directory.display("8:orgid:guid") == "Alice"
    assert await directory.display("8:orgid:guid") == "Alice"  # cached
    assert calls["n"] == 1


async def test_display_negative_caches_unresolved(directory: Directory, monkeypatch) -> None:
    directory.set_token("sk", bearer="id-tok")
    calls = {"n": 0}

    def handler(url: str) -> _Resp:
        calls["n"] += 1
        return _Resp({"value": []})  # endpoint returns nothing

    monkeypatch.setattr(directory_mod.httpx, "AsyncClient", lambda *a, **k: _AsyncClientPost(handler))
    assert await directory.display("8:orgid:zzz") == "zzz"  # stripped fallback
    assert await directory.display("8:orgid:zzz") == "zzz"
    assert calls["n"] == 1  # negative-cached, not re-fetched


async def test_names_cache_persists_across_instances(settings, monkeypatch) -> None:
    monkeypatch.setattr(
        directory_mod.httpx,
        "AsyncClient",
        lambda *a, **k: _AsyncClientPost(
            lambda url: _Resp({"value": [{"mri": "8:orgid:guid", "displayName": "Alice"}]})
        ),
    )
    first = Directory(settings)
    first.set_token("sk", bearer="id-tok")
    assert await first.display("8:orgid:guid") == "Alice"
    # A fresh instance loads the persisted name — no network needed.
    second = Directory(settings)
    assert second.name_for("8:orgid:guid") == "Alice"


async def test_display_without_bearer_skips_resolution(directory: Directory) -> None:
    # no bearer set → no network, just stripped id
    assert await directory.display("8:orgid:guid") == "guid"


async def test_thread_result_is_cached(directory: Directory, monkeypatch) -> None:
    calls = {"n": 0}

    def handler(url: str) -> _Resp:
        calls["n"] += 1
        return _Resp({"members": [], "properties": {"topic": "T"}})

    _patch(monkeypatch, handler)
    await directory.thread("19:c@thread.v2")
    await directory.thread("19:c@thread.v2")
    assert calls["n"] == 1  # second call served from cache
