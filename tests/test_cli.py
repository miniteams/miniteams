"""CLI input resolution for send/update (no network: auth + write calls are mocked)."""

import argparse
from typing import Any

from miniteams import cli
from miniteams.config import Settings


def _ns(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def test_cmd_send_without_text_or_file_returns_2(settings: Settings) -> None:
    rc = cli.cmd_send(settings, _ns(text=None, file=None, thread="48:notes", html=False))
    assert rc == 2  # returns before auth


def test_cmd_update_without_text_or_file_returns_2(settings: Settings) -> None:
    rc = cli.cmd_update(settings, _ns(target="123", text=None, file=None, thread="48:notes", html=False))
    assert rc == 2


def test_cmd_send_file_takes_precedence_over_text(settings: Settings, monkeypatch, tmp_path) -> None:
    msg = tmp_path / "m.txt"
    msg.write_text("from-file")
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: ({}, "sk"))
    seen: dict[str, Any] = {}

    def fake_send(*a: Any, **k: Any) -> None:
        seen.update(text=a[3], html=k["is_html"])

    monkeypatch.setattr("miniteams.send.send_message", fake_send)
    rc = cli.cmd_send(settings, _ns(text="positional", file=str(msg), thread="48:notes", html=True))
    assert rc == 0
    assert seen == {"text": "from-file", "html": True}


def test_cmd_update_deeplink_overrides_thread(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: ({}, "sk"))
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        "miniteams.send.edit_message",
        lambda s, tok, thread, mid, text, *, is_html: seen.update(thread=thread, mid=mid, text=text),
    )
    url = "https://teams.cloud.microsoft/l/message/48:notes/123?context=x"
    rc = cli.cmd_update(settings, _ns(target=url, text="new", file=None, thread="OTHER", html=False))
    assert rc == 0
    assert seen == {"thread": "48:notes", "mid": "123", "text": "new"}
