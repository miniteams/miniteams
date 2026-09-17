"""`miniteams web`: one local page listing conversations newest-activity-first (spec 002).

One process, one port: `websockets.serve` answers `GET /` with the bundled page and upgrades
`/ws`, over which the server pushes the whole row list (small: `--limit` rows) on connect and
on every change. No REST layer — the only client verb (`seen`) rides the same socket.

Bind is `127.0.0.X:PORT` with X and PORT drawn once and persisted (`web.json`) so the URL
survives restarts; the whole 127/8 is loopback on Linux, no interface setup needed.
"""

import asyncio
import html
import ipaddress
import json
import random
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import quote

import structlog
from websockets.asyncio.server import ServerConnection, broadcast, serve
from websockets.http11 import Request, Response
from websockets.typing import Origin

from .chats import fetch_conversations, is_meeting, is_private, last_activity
from .config import Settings
from .directory import Directory
from .dump import fetch_history
from .messages import _TAG_RE, EMOJI, thread_of
from .stream import run_forever

log = structlog.get_logger()

_SNIPPET_LEN = 140
_EMOJI_ALT_RE = re.compile(r'<emoji\b[^>]*\balt="([^"]*)"[^>]*>')
_LABEL_CONCURRENCY = 8
_STUB_PAGE = 5  # history page fetched to resolve an ambiguous listing stub
_PAGE_POLL = 2.0  # seconds between widget.html mtime checks (dev reload)
_TYPING_TTL = 10.0  # seconds; Teams does not always send ClearTyping
# Thread activity that changes the label (topic, member count): drop the cached thread info.
# Someone reading a chat is not activity: bumping the row on it would defeat "seen".
_SILENT_TYPES = {"ThreadActivity/MemberConsumptionHorizonUpdate"}
_RELABEL_TYPES = {"ThreadActivity/TopicUpdate", "ThreadActivity/AddMember", "ThreadActivity/DeleteMember"}
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
    "ThreadActivity/TopicUpdate": "✎ renamed",
    "ThreadActivity/MemberConsumptionHorizonUpdate": "👁 read marker",
    "Event/Call": "📞 call",
}


def read_up_to(props: dict[str, Any] | None) -> str:
    """Id of the newest message Teams considers read (`consumptionhorizon` = "<id>;<ts>;<x>")."""
    horizon = str((props or {}).get("consumptionhorizon") or "")
    return horizon.split(";", 1)[0]


def is_unread(last_id: str, read_id: str) -> bool:
    """Message ids are ms-epoch strings: newer than the horizon ⇒ unread (on every device)."""
    if not last_id or not read_id:
        return False  # nothing known: do not shout
    if last_id.isdigit() and read_id.isdigit():
        return int(last_id) > int(read_id)
    return last_id > read_id


def deep_link(thread_id: str, msg_id: str, scheme: str = "https") -> str:
    """Teams link opening the chat at a message; `msteams` targets the desktop client.

    The client (teams-for-linux) falls back to a full navigation to the link's host, which a
    redirecting host aborts silently — so the msteams form uses the host the client itself loads.
    """
    host = "teams.cloud.microsoft" if scheme == "msteams" else "teams.microsoft.com"
    ctx = quote('{"contextType":"chat"}', safe="")
    thread = quote(thread_id, safe="")
    if not msg_id:  # no message to land on (activity known only from the thread version)
        return f"{scheme}://{host}/l/chat/{thread}/conversations?context={ctx}"
    return f"{scheme}://{host}/l/message/{thread}/{msg_id}?context={ctx}"


def in_scope(thread_id: str) -> bool:
    """V1 widget scope = private 1:1/group + meeting chats (same default set as `archive`)."""
    return is_private(thread_id) or is_meeting(thread_id)


def _json_list(value: Any) -> list[Any]:
    """`properties.files` / `.cards` arrive as JSON *strings* (`'[]'` is truthy) or as lists."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def snippet(msgtype: str, content: str, props: dict[str, Any] | None = None) -> str:
    """One-line preview of a message body, marker for non-text types, capped at _SNIPPET_LEN."""
    props = props or {}
    if msgtype in ("RichText/Html", "Text"):
        # Emoji are tags whose `alt` holds the character; tags then become spaces, not nothing:
        # `<at>Bob</at>dis` would otherwise read "Bobdis".
        text = content
        if msgtype == "RichText/Html":
            text = html.unescape(_TAG_RE.sub(" ", _EMOJI_ALT_RE.sub(r"\1", content)))
        text = " ".join(text.split())
        if not text:
            # A file/card post has an empty body and its payload in properties; the list stub of
            # a deleted message has neither; a body that strips to nothing is an image tag.
            names = [
                str(f.get("fileName") or "file")
                for f in _json_list(props.get("files"))
                if isinstance(f, dict)
            ]
            if names:
                text = "📎 " + ", ".join(names)
            elif _json_list(props.get("cards")):
                text = "🃏 card"
            elif not content.strip():
                text = "🗑 deleted"
            else:
                text = "🖼 image" if "<img" in content else "📎 attachment"
    else:
        text = (_TYPE_MARKERS.get(msgtype) or f"[{msgtype.rsplit('/', 1)[-1]}]") if msgtype else ""
    text = " ".join(text.split())
    return text if len(text) <= _SNIPPET_LEN else text[: _SNIPPET_LEN - 1] + "…"


async def row_from_conversation(conv: dict[str, Any], directory: Directory) -> dict[str, Any]:
    thread_id = str(conv.get("id") or "")
    last = conv.get("lastMessage") or {}
    msgtype = str(last.get("messagetype") or "")
    sender = "" if _is_system(msgtype) else await sender_of(last, directory)
    label = await directory.label(thread_id)
    if label == thread_id:  # thread lookup failed (rate limit…): the listing carries the topic
        label = str((conv.get("threadProperties") or {}).get("topic") or thread_id)
    last_id = str(last.get("id") or "")
    read_id = read_up_to(conv.get("properties"))
    return {
        "id": thread_id,
        "label": label,
        "last_activity": last_activity(conv),
        "last_id": last_id,
        "read_id": read_id,
        "unread": is_unread(last_id, read_id),
        "sender": sender,
        "text": snippet(msgtype, str(last.get("content") or ""), last.get("properties")),
        "seen_at": None,
        "typing": [],
    }


def _is_system(msgtype: str) -> bool:
    """Thread activity (member changes, read markers…) is emitted by the thread, not a person."""
    return msgtype.startswith("ThreadActivity/")


async def sender_of(resource: dict[str, Any], directory: Directory | None) -> str:
    mri = str(resource.get("from") or "")
    name = resource.get("imdisplayname")
    if directory is None:
        return str(name or mri)
    directory.note_name(mri, name)
    return str(name) if name else (await directory.display(mri) if mri else "")


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
            row = await row_from_conversation(conv, directory)
            if row["text"] == "🗑 deleted":
                await _resolve_stub(row, directory)
            return row

    rows = await asyncio.gather(*(build(conv) for conv in picked))
    return {row["id"]: row for row in rows}


async def _resolve_stub(row: dict[str, Any], directory: Directory) -> None:
    """The listing's lastMessage drops `properties`, so a file/card post looks exactly like a
    deleted message. One history call for the few ambiguous rows tells them apart."""
    try:
        # pageSize=1 skips the newest message (window artefact); ask for a few and pick by id.
        recent = await asyncio.to_thread(
            fetch_history, directory.settings, directory.skype_token, row["id"], _STUB_PAGE, 1
        )
    except Exception as exc:  # noqa: BLE001 — cosmetic: keep the stub rather than fail bootstrap
        log.debug("stub_resolve_failed", thread=row["id"], error=str(exc))
        return
    message = next((m for m in recent if str(m.get("id") or "") == row["last_id"]), None)
    if message is None:
        return
    msgtype = str(message.get("messagetype") or "")
    row["text"] = snippet(msgtype, str(message.get("content") or ""), message.get("properties"))
    if not _is_system(msgtype):
        row["sender"] = await sender_of(message, directory)


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
        seen_path: Path | None = None,
        opener: list[str] | None = None,
        open_scheme: str = "msteams",
        browser: list[str] | None = None,
    ) -> None:
        self.rows = rows
        self.directory = directory
        self.reactions = reactions
        self.typing_ttl = typing_ttl
        self.seen_path = seen_path
        self.opener = opener  # argv the deep link is appended to; None = the page follows its link
        self.open_scheme = open_scheme
        self.browser = browser  # argv for the https link on Ctrl/middle click; None = page's own browser
        self.clients: set[ServerConnection] = set()
        self._typing_timers: dict[tuple[str, str], asyncio.TimerHandle] = {}
        self._seen: dict[str, str] = _load_seen(seen_path)
        for row in rows.values():
            row["seen_at"] = self._seen.get(row["id"])

    # --- seen marker (page verb) ---

    def mark_seen(self, thread_id: str, at: str | None = None) -> bool:
        """Stamp `at` (the activity the page showed when clicked) as seen; hidden until newer lands.

        A message that landed between render and click keeps the row visible: the stamp never
        exceeds what the user actually looked at.
        """
        row = self.rows.get(thread_id)
        if row is None or not row.get("last_activity"):
            return False
        stamp = min(at, row["last_activity"]) if at else row["last_activity"]
        self._seen[thread_id] = row["seen_at"] = stamp
        self._save_seen()
        self.broadcast()
        return True

    def mark_unseen(self, thread_id: str) -> bool:
        """Undo `seen`: the row shows again until the next click."""
        row = self.rows.get(thread_id)
        if row is None or thread_id not in self._seen:
            return False
        del self._seen[thread_id]
        row["seen_at"] = None
        self._save_seen()
        self.broadcast()
        return True

    def _save_seen(self) -> None:
        if self.seen_path is not None:
            self.seen_path.parent.mkdir(parents=True, exist_ok=True)
            self.seen_path.write_text(json.dumps(self._seen, ensure_ascii=False, indent=0))

    # --- live events (stream hook) ---

    async def on_event(self, obj: dict[str, Any]) -> None:
        resource = obj.get("resource") or {}
        resource_type = obj.get("resourceType")
        # A ConversationUpdate is keyed by the thread itself (no conversationLink).
        thread_id = (
            str(resource.get("id") or "") if resource_type == "ConversationUpdate" else thread_of(resource)
        )
        msgtype = str(resource.get("messagetype") or "")
        log.debug("web_event", thread=thread_id, resource_type=resource_type, messagetype=msgtype)
        if not in_scope(thread_id):
            return
        if resource_type == "ConversationUpdate":
            self._conversation_update(thread_id, resource)
        elif resource_type == "NewMessage":
            if msgtype in ("Control/Typing", "Control/ClearTyping"):
                await self._typing(thread_id, resource, started=msgtype == "Control/Typing")
            elif not msgtype.startswith("Control/") and msgtype not in _SILENT_TYPES:
                await self._new_message(thread_id, resource, msgtype)
        elif resource_type == "MessageUpdate":
            await self._message_update(thread_id, resource, msgtype)

    def _conversation_update(self, thread_id: str, resource: dict[str, Any]) -> None:
        """Read marker moved (this or another device): Teams' own read state, not our `seen`."""
        row = self.rows.get(thread_id)
        read_id = read_up_to(resource.get("properties"))
        if row is None or not read_id:
            return
        row["read_id"] = read_id
        row["unread"] = is_unread(row.get("last_id", ""), read_id)
        self.broadcast()

    async def _new_message(self, thread_id: str, resource: dict[str, Any], msgtype: str) -> None:
        row = self.rows.get(thread_id)
        if row is None:
            label = await self.directory.label(thread_id) if self.directory else thread_id
            row = self.rows[thread_id] = {
                "id": thread_id,
                "label": label,
                "seen_at": self._seen.get(thread_id),
                "typing": [],
                "read_id": "",
            }
        sender = "" if _is_system(msgtype) else await sender_of(resource, self.directory)
        # A bare-id label is a failed lookup: retry it (Directory spaces transient retries 60s apart).
        if self.directory is not None and (msgtype in _RELABEL_TYPES or row["label"] == thread_id):
            if msgtype in _RELABEL_TYPES:
                self.directory.forget(thread_id)
            fresh = await self.directory.label(thread_id)
            if fresh != thread_id:  # lookup failed (removed from the chat, 429): keep the old name
                row["label"] = fresh
        last_id = str(resource.get("id") or "")
        row.update(
            last_activity=str(resource.get("composetime") or resource.get("originalarrivaltime") or ""),
            last_id=last_id,
            # Own messages come back as unread until Teams moves the horizon (a ConversationUpdate
            # follows within a second) — honest, and it needs no notion of "me".
            unread=is_unread(last_id, row.get("read_id", "")),
            sender=sender,
            text=snippet(msgtype, str(resource.get("content") or ""), resource.get("properties")),
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
            row["text"] = snippet(msgtype, str(resource.get("content") or ""), props)
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
        name = await sender_of(resource, self.directory)
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
        # `page` lets an open tab notice a newer widget.html (server restart, edit) and reload.
        for row in ordered:
            row["link"] = deep_link(row["id"], row.get("last_id", ""))
        return json.dumps(
            {
                "rows": ordered,
                "page": page_version(),
                "opener": self.opener is not None,
                "browser": self.browser is not None,
            },
            ensure_ascii=False,
        )

    async def open_row(self, thread_id: str, web: bool = False) -> bool:
        """Page verb `open`: run the opener (or, for `web`, the browser) on the row's deep link.

        Opening is not reading: the row keeps its seen/unread state until the user says so."""
        row = self.rows.get(thread_id)
        argv = self.browser if web else self.opener
        if row is None or argv is None:
            return False
        url = deep_link(thread_id, row.get("last_id", ""), "https" if web else self.open_scheme)
        try:
            # argv exec, no shell: the only variable part is a URL built from ids we already hold.
            proc = await asyncio.create_subprocess_exec(
                *argv,
                url,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            log.warning("opener_failed", opener=argv[0], error=str(exc))
            return False
        asyncio.get_running_loop().create_task(proc.wait())  # reap; xdg-open returns at once
        log.info("opened", thread=thread_id, web=web)
        return True

    async def watch_page(self, interval: float = _PAGE_POLL) -> None:
        """Broadcast when widget.html changes so open tabs reload without waiting for an event."""
        current = page_version()
        while True:
            await asyncio.sleep(interval)
            version = page_version()
            if version != current:
                current = version
                self.broadcast()

    def broadcast(self) -> None:
        # Library helper: never awaits a slow page; a client with a full buffer just misses one
        # frame (the next push carries the full list anyway).
        broadcast(self.clients, self.payload())

    async def handle(self, ws: ServerConnection) -> None:
        self.clients.add(ws)
        try:
            await ws.send(self.payload())
            async for raw in ws:
                try:
                    verb = json.loads(raw)
                except json.JSONDecodeError, TypeError:
                    verb = None
                at = verb.get("at") if isinstance(verb, dict) else None
                at = at if isinstance(at, str) else None
                if isinstance(verb, dict) and isinstance(verb.get("seen"), str):
                    self.mark_seen(verb["seen"], at)
                elif isinstance(verb, dict) and isinstance(verb.get("unseen"), str):
                    self.mark_unseen(verb["unseen"])
                elif isinstance(verb, dict) and isinstance(verb.get("open"), str):
                    await self.open_row(verb["open"], web=verb.get("web") is True)
                else:
                    log.debug("ws_client_message_ignored", data=str(raw)[:100])
        finally:
            self.clients.discard(ws)


def _load_seen(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except OSError, ValueError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _page_path() -> Path:
    return Path(str(files("miniteams").joinpath("widget.html")))


def page_version() -> str:
    try:
        return str(_page_path().stat().st_mtime_ns)
    except OSError:
        return ""


def _page() -> str:
    return _page_path().read_text(encoding="utf-8")


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
                await board.watch_page()  # until cancelled
        except OSError as exc:
            if bind or attempt == _BIND_ATTEMPTS - 1:
                raise
            # Persisted port got taken by something else since the draw — redraw, keep going.
            log.warning("bind_failed", host=host, port=port, error=str(exc))
            host, port = _draw_bind()
            save_bind(settings, host, port)


async def run(
    settings: Settings,
    skype_token: str,
    bearer: str,
    limit: int,
    bind: str | None,
    reactions: bool = False,
    opener: str = "xdg-open",
    open_scheme: str = "msteams",
    browser: str = "",
) -> None:
    directory = Directory(settings)
    directory.set_token(skype_token, bearer)
    rows = await bootstrap(fetch_conversations(settings, skype_token), directory, limit)
    log.info("web_bootstrap", rows=len(rows))
    board = Board(
        rows,
        directory,
        reactions=reactions,
        seen_path=settings.config_dir / "seen.json",
        opener=None if opener in ("", "none") else opener.split(),
        open_scheme=open_scheme,
        browser=None if browser in ("", "none") else browser.split(),
    )
    # The stream only returns when auth is dead: a page that silently stops updating is worse
    # than an exit, so the server goes down with it and the user re-runs.
    server = asyncio.create_task(serve_board(board, settings, bind))
    # Own endpoint id: sharing `stream`'s would make whichever registered last the only receiver.
    stream = asyncio.create_task(
        run_forever(settings, on_event=board.on_event, directory=directory, epid_name="endpoint_id-web")
    )
    done, pending = await asyncio.wait({server, stream}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        task.result()  # re-raise a server failure (e.g. bind) instead of exiting 0
    raise RuntimeError("live stream ended (auth expired?) — re-run")
