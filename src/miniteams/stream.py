"""Trouter websocket: connect → authenticate → register → keepalive → dispatch.

Framing is **Socket.IO 0.9** (the raw `N:::` message types) — not modern socket.io, so we speak
it by hand. The websocket carries `X-Skypetoken` as a connect header; `user.authenticate` and the
registrar carry the AAD bearer in-band.

`run_forever` wraps a fresh session per attempt: new skype token / info / handshake
on every reconnect, exponential backoff, re-register on `trouter.message_loss` and on TTL. While a
session lasts, the token pair on the shared `Directory` is renewed before it expires.
"""

import asyncio
import base64
import contextlib
import json
import os
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx
import structlog
import websockets

from ._io import force_blocking_stdout
from .auth import AuthExpired, TokenSource, token_source
from .config import Settings
from .directory import Directory
from .messages import decode_body, decode_event, emit_raw_delivery, emit_raw_named, handle_delivery
from .presence import PATHS as PRESENCE_PATHS
from .skype import exchange_skype_token
from .trouter import common_query, get_or_create_epid, handshake, trouter_info

log = structlog.get_logger()

_PING_INTERVAL = 30.0  # seconds; handshake advertised a 70s heartbeat window
_REREGISTER_DEBOUNCE = 20.0  # seconds; collapse the message_loss flood into one re-register
_NGC_RETRY_MIN = 30.0  # seconds before retrying a failed calling registration; doubles up to the TTL
_BACKOFF_MAX = 60.0
_STABLE_AFTER = 60.0  # a connection alive this long resets the backoff
_TOKEN_REFRESH_RATIO = 0.8  # renew the directory's skype token at 80% of its lifetime
_TOKEN_REFRESH_MIN = 60.0  # floor between renewals; a failed renewal retries after this
_LOGIN_POLL = 5.0  # seconds between looks at the token cache while waiting for a `miniteams login`
_TOKEN_LIFETIME_FALLBACK = 1800.0  # authz response without expiresIn: assume a short lifetime
_CONNECT_FLOOD = 30.0  # seconds after registration in which a message_loss is the connect flood
# Calling registration (spec 044): call pushes arrive on the surl + this path, never on the messaging one.
NGC_PATH = "NGCallManagerWin"
_NGC_CLIENT = {
    "appId": "NextGenCalling",
    "aesKey": "",
    "languageId": "en-US",
    "platform": "edge",
    "templateKey": "DesktopNgc_2.5:SkypeNgc",
    "platformUIVersion": "49/1.0.0",
}

EventHook = Callable[[dict[str, Any]], Awaitable[None]]  # receives each decoded EventMessage
GapHook = Callable[[str], Awaitable[None]]  # events may have been missed; arg = reason
AliveHook = Callable[[], Awaitable[None]]  # the socket is up: at connect and on every keepalive
SessionHook = Callable[[str, str], Awaitable[None]]  # (endpoint id, surl) of a registered socket
PresenceHook = Callable[[dict[str, Any]], Awaitable[None]]  # a decoded presence delivery body
CallHook = Callable[[dict[str, Any]], Awaitable[None]]  # a raw NGCallManagerWin delivery (spec 044)
AGENT_PATH = "/callAgent/"  # a joined call's own callbacks (spec 044), on the surl of the socket that joined
# The Teams session: up, down (reconnecting) or login (waiting for a `miniteams login`).
LinkState = Literal["up", "down", "login"]
LinkHook = Callable[[LinkState], None]
ErrorHook = Callable[[str, BaseException], None]  # a failed attempt: ("sign-in" | "Trouter", its error)


async def off_loop[T](fn: Callable[..., T], *args: Any, name: str = "") -> T:
    """`fn(*args)` in a daemon thread: the event loop keeps running, and a Ctrl-C or a stop does not wait
    for a hung HTTP call to time out (asyncio.to_thread's executor threads are joined at exit)."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[T] = loop.create_future()

    def settle(ok: bool, value: Any) -> None:
        if fut.done():  # the awaiting task is gone (cancelled)
            return
        if ok:
            fut.set_result(value)
        else:
            fut.set_exception(value)

    def run() -> None:
        try:
            result: Any = (True, fn(*args))
        except BaseException as exc:  # noqa: BLE001 — handed to the awaiting task
            result = (False, exc)
        try:
            loop.call_soon_threadsafe(settle, *result)
        except RuntimeError:  # the loop closed meanwhile: nobody waits for it
            pass

    label = name or f"off-loop-{getattr(fn, '__name__', 'call')}"
    threading.Thread(target=run, name=label, daemon=True).start()
    return await fut


def _correlation_vector() -> str:
    """22-char base64-ish token (purple-teams teams_generate_correlation_vector)."""
    return base64.b64encode(os.urandom(16)).decode().rstrip("=")[:22]


def ack(req: dict[str, Any], headers: dict[str, str] | None = None) -> str:
    """The 200 a delivery is answered with."""
    out: dict[str, Any] = {"id": req.get("id"), "status": 200}
    if headers is not None:
        out["headers"] = headers
    out["body"] = ""
    return json.dumps(out, separators=(",", ":"))


def agent_ack(req: dict[str, Any]) -> str:
    """A call callback's response: without the request's MS-CV + ".0", its trouter-request and a
    trouter-client header, the call controller redelivers and then ends the call with 408/10056."""
    headers = {str(k).lower(): v for k, v in (req.get("headers") or {}).items()}
    out = {}
    if cv := headers.get("ms-cv"):
        out["MS-CV"] = f"{cv}.0"
    if treq := headers.get("trouter-request"):
        out["trouter-request"] = treq
    out["trouter-client"] = '{"cd":1}'
    return ack(req, out)


class TrouterClient:
    def __init__(
        self,
        settings: Settings,
        aad: dict[str, Any],
        skype_token: str,
        info: dict[str, Any],
        session_id: str,
        epid: str,
        directory: Directory,
        jsonl: bool = False,
        raw: bool = False,
        typing: bool = False,
        on_event: EventHook | None = None,
        on_gap: GapHook | None = None,
        on_alive: AliveHook | None = None,
        on_session: SessionHook | None = None,
        on_presence: PresenceHook | None = None,
        on_call: CallHook | None = None,
        on_agent: CallHook | None = None,
    ) -> None:
        self.settings = settings
        self.aad = aad
        self.skype_token = skype_token
        self.info = info
        self.session_id = session_id
        self.epid = epid
        self.directory = directory
        self.jsonl = jsonl
        self.raw = raw
        self.typing = typing
        self.on_event = on_event  # set → events go to the hook instead of stdout
        self.on_gap = on_gap
        self.on_alive = on_alive
        self.on_session = on_session
        self.on_presence = on_presence
        self.on_call = on_call  # set → also registers on NGC_PATH, its pushes go to this hook only
        self.on_agent = on_agent  # set → AGENT_PATH deliveries go to this hook only
        # Stable per endpoint, so reconnects renew one registration; never the epid, which would replace
        # the messaging registration.
        self._ngc_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"miniteams:ngc:{epid}"))
        self._ngc_ok = False
        self._ngc_wake = asyncio.Event()  # set by every (re-)register: _ngc_loop renews the calling one
        self._loss_etag: str | None = None  # `messaging` etag of the last message_loss seen
        self._count = 0
        self._last_register = 0.0
        # websockets' connection type churns across releases; keep it loose deliberately.
        self._ws: Any = None
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def _bearer(self) -> str:
        # Never the id_token a real refresh returns: the profile lookup answers it 401.
        # Prefer the Directory's pair: the refresher renews it, the connect-time `aad` expires in ~1h.
        return self.directory.bearer or str(self.aad["access_token"])

    def _ws_url(self) -> str:
        wss = self.info["socketio"].replace("https://", "wss://", 1)
        query = common_query(self.settings, self.info["connectparams"], self.epid)
        return f"{wss}socket.io/1/websocket/{self.session_id}?v=v4&{query}"

    # --- frame senders (Socket.IO 0.9) ---

    async def _send_regular(self, payload: dict[str, Any]) -> None:
        self._count += 1
        await self._ws.send(f"5:{self._count}+::{json.dumps(payload, separators=(',', ':'))}")

    async def _send_ephemeral(self, payload: dict[str, Any]) -> None:
        await self._ws.send(f"5:::{json.dumps(payload, separators=(',', ':'))}")

    # --- post-connect choreography ---

    async def _authenticate(self) -> None:
        await self._send_ephemeral(
            {
                "name": "user.authenticate",
                "args": [
                    {
                        "headers": {
                            "X-Ms-Test-User": "False",
                            "Authorization": f"Bearer {self._bearer}",
                            "X-MS-Migration": "True",
                        },
                        "connectparams": self.info["connectparams"],
                    }
                ],
            }
        )
        await self._send_regular(
            {"name": "user.activity", "args": [{"state": "active", "cv": f"{_correlation_vector()}.0.1"}]}
        )

    async def _register(self) -> None:
        # Before the messaging POST, which may raise: the calling registration is renewed regardless.
        self._ngc_ok = False
        self._ngc_wake.set()
        body = {
            "clientDescription": {
                "appId": self.settings.registrar_app_id,
                "aesKey": "",
                "languageId": "en-US",
                "platform": "edge",
                "templateKey": self.settings.registrar_template_key,
                "platformUIVersion": self.settings.clientinfo_version,
            },
            "registrationId": self.epid,
            "nodeId": "",
            "transports": {
                "TROUTER": [{"context": "", "path": self.info["surl"], "ttl": self.settings.trouter_ttl}]
            },
        }
        status = await self._post_registration(body)
        self._last_register = time.monotonic()
        log.info("registered", status=status, surl=self.info["surl"])

    async def _register_ngc(self) -> None:
        path = self.info["surl"] + NGC_PATH
        body = {
            "clientDescription": _NGC_CLIENT,
            "registrationId": self._ngc_id,
            "nodeId": "",
            "transports": {"TROUTER": [{"context": "", "path": path, "ttl": self.settings.trouter_ttl}]},
        }
        try:
            status = await self._post_registration(body)
        except Exception as exc:  # noqa: BLE001 — the call trigger must not cost the chat socket
            self._ngc_ok = False  # _ngc_loop retries it
            log.warning("ngc_register_failed", error=str(exc), error_type=type(exc).__name__)
            return
        self._ngc_ok = True
        log.info("ngc_registered", status=status)

    async def _post_registration(self, body: dict[str, Any]) -> int:
        headers = {
            "X-Skypetoken": self.directory.skype_token or self.skype_token,
            "Authorization": f"Bearer {self._bearer}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(self.settings.registrar_url, headers=headers, json=body)
            resp.raise_for_status()
        return int(resp.status_code)

    async def _maybe_reregister(self, reason: str) -> None:
        if time.monotonic() - self._last_register < _REREGISTER_DEBOUNCE:
            return
        log.info("reregister", reason=reason)
        try:
            await self._register()
        except Exception as exc:  # noqa: BLE001 — a failed re-register shouldn't kill the socket
            log.warning("reregister_failed", error=str(exc))

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(_PING_INTERVAL)
            await self._send_regular({"name": "ping"})
            log.debug("ping_sent", count=self._count)
            await self._alive()

    async def _ngc_loop(self) -> None:
        """The calling registration, off the frame and ping paths: a hanging registrar delays neither."""
        delay = _NGC_RETRY_MIN
        while True:
            self._ngc_wake.clear()
            if not self._ngc_ok:
                await self._register_ngc()
            wait = None if self._ngc_ok else delay  # a failure retries on the timeout, backing off
            delay = _NGC_RETRY_MIN if self._ngc_ok else min(delay * 2, float(self.settings.trouter_ttl))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._ngc_wake.wait(), wait)

    async def _alive(self) -> None:
        if self.on_alive is None:
            return
        try:
            await self.on_alive()
        except Exception as exc:  # noqa: BLE001 — a liveness note must not drop the socket
            log.warning("alive_hook_failed", error=str(exc))

    async def _ttl_loop(self) -> None:
        interval = max(self.settings.trouter_ttl - 10, 60)
        while True:
            await asyncio.sleep(interval)
            await self._maybe_reregister("ttl")

    async def _on_connected(self) -> None:
        log.info("ws_connected")
        await self._authenticate()
        await self._register()
        if self.on_gap is not None:
            await self.on_gap("connected")  # whatever happened while disconnected is unknown
        if self.on_session is not None:
            try:
                await self.on_session(self.epid, self.info["surl"])
            except Exception as exc:  # noqa: BLE001 — presence must not drop the chat socket
                log.warning("session_hook_failed", error=str(exc), error_type=type(exc).__name__)
        await self._alive()
        self._tasks = [
            asyncio.create_task(self._ping_loop()),
            asyncio.create_task(self._ttl_loop()),
        ]
        if self.on_call is not None:
            self._tasks.append(asyncio.create_task(self._ngc_loop()))

    async def _on_named(self, data: str) -> None:
        if self.raw:
            await emit_raw_named(data)
        try:
            evt = json.loads(data)
        except json.JSONDecodeError:
            return
        name = evt.get("name")
        if name == "trouter.message_loss":
            # Before re-registering: `_on_loss` tells the connect flood by the registration time.
            await self._on_loss(evt)
            # Backend floods this until we re-register the messaging worker.
            await self._maybe_reregister("message_loss")
        elif not self.raw:
            log.info("named_event", name=name)

    async def _on_loss(self, evt: dict[str, Any]) -> None:
        """A new `messaging` etag is a real loss. Every connect opens with a flood that repeats
        one etag (the registration instant); the connect gap already covers that one."""
        if self.on_gap is None:
            return
        etag = next(
            (
                str(d.get("etag"))
                for arg in evt.get("args") or []
                if isinstance(arg, dict)
                for d in arg.get("droppedIndicators") or []
                if isinstance(d, dict) and d.get("tag") == "messaging"
            ),
            None,
        )
        if etag is None or etag == self._loss_etag:
            return
        flood = self._loss_etag is None and time.monotonic() - self._last_register < _CONNECT_FLOOD
        self._loss_etag = etag
        if not flood:
            await self.on_gap("message_loss")

    async def _on_delivery(self, data: str) -> None:
        """Inbound `3:::` pseudo-HTTP request. ALWAYS ack 200, then dispatch by url."""
        try:
            req = json.loads(data)
        except json.JSONDecodeError:
            log.warning("delivery_parse_failed", data=data[:200])
            return
        agent = self.on_agent is not None and AGENT_PATH in str(req.get("url") or "")
        # Ack regardless of whether we print anything — missing acks cause redelivery/disconnect.
        await self._ws.send(f"3:::{agent_ack(req) if agent else ack(req)}")
        if agent and self.on_agent is not None:
            try:
                await self.on_agent(req)
            except Exception as exc:  # noqa: BLE001 — a bad callback must not drop the socket
                log.warning("agent_hook_failed", error=str(exc), error_type=type(exc).__name__)
        elif self.raw:
            await emit_raw_delivery(req)  # every endpoint, decoded, no filtering
        elif self.on_call is not None and str(req.get("url") or "").endswith(NGC_PATH):
            try:
                await self.on_call(req)
            except Exception as exc:  # noqa: BLE001 — a bad call push must not drop the socket
                log.warning("call_hook_failed", error=str(exc), error_type=type(exc).__name__)
        elif self.on_presence is not None and str(req.get("url") or "").endswith(PRESENCE_PATHS):
            try:
                await self.on_presence(decode_body(req) or {})
            except Exception as exc:  # noqa: BLE001 — a bad presence frame must not drop the socket
                log.warning("presence_hook_failed", error=str(exc), error_type=type(exc).__name__)
        elif self.on_event is not None:
            obj = decode_event(req)
            if obj is None:
                return
            try:
                await self.on_event(obj)
            except Exception as exc:  # noqa: BLE001 — a bad event must not drop the socket
                log.warning("hook_failed", error=str(exc), error_type=type(exc).__name__)
        else:
            await handle_delivery(req, self.directory, self.jsonl, self.typing)

    async def _handle_frame(self, raw: str | bytes) -> None:
        frame = raw.decode() if isinstance(raw, bytes) else raw
        parts = frame.split(":", 3)
        ftype = parts[0]
        data = parts[3] if len(parts) > 3 else ""

        if ftype == "1":
            await self._on_connected()
        elif ftype == "2":
            await self._ws.send("2::")  # heartbeat echo
        elif ftype == "3":
            await self._on_delivery(data)
        elif ftype == "5":
            await self._on_named(data)
        elif ftype == "6":
            pass  # ack to one of our sends — noop
        elif ftype == "7":
            log.warning("ws_error_frame", frame=frame[:200])
        elif ftype == "0":
            log.warning("ws_disconnect_frame")
        else:
            log.debug("ws_frame", frame=frame[:200])

    async def run(self) -> None:
        url = self._ws_url()
        log.info("ws_connecting", session_id=self.session_id)
        try:
            async with websockets.connect(
                url,
                additional_headers={"X-Skypetoken": self.skype_token},
                user_agent_header=self.settings.user_agent,
                max_size=None,
            ) as ws:
                self._ws = ws
                async for raw in ws:
                    await self._handle_frame(raw)
        finally:
            for task in self._tasks:
                task.cancel()
            self._tasks = []
        log.info("ws_closed")


def _lifetime(skype: dict[str, Any]) -> float:
    return float(skype.get("expires_in") or _TOKEN_LIFETIME_FALLBACK)


async def _refresh_directory_token(
    settings: Settings,
    tokens: TokenSource,
    directory: Directory,
    lifetime: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """A websocket outlives the ~1h skype token; without this every lookup and re-register 401s after."""
    delay = lifetime * _TOKEN_REFRESH_RATIO
    while True:
        await sleep(max(_TOKEN_REFRESH_MIN, delay))
        try:
            aad = await off_loop(tokens.refresh)
            skype = await off_loop(exchange_skype_token, settings, aad["access_token"])
        except AuthExpired as exc:
            # Ours may only be stale: a sibling (archive, login) rotates the token on disk, and the
            # session can outlive this by hours, every call 401ing on the token we stopped renewing.
            log.error("directory_token_refresh_waiting", error=str(exc))
            await _wait_for_login(tokens, exc.stamp, sleep)
            delay = 0.0
            continue
        except Exception as exc:  # noqa: BLE001 — transient (network, authz 5xx): retry after the floor
            log.warning("directory_token_refresh_failed", error=str(exc), error_type=type(exc).__name__)
            delay = _TOKEN_REFRESH_MIN
            continue
        directory.set_token(skype["skype_token"], str(aad["access_token"]))
        lifetime = _lifetime(skype)
        delay = lifetime * _TOKEN_REFRESH_RATIO
        log.info("directory_token_refreshed", expires_in=lifetime)


async def _wait_for_login(
    tokens: TokenSource,
    since: tuple[int, int, int],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Return once another process (`miniteams login`, a sibling's refresh) has written the token cache
    since the version `since` names, including a write that landed during the failed refresh."""
    while not tokens.written_since(since):
        await sleep(_LOGIN_POLL)


async def run_forever(
    settings: Settings,
    jsonl: bool = False,
    raw: bool = False,
    typing: bool = False,
    on_event: EventHook | None = None,
    directory: Directory | None = None,
    epid_name: str = "endpoint_id",
    on_gap: GapHook | None = None,
    on_alive: AliveHook | None = None,
    epid: str | None = None,
    prompt: bool = True,
    wait_for_login: bool = False,
    on_session: SessionHook | None = None,
    on_presence: PresenceHook | None = None,
    on_link: LinkHook | None = None,
    on_call: CallHook | None = None,
    on_agent: CallHook | None = None,
    on_error: ErrorHook | None = None,
) -> None:
    """Re-establish a full session on every disconnect.

    A fresh skype token / trouter info / handshake is minted per attempt, so surl/session rotation
    is handled by reconnecting. A connected session outlives the token, so a background task renews
    the `Directory` token pair (lookups, re-registers) meanwhile. Backoff grows on rapid failures
    and resets once a connection has been stable.
    """
    force_blocking_stdout()  # inside the running loop (see _io); guards `--jsonl | jq` backpressure
    directory = directory or Directory(settings)  # caches survive reconnects; only the tokens change
    # Authenticate ONCE up front (may prompt: device-code in stream mode). Reconnects then only
    # refresh silently — never re-prompt — so a failed connect can't spin into endless logins.
    tokens = token_source(settings)
    if prompt:
        # In this thread: it may prompt, and Ctrl-C must reach it at once.
        tokens.acquire()
    else:
        # Silent only (a watch owns stdout and cannot show a device code), off the event loop like the
        # calls below: a host's other tasks (a call's media) keep running.
        await off_loop(tokens.refresh)

    def link(state: LinkState) -> None:
        if on_link is None:
            return
        try:
            on_link(state)
        except Exception as exc:  # noqa: BLE001 — a page note must not stop the session loop
            log.warning("link_hook_failed", error=str(exc), error_type=type(exc).__name__)

    async def alive() -> None:
        link("up")  # called once registered, then on every keepalive
        if on_alive is not None:
            await on_alive()

    backoff = 1.0
    while True:
        connected_at: float | None = None
        step = "sign-in"
        try:
            # Silent; AuthExpired when the refresh token is dead.
            aad = await off_loop(tokens.refresh)
            skype = await off_loop(exchange_skype_token, settings, aad["access_token"])
            step = "Trouter"
            skype_token = skype["skype_token"]
            directory.set_token(skype_token, str(aad["access_token"]))
            epid = epid or get_or_create_epid(settings, epid_name)
            info = await off_loop(trouter_info, settings, skype_token, epid)
            session_id = await off_loop(handshake, settings, info, skype_token, epid)
            connected_at = time.monotonic()
            refresher = asyncio.create_task(
                _refresh_directory_token(settings, tokens, directory, _lifetime(skype))
            )
            try:
                await TrouterClient(
                    settings,
                    aad,
                    skype_token,
                    info,
                    session_id,
                    epid,
                    directory,
                    jsonl,
                    raw,
                    typing,
                    on_event,
                    on_gap,
                    on_alive=alive,
                    on_session=on_session,
                    on_presence=on_presence,
                    on_call=on_call,
                    on_agent=on_agent,
                ).run()
            finally:
                refresher.cancel()
        except asyncio.CancelledError:
            raise
        except BrokenPipeError:
            # Downstream reader (head/jq/…) closed stdout — stop, don't reconnect into a dead pipe.
            log.info("stdout_closed")
            return
        except AuthExpired as exc:
            # Refresh token dead: reconnecting can't fix it and re-auth needs a prompt — stop
            # cleanly so the user re-runs, rather than spinning the loop forever.
            log.error("auth_expired", error=str(exc), waiting_for_login=wait_for_login)
            if not wait_for_login:
                return
            link("login")
            # The next refresh rereads the rewritten cache (_sync_cache).
            await _wait_for_login(tokens, exc.stamp)
            log.info("login_detected")
            backoff = 1.0
            continue
        except Exception as exc:  # noqa: BLE001 — any other failure is recoverable via reconnect
            log.warning("stream_error", step=step, error=str(exc), error_type=type(exc).__name__)
            if on_error is not None:
                try:
                    on_error(step, exc)
                except Exception as hook_exc:  # noqa: BLE001 — a failure note must not stop the session loop
                    log.warning("error_hook_failed", error=str(hook_exc), error_type=type(hook_exc).__name__)

        if connected_at is not None and time.monotonic() - connected_at > _STABLE_AFTER:
            backoff = 1.0
        link("down")
        log.info("reconnecting", in_seconds=round(backoff, 1))
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _BACKOFF_MAX)
