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

import structlog

from . import attachments
from .archive_store import ChatStore, Index
from .chats import fetch_conversations, is_private
from .config import Settings
from .directory import Directory
from .dump import _epoch_seconds, iter_history_pages

log = structlog.get_logger()

_PAGE_SIZE = 100
_INTER_PAGE_DELAY = 0.2  # politeness pause between history pages (seconds)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _enumerate(settings: Settings, skype_token: str, include_all: bool) -> list[str]:
    targets: list[str] = []
    for page in fetch_conversations(settings, skype_token):
        for conv in page:
            thread_id = str(conv.get("id") or "")
            if thread_id and (include_all or is_private(thread_id)):
                targets.append(thread_id)
    return targets


def _topup(settings: Settings, skype_token: str, thread_id: str, store: ChatStore) -> int:
    """Fetch messages newer than what's stored; stop as soon as a page overlaps the stored top."""
    known_newest = store.newest()
    if not known_newest:
        return 0  # empty store → backfill does the full pull; nothing to top up
    added = 0
    for messages in iter_history_pages(settings, skype_token, thread_id, _PAGE_SIZE, 0):
        added += store.insert_page(messages)
        oldest_in_page = str(messages[-1].get("composetime", ""))  # newest-first → last is oldest
        if oldest_in_page <= known_newest:
            break  # reached messages already stored — everything older is known
        time.sleep(_INTER_PAGE_DELAY)
    return added


def _backfill(settings: Settings, skype_token: str, thread_id: str, store: ChatStore, index: Index) -> int:
    """Walk older than the oldest stored message until history is exhausted, then flag done."""
    oldest = store.oldest()
    end_before = _epoch_seconds(oldest) - 1 if oldest else None
    added = 0
    for messages in iter_history_pages(settings, skype_token, thread_id, _PAGE_SIZE, 0, end_before):
        added += store.insert_page(messages)
        time.sleep(_INTER_PAGE_DELAY)
    # Loop drained naturally (short/empty page) — max_pages is 0, so this is a true end of history.
    index.mark_backfill_done(thread_id)
    return added


async def _download_media(store: ChatStore, skype_token: str) -> int:
    """Best-effort pass: download every stored message's attachments at original quality.

    Runs after messages are committed, iterating stored data — so it is resumable (a crash mid-pass
    re-runs cheaply, skipping files already on disk) and never blocks message capture.
    """
    fetched = 0
    for message in store.iter_messages():
        content = message.get("content") or ""
        msgtype = str(message.get("messagetype") or "")
        if not attachments.extract(content, msgtype):
            continue  # no attachment → don't even open a client
        try:
            notes = await attachments.process(content, msgtype, skype_token, store.media_dir, download=True)
        except Exception as exc:  # noqa: BLE001 — one message's media must not abort the pass
            log.debug("message_media_failed", id=message.get("id"), error=str(exc))
            continue
        fetched += sum(1 for n in notes if "→" in n)
    return fetched


async def archive_chat(
    settings: Settings,
    skype_token: str,
    thread_id: str,
    data_dir: Path,
    directory: Directory,
    index: Index,
    download_media: bool = True,
) -> None:
    info = await directory.thread(thread_id)  # topic + roster (best-effort, may be None)
    label = await directory.label(thread_id)
    index.upsert_chat(
        thread_id,
        label=label,
        topic=(info or {}).get("topic") or "",
        participants=(info or {}).get("members") or [],
    )
    store = ChatStore(data_dir, thread_id)
    try:
        new_top = _topup(settings, skype_token, thread_id, store)
        new_old = 0
        if not index.backfill_done(thread_id):
            new_old = _backfill(settings, skype_token, thread_id, store, index)
        media = await _download_media(store, skype_token) if download_media else 0
        index.touch(thread_id, _now_iso())
        log.info(
            "chat_archived",
            thread=thread_id,
            total=store.count(),
            new_recent=new_top,
            new_backfill=new_old,
            media=media,
        )
    finally:
        store.close()


async def run_archive(
    settings: Settings,
    skype_token: str,
    bearer: str,
    data_dir: Path,
    thread: str | None = None,
    include_all: bool = False,
    download_media: bool = True,
) -> None:
    index = Index(data_dir)
    directory = Directory(settings)
    directory.set_token(skype_token, bearer)
    try:
        targets = [thread] if thread else _enumerate(settings, skype_token, include_all)
        log.info("archive_start", chats=len(targets), data_dir=str(data_dir))
        for thread_id in targets:
            # One failing chat must not abort the archive of the rest.
            try:
                await archive_chat(
                    settings, skype_token, thread_id, data_dir, directory, index, download_media
                )
            except Exception as exc:  # noqa: BLE001 — per-chat isolation; resumes next run
                log.error("chat_archive_failed", thread=thread_id, error=str(exc))
        log.info("archive_done", chats=len(targets))
    finally:
        index.close()
