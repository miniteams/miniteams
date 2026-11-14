"""Trouter client dispatch: the `on_event` hook replaces stdout printing when set."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from miniteams import follow as F
from miniteams import stream as S
from miniteams.config import Settings
from miniteams.directory import Directory
from miniteams.stream import TrouterClient


class _Ws:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


SURL_INFO = {"surl": "https://surl.invalid/s/"}
CALL_PUSH = {"id": 9, "url": f"https://surl.invalid/x/{S.NGC_PATH}", "body": "{}"}


def _client(
    settings: Settings, directory: Directory, info: dict[str, Any] | None = None, **kw: Any
) -> TrouterClient:
    c = TrouterClient(settings, {"access_token": "a"}, "sk", info or {}, "sid", "epid", directory, **kw)
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

    def __init__(self, *results: Any, changed: list[bool] | None = None) -> None:
        self._results = list(results)
        self._changed = changed or [True]  # written_since() answers, the last one repeats
        self.since: list[Any] = []  # the stamps written_since() was asked about

    def refresh(self) -> dict[str, Any]:
        out = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if isinstance(out, Exception):
            raise out
        return out

    def acquire(self) -> None:
        return None

    def written_since(self, stamp: Any) -> bool:
        self.since.append(stamp)
        return self._changed.pop(0) if len(self._changed) > 1 else self._changed[0]


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
    assert (directory.skype_token, directory.bearer) == ("ST2", "A2")  # not the id_token beside it


async def test_dead_refresh_token_waits_for_a_sibling_write_then_renews(
    settings: Settings, directory: Directory, monkeypatch
) -> None:
    """A refresh token dead in memory but rotated on disk by another process (archive, login) must not
    stop the renewals: the session would 401 on the stale skype token until restarted."""
    from miniteams.auth import AuthExpired

    def dead() -> AuthExpired:
        exc = AuthExpired("dead")
        exc.stamp = (7, 7, 7)
        return exc

    _scripted_exchange(monkeypatch, {"skype_token": "ST2", "expires_in": 3600})
    cases = [
        # The sibling's write already landed during the failed refresh: no poll, renewed at the floor.
        (_Tokens(dead(), {"access_token": "A2"}), [3600.0 * 0.8, 60.0]),
        # Not yet: poll the cache until it does, never calling AAD meanwhile.
        (
            _Tokens(dead(), {"access_token": "A2"}, changed=[False, False, True]),
            [3600.0 * 0.8, S._LOGIN_POLL, S._LOGIN_POLL, 60.0],
        ),
        # Still dead after the write (a login elsewhere is pending): waits again, no hot loop.
        (
            _Tokens(dead(), dead(), {"access_token": "A2"}),
            [3600.0 * 0.8, 60.0, 60.0],
        ),
    ]
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if directory.skype_token == "ST2":
            raise asyncio.CancelledError  # renewed: the next wait is the regular one

    for tokens, expected in cases:
        directory.set_token("ST1", "A1")
        sleeps.clear()
        with pytest.raises(asyncio.CancelledError):
            await S._refresh_directory_token(settings, tokens, directory, 3600.0, sleep=fake_sleep)  # type: ignore[arg-type]
        assert sleeps[:-1] == expected and (directory.skype_token, directory.bearer) == ("ST2", "A2")
        assert set(tokens.since) == {(7, 7, 7)}  # waits on the version the refresh failed on


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
        "token_source",
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
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            sessions.append(1)
            seen.append(directory.bearer)  # the connect-time pair

        async def run(self) -> None:  # the session stays up until the refresher has renewed the pair
            async with asyncio.timeout(5):
                while directory.skype_token != "ST2":
                    await asyncio.sleep(0.01)
            seen.append(directory.bearer)

    monkeypatch.setattr(S, "TrouterClient", _Client)
    await S.run_forever(settings, directory=directory)  # 2nd session: refresh raises AuthExpired → returns
    await asyncio.sleep(0)
    assert (seen, len(sessions)) == (["A1", "A2"], 1)  # renewed mid-session, not by reconnecting
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


def _loss(etag: str) -> str:
    indicators = [{"tag": "", "etag": etag}, {"tag": "messaging", "etag": etag}]
    return json.dumps({"name": "trouter.message_loss", "args": [{"droppedIndicators": indicators}]})


async def test_gap_hook_skips_the_connect_flood_and_fires_on_a_new_loss(
    settings: Settings, directory: Directory, monkeypatch
) -> None:
    gaps: list[str] = []

    async def on_gap(reason: str) -> None:
        gaps.append(reason)

    async def no_reregister(self, reason: str) -> None:  # noqa: ANN001
        pass

    monkeypatch.setattr(TrouterClient, "_maybe_reregister", no_reregister)
    client = _client(settings, directory, on_gap=on_gap)
    client._last_register = S.time.monotonic()  # just registered
    for _ in range(5):
        await client._on_named(_loss("T0"))  # the flood every connect opens with: one etag
    assert gaps == []
    await client._on_named(_loss("T1"))  # a real loss later in the session
    await client._on_named(_loss("T1"))
    await client._on_named(json.dumps({"name": "trouter.message_loss", "args": [{}]}))  # no etag
    assert gaps == ["message_loss"]


async def test_first_loss_long_after_connect_is_a_gap(
    settings: Settings, directory: Directory, monkeypatch
) -> None:
    """The real re-register runs on that loss too; it must not make the loss look like a flood."""
    gaps: list[str] = []

    async def on_gap(reason: str) -> None:
        gaps.append(reason)

    async def fake_register(self) -> None:  # noqa: ANN001
        self._last_register = S.time.monotonic()

    monkeypatch.setattr(TrouterClient, "_register", fake_register)
    client = _client(settings, directory, on_gap=on_gap)
    client._last_register = S.time.monotonic() - S._CONNECT_FLOOD - 1
    await client._on_named(_loss("T5"))
    assert gaps == ["message_loss"]


async def test_gap_hook_fires_after_registration(
    settings: Settings, directory: Directory, monkeypatch
) -> None:
    order: list[str] = []

    async def on_gap(reason: str) -> None:
        order.append(f"gap:{reason}")

    async def fake(self) -> None:  # noqa: ANN001
        order.append("auth")

    async def fake_register(self) -> None:  # noqa: ANN001
        order.append("register")

    monkeypatch.setattr(TrouterClient, "_authenticate", fake)
    monkeypatch.setattr(TrouterClient, "_register", fake_register)
    client = _client(settings, directory, on_gap=on_gap)
    await client._on_connected()
    for task in client._tasks:
        task.cancel()
    assert order == ["auth", "register", "gap:connected"]  # subscribed before the catch-up starts


async def test_run_forever_waits_for_a_login_when_asked(
    settings: Settings, directory: Directory, monkeypatch
) -> None:
    """A dead refresh token parks the loop until the token cache file changes, then the same source
    refreshes again (it rereads the rewritten cache); without the flag the loop returns (see above)."""
    from miniteams.auth import AuthExpired

    expired = AuthExpired("dead")
    expired.stamp = (7, 7, 7)
    tokens = _Tokens(expired, {"access_token": "A", "id_token": "I"})
    calls: list[int] = []
    since: list[Any] = []

    def refresh(_self: Any) -> Any:
        calls.append(1)
        return tokens.refresh()

    changed = iter([False] * 2 + [True] * 10)
    monkeypatch.setattr(
        S,
        "token_source",
        lambda _s: type(
            "T",
            (),
            {
                "acquire": lambda self: None,
                "refresh": refresh,
                "written_since": lambda self, st: since.append(st) or next(changed),
            },
        )(),
    )
    monkeypatch.setattr(S, "_LOGIN_POLL", 0.0)
    monkeypatch.setattr(S, "force_blocking_stdout", lambda: None)

    def exchange(*_a: Any) -> Any:
        raise asyncio.CancelledError  # reached only by the refresh after the wait

    monkeypatch.setattr(S, "exchange_skype_token", exchange)
    with pytest.raises(asyncio.CancelledError):
        await S.run_forever(settings, directory=directory, wait_for_login=True)
    assert len(calls) == 2 and next(changed)  # one refresh before the wait, one after the write
    assert set(since) == {(7, 7, 7)}  # the version the refresh failed on


def _link_loop(monkeypatch, *refreshes: Any, exchanges: list[Any]) -> list[int]:
    """run_forever on scripted tokens and exchanges, sessions that report alive twice, no backoff wait."""
    tokens = _Tokens(*refreshes)
    monkeypatch.setattr(S, "token_source", lambda _s: tokens)

    def exchange(*_a: Any) -> Any:
        out = exchanges.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(S, "exchange_skype_token", exchange)
    monkeypatch.setattr(S, "force_blocking_stdout", lambda: None)
    monkeypatch.setattr(S, "get_or_create_epid", lambda *_a: "epid")
    monkeypatch.setattr(S, "trouter_info", lambda *_a: {})
    monkeypatch.setattr(S, "handshake", lambda *_a: "sid")
    monkeypatch.setattr(S, "_LOGIN_POLL", 0.0)
    real_sleep = asyncio.sleep

    async def no_backoff(_delay: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(S.asyncio, "sleep", no_backoff)
    sessions: list[int] = []

    class _Client:
        def __init__(self, *_a: Any, on_alive: Any, **_kw: Any) -> None:
            self.alive = on_alive

        async def run(self) -> None:
            sessions.append(1)
            await self.alive()  # registered
            await self.alive()  # a keepalive

    monkeypatch.setattr(S, "TrouterClient", _Client)
    return sessions


async def test_run_forever_reports_the_teams_link(
    settings: Settings, directory: Directory, monkeypatch
) -> None:
    """036-AC4/AC5: up once registered, down when the session ends or a connect fails, login while it
    waits for one."""
    from miniteams.auth import AuthExpired

    aad = {"access_token": "A", "id_token": "I"}
    st = {"skype_token": "ST", "expires_in": 3600}
    _link_loop(
        monkeypatch,
        aad,
        aad,
        AuthExpired("dead"),
        aad,
        exchanges=[st, RuntimeError("authz down"), st, asyncio.CancelledError()],
    )
    states: list[str] = []
    with pytest.raises(asyncio.CancelledError):
        await S.run_forever(settings, directory=directory, wait_for_login=True, on_link=states.append)
    assert states == ["up", "up", "down", "down", "login", "up", "up", "down"]


async def test_a_failing_link_hook_does_not_stop_the_loop(
    settings: Settings, directory: Directory, monkeypatch
) -> None:
    """036-AC4: the hook raising is logged; the sessions go on."""
    import structlog

    aad = {"access_token": "A", "id_token": "I"}
    st = {"skype_token": "ST", "expires_in": 3600}
    sessions = _link_loop(monkeypatch, aad, exchanges=[st, st, asyncio.CancelledError()])

    def broken(_state: str) -> None:
        raise ValueError("page gone")

    with structlog.testing.capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await S.run_forever(settings, directory=directory, on_link=broken)
    assert len(sessions) == 2
    failed = [e for e in logs if e["event"] == "link_hook_failed"]
    assert len(failed) == 6 and failed[0]["error_type"] == "ValueError"  # 2 × (up, up, down)


async def test_a_failing_error_hook_does_not_stop_the_loop(
    settings: Settings, directory: Directory, monkeypatch
) -> None:
    """Spec 044: a hook raising on a failed attempt is logged; the reconnect loop goes on."""
    import structlog

    aad = {"access_token": "A", "id_token": "I"}
    st = {"skype_token": "ST", "expires_in": 3600}
    sessions = _link_loop(
        monkeypatch, aad, exchanges=[RuntimeError("authz down"), st, asyncio.CancelledError()]
    )
    seen: list[str] = []

    def broken(step: str, _exc: BaseException) -> None:
        seen.append(step)
        raise ValueError("hook broke")

    with structlog.testing.capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await S.run_forever(settings, directory=directory, on_error=broken)
    assert seen == ["sign-in"] and len(sessions) == 1  # reconnected after the failing hook
    failed = [e for e in logs if e["event"] == "error_hook_failed"]
    assert [(e["error"], e["error_type"]) for e in failed] == [("hook broke", "ValueError")]


# --- off_loop: blocking calls in daemon threads (spec 044) ---


async def test_off_loop_returns_raises_and_keeps_the_loop_free() -> None:
    import threading as th

    gate = th.Event()
    ticks: list[int] = []

    def slow() -> str:
        gate.wait(5)
        return th.current_thread().name

    async def tick() -> None:
        ticks.append(1)
        gate.set()

    name, _ = await asyncio.gather(S.off_loop(slow), tick())
    assert ticks == [1] and name.startswith("off-loop-slow")  # the loop ran while it blocked

    def boom(x: int) -> None:
        raise ValueError(x)

    with pytest.raises(ValueError, match="7"):
        await S.off_loop(boom, 7)


async def test_off_loop_cancelled_or_outlived_by_its_thread() -> None:
    """A cancelled wait: the thread's late result lands on a done future and is dropped, never an
    InvalidStateError in the loop's exception handler."""
    import threading as th

    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, ctx: errors.append(ctx))
    release, finished = th.Event(), th.Event()

    def held() -> int:
        release.wait(5)
        finished.set()
        return 1

    task = asyncio.ensure_future(S.off_loop(held))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert finished.wait(5)
    await asyncio.sleep(0.05)  # the late settle runs on this loop now
    loop.set_exception_handler(None)
    assert errors == []


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_off_loop_thread_ending_after_its_loop_closed() -> None:
    """Ctrl-C closes the loop under a running call: the thread's result is dropped, no thread error."""
    import threading as th

    release = th.Event()

    def held() -> int:
        release.wait(5)
        return 1

    async def start() -> asyncio.Future[int]:
        task = asyncio.ensure_future(S.off_loop(held, name="off-loop-test-closed"))
        await asyncio.sleep(0.01)
        return task

    loop = asyncio.new_event_loop()
    task = loop.run_until_complete(start())
    task.cancel()
    loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
    loop.close()
    (t,) = [t for t in th.enumerate() if t.name == "off-loop-test-closed"]
    assert t.daemon
    release.set()
    t.join(5)
    assert not t.is_alive()


async def test_a_silent_start_refreshes_off_the_loop_and_stops_on_a_dead_login(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prompt=False (watch, follower): the first refresh runs off the loop; a dead login ends the run."""
    import threading as th

    from miniteams.auth import AuthExpired

    threads: list[str] = []

    class Dead:
        def refresh(self) -> dict[str, Any]:
            threads.append(th.current_thread().name)
            raise AuthExpired("refresh token expired")

    monkeypatch.setattr(S, "token_source", lambda _s: Dead())
    with pytest.raises(AuthExpired):
        await S.run_forever(settings, prompt=False)
    assert threads == ["off-loop-refresh"]


def test_websocket_url_names_the_session_and_the_query(settings: Settings, directory: Directory) -> None:
    info = {"socketio": "https://trouter.invalid/", "connectparams": {"sr": "x"}, "surl": "s"}
    client = TrouterClient(settings, {}, "sk", info, "sid", "ep", directory)
    url = client._ws_url()
    assert url.startswith("wss://trouter.invalid/socket.io/1/websocket/sid?v=v4&sr=x&") and "epid=ep" in url


# --- spec 044: call callbacks, calling registration, call pushes ---


def test_agent_ack_echoes_the_request_headers() -> None:
    """044-AC1: without MS-CV + .0, trouter-request and trouter-client the call ends at ~40 s."""
    req = {"id": 5, "headers": {"MS-CV": "abc.1", "Trouter-Request": '{"id":"r1"}', "X": "y"}}
    assert json.loads(S.agent_ack(req)) == {
        "id": 5,
        "status": 200,
        "headers": {"MS-CV": "abc.1.0", "trouter-request": '{"id":"r1"}', "trouter-client": '{"cd":1}'},
        "body": "",
    }
    assert json.loads(S.agent_ack({"id": 6}))["headers"] == {"trouter-client": '{"cd":1}'}


async def test_call_callbacks_go_to_the_agent_hook_with_the_echoing_ack(
    settings: Settings, directory: Directory
) -> None:
    got: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    async def agent(req: dict[str, Any]) -> None:
        got.append(req)
        raise RuntimeError("a bad callback")

    async def event(obj: dict[str, Any]) -> None:
        events.append(obj)

    client = _client(settings, directory, on_event=event, on_agent=agent)
    req = {
        "id": 3,
        "url": f"/v4/f/s{S.AGENT_PATH}e/t/call/acceptance/",
        "headers": {"MS-CV": "x"},
        "body": "",
    }
    with capture_logs() as logs:
        await client._handle_frame(f"3:::{json.dumps(req)}")
        await client._handle_frame('3:::{"id":4,"url":"/v4/f/s/messaging","body":""}')
    assert got == [req] and events == []
    assert json.loads(client._ws.sent[0][4:])["headers"]["MS-CV"] == "x.0"
    assert client._ws.sent[1] == '3:::{"id":4,"status":200,"body":""}'  # others keep the plain ack
    assert [e["event"] for e in logs] == ["agent_hook_failed"]


class _Registrar:
    """Fake registrar: `ngc` / `messaging` = "ok", "fail" or "hang"; every body posted, in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.ngc, self.messaging = "ok", "ok"
        registrar = self

        class _Http:
            def __init__(self, *_a: Any, **_k: Any) -> None:
                pass

            async def __aenter__(self) -> _Http:
                return self

            async def __aexit__(self, *_: Any) -> bool:
                return False

            async def post(self, _url: str, headers: dict[str, str], json: Any) -> Any:
                registrar.bodies.append(json)
                ngc = json["clientDescription"]["appId"] == "NextGenCalling"
                mode = registrar.ngc if ngc else registrar.messaging
                if mode == "hang":
                    await asyncio.Event().wait()
                if mode == "fail":
                    raise OSError("registrar down")
                return type("R", (), {"status_code": 202, "raise_for_status": lambda self: None})()

        monkeypatch.setattr(S.httpx, "AsyncClient", _Http)

    @property
    def ngc_bodies(self) -> list[dict[str, Any]]:
        return [b for b in self.bodies if b["clientDescription"]["appId"] == "NextGenCalling"]


async def _noop(_req: dict[str, Any]) -> None:
    return None


async def _until_async(cond: Callable[[], bool], what: str) -> None:
    async with asyncio.timeout(5):
        while not cond():
            await asyncio.sleep(0.005)


@pytest.fixture
def no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    async def skip(self: TrouterClient) -> None:
        pass

    monkeypatch.setattr(TrouterClient, "_authenticate", skip)


async def test_ngc_is_registered_on_connect_and_on_every_reregister(
    settings: Settings, directory: Directory, monkeypatch: pytest.MonkeyPatch, no_auth: None
) -> None:
    """044-AC13"""
    reg = _Registrar(monkeypatch)
    client = _client(settings, directory, info=SURL_INFO, on_call=_noop)
    await client._on_connected()
    try:
        await _until_async(lambda: len(reg.ngc_bodies) == 1, "registered on connect")
        for n, reason in ((2, "message_loss"), (3, "ttl")):
            client._last_register -= S._REREGISTER_DEBOUNCE + 1
            await client._maybe_reregister(reason)
            await _until_async(lambda: len(reg.ngc_bodies) == n, reason)  # noqa: B023
    finally:
        for task in client._tasks:
            task.cancel()
    assert len(reg.bodies) == 6
    paths = {b["transports"]["TROUTER"][0]["path"] for b in reg.ngc_bodies}
    assert paths == {"https://surl.invalid/s/NGCallManagerWin"}
    ids = {b["registrationId"] for b in reg.ngc_bodies}
    assert len(ids) == 1 and "epid" not in ids  # one registration renewed, never the messaging one's id


async def test_without_the_hook_only_messaging_registers(
    settings: Settings, directory: Directory, monkeypatch: pytest.MonkeyPatch, no_auth: None
) -> None:
    reg = _Registrar(monkeypatch)
    client = _client(settings, directory, info=SURL_INFO)
    await client._on_connected()
    await asyncio.sleep(0.05)
    for task in client._tasks:
        task.cancel()
    assert [b["clientDescription"]["appId"] for b in reg.bodies] == [settings.registrar_app_id]


async def test_a_failed_ngc_registration_is_retried_with_backoff(
    settings: Settings, directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC13, 044-AC17: the socket stays; retries back off instead of hammering the registrar."""
    reg = _Registrar(monkeypatch)
    reg.ngc = "fail"
    monkeypatch.setattr(S, "_NGC_RETRY_MIN", 0.01)
    client = _client(settings, directory, info=SURL_INFO, on_call=_noop)
    await client._register()  # must not raise
    loop = asyncio.create_task(client._ngc_loop())
    try:
        await asyncio.sleep(0.35)  # backoff: tries at 0, .01, .03, .07, .15, .31; a flat retry: ~35
        tries = len(reg.ngc_bodies)
        assert 2 <= tries <= 8 and not client._ngc_ok
        reg.ngc = "ok"
        await _until_async(lambda: client._ngc_ok, "registered on a later retry")
        settled = len(reg.ngc_bodies)
        await asyncio.sleep(0.1)
        assert len(reg.ngc_bodies) == settled  # no more once registered
    finally:
        loop.cancel()


async def test_a_failed_messaging_reregister_still_renews_the_calling_one(
    settings: Settings, directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC13"""
    reg = _Registrar(monkeypatch)
    client = _client(settings, directory, info=SURL_INFO, on_call=_noop)
    loop = asyncio.create_task(client._ngc_loop())
    try:
        await _until_async(lambda: client._ngc_ok, "first registration")
        reg.messaging = "fail"
        with pytest.raises(OSError):
            await client._register()
        assert not client._ngc_ok
        await _until_async(lambda: client._ngc_ok and len(reg.ngc_bodies) == 2, "renewed anyway")
    finally:
        loop.cancel()


async def test_a_hanging_ngc_registrar_does_not_delay_the_keepalive(
    settings: Settings, directory: Directory, monkeypatch: pytest.MonkeyPatch, no_auth: None
) -> None:
    """044-AC13"""
    reg = _Registrar(monkeypatch)
    reg.ngc = "hang"
    monkeypatch.setattr(S, "_PING_INTERVAL", 0.01)
    client = _client(settings, directory, info=SURL_INFO, on_call=_noop)
    await asyncio.wait_for(client._on_connected(), 1)  # connect does not wait for the calling registration
    try:
        await _until_async(lambda: sum('"ping"' in f for f in client._ws.sent) >= 5, "pings keep flowing")
    finally:
        for task in client._tasks:
            task.cancel()
    assert len(reg.ngc_bodies) == 1  # still hanging


def test_ngc_registration_id_is_stable_per_endpoint(settings: Settings, directory: Directory) -> None:
    """044-AC13: reconnects renew one registration instead of piling new ones up."""
    a, b = _client(settings, directory), _client(settings, directory)
    assert a._ngc_id == b._ngc_id != "epid"
    other = TrouterClient(settings, {}, "sk", {}, "sid", "epid-2", directory)
    assert other._ngc_id != a._ngc_id


async def test_call_pushes_go_to_the_hook_only_and_its_failure_keeps_the_socket(
    settings: Settings, directory: Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """044-AC13, 044-AC17: a trigger exception is logged; the frame is acked, the socket stays."""
    events: list[Any] = []

    async def on_event(obj: dict[str, Any]) -> None:
        events.append(obj)

    def boom(*_a: Any) -> None:
        raise RuntimeError("trigger bug")

    monkeypatch.setattr(F, "handle", boom)
    client = _client(settings, directory, on_event=on_event, on_call=F.trigger(settings, Path("data")))
    with capture_logs() as logs:
        await client._on_delivery(json.dumps(CALL_PUSH))
        await _until_async(lambda: bool(logs), "the trigger's thread logged it")
    assert events == [] and client._ws.sent == ['3:::{"id":9,"status":200,"body":""}']
    assert [e["event"] for e in logs] == ["follow_trigger_failed"]


async def test_a_failing_call_hook_keeps_the_socket(settings: Settings, directory: Directory) -> None:
    """044-AC17: a raising on_call is logged; the frame is acked, the chat hook untouched."""
    events: list[Any] = []

    async def on_event(obj: dict[str, Any]) -> None:
        events.append(obj)

    async def boom(_req: dict[str, Any]) -> None:
        raise RuntimeError("hook bug")

    client = _client(settings, directory, on_event=on_event, on_call=boom)
    with capture_logs() as logs:
        await client._on_delivery(json.dumps(CALL_PUSH))
    assert events == [] and client._ws.sent == ['3:::{"id":9,"status":200,"body":""}']
    assert [(e["event"], e["error"]) for e in logs] == [("call_hook_failed", "hook bug")]
