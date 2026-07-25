"""Archive dumper (spec 001): resumable, reentrant per-chat message + metadata archive.

Two phases, each reentrant:
1. enumerate  → upsert every (private) chat into `data/index.db`;
2. per chat   → top-up (fetch new messages newest-first until they overlap what's stored), then
   backfill (walk older until the history is exhausted), committing one page at a time.

Resume state is the stored data itself (`ChatStore.oldest/newest`), so an interrupted run
resumes with no gap and no duplicate — see `archive_store` for the storage invariants.
"""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
import structlog

from . import attachments, avatars
from .archive_store import ChatStore, Index
from .auth import AuthExpired, TokenSource
from .chats import fetch_conversations, is_meeting, is_private, last_activity
from .config import Settings
from .directory import Directory
from .dump import _epoch_seconds, iter_history_pages
from .skype import exchange_skype_token

log = structlog.get_logger()

_PAGE_SIZE = 100
_INTER_PAGE_DELAY = 0.2  # politeness pause between history pages (seconds)
_MEDIA_CONCURRENCY = 8  # max concurrent attachment downloads per chat (429-friendly)


class TokenProvider(Protocol):
    """Supplies a currently-valid skype token + bearer; may refresh under the hood."""

    def token(self) -> str: ...
    def bearer(self) -> str: ...
    def sharepoint_token(self, host: str) -> str | None: ...
    def graph_token(self) -> str | None: ...


class StaticToken:
    """A fixed token pair (short single-thread runs, tests) — never refreshes."""

    def __init__(self, token: str, bearer: str = "") -> None:
        self._token, self._bearer = token, bearer

    def token(self) -> str:
        return self._token

    def bearer(self) -> str:
        return self._bearer

    def sharepoint_token(self, host: str) -> str | None:
        return None

    def graph_token(self) -> str | None:
        return None


class RefreshingToken:
    """Keeps the skype token + bearer live across a multi-hour run.

    The skype token expires in ~45–60 min — far shorter than a full-account archive — so it is
    re-minted (silent AAD refresh → re-exchange) a few minutes before expiry. A run that outlives
    the AAD refresh token stops cleanly on `AuthExpired`; re-running resumes.
    """

    _MARGIN = 300.0  # re-mint this many seconds before the reported expiry

    def __init__(self, settings: Settings, source: TokenSource) -> None:
        self.settings, self.source = settings, source
        self._token = self._bearer = ""
        self._deadline = 0.0

    def _ensure(self) -> None:
        if time.monotonic() < self._deadline:
            return
        aad = self.source.refresh()  # silent-only; raises AuthExpired when the RT is dead
        skype = exchange_skype_token(self.settings, aad["access_token"])
        self._token = skype["skype_token"]
        self._bearer = str(aad.get("id_token") or aad["access_token"])
        self._deadline = time.monotonic() + float(skype.get("expires_in") or 3600) - self._MARGIN

    def token(self) -> str:
        self._ensure()
        return self._token

    def bearer(self) -> str:
        self._ensure()
        return self._bearer

    def sharepoint_token(self, host: str) -> str | None:
        return self.source.sharepoint_token(host)

    def graph_token(self) -> str | None:
        return self.source.graph_token()


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reached_start(oldest_iso: str | None, created_at_ms: Any) -> bool | None:
    """True if the oldest stored message is at/before the thread's creation time — i.e. the
    backfill reached the very first message. None when createdat is unknown (can't tell)."""
    if not oldest_iso:
        return None
    try:
        created_s = int(created_at_ms) // 1000
    except TypeError, ValueError:
        return None  # createdat missing/garbage → completeness unknown
    # createdat is the thread-creation event; the first message lands a beat later. Allow a
    # small margin so "reached the start" isn't missed by that offset.
    return _epoch_seconds(oldest_iso) <= created_s + 5


def _rate(n: int, start: float) -> float:
    """Throughput (items/s) since `start`; the inter-page delay is included on purpose — this is
    the effective recovery rate, the number an ETA should be built from."""
    elapsed = time.monotonic() - start
    return round(n / elapsed, 1) if elapsed > 0 else 0.0


def _enumerate(settings: Settings, skype_token: str, include_all: bool) -> list[dict[str, Any]]:
    """Archive scope: private chats (1:1 + groups) AND meeting chats by default; --all adds
    channels and everything else. Broader than the `chats` browse view, which stays meeting-free.

    Returns the full conversation objects (not just ids) so their metadata — `lastMessage`,
    `version`, … — is persisted verbatim in index.db."""
    targets: list[dict[str, Any]] = []
    pages = seen = 0
    for page in fetch_conversations(settings, skype_token):
        pages += 1
        seen += len(page)
        for conv in page:
            thread_id = str(conv.get("id") or "")
            if thread_id and (include_all or is_private(thread_id) or is_meeting(thread_id)):
                targets.append(conv)
        log.info("enumerate_progress", pages=pages, scanned=seen, matched=len(targets))
    return targets


def _topup(settings: Settings, skype_token: str, thread_id: str, store: ChatStore, label: str) -> int:
    """Fetch messages newer than what's stored; stop as soon as a page overlaps the stored top."""
    known_newest = store.newest()
    if not known_newest:
        return 0  # empty store → backfill does the full pull; nothing to top up
    added = pages = 0
    start = time.monotonic()
    for messages in iter_history_pages(settings, skype_token, thread_id, _PAGE_SIZE, 0):
        added += store.insert_page(messages)
        pages += 1
        oldest_in_page = str(messages[-1].get("composetime", ""))  # newest-first → last is oldest
        log.info(
            "topup_progress",
            chat=label,
            thread=thread_id,
            pages=pages,
            new=added,
            oldest=oldest_in_page[:19],
            msgs_per_s=_rate(added, start),
        )
        if oldest_in_page <= known_newest:
            break  # reached messages already stored — everything older is known
        time.sleep(_INTER_PAGE_DELAY)
    return added


def _backfill(
    settings: Settings, skype_token: str, thread_id: str, store: ChatStore, index: Index, label: str
) -> int:
    """Walk older than the oldest stored message until history is exhausted, then flag done."""
    oldest = store.oldest()
    end_before = _epoch_seconds(oldest) - 1 if oldest else None
    added = pages = 0
    start = time.monotonic()
    for messages in iter_history_pages(settings, skype_token, thread_id, _PAGE_SIZE, 0, end_before):
        added += store.insert_page(messages)
        pages += 1
        oldest_in_page = str(messages[-1].get("composetime", ""))  # newest-first → last is oldest
        log.info(
            "backfill_progress",
            chat=label,
            thread=thread_id,
            pages=pages,
            new=added,
            oldest=oldest_in_page[:19],
            msgs_per_s=_rate(added, start),
        )
        time.sleep(_INTER_PAGE_DELAY)
    # Loop drained naturally (short/empty page) — max_pages is 0, so this is a true end of history.
    index.mark_backfill_done(thread_id)
    return added


async def _download_media(
    store: ChatStore,
    skype_token: str,
    label: str,
    sp_token: Callable[[str], str | None] | None = None,
    graph_token: Callable[[], str | None] | None = None,
    ignore_denied: bool = False,
) -> int:
    """Best-effort pass: download every stored message's attachments at original quality.

    Runs after messages are committed, iterating stored data — so it is resumable (a crash mid-pass
    re-runs cheaply, skipping files already on disk) and never blocks message capture. Downloads run
    over a bounded async pool sharing one connection pool; `_MEDIA_CONCURRENCY` caps in-flight
    requests to stay polite (429-friendly).

    Assets that came back 403 are remembered (`denied_assets`) and never re-polled — a deleted
    object or lost share permission stays 403 forever, and the loop mode would hammer it every
    cycle. `ignore_denied` (--verify-media / --assets-only) retries them; fresh 403s are recorded
    either way.
    """
    targets = [
        m
        for m in store.iter_messages()
        if attachments.extract(m.get("content") or "", str(m.get("messagetype") or ""))
    ]
    if not targets:
        return 0
    denied = set() if ignore_denied else store.denied_urls()
    skipped = 0

    def _skip(url: str) -> bool:
        nonlocal skipped
        if url in denied:
            skipped += 1
            return True
        return False

    def _on_fail(url: str, exc: Exception) -> None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 403:
            store.mark_denied(url, 403, _now_iso())

    fetched = done = 0
    start = time.monotonic()
    sem = asyncio.Semaphore(_MEDIA_CONCURRENCY)

    async def _one(message: dict[str, Any], client: httpx.AsyncClient) -> int:
        async with sem:  # cap concurrent downloads
            try:
                notes = await attachments.process(
                    message.get("content") or "",
                    str(message.get("messagetype") or ""),
                    skype_token,
                    store.media_dir,
                    download=True,
                    client=client,
                    sp_token=sp_token,
                    graph_token=graph_token,
                    skip_url=_skip,
                    on_fail=_on_fail,
                )
            except Exception as exc:  # noqa: BLE001 — one message's media must not abort the pass
                log.debug("message_media_failed", id=message.get("id"), error=str(exc))
                return 0
            return sum(1 for n in notes if "→" in n)

    async with httpx.AsyncClient(timeout=60.0) as client:
        tasks = [asyncio.create_task(_one(m, client)) for m in targets]
        for task in asyncio.as_completed(tasks):
            fetched += await task
            done += 1
            if done % 50 == 0:
                log.info(
                    "media_progress",
                    chat=label,
                    thread=store.thread_id,
                    done=done,
                    of=len(targets),
                    files=fetched,
                    files_per_s=_rate(fetched, start),
                )
    if skipped:
        log.info("media_denied_skipped", chat=label, thread=store.thread_id, skipped=skipped)
    return fetched


async def _download_avatars(store: ChatStore, info: dict[str, Any], skype_token: str, bearer: str) -> int:
    """Best-effort: group icon (from thread properties) + each member's profile picture."""
    saved = 0
    avatars_dir = store.dir / "avatars"
    async with httpx.AsyncClient(timeout=30.0) as client:
        picture = info.get("picture")
        if picture and await avatars.fetch_group_icon(client, picture, skype_token, avatars_dir):
            saved += 1
        for member in info.get("members") or []:
            if await avatars.fetch_user_avatar(client, member.get("mri", ""), bearer, avatars_dir):
                saved += 1
    return saved


async def archive_chat(
    settings: Settings,
    skype_token: str,
    bearer: str,
    thread_id: str,
    data_dir: Path,
    directory: Directory,
    index: Index,
    conv: dict[str, Any] | None = None,
    download_media: bool = True,
    download_avatars: bool = True,
    verify_media: bool = False,
    assets_only: bool = False,
    sp_token: Callable[[str], str | None] | None = None,
    graph_token: Callable[[], str | None] | None = None,
) -> dict[str, int]:
    """Returns per-chat counts (`new`, `media`, `avatars`) for the run recap."""
    directory.set_token(skype_token, bearer)
    counts = {"new": 0, "media": 0, "avatars": 0}
    # Assets-only fast pass: no history, no metadata, no avatars — just re-scan already-stored
    # messages and download any missing asset (skip-exists). For recovering media/transcripts
    # over an existing archive without paying for a full re-run.
    if assets_only:
        store = ChatStore(data_dir, thread_id)
        try:
            if store.count():
                media = await _download_media(
                    store, skype_token, thread_id, sp_token, graph_token, ignore_denied=verify_media
                )
                index.touch(thread_id, _now_iso())
                log.info("chat_assets", thread=thread_id, total=store.count(), media=media)
                counts["media"] = media
        finally:
            store.close()
        return counts

    # Fast-skip on resume: enumeration carries the chat's last activity; if it's already
    # backfilled and that activity is at/before our newest stored message, nothing changed —
    # skip the thread fetch, the top-up probe, and the media/avatar rescan entirely.
    # `verify_media` disables the skip so every message's assets are re-checked against disk
    # (skip-exists means only missing ones download) — covers a backfill that succeeded while
    # its media didn't (a prior --no-media run, download failures, an interrupted media pass).
    if conv and index.backfill_done(thread_id) and not verify_media:
        probe = ChatStore(data_dir, thread_id)
        newest, last = probe.newest(), last_activity(conv)
        probe.close()
        # Second granularity is enough for "unchanged"; sub-second arrivals are caught next run.
        if newest and last and _epoch_seconds(last) <= _epoch_seconds(newest):
            log.info("chat_unchanged", thread=thread_id, newest=newest[:19])
            index.touch(thread_id, _now_iso())
            return counts

    info = await directory.thread(thread_id) or {}  # topic + roster + picture (best-effort)
    label = await directory.label(thread_id)
    # The directory fetch is itself denied on meeting chats whose access was revoked (403);
    # the enumeration raw still carries the topic — use it so index + logs show a name, not the id.
    topic = str(info.get("topic") or ((conv or {}).get("threadProperties") or {}).get("topic") or "")
    if label == thread_id and topic:
        label = topic
    log.info("chat_start", chat=label, thread=thread_id)
    # Store the full conversation object only when we have a real one (enumeration); a bare
    # single-thread run carries just the id, which must not clobber richer stored metadata.
    raw = conv if conv and len(conv) > 1 else None
    index.upsert_chat(
        thread_id,
        label=label,
        topic=topic,
        participants=info.get("members") or [],
        raw=raw,
    )
    store = ChatStore(data_dir, thread_id)
    start = time.monotonic()
    try:
        new_top = _topup(settings, skype_token, thread_id, store, label)
        new_old = 0
        # Empty store must re-backfill even when flagged done: a meeting chat archived BEFORE its
        # meeting drains an empty history and gets marked done — top-up then starts from nothing
        # (no overlap bound) and would never fetch, freezing the chat empty forever.
        if not index.backfill_done(thread_id) or not store.count():
            new_old = _backfill(settings, skype_token, thread_id, store, index, label)
        media = (
            await _download_media(
                store, skype_token, label, sp_token, graph_token, ignore_denied=verify_media
            )
            if download_media
            else 0
        )
        avatars_n = await _download_avatars(store, info, skype_token, bearer) if download_avatars else 0
        index.touch(thread_id, _now_iso())
        counts = {"new": new_top + new_old, "media": media, "avatars": avatars_n}
        log.info(
            "chat_archived",
            chat=label,
            thread=thread_id,
            total=store.count(),
            new_recent=new_top,
            new_backfill=new_old,
            media=media,
            avatars=avatars_n,
            oldest=(store.oldest() or "")[:19],
            # Positive-only completeness confirmation: True = oldest stored message reaches the
            # thread's creation. False/None is NOT a problem — a thread can exist before its first
            # message (meetings scheduled ahead of any chat); the real end-of-history guarantee is
            # that the backfill drained to an empty page.
            reached_start=_reached_start(store.oldest(), info.get("created_at")),
            duration_s=round(time.monotonic() - start, 1),
            msgs_per_s=_rate(new_top + new_old, start),
        )
    finally:
        store.close()
    return counts


async def run_archive(
    settings: Settings,
    data_dir: Path,
    *,
    token_provider: TokenProvider,
    thread: str | None = None,
    include_all: bool = False,
    download_media: bool = True,
    download_avatars: bool = True,
    verify_media: bool = False,
    assets_only: bool = False,
    retry_denied: bool = False,
) -> bool:
    """Returns True when the run stopped on AuthExpired (re-login needed), False otherwise."""
    # Absolute: downloaded media paths are turned into file:// URIs (Path.as_uri), which rejects
    # relative paths — a relative --data-dir would otherwise fail every attachment.
    data_dir = data_dir.resolve()
    index = Index(data_dir)
    directory = Directory(settings)
    run_start = time.monotonic()
    totals = {"new": 0, "media": 0, "avatars": 0}
    failed = denied = 0
    auth_expired = False
    try:
        if thread:
            targets: list[dict[str, Any]] = [{"id": thread}]
        elif assets_only:
            # Recover assets over what's already archived — no network enumeration needed.
            targets = [{"id": c["id"]} for c in index.chats()]
        else:
            # Enumeration is the first token use of a run — in --loop mode it is where a refresh
            # token that died during the sleep surfaces, so it needs the same clean stop as the
            # per-chat path below.
            try:
                targets = _enumerate(settings, token_provider.token(), include_all)
            except AuthExpired:
                log.error("archive_auth_expired", done=0, remaining=0, hint="run `miniteams login`")
                targets = []
                auth_expired = True
        log.info("archive_start", chats=len(targets), data_dir=str(data_dir), assets_only=assets_only)
        # Chats whose history came back 403: access does not come back on its own, so they are
        # skipped entirely (no directory fetch, no probe) until --retry-denied. An explicit
        # --thread run is its own retry request.
        denied_ids = set() if (retry_denied or thread or assets_only) else index.history_denied_ids()
        done = 0
        for conv in targets:
            thread_id = str(conv.get("id") or "")
            if not thread_id:
                continue
            if thread_id in denied_ids:
                log.info("chat_skipped_denied", thread=thread_id, hint="--retry-denied to re-attempt")
                denied += 1
                done += 1
                continue
            # Resolve the token OUTSIDE the per-chat guard: a dead refresh token (AuthExpired) is
            # fatal to the whole run — re-auth is needed — so stop loudly instead of cascading it
            # into one "chat_archive_failed" per remaining chat and a uselessly "done" run.
            try:
                skype_token, bearer = token_provider.token(), token_provider.bearer()
            except AuthExpired:
                log.error(
                    "archive_auth_expired",
                    done=done,
                    remaining=len(targets) - done,
                    hint="re-run to resume from here",
                )
                auth_expired = True
                break
            # One failing chat must not abort the archive of the rest.
            try:
                counts = await archive_chat(
                    settings,
                    skype_token,
                    bearer,
                    thread_id,
                    data_dir,
                    directory,
                    index,
                    conv=conv,
                    download_media=download_media,
                    download_avatars=download_avatars,
                    verify_media=verify_media,
                    assets_only=assets_only,
                    sp_token=token_provider.sharepoint_token,
                    graph_token=token_provider.graph_token,
                )
            except httpx.HTTPStatusError as exc:
                # A chat can enumerate while its history is 403 (meeting access revoked): a known
                # permanent state, not a run failure. Persisted — later runs skip the chat without
                # a single request; --retry-denied (or --thread) forces a new attempt.
                if exc.response.status_code == 403:
                    log.warning("chat_history_denied", thread=thread_id)
                    index.mark_history_denied(thread_id, _now_iso())
                    denied += 1
                else:
                    log.error("chat_archive_failed", thread=thread_id, error=str(exc))
                    failed += 1
            except Exception as exc:  # noqa: BLE001 — per-chat isolation; resumes next run
                log.error("chat_archive_failed", thread=thread_id, error=str(exc))
                failed += 1
            else:
                # A successful pass proves history access — lift any stored denial (forced retry
                # that worked, or access restored).
                if not assets_only:
                    index.clear_history_denied(thread_id)
                for key in totals:
                    totals[key] += counts[key]
            done += 1
        log.info(
            "archive_recap",
            chats=done,
            of=len(targets),
            failed=failed,
            denied=denied,
            new_messages=totals["new"],
            media_files=totals["media"],
            avatars=totals["avatars"],
            duration_s=round(time.monotonic() - run_start, 1),
            auth_expired=auth_expired,
        )
    finally:
        index.close()
    return auth_expired
