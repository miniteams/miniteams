"""`miniteams widget`: the board of a running `miniteams web` in a native window with a tray icon (spec 024).

The window lives in `widget_qt`, which imports PySide6, an optional extra: this module needs no Qt, so
the checks before the window opens and the badge and link rules are tested without it.
"""

import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .config import Settings
from .web import netloc, read_web_json

INSTALL = (
    "miniteams widget needs the optional PySide6 dependencies: "
    "`uv sync --extra widget`, or run it as `uv run --extra widget miniteams widget`"
)
# The page's badge preference (its `filters` key, ui.js) → the tooltip's noun; "none" shows no badge.
COUNTED = {"unread": "unread", "mention": "mention", "all": "in Inbox"}


def target(config_dir: Path) -> tuple[str, str]:
    """(base URL, launch token) from web.json, once the server answers; RuntimeError naming what failed."""
    found = read_web_json(config_dir)
    if found is None:
        raise RuntimeError(f"no {config_dir / 'web.json'}: start `miniteams web` first")
    host, port, token = found
    base = f"http://{netloc(host, port)}"
    try:
        socket.create_connection((host, port), timeout=2).close()
    except OSError as exc:
        raise RuntimeError(f"miniteams web is not running at {base}/ ({exc.strerror or exc})") from None
    return base, token


def token(config_dir: Path) -> str:
    """web.json's current launch token, read at each use: the server renews it after every one."""
    try:
        found = read_web_json(config_dir)
    except RuntimeError:
        return ""
    return found[2] if found else ""


def chat_link(url: str, base: str, fresh: str) -> str:
    """A chat page of the served board, as the system browser opens it: signed in by the launch token in
    its fragment, as `--browser` opens one. Anything else unchanged."""
    u, b = urlsplit(url), urlsplit(base)
    if not fresh or (u.scheme, u.netloc) != (b.scheme, b.netloc) or not u.path.startswith("/chat/"):
        return url
    return u._replace(fragment=fresh).geturl()


def badge_text(pref: str, count: int) -> str:
    """What the tray icon's badge shows: nothing, the count ("99+" past 99), or "@" for mentions."""
    if pref not in COUNTED or count <= 0:
        return ""
    if pref == "mention":
        return "@"
    return "99+" if count > 99 else str(count)


def tooltip(pref: str, count: int) -> str:
    if pref not in COUNTED:
        return "miniteams"
    noun = COUNTED[pref] + ("s" if pref == "mention" and count != 1 else "")
    return f"miniteams — {max(count, 0)} {noun}"


def is_board(url: str, base: str) -> bool:
    """The board page of the served origin, the only page the window shows (chat pages open elsewhere)."""
    u, b = urlsplit(url), urlsplit(base)
    return (u.scheme, u.netloc) == (b.scheme, b.netloc) and u.path in ("", "/")


def is_external(url: str) -> bool:
    """A link the system browser may open: web pages only, never file: or a custom scheme from a message."""
    return urlsplit(url).scheme in ("http", "https")


def run(settings: Settings) -> int:
    try:
        from . import widget_qt
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith(("PySide6", "shiboken6")):
            raise  # a broken install, not a missing one: the install hint would mislead
        print(INSTALL, file=sys.stderr)
        return 1
    try:
        base, launch = target(settings.config_dir)
    except RuntimeError as exc:
        print(f"miniteams widget: {exc}", file=sys.stderr)
        return 1
    return widget_qt.main(base, launch, settings.config_dir)
