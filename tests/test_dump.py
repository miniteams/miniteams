"""History pagination, ordering, de-dup, and timestamp parsing."""

from typing import Any

from miniteams import dump
from miniteams.config import Settings
from miniteams.dump import _epoch_seconds


def test_epoch_seconds_truncates_to_seconds() -> None:
    assert _epoch_seconds("1970-01-01T00:00:10.5000000Z") == 10
    assert _epoch_seconds("garbage") == 0


class _Resp:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._data


class _Client:
    """Minimal httpx.Client stand-in returning canned pages in order."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[str] = []

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_: Any) -> None:
        pass

    def get(self, url: str) -> _Resp:
        self.calls.append(url)
        return _Resp(self._pages.pop(0))


def _msg(mid: str, ts: str) -> dict[str, Any]:
    return {"id": mid, "composetime": ts}


def test_fetch_history_paginates_and_sorts_oldest_first(settings: Settings, monkeypatch) -> None:
    pages = [
        {"messages": [_msg("4", "1970-01-01T00:00:40Z"), _msg("3", "1970-01-01T00:00:30Z")]},
        {"messages": [_msg("2", "1970-01-01T00:00:20Z")]},  # short page → stop
    ]
    client = _Client(pages)
    monkeypatch.setattr(dump.httpx, "Client", lambda *a, **k: client)
    out = dump.fetch_history(settings, "sk", "48:notes", page_size=2, max_pages=0)
    assert [m["id"] for m in out] == ["2", "3", "4"]
    # second page used endTime derived from the oldest of page 1 (ts 30 → 29s)
    assert "endTime=29000" in client.calls[1]


def test_fetch_history_dedups_overlapping_pages(settings: Settings, monkeypatch) -> None:
    pages = [
        {"messages": [_msg("3", "1970-01-01T00:00:30Z"), _msg("2", "1970-01-01T00:00:20Z")]},
        {"messages": [_msg("2", "1970-01-01T00:00:20Z")]},  # duplicate id "2"
    ]
    monkeypatch.setattr(dump.httpx, "Client", lambda *a, **k: _Client(pages))
    out = dump.fetch_history(settings, "sk", "48:notes", page_size=2, max_pages=0)
    assert [m["id"] for m in out] == ["2", "3"]


def test_fetch_history_respects_max_pages(settings: Settings, monkeypatch) -> None:
    pages = [
        {"messages": [_msg("4", "1970-01-01T00:00:40Z"), _msg("3", "1970-01-01T00:00:30Z")]},
        {"messages": [_msg("2", "1970-01-01T00:00:20Z"), _msg("1", "1970-01-01T00:00:10Z")]},
    ]
    monkeypatch.setattr(dump.httpx, "Client", lambda *a, **k: _Client(pages))
    out = dump.fetch_history(settings, "sk", "48:notes", page_size=2, max_pages=1)
    assert [m["id"] for m in out] == ["3", "4"]  # stopped after one page
