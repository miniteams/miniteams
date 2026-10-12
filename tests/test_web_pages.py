"""The served pages and the socket handshake (spec 011 fix rounds): the persisted cookie secret, the
launch URL, the page headers, and ui.css / ui.js inlined into both pages."""

import argparse
import asyncio
import base64
import hashlib
import logging
import os
import re
import socket
import stat
import threading
import time
import types
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

from miniteams import auth, cli
from miniteams import chat_verbs as CV
from miniteams import web as W
from miniteams.config import Settings, write_private

B = "19:bbbb@thread.v2"


def _board() -> W.Board:
    return W.Board({B: {"id": B, "label": "B", "last_activity": "2026-10-09T10:00:00Z", "seen_at": None}})


async def _refused(url: str, host: str, cookie: str | None) -> int:
    headers = {"Cookie": cookie} if cookie is not None else None
    with pytest.raises(websockets.InvalidStatus) as exc:
        await websockets.connect(url, origin=websockets.Origin(f"http://{host}"), additional_headers=headers)
    return int(exc.value.response.status_code)


async def test_the_socket_needs_the_cookie(settings: Settings) -> None:
    """M2: the Origin is forgeable by any local process: `/ws` also needs the `mt` cookie; a launch nonce
    on the socket opens nothing (and is not spent)."""
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
                launch = board.launch_url
                assert await _refused(f"ws://{host}{path}?{launch.split('?')[1]}", host, None) == 403, path
                assert board.launch_url == launch
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
                assert "rows" in str(await ws.recv())
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


def test_the_token_files_are_owner_only(settings: Settings) -> None:
    """M10: the skype token, the endpoint ids and the MSAL cache go through write_private."""
    from miniteams import auth, skype, trouter

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
    W.save_bind(settings, "127.0.0.1", 1)
    return settings.config_dir / "web.json"


def _marks(settings: Settings) -> Path:
    W._save_marks(settings.config_dir / "seen.json", {"a": "b"})
    return settings.config_dir / "seen.json"


def _names(settings: Settings) -> Path:
    from miniteams.directory import Directory

    Directory(settings)._save_names()
    return settings.cache_dir / "names.json"


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


def test_the_run_dir_is_private(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """H1: the launch files live in $XDG_RUNTIME_DIR/miniteams when that dir is ours, else
    <config_dir>/run; made 0700, refused when open to others or not a plain directory."""
    assert W.resolve_run_dir(settings) == settings.config_dir / "run"
    runtime = tmp_path / "xdg"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    made = W.resolve_run_dir(settings)
    assert made == runtime / "miniteams" and stat.S_IMODE(made.stat().st_mode) == 0o700
    made.chmod(0o750)
    with pytest.raises(RuntimeError, match="0700"):
        W.resolve_run_dir(settings)
    made.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    made.symlink_to(elsewhere)
    with pytest.raises(RuntimeError, match="0700"):
        W.resolve_run_dir(settings)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "missing"))
    assert W.resolve_run_dir(settings) == settings.config_dir / "run"


async def test_the_run_dir_setting_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """MINITEAMS_RUN_DIR (`~` expanded) holds the launch files, made 0700 like the default."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MINITEAMS_RUN_DIR", "~/launch")
    settings = Settings(tenant_id="t")
    settings.config_dir, settings.cache_dir = tmp_path / "cfg", tmp_path / "cache"
    board = _board()
    async with _served(board, settings) as host, httpx.AsyncClient() as client:
        assert board.run_dir == tmp_path / "launch" == W.resolve_run_dir(settings)
        path, url = _launch_file(board)
        assert path == tmp_path / "launch" / "open.html"
        hint = (await client.get(f"http://{host}/")).text
        assert f"({path.as_uri()})" in hint
    assert f"open: {path.as_uri()}\n" in capsys.readouterr().err


def _launch_file(board: W.Board) -> tuple[Path, str]:
    """The launch file and the URL it redirects to, checked owner-only and secret-free."""
    assert board.run_dir is not None
    path = board.run_dir / "open.html"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(board.run_dir.stat().st_mode) == 0o700
    text = path.read_text()
    assert board.secret not in text
    found = re.fullmatch(
        r'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=([^"]+)">'
        r'<a href="\1">miniteams</a>\n',
        text,
    )
    assert found, text
    return path, found[1]


async def test_the_launch_file_sets_the_cookie_once(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """H1: `open.html` holds `/?n=<nonce>`; its first use sets the cookie and moves on to the same URL
    without `n`, other params kept; the file then holds a fresh nonce. Only its path is printed, once;
    the secret and the nonces are never printed nor logged; the file goes at shutdown."""
    board = _board()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    seen: list[str] = []
    with structlog.testing.capture_logs() as logs:
        async with _served(board, settings) as host, httpx.AsyncClient() as client:
            path, url = _launch_file(board)
            assert url == board.launch_url
            assert re.fullmatch(rf"http://{re.escape(host)}/\?n=[\w-]{{32}}", url)
            cookie = f"mt={board.secret}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"
            thread = quote(B, safe="")
            for ask, landing in (
                ("/?{n}", "/"),
                ("/?theme=dark&{n}&avatars=0", "/?theme=dark&avatars=0"),
                (f"/chat/{thread}?{{n}}&chatnav=split", f"/chat/{thread}?chatnav=split"),
            ):
                nonce = _launch_file(board)[1].split("?", 1)[1]
                seen.append(nonce)
                got = await client.get(f"http://{host}{ask.format(n=nonce)}")
                assert got.status_code == 200, ask
                assert f'content="0;url={landing.replace("&", "&amp;")}"' in got.text, ask
                assert got.headers["set-cookie"] == cookie, ask
                assert got.headers["cache-control"] == "no-store", ask
                assert _launch_file(board)[1] != f"http://{host}/?{nonce}"  # re-minted
                client.cookies.clear()
            got = await client.get(f"http://{host}/?{seen[0]}")  # spent, no cookie
            assert got.status_code == 403 and "set-cookie" not in got.headers
            got = await client.get(
                f"http://{host}/?{_launch_file(board)[1].split('?', 1)[1]}", headers={"Host": "evil.example"}
            )
            assert got.status_code == 404 and "set-cookie" not in got.headers
            assert path.exists()
        assert not path.exists()
    err = capsys.readouterr().err
    assert err.count("open: ") == 1 and f"open: {path.as_uri()}\n" in err
    for secret in (board.secret, *seen):
        assert secret not in err and secret not in repr(logs)
    assert [e["url"] for e in logs if e["event"] == "web_listening"] == [f"http://{host}/"]


async def test_the_launch_nonce_never_expires(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """H1: `open.html` is the user's way in at any time: its nonce has no TTL, only its single use."""
    board = _board()
    async with _served(board, settings), httpx.AsyncClient() as client:
        first = board.launch_url
        later = time.monotonic() + 24 * 3600
        monkeypatch.setattr(W, "time", types.SimpleNamespace(monotonic=lambda: later))  # web's clock only
        assert (await client.get(first)).status_code == 200
        assert board.launch_url != first and _launch_file(board)[1] == board.launch_url
        client.cookies.clear()
        assert (await client.get(first)).status_code == 403


async def test_startup_drops_a_previous_runs_launch_files(settings: Settings) -> None:
    """H1: a previous run's chat files hold dead nonces: the server deletes them when it starts."""
    run = W.resolve_run_dir(settings)
    stale = [run / "chat-0123456789abcdef.html", run / "chat-x.html"]
    for file in stale:
        write_private(file, "dead")
    kept = run / "notes.html"
    write_private(kept, "not ours")
    async with _served(_board(), settings):
        assert not any(file.exists() for file in stale) and kept.exists()


def _web_args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        limit=None,
        bind=None,
        reactions=False,
        opener="none",
        open_scheme="msteams",
        browser="",
        data_dir=str(tmp_path),
    )


def test_a_second_web_on_the_same_run_dir_refuses_before_any_network(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: a second web on a live run dir stops before the login, the skype exchange and the server."""

    def started(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("reached the network before the run dir lock")

    for name in ("acquire_aad_token", "exchange_skype_token"):
        monkeypatch.setattr(cli, name, started)
    monkeypatch.setattr(auth, "self_mri", started)
    monkeypatch.setattr(W, "run", started)
    held = W.lock_run_dir(W.resolve_run_dir(settings))
    try:
        with pytest.raises(RuntimeError, match="another `miniteams web` is running") as refused:
            cli.cmd_web(settings, _web_args(tmp_path))
    finally:
        os.close(held)
    assert "MINITEAMS_ENDPOINT_SUFFIX" in str(refused.value) and "--bind" in str(refused.value)


def test_web_holds_the_run_dir_lock_while_it_serves(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: the lock taken by `cmd_web` is still held while the server runs."""
    fds: list[int] = []
    lock = W.lock_run_dir

    def locking(run_dir: Path) -> int:
        fds.append(lock(run_dir))
        return fds[-1]

    async def serving(*_args: object, **_kwargs: object) -> None:
        with pytest.raises(RuntimeError, match="another `miniteams web` is running"):
            lock(W.resolve_run_dir(settings))
        served.append(True)

    served: list[bool] = []
    monkeypatch.setattr(W, "lock_run_dir", locking)
    monkeypatch.setattr(W, "run", serving)
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: ({"access_token": "a"}, "sk"))
    monkeypatch.setattr(auth, "self_mri", lambda token: "8:me")
    try:
        assert cli.cmd_web(settings, _web_args(tmp_path)) == 0
    finally:
        for fd in fds:
            os.close(fd)
    assert served == [True] and len(fds) == 1


def test_a_relative_run_dir_is_made_absolute(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L2: a relative MINITEAMS_RUN_DIR or MINITEAMS_CONFIG_DIR resolves against the cwd, so its launch
    file has a file:// URI."""
    monkeypatch.chdir(tmp_path)
    path = W.resolve_run_dir(settings.model_copy(update={"run_dir": Path("rel")}))
    assert path == tmp_path / "rel" and path.as_uri().startswith("file:///")
    path = W.resolve_run_dir(settings.model_copy(update={"config_dir": Path("cfg-rel")}))
    assert path == tmp_path / "cfg-rel" / "run" and path.as_uri().startswith("file:///")


async def test_a_second_run_on_the_same_port_leaves_the_live_files(
    settings: Settings, tmp_path: Path
) -> None:
    """M1: the launch files are dropped only once bound: a run failing on the port keeps the live ones."""
    board = _board()
    async with _served(board, settings) as host:
        path, url = _launch_file(board)
        with pytest.raises(OSError):
            await W.serve_board(_board(), settings.model_copy(update={"run_dir": tmp_path / "other"}), host)
        assert _launch_file(board) == (path, url)


async def test_a_failed_launch_file_write_keeps_the_previous_nonce(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L1: re-minting `open.html` fails: the nonce it still holds stays the valid one."""
    board = _board()
    async with _served(board, settings):
        path, url = _launch_file(board)
        assert url == board.launch_url

        def fail(*_: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(W, "write_private", fail)
        with structlog.testing.capture_logs() as logs:
            assert board.take_nonce(url.split("?n=", 1)[1])
        assert board.launch_url == url and _launch_file(board) == (path, url)
        assert [e["error"] for e in logs if e["event"] == "launch_file_failed"] == ["disk full"]


async def test_a_spent_nonce_with_the_cookie_lands_without_it(settings: Settings) -> None:
    """M3: reloading the launch URL, or a tab reopened on it, already holds the cookie: 303 without `n`."""
    board = _board()
    async with _served(board, settings) as host, httpx.AsyncClient(headers=_cookie(board)) as client:
        nonce = board.launch_url.split("?", 1)[1]
        assert (await client.get(f"http://{host}/?{nonce}")).status_code == 200
        for ask, landing in ((f"/?theme=dark&{nonce}", "/?theme=dark"), (f"/?{nonce}", "/"), ("/?n=x", "/")):
            got = await client.get(f"http://{host}{ask}")
            assert (got.status_code, got.headers["location"]) == (303, landing), ask
            assert "set-cookie" not in got.headers, ask


async def test_a_page_without_the_cookie_says_how_to_sign_in(settings: Settings) -> None:
    """M4: a page route's 403 names the launch file; no secret, no nonce. Other routes stay terse."""
    board = _board()
    async with _served(board, settings) as host, httpx.AsyncClient() as client:
        assert board.run_dir is not None
        hint = (
            "Not signed in to this miniteams page: open the launch file printed by `miniteams web` "
            f"({(board.run_dir / 'open.html').as_uri()})\n"
        )
        for path in ("/", "/?theme=dark", f"/chat/{quote(B, safe='')}", "/?n=spent"):
            got = await client.get(f"http://{host}{path}")
            assert (got.status_code, got.text) == (403, hint), path
            assert board.secret not in got.text and board.launch_url.split("=")[-1] not in got.text
        for path in (f"/media/{quote(B, safe='')}/x.png", "/emoji/x", "/nope"):
            got = await client.get(f"http://{host}{path}")
            assert (got.status_code, got.text) == (403, "forbidden\n"), path


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


async def test_every_route_needs_the_cookie(settings: Settings) -> None:
    """M2: without the cookie no page, chat, media or emoji is served."""
    board = _board()
    async with _served(board, settings) as host, httpx.AsyncClient() as client:
        thread = quote(B, safe="")
        for path in ("/", "/?theme=dark", f"/chat/{thread}", f"/media/{thread}/x.png", "/emoji/x", "/nope"):
            for cookie in (None, f"mt={board.secret[1:]}", f"x={board.secret}"):
                headers = {"Cookie": cookie} if cookie else {}
                assert (await client.get(f"http://{host}{path}", headers=headers)).status_code == 403, path
        assert (await client.get(f"http://{host}/", headers=_cookie(board))).status_code == 200


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
