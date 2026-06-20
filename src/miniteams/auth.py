"""AAD token acquisition via MSAL.

Browser auth-code+PKCE is the default (matches the official client; survives Conditional
Access). Device-code is an opt-in fallback (`--device-code`) and is frequently CA-blocked in
corporate tenants. Tokens are cached on disk so re-runs are silent until the refresh token dies.
"""

import atexit
import sys
from pathlib import Path
from typing import Any

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


def build_app(
    settings: Settings,
) -> tuple[msal.PublicClientApplication, msal.SerializableTokenCache, Path]:
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    settings.config_dir.chmod(0o700)
    cache_path = settings.config_dir / "msal_cache.json"
    cache = _load_cache(cache_path)
    app = msal.PublicClientApplication(
        settings.client_id,
        authority=settings.authority,
        token_cache=cache,
    )
    # Flush on exit too, so an interrupted run still persists a fresh refresh token.
    atexit.register(_persist_cache, cache, cache_path)
    return app, cache, cache_path


def _device_code_flow(app: msal.PublicClientApplication, scopes: list[str]) -> dict[str, Any]:
    flow = app.initiate_device_flow(scopes=scopes)
    if "user_code" not in flow:
        raise RuntimeError(f"device flow init failed: {flow}")
    # stderr, not stdout — stdout must stay a clean JSON stream for `--jsonl | jq`.
    print(flow["message"], file=sys.stderr, flush=True)
    return dict(app.acquire_token_by_device_flow(flow))


def acquire_aad_token(settings: Settings) -> dict[str, Any]:
    app, cache, cache_path = build_app(settings)
    scopes = settings.scope_list

    result: dict[str, Any] | None = None
    accounts = app.get_accounts()
    if accounts:
        log.info("token_silent_attempt", account=accounts[0].get("username"))
        result = app.acquire_token_silent(scopes, account=accounts[0])

    if not result:
        if settings.use_device_code:
            log.info("device_code_login_start")
            result = _device_code_flow(app, scopes)
        else:
            log.info("interactive_login_start")
            result = app.acquire_token_interactive(scopes, prompt="select_account")

    _persist_cache(cache, cache_path)

    if result is None:
        raise RuntimeError("authentication produced no result")
    if "access_token" not in result:
        raise RuntimeError(f"AAD auth failed: {result.get('error')}: {result.get('error_description')}")
    return result
