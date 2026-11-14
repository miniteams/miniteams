"""Runtime configuration.

Defaults are the purple-teams **work/TFW** constants, captured verbatim from
EionRobb/purple-teams source. They are version-pinned and drift over time — if auth
or handshakes start returning 4xx, re-capture from a live teams.microsoft.com session
and override via the matching MINITEAMS_* env var.
"""

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "miniteams"


def _default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "miniteams"


def write_private(path: Path, text: str) -> None:
    """Replace `path` with `text`, owner-only (0600) from its first byte; the parent is made 0700 if new.

    Per-writer temp + os.replace: a reader never sees a partial file, two writers never share a temp."""
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)  # the umask can only remove bits
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")  # Security: O_EXCL, created 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)  # may hold a partial secret
        raise


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
    # Fallback only: the skype token exchange switches it to the account's region (see skype.py).
    contacts_host: str = "apac.ng.msg.teams.microsoft.com"
    # Chat-service aggregator (custom emojis); same fallback-only rule as contacts_host.
    csa_url: str = "https://teams.microsoft.com/api/csa/emea"
    # Custom emoji image host the composer writes into a message; same fallback-only rule as contacts_host.
    asyncgw_host: str = "fr-prod.asyncgw.teams.microsoft.com"
    # Batched MRI → display-name lookup (teams_contacts.c TEAMS_PROFILES_PREFIX). Bearer access token.
    profiles_url: str = (
        "https://teams.microsoft.com/api/mt/beta/users/fetchShortProfile"
        "?isMailAddress=false&canBeSmtpAddress=false&enableGuest=true"
        "&includeIBBarredUsers=true&skypeTeamsInfo=true&includeBots=true"
    )
    # The user's tenants (AAD bearer); the primary domain names the OneDrive host (files.py).
    tenants_url: str = "https://teams.microsoft.com/api/mt/beta/users/tenants"

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
    use_device_code: bool = True  # False: browser sign-in (`--browser-login`)
    download_media: bool = True  # fetch inbound image/file bytes to media_dir
    # `archive --live --verify-media`: seconds between two chats, so Teams, SharePoint and the NAS breathe.
    sweep_pause: float = 5.0
    log_level: str = "INFO"
    config_dir: Path = Field(default_factory=_default_config_dir)  # tokens, epid (state)
    # The registrar keeps one socket per endpoint id: a test run that shares the real instance's id
    # steals its deliveries. A suffix gives it its own ids while sharing the token cache.
    endpoint_suffix: str = ""
    cache_dir: Path = Field(default_factory=_default_cache_dir)  # regenerable downloads
    # `follow --capture DIR` (spec 044): each follower dumps its call's RTP, Trouter callbacks and SDP
    # under DIR/<call>/ for protocol diagnosis. Off by default: nothing but text is written otherwise.
    follow_capture: Path | None = None
    # The follower's STUN server for its server-reflexive ICE candidate (a third party: it sees this
    # host's address); empty: host candidates only.
    stun_server: str = "stun.l.google.com:19302"

    # --- meeting follower speech to text (spec 044): from grill-room `stt/grill_stt/config.py` @ ae133de,
    # Apache-2.0 ---
    # parakeet: parakeet-ultra through transcribe.cpp. whisper: faster-whisper small.
    stt_engine: Literal["parakeet", "whisper"] = "parakeet"
    # Unset: where scripts/build-transcribe.sh puts them, under cache_dir (see the properties below).
    transcribe_library: Path | None = None
    parakeet_model: Path | None = None
    whisper_model: Path | None = None
    # Engine processes, each with stt_cpu_threads threads: two of 2 threads gave 8.4x real time on a
    # 4-CPU node, one of 4 threads 6.2x (grill-room spike S10d).
    stt_engine_processes: int = Field(default=2, ge=1)
    stt_cpu_threads: int = Field(default=2, ge=1)
    # An engine that has not answered by then is killed and restarted; a 15 s utterance takes ~3 s.
    stt_engine_timeout_s: float = Field(default=60.0, gt=0)
    # Utterances waiting for an engine; past it new ones are dropped and logged.
    stt_queue_size: int = Field(default=64, ge=1)
    # A speaker from whom no frame arrived for this long stopped talking: silence is fed for them. Above
    # delivery jitter, or silence lands inside speech.
    stt_quiet_s: float = Field(default=0.4, gt=0)
    # At the end of a call, the wait for the lines still in the engines; within AC5's 15 s.
    stt_close_timeout_s: float = Field(default=10.0, gt=0)
    # Silence that ends an utterance, the longest one before a forced cut, and how far back from that cap
    # the forced cut looks for the quietest window (0 cuts at the cap). Same defaults as stt.vad.Segmenter,
    # not imported from it: config must load without the transcript extra's numpy.
    vad_min_silence_ms: int = Field(default=800, ge=1)
    vad_max_speech_s: float = Field(default=15.0, gt=0)
    vad_cut_search_s: float = Field(default=3.0, ge=0)

    @field_validator("stun_server")
    @classmethod
    def _stun_host_port(cls, value: str) -> str:
        host, sep, port = value.rpartition(":")
        if value and not (sep and host and port.isdigit() and 0 < int(port) < 65536):
            raise ValueError("stun_server must be empty or host:port")
        return value

    @model_validator(mode="after")
    def _cut_search_within_cap(self) -> Self:
        # A search as long as the cap could cut at the utterance's first window and emit a stub.
        if self.vad_cut_search_s >= self.vad_max_speech_s:
            raise ValueError("vad_cut_search_s must be below vad_max_speech_s")
        return self

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @property
    def media_dir(self) -> Path:
        # Downloaded media is regenerable cache (XDG_CACHE_HOME), not config/state.
        return self.cache_dir / "media"

    @property
    def stt_library(self) -> Path:
        return self.transcribe_library or self.cache_dir / "transcribe" / "lib" / "libtranscribe.so"

    @property
    def stt_model(self) -> Path:
        """The selected engine's model: the parakeet GGUF file or the whisper model directory."""
        if self.stt_engine == "parakeet":
            return self.parakeet_model or self.cache_dir / "models" / "parakeet" / "parakeet-ultra-Q8_0.gguf"
        return self.whisper_model or self.cache_dir / "models" / "whisper-small"

    @property
    def scope_list(self) -> list[str]:
        return self.oauth_scope.split()
