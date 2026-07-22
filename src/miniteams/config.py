"""Runtime configuration.

Defaults are the purple-teams **work/TFW** constants, captured verbatim from
EionRobb/purple-teams source. They are version-pinned and drift over time — if auth
or handshakes start returning 4xx, re-capture from a live teams.microsoft.com session
and override via the matching MINITEAMS_* env var.
"""

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "miniteams"


def _default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "miniteams"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MINITEAMS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- tenant (required; set in .env) ---
    tenant_id: str

    # --- first-party Teams client (purple-teams TFW: teams_login.c) ---
    client_id: str = "1fec8e78-bce4-4aaf-ab1b-5451cc387264"
    # MSAL reserved scopes (openid/profile/offline_access) are added automatically,
    # so only the resource .default is listed here.
    oauth_scope: str = "https://api.spaces.skype.com/.default"
    # The Teams client has no http://localhost redirect, so MSAL's loopback interactive flow
    # fails (AADSTS50011). Seed the FOCI family refresh token with a localhost-registered public
    # client (Azure CLI), then redeem it for the Teams scope via the Teams client_id (FOCI). The
    # seed scope is one the auth client is allowed (Graph). Device-code uses the Teams client
    # directly (no redirect). All three are FOCI family members.
    auth_client_id: str = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"  # Azure CLI
    seed_scope: str = "https://graph.microsoft.com/.default"

    # --- skype-token exchange (authsvc authz) ---
    authz_url: str = "https://teams.microsoft.com/api/authsvc/v1.0/authz"

    # --- chat-service metadata (libteams.h TEAMS_CONTACTS_HOST, TFW) ---
    contacts_host: str = "apac.ng.msg.teams.microsoft.com"
    # Batched MRI → display-name lookup (teams_contacts.c TEAMS_PROFILES_PREFIX). Bearer id_token.
    profiles_url: str = (
        "https://teams.microsoft.com/api/mt/beta/users/fetchShortProfile"
        "?isMailAddress=false&canBeSmtpAddress=false&enableGuest=true"
        "&includeIBBarredUsers=true&skypeTeamsInfo=true&includeBots=true"
    )

    # --- trouter / registrar (M1+) ---
    trouter_info_url: str = "https://go.trouter.teams.microsoft.com/v4/a"
    # Used only if the info response omits "socketio" (teams_trouter.c TFW fallback).
    trouter_socketio_fallback: str = "https://go.trouter.teams.microsoft.com/"
    registrar_url: str = "https://teams.microsoft.com/registrar/prod/V2/registrations"
    # TeamsCDLWebWorker = the messaging worker; for an MVP it's the only registration needed.
    registrar_app_id: str = "TeamsCDLWebWorker"
    registrar_template_key: str = "TeamsCDLWebWorker_2.1"
    tccv: str = "2024.23.01.2"  # teams_trouter.c TEAMS_TROUTER_TCCV
    clientinfo_version: str = "49/25113001312"  # libteams.h; tc "v" + platformUIVersion
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0 "
        "Teams/24165.1410.2974.6689/49"
    )
    trouter_ttl: int = 86400

    # --- behaviour ---
    use_device_code: bool = False
    download_media: bool = True  # fetch inbound image/file bytes to media_dir
    log_level: str = "INFO"
    config_dir: Path = Field(default_factory=_default_config_dir)  # tokens, epid (state)
    cache_dir: Path = Field(default_factory=_default_cache_dir)  # regenerable downloads

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @property
    def media_dir(self) -> Path:
        # Downloaded media is regenerable cache (XDG_CACHE_HOME), not config/state.
        return self.cache_dir / "media"

    @property
    def scope_list(self) -> list[str]:
        return self.oauth_scope.split()
