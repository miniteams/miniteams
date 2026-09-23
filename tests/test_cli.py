"""CLI input resolution for send/update (no network: auth + write calls are mocked)."""

import argparse
from typing import Any

import pytest

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


def test_videos_conflicts_with_no_media(capsys, monkeypatch) -> None:
    """--videos rides the media pass; with --no-media it would be a silent no-op."""
    import pytest

    from miniteams import archive, auth

    async def fake_run_archive(settings, data_dir, **kw):
        raise AssertionError("guard bypassed: the archive run must never start")

    monkeypatch.setattr(archive, "run_archive", fake_run_archive)
    monkeypatch.setattr(auth.TokenSource, "acquire", lambda self: {"access_token": "x"})

    with pytest.raises(SystemExit) as exc:
        cli.main(["archive", "--videos", "--no-media"])
    assert exc.value.code == 2
    assert "drop --no-media" in capsys.readouterr().err


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


def _archive_retry_case(monkeypatch, outcomes: list[Any], argv: list[str]) -> tuple[int, list[float]]:
    """Drive `miniteams archive` over a scripted sequence of run_archive outcomes (exception
    instances are raised, values returned) with every sleep captured instead of served."""
    import time

    from miniteams import archive, auth

    slept: list[float] = []
    pending = list(outcomes)

    async def fake_run_archive(settings, data_dir, **kw):
        outcome = pending.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(archive, "run_archive", fake_run_archive)
    monkeypatch.setattr(auth.TokenSource, "acquire", lambda self: {"access_token": "x"})
    monkeypatch.setattr(time, "sleep", slept.append)
    return cli.main(argv), slept


def test_archive_retries_transport_resets(monkeypatch) -> None:
    """A reset mid-run is transient; the archive is reentrant, so a fresh run resumes it."""
    import httpx

    rc, slept = _archive_retry_case(
        monkeypatch,
        [
            ConnectionResetError(104, "Connection reset by peer"),
            httpx.ConnectError("[Errno 104] Connection reset by peer"),
            httpx.RemoteProtocolError("server disconnected"),
            False,  # completed run, not auth-expired
        ],
        ["archive"],
    )
    assert rc == 0
    assert slept == [2.0, 4.0, 8.0]  # backoff grows per consecutive failure


def test_archive_gives_up_after_four_retries(monkeypatch) -> None:
    """msal talks over requests, whose RequestException subclasses OSError — same guard."""
    import pytest

    requests = pytest.importorskip("requests")  # transitive via msal
    boom = requests.exceptions.ConnectionError(
        "('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))"
    )
    rc, slept = _archive_retry_case(monkeypatch, [boom] * 6, ["archive"])
    assert rc == 1
    assert len(slept) == 4  # four retries, then the fifth failure gives up


def test_archive_retry_streak_resets_on_a_completed_run(monkeypatch) -> None:
    """--loop runs for days: without the reset, four resets spread over a week would kill it."""
    import httpx

    fail = httpx.ReadError("connection reset")
    rc, slept = _archive_retry_case(
        monkeypatch,
        [fail] * 4 + [False] + [fail] * 4 + [False] + [KeyboardInterrupt()],
        ["archive", "--loop", "300"],
    )
    assert rc == 0  # the 8th failure is only the 4th of its streak, so the run survives
    # Two backoff ladders, each restarted from scratch, plus one inter-cycle loop sleep each.
    assert slept == [2.0, 4.0, 8.0, 16.0, 300, 2.0, 4.0, 8.0, 16.0, 300]


def _status_error(code: int) -> Any:
    import httpx

    req = httpx.Request("GET", "https://chatsvc.example/x")
    return httpx.HTTPStatusError(str(code), request=req, response=httpx.Response(code, request=req))


def test_archive_retries_upstream_5xx(monkeypatch) -> None:
    """A chatsvc 502/503 is as transient as a reset — get_with_retry backs off on 429 only."""
    rc, slept = _archive_retry_case(monkeypatch, [_status_error(503), _status_error(502), False], ["archive"])
    assert rc == 0
    assert slept == [2.0, 4.0]


def test_archive_does_not_retry_4xx(monkeypatch) -> None:
    """401/403/404 do not heal by waiting: surface them instead of burning the retry budget."""
    rc, slept = _archive_retry_case(monkeypatch, [_status_error(401)], ["archive"])
    assert rc == 1
    assert slept == []


def test_web_parser_defaults_and_limit_validation(monkeypatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "cmd_web", lambda s, a: seen.update(vars(a)) or 0)
    monkeypatch.setenv("MINITEAMS_TENANT_ID", "t")
    assert cli.main(["web"]) == 0
    assert seen["limit"] == 50 and seen["bind"] is None and seen["reactions"] is False
    assert seen["opener"] == "xdg-open" and seen["open_scheme"] == "msteams" and seen["browser"] == ""
    with pytest.raises(SystemExit):
        cli.main(["web", "--limit", "-1"])


def test_web_passes_self_mri_from_the_access_token(monkeypatch) -> None:
    import base64
    import json

    from miniteams import cli as C
    from miniteams import web

    body = base64.urlsafe_b64encode(json.dumps({"oid": "me-guid"}).encode()).decode().rstrip("=")
    aad = {"access_token": f"h.{body}.s"}
    monkeypatch.setattr(C, "_ensure_skype_token", lambda settings: (aad, "skype"))
    got: dict[str, Any] = {}

    async def fake_run(settings: Any, skype_token: str, bearer: str, *args: Any, **kw: Any) -> None:
        got.update(kw, skype_token=skype_token, bearer=bearer)

    monkeypatch.setattr(web, "run", fake_run)
    assert C.main(["web", "--bind", "127.0.0.77:47123"]) == 0
    assert (got["me"], got["skype_token"], got["bearer"]) == ("8:orgid:me-guid", "skype", f"h.{body}.s")


def test_archive_retries_a_transient_aad_error(monkeypatch) -> None:
    """AAD throttling or a 5xx is not a dead refresh token: back off and resume, do not stop."""
    from miniteams.auth import AuthUnavailable

    rc, slept = _archive_retry_case(
        monkeypatch,
        [AuthUnavailable("silent token refresh failed (temporarily_unavailable)"), False],
        ["archive"],
    )
    assert rc == 0
    assert slept == [2.0]
