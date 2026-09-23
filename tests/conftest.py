"""Shared fixtures for the test suite."""

import logging
import sys
from typing import Any

import pytest
import structlog

from miniteams.config import Settings
from miniteams.directory import Directory


@pytest.fixture(autouse=True)
def _quiet_logs() -> None:
    # Route structlog to the real stderr at WARNING — without this its default config writes
    # info/debug to stdout and pollutes capsys assertions on the printed message stream.
    structlog.configure(
        processors=[structlog.processors.add_log_level, structlog.processors.KeyValueRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        logger_factory=structlog.PrintLoggerFactory(file=sys.__stderr__),
        cache_logger_on_first_use=False,
    )


@pytest.fixture(autouse=True)
def _fresh_token_sources() -> Any:
    from miniteams import auth

    auth._SOURCES.clear()
    yield
    auth._SOURCES.clear()


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    s = Settings(tenant_id="tenant-123")
    s.config_dir = tmp_path / "cfg"
    s.cache_dir = tmp_path / "cache"
    return s


@pytest.fixture
def directory(settings: Settings) -> Directory:
    d = Directory(settings)
    d.set_token("sk")
    return d
