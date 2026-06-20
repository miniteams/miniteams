"""Low-level stdout helpers shared by the commands.

Two concerns, both about piping bulk output into a slow reader (`… | jq`):

1. The event loop / parent (uv, some shells) can hand us a **non-blocking** stdout. A write
   then raises `BlockingIOError` mid-stream under pipe backpressure and truncates output.
   `force_blocking_stdout` clears `O_NONBLOCK` so writes wait for the reader instead.
2. A blocking write on the **event-loop thread** would freeze the loop while the pipe is full —
   in `stream` that starves the keepalive ping and drops the socket. `emit` runs the write on a
   worker thread so the loop keeps servicing the websocket.

Async commands must call `force_blocking_stdout` *inside* the running loop — the loop otherwise
re-applies non-blocking after an early call.
"""

import asyncio
import contextlib
import os
import sys


def force_blocking_stdout() -> None:
    with contextlib.suppress(OSError, ValueError):  # redirected/closed/no-fileno stdout
        os.set_blocking(sys.stdout.fileno(), True)


def _write_blocking(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


async def emit(text: str) -> None:
    """Write+flush stdout off the event-loop thread (requires a running loop)."""
    await asyncio.to_thread(_write_blocking, text)
