"""Shared fixtures for the test suite."""

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import socket
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
import websockets

from miniteams.config import Settings
from miniteams.directory import Directory
from miniteams.follow import missing_extra

_MEMORY_CAP = 4 << 30  # bytes of address space; glibc reserves 64 MiB per thread arena
_NO_EXTRA = missing_extra()
_NO_EXTRA_WHY = f"transcript extra missing ({', '.join(_NO_EXTRA)}): uv run --extra transcript pytest"
# The test modules importing the extra, directly or through call_fakes, stt_helpers or stt.session.
_EXTRA_MODULES = {
    "test_call.py",
    "test_follower.py",
    "test_stt_parakeet.py",
    "test_stt_pipeline.py",
    "test_stt_pool.py",
    "test_stt_real.py",
    "test_stt_session.py",
    "test_stt_vad.py",
}


def _needs_extra() -> None:
    # Under CI a missing extra fails: the follower's tests must never silently stop running.
    if os.getenv("CI"):
        pytest.fail(_NO_EXTRA_WHY)
    pytest.skip(_NO_EXTRA_WHY)


class _NeedsExtra(pytest.File):
    def collect(self) -> Iterator[pytest.Item]:
        _needs_extra()
        yield from ()


def pytest_pycollect_makemodule(module_path: Path, parent: pytest.Collector) -> pytest.File | None:
    """The modules that import the transcript extra (spec 044) skip whole: they cannot even be imported."""
    if _NO_EXTRA and module_path.name in _EXTRA_MODULES:
        return _NeedsExtra.from_parent(parent, path=module_path)
    return None


def pytest_runtest_setup(item: pytest.Item) -> None:
    """`transcript`-marked tests of other modules need the extra too."""
    if _NO_EXTRA and item.get_closest_marker("transcript"):
        _needs_extra()


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
_STT_MEMORY_CAP = 8 << 30


@pytest.fixture(autouse=True, scope="module")
def _stt_memory(request: pytest.FixtureRequest) -> Iterator[None]:
    """A module of `stt` tests runs under a higher cap: MKL and onnxruntime reserve address space per
    thread far past 4 GiB while both real models stay under 2 GiB resident. Module scope: the models
    load in module fixtures, and engine children inherit the cap at spawn."""
    import resource

    marked = any(i.module is request.module and i.get_closest_marker("stt") for i in request.session.items)
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if not marked or soft == resource.RLIM_INFINITY:
        yield
        return
    resource.setrlimit(resource.RLIMIT_AS, (min(_STT_MEMORY_CAP, hard), hard))
    yield
    resource.setrlimit(resource.RLIMIT_AS, (soft, hard))


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
def _private_dirs(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings the code builds itself (`cli.main`) default under XDG: never the user's names.db or caches."""
    home = tmp_path_factory.mktemp("xdg")
    monkeypatch.setenv("MINITEAMS_TENANT_ID", "t")  # a clean checkout has no .env to read it from
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / "cache"))


@pytest.fixture(autouse=True)
def _fresh_token_sources() -> Any:
    from miniteams import auth

    auth._SOURCES.clear()
    yield
    auth._SOURCES.clear()


@pytest.fixture(autouse=True)
def _browser_reads_stay_local(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real chat page marks its chat read on open (spec 020): its `read` and `mute` verbs must not
    reach Teams; nor its themed chips Teams' CDN (spec 042)."""
    if request.node.get_closest_marker("browser"):
        from miniteams import emojis, web

        async def no_art(emoticon: str) -> bytes:
            raise OSError("no CDN in tests")

        monkeypatch.setattr(web, "mark_read", lambda *args: None)
        monkeypatch.setattr(web, "set_alerts", lambda *args: None)
        monkeypatch.setattr(emojis, "_download_art", no_art)


@pytest.fixture(autouse=True)
def _fresh_archive_pool() -> Iterator[None]:
    """Each test gets an open archive pool: the process closes it once, a test run many times."""
    from miniteams import chat_verbs

    yield
    chat_verbs.close_archive_pool()
    chat_verbs._closed = False


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


def auth_cookie(board: Any) -> dict[str, str]:
    """The `mt` cookie of a served board, in Playwright's `context.add_cookies` shape."""
    return {"name": "mt", "value": board.secret, "url": board.base_url}


def cookie_header(secret: str) -> dict[str, str]:
    """The `mt` cookie as a request header."""
    return {"Cookie": f"mt={secret}"}


@contextlib.asynccontextmanager
async def board_socket(host: str, secret: str, *, chat: bool = False) -> AsyncIterator[Any]:
    """A socket on the board at `host` as its served page opens one: own Origin, the `mt` cookie."""
    url = f"ws://{host}/ws{'?chat' if chat else ''}"
    headers = cookie_header(secret)
    async with websockets.connect(
        url, origin=websockets.Origin(f"http://{host}"), additional_headers=headers
    ) as ws:
        # 036-AC4: every page hears the Teams link first; tests asking for it connect without this helper.
        assert set(json.loads(await asyncio.wait_for(ws.recv(), 2))) == {"link"}
        yield ws


async def wait(cond: Callable[[], bool], what: str = "condition", tries: int = 500) -> None:
    """Poll `cond` every 10 ms; AssertionError after `tries` polls."""
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{what} not met in {tries / 100:g} s")
