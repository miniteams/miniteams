"""Conversation history backfill / pull (purple-teams teams_fetch_conv_history_paginated).

`GET .../messages` with **time-windowed** pagination — there is no syncState/continuation token.
The server returns newest-first; we walk older pages by setting `endTime` to the oldest
`composetime` seen, then print oldest-first reusing the live-stream message renderer.
"""

import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from ._io import emit, force_blocking_stdout
from .config import Settings
from .directory import Directory
from .messages import print_resource, resource_to_record

log = structlog.get_logger()

_TARGET_TYPE = "Passport|Skype|Lync|Thread|PSTN|Agent"
_ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def _epoch_seconds(composetime: str) -> int:
    match = _ISO_RE.match(composetime or "")
    if not match:
        return 0
    dt = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    return int(dt.timestamp())


def fetch_history(
    settings: Settings, skype_token: str, thread_id: str, page_size: int, max_pages: int
) -> list[dict[str, Any]]:
    base = f"https://{settings.contacts_host}/v1/users/ME/conversations/{quote(thread_id, safe='')}/messages"
    headers = {
        "X-Skypetoken": skype_token,
        "User-Agent": settings.user_agent,
        "Accept": "application/json; ver=1.0;",
    }
    collected: list[dict[str, Any]] = []
    end_before: int | None = None  # epoch seconds; None ⇒ no endTime on the first page
    page = 0
    with httpx.Client(timeout=30.0, headers=headers) as client:
        while True:
            query = f"startTime=0000{f'&endTime={end_before}000' if end_before is not None else ''}"
            # targetType pipes are sent unescaped (matches the reference client).
            query += f"&pageSize={page_size}&view=msnp24Equivalent&targetType={_TARGET_TYPE}"
            resp = client.get(f"{base}?{query}")
            resp.raise_for_status()
            messages = resp.json().get("messages") or []
            if not messages:
                break
            collected.extend(messages)
            page += 1
            log.debug("history_page", page=page, count=len(messages), total=len(collected))
            if len(messages) < page_size:
                break
            if max_pages and page >= max_pages:
                log.info("history_truncated", pages=page, hint="raise --max-pages for more")
                break
            oldest = _epoch_seconds(messages[-1].get("composetime", ""))  # newest-first → last
            if oldest <= 0:
                break
            new_end = oldest - 1
            if end_before is not None and new_end >= end_before:
                break  # window not advancing — server ignored endTime; stop cleanly
            end_before = new_end

    # Page windows are second-granular and can overlap → de-dup by message id before emitting.
    unique: dict[str, dict[str, Any]] = {}
    for index, message in enumerate(collected):
        unique[str(message.get("id") or f"_{index}")] = message
    # API is newest-first; emit chronologically. ISO-Z composetime sorts lexically.
    return sorted(unique.values(), key=lambda m: m.get("composetime", ""))


async def dump_conversation(
    settings: Settings,
    skype_token: str,
    thread_id: str,
    page_size: int,
    max_pages: int,
    jsonl: bool = False,
) -> None:
    force_blocking_stdout()  # inside the running loop (see _io); guards `| jq` backpressure
    directory = Directory(settings)
    directory.set_token(skype_token)
    messages = fetch_history(settings, skype_token, thread_id, page_size, max_pages)
    log.info("history_fetched", thread=thread_id, count=len(messages))
    for resource in messages:
        if jsonl:
            # Full-detail metadata, every message (no printable filter), no media download.
            record = await resource_to_record(resource, directory)
            await emit(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        else:
            await print_resource(resource, directory)
