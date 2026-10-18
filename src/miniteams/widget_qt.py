"""The native widget window (spec 024): the board page in QtWebEngine, a tray icon, close-to-tray.

The page and the window talk over a QWebChannel: the page finds `window.miniteamsTray`, a promise of the
`tray` object below, publishes its badge count through `badge()` and listens to `settings`. A browser has
no such promise, so the page's tray code stays inert there.
"""

import hashlib
import os
import signal
from importlib.resources import files
from pathlib import Path

import shiboken6
import structlog
from PySide6.QtCore import (
    QByteArray,
    QEvent,
    QFile,
    QIODevice,
    QLockFile,
    QObject,
    QRectF,
    Qt,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineProfileBuilder,
    QWebEngineScript,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .widget import badge_text, chat_link, is_board, is_external, token, tooltip

APP = "miniteams"
SIZES = (16, 22, 24, 32, 48, 64, 128, 256)
BADGE = 64  # px the tray badge is drawn at; the panel scales it down
# Before the page's own script, in its world: the page reads the promise once, at its start.
BOOT = (
    "window.miniteamsTray = new Promise((ok) =>"
    " new QWebChannel(qt.webChannelTransport, (c) => ok(c.objects.tray)));"
)

log = structlog.get_logger()


def app_icon() -> QIcon:
    """favicon.svg rendered at every size a panel or switcher asks for, 256 px included."""
    svg = QSvgRenderer(QByteArray(files("miniteams").joinpath("favicon.svg").read_bytes()))
    icon = QIcon()
    for size in SIZES:
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        svg.render(p)
        p.end()
        icon.addPixmap(pm)
    return icon


def badged(icon: QIcon, text: str) -> QIcon:
    """`icon` with `text` in a pill at its bottom right; `icon` itself when there is no text."""
    if not text:
        return icon
    pm = icon.pixmap(BADGE, BADGE)
    # Contrasting with the dark icon and its blue bubble: an "@" in that blue was unreadable at 22 px.
    fill, ink = ("#f5a524", "#171a21") if text == "@" else ("#e5484d", "#ffffff")
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = QFont()
    font.setBold(True)
    font.setPixelSize(34 if len(text) < 3 else 24)
    p.setFont(font)
    w = min(BADGE, max(38, p.fontMetrics().horizontalAdvance(text) + 12))
    pill = QRectF(BADGE - w, BADGE - 38, w, 38)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(fill))
    p.drawRoundedRect(pill, 19, 19)
    p.setPen(QColor(ink))
    p.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return QIcon(pm)


def _channel_js() -> str:
    f = QFile(":/qtwebchannel/qwebchannel.js")
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError("qwebchannel.js missing from the Qt install")
    return bytes(f.readAll().data()).decode()


class Tray(QObject):
    """The page's side of the tray: `badge(pref, count)` from the page, `settings` to it."""

    settings = Signal()

    def __init__(self, icon: QIcon, tray: QSystemTrayIcon) -> None:
        super().__init__()
        self.icon, self.tray, self.shown = icon, tray, ("", "miniteams")

    @Slot(str, int)
    def badge(self, pref: str, count: int) -> None:
        now = (badge_text(pref, count), tooltip(pref, count))
        if now == self.shown:  # every board broadcast publishes: most change nothing
            return
        self.shown = now
        self.tray.setIcon(badged(self.icon, now[0]))
        self.tray.setToolTip(now[1])


def browse(url: QUrl | str, base: str, config_dir: Path) -> None:
    """`url` in the system browser: web pages only; a chat page of ours ("Chat here" without --browser)
    with the launch token, as that browser has no cookie."""
    link = url.toString() if isinstance(url, QUrl) else url
    if is_external(link):
        QDesktopServices.openUrl(QUrl(chat_link(link, base, token(config_dir))))
    else:
        log.warning("widget_link_refused", scheme=QUrl(link).scheme())


class Sink(QWebEnginePage):
    """A page asked for a new window or tab: its URL goes to the system browser, the page loads nothing."""

    def __init__(self, page: Page) -> None:
        super().__init__(page.profile(), page)
        self.base, self.config_dir = page.base, page.config_dir

    def acceptNavigationRequest(
        self, url: QUrl | str, kind: QWebEnginePage.NavigationType, main: bool
    ) -> bool:
        browse(url, self.base, self.config_dir)
        self.deleteLater()
        return False


class Page(QWebEnginePage):
    def __init__(self, profile: QWebEngineProfile, base: str, config_dir: Path, parent: QObject) -> None:
        super().__init__(profile, parent)
        self.base, self.config_dir = base, config_dir

    def acceptNavigationRequest(
        self, url: QUrl | str, kind: QWebEnginePage.NavigationType, main: bool
    ) -> bool:
        link = url.toString() if isinstance(url, QUrl) else url
        if is_board(link, self.base):
            return True
        # Anything else (a chat page, a Teams link followed in place) would replace the board.
        if main:
            browse(url, self.base, self.config_dir)
        return False

    def createWindow(self, kind: QWebEnginePage.WebWindowType) -> QWebEnginePage:
        return Sink(self)


class Window(QWebEngineView):
    """The board; closing it hides it to the tray, tray Quit ends the process."""

    def __init__(self, base: str, profile: QWebEngineProfile, icon: QIcon, config_dir: Path) -> None:
        super().__init__()
        self.setWindowTitle(APP)
        self.setWindowIcon(icon)
        self.resize(480, 760)
        page = Page(profile, base, config_dir, self)
        script = QWebEngineScript()
        script.setName("miniteams-tray")
        script.setSourceCode(_channel_js() + "\n" + BOOT)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        page.scripts().insert(script)
        self.setPage(page)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(APP)
        self.bridge = Tray(icon, self.tray)
        channel = QWebChannel(page)
        channel.registerObject("tray", self.bridge)
        page.setWebChannel(channel)
        menu = QMenu(self)
        for label, act in (
            ("Show", self.reveal),
            ("Settings…", self.open_settings),
            ("Quit", QApplication.quit),
        ):
            action = QAction(label, menu)
            action.triggered.connect(act)
            menu.addAction(action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.activated)
        self.tray.show()
        # The builder's profile is Qt-owned: as the last child it goes after the page, as Qt requires.
        profile.setParent(self)

    def activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # A panel with menu-is-primary opens the menu on a left click itself and sends middle clicks here.
        if reason != QSystemTrayIcon.ActivationReason.Context:
            self.reveal()

    def reveal(self) -> None:
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def open_settings(self) -> None:
        self.reveal()
        if shiboken6.isValid(self.bridge):  # the page can deleteLater() any object on the channel
            self.bridge.settings.emit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if not (self.tray.isVisible() and QSystemTrayIcon.isSystemTrayAvailable()):
            QApplication.quit()  # no tray to come back from: a hidden window would be unreachable
            return
        event.ignore()  # QWebEngineView's own close tears the page down: hidden, it must keep counting
        self.hide()


def profile(path: Path) -> QWebEngineProfile:
    """Named, so on disk: the `mt` cookie survives restarts. Owner-only: it holds the board's secret.
    Built with its paths set: a plain named profile first creates its default dirs under ~/.local/share."""
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    builder = QWebEngineProfileBuilder()
    builder.setPersistentStoragePath(str(path))
    builder.setCachePath(str(path))
    builder.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
    return builder.createProfile(APP)


def _server_name(storage: Path) -> str:
    return "miniteams-widget-" + hashlib.sha256(str(storage.resolve()).encode()).hexdigest()[:16]


def _wake(name: str) -> bool:
    """Ask the running widget to show itself; False when it does not answer."""
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(2000):
        return False
    sock.write(b"show")
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


def main(base: str, launch: str, config_dir: Path) -> int:
    app = QApplication.instance() or QApplication([APP])  # argv[0] names the X11 WM_CLASS instance
    assert isinstance(app, QApplication)
    storage = config_dir / "widget"
    storage.mkdir(parents=True, mode=0o700, exist_ok=True)
    # One window per profile: a second one would share its cookie store, and spend a token for nothing.
    lock_path = str(storage / "widget.lock")
    lock = QLockFile(lock_path)
    name = _server_name(storage)
    if not lock.tryLock(0):
        if not _wake(name):
            log.warning("widget_locked_no_answer", lock=lock_path)
            return 1
        log.info("widget_already_running")
        return 0
    # Listening before the window exists: a launch during startup queues, it is answered once the loop runs.
    QLocalServer.removeServer(name)  # a crashed run's socket file: we hold the lock, it is ours
    server = QLocalServer(app)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(name):  # the window works; a second launch just cannot show it
        log.warning("widget_wake_unavailable", error=server.errorString())
    app.setApplicationName(APP)
    app.setDesktopFileName(APP)
    app.setQuitOnLastWindowClosed(False)
    icon = app_icon()
    app.setWindowIcon(icon)
    store = profile(storage)
    window = Window(base, store, icon, config_dir)
    server.newConnection.connect(lambda: _shown(server, window))
    # Qt's loop never returns to Python on its own: a timer lets SIGINT / SIGTERM reach the handler.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: QApplication.quit())
    tick = QTimer(window)
    tick.timeout.connect(lambda: None)
    tick.start(500)
    window.load(QUrl(f"{base}/#{launch}" if launch else f"{base}/"))
    window.show()
    log.info("widget_started", url=f"{base}/")
    code = int(app.exec())
    dispose(window)
    server.close()
    server.deleteLater()
    lock.unlock()
    return code


def _shown(server: QLocalServer, window: Window) -> None:
    while (conn := server.nextPendingConnection()) is not None:
        conn.disconnectFromServer()
        conn.deleteLater()
    window.reveal()


def dispose(window: Window) -> None:
    """The window now, its page, then its profile (`Window` owns both, in that order)."""
    window.tray.hide()
    window.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
