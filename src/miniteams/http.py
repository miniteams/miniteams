"""Shared GET with Retry-After-aware backoff for the chat-service endpoints.

Both history and conversation-listing walks page the same rate-limited API; a 429 must slow
the caller down, never crash it. Centralised here so every paged walk inherits the behaviour.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

_MAX_RETRIES = 5
_MAX_BACKOFF = 30.0  # ceiling for the exponential backoff, which a longer Retry-After overrides


def failure(exc: Exception) -> str:
    """Reason a failed Teams call shows on the page or in a log: never a body or a URL."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Teams answered {exc.response.status_code}"
    if isinstance(exc, httpx.TransportError):
        return "network error"
    return str(exc)[:200]


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    # Retry-After is a floor: after its first wait Teams answers `Retry-After: 0` while the
    # conversation's 15-per-10s quota is still full, and taking it literally burns every retry.
    header = resp.headers.get("Retry-After", "")
    backoff = min(2.0**attempt, _MAX_BACKOFF)
    return max(float(header), backoff) if header.isdigit() else backoff


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


@asynccontextmanager
async def astream_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_retries: int = _MAX_RETRIES,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    **kwargs: Any,
) -> AsyncIterator[httpx.Response]:
    """Streaming twin of `aget_with_retry`: `async with astream_with_retry(...) as resp`.

    A 429 is decided on the headers alone, before any body byte is consumed, so the retry costs
    no bandwidth — what matters for the GB-scale recordings the streamed paths carry."""
    for attempt in range(max_retries + 1):
        async with client.stream("GET", url, **kwargs) as resp:
            if resp.status_code != 429 or attempt == max_retries:
                resp.raise_for_status()
                yield resp
                return
            delay = _retry_delay(resp, attempt)
        log.warning("rate_limited", url=url, attempt=attempt + 1, delay=delay)
        await sleep(delay)
    raise AssertionError("unreachable")  # loop always returns or raises
