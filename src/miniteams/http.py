"""Shared GET with Retry-After-aware backoff for the chat-service endpoints.

Both history and conversation-listing walks page the same rate-limited API; a 429 must slow
the caller down, never crash it. Centralised here so every paged walk inherits the behaviour.
"""

import time
from collections.abc import Callable

import httpx
import structlog

log = structlog.get_logger()

_MAX_RETRIES = 5
_MAX_BACKOFF = 30.0  # ceiling for the exponential fallback when no Retry-After is given


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
        header = resp.headers.get("Retry-After", "")
        delay = float(header) if header.isdigit() else min(2.0**attempt, _MAX_BACKOFF)
        log.warning("rate_limited", url=url, attempt=attempt + 1, delay=delay)
        sleep(delay)
    raise AssertionError("unreachable")  # loop always returns or raises
