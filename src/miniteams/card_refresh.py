"""Up-to-date app cards (spec 037): ask a card's bot for its current version, as the Teams client does.

The latest card per (chat, message, card) lives in `data/cards.db`; the archived message is never touched.
"""

import asyncio
import json
import math
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote

import httpx
import structlog

from .config import Settings
from .render import card_entries

log = structlog.get_logger()

REFRESH_INTERVAL = 600.0  # seconds
_CONCURRENCY = 4
_BOTS_MAX = 64
_HOLDS_MAX = 64  # apps held back by a 429
_SAVED_MAX = 4096  # (chat, message, card) saves kept in memory, for an unwritable cards.db
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_CARD_MAX = 256 * 1024  # bytes; a bigger reply is a failure
_DEFINITIONS_MAX = 1 << 20  # bytes of a batchedDefinitions reply
_POST_DEADLINE = 15.0  # seconds for a whole request, connect to last byte: httpx times each read alone
_RETRY_MAX = 86400.0
_BUSY_TIMEOUT = 5.0  # seconds a cards.db lock is waited for
_ADAPTIVE = "application/vnd.microsoft.card.adaptive"
APPS_URL = "https://teams.microsoft.com/api/mt/beta/users/apps/batchedDefinitions"
_SCHEMA = """CREATE TABLE IF NOT EXISTS cards (
    thread_id TEXT, message_id TEXT, card_id TEXT, card TEXT, refreshed_at TEXT, retry_at TEXT,
    PRIMARY KEY (thread_id, message_id, card_id)
)"""

Cards = dict[str, dict[str, Any]]


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def refresh_action(entry: object, me: str) -> dict[str, Any] | None:
    """The `refresh.action` of a `properties.cards[]` entry Teams refreshes for `me` (037-AC4), else None."""
    if not isinstance(entry, dict) or not _text(entry.get("appId")) or not _text(entry.get("cardClientId")):
        return None
    content = entry.get("content")
    refresh = content.get("refresh") if isinstance(content, dict) else None
    action = refresh.get("action") if isinstance(refresh, dict) else None
    if (
        not isinstance(action, dict)
        or action.get("type") != "Action.Execute"
        or not _text(action.get("verb"))
    ):
        return None
    users = refresh.get("userIds") if isinstance(refresh, dict) else None
    if users not in (None, []) and not (me and isinstance(users, list) and me in users):
        return None
    return action


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat(timespec="seconds")


def _epoch(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return 0.0  # unreadable: due now


class Row(NamedTuple):
    card: dict[str, Any] | None  # the last good card
    refreshed: float  # epoch of that card, 0 when none
    retry: float  # epoch of the next attempt


class CardStore:
    """`data/cards.db`; reads never create it and never raise, a write creates it beside the archive."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, thread_id: str, message_id: str) -> dict[str, Row]:
        """card id → its row; {} when the file is missing or locked."""
        if not self.path.is_file():
            return {}
        try:
            db = sqlite3.connect(f"{self.path.absolute().as_uri()}?mode=rw", uri=True, timeout=_BUSY_TIMEOUT)
            try:
                rows = db.execute(
                    "SELECT card_id, card, refreshed_at, retry_at FROM cards"
                    " WHERE thread_id = ? AND message_id = ?",
                    (thread_id, message_id),
                ).fetchall()
            finally:
                db.close()
        except sqlite3.Error as exc:
            log.info("card_store_read_failed", error=str(exc))
            return {}
        out: dict[str, Row] = {}
        for card_id, text, refreshed_at, retry_at in rows:
            try:
                card = json.loads(text) if text else None
            except ValueError, RecursionError:
                card = None
            out[str(card_id)] = Row(
                card if isinstance(card, dict) else None, _epoch(refreshed_at), _epoch(retry_at)
            )
        return out

    def save(
        self,
        thread_id: str,
        message_id: str,
        card_id: str,
        card: dict[str, Any] | None,
        now: float,
        retry: float,
    ) -> None:
        """A refreshed `card`, or (None) a failure that keeps the stored card and only moves `retry_at`."""
        text = json.dumps(card, ensure_ascii=False, separators=(",", ":")) if card is not None else ""
        try:
            db = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute(_SCHEMA)
                with db:
                    db.execute(
                        "INSERT INTO cards VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET"
                        " retry_at = excluded.retry_at"
                        + (", card = excluded.card, refreshed_at = excluded.refreshed_at" if text else ""),
                        (thread_id, message_id, card_id, text, _iso(now) if text else "", _iso(retry)),
                    )
            finally:
                db.close()
        except sqlite3.Error as exc:
            log.info("card_store_write_failed", error=str(exc))


class Failed(Exception):
    def __init__(self, reason: str, status: int | None = None, wait: float = 0.0) -> None:
        super().__init__(reason)
        self.reason, self.status, self.wait = reason, status, wait


def _retry_wait(resp: httpx.Response, body: object) -> float:
    """A 429's own wait, seconds: `Retry-After`, or the body's `value`."""
    header = resp.headers.get("Retry-After", "")
    wait = float(header) if header.isdigit() else 0.0
    value = body.get("value") if isinstance(body, dict) else None
    if isinstance(value, int | float) and math.isfinite(value):
        wait = max(wait, value)  # unit unknown: seconds, the reading that never waits less than asked
    return wait


class CardRefresher:
    """Refreshes the eligible app cards of a message; the caller passes the tokens."""

    def __init__(
        self,
        settings: Settings,
        store: CardStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.store = store
        self.clock = clock
        self._transport = transport
        # app id → (bot id or "", expiry, the failure behind a negative entry); process-wide (037-AC7)
        self._bots: OrderedDict[str, tuple[str, float, Failed | None]] = OrderedDict()
        # (chat, message, card) → this process's last save: it outlives an unwritable cards.db (037-AC5)
        self._saved: OrderedDict[tuple[str, str, str], Row] = OrderedDict()
        self._holds: OrderedDict[str, float] = OrderedDict()  # app id → end of its 429 wait
        self._bind(None)

    def _bind(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Fresh per-loop state: the MCP runs each tool in its own `asyncio.run`."""
        self._loop = loop
        self._sem = asyncio.Semaphore(_CONCURRENCY)
        self._inflight: dict[tuple[str, str], asyncio.Task[Cards]] = {}
        self._lookups: dict[str, asyncio.Task[tuple[str, float, Failed | None]]] = {}

    def stored(self, thread_id: str, message: dict[str, Any]) -> Cards:
        """The refreshed cards of `message` kept so far, by cardClientId. Blocking."""
        message_id = _text(message.get("id"))
        ids = [k for k in card_entries(message) if isinstance(k, str)]
        if not message_id or not ids:
            return {}
        rows = self._merged(thread_id, message_id, ids, self.store.load(thread_id, message_id))
        return {cid: row.card for cid, row in rows.items() if row.card is not None}

    def _merged(
        self, thread_id: str, message_id: str, ids: list[str], rows: dict[str, Row]
    ) -> dict[str, Row]:
        """The stored rows of the cards `ids` with this process's saves: the later card, the later retry."""
        out = dict(rows)
        for cid in ids:
            mine = self._saved.get((thread_id, message_id, cid))
            if mine is None:
                continue
            row = out.get(cid, Row(None, 0.0, 0.0))
            # Seconds: the store keeps refreshed_at to the second, and a tie is the same save.
            newer = mine.card is not None and (row.card is None or math.floor(mine.refreshed) > row.refreshed)
            out[cid] = Row(
                *((mine.card, mine.refreshed) if newer else (row.card, row.refreshed)),
                max(row.retry, mine.retry),
            )
        return out

    async def refresh(
        self, thread_id: str, message: dict[str, Any], *, me: str, skype_token: str, bearer: str
    ) -> Cards:
        """The message's cards after asking the bots of those due; one request per message at a time."""
        if (loop := asyncio.get_running_loop()) is not self._loop:
            if self._inflight and self._loop is not None and not self._loop.is_closed():
                raise RuntimeError("CardRefresher used from two event loops")
            self._bind(loop)
        key = (thread_id, _text(message.get("id")))
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._refresh(key, message, me, skype_token, bearer))
            self._inflight[key] = task
            # A callback, not a finally: it also runs for a task cancelled before it started.
            task.add_done_callback(
                lambda done: self._inflight.pop(key) if self._inflight.get(key) is done else None
            )
        # Shielded: one caller giving up does not cancel the request the others wait on.
        return await asyncio.shield(task)

    async def _refresh(
        self, key: tuple[str, str], message: dict[str, Any], me: str, skype_token: str, bearer: str
    ) -> Cards:
        thread_id, message_id = key
        entries = {k: e for k, e in card_entries(message).items() if isinstance(k, str)}
        if not message_id or not entries:
            return {}
        stored = await asyncio.to_thread(self.store.load, thread_id, message_id)
        rows = self._merged(thread_id, message_id, list(entries), stored)
        cards = {cid: row.card for cid, row in rows.items() if row.card is not None}
        if not skype_token or not bearer:
            return cards
        now = self.clock()
        due = []
        for cid, entry in entries.items():
            latest = {**entry, "content": cards[cid]} if cid in cards else entry
            action = refresh_action(latest, me)
            if action is not None and (cid not in rows or rows[cid].retry <= now):
                due.append((cid, _text(entry.get("appId")), action))
        if not due:
            return cards
        # One client per message, closed with it: it cannot outlive its loop.
        async with httpx.AsyncClient(transport=self._transport, timeout=_TIMEOUT) as client:
            got = await asyncio.gather(
                *(
                    self._one(client, thread_id, message, a, app, cid, skype_token, bearer)
                    for cid, app, a in due
                )
            )
        cards.update({cid: card for (cid, _, _), card in zip(due, got, strict=True) if card is not None})
        return cards

    async def _one(
        self,
        client: httpx.AsyncClient,
        thread_id: str,
        message: dict[str, Any],
        action: dict[str, Any],
        app_id: str,
        card_id: str,
        skype_token: str,
        bearer: str,
    ) -> dict[str, Any] | None:
        message_id = _text(message.get("id"))
        card = None
        try:
            async with self._sem:
                if not self._held(app_id):
                    bot = await self._bot(app_id, skype_token, bearer)
                    if not self._held(app_id):  # a 429 elsewhere may have held the app during the lookup
                        card = await self._invoke(
                            client, thread_id, message, action, app_id, bot, skype_token
                        )
        except Exception as exc:  # noqa: BLE001 — any failure keeps the stored card (037-AC5)
            failed = exc if isinstance(exc, Failed) else Failed("error")
            extra = {} if isinstance(exc, Failed) else {"error": type(exc).__name__}
            log.warning(
                "card_refresh_failed", reason=failed.reason, status=failed.status, app_id=app_id, **extra
            )
            if failed.reason == "throttled":
                retry = self._hold(app_id, failed.wait)
            else:
                retry = self.clock() + min(max(REFRESH_INTERVAL, failed.wait), _RETRY_MAX)
            await self._save(thread_id, message_id, card_id, None, retry)
            return None
        if card is None:  # held by a 429: no request, the card waits for the hold's end
            await self._save(thread_id, message_id, card_id, None, self._held(app_id) or self.clock())
            return None
        await self._save(thread_id, message_id, card_id, card, self.clock() + REFRESH_INTERVAL)
        return card

    def _held(self, app_id: str) -> float:
        """End of the app's 429 hold, 0 when it is not held."""
        until = self._holds.get(app_id, 0.0)
        if until and until <= self.clock():
            del self._holds[app_id]
            return 0.0
        return until

    def _hold(self, app_id: str, wait: float) -> float:
        """Hold the app for its 429's wait, never shortening a hold; the hold's end."""
        now = self.clock()
        for app in [a for a, end in self._holds.items() if end <= now]:
            del self._holds[app]
        until = max(self._holds.pop(app_id, 0.0), now + min(max(REFRESH_INTERVAL, wait), _RETRY_MAX))
        self._holds[app_id] = until
        while len(self._holds) > _HOLDS_MAX:
            self._holds.popitem(last=False)
        return until

    async def _save(
        self, thread_id: str, message_id: str, card_id: str, card: dict[str, Any] | None, retry: float
    ) -> None:
        now = self.clock()
        key = (thread_id, message_id, card_id)
        prev = self._saved.get(key)
        held = (card, now) if card is not None else (prev.card, prev.refreshed) if prev else (None, 0.0)
        self._saved[key] = Row(*held, retry)
        self._saved.move_to_end(key)
        while len(self._saved) > _SAVED_MAX:
            self._saved.popitem(last=False)
        await asyncio.to_thread(self.store.save, thread_id, message_id, card_id, card, now, retry)

    async def _bot(self, app_id: str, skype_token: str, bearer: str) -> str:
        """The app's bot id, cached per process (037-AC7); an app without one is negative-cached."""
        hit = self._bots.get(app_id)
        if hit is None or hit[1] <= self.clock():
            task = self._lookups.get(app_id)
            if task is None:
                task = asyncio.ensure_future(self._lookup(app_id, skype_token, bearer))
                self._lookups[app_id] = task
                task.add_done_callback(
                    lambda done: self._lookups.pop(app_id) if self._lookups.get(app_id) is done else None
                )
            # Shielded and on its own client: every first caller of the app shares this one lookup.
            hit = await asyncio.shield(task)
        else:
            self._bots.move_to_end(app_id)
        bot, _, failed = hit
        if failed is not None:
            raise Failed(failed.reason, failed.status, failed.wait)
        return bot

    async def _lookup(self, app_id: str, skype_token: str, bearer: str) -> tuple[str, float, Failed | None]:
        """One definition lookup, cached: the bot id, or the failure until its expiry."""
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=_TIMEOUT) as client:
                hit: tuple[str, float, Failed | None] = (
                    await self._fetch_bot(client, app_id, skype_token, bearer),
                    math.inf,
                    None,
                )
        except Failed as exc:
            # The throttled entry ends with the hold; a fresh copy: the raised one's traceback holds tokens.
            end = (
                self._hold(app_id, exc.wait) if exc.reason == "throttled" else self.clock() + REFRESH_INTERVAL
            )
            hit = ("", end, Failed(exc.reason, exc.status, exc.wait))
        self._bots[app_id] = hit
        self._bots.move_to_end(app_id)
        while len(self._bots) > _BOTS_MAX:
            self._bots.popitem(last=False)
        return hit

    async def _fetch_bot(self, client: httpx.AsyncClient, app_id: str, skype_token: str, bearer: str) -> str:
        headers = {
            "Authorization": f"Bearer {bearer}",
            "X-Skypetoken": skype_token,
            "User-Agent": self.settings.user_agent,
        }
        resp, body = await self._post(client, APPS_URL, headers, [app_id], _DEFINITIONS_MAX)
        if resp.status_code == 429 or (isinstance(body, dict) and body.get("statusCode") == 429):
            raise Failed("throttled", 429, _retry_wait(resp, body))
        if resp.status_code != 200:
            raise Failed("app_definition", resp.status_code)
        app = (
            next(
                (a for a in body if isinstance(a, dict) and a.get("id") == app_id),
                None,
            )
            if isinstance(body, list)
            else None
        )
        if app is None:
            raise Failed("app_definition", resp.status_code)
        for field, key in (("inputExtensions", "botId"), ("bots", "id")):
            items = app.get(field)
            first = items[0] if isinstance(items, list) and items else None
            bot = _text(first.get(key)) if isinstance(first, dict) else ""
            if bot:
                return bot
        raise Failed("no_bot", resp.status_code)

    async def _invoke(
        self,
        client: httpx.AsyncClient,
        thread_id: str,
        message: dict[str, Any],
        action: dict[str, Any],
        app_id: str,
        bot: str,
        skype_token: str,
    ) -> dict[str, Any]:
        url = f"https://{self.settings.contacts_host}/v1/agents/{quote(f'28:{bot}', safe='')}/invoke"
        headers = {
            "X-Skypetoken": skype_token,
            "User-Agent": self.settings.user_agent,
            "X-MS-AutoRefreshRequestInThread": thread_id,
        }
        payload = {
            "name": "adaptiveCard/action",
            "appId": app_id,
            "value": {
                "action": {"type": "Action.Execute", "verb": action["verb"], "data": action.get("data", {})},
                "trigger": "automatic",
            },
            "serverMessageId": _text(message.get("id")),
            "clientMessageId": _text(message.get("clientmessageid")),
            "conversation": {"id": thread_id},
        }
        resp, body = await self._post(client, url, headers, payload, _CARD_MAX)
        status = body.get("statusCode") if isinstance(body, dict) else None
        if resp.status_code == 429 or status == 429:
            raise Failed("throttled", 429, _retry_wait(resp, body))
        if resp.status_code != 200:
            raise Failed("http", resp.status_code)
        if not isinstance(body, dict) or status != 200:
            raise Failed("status", status if isinstance(status, int) else None)
        card = body.get("value")
        if body.get("type") != _ADAPTIVE or not isinstance(card, dict) or card.get("type") != "AdaptiveCard":
            raise Failed("not_a_card", 200)
        return card

    async def _post(
        self, client: httpx.AsyncClient, url: str, headers: dict[str, str], payload: object, limit: int
    ) -> tuple[httpx.Response, object]:
        """POST, the reply read up to `limit` bytes within `_POST_DEADLINE`; body None when it is not JSON."""
        try:
            async with (
                asyncio.timeout(_POST_DEADLINE),
                client.stream("POST", url, headers=headers, json=payload) as resp,
            ):
                raw = bytearray()
                async for chunk in resp.aiter_bytes():
                    raw += chunk
                    if len(raw) > limit:
                        raise Failed("too_large", resp.status_code)
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise Failed("timeout") from exc
        except httpx.HTTPError as exc:
            raise Failed("network") from exc
        try:
            body: object = json.loads(raw)
        except ValueError, RecursionError:
            body = None
        return resp, body
