"""Archive dumper (spec 001): resumable, reentrant per-chat message + metadata archive.

Two phases, each reentrant:
1. enumerate  → upsert every (private) chat into `data/index.db`;
2. per chat   → top-up (fetch new messages newest-first until they overlap what's stored), then
   backfill (walk older until the history is exhausted), committing one page at a time.

Resume state is the stored data itself (`ChatStore.oldest/newest`), so an interrupted run
resumes with no gap and no duplicate — see `archive_store` for the storage invariants.
"""

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
import structlog

from . import attachments, avatars
from .archive_store import ChatStore, Index
from .auth import TokenSource
from .chats import fetch_conversations, is_meeting, is_private
from .config import Settings
from .directory import Directory
from .dump import _epoch_seconds, iter_history_pages
from .skype import exchange_skype_token

log = structlog.get_logger()

_PAGE_SIZE = 100
_INTER_PAGE_DELAY = 0.2  # politeness pause between history pages (seconds)


class TokenProvider(Protocol):
    """Supplies a currently-valid skype token + bearer; may refresh under the hood."""

    def token(self) -> str: ...
    def bearer(self) -> str: ...


class StaticToken:
    """A fixed token pair (short single-thread runs, tests) — never refreshes."""

    def __init__(self, token: str, bearer: str = "") -> None:
        self._token, self._bearer = token, bearer

    def token(self) -> str:
        return self._token

    def bearer(self) -> str:
        return self._bearer


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


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    for page in fetch_conversations(settings, skype_token):
        for conv in page:
            thread_id = str(conv.get("id") or "")
            if thread_id and (include_all or is_private(thread_id) or is_meeting(thread_id)):
                targets.append(conv)
    return targets


def _topup(settings: Settings, skype_token: str, thread_id: str, store: ChatStore) -> int:
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


def _backfill(settings: Settings, skype_token: str, thread_id: str, store: ChatStore, index: Index) -> int:
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


async def _download_media(store: ChatStore, skype_token: str) -> int:
    """Best-effort pass: download every stored message's attachments at original quality.

    Runs after messages are committed, iterating stored data — so it is resumable (a crash mid-pass
    re-runs cheaply, skipping files already on disk) and never blocks message capture.
    """
    fetched = seen = 0
    start = time.monotonic()
    for message in store.iter_messages():
        content = message.get("content") or ""
        msgtype = str(message.get("messagetype") or "")
        if not attachments.extract(content, msgtype):
            continue  # no attachment → don't even open a client
        seen += 1
        try:
            notes = await attachments.process(content, msgtype, skype_token, store.media_dir, download=True)
        except Exception as exc:  # noqa: BLE001 — one message's media must not abort the pass
            log.debug("message_media_failed", id=message.get("id"), error=str(exc))
            continue
        fetched += sum(1 for n in notes if "→" in n)
        if seen % 50 == 0:
            log.info(
                "media_progress",
                thread=store.thread_id,
                with_media=seen,
                files=fetched,
                files_per_s=_rate(fetched, start),
            )
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
) -> None:
    directory.set_token(skype_token, bearer)
    info = await directory.thread(thread_id) or {}  # topic + roster + picture (best-effort)
    label = await directory.label(thread_id)
    # Store the full conversation object only when we have a real one (enumeration); a bare
    # single-thread run carries just the id, which must not clobber richer stored metadata.
    raw = conv if conv and len(conv) > 1 else None
    index.upsert_chat(
        thread_id,
        label=label,
        topic=info.get("topic") or "",
        participants=info.get("members") or [],
        raw=raw,
    )
    store = ChatStore(data_dir, thread_id)
    start = time.monotonic()
    try:
        new_top = _topup(settings, skype_token, thread_id, store)
        new_old = 0
        if not index.backfill_done(thread_id):
            new_old = _backfill(settings, skype_token, thread_id, store, index)
        media = await _download_media(store, skype_token) if download_media else 0
        avatars_n = await _download_avatars(store, info, skype_token, bearer) if download_avatars else 0
        index.touch(thread_id, _now_iso())
        log.info(
            "chat_archived",
            thread=thread_id,
            total=store.count(),
            new_recent=new_top,
            new_backfill=new_old,
            media=media,
            avatars=avatars_n,
            duration_s=round(time.monotonic() - start, 1),
            msgs_per_s=_rate(new_top + new_old, start),
        )
    finally:
        store.close()


async def run_archive(
    settings: Settings,
    data_dir: Path,
    *,
    token_provider: TokenProvider,
    thread: str | None = None,
    include_all: bool = False,
    download_media: bool = True,
    download_avatars: bool = True,
) -> None:
    index = Index(data_dir)
    directory = Directory(settings)
    try:
        targets = [{"id": thread}] if thread else _enumerate(settings, token_provider.token(), include_all)
        log.info("archive_start", chats=len(targets), data_dir=str(data_dir))
        for conv in targets:
            thread_id = str(conv.get("id") or "")
            if not thread_id:
                continue
            # One failing chat must not abort the archive of the rest.
            try:
                await archive_chat(
                    settings,
                    token_provider.token(),  # resolved per chat → picks up a mid-run token refresh
                    token_provider.bearer(),
                    thread_id,
                    data_dir,
                    directory,
                    index,
                    conv=conv,
                    download_media=download_media,
                    download_avatars=download_avatars,
                )
            except Exception as exc:  # noqa: BLE001 — per-chat isolation; resumes next run
                log.error("chat_archive_failed", thread=thread_id, error=str(exc))
        log.info("archive_done", chats=len(targets))
    finally:
        index.close()
