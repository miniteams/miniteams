"""Trouter info call + session handshake.

URL building mirrors purple-teams teams_trouter.c verbatim: the `tc` JSON blob, the hardcoded
`con_num`, per-key connectparams expansion, and the trailing-`&` quirk. Both calls authenticate
with `X-Skypetoken` only (no AAD bearer yet — that arrives on the websocket at M2).
"""

import json
import uuid
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from .config import Settings

log = structlog.get_logger()

# purple-teams hardcodes this (teams_trouter.c; upstream TODO to make it a real counter).
_CON_NUM = "1234567890123_1"


def get_or_create_epid(settings: Settings, name: str = "endpoint_id") -> str:
    """Stable endpoint GUID, reused across the info call and the registrar.

    One file per consumer (`name`): the registrar maps an epid to a single socket, so two
    processes sharing one would silently steal each other's deliveries.
    """
    path = settings.config_dir / name
    if path.exists():
        return path.read_text().strip()
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    epid = str(uuid.uuid4())
    path.write_text(epid)
    path.chmod(0o600)
    return epid


def trouter_info(settings: Settings, skype_token: str, epid: str) -> dict[str, Any]:
    url = f"{settings.trouter_info_url}?epid={quote(epid, safe='')}"
    headers = {"x-skypetoken": skype_token, "Content-Length": "0"}
    resp = httpx.post(url, headers=headers, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    socketio = data.get("socketio") or settings.trouter_socketio_fallback
    info = {
        "socketio": socketio,
        "surl": data["surl"],
        "connectparams": data.get("connectparams", {}),
    }
    log.info(
        "trouter_info",
        socketio=socketio,
        has_surl=bool(info["surl"]),
        connectparams=sorted(info["connectparams"]),
    )
    return info


def _tc_blob(settings: Settings) -> str:
    return json.dumps(
        {"cv": settings.tccv, "ua": "TeamsCDL", "hr": "", "v": settings.clientinfo_version},
        separators=(",", ":"),
    )


def common_query(settings: Settings, connectparams: dict[str, Any], epid: str) -> str:
    """Shared query suffix for both the handshake GET and the websocket URL (M2)."""
    parts = [f"{quote(k, safe='')}={quote(str(v), safe='')}" for k, v in connectparams.items()]
    parts += [
        f"tc={quote(_tc_blob(settings), safe='')}",
        f"con_num={_CON_NUM}",
        f"epid={quote(epid, safe='')}",
        "auth=true",
        "timeout=40",
    ]
    return "&".join(parts) + "&"  # trailing & is intentional (matches reference)


def handshake(settings: Settings, info: dict[str, Any], skype_token: str, epid: str) -> str:
    """GET socket.io/1/ → plaintext '<sessionId>:<hb>:<timeout>:<transports>'; return sessionId."""
    query = common_query(settings, info["connectparams"], epid)
    url = f"{info['socketio']}socket.io/1/?v=v4&{query}"
    headers = {"X-Skypetoken": skype_token, "User-Agent": settings.user_agent}
    resp = httpx.get(url, headers=headers, timeout=30.0)
    resp.raise_for_status()
    session_id = resp.text.split(":", 1)[0]
    log.info("trouter_handshake", session_id=session_id, raw=resp.text.strip())
    return session_id
