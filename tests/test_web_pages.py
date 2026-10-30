"""The served pages and the socket handshake (spec 011 fix rounds): the persisted cookie secret, the
launch token, the page headers, and ui.css / ui.js inlined into both pages."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import socket
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
import structlog
import structlog.testing
import websockets
from conftest import auth_cookie, board_socket, cookie_header
from test_web_chat import _cookie, _served

from miniteams import auth
from miniteams import chat_verbs as CV
from miniteams import web as W
from miniteams.config import Settings

B = "19:bbbb@thread.v2"


def _board() -> W.Board:
    return W.Board({B: {"id": B, "label": "B", "last_activity": "2026-10-09T10:00:00Z", "seen_at": None}})


async def _refused(url: str, host: str, cookie: str | None) -> int:
    headers = {"Cookie": cookie} if cookie is not None else None
    with pytest.raises(websockets.InvalidStatus) as exc:
        await websockets.connect(url, origin=websockets.Origin(f"http://{host}"), additional_headers=headers)
    return int(exc.value.response.status_code)


async def test_the_socket_needs_the_cookie(settings: Settings) -> None:
    """M2: the Origin is forgeable by any local process: `/ws` also needs the `mt` cookie; the launch
    token on the socket opens nothing (and is not spent)."""
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    board = _board()
    with structlog.testing.capture_logs() as logs:
        async with _served(board, settings) as host:
            secret = board.secret
            assert len(secret) >= 43  # token_urlsafe(32)
            for path in ("/ws", "/ws?chat"):
                for cookie in (
                    None,
                    "",
                    "mt=",
                    f"mt={secret[:-1]}",
                    f"mt={secret}x",
                    f"x={secret}",
                    f"mtx={secret}",
                ):
                    assert await _refused(f"ws://{host}{path}", host, cookie) == 403, (path, cookie)
                token = board.token
                with pytest.raises(websockets.InvalidStatus) as exc:
                    await websockets.connect(
                        f"ws://{host}{path}",
                        origin=websockets.Origin(f"http://{host}"),
                        additional_headers={"X-Miniteams-Token": token},
                    )
                assert exc.value.response.status_code == 403 and board.token == token, path
            async with board_socket(host, secret) as ws:
                assert "rows" in str(await ws.recv())
            async with board_socket(host, secret, chat=True):
                pass
            # Among other cookies, as a browser sends them.
            mixed = f"theme=dark; mt={secret}; other=1"
            async with websockets.connect(
                f"ws://{host}/ws",
                origin=websockets.Origin(f"http://{host}"),
                additional_headers={"Cookie": mixed},
            ) as ws:
                assert "link" in str(await ws.recv())
    assert secret not in repr(logs)


def test_the_secret_persists_owner_only(settings: Settings) -> None:
    """M2 + M10: one secret per config dir, reread at start, created 0600 in a 0700 dir; an unusable file
    is replaced, and so is one others could have read or planted (group/other bits, a symlink)."""
    secret = W.load_secret(settings)
    path = settings.config_dir / "web-secret"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.config_dir.stat().st_mode) == 0o700
    assert W.load_secret(settings) == secret
    for bad in ("", "short", "x" * 40 + ";" + "y"):
        path.write_text(bad)
        fresh = W.load_secret(settings)
        assert fresh not in (bad, secret) and re.fullmatch(r"[\w-]{43}", fresh)
        assert path.read_text() == fresh
    for mode in (0o644, 0o640, 0o604):
        exposed = "e" * 43
        path.write_text(exposed)
        path.chmod(mode)
        assert W.load_secret(settings) != exposed, oct(mode)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, oct(mode)
    planted = settings.config_dir / "planted"
    planted.write_text("p" * 43)
    planted.chmod(0o600)
    path.unlink()
    path.symlink_to(planted)
    assert W.load_secret(settings) != "p" * 43
    assert not path.is_symlink() and planted.read_text() == "p" * 43  # the link is replaced, not followed
    path.unlink()
    os.mkfifo(path)
    got: list[str] = []
    reader = threading.Thread(target=lambda: got.append(W.load_secret(settings)), daemon=True)
    reader.start()
    reader.join(5)  # a blocking open would hang here: fail instead
    assert got and re.fullmatch(r"[\w-]{43}", got[0]) and path.is_file()


def test_an_unreadable_secret_is_one_clear_error(settings: Settings) -> None:
    """L5: no traceback, the path named."""
    W.load_secret(settings)
    path = settings.config_dir / "web-secret"
    path.chmod(0)
    if os.access(path, os.R_OK):
        pytest.skip("running as root")
    with pytest.raises(RuntimeError, match=f"cannot read {re.escape(str(path))}: Permission denied") as exc:
        W.load_secret(settings)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_an_unwritable_secret_is_one_clear_error(settings: Settings) -> None:
    settings.config_dir.mkdir(parents=True)
    settings.config_dir.chmod(0o500)
    path = settings.config_dir / "web-secret"
    try:
        if os.access(settings.config_dir, os.W_OK):
            pytest.skip("running as root")
        with pytest.raises(
            RuntimeError, match=f"cannot write {re.escape(str(path))}: Permission denied"
        ) as exc:
            W.load_secret(settings)
    finally:
        settings.config_dir.chmod(0o700)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__ and not path.exists()


def test_the_token_files_are_owner_only(settings: Settings) -> None:
    """M10: the skype token, the endpoint ids and the MSAL cache go through write_private."""
    from miniteams import skype, trouter

    settings.config_dir.mkdir(parents=True)
    settings.config_dir.chmod(0o755)
    skype.persist_skype_token(settings, {"skype_token": "x"})
    trouter.get_or_create_epid(settings, "endpoint_id-test")

    class _Cache:
        has_state_changed = True

        def serialize(self) -> str:
            return "{}"

    auth._persist_cache(_Cache(), settings.config_dir / "msal_cache.json")  # type: ignore[arg-type]
    for name in ("skype_token.json", "endpoint_id-test", "msal_cache.json"):
        assert stat.S_IMODE((settings.config_dir / name).stat().st_mode) == 0o600, name
    assert sorted(os.listdir(settings.config_dir)) == [
        "endpoint_id-test",
        "msal_cache.json",
        "skype_token.json",
    ]


def _bind(settings: Settings) -> Path:
    W.save_bind(settings, "127.0.0.1", 1, "tok")
    return settings.config_dir / "web.json"


def _marks(settings: Settings) -> Path:
    W._save_marks(settings.config_dir / "seen.json", {"a": "b"})
    return settings.config_dir / "seen.json"


def _names(settings: Settings) -> Path:
    from miniteams.directory import Directory

    Directory(settings).note_name("8:orgid:ann", "Ann Lee")
    return settings.config_dir / "names.db"


def _sent(settings: Settings) -> Path:
    from miniteams.watch import note_sent

    note_sent(settings, "c1")
    return settings.cache_dir / "mcp-sent.json"


@pytest.mark.parametrize("write", [_bind, _marks, _names, _sent])
def test_the_state_files_are_owner_only(settings: Settings, write: Callable[[Settings], Path]) -> None:
    """M5: the bind, the marks, the names cache and the sent ids go through write_private, umask or not."""
    old = os.umask(0o022)
    try:
        path = write(settings)
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def _web_json(settings: Settings) -> dict[str, object]:
    path = settings.config_dir / "web.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return dict(json.loads(path.read_text()))


async def _auth(client: httpx.AsyncClient, host: str, *tokens: str, **headers: str) -> httpx.Response:
    sent = [("X-Miniteams-Token", token) for token in tokens] + list(headers.items())
    return await client.get(f"http://{host}/auth", headers=sent)


async def test_the_launch_token_buys_the_cookie_once(settings: Settings) -> None:
    """web.json holds host, port and a single-use token: /auth trades it (header only) for the cookie and
    writes a new one; a spent, wrong, missing, doubled or URL-borne token gets 403, a foreign Host 404.
    Neither the secret nor a token is ever logged, and no launch file is written (024-AC8)."""
    board = _board()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    with structlog.testing.capture_logs() as logs:
        async with _served(board, settings) as host, httpx.AsyncClient() as client:
            first = board.token
            port = int(host.rsplit(":")[1])
            assert _web_json(settings) == {"host": "127.0.0.1", "port": port, "token": first}
            got = await _auth(client, host, first, Host="evil.example")
            assert got.status_code == 404 and board.token == first  # not spent
            for bad in ((), ("",), (first[:-1],), (first + "x",), (first, first)):
                got = await _auth(client, host, *bad)
                assert (got.status_code, board.token) == (403, first), bad
            got = await client.get(f"http://{host}/auth?{first}")
            assert got.status_code == 403 and "set-cookie" not in got.headers
            got = await _auth(client, host, first)
            cookie = f"mt={board.secret}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"
            assert (got.status_code, got.headers["set-cookie"], got.headers["cache-control"]) == (
                204,
                cookie,
                "no-store",
            )
            assert board.token != first and _web_json(settings)["token"] == board.token
            client.cookies.clear()
            got = await _auth(client, host, first)  # spent
            assert got.status_code == 403 and "set-cookie" not in got.headers
    for secret in (board.secret, first, board.token):
        assert secret not in repr(logs)
    assert sorted(p.name for p in settings.config_dir.iterdir()) == ["web-secret", "web.json"]
    [listening] = [e for e in logs if e["event"] == "web_listening"]
    web_json = str(settings.config_dir / "web.json")
    assert (listening["url"], listening["token_in"]) == (f"http://{host}/", web_json)


async def test_a_failed_web_json_write_keeps_the_token(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L1: re-minting into web.json fails: the token it still holds stays the valid one."""
    board = _board()
    async with _served(board, settings) as host, httpx.AsyncClient() as client:
        token = board.token

        def fail(*_: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(W, "write_private", fail)
        with structlog.testing.capture_logs() as logs:
            assert (await _auth(client, host, token)).status_code == 204
        assert board.token == token and _web_json(settings)["token"] == token
        assert [e["error"] for e in logs if e["event"] == "web_json_failed"] == ["disk full"]
        monkeypatch.undo()
        assert (await _auth(client, host, token)).status_code == 204
        assert board.token != token and _web_json(settings)["token"] == board.token


async def test_a_bind_run_leaves_another_servers_web_json(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """A --bind run sharing the config dir must not take over the live server's launch URL: it prints
    its own on stderr, again after each use re-mints it."""
    W.save_bind(settings, "127.0.0.9", 40000, "live")
    before = _web_json(settings)
    board = _board()
    async with _served(board, settings) as host, httpx.AsyncClient() as client:
        assert _web_json(settings) == before
        assert capsys.readouterr().err == f"http://{host}/#{board.token}\n"
        assert (await _auth(client, host, board.token)).status_code == 204  # --browser opens still work
    assert _web_json(settings) == before
    assert capsys.readouterr().err == f"http://{host}/#{board.token}\n"


async def test_a_re_mint_re_checks_who_publishes(settings: Settings) -> None:
    """M1: a --bind run that published at start stops once web.json names another server, so two runs
    never take turns overwriting it."""
    board = _board()
    async with _served(board, settings) as host, httpx.AsyncClient() as client:
        assert _web_json(settings)["token"] == board.token
        W.publish(settings, "127.0.0.9", 40000, False, "other")
        other = _web_json(settings)
        assert (await _auth(client, host, board.token)).status_code == 204
        assert _web_json(settings) == other


async def test_non_ascii_token_or_cookie_is_refused_not_a_crash(settings: Settings) -> None:
    """L2: header bytes >= 0x80 come in surrogate-escaped; comparing them must answer 403, not 500."""
    board = _board()
    async with _served(board, settings) as host:
        port = int(host.rsplit(":")[1])
        sent = (
            (b"/auth", b"X-Miniteams-Token: \xc3\xa9" + board.token.encode()),
            (b"/media/x", b"Cookie: mt=\xe9"),
        )
        for path, header in sent:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET " + path + b" HTTP/1.1\r\nHost: " + host.encode() + b"\r\n" + header + b"\r\n\r\n"
            )
            status = await asyncio.wait_for(reader.readline(), 5)
            writer.close()
            assert status.startswith(b"HTTP/1.1 403 "), (header, path, status)


async def test_a_second_run_on_the_same_port_leaves_the_live_web_json(settings: Settings) -> None:
    """M1: web.json is written only once bound: a run failing on the port keeps the live token."""
    board = _board()
    async with _served(board, settings) as host:
        live = _web_json(settings)
        with pytest.raises(OSError):
            await W.serve_board(_board(), settings, host)
        assert _web_json(settings) == live == {**live, "token": board.token}


async def test_pages_are_served_without_the_cookie(settings: Settings) -> None:
    """Pages are static shells: served without the cookie (no Set-Cookie, no secret, no token, and no
    hint whether a chat exists); with it, an unknown chat is a 404."""
    board = _board()
    async with _served(board, settings) as host, httpx.AsyncClient() as client:
        for path in ("/", "/?theme=dark", f"/chat/{quote(B, safe='')}", "/chat/unknown"):
            got = await client.get(f"http://{host}{path}")
            assert got.status_code == 200 and "set-cookie" not in got.headers, path
            assert board.secret not in got.text and board.token not in got.text, path
        assert (await client.get(f"http://{host}/chat/unknown", headers=_cookie(board))).status_code == 404


async def test_favicon_is_served_without_the_cookie(settings: Settings) -> None:
    board = _board()
    async with _served(board, settings) as host, httpx.AsyncClient() as client:
        for path in ("/", f"/chat/{quote(B, safe='')}"):
            assert '<link rel="icon" href="/favicon.svg">' in (await client.get(f"http://{host}{path}")).text
        got = await client.get(f"http://{host}/favicon.svg")
        assert (got.status_code, got.headers["content-type"]) == (200, "image/svg+xml")
        assert got.text.startswith("<svg") and "set-cookie" not in got.headers
        assert (
            await client.get(f"http://{host}/favicon.svg", headers={"Host": "evil.example"})
        ).status_code == 404


def test_favicon_is_the_logo() -> None:
    assert (
        W._page_path("favicon.svg").read_bytes()
        == (Path(__file__).parents[1] / "docs/assets/logo.svg").read_bytes()
    )


async def test_a_page_load_refreshes_the_cookie(settings: Settings) -> None:
    """L3: every authorised page load re-issues the cookie, so it expires a year after the last use."""
    board = _board()
    async with _served(board, settings) as host, httpx.AsyncClient(headers=_cookie(board)) as client:
        cookie = f"mt={board.secret}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"
        for path in ("/", "/?theme=dark", f"/chat/{quote(B, safe='')}"):
            got = await client.get(f"http://{host}{path}")
            assert (got.status_code, got.headers.get("set-cookie")) == (200, cookie), path
        for path in ("/emoji/x", "/nope"):
            assert "set-cookie" not in (await client.get(f"http://{host}{path}")).headers, path


async def test_every_data_route_needs_the_cookie(settings: Settings) -> None:
    """M2: without the cookie no media, emoji or other route is served."""
    board = _board()
    async with _served(board, settings) as host, httpx.AsyncClient() as client:
        thread = quote(B, safe="")
        for path in (f"/media/{thread}/x.png", "/emoji/x", "/nope"):
            for cookie in (None, f"mt={board.secret[1:]}", f"x={board.secret}"):
                headers = {"Cookie": cookie} if cookie else {}
                got = await client.get(f"http://{host}{path}", headers=headers)
                assert (got.status_code, got.text) == (403, "forbidden\n"), path
            assert (await client.get(f"http://{host}{path}", headers=_cookie(board))).status_code == 404, path


def test_auth_cookie_is_the_playwright_shape() -> None:
    board = _board()
    board.secret, board.base_url = "s" * 43, "http://127.0.0.9:40000"
    assert auth_cookie(board) == {"name": "mt", "value": "s" * 43, "url": "http://127.0.0.9:40000"}


async def test_pages_refuse_framing_sniffing_and_foreign_hosts(settings: Settings) -> None:
    """L1: both pages carry a hashed script-src, frame-ancestors 'none', nosniff and same-origin CORP; a
    foreign Host (DNS rebinding) cannot read a page."""
    board = _board()
    async with _served(board, settings) as host, httpx.AsyncClient(headers=_cookie(board)) as client:
        for path in ("/", "/?theme=dark", f"/chat/{quote(B, safe='')}"):
            got = await client.get(f"http://{host}{path}")
            assert got.status_code == 200, path
            csp = got.headers["content-security-policy"]
            script = got.text.split("<script>", 1)[1].split("</script>", 1)[0]
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            assert f"script-src 'sha256-{digest}';" in csp, path
            assert "unsafe-inline" not in csp.split("script-src", 1)[1].split(";", 1)[0], path
            assert "frame-ancestors 'none'" in csp, path
            assert got.headers["x-content-type-options"] == "nosniff", path
            assert got.headers["cross-origin-resource-policy"] == "same-origin", path
            assert got.headers["cache-control"] == "no-store", path
            for foreign in ("evil.example", f"evil.example:{host.rsplit(':', 1)[1]}", "localhost"):
                got = await client.get(f"http://{host}{path}", headers={"Host": foreign})
                assert got.status_code == 404, (path, foreign)


async def test_a_hung_archive_answers_503(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H2: a chat lookup or a media read past its deadline is a 503; the stuck thread is left behind."""
    release = threading.Event()

    def hung(*args: object) -> None:
        release.wait(5)

    board = W.Board({}, data_dir=tmp_path)
    monkeypatch.setattr(W, "archive_dir", hung)
    monkeypatch.setattr(board.chat, "media_file", hung)
    monkeypatch.setitem(CV._DEADLINES, "dir", 0.05)
    monkeypatch.setitem(CV._DEADLINES, "media", 0.05)
    try:
        async with _served(board, settings) as host, httpx.AsyncClient(headers=_cookie(board)) as client:
            for path in (f"/chat/{quote(B, safe='')}", f"/media/{quote(B, safe='')}/x.png"):
                got = await client.get(f"http://{host}{path}")
                assert got.status_code == 503, path
    finally:
        release.set()


@pytest.mark.parametrize("name", ["widget.html", "chat.html"])
def test_both_pages_inline_one_copy_of_the_shared_files(name: str) -> None:
    """H6: ui.css and ui.js are one source, inlined once into each page; no copied block is left."""
    page = W._page(name)
    ui_js, ui_css = (W._page_path(part).read_text(encoding="utf-8") for part in ("ui.js", "ui.css"))
    assert page.count(ui_js) == 1 and page.count(ui_css) == 1
    raw = W._page_path(name).read_text(encoding="utf-8")
    assert "/* shared:" not in raw
    # Each shared definition lives in ui.js only: a page redefining one in its scope would be a copy.
    for found in re.finditer(r"^  (?:const|let) (\w+) = ", ui_js, re.M):
        assert not re.search(rf"^  (?:const|let) {found[1]}\b", raw, re.M), (name, found[1])


@pytest.mark.parametrize("name", ["widget.html", "chat.html"])
def test_both_pages_send_through_the_one_queue(name: str) -> None:
    """017-AC9: the send queue lives in ui.js; each page builds it once and keeps no outbox, pump or
    direct image send of its own."""
    ui_js = W._page_path("ui.js").read_text(encoding="utf-8")
    raw = W._page_path(name).read_text(encoding="utf-8")
    assert re.search(r"^  const sendQueue = ", ui_js, re.M)
    assert len(re.findall(r"\bsendQueue\(", raw)) == 1, name
    assert not re.search(r"\b(?:outbox|pump)\b|\bimgs\.send\(", raw), name


def _copy_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("widget.html", "chat.html", "ui.js", "ui.css"):
        (tmp_path / name).write_bytes(W._page_path(name).read_bytes())
    monkeypatch.setattr(W, "_page_path", lambda name="widget.html": tmp_path / name)
    return tmp_path


def test_an_edit_to_a_shared_file_shows_without_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H6: the assembled page is read once per version, the mtimes of the page and of ui.js / ui.css."""
    root = _copy_package(tmp_path, monkeypatch)
    W._assembled.cache_clear()
    before, version = W._page("chat.html"), W.page_version("chat.html")
    W._page("chat.html")
    assert W._assembled.cache_info().hits == 1  # not read again per request
    ui = root / "ui.js"
    ui.write_text(ui.read_text() + "  // edited\n")
    os.utime(ui, ns=(ui.stat().st_atime_ns, ui.stat().st_mtime_ns + 1_000_000))
    assert W.page_version("chat.html") != version  # open tabs reload
    assert "// edited" in W._page("chat.html") and "// edited" not in before


def test_page_version_is_empty_when_a_file_is_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L10: a page or shared file missing mid-run gives "" (no reload storm), not an exception."""
    root = _copy_package(tmp_path, monkeypatch)
    assert W.page_version("chat.html")
    (root / "ui.css").unlink()
    assert W.page_version("chat.html") == ""
    assert W.page_version("nope.html") == ""


async def test_a_missing_shared_file_fails_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """H6: a wheel without ui.js must not start and serve a broken page."""
    root = _copy_package(tmp_path, monkeypatch)
    (root / "ui.js").unlink()
    with pytest.raises(FileNotFoundError):
        await W.serve_board(_board(), settings, "127.0.0.1:1")
    (root / "ui.js").write_text("  // no marker in the page is an error too\n")
    (root / "chat.html").write_text((root / "chat.html").read_text().replace("/* ui.js */", ""))
    with pytest.raises(ValueError, match="ui.js"):
        await W.serve_board(_board(), settings, "127.0.0.1:1")


def test_ipv6_binds_are_bracketed() -> None:
    """L3: an IPv6 loopback bind parses in both forms and is bracketed wherever a URL or Host carries it."""
    assert W.parse_bind("[::1]:47123") == ("::1", 47123)
    assert W.parse_bind("::1:47123") == ("::1", 47123)
    assert W.netloc("::1", 47123) == "[::1]:47123"
    assert W.netloc("127.0.0.77", 47123) == "127.0.0.77:47123"
    with pytest.raises(ValueError, match="non-loopback"):
        W.parse_bind("[2001:db8::1]:47123")


def test_binds_are_canonical() -> None:
    """L2: the Host and Origin checks compare text: any spelling of a loopback address binds as the
    compressed form a browser sends."""
    assert W.parse_bind("[0:0:0:0:0:0:0:1]:1") == ("::1", 1)
    assert W.parse_bind("[0::1]:1") == ("::1", 1)
    assert W.parse_bind("[::FFFF:127.0.0.9]:1") == ("::ffff:127.0.0.9", 1)
    assert W.parse_bind("[::ffff:7f00:9]:1") == ("::ffff:127.0.0.9", 1)
    assert W.parse_bind("127.0.0.77:47123") == ("127.0.0.77", 47123)
    for bad in ("localhost:1", "127.1:1", "[::ffff:10.0.0.1]:1"):
        with pytest.raises(ValueError):
            W.parse_bind(bad)


def test_stuck_archive_threads_force_the_exit() -> None:
    """M6: a hung archive thread would hold the interpreter's exit: leave at once, with the code `main`
    would have returned (0 on Ctrl-C, 1 on a failure); nothing to force without stuck threads."""
    assert W.stuck_exit_code(RuntimeError("live stream ended"), 0) is None
    assert W.stuck_exit_code(None, 0) is None
    assert W.stuck_exit_code(RuntimeError("live stream ended"), 2) == 1
    assert W.stuck_exit_code(OSError("bind"), 1) == 1
    assert W.stuck_exit_code(KeyboardInterrupt(), 1) == 0
    assert W.stuck_exit_code(asyncio.CancelledError(), 1) == 0
    assert W.stuck_exit_code(None, 1) == 0


def test_stuck_archive_threads_counts_only_live_archive_threads() -> None:
    """M6: the decision's input: archive threads still alive after the grace, idle ones joined."""
    release = threading.Event()
    hung = threading.Thread(target=release.wait, args=(5,), name="archive_hung")
    other = threading.Thread(target=release.wait, args=(5,), name="worker")
    hung.start()
    other.start()
    try:
        assert CV.stuck_archive_threads(0.05) == 1
    finally:
        release.set()
        hung.join()
        other.join()
    assert CV.stuck_archive_threads(0.05) == 0


async def test_an_ipv6_board_serves_its_pages(settings: Settings) -> None:
    """L3: on ::1 the Host check, the Origin check and the chat URLs all use the bracketed form."""
    with socket.socket(socket.AF_INET6) as probe:
        try:
            probe.bind(("::1", 0))
        except OSError:
            pytest.skip("no IPv6 loopback here")
        port = int(probe.getsockname()[1])
    board = _board()
    task = asyncio.create_task(W.serve_board(board, settings, f"[::1]:{port}"))
    try:
        async with httpx.AsyncClient(headers=cookie_header(W.load_secret(settings))) as client:
            for _ in range(200):
                try:
                    got = await client.get(f"http://[::1]:{port}/")
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.01)
            assert got.status_code == 200
        assert board.base_url == f"http://[::1]:{port}"
        async with board_socket(f"[::1]:{port}", board.secret) as ws:
            assert "rows" in str(await ws.recv())
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
