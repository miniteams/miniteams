"""`miniteams widget` without Qt (spec 024): the checks before the window opens, the badge, the links."""

import json
import re
import socket
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from miniteams import cli, widget
from miniteams import web as W
from miniteams.config import Settings


@pytest.fixture
def listening() -> Iterator[int]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        yield s.getsockname()[1]


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _web_json(settings: Settings, **data: Any) -> Path:
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    path = settings.config_dir / "web.json"
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize(
    ("pref", "count", "text", "tip"),
    [
        ("unread", 0, "", "miniteams — 0 unread"),
        ("unread", 7, "7", "miniteams — 7 unread"),
        ("unread", 99, "99", "miniteams — 99 unread"),
        ("unread", 100, "99+", "miniteams — 100 unread"),
        ("all", 3, "3", "miniteams — 3 in Inbox"),
        ("mention", 1, "1", "miniteams — 1 mention"),
        ("mention", 4, "4", "miniteams — 4 mentions"),
        ("mention", 100, "99+", "miniteams — 100 mentions"),
        ("mention", 0, "", "miniteams — 0 mentions"),
        ("none", 5, "", "miniteams"),
        ("seen", 5, "", "miniteams"),  # not a badge choice: no badge
        ("unread", -1, "", "miniteams — 0 unread"),
    ],
)
def test_badge_and_tooltip_follow_the_preference(pref: str, count: int, text: str, tip: str) -> None:
    """024-AC4: the badge's text and the tooltip per preference; "99+" past 99, mentions included."""
    assert widget.badge_text(pref, count) == text
    assert widget.tooltip(pref, count) == tip


@pytest.mark.parametrize(
    ("url", "board"),
    [
        ("http://127.0.0.5:8765/", True),
        ("http://127.0.0.5:8765", True),
        ("http://127.0.0.5:8765/#tok", True),
        ("http://127.0.0.5:8765/?tab=unread", True),
        ("http://127.0.0.5:8765/chat/19%3Ax", False),
        ("http://127.0.0.5:8766/", False),
        ("https://127.0.0.5:8765/", False),
        ("http://127.0.0.6:8765/", False),
        ("https://teams.microsoft.com/l/chat/x", False),
    ],
)
def test_only_the_board_stays_in_the_window(url: str, board: bool) -> None:
    """024-AC6: the window keeps the served board; every other page goes elsewhere."""
    assert widget.is_board(url, "http://127.0.0.5:8765") is board


@pytest.mark.parametrize(
    ("url", "external"),
    [
        ("https://teams.microsoft.com/l/chat/x", True),
        ("http://example.com/", True),
        ("file:///etc/passwd", False),
        ("javascript:alert(1)", False),
        ("msteams:/l/chat/x", False),
        ("about:blank", False),
    ],
)
def test_only_web_links_reach_the_system_browser(url: str, external: bool) -> None:
    """024-AC6: a message's link is data: no file: or custom scheme handed to the desktop."""
    assert widget.is_external(url) is external


def test_target_reads_web_json_once_the_server_answers(settings: Settings, listening: int) -> None:
    """024-AC1: base URL and launch token from web.json."""
    _web_json(settings, host="127.0.0.1", port=listening, token="tok")
    assert widget.target(settings.config_dir) == (f"http://127.0.0.1:{listening}", "tok")


def test_target_without_web_json_names_the_file(settings: Settings) -> None:
    """024-AC7: no web.json: start `miniteams web`, naming where it was looked for."""
    with pytest.raises(RuntimeError, match=r"no .*web\.json: start `miniteams web` first"):
        widget.target(settings.config_dir)


def test_target_with_the_server_down_names_the_address(settings: Settings) -> None:
    """024-AC7: web.json there but nothing listening: the address tried."""
    port = _closed_port()
    _web_json(settings, host="127.0.0.1", port=port, token="tok")
    with pytest.raises(RuntimeError, match=rf"not running at http://127\.0\.0\.1:{port}/"):
        widget.target(settings.config_dir)


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ({"host": "10.0.0.1", "port": 80}, "refusing non-loopback bind '10.0.0.1'"),
        ({"port": 80}, 'missing "host"'),
        ({"host": "127.0.0.1"}, 'missing "port"'),
        ({"host": "127.0.0.1", "port": "x"}, "invalid literal for int()"),
    ],
)
def test_web_json_is_read_one_way_for_web_and_widget(settings: Settings, data: Any, reason: str) -> None:
    """One reader, one message: `web` and `widget` name the file and what is wrong with it, never dial it."""
    path = _web_json(settings, **data)
    advice = "; fix it, or delete it to draw a new address"
    message = rf"^cannot use {re.escape(str(path))}: {re.escape(reason)}.*{re.escape(advice)}$"
    for read in (lambda: widget.target(settings.config_dir), lambda: W._persisted_bind(settings)):
        with pytest.raises(RuntimeError, match=message):
            read()


def test_web_json_token_and_bind_come_from_the_shared_reader(settings: Settings) -> None:
    _web_json(settings, host="127.0.0.1", port=4000, token="tok")
    assert W.read_web_json(settings.config_dir) == ("127.0.0.1", 4000, "tok")
    assert W._persisted_bind(settings) == ("127.0.0.1", 4000)
    _web_json(settings, host="127.0.0.1", port=4000)
    assert W.read_web_json(settings.config_dir) == ("127.0.0.1", 4000, "")
    assert widget.token(settings.config_dir) == ""
    _web_json(settings, port=4000, token="tok")
    assert widget.token(settings.config_dir) == ""  # unusable: a chat page then opens as is
    (settings.config_dir / "web.json").unlink()
    assert W.read_web_json(settings.config_dir) is None and widget.token(settings.config_dir) == ""


@pytest.mark.parametrize(
    ("url", "fresh", "opened"),
    [
        ("http://127.0.0.5:8765/chat/19%3Ax", "tok", "http://127.0.0.5:8765/chat/19%3Ax#tok"),
        (
            "http://127.0.0.5:8765/chat/19%3Ax?chatnav=split",
            "tok",
            "http://127.0.0.5:8765/chat/19%3Ax?chatnav=split#tok",
        ),
        ("http://127.0.0.5:8765/chat/19%3Ax#old", "tok", "http://127.0.0.5:8765/chat/19%3Ax#tok"),
        ("http://127.0.0.5:8765/chat/19%3Ax", "", "http://127.0.0.5:8765/chat/19%3Ax"),
        ("http://127.0.0.6:8765/chat/19%3Ax", "tok", "http://127.0.0.6:8765/chat/19%3Ax"),
        ("https://teams.microsoft.com/chat/x", "tok", "https://teams.microsoft.com/chat/x"),
        ("http://127.0.0.5:8765/media/x", "tok", "http://127.0.0.5:8765/media/x"),
        ("http://127.0.0.5:8765/", "tok", "http://127.0.0.5:8765/"),
    ],
)
def test_only_our_chat_pages_get_the_token(url: str, fresh: str, opened: str) -> None:
    """A chat page opened in the system browser signs in with the launch token; nothing else carries it."""
    assert widget.chat_link(url, "http://127.0.0.5:8765", fresh) == opened


def _without_qt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "miniteams.widget_qt", raising=False)
    monkeypatch.delattr("miniteams.widget_qt", raising=False)  # `from . import` reads it first
    for name in [
        "PySide6",
        "shiboken6",
        *(m for m in sys.modules if m.startswith(("PySide6.", "shiboken6."))),
    ]:
        monkeypatch.setitem(sys.modules, name, None)  # what a missing install raises on import


def test_cli_without_the_extra_says_how_to_install(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """024-AC7: without PySide6, `miniteams widget` exits 1 with the install command."""
    _without_qt(monkeypatch)
    monkeypatch.setenv("MINITEAMS_CONFIG_DIR", str(settings.config_dir))
    assert cli.main(["widget"]) == 1
    assert "uv sync --extra widget" in capsys.readouterr().err


def test_a_broken_install_is_not_reported_as_missing(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Another missing module than PySide6 is a real error, not the install hint."""
    monkeypatch.delattr("miniteams.widget_qt", raising=False)
    monkeypatch.setitem(sys.modules, "miniteams.widget_qt", None)
    with pytest.raises(ModuleNotFoundError):
        widget.run(settings)


def test_run_opens_the_window_on_web_json(
    settings: Settings, listening: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """024-AC1: the window gets the base URL, the token and its profile under the config dir."""
    calls: list[tuple[str, str, Path]] = []
    fake = types.ModuleType("miniteams.widget_qt")
    fake.main = lambda *a: calls.append(a) or 0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "miniteams.widget_qt", fake)
    monkeypatch.setattr("miniteams.widget_qt", fake, raising=False)
    _web_json(settings, host="127.0.0.1", port=listening, token="tok")
    assert widget.run(settings) == 0
    assert calls == [(f"http://127.0.0.1:{listening}", "tok", settings.config_dir)]


def test_run_with_the_server_down_exits_1(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """024-AC7: the window never opens; the message names the address."""
    fake = types.ModuleType("miniteams.widget_qt")
    fake.main = lambda *a: pytest.fail("no window expected")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "miniteams.widget_qt", fake)
    monkeypatch.setattr("miniteams.widget_qt", fake, raising=False)
    port = _closed_port()
    _web_json(settings, host="127.0.0.1", port=port, token="tok")
    assert widget.run(settings) == 1
    assert f"http://127.0.0.1:{port}/" in capsys.readouterr().err
