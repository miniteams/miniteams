"""`miniteams web`: one local page listing conversations newest-activity-first (spec 002).

One process, one port: `websockets.serve` answers `GET /` with the bundled page and upgrades
`/ws`, over which the server pushes the whole row list (small: `--limit` rows) on connect and
on every change. No REST layer — the only client verb (`seen`) rides the same socket.

Bind is `127.0.0.X:PORT` with X and PORT drawn once and persisted (`web.json`) so the URL
survives restarts; the whole 127/8 is loopback on Linux, no interface setup needed.
"""

import asyncio
import ipaddress
import json
import random
from collections.abc import Iterable
from datetime import UTC, datetime
from http import HTTPStatus
from importlib.resources import files
from typing import Any

import structlog
from websockets.asyncio.server import ServerConnection, broadcast, serve
from websockets.http11 import Request, Response
from websockets.typing import Origin

from .chats import fetch_conversations, is_meeting, is_private, last_activity
from .config import Settings
from .directory import Directory
from .messages import EMOJI, strip_html, thread_of
from .stream import run_forever

log = structlog.get_logger()

_SNIPPET_LEN = 140
_LABEL_CONCURRENCY = 8
_TYPING_TTL = 10.0  # seconds; Teams does not always send ClearTyping
_BIND_ATTEMPTS = 3  # redraws when the persisted port turns out taken
# Non-text last messages: a short marker instead of the raw HTML/card payload.
_TYPE_MARKERS = {
    "RichText/Media_GenericFile": "📎 file",
    "RichText/UriObject": "🖼 image",
    "RichText/Media_Card": "🃏 card",
    "RichText/Media_CallRecording": "🎥 recording",
    "RichText/Media_LocalRecording": "🎥 recording",
    "ThreadActivity/AddMember": "👥 member added",
    "ThreadActivity/DeleteMember": "👥 member removed",
    "Event/Call": "📞 call",
}


def in_scope(thread_id: str) -> bool:
    """V1 widget scope = private 1:1/group + meeting chats (same default set as `archive`)."""
    return is_private(thread_id) or is_meeting(thread_id)


def snippet(msgtype: str, content: str) -> str:
    """One-line preview of a message body, marker for non-text types, capped at _SNIPPET_LEN."""
    if msgtype in ("RichText/Html", "Text"):
        text = strip_html(content) if msgtype == "RichText/Html" else content.strip()
        if not text:
            # The list stub of a deleted message keeps its type but loses body and properties;
            # a body that strips to nothing is an image/attachment tag.
            text = "🗑 deleted" if not content.strip() else "🖼 image" if "<img" in content else "📎 attachment"
    else:
        text = (_TYPE_MARKERS.get(msgtype) or f"[{msgtype.rsplit('/', 1)[-1]}]") if msgtype else ""
    text = " ".join(text.split())
    return text if len(text) <= _SNIPPET_LEN else text[: _SNIPPET_LEN - 1] + "…"


async def row_from_conversation(conv: dict[str, Any], directory: Directory) -> dict[str, Any]:
    thread_id = str(conv.get("id") or "")
    last = conv.get("lastMessage") or {}
    sender_mri = str(last.get("from") or "")
    directory.note_name(sender_mri, last.get("imdisplayname"))
    sender = last.get("imdisplayname") or (await directory.display(sender_mri) if sender_mri else "")
    return {
        "id": thread_id,
        "label": await directory.label(thread_id),
        "last_activity": last_activity(conv),
        "last_id": str(last.get("id") or ""),
        "sender": sender,
        "text": snippet(str(last.get("messagetype") or ""), str(last.get("content") or "")),
        "seen_at": None,
        "typing": [],
    }


async def bootstrap(
    pages: Iterable[list[dict[str, Any]]], directory: Directory, limit: int
) -> dict[str, dict[str, Any]]:
    """In-scope conversations from the newest-first listing, capped at `limit` (0 = walk all)."""
    picked: list[dict[str, Any]] = []
    for page in pages:
        for conv in page:
            thread_id = str(conv.get("id") or "")
            if thread_id and in_scope(thread_id) and last_activity(conv):
                picked.append(conv)
            if limit and len(picked) >= limit:
                break
        if limit and len(picked) >= limit:
            break
    # Labels cost one thread fetch each; sequential that is ~80s for 50 rows. Bounded fan-out
    # keeps it a few seconds without hammering the chat service.
    gate = asyncio.Semaphore(_LABEL_CONCURRENCY)

    async def build(conv: dict[str, Any]) -> dict[str, Any]:
        async with gate:
            return await row_from_conversation(conv, directory)

    rows = await asyncio.gather(*(build(conv) for conv in picked))
    return {row["id"]: row for row in rows}


# --- bind address ---


def parse_bind(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"expected ADDR:PORT, got {value!r}")
    # The page has no auth: anything but loopback would publish the chat list to the network.
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError(f"refusing non-loopback bind {host!r}")
    return host, int(port)


def _draw_bind() -> tuple[str, int]:
    # 127.0.0.1 is what every other local daemon binds; a random sibling avoids collisions and
    # keeps the widget off the well-known address (user request).
    return f"127.0.0.{random.randint(2, 254)}", random.randint(32768, 60999)


def load_bind(settings: Settings, override: str | None = None) -> tuple[str, int]:
    """`--bind` wins; else the persisted draw; else draw now and persist."""
    if override:
        return parse_bind(override)
    path = settings.config_dir / "web.json"
    try:
        data = json.loads(path.read_text())
        return str(data["host"]), int(data["port"])
    except OSError, ValueError, KeyError, TypeError:
        pass
    host, port = _draw_bind()
    save_bind(settings, host, port)
    return host, port


def save_bind(settings: Settings, host: str, port: int) -> None:
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    (settings.config_dir / "web.json").write_text(json.dumps({"host": host, "port": port}))


# --- server ---


class Board:
    """Row table + connected pages. Every mutation ends in a broadcast of the full sorted list."""

    def __init__(
        self,
        rows: dict[str, dict[str, Any]],
        directory: Directory | None = None,
        reactions: bool = False,
        typing_ttl: float = _TYPING_TTL,
    ) -> None:
        self.rows = rows
        self.directory = directory
        self.reactions = reactions
        self.typing_ttl = typing_ttl
        self.clients: set[ServerConnection] = set()
        self._typing_timers: dict[tuple[str, str], asyncio.TimerHandle] = {}

    # --- live events (stream hook) ---

    async def on_event(self, obj: dict[str, Any]) -> None:
        resource = obj.get("resource") or {}
        thread_id = thread_of(resource)
        resource_type = obj.get("resourceType")
        msgtype = str(resource.get("messagetype") or "")
        log.debug("web_event", thread=thread_id, resource_type=resource_type, messagetype=msgtype)
        if not in_scope(thread_id):
            return
        if resource_type == "NewMessage":
            if msgtype in ("Control/Typing", "Control/ClearTyping"):
                await self._typing(thread_id, resource, started=msgtype == "Control/Typing")
            elif not msgtype.startswith("Control/"):  # read receipts & co carry no row change
                await self._new_message(thread_id, resource, msgtype)
        elif resource_type == "MessageUpdate":
            await self._message_update(thread_id, resource, msgtype)

    async def _sender(self, resource: dict[str, Any]) -> str:
        mri = str(resource.get("from") or "")
        name = resource.get("imdisplayname")
        if self.directory is None:
            return str(name or mri)
        self.directory.note_name(mri, name)
        return str(name) if name else await self.directory.display(mri)

    async def _new_message(self, thread_id: str, resource: dict[str, Any], msgtype: str) -> None:
        row = self.rows.get(thread_id)
        if row is None:
            label = await self.directory.label(thread_id) if self.directory else thread_id
            row = self.rows[thread_id] = {"id": thread_id, "label": label, "seen_at": None, "typing": []}
        sender = await self._sender(resource)
        row.update(
            last_activity=str(resource.get("composetime") or resource.get("originalarrivaltime") or ""),
            last_id=str(resource.get("id") or ""),
            sender=sender,
            text=snippet(msgtype, str(resource.get("content") or "")),
        )
        self._typing_stop(thread_id, sender)  # their message is the end of their typing
        self.broadcast()

    async def _message_update(self, thread_id: str, resource: dict[str, Any], msgtype: str) -> None:
        row = self.rows.get(thread_id)
        props = resource.get("properties") or {}
        if props.get("emotions") is not None:
            if self.reactions and row is not None:
                await self._reaction(row, resource, props["emotions"])
            return
        if row is None or row.get("last_id") != str(resource.get("id") or ""):
            return  # edit/delete of an older message: the row shows the latest one, unchanged
        if props.get("deletetime"):
            row["text"] = "🗑 deleted"
        elif resource.get("skypeeditedid") or props.get("edittime"):
            row["text"] = snippet(msgtype, str(resource.get("content") or ""))
        else:
            return
        self.broadcast()

    async def _reaction(self, row: dict[str, Any], resource: dict[str, Any], emotions: list[Any]) -> None:
        """Newest added reaction becomes the row's last event and bumps it (opt-in, `--reactions`)."""
        msg_id = str(resource.get("id") or resource.get("clientmessageid") or "")
        latest: tuple[int, str, str] | None = None  # (time_ms, emoji, mri)
        for emotion in emotions:
            key = emotion.get("key", "?")
            users = emotion.get("users") or []
            added, _ = (
                self.directory.reaction_diff(msg_id, key, [str(u.get("mri", "")) for u in users])
                if self.directory
                else ({str(u.get("mri", "")) for u in users}, set())
            )
            for u in users:
                mri = str(u.get("mri", ""))
                if mri in added:
                    candidate = (int(u.get("time") or 0), EMOJI.get(key, f":{key}:"), mri)
                    latest = max(latest, candidate) if latest else candidate
        if latest is None:
            return
        time_ms, emoji, mri = latest
        who = await self.directory.display(mri) if self.directory else mri
        when = datetime.fromtimestamp(time_ms / 1000, tz=UTC) if time_ms else datetime.now(UTC)
        row.update(
            last_activity=when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            sender="",
            text=f"{emoji} {who} reacted",
        )
        self.broadcast()

    async def _typing(self, thread_id: str, resource: dict[str, Any], started: bool) -> None:
        row = self.rows.get(thread_id)
        if row is None:
            return  # a thread we never listed: nothing to annotate
        name = await self._sender(resource)
        if not started:
            self._typing_stop(thread_id, name)
            self.broadcast()
            return
        if name not in row["typing"]:
            row["typing"].append(name)
        key = (thread_id, name)
        if key in self._typing_timers:
            self._typing_timers[key].cancel()
        loop = asyncio.get_running_loop()
        self._typing_timers[key] = loop.call_later(self.typing_ttl, self._typing_expired, thread_id, name)
        self.broadcast()

    def _typing_stop(self, thread_id: str, name: str) -> None:
        timer = self._typing_timers.pop((thread_id, name), None)
        if timer:
            timer.cancel()
        row = self.rows.get(thread_id)
        if row and name in row["typing"]:
            row["typing"].remove(name)

    def _typing_expired(self, thread_id: str, name: str) -> None:
        self._typing_stop(thread_id, name)
        self.broadcast()

    # --- pages ---

    def payload(self) -> str:
        ordered = sorted(self.rows.values(), key=lambda r: r["last_activity"], reverse=True)
        return json.dumps({"rows": ordered}, ensure_ascii=False)

    def broadcast(self) -> None:
        # Library helper: never awaits a slow page; a client with a full buffer just misses one
        # frame (the next push carries the full list anyway).
        broadcast(self.clients, self.payload())

    async def handle(self, ws: ServerConnection) -> None:
        self.clients.add(ws)
        try:
            await ws.send(self.payload())
            async for raw in ws:
                log.debug("ws_client_message", data=str(raw)[:100])  # `seen` lands here (phase 3)
        finally:
            self.clients.discard(ws)


def _page() -> str:
    return files("miniteams").joinpath("widget.html").read_text(encoding="utf-8")


def _process_request(ws: ServerConnection, request: Request) -> Response | None:
    if request.path == "/ws":
        return None  # proceed with the websocket upgrade
    if request.path == "/":
        resp = ws.respond(HTTPStatus.OK, _page())
        del resp.headers["Content-Type"]  # respond() stamps text/plain; Headers is a multidict
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        return resp
    return ws.respond(HTTPStatus.NOT_FOUND, "not found\n")


async def serve_board(board: Board, settings: Settings, bind: str | None) -> None:
    host, port = load_bind(settings, bind)
    for attempt in range(_BIND_ATTEMPTS):
        try:
            # Only the page we serve may open the socket — any other site's Origin gets 403,
            # else every open tab could read the chat list (cross-site websocket hijacking).
            origins = [Origin(f"http://{host}:{port}")]
            async with serve(board.handle, host, port, origins=origins, process_request=_process_request):
                log.info("web_listening", url=f"http://{host}:{port}/")
                await asyncio.Future()  # until cancelled
        except OSError as exc:
            if bind or attempt == _BIND_ATTEMPTS - 1:
                raise
            # Persisted port got taken by something else since the draw — redraw, keep going.
            log.warning("bind_failed", host=host, port=port, error=str(exc))
            host, port = _draw_bind()
            save_bind(settings, host, port)


async def run(
    settings: Settings, skype_token: str, bearer: str, limit: int, bind: str | None, reactions: bool = False
) -> None:
    directory = Directory(settings)
    directory.set_token(skype_token, bearer)
    rows = await bootstrap(fetch_conversations(settings, skype_token), directory, limit)
    log.info("web_bootstrap", rows=len(rows))
    board = Board(rows, directory, reactions=reactions)
    # The stream only returns when auth is dead: a page that silently stops updating is worse
    # than an exit, so the server goes down with it and the user re-runs.
    server = asyncio.create_task(serve_board(board, settings, bind))
    stream = asyncio.create_task(run_forever(settings, on_event=board.on_event, directory=directory))
    done, pending = await asyncio.wait({server, stream}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        task.result()  # re-raise a server failure (e.g. bind) instead of exiting 0
    raise RuntimeError("live stream ended (auth expired?) — re-run")
