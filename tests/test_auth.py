"""TokenSource flow + device-code URL injection (no real MSAL/network)."""

import json
import threading

import msal
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


def _cache_doc(refresh_token: str) -> str:
    """A minimal msal cache file: one account plus the family refresh token AAD rotates."""
    return json.dumps(
        {
            "Account": {
                "uid.utid-login.windows.net-tenant": {
                    "home_account_id": "uid.utid",
                    "environment": "login.windows.net",
                    "username": "u@example.test",
                    "realm": "tenant",
                }
            },
            "RefreshToken": {
                "uid.utid-login.windows.net-refreshtoken-1fec8e78--": {
                    "credential_type": "RefreshToken",
                    "secret": refresh_token,
                    "home_account_id": "uid.utid",
                    "environment": "login.windows.net",
                    "client_id": "1fec8e78",
                    "family_id": "1",
                }
            },
        }
    )


def _sibling_write(path, refresh_token: str) -> None:
    """Another miniteams process persisting its cache, through the very writer it uses."""
    cache = msal.SerializableTokenCache()
    cache.deserialize(_cache_doc(refresh_token))
    cache.has_state_changed = True
    A._persist_cache(cache, path)


def _rt(src: A.TokenSource) -> str:
    (token,) = src._cache.search("RefreshToken")
    return str(token["secret"])


class _CacheBackedApp(_FakeApp):
    """Reads its accounts out of the shared token cache, the way msal does."""

    def __init__(self, cache: msal.SerializableTokenCache) -> None:
        super().__init__(silent={"access_token": "tok"})
        self._shared = cache

    def get_accounts(self) -> list:
        return list(self._shared.search("Account"))

    def acquire_token_silent(self, scopes, account) -> dict | None:
        return {"access_token": f"tok-{scopes[0]}"}


def test_silent_picks_up_a_refresh_token_rotated_by_another_process(settings: Settings, monkeypatch) -> None:
    # AAD revokes the old refresh token on every redemption, so the sibling's write must win over
    # the copy we loaded at startup — otherwise we redeem a dead token and call it AuthExpired.
    monkeypatch.setattr(A, "_app", lambda cid, s, cache: _CacheBackedApp(cache))
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    path = settings.config_dir / "msal_cache.json"
    _sibling_write(path, "rt-before")
    src = A.TokenSource(settings)
    assert _rt(src) == "rt-before"

    _sibling_write(path, "rt-rotated")
    assert src._silent() == {"access_token": "tok"}
    assert _rt(src) == "rt-rotated"


def test_silent_picks_up_a_cache_that_did_not_exist_at_startup(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(A, "_app", lambda cid, s, cache: _CacheBackedApp(cache))
    src = A.TokenSource(settings)
    assert A._stamp(src._cache_path) == (0, 0, 0)  # nothing on disk yet
    assert src._silent() is None  # no account → nothing to refresh

    _sibling_write(src._cache_path, "rt-first-login")
    assert src._silent() == {"access_token": "tok"}
    assert _rt(src) == "rt-first-login"


def test_own_write_is_not_mistaken_for_a_sibling_write(settings: Settings, monkeypatch) -> None:
    # A reload drops whatever msal put in the cache in-memory, so our own persist must not arm one.
    monkeypatch.setattr(A, "_app", lambda cid, s, cache: _CacheBackedApp(cache))
    src = A.TokenSource(settings)
    src._cache.deserialize(_cache_doc("rt-ours"))
    src._cache.has_state_changed = True
    src._persist()

    src._cache.deserialize(_cache_doc("rt-not-persisted-yet"))
    src._sync_cache()
    assert _rt(src) == "rt-not-persisted-yet"


def test_refresh_never_interleaves_across_threads(settings: Settings, monkeypatch) -> None:
    # `stream` refreshes from its session loop and from its refresher thread. A reload landing
    # between the other thread's mint and its persist would restore the token AAD just revoked.
    trace: list[str] = []

    # The barrier makes the interleaving attempt certain: thread A waits inside the critical
    # section until thread B has reached it, so an unlocked refresh() always trips the assert.
    start = threading.Barrier(2, timeout=5)

    class _SlowApp(_FakeApp):
        def __init__(self) -> None:
            super().__init__(accounts=[{"username": "u"}], silent={"access_token": "tok"})

        def acquire_token_silent_with_error(self, scopes, account) -> dict | None:
            trace.append("enter")
            try:
                start.wait()
            except threading.BrokenBarrierError:
                pass  # the lock held: the other thread never got here, which is the passing case
            trace.append("leave")
            return self._silent

    monkeypatch.setattr(A, "_app", lambda *a, **k: _SlowApp())
    src = A.TokenSource(settings)
    threading.Timer(0.5, start.abort).start()  # nobody is coming: release the first thread
    threads = [threading.Thread(target=src.refresh) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert [t.is_alive() for t in threads] == [False, False]  # no deadlock on the re-entrant lock
    assert trace == ["enter", "leave", "enter", "leave"]


def test_persist_does_not_claim_a_stamp_it_never_read(settings: Settings, monkeypatch) -> None:
    # A sibling rotating the token while our msal call is in flight leaves nothing for us to write;
    # recording its stamp anyway would mark the rotated token as read and pin us to the dead one.
    monkeypatch.setattr(A, "_app", lambda *a, **k: _FakeApp(accounts=[{"username": "u"}]))
    src = A.TokenSource(settings)
    _sibling_write(src._cache_path, "rt-loaded")
    src._sync_cache()

    _sibling_write(src._cache_path, "rt-rotated")
    assert src._cache.has_state_changed is False  # msal touched nothing, so neither do we
    src._persist()

    src._sync_cache()
    assert _rt(src) == "rt-rotated"


def test_interactive_seed_survives_a_sibling_write(settings: Settings, monkeypatch, capsys) -> None:
    # The browser leg seeds the family refresh token in memory only. Reloading the file over it
    # costs the user a second, pointless device-code prompt.
    path = settings.config_dir / "msal_cache.json"

    class _TeamsApp(_FakeApp):
        def __init__(self, cache: msal.SerializableTokenCache) -> None:
            super().__init__(accounts=[{"username": "u"}])
            self._shared = cache

        def acquire_token_silent_with_error(self, scopes, account) -> dict | None:
            seeded = any(c["secret"] == "rt-seed" for c in self._shared.search("RefreshToken"))
            return {"access_token": "teamstok"} if seeded else {"error": "invalid_grant"}

        def initiate_device_flow(self, scopes) -> dict:
            return {"user_code": "X", "verification_uri": "https://microsoft.com/devicelogin"}

        def acquire_token_by_device_flow(self, flow) -> dict:
            return {"access_token": "devicetok"}

    class _AuthApp:
        def __init__(self, cache: msal.SerializableTokenCache) -> None:
            self._shared = cache

        def acquire_token_interactive(self, scopes, prompt) -> dict:
            self._shared.deserialize(_cache_doc("rt-seed"))  # msal: in memory, not yet on disk
            self._shared.has_state_changed = True
            _sibling_write(path, "rt-sibling")  # another process persists mid sign-in
            return {"access_token": "seedtok"}

    def fake_app(client_id, s, cache):
        return _AuthApp(cache) if client_id == s.auth_client_id else _TeamsApp(cache)

    monkeypatch.setattr(A, "_app", fake_app)
    assert A.TokenSource(settings).acquire()["access_token"] == "teamstok"
    assert "falling back to device-code" not in capsys.readouterr().err


def test_deleted_cache_file_leaves_the_in_memory_copy(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(A, "_app", lambda cid, s, cache: _CacheBackedApp(cache))
    src = A.TokenSource(settings)
    _sibling_write(src._cache_path, "rt-live")
    assert src._silent() == {"access_token": "tok"}

    src._cache_path.unlink()  # someone clears the cache to force a re-login
    assert src._silent() == {"access_token": "tok"}
    assert _rt(src) == "rt-live"


def test_graph_token_picks_up_a_cache_written_by_another_process(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(A, "_app", lambda cid, s, cache: _CacheBackedApp(cache))
    src = A.TokenSource(settings)
    assert src.graph_token() is None  # no account yet

    _sibling_write(src._cache_path, "rt-sibling")
    assert src.graph_token() == "tok-https://graph.microsoft.com/.default"
    assert _rt(src) == "rt-sibling"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("invalid_grant", A.AuthExpired),
        ("interaction_required", A.AuthExpired),
        ("invalid_client", A.AuthExpired),
        ("unauthorized_client", A.AuthExpired),
        ("temporarily_unavailable", A.AuthUnavailable),
        ("server_error", A.AuthUnavailable),
        ("request_throttled", A.AuthUnavailable),
        ("", A.AuthUnavailable),
    ],
)
def test_refresh_separates_a_dead_credential_from_an_aad_hiccup(
    settings: Settings, monkeypatch, code: str, expected: type[Exception]
) -> None:
    # AuthExpired stops the run and tells the user to re-login; AuthUnavailable is retried.
    monkeypatch.setattr(
        A, "_app", lambda *a, **k: _FakeApp(accounts=[{"username": "u"}], silent={"error": code})
    )
    with pytest.raises(expected):
        A.TokenSource(settings).refresh()
    assert not issubclass(A.AuthUnavailable, A.AuthExpired)  # the two branches must stay distinct


def test_token_source_is_one_per_cache_dir(settings: Settings, monkeypatch) -> None:
    # Two sources would each register an atexit writer; atexit runs them last-first, so the older
    # cache lands last and puts a revoked refresh token back on disk.
    registered: list = []
    monkeypatch.setattr(A.atexit, "register", lambda fn, *a: registered.append((fn, a)))
    monkeypatch.setattr(A, "_app", lambda *a, **k: _FakeApp(accounts=[{"username": "u"}]))
    first, second = A.token_source(settings), A.token_source(settings)
    assert first is second
    assert len(registered) == 1


def test_sync_survives_the_cache_vanishing_after_it_was_stamped(settings: Settings, monkeypatch) -> None:
    # A sibling can unlink the file between _stamp() and the read; the in-memory copy stands in.
    monkeypatch.setattr(
        A, "_app", lambda *a, **k: _FakeApp(accounts=[{"username": "u"}], silent={"access_token": "tok"})
    )
    src = A.TokenSource(settings)
    _sibling_write(settings.config_dir / "msal_cache.json", "rt-sibling")
    monkeypatch.setattr(A.Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(OSError("vanished")))
    assert src.refresh()["access_token"] == "tok"
