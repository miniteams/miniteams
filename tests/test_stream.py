"""Trouter client dispatch: the `on_event` hook replaces stdout printing when set."""

import asyncio
import json
from typing import Any

import pytest

from miniteams import stream as S
from miniteams.config import Settings
from miniteams.directory import Directory
from miniteams.stream import TrouterClient


class _Ws:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


def _client(settings: Settings, directory: Directory, **kw: Any) -> TrouterClient:
    c = TrouterClient(settings, {"access_token": "a"}, "sk", {}, "sid", "epid", directory, **kw)
    c._ws = _Ws()
    return c


def _delivery(body: dict[str, Any], endpoint: str = "messaging") -> str:
    return json.dumps({"id": 7, "url": f"https://h/x/{endpoint}", "body": json.dumps(body)})


async def test_hook_receives_decoded_event_and_frame_is_acked(
    settings: Settings, directory: Directory, capsys
) -> None:
    got: list[dict[str, Any]] = []

    async def hook(obj: dict[str, Any]) -> None:
        got.append(obj)

    client = _client(settings, directory, on_event=hook)
    event = {"type": "EventMessage", "resourceType": "NewMessage", "resource": {"id": "1"}}
    await client._on_delivery(_delivery(event))
    await client._on_delivery(_delivery({"type": "Other"}))  # not an EventMessage → hook not called
    await client._on_delivery(_delivery(event, endpoint="unifiedPresenceService"))
    assert got == [event]
    assert len(client._ws.sent) == 3 and all(s.startswith("3:::") for s in client._ws.sent)  # always ack
    assert capsys.readouterr().out == ""  # nothing printed when a hook is set


async def test_without_hook_stdout_path_still_prints(
    settings: Settings, directory: Directory, capsys
) -> None:
    client = _client(settings, directory)
    event = {
        "type": "EventMessage",
        "resourceType": "NewMessage",
        "resource": {"id": "1", "messagetype": "Text", "content": "hello", "from": "8:x", "to": "48:notes"},
    }
    await client._on_delivery(_delivery(event))
    assert "hello" in capsys.readouterr().out


async def test_hook_exception_is_logged_not_raised(settings: Settings, directory: Directory) -> None:
    async def bad_hook(obj: dict[str, Any]) -> None:
        raise ValueError("poison event")

    client = _client(settings, directory, on_event=bad_hook)
    event = {"type": "EventMessage", "resourceType": "NewMessage", "resource": {"id": "1"}}
    await client._on_delivery(_delivery(event))  # must not raise: the socket stays up
    assert client._ws.sent and client._ws.sent[0].startswith("3:::")


class _Tokens:
    """TokenSource stand-in: scripted refresh results (an exception instance is raised)."""

    def __init__(self, *results: Any) -> None:
        self._results = list(results)

    def refresh(self) -> dict[str, Any]:
        out = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if isinstance(out, Exception):
            raise out
        return out


def _scripted_exchange(monkeypatch, *results: Any) -> None:
    queue = list(results)

    def fake_exchange(_settings: Settings, _aad: str) -> dict[str, Any]:
        out = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(S, "exchange_skype_token", fake_exchange)


async def _refresh_sleeps(settings: Settings, directory: Directory, lifetime: float, n: int) -> list[float]:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) > n:
            raise asyncio.CancelledError

    tokens = _Tokens({"access_token": "A2", "id_token": "I2"})
    with pytest.raises(asyncio.CancelledError):
        await S._refresh_directory_token(settings, tokens, directory, lifetime, sleep=fake_sleep)  # type: ignore[arg-type]
    return sleeps[:n]


async def test_directory_token_is_renewed_before_expiry(
    settings: Settings, directory: Directory, monkeypatch
) -> None:
    _scripted_exchange(monkeypatch, RuntimeError("authz down"), {"skype_token": "ST2", "expires_in": 3600})
    directory.set_token("ST1", "I1")
    # 80% of the lifetime, then the floor after a failed renewal, then 80% of the new lifetime.
    assert await _refresh_sleeps(settings, directory, 3694.0, 3) == [3694.0 * 0.8, 60.0, 3600 * 0.8]
    assert (directory.skype_token, directory.bearer) == ("ST2", "I2")


async def test_dead_refresh_token_stops_the_refresher(
    settings: Settings, directory: Directory, monkeypatch
) -> None:
    from miniteams.auth import AuthExpired

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) > 3:
            raise asyncio.CancelledError  # still looping: the guard is broken

    cases = [
        (_Tokens(AuthExpired("dead")), [3600.0 * 0.8]),
        (_Tokens(RuntimeError("blip"), AuthExpired("dead")), [3600.0 * 0.8, S._TOKEN_REFRESH_MIN]),
    ]
    for dead, expected in cases:
        sleeps.clear()
        await S._refresh_directory_token(settings, dead, directory, 3600.0, sleep=fake_sleep)  # type: ignore[arg-type]
        assert sleeps == expected  # returned right after AuthExpired: no further AAD calls


async def test_missing_expires_in_does_not_refresh_every_minute(
    settings: Settings, directory: Directory, monkeypatch
) -> None:
    for missing in (None, 0, ""):
        _scripted_exchange(monkeypatch, {"skype_token": "ST2", "expires_in": missing})
        sleeps = await _refresh_sleeps(settings, directory, S._lifetime({"expires_in": missing}), 2)
        assert sleeps == [S._TOKEN_LIFETIME_FALLBACK * 0.8] * 2


async def test_run_forever_renews_directory_token_and_stops_the_refresher(
    settings: Settings, directory: Directory, monkeypatch
) -> None:
    from miniteams.auth import AuthExpired

    tokens = _Tokens(
        {"access_token": "A1", "id_token": "I1"},
        {"access_token": "A2", "id_token": "I2"},
        AuthExpired("dead"),
    )
    monkeypatch.setattr(
        S,
        "TokenSource",
        lambda _s: type("T", (), {"acquire": lambda self: None, "refresh": lambda self: tokens.refresh()})(),
    )
    _scripted_exchange(
        monkeypatch, {"skype_token": "ST1", "expires_in": 0.01}, {"skype_token": "ST2", "expires_in": 3600}
    )
    monkeypatch.setattr(S, "_TOKEN_REFRESH_MIN", 0.0)
    monkeypatch.setattr(S, "force_blocking_stdout", lambda: None)
    monkeypatch.setattr(S, "get_or_create_epid", lambda *_a: "epid")
    monkeypatch.setattr(S, "trouter_info", lambda *_a: {})
    monkeypatch.setattr(S, "handshake", lambda *_a: "sid")
    seen: list[str] = []
    sessions: list[int] = []

    class _Client:
        def __init__(self, *_a: Any) -> None:
            sessions.append(1)

        async def run(self) -> None:  # the session stays up until the refresher has renewed the pair
            async with asyncio.timeout(5):
                while directory.skype_token != "ST2":
                    await asyncio.sleep(0.01)
            seen.append(directory.bearer)

    monkeypatch.setattr(S, "TrouterClient", _Client)
    await S.run_forever(settings, directory=directory)  # 2nd session: refresh raises AuthExpired → returns
    await asyncio.sleep(0)
    assert (seen, len(sessions)) == (["I2"], 1)  # renewed mid-session, not by reconnecting
    leftover = [t for t in asyncio.all_tasks() if t.get_coro().__name__ == "_refresh_directory_token"]  # type: ignore[union-attr]
    assert all(t.done() for t in leftover)


async def test_reregister_uses_the_renewed_token_pair(
    settings: Settings, directory: Directory, monkeypatch
) -> None:
    sent: list[dict[str, str]] = []

    class _Http:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        async def __aenter__(self) -> _Http:
            return self

        async def __aexit__(self, *_: Any) -> bool:
            return False

        async def post(self, _url: str, headers: dict[str, str], json: Any) -> Any:
            sent.append(headers)
            return type("R", (), {"status_code": 202, "raise_for_status": lambda self: None})()

    monkeypatch.setattr(S.httpx, "AsyncClient", _Http)
    client = _client(settings, directory)  # connected with skype "sk" / aad access_token "a"
    client.info = {"surl": "https://surl/"}
    directory.set_token("ST2", "B2")  # what the refresher does an hour later
    await client._register()
    assert (sent[0]["X-Skypetoken"], sent[0]["Authorization"]) == ("ST2", "Bearer B2")
