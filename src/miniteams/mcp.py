"""MCP server over stdio (spec 008): newline-delimited JSON-RPC 2.0 exposing the archive to agents.

stdout carries protocol lines only; logs go to stderr (see `logging`). One request at a time.
"""

import asyncio
import json
import re
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import httpx
import structlog

from . import __version__
from .archive import RefreshingToken
from .archive_store import chat_dir_name, find_chat_dir, read_chats, read_meta, search_chat
from .auth import AuthExpired, AuthUnavailable, TokenSource, self_mri, token_source
from .card_refresh import _BUSY_TIMEOUT, CardRefresher, Cards, CardStore
from .chats import chat_kind, is_system, last_activity, parse_when
from .compose import render_outgoing
from .config import Settings
from .directory import Directory, member_label, sender_name, unknown
from .drafts import (
    SCHEDULE_MAX_AHEAD,
    SCHEDULE_MIN_AHEAD,
    DraftsStore,
    draft_payload,
    draft_state,
    new_draft_message,
    notes_thread_id,
    unused_client_id,
)
from .http import failure
from .mcp_api import ApiReader
from .messages import (
    SNIPPET_LEN,
    fold,
    is_deleted,
    is_edited,
    json_list,
    message_text,
    plain_text,
    quote_names,
)
from .render import card_entries, cards_text
from .send import NOTES_THREAD, edit_message, send_message
from .watch import note_sent

log = structlog.get_logger()

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
_PARSE_ERROR, _INVALID_REQUEST, _METHOD_NOT_FOUND, _INVALID_PARAMS = -32700, -32600, -32601, -32602
_SEARCH_DAYS = 7  # default window of search_messages, and of read_messages on the API
_PROBE_DEADLINE = 3.0  # seconds an archive gets to answer before the API takes over
_PROBE_HOLD = 60.0  # seconds the archive counts as unavailable after a failed probe
_CARDS_BUDGET = 5.0  # seconds of card refreshes per read_messages call, tokens in; a running save outlasts it
_CARDS_GRACE = 0.5  # seconds of that budget left after the last request for its save
_LOGIN_HINT = "An archiver that stopped on the expired credential has to be started again after signing in."
_INSTRUCTIONS = (
    "miniteams: your own Microsoft Teams chats, read from a local archive. Results carry `source` and, "
    "from the archive, the archiver's heartbeat (`archiver_seen`, `archiver_state`, `sync_started_at`, "
    "`media_sweep` while a media sweep runs): an old heartbeat means the archive stopped moving. "
    "Chat ids come from find_chats; never guess one."
)


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class ToolError(Exception):
    """A tool that ran and failed: reported as `isError`, the server stays up."""


Handler = Callable[["Server", dict[str, Any]], Any]


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Handler
    read_only: bool = True
    destructive: bool = False
    required: tuple[str, ...] = ()
    annotations: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        self.annotations = {"readOnlyHint": self.read_only, "destructiveHint": self.destructive}

    def spec(self) -> dict[str, Any]:
        schema = {"type": "object", "properties": self.schema, "additionalProperties": False}
        if self.required:
            schema["required"] = list(self.required)
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": schema,
            "annotations": self.annotations,
        }


def _validate(tool: Tool, args: Any) -> dict[str, Any]:
    """The subset of JSON Schema the tool table uses: type, enum, minimum, maximum, required."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise RpcError(_INVALID_PARAMS, "arguments must be an object")
    for name in tool.required:
        if name not in args:
            raise RpcError(_INVALID_PARAMS, f"missing argument: {name}")
    types = {"string": str, "integer": int, "boolean": bool}
    for name, value in args.items():
        prop = tool.schema.get(name)
        if prop is None:
            raise RpcError(_INVALID_PARAMS, f"unknown argument: {name}")
        kind = types[prop["type"]]
        # bool is an int in Python: an integer argument must not take true/false.
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise RpcError(_INVALID_PARAMS, f"{name} must be {prop['type']}")
        if "enum" in prop and value not in prop["enum"]:
            raise RpcError(_INVALID_PARAMS, f"{name} must be one of {', '.join(prop['enum'])}")
        if "minimum" in prop and value < prop["minimum"]:
            raise RpcError(_INVALID_PARAMS, f"{name} must be >= {prop['minimum']}")
        if "maximum" in prop and value > prop["maximum"]:
            raise RpcError(_INVALID_PARAMS, f"{name} must be <= {prop['maximum']}")
    return args


# --- archive reads ---


_COMPOSETIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z$")


def _utc(value: str, *, end: bool = False) -> str:
    """ISO date/datetime → UTC text comparable with message times.

    A message time handed back (`next_since`) keeps its fraction: rounded to the second it would
    resume before rows already returned."""
    if _COMPOSETIME_RE.match(value):
        return value
    try:
        return parse_when(value, end=end).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise ToolError(f"invalid ISO date/datetime: {value!r}") from exc


def _props(message: dict[str, Any]) -> dict[str, Any]:
    props = message.get("properties")
    return props if isinstance(props, dict) else {}


def _shows_cards(message: dict[str, Any]) -> bool:
    """The chat page shows the message's cards: not deleted, a card message or one with app cards."""
    props = _props(message)
    return not is_deleted(props) and (
        message.get("messagetype") == "RichText/Media_Card" or bool(props.get("cards"))
    )


def message_record(
    message: dict[str, Any],
    known_name: Callable[[str], str] | None = None,
    cards: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """`cards`: refreshed app cards by cardClientId (spec 037), shown instead of the posted ones."""
    props = _props(message)
    ams = message.get("amsreferences") or []
    mri = str(message.get("from") or "").rsplit("/", 1)[-1]
    record = {
        "id": str(message.get("id") or ""),
        "time": str(message.get("composetime") or ""),
        "sender": sender_name(mri, message.get("imdisplayname"), known_name) or unknown(mri),
        "from": mri,
        "text": message_text(message, known_name),
        "edited": is_edited(message),
        "deleted": is_deleted(props),
        "attachments": len(json_list(props.get("files"))) + len(ams),
        "cards": cards_text(message, cards) if _shows_cards(message) else [],
    }
    return record


class Archive:
    """Read-only view of `--data-dir`: every archive database is opened `mode=ro`, none is created. Only
    `cards.db`, the app card cache beside them (spec 037), is written."""

    def __init__(self, data_dir: Path, settings: Settings | None = None) -> None:
        self.data_dir = data_dir
        self.settings = settings  # its config_dir holds names.db, the names every miniteams process learned
        # One per process: its bot ids and holds outlive a call.
        self.cards = CardRefresher(settings, CardStore(data_dir / "cards.db")) if settings else None

        self._down_until = 0.0
        self._probe: Callable[[], None] = self._open_index
        self._directory: Directory | None = None

    def directory(self) -> Directory | None:
        """The process's names, over names.db; made on first use."""
        if self._directory is None and self.settings:
            self._directory = Directory(self.settings)
        return self._directory

    def _known_name(self) -> Callable[[str], str] | None:
        # One store open per process, refreshed per call: other processes improve names meanwhile.
        directory = self.directory()
        if directory is None:
            return None
        directory.refresh()
        return directory.known_name

    def _unavailable(self, exc: Exception) -> ToolError:
        return ToolError(f"archive unavailable at {self.data_dir}: {exc}")

    def _open_index(self) -> None:
        path = self.data_dir / "index.db"
        if not path.is_file():
            raise FileNotFoundError(path)
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            db.execute("SELECT 1 FROM chats LIMIT 1").fetchall()
        finally:
            db.close()

    def available(self) -> bool:
        """Can the archive answer? Probed in a thread with a deadline: storage that is gone can
        hang a read instead of failing it, and a stuck thread cannot be killed, so a failed probe
        holds the verdict for a while instead of piling threads up."""
        if time.monotonic() < self._down_until:
            return False
        outcome: dict[str, Exception] = {}

        def _run() -> None:
            try:
                self._probe()
            except Exception as exc:  # noqa: BLE001 — any failure means unavailable
                outcome["error"] = exc

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(_PROBE_DEADLINE)
        if worker.is_alive() or "error" in outcome:
            reason = str(outcome.get("error") or f"no answer within {_PROBE_DEADLINE:.0f} s")
            log.warning("archive_unavailable", data_dir=str(self.data_dir), error=reason)
            self._down_until = time.monotonic() + _PROBE_HOLD
            return False
        return True

    def meta(self) -> dict[str, Any]:
        try:
            rows = read_meta(self.data_dir)
        except (OSError, sqlite3.Error) as exc:
            raise self._unavailable(exc) from exc
        keys = ("archiver_seen", "archiver_state", "sync_started_at", "media_sweep")
        return {"source": "archive", **{k: rows[k] for k in keys if rows.get(k)}}

    def chats(self) -> Iterator[dict[str, Any]]:
        try:
            yield from read_chats(self.data_dir)
        except (OSError, sqlite3.Error) as exc:
            raise self._unavailable(exc) from exc

    def chat_dir(self, thread_id: str) -> Path:
        """The folder of an indexed chat; a thread id the index does not know never becomes a path."""
        try:
            found = find_chat_dir(self.data_dir, thread_id)
        except (OSError, sqlite3.Error) as exc:
            raise self._unavailable(exc) from exc
        if found is None:
            raise ToolError(f"unknown chat: {thread_id}")
        return found

    def find_chats(
        self, query: str, participant: str, kind: str, since: str, limit: int
    ) -> list[dict[str, Any]]:
        q, who = fold(query), fold(participant)
        found: list[dict[str, Any]] = []
        for chat in self.chats():
            thread_id = str(chat["id"])
            members = [m for m in chat["participants"] if isinstance(m, dict)]
            names = [str(m.get("name") or "") for m in members]
            if kind and chat_kind(thread_id) != kind:
                continue
            if q and q not in fold(" ".join([chat["label"], chat["topic"], *names])):
                continue
            roster = list(zip(names, members, strict=True))
            if who and not any(who in fold(n) or participant == m.get("mri") for n, m in roster):
                continue
            activity = last_activity(chat["raw"])
            if since and activity < since:
                continue
            found.append(
                {
                    "id": thread_id,
                    "label": chat["label"],
                    "kind": chat_kind(thread_id),
                    "last_activity": activity,
                    "participants": [
                        {"name": member_label(str(m.get("mri") or ""), n), "mri": str(m.get("mri") or "")}
                        for n, m in roster
                    ],
                    "backfill_done": chat["backfill_done"],
                    "history_denied": chat["history_denied"],
                    "synced_at": chat["synced_at"],
                }
            )
        found.sort(key=lambda c: c["last_activity"], reverse=True)
        return found[:limit]

    def _cards(
        self,
        thread_id: str,
        messages: list[dict[str, Any]],
        tokens: Callable[[float], tuple[str, str] | None] | None = None,
    ) -> dict[str, Cards]:
        """message id → its refreshed app cards as stored; with `tokens` (given a timeout), the stale eligible
        ones are asked of their bots first, tokens and all within `_CARDS_BUDGET` (037-AC9): past it, as
        stored."""
        carded = {str(m.get("id") or ""): m for m in messages if _shows_cards(m) and card_entries(m)}
        carded.pop("", None)
        if self.cards is None or not carded:
            return {}
        refresher = self.cards
        end = time.monotonic() + _CARDS_BUDGET
        deadline = end - _CARDS_GRACE
        rows = refresher.store.load_many(thread_id, list(carded))
        out = {mid: refresher.stored(thread_id, m, rows.get(mid, {})) for mid, m in carded.items()}
        # Tokens only for a card some user would refresh now: `me` comes with them.
        if tokens is None or not any(
            refresher.due(thread_id, m, None, rows.get(mid, {})) for mid, m in carded.items()
        ):
            return out
        pair = tokens(deadline - time.monotonic())
        if pair is None:
            return out
        skype_token, bearer = pair
        me = self_mri(bearer)
        due = {mid: m for mid, m in carded.items() if refresher.due(thread_id, m, me, rows.get(mid, {}))}
        if not due:
            return out

        async def refresh_all() -> None:
            tasks = {
                mid: asyncio.ensure_future(
                    refresher.refresh(
                        thread_id,
                        m,
                        me=me,
                        skype_token=skype_token,
                        bearer=bearer,
                        rows=rows.get(mid, {}),
                        deadline=deadline,
                    )
                )
                for mid, m in due.items()
            }
            await asyncio.wait(tasks.values(), timeout=max(0.0, end - time.monotonic()))
            late = 0
            for mid, task in tasks.items():
                if not task.done():  # asyncio.run cancels it on return, closing its clients
                    late += 1
                elif task.exception() is not None:
                    log.warning("mcp_card_refresh_failed", error_type=type(task.exception()).__name__)
                else:
                    out[mid] = task.result()
            if late:
                log.info("mcp_card_budget_spent", late=late, due=len(tasks))

        asyncio.run(refresh_all())
        return out

    def read_messages(
        self,
        thread_id: str,
        since: str,
        until: str,
        limit: int,
        tokens: Callable[[float], tuple[str, str] | None] | None = None,
    ) -> dict[str, Any]:
        """`tokens`: (skype token, bearer) within its timeout, or None, given to refresh the app cards."""
        path = self.chat_dir(thread_id) / "messages.db"
        if not path.is_file():
            return {"thread_id": thread_id, "messages": []}
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise self._unavailable(exc) from exc
        try:
            rows = db.execute(
                "SELECT composetime, raw FROM messages WHERE composetime >= ? AND composetime < ?"
                " ORDER BY composetime, id LIMIT ?",
                (since, until or "￿", limit + 1),
            ).fetchall()
        except sqlite3.Error as exc:
            raise self._unavailable(exc) from exc
        finally:
            db.close()
        result: dict[str, Any] = {"thread_id": thread_id}
        if len(rows) > limit:
            # Resume at the first time not returned. Rows sharing that time stay together on the
            # next page, so the cursor never repeats one and never skips one.
            cut = rows[limit][0]
            rows = [r for r in rows[:limit] if r[0] < cut]
            result["next_since"] = cut
        known_name = self._known_name()
        messages = [json.loads(raw) for _, raw in rows]
        cards = self._cards(thread_id, messages, tokens)
        result["messages"] = [
            message_record(m, known_name, cards.get(str(m.get("id") or ""))) for m in messages
        ]
        return result

    def search_messages(
        self, query: str, since: str, until: str, thread_id: str, limit: int
    ) -> dict[str, Any]:
        """Case- and accent-insensitive substring match on plain text, newest first."""
        if thread_id:
            targets = [(thread_id, "", self.chat_dir(thread_id))]
        else:
            # Feeds other than Notes hold copies of messages that live in other chats (spec 007).
            # The folder comes with the row: one index read, not one per chat.
            targets = [
                (str(c["id"]), str(c["label"]), self.data_dir / (c["dir"] or chat_dir_name(str(c["id"]))))
                for c in self.chats()
                if (not is_system(str(c["id"])) or str(c["id"]) == NOTES_THREAD)
                and last_activity(c["raw"]) >= since
            ]
        hits: list[dict[str, Any]] = []
        more = False
        known_name = self._known_name()
        for chat_id, label, folder in targets:
            try:
                found, extra = search_chat(folder, query, limit, since=since, until=until)
            except sqlite3.Error as exc:
                raise self._unavailable(exc) from exc
            more = more or extra
            cards = self._cards(chat_id, found)
            hits += [
                {
                    **message_record(m, known_name, cards.get(str(m.get("id") or ""))),
                    "thread_id": chat_id,
                    "chat": label,
                }
                for m in found
            ]
        hits.sort(key=lambda h: h["time"], reverse=True)
        more = more or len(hits) > limit
        return {"chats_scanned": len(targets), "more": more, "messages": hits[:limit]}

    def recordings(self, thread_id: str) -> list[dict[str, Any]]:
        path = self.chat_dir(thread_id) / "media" / "recordings.json"
        if not path.is_file():
            return []
        try:
            entries = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ToolError(f"unreadable recordings manifest for {thread_id}: {exc}") from exc
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

    def transcript(self, thread_id: str, message_id: str, offset: int, limit: int) -> dict[str, Any]:
        entry = next((e for e in self.recordings(thread_id) if str(e.get("message_id")) == message_id), None)
        if entry is None:
            raise ToolError(f"no recording with message id {message_id} in {thread_id}")
        # Only the JSON rendition names its speakers (docs/agent-archive.md); the VTT is not read.
        names = [n for n in entry.get("transcripts") or [] if str(n).endswith(".transcript.json")]
        if not names:
            raise ToolError(f"no transcript on disk for recording {message_id} in {thread_id}")
        media_dir = (self.chat_dir(thread_id) / "media").resolve()
        path = (media_dir / str(names[0])).resolve()
        if path.parent != media_dir:  # the manifest is archive-written, but a name is still a name
            raise ToolError(f"transcript name outside the media folder: {names[0]}")
        try:
            turns = json.loads(path.read_text()).get("entries") or []
        except (OSError, ValueError, AttributeError) as exc:
            raise ToolError(f"unreadable transcript {names[0]}: {exc}") from exc
        page = [
            {
                "speaker": str(t.get("speakerDisplayName") or t.get("speakerId") or ""),
                "start": str(t.get("startOffset") or ""),
                "text": str(t.get("text") or ""),
            }
            for t in turns[offset : offset + limit]
            if isinstance(t, dict)
        ]
        result = {
            "message_id": message_id,
            "title": str(entry.get("title") or ""),
            "total": len(turns),
            "offset": offset,
            "turns": page,
        }
        if offset + limit < len(turns):
            result["next_offset"] = offset + limit
        return result


class Session:
    """Credentials for the API paths: silent refresh, and a device-code sign-in when that is dead.

    While the archive answers, only `quiet_tokens` touches it, for a page with app cards (spec 037);
    other reads never refresh the token under `web` or `archive --live`."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._source: TokenSource | None = None
        self._provider: RefreshingToken | None = None
        self._flow: dict[str, Any] | None = None
        self._flow_result: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._quiet: tuple[threading.Thread, dict[str, Any]] | None = None  # the fetch of `quiet_tokens`
        self._quiet_hold = 0.0  # monotonic end of a failed fetch's hold

    @property
    def source(self) -> TokenSource:
        if self._source is None:
            self._source = token_source(self.settings)
        return self._source

    @property
    def provider(self) -> RefreshingToken:
        if self._provider is None:
            self._provider = RefreshingToken(self.settings, self.source)
        return self._provider

    def tokens(self) -> tuple[str, str]:
        """(skype token, bearer), or a ToolError carrying what to do."""
        try:
            return self.provider.token(), self.provider.bearer()
        except AuthExpired:
            raise self._login_error() from None
        except AuthUnavailable as exc:
            raise ToolError(f"Microsoft did not answer the token refresh ({exc}); retry later") from exc

    def quiet_tokens(self, timeout: float) -> tuple[str, str] | None:
        """(skype token, bearer) for an optional call within `timeout` s, else None; no sign-in starts.

        Fetched in a thread, as `Archive.available` probes: a slow fetch carries on for the next call, one at
        a time, and a failure holds for `_PROBE_HOLD`."""
        if time.monotonic() < self._quiet_hold:
            return None
        worker, outcome = self._quiet or (None, {})
        if worker is None or not worker.is_alive() and "error" not in outcome:
            outcome = {}
            worker = threading.Thread(target=self._fetch_quiet, args=(outcome,), daemon=True)
            worker.start()
            self._quiet = (worker, outcome)
        worker.join(max(0.0, timeout))
        if "error" in outcome:
            log.info("mcp_tokens_unavailable", error_type=outcome["error"])
            self._quiet, self._quiet_hold = None, time.monotonic() + _PROBE_HOLD
            return None
        pair: tuple[str, str] | None = outcome.get("pair")
        return pair

    def _fetch_quiet(self, outcome: dict[str, Any]) -> None:
        try:
            outcome["pair"] = (self.provider.token(), self.provider.bearer())
        except Exception as exc:  # noqa: BLE001 — the caller does without; the type only, never the text
            outcome["error"] = type(exc).__name__

    def _login_error(self) -> ToolError:
        pending = self.login()
        if pending.get("state") == "pending":
            return ToolError(
                f"not signed in. Open {pending['url']} and enter code {pending['code']} "
                f"({pending['expires_in']} s left), then call again. {_LOGIN_HINT}"
            )
        return ToolError(f"not signed in: {pending.get('reason') or 'sign-in failed'}")

    def login(self) -> dict[str, Any]:
        with self._lock:
            result = self._flow_result
            if result is not None:
                self._flow = self._flow_result = None  # consumed; a success sits in the cache now
                code = str(result.get("error") or "")
                if "access_token" not in result and code != "expired_token":
                    return {"state": "signed_out", "reason": f"{code}: {result.get('error_description')}"}
            if self._flow is not None and time.time() < float(self._flow.get("expires_at") or 0):
                return self._pending()
            self._flow = None
        try:
            result = self.source.refresh()
        except AuthExpired:
            with self._lock:
                self._flow = self.source.start_device_flow()
                threading.Thread(target=self._finish, args=(self._flow,), daemon=True).start()
                return self._pending()
        except AuthUnavailable as exc:
            raise ToolError(f"Microsoft did not answer the token refresh ({exc}); retry later") from exc
        claims = result.get("id_token_claims") or {}
        account = str(claims.get("preferred_username") or claims.get("name") or "")
        return {"state": "signed_in", "account": account}

    def proxy(self) -> tuple[str, str]:
        """(ic3 bearer, region) for the drafts store; the region comes with the skype token."""
        self.tokens()
        ic3 = self.source.ic3_token()
        if not ic3:
            raise self._login_error()
        return ic3, self.provider.region

    def mri(self) -> str:
        return self_mri(self.provider.bearer())

    def display_name(self) -> str:
        """Name stamped on outgoing messages, read from the token cache."""
        return self.source.display_name()

    def _pending(self) -> dict[str, Any]:
        assert self._flow is not None
        return {
            "state": "pending",
            "url": self._flow["verification_uri"],
            "code": self._flow["user_code"],
            "expires_in": max(0, int(float(self._flow.get("expires_at") or 0) - time.time())),
            "hint": _LOGIN_HINT,
        }

    def _finish(self, flow: dict[str, Any]) -> None:
        try:
            result = self.source.finish_device_flow(flow)
        except Exception as exc:  # noqa: BLE001 — reported through login(), never raised here
            result = {"error": type(exc).__name__, "error_description": str(exc)}
        with self._lock:
            if self._flow is flow:
                self._flow_result = result
        log.info("mcp_login_finished", ok="access_token" in result, error=result.get("error"))


# --- tools ---


def _default_since() -> str:
    return (datetime.now(UTC) - timedelta(days=_SEARCH_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")


def _find_chats(server: Server, args: dict[str, Any]) -> dict[str, Any]:
    since = _utc(args["since"]) if args.get("since") else ""
    query, who = args.get("query", ""), args.get("participant", "")
    kind, limit = args.get("kind", ""), args.get("limit", 20)
    if server.archive.available():
        archive = server.archive
        return {**archive.meta(), "chats": archive.find_chats(query, who, kind, since, limit)}
    return {"source": "api", **server.api().find_chats(query, who, kind, since, limit)}


def _read_messages(server: Server, args: dict[str, Any]) -> dict[str, Any]:
    since = _utc(args["since"]) if args.get("since") else ""
    until = _utc(args["until"], end=True) if args.get("until") else ""
    thread_id, limit = args["thread_id"], args.get("limit", 100)
    if server.archive.available():
        archive = server.archive
        return {
            **archive.meta(),
            **archive.read_messages(thread_id, since, until, limit, server.session.quiet_tokens),
        }
    page = server.api().read_messages(thread_id, since or _default_since(), until, limit)
    return {"source": "api", **page}


def _search_messages(server: Server, args: dict[str, Any]) -> dict[str, Any]:
    since = _utc(args["since"]) if args.get("since") else _default_since()
    until = _utc(args["until"], end=True) if args.get("until") else ""
    query, thread_id, limit = args["query"], args.get("thread_id", ""), args.get("limit", 50)
    if server.archive.available():
        archive = server.archive
        found = archive.search_messages(query, since, until, thread_id, limit)
        return {**archive.meta(), "since": since, **found}
    found = server.api().search_messages(query, since, until, thread_id, limit)
    return {"source": "api", "since": since, **found}


def _read_transcript(server: Server, args: dict[str, Any]) -> dict[str, Any]:
    archive = server.archive
    if not archive.available():
        raise ToolError(f"transcripts need the archive, which is unavailable at {archive.data_dir}")
    meta = archive.meta()
    thread_id = args["thread_id"]
    if not args.get("message_id"):
        recordings = [
            {
                "message_id": str(e.get("message_id") or ""),
                "time": str(e.get("composetime") or ""),
                "title": str(e.get("title") or ""),
                "duration": str(e.get("duration") or ""),
                "has_transcript": any(
                    str(n).endswith(".transcript.json") for n in e.get("transcripts") or []
                ),
                "videos": len(e.get("videos") or []),
            }
            for e in archive.recordings(thread_id)
        ]
        return {**meta, "thread_id": thread_id, "recordings": recordings}
    page = archive.transcript(thread_id, args["message_id"], args.get("offset", 0), args.get("limit", 200))
    return {**meta, "thread_id": thread_id, **page}


def _outgoing_text(args: dict[str, Any]) -> str:
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ToolError("text must be a non-empty string")
    return text


def _compose(
    server: Server, thread_id: str, text: str, info: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any] | None]:
    """render_outgoing on the chat's roster: `info`'s when given, else fetched."""

    def members() -> list[dict[str, Any]] | None:
        found = info or asyncio.run(server.api().directory.thread(thread_id))
        return None if found is None else list(found["members"])

    try:
        return render_outgoing(text, thread_id, members, server.session.mri)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def _known_thread(server: Server, thread_id: str) -> dict[str, Any] | None:
    """Refuse an id nothing vouches for: the index while the archive answers, a thread lookup on
    the API otherwise, whose info is returned (None when not looked up). Notes to self is always yours."""
    if thread_id == NOTES_THREAD:
        return None
    if server.archive.available():
        server.archive.chat_dir(thread_id)  # raises "unknown chat"
        return None
    if (info := asyncio.run(server.api().directory.thread(thread_id))) is None:
        raise ToolError(f"unknown chat: {thread_id}")
    return info


def _write[T](call: Callable[[], T], action: str) -> T:
    try:
        return call()
    except (httpx.HTTPStatusError, httpx.TransportError) as exc:
        raise ToolError(f"{action} failed: {failure(exc)}") from exc  # failure(): never a URL
    except RuntimeError as exc:  # send._check: a 2xx with an error envelope
        raise ToolError(str(exc)) from exc


def _send_at_ms(value: str) -> int:
    """`send_at` as epoch ms, inside the service's bounds; a time without an offset is refused
    because UTC and the user's zone differ by hours."""
    try:
        when = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(f"send_at is not an ISO 8601 datetime: {value!r}") from exc
    if when.tzinfo is None:
        raise ToolError("send_at needs a UTC offset or a trailing Z")
    ahead = when - datetime.now(UTC)
    if ahead < SCHEDULE_MIN_AHEAD:
        raise ToolError(f"send_at must be at least {SCHEDULE_MIN_AHEAD.total_seconds():.0f} seconds ahead")
    if ahead > SCHEDULE_MAX_AHEAD:
        raise ToolError(
            f"send_at must be at most {SCHEDULE_MAX_AHEAD.days} days ahead, the most Teams accepts"
        )
    return int(when.timestamp() * 1000)


def _iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _inner_thread(server: Server, thread_id: str, skype_token: str) -> str:
    """The drafts store refuses the `48:notes` alias and takes the thread behind it."""
    return notes_thread_id(server.settings, skype_token) if thread_id == NOTES_THREAD else thread_id


def _schedule(
    server: Server, thread_id: str, text: str, send_at_ms: int, info: dict[str, Any] | None
) -> dict[str, Any]:
    content, props = _compose(server, thread_id, text, info)
    skype_token, _ = server.session.tokens()
    store = server.drafts()
    inner = _inner_thread(server, thread_id, skype_token)
    client_id = unused_client_id(_write(store.list, "list"), str(int(time.time() * 1000)))
    name = server.session.display_name()
    message = new_draft_message(content, server.session.mri(), name, client_id, props)
    draft_id = _write(lambda: store.create(draft_payload(inner, message, send_at_ms)), "schedule")
    note_sent(server.settings, client_id)  # Teams will send it with this client id: a watch on me skips it
    return {"thread_id": thread_id, "scheduled_id": draft_id, "send_at": _iso_ms(send_at_ms)}


def _send_message(server: Server, args: dict[str, Any]) -> dict[str, Any]:
    text = _outgoing_text(args)
    send_at_ms = _send_at_ms(args["send_at"]) if args.get("send_at") else None
    thread_id = args["thread_id"]
    info = _known_thread(server, thread_id)
    if send_at_ms is not None:
        return _schedule(server, thread_id, text, send_at_ms, info)
    content, props = _compose(server, thread_id, text, info)
    skype_token, _ = server.session.tokens()
    name = server.session.display_name()
    sent = _write(
        lambda: send_message(
            server.settings, skype_token, thread_id, content, name, is_html=True, properties=props
        ),
        "send",
    )
    note_sent(server.settings, str(sent.get("clientmessageid") or ""))  # so a watch on me skips it
    return {
        "thread_id": thread_id,
        "clientmessageid": sent.get("clientmessageid", ""),
        "status": sent.get("status"),
    }


def _scheduled_record(
    draft: dict[str, Any], labels: dict[str, str], known_name: Callable[[str], str] | None
) -> dict[str, Any]:
    thread_id = str(draft.get("innerThreadId") or "").split(";messageid=")[0]
    return {
        "id": str(draft.get("id") or ""),
        "thread_id": thread_id,
        "chat": labels.get(thread_id, ""),
        "send_at": _iso_ms(int((draft.get("draftDetails") or {}).get("sendAt") or 0)),
        "text": plain_text(quote_names(str(draft.get("content") or ""), known_name))[:SNIPPET_LEN],
    }


def _labels(server: Server) -> dict[str, str]:
    if not server.archive.available():
        return {}
    return {str(c["id"]): str(c["label"]) for c in server.archive.chats()}


def _list_scheduled(server: Server, args: dict[str, Any]) -> dict[str, Any]:
    skype_token, _ = server.session.tokens()
    store = server.drafts()
    wanted = _inner_thread(server, args["thread_id"], skype_token) if args.get("thread_id") else ""
    now_ms = int(time.time() * 1000)
    labels, known_name = _labels(server), server.archive._known_name()
    pending = [
        _scheduled_record(d, labels, known_name)
        for d in _write(store.list, "list")
        if draft_state(d, now_ms) == "pending"
        and (not wanted or str(d.get("innerThreadId") or "").split(";messageid=")[0] == wanted)
    ]
    pending.sort(key=lambda d: d["send_at"])
    return {"scheduled": pending}


def _pending_draft(server: Server, thread_id: str, message_id: str) -> tuple[DraftsStore, dict[str, Any]]:
    """The draft, once it is known, pending and aimed at the chat named. Read before any write:
    Teams answers 200 to a second cancel, so the answer alone does not tell the state."""
    skype_token, _ = server.session.tokens()
    store = server.drafts()
    draft = _write(lambda: store.get(message_id) or {}, "read")
    if not draft:
        raise ToolError(f"no scheduled message with id {message_id}")
    state = draft_state(draft, int(time.time() * 1000))
    if state != "pending":
        raise ToolError(f"scheduled message {message_id} is not pending: {state}")
    inner = str(draft.get("innerThreadId") or "").split(";messageid=")[0]
    if inner != _inner_thread(server, thread_id, skype_token):
        raise ToolError(f"scheduled message {message_id} is not aimed at {thread_id}")
    return store, draft


def _cancel_scheduled(server: Server, args: dict[str, Any]) -> dict[str, Any]:
    store, _ = _pending_draft(server, args["thread_id"], args["message_id"])
    _write(lambda: store.cancel(args["message_id"]), "cancel")
    return {"thread_id": args["thread_id"], "message_id": args["message_id"], "cancelled": True}


def _update_scheduled(server: Server, args: dict[str, Any]) -> dict[str, Any]:
    if not args.get("send_at") and not args.get("text"):
        raise ToolError("nothing to change: give send_at, text or both")
    text = _outgoing_text(args) if args.get("text") else None
    send_at_ms = _send_at_ms(args["send_at"]) if args.get("send_at") else None
    store, draft = _pending_draft(server, args["thread_id"], args["message_id"])
    if send_at_ms is None:
        send_at_ms = int((draft.get("draftDetails") or {}).get("sendAt") or 0)
    props = draft.get("properties") or {}
    content = draft.get("content")
    if text is not None:
        content, fresh = _compose(server, args["thread_id"], text)
        props = {**props, "mentions": "[]", **(fresh or {})}  # the old text's mentions go with it
    message = {
        **draft,
        "content": content,
        # Editing knocks `draftId` off the client id, and the clients then draw the row as sent.
        "properties": {**props, "draftId": draft.get("clientmessageid")},
    }
    payload = draft_payload(str(draft.get("innerThreadId") or ""), message, send_at_ms, args["message_id"])
    _write(lambda: store.update(args["message_id"], payload), "update")
    return {"thread_id": args["thread_id"], "message_id": args["message_id"], "send_at": _iso_ms(send_at_ms)}


def _update_message(server: Server, args: dict[str, Any]) -> dict[str, Any]:
    text = _outgoing_text(args)
    thread_id, message_id = args["thread_id"], args["message_id"]
    info = _known_thread(server, thread_id)
    content, props = _compose(server, thread_id, text, info)
    skype_token, _ = server.session.tokens()
    done = _write(
        lambda: edit_message(
            server.settings, skype_token, thread_id, message_id, content, is_html=True, properties=props
        ),
        "edit",
    )
    return {"thread_id": thread_id, "message_id": message_id, "status": done.get("status")}


def _login(server: Server, args: dict[str, Any]) -> dict[str, Any]:
    state = server.session.login()
    if state.get("state") == "signed_out":
        raise ToolError(f"sign-in failed: {state.get('reason')}")
    return state


TOOLS: tuple[Tool, ...] = (
    Tool(
        "find_chats",
        "Find chats by label, topic, participant or kind, newest activity first. Returns ids to use with "
        "the other tools.",
        {
            "query": {"type": "string", "description": "matched against label, topic and participant names"},
            "participant": {"type": "string", "description": "a member's name (part of it) or MRI"},
            "kind": {"type": "string", "enum": ["private", "group", "meeting", "channel", "system"]},
            "since": {"type": "string", "description": "ISO date/datetime: only chats active at/after"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        _find_chats,
    ),
    Tool(
        "read_messages",
        "Messages of one chat as plain text, oldest first, inside [since, until). A cut page carries "
        "`next_since` to resume from. `cards`: one text line per app or bot card ([] when none); from the "
        f"archive, a stale card is first asked again of its app: requests stop at {_CARDS_BUDGET:.0f} s per "
        f"call, saving to a busy cards.db can take up to {2 * _BUSY_TIMEOUT:.0f} s more.",
        {
            "thread_id": {"type": "string"},
            "since": {"type": "string", "description": "ISO date/datetime (UTC unless an offset is given)"},
            "until": {"type": "string", "description": "ISO date/datetime; a date covers its whole day"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        },
        _read_messages,
        required=("thread_id",),
    ),
    Tool(
        "search_messages",
        "Find messages whose text contains a word or phrase (case and accents ignored), newest first, across "
        "the chats active in the window or inside one chat. `since` defaults to 7 days back. `cards`: as "
        "read_messages, never asked again.",
        {
            "query": {"type": "string"},
            "since": {"type": "string", "description": "ISO date/datetime; default: 7 days ago"},
            "until": {"type": "string", "description": "ISO date/datetime; a date covers its whole day"},
            "thread_id": {"type": "string", "description": "search this chat only"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        _search_messages,
        required=("query",),
    ),
    Tool(
        "read_transcript",
        "Meeting transcripts of a chat. Without `message_id`: the recordings and whether each has a "
        "transcript. With it: the turns (speaker, start offset, text), paged by offset.",
        {
            "thread_id": {"type": "string"},
            "message_id": {"type": "string", "description": "the recording, from the list"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        },
        _read_transcript,
        required=("thread_id",),
    ),
    Tool(
        "send_message",
        "Post a message to a chat as you, now or at `send_at` (Teams holds it until then). Get "
        "the chat id from find_chats; `48:notes` is your own Notes. Markdown and :shortcode: render as in "
        "the chat page; `@Full Name`, `@First` or `@Last` mentions a member (`@everyone` the chat), "
        "`@{Who|label}` mentions the member Who names (any of those forms) shown as `label`, read raw up "
        "to its `}` (the label: one line, no `|` or `}`, no tab or other blank but the space, no control, "
        "format, private-use or unassigned character; empty: the full name), `\\@` is a literal @. "
        "A name matching no member, or several (listed), is refused: nothing is sent.",
        {
            "thread_id": {"type": "string"},
            "text": {"type": "string"},
            "send_at": {"type": "string", "description": "ISO 8601 datetime with an offset or Z"},
        },
        _send_message,
        read_only=False,
        required=("thread_id", "text"),
    ),
    Tool(
        "list_scheduled",
        "Messages Teams is holding for later: id, chat, send time, text. Sent and cancelled ones are "
        "left out.",
        {"thread_id": {"type": "string", "description": "only this chat"}},
        _list_scheduled,
        read_only=False,
    ),
    Tool(
        "cancel_scheduled",
        "Drop a pending scheduled message so it is never sent.",
        {"thread_id": {"type": "string"}, "message_id": {"type": "string"}},
        _cancel_scheduled,
        read_only=False,
        destructive=True,
        required=("thread_id", "message_id"),
    ),
    Tool(
        "update_scheduled",
        "Move a pending scheduled message to another time, rewrite its text, or both.",
        {
            "thread_id": {"type": "string"},
            "message_id": {"type": "string"},
            "send_at": {"type": "string", "description": "ISO 8601 datetime with an offset or Z"},
            "text": {"type": "string"},
        },
        _update_scheduled,
        read_only=False,
        destructive=True,
        required=("thread_id", "message_id"),
    ),
    Tool(
        "update_message",
        "Replace the text of a message you sent, by chat id and message id.",
        {"thread_id": {"type": "string"}, "message_id": {"type": "string"}, "text": {"type": "string"}},
        _update_message,
        read_only=False,
        destructive=True,
        required=("thread_id", "message_id", "text"),
    ),
    Tool(
        "login",
        "Sign the server in when its cached credential is dead or absent: returns `signed_in`, or "
        "`pending` with a URL for the user to open and a code to enter there. "
        "Reads on a reachable archive never need it.",
        {},
        _login,
    ),
)


# --- protocol ---


class Server:
    def __init__(
        self,
        settings: Settings,
        data_dir: Path,
        *,
        read_only: bool = False,
        stdin: IO[str] | None = None,
        stdout: IO[str] | None = None,
    ) -> None:
        self.settings = settings
        self.archive = Archive(data_dir.resolve(), settings)
        self.session = Session(settings)
        self.read_only = read_only
        self._in = stdin or sys.stdin
        self._out = stdout or sys.stdout
        self._out_lock = threading.Lock()  # notifications may come from another thread later

    def api(self) -> ApiReader:
        """A reader on the chat service with fresh tokens; raises the login ToolError when signed out."""
        skype_token, bearer = self.session.tokens()
        directory = self.archive.directory()
        assert directory is not None  # a Server always has settings
        directory.refresh()
        directory.set_token(skype_token, bearer)
        return ApiReader(self.settings, skype_token, directory, record=message_record)

    def drafts(self) -> DraftsStore:
        ic3, region = self.session.proxy()
        return DraftsStore(ic3, region)

    def tools(self) -> list[Tool]:
        return [t for t in TOOLS if t.read_only or not self.read_only]

    def serve(self) -> int:
        """Read requests until EOF. Every line written is one JSON-RPC message."""
        if directory := self.archive.directory():
            directory.start_roster_import(self.archive.data_dir)
        for line in self._in:
            if not line.strip():
                continue
            response = self.handle(line)
            if response is not None:
                self.write(response)
        return 0

    def write(self, message: dict[str, Any]) -> None:
        with self._out_lock:
            self._out.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")
            self._out.flush()

    def handle(self, line: str) -> dict[str, Any] | None:
        """One request line → its response, or None for a notification."""
        try:
            request = json.loads(line)
        except ValueError:
            return _error(None, _PARSE_ERROR, "parse error")
        if not isinstance(request, dict) or not isinstance(request.get("method"), str):
            req_id = request.get("id") if isinstance(request, dict) else None
            return _error(req_id, _INVALID_REQUEST, "invalid request")
        method, params, req_id = request["method"], request.get("params") or {}, request.get("id")
        notification = "id" not in request
        try:
            result = self._dispatch(method, params if isinstance(params, dict) else {})
        except RpcError as exc:
            return None if notification else _error(req_id, exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 — a failing handler must not take the server down
            log.error("mcp_request_failed", method=method, error=str(exc), error_type=type(exc).__name__)
            return None if notification else _error(req_id, -32603, f"internal error: {exc}")
        return None if notification else {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            asked = str(params.get("protocolVersion") or "")
            return {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "miniteams", "version": __version__},
                "instructions": _INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [t.spec() for t in self.tools()]}
        if method == "tools/call":
            return self._call(params)
        if method.startswith("notifications/"):
            return None
        raise RpcError(_METHOD_NOT_FOUND, f"method not found: {method}")

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        tool = next((t for t in self.tools() if t.name == name), None)
        if tool is None:
            raise RpcError(_INVALID_PARAMS, f"unknown tool: {name}")
        args = _validate(tool, params.get("arguments"))
        try:
            result = tool.handler(self, args)
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}]}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
