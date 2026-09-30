"""AAD token acquisition via MSAL (FOCI family-token flow).

The Teams first-party client (`client_id`) has no `http://localhost` redirect, so MSAL's loopback
interactive flow is rejected (AADSTS50011). To keep the browser flow, the **interactive** leg runs
on a localhost-registered FOCI public client (`auth_client_id`, Azure CLI) requesting a scope it is
allowed (`seed_scope`, Graph); that seeds a *family* refresh token into the shared cache. A silent
call on the Teams client then redeems it for the Teams scope (FOCI). Device-code needs no redirect,
so it runs on the Teams client directly. Tokens are cached on disk; re-runs are silent.

`TokenSource` holds one cache/app for a process's lifetime: `acquire()` runs the full flow once
(may prompt), `refresh()` is silent-only for a reconnect loop — it never prompts, raising
`AuthExpired` when the refresh token is dead so the caller can stop instead of re-prompting on
every reconnect. The disk cache is shared with every other miniteams process, and AAD revokes a
refresh token the moment one of them redeems it, so the file is re-read whenever it changes.
"""

import atexit
import os
import sys
import threading
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
        # Security: the cache holds refresh tokens — created owner-only, before any byte is written.
        # Per-writer temp + os.replace: the refresher thread and the session loop may persist at once.
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
                fh.write(cache.serialize())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)  # may hold a partial refresh-token payload
            raise


_NO_FILE = (0, 0, 0)


def _stamp(path: Path) -> tuple[int, int, int]:
    """Identity of the cache file's current content.

    Inode first: `_persist_cache` swaps the file in with os.replace, so a writer always lands on a
    new inode — a timestamp alone would miss a write sharing our clock tick, and never reload again.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return _NO_FILE
    return (st.st_ino, st.st_mtime_ns, st.st_size)


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


class AuthUnavailable(RuntimeError):
    """Silent refresh failed on AAD's side — the credential may still be good, so retry."""


# Everything else AAD can answer (temporarily_unavailable, server_error, request_throttled…)
# heals by waiting; only these mean the cached credential itself is gone.
_DEAD_CREDENTIAL = frozenset(
    {"invalid_grant", "interaction_required", "invalid_client", "unauthorized_client"}
)


class TokenSource:
    """One MSAL cache/app for the process lifetime; see module docstring for acquire vs refresh."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache, self._cache_path = _build_cache(settings)
        self._cache_stamp = _stamp(self._cache_path)
        # `stream` refreshes from two threads (session loop + refresher task via to_thread).
        # Reload → mint → persist has to be atomic per process: otherwise one thread reloads the
        # file over the token the other just rotated in memory, restoring the revoked one.
        # Re-entrant: refresh() holds it across _silent() and _persist().
        self._lock = threading.RLock()
        self._teams = _app(settings.client_id, settings, self._cache)

    def _persist(self) -> None:
        with self._lock:
            wrote = self._cache.has_state_changed
            _persist_cache(self._cache, self._cache_path)
            # Nothing written means the file is still whoever else's: claiming its stamp would mark
            # a sibling's rotated token as already loaded, and we would never read it.
            if wrote:
                self._cache_stamp = _stamp(self._cache_path)

    def _sync_cache(self) -> None:
        """Re-read the cache file when another miniteams process has written it.

        AAD rotates the refresh token on every redemption, so a sibling process (`web`, `stream`,
        a second `archive`) that refreshes leaves our in-memory copy holding a revoked token. That
        surfaces hours later as a fatal AuthExpired mid-run, while a fresh process succeeds at once
        because it reads the rotated token from disk. Re-reading first keeps us on the live token.
        """
        # ponytail: last-writer-wins, no file lock — two processes minting in the same instant can
        # still lose one update; msal_extensions' PersistedTokenCache if that ever bites.
        with self._lock:
            # deserialize() replaces the cache wholesale: credentials msal just put in memory (the
            # interactive leg's seed) are not on disk yet, and reloading over them loses the login.
            if self._cache.has_state_changed:
                return
            stamp = _stamp(self._cache_path)
            if stamp == _NO_FILE or stamp == self._cache_stamp:
                return
            try:
                doc = self._cache_path.read_text()
            except OSError:
                return  # deleted under us since _stamp(): keep the copy we hold
            self._cache.deserialize(doc)
            self._cache_stamp = stamp

    def _silent(self) -> dict[str, Any] | None:
        with self._lock:
            self._sync_cache()
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

        self._persist()
        if not _has_token(result):
            err = result or {}
            raise RuntimeError(
                f"AAD auth failed (scope {scopes}): {err.get('error')}: {err.get('error_description')}"
            )
        return result

    def refresh(self) -> dict[str, Any]:
        """Silent-only refresh for a reconnect loop; never prompts. Raises AuthExpired if dead."""
        with self._lock:
            result = self._silent()
            self._persist()
            if not _has_token(result):
                err = result or {}
                code = str(err.get("error") or "")
                # No account at all (result is None) is a dead cache too, not an AAD hiccup.
                if result is None or code in _DEAD_CREDENTIAL:
                    raise AuthExpired(
                        f"silent token refresh failed ({code or 'no cached account'}) — "
                        "refresh token expired or revoked"
                    )
                raise AuthUnavailable(f"silent token refresh failed ({code}) — AAD-side, retryable")
            return result

    def sharepoint_token(self, host: str) -> str | None:
        """Silent SharePoint token for `host` (e.g. `contoso-my.sharepoint.com`), redeemed on the
        Teams client via FOCI — no extra consent. Used to fetch meeting transcripts/recordings
        stored on OneDrive/SharePoint. Returns None if unavailable (never prompts)."""
        return self._silent_scope(f"https://{host}/.default")

    def graph_token(self) -> str | None:
        """Silent Microsoft Graph token (FOCI, no consent). Used for the /shares API to fetch
        SharePoint-hosted files shared into chats. Returns None if unavailable (never prompts)."""
        return self._silent_scope("https://graph.microsoft.com/.default")

    def csa_token(self) -> str | None:
        """Silent chat-service aggregator token (FOCI, no consent), e.g. for custom emojis.
        Returns None if unavailable (never prompts)."""
        return self._silent_scope("https://chatsvcagg.teams.microsoft.com/.default")

    def _silent_scope(self, scope: str) -> str | None:
        with self._lock:
            self._sync_cache()
            accounts = self._teams.get_accounts()
            if not accounts:
                return None
            result = self._teams.acquire_token_silent([scope], account=accounts[0])
            self._persist()
            return result.get("access_token") if result else None


_SOURCES: dict[Path, TokenSource] = {}
_SOURCES_LOCK = threading.Lock()


def token_source(settings: Settings) -> TokenSource:
    """The process's TokenSource for this cache dir.

    Two instances would each register an atexit writer, and atexit runs them last-registered-first:
    the older cache lands last and puts a token AAD already revoked back on disk.
    """
    key = settings.config_dir.resolve()
    with _SOURCES_LOCK:
        source = _SOURCES.get(key)
        if source is None:
            source = _SOURCES[key] = TokenSource(settings)
        return source


def acquire_aad_token(settings: Settings) -> dict[str, Any]:
    """One-shot acquisition for non-streaming commands (dump/send)."""
    return token_source(settings).acquire()
