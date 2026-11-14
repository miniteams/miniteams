"""structlog setup with TTY detection: console renderer on a terminal, JSON otherwise."""

import logging
import sys

import structlog


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
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
