"""CLI input resolution for send/update/react/typing (no network: auth + write calls are mocked)."""

import argparse
from typing import Any

import pytest
import structlog.testing

from miniteams import cli, compose
from miniteams.config import Settings


@pytest.fixture(autouse=True)
def _keep_test_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    # setup_logging caches cli.log on first use, past the reach of a later capture_logs.
    monkeypatch.setattr(cli, "setup_logging", lambda level: None)


class _NoAccountApp:
    def get_accounts(self) -> list[Any]:
        return []


@pytest.fixture(autouse=True)
def _offline_msal(monkeypatch: pytest.MonkeyPatch) -> None:
    # A send reads the sender name from the token cache; a real msal app discovers its authority online.
    from miniteams import auth

    monkeypatch.setattr(auth, "_app", lambda *a, **k: _NoAccountApp())


def _ns(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def test_cmd_send_without_text_or_file_returns_2(settings: Settings) -> None:
    rc = cli.cmd_send(
        settings, _ns(text=None, file=None, thread="48:notes", html=False, image=None, attach=None)
    )
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
    rc = cli.cmd_send(
        settings, _ns(text="positional", file=str(msg), thread="48:notes", html=True, image=None, attach=None)
    )
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


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("https://teams.cloud.microsoft/l/message/19:a@thread.v2/42?context=x", ("19:a@thread.v2", "42")),
        ("42", ("OTHER", "42")),
    ],
)
def test_cmd_react_resolves_target(settings: Settings, monkeypatch, target: str, expected) -> None:
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: ({}, "sk"))
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        "miniteams.send.react",
        lambda s, tok, thread, mid, key, *, remove: seen.update(where=(thread, mid), key=key, remove=remove),
    )
    rc = cli.cmd_react(settings, _ns(target=target, key="heart", remove=True, thread="OTHER"))
    assert rc == 0
    assert seen == {"where": expected, "key": "heart", "remove": True}


def test_react_parser_defaults(monkeypatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "cmd_react", lambda s, a: seen.update(vars(a)) or 0)
    assert cli.main(["react", "42", "like"]) == 0
    assert (seen["target"], seen["key"], seen["remove"], seen["thread"]) == ("42", "like", False, "48:notes")


@pytest.mark.parametrize("key", ["", " ", "like ", "a b", "like\t"])
def test_react_rejects_blank_key(key: str, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "cmd_react", lambda s, a: pytest.fail("reached the Teams call"))
    with pytest.raises(SystemExit):
        cli.main(["react", "42", key])
    assert "without spaces" in capsys.readouterr().err


# Real keys seen in reactions: skin tone, mixed case, custom emoji.
@pytest.mark.parametrize(
    "key", ["yes-tone1", "starMSER", "ah-denis;0-frc-d4-2de7f26db42058a360d77508b203b9fc"]
)
def test_react_accepts_real_keys(key: str, monkeypatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "cmd_react", lambda s, a: seen.update(vars(a)) or 0)
    assert cli.main(["react", "42", key]) == 0
    assert seen["key"] == key


def _typing_clock(monkeypatch) -> tuple[list[float], list[str]]:
    """Fake clock + mocked send: (send times, threads) for a `cmd_typing` run."""
    import time

    now = [0.0]
    sent_at: list[float] = []
    threads: list[str] = []
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: ({}, "sk"))
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr(
        "miniteams.send.send_typing",
        lambda s, tok, thread: (sent_at.append(now[0]), threads.append(thread)),
    )
    return sent_at, threads


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (0, [0.0]),
        (5, [0.0]),  # the first send already covers one resend interval
        (6, [0.0, 5.0]),
        (20, [0.0, 5.0, 10.0, 15.0]),
    ],
)
def test_cmd_typing_resends_until_the_duration_is_covered(
    settings: Settings, monkeypatch, duration: int, expected: list[float]
) -> None:
    sent_at, threads = _typing_clock(monkeypatch)
    assert cli.cmd_typing(settings, _ns(thread="19:a@thread.v2", duration=duration)) == 0
    assert sent_at == expected
    assert set(threads) == {"19:a@thread.v2"}


def test_cmd_typing_stops_on_ctrl_c(settings: Settings, monkeypatch) -> None:
    import time

    sent_at, _ = _typing_clock(monkeypatch)

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", interrupt)
    assert cli.cmd_typing(settings, _ns(thread="48:notes", duration=60)) == 0
    assert sent_at == [0.0]


def test_typing_parser_defaults(monkeypatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "cmd_typing", lambda s, a: seen.update(vars(a)) or 0)
    assert cli.main(["typing"]) == 0
    assert (seen["thread"], seen["duration"]) == ("48:notes", 0)
    assert cli.main(["typing", "--thread", "19:a@thread.v2", "--for", "30"]) == 0
    assert (seen["thread"], seen["duration"]) == ("19:a@thread.v2", 30)


def test_typing_rejects_negative_duration(monkeypatch) -> None:
    monkeypatch.setattr(cli, "cmd_typing", lambda s, a: pytest.fail("reached the Teams call"))
    with pytest.raises(SystemExit):
        cli.main(["typing", "--for", "-1"])


def test_cmd_emojis_without_csa_token_returns_1(settings: Settings, monkeypatch) -> None:
    from miniteams import auth, emojis

    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: ({"access_token": "a"}, "sk"))
    monkeypatch.setattr(auth, "token_source", lambda s: argparse.Namespace(csa_token=lambda: None))
    monkeypatch.setattr(emojis, "list_emojis", lambda *a, **k: pytest.fail("listed without a token"))
    assert cli.cmd_emojis(settings, _ns(jsonl=False, download=False)) == 1


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
    monkeypatch.setattr(auth, "_app", lambda *a: None)  # msal discovers its authority online

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
    monkeypatch.setattr(auth, "_app", lambda *a: None)  # msal discovers its authority online
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
    # None, not a number: `web` picks 400 with an archive to seed from, 250 without.
    assert seen["limit"] is None and seen["bind"] is None and seen["reactions"] is False
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


def test_web_exits_1_on_a_server_error(monkeypatch) -> None:
    """024-AC9: the RuntimeError a taken web.json bind raises ends `web` with rc 1 and `command_failed`."""
    from miniteams import web

    monkeypatch.setattr(cli, "_ensure_skype_token", lambda settings: ({"access_token": "a"}, "skype"))
    error = "cannot listen on 127.0.0.33:4000 (from /cfg/web.json): in use"

    async def fail(*_: Any, **__: Any) -> None:
        raise RuntimeError(error)

    monkeypatch.setattr(web, "run", fail)
    with structlog.testing.capture_logs() as logs:
        assert cli.main(["web"]) == 1
    assert [e["error"] for e in logs if e["event"] == "command_failed"] == [error]


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


# What `archive --live` does with each `archive` flag: honoured, or refused with its sample value.
_LIVE_HONOURED = {
    "--help",
    "--live",
    "--data-dir",
    "--all",
    "--no-media",
    "--no-avatars",
    "--videos",
    "--media",
    "--verify-media",
}
_LIVE_REFUSED = {
    "--loop": [],
    "--thread": ["19:x"],
    "--assets-only": [],
    "--retry-denied": [],
    "--retry-assets": [],
    "--recheck-since": ["2026-09-22"],
}


def test_every_archive_flag_is_honoured_or_refused_by_live(monkeypatch) -> None:
    """A new `archive` flag must be wired into `--live` or refused there, never silently ignored."""
    flags: set[str] = set()

    def record(parser: argparse.ArgumentParser, file: Any = None) -> None:
        flags.update(s for action in parser._actions for s in action.option_strings)

    monkeypatch.setattr(argparse.ArgumentParser, "print_help", record)
    with pytest.raises(SystemExit):
        cli.main(["archive", "--help"])
    assert flags == _LIVE_HONOURED | set(_LIVE_REFUSED) | {"-h"}


def test_archive_live_sweep_refuses_no_media(capsys) -> None:
    for argv in (
        ["--live", "--verify-media", "--no-media"],
        ["--no-media", "--verify-media", "--live", "60"],
    ):
        with pytest.raises(SystemExit) as exc:
            cli.main(["archive", *argv])
        assert exc.value.code == 2
    assert capsys.readouterr().err.count("--verify-media with --live sweeps media") == 2


@pytest.mark.parametrize("flag", sorted(_LIVE_REFUSED))
def test_archive_live_refuses_contradicting_flags(flag: str, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["archive", "--live", flag, *_LIVE_REFUSED[flag]])
    assert exc.value.code == 2 and f"--live cannot be combined with {flag}" in capsys.readouterr().err


def test_archive_live_exits_1_when_the_refresh_token_dies(monkeypatch) -> None:
    from miniteams import archive_live, auth

    calls: list[dict[str, object]] = []

    async def fake_run_live(settings, data_dir, **kw):  # noqa: ANN001
        calls.append(kw)
        return True

    monkeypatch.setattr(archive_live, "run_live", fake_run_live)
    monkeypatch.setattr(auth.TokenSource, "acquire", lambda self: {"access_token": "x"})
    monkeypatch.setattr(auth, "_app", lambda *a: None)  # msal discovers its authority online
    assert cli.main(["archive", "--live", "--no-media"]) == 1
    assert calls and calls[0]["download_media"] is False
    assert calls[0]["reconcile"] == 1800
    assert cli.main(["archive", "--live", "60"]) == 1
    assert calls[1]["reconcile"] == 60 and calls[1]["verify_media"] is False
    assert cli.main(["archive", "--live", "--verify-media", "--videos", "--all", "--no-avatars"]) == 1
    assert calls[2]["verify_media"] is True and "videos" in calls[2]["media_types"]
    assert calls[2]["include_all"] is True and calls[2]["download_avatars"] is False


def test_recheck_since_is_one_shot_and_validated(capsys) -> None:
    for argv in (
        ["--recheck-since", "2026-09-22", "--loop"],
        ["--recheck-since", "yesterday"],
        ["--recheck-since", "2026-09-22", "--assets-only"],
    ):
        with pytest.raises(SystemExit) as exc:
            cli.main(["archive", *argv])
        assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "cannot be combined with --loop" in err and "invalid" in err
    assert "cannot be combined with --assets-only" in err


def test_recheck_since_reaches_the_run_as_a_utc_floor(monkeypatch) -> None:
    from miniteams import archive, auth

    seen: dict[str, object] = {}

    async def fake_run_archive(settings, data_dir, **kw):  # noqa: ANN001
        seen.update(kw)
        return False

    monkeypatch.setattr(archive, "run_archive", fake_run_archive)
    monkeypatch.setattr(auth.TokenSource, "acquire", lambda self: {"access_token": "x"})
    monkeypatch.setattr(auth, "_app", lambda *a: None)  # msal discovers its authority online
    assert cli.main(["archive", "--recheck-since", "2026-09-22T02:00:00+02:00"]) == 0
    assert seen["recheck_since"] == "2026-09-22T00:00:00"


@pytest.mark.parametrize(
    ("env", "flags", "device_code"),
    [
        (None, [], True),
        (None, ["--device-code"], True),
        (None, ["--browser-login"], False),
        ("false", [], False),
        ("false", ["--device-code"], True),
    ],
)
def test_login_mode_flags(
    monkeypatch, tmp_path, env: str | None, flags: list[str], device_code: bool
) -> None:
    monkeypatch.chdir(tmp_path)  # Settings reads ./.env: a developer's own must not decide the default
    monkeypatch.setenv("MINITEAMS_TENANT_ID", "tenant-123")
    if env is None:
        monkeypatch.delenv("MINITEAMS_USE_DEVICE_CODE", raising=False)
    else:
        monkeypatch.setenv("MINITEAMS_USE_DEVICE_CODE", env)
    seen: list[bool] = []
    monkeypatch.setattr(cli, "cmd_login", lambda s, a: seen.append(s.use_device_code) or 0)
    assert cli.main([*flags, "login"]) == 0
    assert seen == [device_code]


def test_login_mode_flags_exclude_each_other() -> None:
    with pytest.raises(SystemExit):
        cli.main(["--device-code", "--browser-login", "login"])


@pytest.mark.parametrize("argv", [["--browser", "login"], ["--device", "login"], ["chats", "--al"]])
def test_abbreviated_options_are_refused(monkeypatch, argv: list[str]) -> None:
    monkeypatch.setattr(cli, "cmd_login", lambda s, a: pytest.fail("ran on an abbreviation"))
    monkeypatch.setattr(cli, "cmd_chats", lambda s, a: pytest.fail("ran on an abbreviation"))
    with pytest.raises(SystemExit):
        cli.main(argv)


# --- send: composer rendering and @mentions (spec 029) ---

GROUP = "19:grp@thread.v2"
ME = "8:orgid:me"
ANN, BOB = "8:orgid:ann", "8:orgid:bob"
ROSTER = [
    {"mri": ME, "name": "Me Self"},
    {"mri": ANN, "name": "Ann Lee"},
    {"mri": BOB, "name": "Bob Roe"},
    {"mri": "28:bot", "name": "Bot"},
]


def _span(n: int, label: str) -> str:
    return f'<span itemtype="http://schema.skype.com/Mention" itemscope="" itemid="{n}">{label}</span>'


def _entries(props: dict[str, Any] | None) -> list[tuple[Any, ...]]:
    import json

    assert props is not None
    return [(e["itemid"], e["mri"], e["displayName"]) for e in json.loads(props["mentions"])]


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Signed in as ME; the roster, posts, image and file uploads recorded instead of sent."""
    from types import SimpleNamespace

    from miniteams import files, send
    from miniteams.directory import Directory

    seen: dict[str, Any] = {"roster": [], "posts": [], "uploads": [], "members": ROSTER, "bearers": []}
    seen["aad"] = {"access_token": "aad", "id_token": "idt"}
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: (seen["aad"], "sk"))
    real_set_token = Directory.set_token

    def set_token(self: Any, skype_token: str, bearer: str = "") -> None:
        seen["bearers"].append(bearer)
        real_set_token(self, skype_token, bearer)

    monkeypatch.setattr("miniteams.auth.self_mri", lambda token: ME)

    async def thread(self: Any, thread_id: str, max_age: float | None = None) -> dict[str, Any] | None:
        seen["roster"].append(thread_id)
        members = seen["members"]
        return None if members is None else {"topic": None, "members": members}

    def post(settings: Any, token: str, thread: str, text: str, name: str, **kw: Any) -> dict[str, Any]:
        seen["posts"].append((thread, text, kw.get("is_html"), kw.get("properties")))
        return {"clientmessageid": "1", "status": 201}

    def upload_image(*a: Any) -> str:
        seen["uploads"].append("image")
        return "0-weu-d1-" + "a" * 32

    def upload_files(*a: Any) -> tuple[list[Any], list[Any]]:
        seen["uploads"].append("file")
        return [], []

    def post_files(settings: Any, token: str, thread: str, text: str, name: str, **kw: Any) -> None:
        seen["posts"].append((thread, text, kw.get("is_html"), kw.get("properties")))

    monkeypatch.setattr(Directory, "thread", thread)
    monkeypatch.setattr(Directory, "set_token", set_token)
    monkeypatch.setattr(send, "send_message", post)
    monkeypatch.setattr(send, "upload_image", upload_image)
    monkeypatch.setattr(files, "onedrive", lambda *a: seen["uploads"].append("drive"))
    monkeypatch.setattr(files, "upload_files", upload_files)
    monkeypatch.setattr(files, "post_files", post_files)
    monkeypatch.setattr(
        "miniteams.auth.token_source",
        lambda s: SimpleNamespace(sharepoint_token=lambda h: "sp", display_name=lambda: ""),
    )
    return seen


def _send_ns(text: str | None, **kw: Any) -> argparse.Namespace:
    return _ns(
        **{"text": text, "file": None, "thread": GROUP, "html": False, "image": None, "attach": None, **kw}
    )


@pytest.mark.parametrize(
    ("text", "html", "entries"),
    [
        ("@Ann Lee hi", f"{_span(0, 'Ann Lee')} hi", [(0, ANN, "Ann Lee")]),
        (
            "@bob and **@ann**",
            f"{_span(0, 'Bob')} and <b>{_span(1, 'Ann')}</b>",
            [(0, BOB, "Bob"), (1, ANN, "Ann")],
        ),
    ],
)
def test_send_renders_and_mentions(
    settings: Settings, wire: dict[str, Any], text: str, html: str, entries: list[tuple[Any, ...]]
) -> None:
    """029-AC10: `miniteams send --thread X "@Ann Lee hi"` posts the span and `properties.mentions`."""
    assert cli.cmd_send(settings, _send_ns(text)) == 0
    ((thread, body, is_html, props),) = wire["posts"]
    assert (thread, body, is_html, wire["roster"]) == (GROUP, html, True, [GROUP])
    assert _entries(props) == entries


@pytest.mark.parametrize(
    ("text", "html", "entries"),
    [
        ("@Ann Lee look", f"<p>{_span(0, 'Ann Lee')} look</p>", [(0, ANN, "Ann Lee")]),
        ("**@bob** ok", f"<p><b>{_span(0, 'Bob')}</b> ok</p>", [(0, BOB, "Bob")]),
        (
            "- @bob\n- @ann",
            f"<ul><li>{_span(0, 'Bob')}</li><li>{_span(1, 'Ann')}</li></ul>",
            [(0, BOB, "Bob"), (1, ANN, "Ann")],
        ),
    ],
)
def test_send_with_an_image_carries_the_mention(
    settings: Settings, wire: dict[str, Any], tmp_path, text: str, html: str, entries: list[tuple[Any, ...]]
) -> None:
    """029-AC10: with `--image` the text part carries the span, inline text in its own paragraph, and the
    properties."""
    from test_images import png

    image = tmp_path / "a.png"
    image.write_bytes(png())
    assert cli.cmd_send(settings, _send_ns(text, image=[str(image)])) == 0
    ((_, body, is_html, props),) = wire["posts"]
    assert body.startswith(f"{html}<p><img ") and is_html
    assert _entries(props) == entries and wire["uploads"] == ["image"]


@pytest.mark.parametrize(
    ("text", "html", "entries"),
    [
        ("@bob see :fire:", f"{_span(0, 'Bob')} see 🔥", [(0, BOB, "Bob")]),
        ("> @Ann Lee", f"<blockquote>{_span(0, 'Ann Lee')}</blockquote>", [(0, ANN, "Ann Lee")]),
    ],
)
def test_send_with_an_attachment_carries_the_mention(
    settings: Settings, wire: dict[str, Any], tmp_path, text: str, html: str, entries: list[tuple[Any, ...]]
) -> None:
    """029-AC10: with `--attach` the text part carries the span and the properties."""
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"%PDF")
    assert cli.cmd_send(settings, _send_ns(text, attach=[str(doc)])) == 0
    ((thread, body, is_html, props),) = wire["posts"]
    assert (thread, body, is_html) == (GROUP, html, True) and _entries(props) == entries


@pytest.mark.parametrize("text", ["@Ann Lee <b>x</b>", "**@bob** \\@x"])
def test_send_html_is_verbatim_with_no_roster_call(
    settings: Settings, wire: dict[str, Any], text: str
) -> None:
    """029-AC10: `--html` sends the text as typed: no rendering, no resolution, no roster call."""
    assert cli.cmd_send(settings, _send_ns(text, html=True)) == 0
    assert wire["posts"] == [(GROUP, text, True, None)] and wire["roster"] == []


@pytest.mark.parametrize(
    ("text", "thread", "members", "reason"),
    [
        ("@Nobody hi", GROUP, ROSTER, "unknown mention @Nobody (\\@ for a literal @)"),
        (
            "@Ann hi",
            GROUP,
            [*ROSTER, {"mri": "8:orgid:moore", "name": "Ann Moore"}],
            "ambiguous mention @Ann: Ann Lee, Ann Moore (\\@ for a literal @)",
        ),
        ("@Ann hi", GROUP, None, "chat members unavailable"),
        ("@everyone hi", "19:a_b@unq.gbl.spaces", ROSTER, "@everyone is not available in this chat"),
        ("@Ann hi", "48:notes", ROSTER, "unknown mention @Ann (\\@ for a literal @)"),
    ],
)
@pytest.mark.parametrize("extra", ["none", "image", "attach"])
def test_a_refused_send_exits_2_before_any_upload_or_write(
    settings: Settings,
    wire: dict[str, Any],
    tmp_path,
    text: str,
    thread: str,
    members: list[dict[str, str]] | None,
    reason: str,
    extra: str,
) -> None:
    """029-AC10: a refusal is logged and exits 2; nothing uploaded or posted, whatever is attached."""
    from test_images import png

    wire["members"] = members
    path = tmp_path / ("a.png" if extra == "image" else "a.pdf")
    path.write_bytes(png())
    kw = {} if extra == "none" else {extra: [str(path)]}
    with structlog.testing.capture_logs() as logs:
        assert cli.cmd_send(settings, _send_ns(text, thread=thread, **kw)) == 2
    assert [(e["event"], e.get("reason")) for e in logs] == [("send_refused", reason)]
    assert wire["posts"] == [] and wire["uploads"] == []
    assert wire["roster"] == ([] if thread == "48:notes" else [thread])


@pytest.mark.parametrize(
    ("text", "body"),
    [("mail a@b.com", "mail a@b.com"), ("`@Ann` x", "<code>@Ann</code> x"), ("\\@Ann Lee", "@Ann Lee")],
)
def test_no_candidate_mention_asks_no_roster(
    settings: Settings, wire: dict[str, Any], text: str, body: str
) -> None:
    """029-AC10: no `@Name` to resolve: no roster call, no properties, `\\@` a literal `@`."""
    assert cli.cmd_send(settings, _send_ns(text)) == 0
    assert wire["posts"] == [(GROUP, body, True, None)] and wire["roster"] == []


@pytest.mark.parametrize("text", ["https://x.test/" + "a" * 14_400, "<" * 7_200])
def test_a_text_without_mentions_is_refused_before_auth(settings: Settings, monkeypatch, text: str) -> None:
    """029-AC10: a rendered text past 28 KB of HTML is refused before any login: no `@Name` to resolve."""
    monkeypatch.setattr(cli, "_ensure_skype_token", lambda s: pytest.fail("auth before validation"))
    with structlog.testing.capture_logs() as logs:
        assert cli.cmd_send(settings, _send_ns(text)) == 2
    assert [e["event"] for e in logs] == ["send_refused"]


@pytest.mark.parametrize(
    ("aad", "bearer"),
    [({"access_token": "aad", "id_token": "idt"}, "aad"), ({"access_token": "aad"}, "aad")],
    ids=["id-token", "access-token-only"],
)
def test_the_roster_lookup_gets_the_bearer_every_directory_gets(
    settings: Settings, wire: dict[str, Any], aad: dict[str, str], bearer: str
) -> None:
    """029-AC10: the access token, never the id_token beside it, as every Directory gets."""
    wire["aad"] = aad
    assert cli.cmd_send(settings, _send_ns("@Ann Lee hi")) == 0
    assert wire["bearers"] == [bearer] and wire["roster"] == [GROUP]


# --- send: name forms and free labels (spec 033) ---


@pytest.mark.parametrize(
    ("text", "members", "html", "entries"),
    [
        ("@lee hi", ROSTER, f"{_span(0, 'Lee')} hi", [(0, ANN, "Lee")]),
        ("@Roe hi", ROSTER, f"{_span(0, 'Roe')} hi", [(0, BOB, "Roe")]),
        (
            "@{Ann Lee|Lee} hi",
            [*ROSTER, {"mri": "8:orgid:boblee", "name": "Bob Lee"}],
            f"{_span(0, 'Lee')} hi",
            [(0, ANN, "Lee")],
        ),
        ("@{ann|the boss} hi", ROSTER, f"{_span(0, 'the boss')} hi", [(0, ANN, "the boss")]),
        ("@{ann|} hi", ROSTER, f"{_span(0, 'Ann Lee')} hi", [(0, ANN, "Ann Lee")]),
        (
            "@{John Doe | Sales|boss} hi",
            [*ROSTER, {"mri": "8:orgid:john", "name": "John Doe | Sales"}],
            f"{_span(0, 'boss')} hi",
            [(0, "8:orgid:john", "boss")],
        ),
    ],
)
def test_send_takes_every_name_form_and_free_labels(
    settings: Settings,
    wire: dict[str, Any],
    text: str,
    members: list[dict[str, str]],
    html: str,
    entries: list[tuple[Any, ...]],
) -> None:
    """033-AC4 (AC1–AC3): a last name, or `@{Who|label}`, mentions its member as MCP does."""
    wire["members"] = members
    assert cli.cmd_send(settings, _send_ns(text)) == 0
    ((_, body, _, props),) = wire["posts"]
    assert body == html and _entries(props) == entries


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("@Lee hi", "ambiguous mention @Lee: Ann Lee, Bob Lee (\\@ for a literal @)"),
        (
            "@{Ann Lee|a\tb} hi",
            f"mention label refused @{{Ann Lee|a\tb}}: {compose._LABEL_RULE} (\\@ for a literal @)",
        ),
        ("@{Nobody|x} hi", "unknown mention @{Nobody|x} (\\@ for a literal @)"),
    ],
)
@pytest.mark.parametrize("extra", ["none", "image"])
def test_a_refused_form_exits_2_before_any_write(
    settings: Settings, wire: dict[str, Any], tmp_path, text: str, reason: str, extra: str
) -> None:
    """033-AC4: an ambiguous form or a malformed free label is logged and exits 2; nothing uploaded or
    posted."""
    from test_images import png

    wire["members"] = [*ROSTER, {"mri": "8:orgid:boblee", "name": "Bob Lee"}]
    path = tmp_path / "a.png"
    path.write_bytes(png())
    kw = {} if extra == "none" else {extra: [str(path)]}
    with structlog.testing.capture_logs() as logs:
        assert cli.cmd_send(settings, _send_ns(text, **kw)) == 2
    assert [(e["event"], e.get("reason")) for e in logs] == [("send_refused", reason)]
    assert wire["posts"] == [] and wire["uploads"] == []


def test_an_escaped_free_label_is_sent_as_typed(settings: Settings, wire: dict[str, Any]) -> None:
    """033-AC4 (AC3): `\\@{Ann Lee|x}` is literal: no roster call, no properties."""
    assert cli.cmd_send(settings, _send_ns(r"\@{Ann Lee|x}")) == 0
    assert wire["posts"] == [(GROUP, "@{Ann Lee|x}", True, None)] and wire["roster"] == []
