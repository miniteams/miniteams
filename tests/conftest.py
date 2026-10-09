"""Shared fixtures for the test suite."""

import ipaddress
import logging
import socket
import sys
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from miniteams.config import Settings
from miniteams.directory import Directory

_MEMORY_CAP = 4 << 30  # bytes of address space; glibc reserves 64 MiB per thread arena


def _cap_memory() -> None:
    """A runaway test fails with MemoryError instead of pushing the host into swap."""
    try:
        import resource
    except ImportError:  # not on this platform
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    cap = _MEMORY_CAP if hard == resource.RLIM_INFINITY else min(_MEMORY_CAP, hard)
    if soft == resource.RLIM_INFINITY or soft > cap:
        resource.setrlimit(resource.RLIMIT_AS, (cap, hard))


_cap_memory()


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


def _loopback(address: Any) -> bool:
    if not isinstance(address, tuple):  # AF_UNIX path
        return True
    host = str(address[0]).strip("[]")
    if host in ("localhost", ""):
        return True
    # IP literals only: a name like 127.0.0.1.nip.io would reach a real DNS server.
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Any socket leaving loopback fails the test. Recorded and asserted at teardown too: code that
    turns a connect error into a quiet fallback would otherwise hide the attempt."""
    tried: list[str] = []
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_sendto, real_sendmsg = socket.socket.sendto, socket.socket.sendmsg
    real_getaddrinfo = socket.getaddrinfo

    def refuse(what: Any) -> None:
        tried.append(str(what))
        raise ConnectionRefusedError(f"network blocked in tests: {what}")

    def connect(self: socket.socket, address: Any) -> None:
        if not _loopback(address):
            refuse(address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        if not _loopback(address):
            refuse(address)
        return real_connect_ex(self, address)

    def sendto(self: socket.socket, data: Any, *args: Any) -> int:
        if not _loopback(args[-1]):  # (data, address) or (data, flags, address)
            refuse(args[-1])
        return real_sendto(self, data, *args)

    def sendmsg(self: socket.socket, buffers: Any, *args: Any) -> int:
        if len(args) >= 3 and not _loopback(args[2]):  # (buffers, ancdata, flags, address)
            refuse(args[2])
        return real_sendmsg(self, buffers, *args)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else host
        if name is not None and not _loopback((name, 0)):
            refuse(name)
        return real_getaddrinfo(host, *args, **kwargs)

    # asyncio and anyio connect through socket.socket.connect and resolve through socket.getaddrinfo;
    # UDP needs no connect, so sendto/sendmsg are guarded too.
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket.socket, "sendmsg", sendmsg)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
    assert tried == [], f"test tried the network: {tried}"


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
