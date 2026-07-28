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


def test_retry_assets_cannot_be_looped(capsys, monkeypatch) -> None:
    """A one-shot override on a timer would re-request every dead asset every cycle."""
    import pytest

    from miniteams import archive, auth

    # Stub the run itself: without this, a regressed guard makes this test authenticate and loop
    # for real instead of failing — a hang, not a red.
    async def fake_run_archive(settings, data_dir, **kw):
        raise AssertionError("guard bypassed: the archive run must never start with --loop")

    monkeypatch.setattr(archive, "run_archive", fake_run_archive)
    monkeypatch.setattr(auth.TokenSource, "acquire", lambda self: {"access_token": "x"})

    with pytest.raises(SystemExit) as exc:
        cli.main(["archive", "--retry-assets", "--loop", "300"])
    assert exc.value.code == 2  # argparse usage error, raised before any work
    assert "cannot be combined with --loop" in capsys.readouterr().err


def test_assets_only_no_longer_forces_denied_assets(monkeypatch) -> None:
    """--assets-only is scope, not policy: it must respect the backoff unless --retry-assets."""
    from miniteams import archive, auth

    seen: dict[str, Any] = {}

    async def fake_run_archive(settings, data_dir, **kw):
        seen.update(kw)
        return False  # not auth-expired → cmd_archive returns 0 (no --loop)

    monkeypatch.setattr(archive, "run_archive", fake_run_archive)
    monkeypatch.setattr(auth.TokenSource, "acquire", lambda self: {"access_token": "x"})

    assert cli.main(["archive", "--assets-only"]) == 0
    assert seen["assets_only"] is True
    assert seen["retry_assets"] is False
    assert seen["verify_media"] is False  # no longer smuggled in by --assets-only

    seen.clear()
    assert cli.main(["archive", "--assets-only", "--retry-assets"]) == 0
    assert seen["retry_assets"] is True  # the policy is opted into explicitly
