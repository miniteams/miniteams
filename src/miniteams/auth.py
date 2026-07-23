"""AAD token acquisition via MSAL (FOCI family-token flow).

The Teams first-party client (`client_id`) has no `http://localhost` redirect, so MSAL's loopback
interactive flow is rejected (AADSTS50011). To keep the browser flow, the **interactive** leg runs
on a localhost-registered FOCI public client (`auth_client_id`, Azure CLI) requesting a scope it is
allowed (`seed_scope`, Graph); that seeds a *family* refresh token into the shared cache. A silent
call on the Teams client then redeems it for the Teams scope (FOCI). Device-code needs no redirect,
so it runs on the Teams client directly. Tokens are cached on disk; re-runs are silent.

`TokenSource` holds one cache/app for a process's lifetime: `acquire()` runs the full flow once
(may prompt), `refresh()` is silent-only for a reconnect loop — it never prompts, returning None
when the refresh token is dead so the caller can stop instead of re-prompting on every reconnect.
"""

import atexit
import sys
from pathlib import Path
from typing import Any, TypeGuard

import msal
import structlog

from .config import Settings

log = structlog.get_logger()


def _load_cache(path: Path) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if path.exists():
        cache.deserialize(path.read_text())
    return cache


def _persist_cache(cache: msal.SerializableTokenCache, path: Path) -> None:
    if cache.has_state_changed:
        path.write_text(cache.serialize())
        path.chmod(0o600)  # Security: token cache holds refresh tokens — owner-only.


def _build_cache(settings: Settings) -> tuple[msal.SerializableTokenCache, Path]:
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    settings.config_dir.chmod(0o700)
    cache_path = settings.config_dir / "msal_cache.json"
    cache = _load_cache(cache_path)
    atexit.register(_persist_cache, cache, cache_path)
    return cache, cache_path


def _app(
    client_id: str, settings: Settings, cache: msal.SerializableTokenCache
) -> msal.PublicClientApplication:
    return msal.PublicClientApplication(client_id, authority=settings.authority, token_cache=cache)


def _device_code_flow(app: msal.PublicClientApplication, scopes: list[str]) -> dict[str, Any]:
    flow = app.initiate_device_flow(scopes=scopes)
    if "user_code" not in flow:
        raise RuntimeError(f"device flow init failed: {flow}")
    # Prefer the complete URL with the code embedded (otc=) so the user just clicks + confirms;
    # synthesize it if AAD didn't return one. stderr, not stdout — stdout must stay a clean JSON
    # stream for `--jsonl | jq`.
    uri = flow.get("verification_uri_complete") or f"{flow['verification_uri']}?otc={flow['user_code']}"
    print(
        f"Device login → {uri}\n(code {flow['user_code']} pre-filled; open it and sign in)",
        file=sys.stderr,
        flush=True,
    )
    return dict(app.acquire_token_by_device_flow(flow))


def _has_token(result: dict[str, Any] | None) -> TypeGuard[dict[str, Any]]:
    return result is not None and "access_token" in result


class AuthExpired(RuntimeError):
    """Silent refresh failed with no cached/refreshable token — re-auth needed, don't retry."""


class TokenSource:
    """One MSAL cache/app for the process lifetime; see module docstring for acquire vs refresh."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache, self._cache_path = _build_cache(settings)
        self._teams = _app(settings.client_id, settings, self._cache)

    def _silent(self) -> dict[str, Any] | None:
        accounts = self._teams.get_accounts()
        if not accounts:
            return None
        # with_error: on failure returns the AAD error dict instead of None, so the user sees
        # the real reason (e.g. interaction_required / CA policy) rather than "None: None".
        result: dict[str, Any] | None = self._teams.acquire_token_silent_with_error(
            self.settings.scope_list, account=accounts[0]
        )
        return result

    def acquire(self) -> dict[str, Any]:
        """Full flow, runs once and may prompt: silent → device-code/interactive fallback."""
        scopes = self.settings.scope_list
        result = self._silent()

        if not _has_token(result):
            if self.settings.use_device_code:
                # Device-code needs no redirect → use the Teams client directly for the Teams scope.
                log.info("device_code_login_start")
                result = _device_code_flow(self._teams, scopes)
            else:
                # Seed a FOCI family refresh token via the localhost-registered Azure CLI client,
                # then redeem it for the Teams scope silently on the Teams client.
                log.info("interactive_login_start", auth_client=self.settings.auth_client_id)
                auth_app = _app(self.settings.auth_client_id, self.settings, self._cache)
                seed = auth_app.acquire_token_interactive(
                    self.settings.seed_scope.split(), prompt="select_account"
                )
                if "access_token" not in seed:
                    raise RuntimeError(
                        f"AAD auth failed: {seed.get('error')}: {seed.get('error_description')}"
                    )
                result = self._silent()
                if not _has_token(result):
                    # FOCI swap refused (typically Conditional Access on the Teams client/scope).
                    # Device-code hits the Teams client directly and usually passes — fall back
                    # instead of dying with a cryptic error.
                    err = result or {}
                    log.warning(
                        "foci_swap_refused_falling_back_to_device_code",
                        error=err.get("error"),
                        error_description=err.get("error_description"),
                    )
                    print(
                        "Browser sign-in worked but the Teams token swap was refused; "
                        "falling back to device-code login.",
                        file=sys.stderr,
                        flush=True,
                    )
                    result = _device_code_flow(self._teams, scopes)

        _persist_cache(self._cache, self._cache_path)
        if not _has_token(result):
            err = result or {}
            raise RuntimeError(
                f"AAD auth failed (scope {scopes}): {err.get('error')}: {err.get('error_description')}"
            )
        return result

    def refresh(self) -> dict[str, Any]:
        """Silent-only refresh for a reconnect loop; never prompts. Raises AuthExpired if dead."""
        result = self._silent()
        _persist_cache(self._cache, self._cache_path)
        if not _has_token(result):
            err = result or {}
            raise AuthExpired(
                f"silent token refresh failed ({err.get('error')}) — refresh token expired or revoked"
            )
        return result

    def sharepoint_token(self, host: str) -> str | None:
        """Silent SharePoint token for `host` (e.g. `contoso-my.sharepoint.com`), redeemed on the
        Teams client via FOCI — no extra consent. Used to fetch meeting transcripts/recordings
        stored on OneDrive/SharePoint. Returns None if unavailable (never prompts)."""
        accounts = self._teams.get_accounts()
        if not accounts:
            return None
        result = self._teams.acquire_token_silent([f"https://{host}/.default"], account=accounts[0])
        _persist_cache(self._cache, self._cache_path)
        return result.get("access_token") if result else None


def acquire_aad_token(settings: Settings) -> dict[str, Any]:
    """One-shot acquisition for non-streaming commands (dump/send)."""
    return TokenSource(settings).acquire()
