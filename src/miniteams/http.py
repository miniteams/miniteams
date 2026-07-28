"""Shared GET with Retry-After-aware backoff for the chat-service endpoints.

Both history and conversation-listing walks page the same rate-limited API; a 429 must slow
the caller down, never crash it. Centralised here so every paged walk inherits the behaviour.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

_MAX_RETRIES = 5
_MAX_BACKOFF = 30.0  # ceiling for the exponential fallback when no Retry-After is given


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    header = resp.headers.get("Retry-After", "")
    return float(header) if header.isdigit() else min(2.0**attempt, _MAX_BACKOFF)


def get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    max_retries: int = _MAX_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """GET honoring `Retry-After` on 429; capped exponential backoff otherwise. Raises on 4xx/5xx
    other than a retryable 429, and after `max_retries` exhausted 429s."""
    for attempt in range(max_retries + 1):
        resp = client.get(url)
        if resp.status_code != 429 or attempt == max_retries:
            resp.raise_for_status()
            return resp
        delay = _retry_delay(resp, attempt)
        log.warning("rate_limited", url=url, attempt=attempt + 1, delay=delay)
        sleep(delay)
    raise AssertionError("unreachable")  # loop always returns or raises


async def aget_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_retries: int = _MAX_RETRIES,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """Async twin of `get_with_retry` for the attachment downloads.

    Not a wrapper around it: `time.sleep` under the archive's download semaphore would stall every
    other in-flight download on the same event loop, turning one rate-limited host into a global
    stall. `**kwargs` passes through headers/params/follow_redirects."""
    for attempt in range(max_retries + 1):
        resp = await client.get(url, **kwargs)
        if resp.status_code != 429 or attempt == max_retries:
            resp.raise_for_status()
            return resp
        delay = _retry_delay(resp, attempt)
        log.warning("rate_limited", url=url, attempt=attempt + 1, delay=delay)
        await sleep(delay)
    raise AssertionError("unreachable")  # loop always returns or raises
