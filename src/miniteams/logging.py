"""structlog setup with TTY detection: console renderer on a terminal, JSON otherwise."""

import logging
import sys
from typing import Any, TextIO

import structlog


class SafePrintLogger(structlog.PrintLogger):
    """A log line that cannot be written (disk full, a stale NFS handle, a closed pipe) is lost, never
    raised into the code that logged it: a follower must still hang up."""

    def msg(self, message: str) -> None:
        try:
            super().msg(message)
        except OSError:
            pass

    log = debug = info = warn = warning = msg
    fatal = failure = err = error = critical = exception = msg


def _safe_factory(file: TextIO) -> Any:
    return lambda *_args: SafePrintLogger(file)


def setup_logging(level: str = "INFO") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    # Logs go to stderr so stdout stays a clean event stream (M3 dump).
    tty = sys.stderr.isatty()
    # The console renderer formats exc_info itself; JSON needs the traceback turned into a field.
    tail: list[structlog.typing.Processor] = (
        [structlog.dev.ConsoleRenderer()]
        if tty
        else [structlog.processors.format_exc_info, structlog.processors.JSONRenderer()]
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            *tail,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=_safe_factory(sys.stderr),
        cache_logger_on_first_use=True,
    )
