"""The native widget window (spec 024) on a real served board, offscreen. Synthetic data, no Teams.

Run: `uv run --extra widget pytest tests/test_widget_qt.py`; skipped without the extra.
"""

import asyncio
import contextlib
import os
import resource
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6.QtWebEngineWidgets", reason="run with `uv run --extra widget`")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6  # noqa: E402
import structlog  # noqa: E402
import test_web_chat  # noqa: E402
from PySide6.QtCore import QEvent, QLockFile, QProcess, QSize, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QCloseEvent, QColor, QDesktopServices  # noqa: E402
from PySide6.QtWebEngineCore import QWebEnginePage  # noqa: E402
from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402
from test_web_chat import ME, _archive, _at, _m, _served  # noqa: E402

from miniteams import web as W  # noqa: E402
from miniteams import widget, widget_qt  # noqa: E402
from miniteams.config import Settings  # noqa: E402

quiet_directory = test_web_chat.quiet_directory
A, B, C = "19:qa@thread.v2", "19:qb@thread.v2", "19:qc@thread.v2"
Reason = QSystemTrayIcon.ActivationReason


@pytest.fixture(scope="module", autouse=True)
def private_bus() -> Iterator[None]:
    """Never the desktop's session bus: a notification must not reach the user's screen (`gdbus` is faked)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent/miniteams-tests-bus")
        yield


@pytest.fixture(scope="module")
def app(private_bus: None) -> Iterator[QApplication]:
    """One QApplication per process, set up as `widget_qt.main` does; Chromium reserves far more
    address space than conftest's RLIMIT_AS cap, lifted while its processes start and run."""
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS, (hard, hard))
    found = QApplication.instance() or QApplication([widget_qt.APP])
    assert isinstance(found, QApplication)
    found.setApplicationName(widget_qt.APP)
    found.setDesktopFileName(widget_qt.APP)
    found.setQuitOnLastWindowClosed(False)
    try:
        yield found
    finally:
        resource.setrlimit(resource.RLIMIT_AS, (soft, hard))


def _row(thread_id: str, label: str, i: int, **over: Any) -> dict[str, Any]:
    return {
        "id": thread_id,
        "label": label,
        "last_activity": _at(i),
        "last_id": str(i),
        "unread": False,
        "mention": None,
        "sender": "Ann Lee",
        "text": "snippet",
        "seen_at": None,
        "muted": False,
        "typing": [],
        **over,
    }


@pytest.fixture
def board(quiet_directory: Any, tmp_path: Path) -> W.Board:
    """A unread, B unread and mentioned, C muted and unread: Unread 2, @ 1, Inbox 2."""
    data = tmp_path / "data"
    for t in (A, B, C):
        _archive(data, t, [_m("1", _at(1))])
    mention = {"kind": "me", "by": "Ann Lee", "text": "hi", "at": _at(20), "edited": False}
    rows = {
        A: _row(A, "Fictional A", 30, unread=True),
        B: _row(B, "Fictional B", 20, unread=True, mention=mention),
        C: _row(C, "Fictional C", 10, unread=True),
    }
    board = W.Board(rows, quiet_directory, me=ME, data_dir=data, sections_path=tmp_path / "sections.json")
    board.rows[C]["muted"] = True
    return board


@pytest.fixture
async def served(board: W.Board, settings: Settings) -> AsyncIterator[str]:
    async with _served(board, settings) as host:
        yield host


async def until(cond: Callable[[], Any], what: str, tries: int = 1500) -> None:
    """Pump Qt's events and the test's loop alike until `cond()`."""
    for _ in range(tries):
        QApplication.processEvents()
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{what} not met in {tries / 100:g} s")


async def js(window: widget_qt.Window, code: str) -> Any:
    got: list[Any] = []
    window.page().runJavaScript(code, 0, got.append)
    await until(lambda: got, f"answer to {code}")
    return got[0]


async def shows_rows(window: widget_qt.Window) -> None:
    for _ in range(1500):
        if await js(window, "document.querySelectorAll('#rows a.name').length") == 2:  # C is muted
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the board's rows never showed")


@pytest.fixture
async def opened(
    app: QApplication, served: str, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[widget_qt.Window, list[str]]]:
    """The window on the served board, signed in through web.json's token; the URLs it handed to the
    system browser."""
    opened: list[str] = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: True)
    base, token = widget.target(settings.config_dir)
    store = widget_qt.profile(settings.config_dir / "widget")
    window = widget_qt.Window(base, store, widget_qt.app_icon(), settings.config_dir)
    window.load(QUrl(f"{base}/#{token}"))
    window.show()
    await shows_rows(window)
    yield window, opened
    widget_qt.dispose(window)


async def test_window_signs_in_with_the_token_then_with_the_kept_cookie(
    app: QApplication, served: str, settings: Settings, board: W.Board
) -> None:
    """024-AC1: the first run signs in through web.json's token; a restart on the same profile, with no
    token, is still signed in (the cookie on disk)."""
    base, token = widget.target(settings.config_dir)
    assert token
    storage = settings.config_dir / "widget"
    storage.mkdir(parents=True)
    storage.chmod(0o755)  # an older or hand-made one: tightened, it will hold the cookie
    for url in (f"{base}/#{token}", f"{base}/"):
        store = widget_qt.profile(storage)
        window = widget_qt.Window(base, store, widget_qt.app_icon(), settings.config_dir)
        window.load(QUrl(url))
        await shows_rows(window)
        assert await js(window, "location.hash") == ""  # the token left the address
        widget_qt.dispose(window)
    assert storage.stat().st_mode & 0o777 == 0o700
    assert board.secret.encode() in (storage / "Cookies").read_bytes()  # on disk, not only in this process
    assert widget.target(settings.config_dir)[1] != token  # spent, the server minted the next


def test_icon_has_every_size_up_to_256_and_the_app_is_named_miniteams(app: QApplication) -> None:
    """024-AC2: the window icon carries 256×256 (what _NET_WM_ICON gets); the names WM_CLASS is built from."""
    icon = widget_qt.app_icon()
    assert QSize(256, 256) in icon.availableSizes()
    assert QSize(22, 22) in icon.availableSizes()
    assert not icon.pixmap(256, 256).toImage().pixelColor(128, 128).alpha() == 0  # rendered, not blank
    assert (app.applicationName(), app.desktopFileName()) == ("miniteams", "miniteams")


async def test_close_hides_and_the_tray_shows_again(opened: tuple[widget_qt.Window, list[str]]) -> None:
    """024-AC3: closing hides the window, the page living on; the tray's activation shows it again,
    its context click (the menu) does not."""
    window, _ = opened
    window.close()
    assert not window.isVisible() and window.tray.isVisible()
    assert await js(window, "1 + 1") == 2
    window.activated(Reason.Context)
    assert not window.isVisible()
    for reason in (Reason.Trigger, Reason.MiddleClick, Reason.DoubleClick):
        window.hide()
        window.activated(reason)
        assert window.isVisible(), reason
    assert [a.text() for a in window.tray.contextMenu().actions()] == ["Show", "Settings…", "Quit"]


def test_main_outlives_a_closed_window_and_quit_ends_it(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """024-AC3: in `main`'s loop a closed window leaves the process running; tray Quit, the window shown
    again (Qt's quit asks it to close), returns 0."""
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: True)
    seen: list[str] = []

    def window() -> widget_qt.Window:
        return next(w for w in QApplication.topLevelWidgets() if isinstance(w, widget_qt.Window))

    def close() -> None:
        window().close()
        QTimer.singleShot(300, still)

    def still() -> None:
        seen.append("hidden" if not window().isVisible() else "shown")
        window().reveal()
        next(a for a in window().tray.contextMenu().actions() if a.text() == "Quit").trigger()

    handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    app.setQuitOnLastWindowClosed(True)  # Qt's default: `main` must change it
    QTimer.singleShot(300, close)
    QTimer.singleShot(10000, lambda: QApplication.exit(3))  # a cancelled quit fails, never hangs
    try:
        assert widget_qt.main("http://127.0.0.1:9", "", tmp_path) == 0
    finally:
        for s, h in handlers.items():
            signal.signal(s, h)
    assert seen == ["hidden"]
    assert not [w for w in QApplication.topLevelWidgets() if isinstance(w, widget_qt.Window)]


async def test_page_publishes_the_badge_per_preference_also_hidden(
    opened: tuple[widget_qt.Window, list[str]], board: W.Board
) -> None:
    """024-AC4: the page's count reaches the tray through the channel: unread by default, then per the
    ⚙ choice; a board change updates it while the window is hidden; the filter box changes nothing."""
    window, _ = opened
    await until(lambda: window.tray.toolTip() == "miniteams — 2 unread", "unread tooltip")
    window.hide()
    board.rows[C]["muted"] = False  # C now counts as unread
    board.broadcast()
    await until(lambda: window.tray.toolTip() == "miniteams — 3 unread", "tooltip after a change, hidden")
    await js(
        window,
        "const q = document.getElementById('q'); q.value = 'Fictional A';q.dispatchEvent(new Event('input'))",
    )
    await js(window, "1")
    assert window.tray.toolTip() == "miniteams — 3 unread"
    window.open_settings()
    assert window.isVisible()
    await until(lambda: True, "settings signal")
    for label, tip in (
        ("@", "miniteams — 1 mention"),
        ("Inbox", "miniteams — 3 in Inbox"),
        ("None", "miniteams"),
    ):
        await js(
            window,
            f"[...document.querySelectorAll('#tray button')].find(b => b.textContent === '{label}').click()",
        )
        await until(lambda tip=tip: window.tray.toolTip() == tip, f"tooltip {tip}")
        if label == "@":
            pill = window.tray.icon().pixmap(64, 64).toImage().pixelColor(62, 45)
            assert pill.name() == "#f59e0b"


async def test_tray_settings_opens_the_tray_section(opened: tuple[widget_qt.Window, list[str]]) -> None:
    """024-AC5: tray → Settings… shows the window with ⚙ open on its Tray section."""
    window, _ = opened
    window.hide()
    assert await js(window, "!!document.querySelector('.menu')") is False
    next(a for a in window.tray.contextMenu().actions() if a.text() == "Settings…").trigger()
    assert window.isVisible()
    for _ in range(500):
        if await js(window, "!!document.querySelector('.menu #tray')"):
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("⚙ did not open on its Tray section")


async def test_new_window_links_go_to_the_system_browser(opened: tuple[widget_qt.Window, list[str]]) -> None:
    """024-AC6: window.open, target=_blank and a link followed in place open in the system browser; the
    board stays; a non-web scheme opens nowhere."""
    window, urls = opened
    await js(window, "window.open('https://example.com/a', '_blank', 'noopener')")
    await until(lambda: "https://example.com/a" in urls, "window.open")
    await js(
        window,
        "const a = Object.assign(document.createElement('a'),"
        " {href: 'https://example.com/b', target: '_blank'});"
        "document.body.append(a); a.click()",
    )
    await until(lambda: "https://example.com/b" in urls, "target=_blank")
    await js(window, "window.open('file:///etc/passwd', '_blank')")
    await js(window, "location.assign('https://example.com/c')")
    await until(lambda: "https://example.com/c" in urls, "in place")
    assert not any(u.startswith("file:") for u in urls)
    await shows_rows(window)  # still the board


async def test_a_chat_page_opens_signed_in_with_the_current_token(
    opened: tuple[widget_qt.Window, list[str]], settings: Settings
) -> None:
    """024-AC6: "Chat here" without --browser opens the chat page in the system browser, which has no
    cookie: it goes with web.json's current token, read at that moment; no chat URL leaves without one."""
    window, urls = opened
    base, _ = widget.target(settings.config_dir)
    for i in range(2):
        await js(window, f"window.open('/chat/19%3Ax{i}%40thread.v2?chatnav=split', '_blank', 'noopener')")
        await until(lambda i=i: any(f"/chat/19%3Ax{i}%40" in u for u in urls), "chat open")
        current = widget.target(settings.config_dir)[1]
        assert urls[-1] == f"{base}/chat/19%3Ax{i}%40thread.v2?chatnav=split#{current}"
    chats = [u for u in urls if u.startswith(f"{base}/chat/")]
    assert len(chats) == 2 and all("#" in u and not u.endswith("#") for u in chats)


def test_other_schemes_never_reach_the_desktop(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """024-AC6: a file: or custom-scheme link, in place or in a new window, opens nowhere; the board loads."""
    opened: list[str] = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    store = widget_qt.profile(tmp_path / "w")
    window = widget_qt.Window("http://127.0.0.1:9", store, widget_qt.app_icon(), tmp_path)
    page, click = window.page(), QWebEnginePage.NavigationType.NavigationTypeLinkClicked
    for url in ("file:///etc/passwd", "msteams:/l/chat/x", "javascript:alert(1)"):
        sink = page.createWindow(QWebEnginePage.WebWindowType.WebBrowserTab)
        assert sink.acceptNavigationRequest(QUrl(url), click, True) is False
        assert page.acceptNavigationRequest(QUrl(url), click, True) is False
    assert opened == []
    assert page.acceptNavigationRequest(QUrl("http://127.0.0.1:9/#tok"), click, True) is True
    widget_qt.dispose(window)


def _luminance(c: QColor) -> float:
    rgb = (c.redF(), c.greenF(), c.blueF())
    r, g, b = (x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_badge_pill_per_preference(app: QApplication) -> None:
    """024-AC4: no text keeps the plain icon; unread and Inbox counts draw on red, the mention count on
    amber; the digits contrast at least 4.5:1 with either pill."""
    icon = widget_qt.app_icon()
    assert widget_qt.badged(icon, "", "mention") is icon
    for text, pref, colour in (
        ("7", "unread", "#c62828"),
        ("99+", "all", "#c62828"),
        ("7", "mention", "#f59e0b"),
        ("99+", "mention", "#f59e0b"),
    ):
        image = widget_qt.badged(icon, text, pref).pixmap(64, 64).toImage()
        fill = image.pixelColor(62, 45)  # the pill's right edge, mid-height
        assert fill.name() == colour, (text, pref)
        assert image.pixelColor(20, 10) == icon.pixmap(64, 64).toImage().pixelColor(20, 10)  # icon above
        # The digits: the pixel furthest in luminance from the fill, in a band inside the pill only (its
        # rounded corners show the icon's dark background).
        pill = [image.pixelColor(x, y) for x in range(30, 62) for y in range(38, 53)]
        ink = max(pill, key=lambda c: abs(_luminance(c) - _luminance(fill)))
        hi, lo = sorted((_luminance(fill), _luminance(ink)), reverse=True)
        assert (hi + 0.05) / (lo + 0.05) >= 4.5, (text, pref, ink.name())


def test_without_a_tray_closing_the_window_quits(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hidden window with no tray icon to bring it back would be unreachable: closing quits instead,
    with no tray host as with the icon hidden."""
    quits: list[int] = []
    monkeypatch.setattr(QApplication, "quit", lambda: quits.append(1))
    store = widget_qt.profile(tmp_path / "w")
    window = widget_qt.Window("http://127.0.0.1:9", store, widget_qt.app_icon(), tmp_path)
    window.show()
    for available, shown, quit in ((False, True, 1), (True, False, 1), (True, True, 0)):
        monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda a=available: a)
        window.tray.setVisible(shown)
        window.show()
        quits.clear()
        event = QCloseEvent()
        window.closeEvent(event)
        assert len(quits) == quit, (available, shown)
        assert event.isAccepted() is bool(quit)
    widget_qt.dispose(window)


# The first widget, in its own process as in real use: says when its loop runs, and when it is woken.
FIRST = """import sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from miniteams import widget_qt
def reveal(self):
    print("revealed", flush=True)
    QApplication.exit(0)
widget_qt.Window.reveal = reveal
QTimer.singleShot(0, lambda: print("ready", flush=True))
sys.exit(widget_qt.main("http://127.0.0.1:9", "", Path(sys.argv[1])))
"""


def _said(out: Path, word: str, seconds: float = 30) -> bool:
    """`word` on a line of `out` within `seconds`: a file, not a pipe, which Chromium's helpers keep open."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if word in out.read_text().split():
            return True
        time.sleep(0.05)
    return False


def test_a_second_widget_shows_the_first_and_exits(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One window per profile: a second `main` opens no window and spends no token, it wakes the first."""
    out = tmp_path / "first.out"
    with out.open("w") as sink:
        first = subprocess.Popen(
            [sys.executable, "-c", FIRST, str(tmp_path)],
            stdout=sink,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    try:
        assert _said(out, "ready")
        lock = tmp_path / "widget" / "widget.lock"
        past = time.time() - 60
        os.utime(lock, (past, past))  # past Qt's 30 s stale time: a live holder keeps it all the same
        built: list[str] = []
        monkeypatch.setattr(widget_qt, "Window", lambda *a: built.append("window"))
        assert widget_qt.main("http://127.0.0.1:9", "tok", tmp_path) == 0
        assert built == []
        assert _said(out, "revealed")
        assert first.wait(30) == 0
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(first.pid, signal.SIGKILL)  # the child and its Chromium helpers


async def test_settings_survives_the_page_deleting_the_bridge(
    opened: tuple[widget_qt.Window, list[str]],
) -> None:
    """Any object on the channel can be deleteLater()'d from the page: tray → Settings… must not raise."""
    window, _ = opened
    window.page().runJavaScript("window.miniteamsTray.then((t) => t.deleteLater())")

    def gone() -> bool:
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return not shiboken6.isValid(window.bridge)

    await until(gone, "the bridge deleted from the page")
    window.hide()
    window.open_settings()
    assert window.isVisible()


def test_the_profile_writes_only_under_its_own_dir(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No default QtWebEngine dir under the user's data or cache dirs: everything under the given one."""
    home = tmp_path / "home"
    for var, sub in (("HOME", ""), ("XDG_DATA_HOME", ".local/share"), ("XDG_CACHE_HOME", ".cache")):
        monkeypatch.setenv(var, str(home / sub))
    home.mkdir()
    store = widget_qt.profile(tmp_path / "w")
    window = widget_qt.Window("http://127.0.0.1:9", store, widget_qt.app_icon(), tmp_path)
    QApplication.processEvents()
    assert store.persistentStoragePath() == str(tmp_path / "w")
    assert [p for p in home.rglob("*") if "QtWebEngine" in p.parts] == []
    widget_qt.dispose(window)


def test_a_locked_profile_with_no_answer_fails(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock held but nobody answering the wake: exit 1, no window, never a silent 0."""
    storage = tmp_path / "widget"
    storage.mkdir()
    held = QLockFile(str(storage / "widget.lock"))
    assert held.tryLock(0)
    built: list[str] = []
    monkeypatch.setattr(widget_qt, "Window", lambda *a: built.append("window"))
    try:
        assert widget_qt.main("http://127.0.0.1:9", "tok", tmp_path) == 1
    finally:
        held.unlock()
    assert built == []


@pytest.mark.parametrize("new_window", [False, True])
def test_a_chat_page_followed_in_place_also_gets_the_token(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, new_window: bool
) -> None:
    """024-AC6: one route to the system browser, in place or from a new window: a chat page of ours
    always leaves with web.json's token."""
    opened: list[str] = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    (tmp_path / "web.json").write_text('{"host": "127.0.0.1", "port": 9, "token": "tok"}')
    store = widget_qt.profile(tmp_path / "w")
    window = widget_qt.Window("http://127.0.0.1:9", store, widget_qt.app_icon(), tmp_path)
    page, click = window.page(), QWebEnginePage.NavigationType.NavigationTypeLinkClicked
    target = page.createWindow(QWebEnginePage.WebWindowType.WebBrowserTab) if new_window else page
    assert target.acceptNavigationRequest(QUrl("http://127.0.0.1:9/chat/19%3Ax"), click, True) is False
    assert opened == ["http://127.0.0.1:9/chat/19%3Ax#tok"]
    widget_qt.dispose(window)


# The first widget held between taking its lock and building its window, until the test creates `go`.
SLOW = FIRST.replace(
    "widget_qt.Window.reveal = reveal\n",
    """widget_qt.Window.reveal = reveal
import time
real = widget_qt.Window
def held(*a):
    print("building", flush=True)
    while not (Path(sys.argv[1]) / "go").exists():
        time.sleep(0.02)
    return real(*a)
widget_qt.Window = held
""",
)


def test_a_launch_during_startup_waits_for_the_window(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second launch while the first has its lock but no window yet still exits 0; the first shows
    once its window exists."""
    assert SLOW != FIRST
    out = tmp_path / "first.out"
    with out.open("w") as sink:
        first = subprocess.Popen(
            [sys.executable, "-c", SLOW, str(tmp_path)],
            stdout=sink,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    try:
        assert _said(out, "building")
        built: list[str] = []
        monkeypatch.setattr(widget_qt, "Window", lambda *a: built.append("window"))
        assert widget_qt.main("http://127.0.0.1:9", "tok", tmp_path) == 0
        assert built == [] and "revealed" not in out.read_text()
        (tmp_path / "go").touch()
        assert _said(out, "revealed")
        assert first.wait(30) == 0
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(first.pid, signal.SIGKILL)


# --- notifications (024-AC10, AC11) ---

DISMISSED = 2  # NotificationClosed's reason: dismissed by the user
MENTION = {"kind": "me", "by": "Bob Roe", "text": "see <b>this</b> & that", "at": _at(40), "edited": False}


@pytest.fixture
def gdbus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in `gdbus`: writes its arguments, one per line, to the returned file and answers id 7."""
    fake, args = tmp_path / "gdbus", tmp_path / "gdbus.args"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {args}\necho '(uint32 7,)'\n")
    fake.chmod(0o755)
    monkeypatch.setattr(widget_qt, "GDBUS", str(fake))
    return args


async def test_a_notification_from_the_page_and_its_open_action_opens_the_chat(
    opened: tuple[widget_qt.Window, list[str]], board: W.Board, gdbus: Path
) -> None:
    """024-AC10, AC11: a new mention on the board, the window hidden, reaches the desktop's notification
    service (sender, chat, text as data); its Open action shows the window and opens the chat as its row does
    (no opener here: its Teams link, in the system browser)."""
    window, urls = opened
    await until(lambda: window.tray.toolTip() == "miniteams — 2 unread", "the page's first count")
    window.hide()
    board.rows[A]["mention"] = MENTION | {"msg_id": "m9"}
    board.broadcast()
    await until(lambda: window.notifier.chats == {7: A}, "a notification sent")
    args = gdbus.read_text().split("\n")
    assert args[:10] == [
        "call",
        "--session",
        "--timeout",
        "5",
        "--dest",
        widget_qt.NOTIFICATIONS,
        "--object-path",
        widget_qt.NOTIFICATIONS_PATH,
        "--method",
        widget_qt.NOTIFICATIONS + ".Notify",
    ]
    assert args[10:13] == ["--", "'miniteams'", "0"]  # nothing to replace yet
    assert args[14:17] == [
        "'Bob Roe · Fictional A'",
        "'see &lt;b&gt;this&lt;/b&gt; &amp; that'",
        "['default', 'Open']",
    ]
    window.notifier.invoked(7, "default")
    assert window.isVisible()
    link = W.deep_link(A, "30")
    await until(lambda: link in urls, "the chat's link opened")
    window.notifier.closed(7, DISMISSED)  # the daemon closes it after the action: no second open
    await js(window, "1")
    assert urls.count(link) == 1


def test_clicks_route_by_notification_id(app: QApplication) -> None:
    """024-AC11: only the Open action opens that notification's chat, once; a dismissal (one, or a
    "clear all" of several), an expiry, another action, an unknown id open nothing and forget the id."""
    notifier = widget_qt.Notifier(widget_qt.QDBusConnection.sessionBus())
    got: list[str] = []
    notifier.clicked.connect(got.append)
    notifier.chats = {1: A, 2: B, 3: C, 4: A, 5: B, 6: C}
    notifier.closed(1, 1)  # expired
    notifier.invoked(2, "other")
    notifier.invoked(9, "default")
    notifier.closed(3, DISMISSED)
    notifier.invoked(4, "default")
    notifier.closed(4, DISMISSED)
    for nid in (5, 6):  # "clear all"
        notifier.closed(nid, DISMISSED)
    assert got == [A]
    assert notifier.chats == {}


async def test_no_notification_while_the_window_is_in_front(
    opened: tuple[widget_qt.Window, list[str]], gdbus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """024-AC10: the window active and shown, the board in view: the page's notification is dropped."""
    window, _ = opened
    monkeypatch.setattr(window, "isActiveWindow", lambda: True)
    window.bridge.notify(A, "t", "b")
    window.hide()
    window.bridge.notify(B, "t", "b")
    await until(lambda: window.notifier.chats, "the hidden window's notification")
    assert window.notifier.chats == {7: B}


async def test_a_failing_or_missing_gdbus_only_logs(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No notification service (gdbus fails) or no gdbus: nothing kept, nothing held, nothing raised. No gdbus
    first: a send left marked in flight would hold the next one."""
    notifier = widget_qt.Notifier(widget_qt.QDBusConnection.sessionBus())
    failing = tmp_path / "gdbus"
    failing.write_text("#!/bin/sh\necho 'no service' >&2\nexit 1\n")
    failing.chmod(0o755)
    for program in (str(tmp_path / "absent"), str(failing)):
        monkeypatch.setattr(widget_qt, "GDBUS", program)
        with structlog.testing.capture_logs() as logs:
            notifier.show(A, "t", "b")
            await until(lambda logs=logs: any(e["event"] == "widget_notify_failed" for e in logs), program)
        assert notifier.pending == {}, program
    assert notifier.chats == {}
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert notifier.findChildren(QProcess) == []


def test_gvariant_quotes_what_gdbus_would_unquote() -> None:
    assert widget_qt.gvariant("it's a \\ 'x'") == "'it\\'s a \\\\ \\'x\\''"


@pytest.fixture
def numbered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in `gdbus` answering ids 1, 2, 3…, or the replaces_id it is given, as a daemon does; each
    call's arguments appended, one call per line; each call sleeps `gdbus.delay` seconds if written."""
    fake, calls, n = tmp_path / "gdbus", tmp_path / "gdbus.calls", tmp_path / "gdbus.n"
    delay = tmp_path / "gdbus.delay"
    fake.write_text(
        f'#!/bin/sh\necho "$*" >> {calls}\nsleep $(cat {delay} 2>/dev/null || echo 0)\n'
        "r=$(echo \"$*\" | sed 's/.* -- [^ ]* \\([0-9]*\\) .*/\\1/')\n"
        f'if [ "$r" != 0 ]; then echo "(uint32 $r,)"; exit; fi\n'
        f'n=$(( $(cat {n} 2>/dev/null || echo 0) + 1 ))\necho $n > {n}\necho "(uint32 $n,)"\n'
    )
    fake.chmod(0o755)
    monkeypatch.setattr(widget_qt, "GDBUS", str(fake))
    return calls


async def _shown(notifier: widget_qt.Notifier, chat: str, n: int) -> None:
    notifier.show(chat, "t", "b")
    await until(lambda: len(notifier.chats) == n and chat in notifier.chats.values(), f"{chat} sent")


async def test_a_chat_keeps_one_live_notification(app: QApplication, numbered: Path) -> None:
    """A newer notification for a chat replaces the chat's live one (replaces_id); other chats keep theirs."""
    notifier = widget_qt.Notifier(widget_qt.QDBusConnection.sessionBus())
    await _shown(notifier, A, 1)
    await _shown(notifier, B, 2)
    notifier.show(A, "t", "b")
    await until(lambda: len(_lines(numbered)) == 3 and not notifier.findChildren(QProcess), "A replaced")
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert notifier.chats == {2: B, 1: A}
    assert _replaces(numbered) == ["0", "0", "1"]


def _lines(calls: Path) -> list[str]:
    return calls.read_text().splitlines() if calls.exists() else []


def _replaces(calls: Path) -> list[str]:
    return [line.split(" -- ")[1].split()[1] for line in _lines(calls)]


async def test_a_chat_busy_while_its_send_is_in_flight_chains_its_replaces(
    app: QApplication, numbered: Path
) -> None:
    """Shown again while its first send is still out: one more send once that one answers, replacing it with
    the latest text (no second notification whose Open would be lost); Open on the live id opens the chat."""
    (numbered.parent / "gdbus.delay").write_text("0.3")
    notifier = widget_qt.Notifier(widget_qt.QDBusConnection.sessionBus())
    got: list[str] = []
    notifier.clicked.connect(got.append)
    for body in ("b1", "b2", "b3"):
        notifier.show(A, "t", body)
    await until(lambda: len(_lines(numbered)) == 2 and not notifier.pending, "chained")
    assert notifier.chats == {1: A}
    for _ in range(60):  # no third send turns up
        QApplication.processEvents()
        await asyncio.sleep(0.01)
    lines = numbered.read_text().splitlines()
    assert _replaces(numbered) == ["0", "1"]
    assert "'b1'" in lines[0] and "'b3'" in lines[1]
    notifier.invoked(1, "default")
    assert got == [A]


async def test_another_chat_is_not_held_by_one_in_flight(app: QApplication, numbered: Path) -> None:
    """Only the same chat waits: B, shown between two A's, goes out at once; A's second send replaces A's."""
    (numbered.parent / "gdbus.delay").write_text("0.3")
    notifier = widget_qt.Notifier(widget_qt.QDBusConnection.sessionBus())
    notifier.show(A, "t", "a1")
    await until(lambda: len(_lines(numbered)) == 1, "A out")
    notifier.show(B, "t", "b1")
    notifier.show(A, "t", "a2")
    await until(lambda: len(_lines(numbered)) == 2, "B out")
    assert "'b1'" in _lines(numbered)[1]
    await until(lambda: len(_lines(numbered)) == 3 and not notifier.pending, "A again")
    assert set(notifier.chats.values()) == {A, B}
    assert [ln.split(" -- ")[1].split()[4] for ln in _lines(numbered)] == ["'a1'", "'b1'", "'a2'"]
    assert _replaces(numbered)[2] == str(next(n for n, c in notifier.chats.items() if c == A))


async def test_a_reply_with_another_id_than_replaced_means_a_restarted_daemon(
    app: QApplication, gdbus: Path
) -> None:
    """A daemon answering a replace with a new id (one that issues fresh ids after a restart): every id kept
    before that reply is dropped, none routes a click or goes out as a stale replaces_id; a first send for
    a chat (replaces 0) drops nothing. xfce4-notifyd instead adopts any replaces_id: the owner watcher is
    what clears on its restart."""
    notifier = widget_qt.Notifier(widget_qt.QDBusConnection.sessionBus())
    notifier.chats = {5: B}
    notifier.show(C, "t", "b")
    await until(lambda: 7 in notifier.chats, "a first send")
    assert notifier.chats == {5: B, 7: C}
    notifier.chats = {5: A, 6: B}
    notifier.show(A, "t", "b")
    await until(lambda: 7 in notifier.chats, "the replace answered")
    assert notifier.chats == {7: A}


async def test_a_restarted_daemon_forgets_the_ids(app: QApplication, numbered: Path) -> None:
    """The notification service changing owner (a restarted daemon reuses ids): no old id routes a click."""
    notifier = widget_qt.Notifier(widget_qt.QDBusConnection.sessionBus())
    got: list[str] = []
    notifier.clicked.connect(got.append)
    await _shown(notifier, A, 1)
    notifier.watcher.serviceOwnerChanged.emit(widget_qt.NOTIFICATIONS, ":1.4", ":1.9")
    assert notifier.chats == {}
    notifier.invoked(1, "default")
    assert got == []


async def test_awaited_clicks_are_capped(app: QApplication, gdbus: Path) -> None:
    """At most KEPT notifications await a click: past it, the oldest is forgotten."""
    notifier = widget_qt.Notifier(widget_qt.QDBusConnection.sessionBus())
    notifier.chats = {1000 + i: f"19:c{i}@thread.v2" for i in range(widget_qt.KEPT)}
    notifier.show(A, "t", "b")
    await until(lambda: 7 in notifier.chats, "the new one kept")
    assert len(notifier.chats) == widget_qt.KEPT
    assert 1000 not in notifier.chats and 1001 in notifier.chats


async def test_a_gdbus_failure_logs_its_error(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing call logs gdbus' own error, cut to 200 characters."""
    notifier = widget_qt.Notifier(widget_qt.QDBusConnection.sessionBus())
    failing = tmp_path / "gdbus"
    failing.write_text("#!/bin/sh\nprintf 'Error: no service%0300d' 0 >&2\nexit 1\n")
    failing.chmod(0o755)
    monkeypatch.setattr(widget_qt, "GDBUS", str(failing))
    with structlog.testing.capture_logs() as logs:
        notifier.show(A, "t", "b")
        await until(lambda: any(e["event"] == "widget_notify_failed" for e in logs), "the failure logged")
    event = next(e for e in logs if e["event"] == "widget_notify_failed")
    assert event["stderr"].startswith("Error: no service") and len(event["stderr"]) == 200
