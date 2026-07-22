"""Rate-limit backoff for the shared chat-service GET."""

import httpx
import pytest

from miniteams.http import get_with_retry


class _Resp:
    def __init__(self, status: int, headers: dict | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]


class _Client:
    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = responses
        self.calls = 0

    def get(self, url: str) -> _Resp:
        resp = self._responses[self.calls]
        self.calls += 1
        return resp


def test_retries_on_429_then_succeeds() -> None:
    slept: list[float] = []
    client = _Client([_Resp(429, {"Retry-After": "3"}), _Resp(200)])
    resp = get_with_retry(client, "u", sleep=slept.append)  # type: ignore[arg-type]
    assert resp.status_code == 200
    assert slept == [3.0]  # honored Retry-After


def test_exponential_backoff_without_retry_after() -> None:
    slept: list[float] = []
    client = _Client([_Resp(429), _Resp(429), _Resp(200)])
    get_with_retry(client, "u", sleep=slept.append)  # type: ignore[arg-type]
    assert slept == [1.0, 2.0]  # 2**0, 2**1


def test_raises_after_max_retries() -> None:
    client = _Client([_Resp(429) for _ in range(4)])
    with pytest.raises(httpx.HTTPStatusError):
        get_with_retry(client, "u", max_retries=2, sleep=lambda _: None)  # type: ignore[arg-type]
    assert client.calls == 3  # initial + 2 retries, then raise_for_status on the last 429


def test_non_429_error_raises_immediately() -> None:
    client = _Client([_Resp(500)])
    with pytest.raises(httpx.HTTPStatusError):
        get_with_retry(client, "u", sleep=lambda _: None)  # type: ignore[arg-type]
    assert client.calls == 1
