"""TokenSource flow + device-code URL injection (no real MSAL/network)."""

import pytest

from miniteams import auth as A
from miniteams.config import Settings


class _FakeApp:
    """Minimal msal.PublicClientApplication stand-in driven by the scripted attrs below."""

    def __init__(self, accounts: list | None = None, silent: dict | None = None) -> None:
        self._accounts = accounts or []
        self._silent = silent

    def get_accounts(self) -> list:
        return self._accounts

    def acquire_token_silent_with_error(self, scopes, account) -> dict | None:
        return self._silent


def test_refresh_raises_auth_expired_when_no_account(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(A, "_app", lambda *a, **k: _FakeApp(accounts=[]))
    src = A.TokenSource(settings)
    with pytest.raises(A.AuthExpired):
        src.refresh()


def test_refresh_returns_token_when_silent_succeeds(settings: Settings, monkeypatch) -> None:
    app = _FakeApp(accounts=[{"username": "u"}], silent={"access_token": "tok"})
    monkeypatch.setattr(A, "_app", lambda *a, **k: app)
    src = A.TokenSource(settings)
    assert src.refresh()["access_token"] == "tok"


def test_interactive_foci_swap_refusal_falls_back_to_device_code(
    settings: Settings, monkeypatch, capsys
) -> None:
    class _TeamsApp(_FakeApp):
        def __init__(self) -> None:
            # Account exists but silent FOCI swap is refused (e.g. CA policy).
            super().__init__(accounts=[{"username": "u"}], silent={"error": "interaction_required"})

        def initiate_device_flow(self, scopes) -> dict:
            return {"user_code": "X", "verification_uri": "https://microsoft.com/devicelogin"}

        def acquire_token_by_device_flow(self, flow) -> dict:
            return {"access_token": "devicetok"}

    class _AuthApp:
        def acquire_token_interactive(self, scopes, prompt) -> dict:
            return {"access_token": "seedtok"}

    def fake_app(client_id, s, cache):
        return _AuthApp() if client_id == s.auth_client_id else _TeamsApp()

    monkeypatch.setattr(A, "_app", fake_app)
    result = A.TokenSource(settings).acquire()
    assert result["access_token"] == "devicetok"
    assert "falling back to device-code" in capsys.readouterr().err


def test_device_code_injects_code_into_url(capsys, monkeypatch) -> None:
    class _DeviceApp:
        def initiate_device_flow(self, scopes) -> dict:
            # No verification_uri_complete → must be synthesized from uri + user_code.
            return {"user_code": "ABCD1234", "verification_uri": "https://microsoft.com/devicelogin"}

        def acquire_token_by_device_flow(self, flow) -> dict:
            return {"access_token": "tok"}

    result = A._device_code_flow(_DeviceApp(), ["scope"])
    assert result["access_token"] == "tok"
    err = capsys.readouterr().err
    assert "https://microsoft.com/devicelogin?otc=ABCD1234" in err


def test_persist_cache_replaces_atomically_owner_only(tmp_path) -> None:
    class _Cache:
        has_state_changed = True

        def serialize(self) -> str:
            return "{}"

    path = tmp_path / "msal_cache.json"
    path.write_text("old")
    path.chmod(0o644)
    inode = path.stat().st_ino
    A._persist_cache(_Cache(), path)  # type: ignore[arg-type]
    assert path.read_text() == "{}"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_ino != inode  # swapped in whole, never truncated in place
    assert [p.name for p in tmp_path.iterdir()] == ["msal_cache.json"]  # no temp left behind


def test_persist_cache_failure_leaves_no_temp_file(tmp_path, monkeypatch) -> None:
    class _Broken:
        has_state_changed = True

        def serialize(self) -> str:
            raise OSError(28, "No space left on device")

    class _Cache:
        has_state_changed = True

        def serialize(self) -> str:
            return "{}"

    path = tmp_path / "msal_cache.json"
    path.write_text("old")
    with pytest.raises(OSError):
        A._persist_cache(_Broken(), path)  # type: ignore[arg-type]
    monkeypatch.setattr(A.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError(18, "cross-device")))
    with pytest.raises(OSError):
        A._persist_cache(_Cache(), path)  # type: ignore[arg-type]
    assert [p.name for p in tmp_path.iterdir()] == ["msal_cache.json"]
    assert path.read_text() == "old"  # the live cache is untouched by either failure
