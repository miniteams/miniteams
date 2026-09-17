"""Skype-token exchange.

POST the AAD bearer token to the authz endpoint; the response carries the skype token used
as `X-Skypetoken` for every trouter/registrar call. Two response shapes exist in the wild
(handled per purple-teams teams_login.c).
"""

import json
from pathlib import Path
from typing import Any

import httpx
import structlog

from .config import Settings

log = structlog.get_logger()


def exchange_skype_token(settings: Settings, access_token: str) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json; ver=1.0",
        "Content-Length": "0",
    }
    resp = httpx.post(settings.authz_url, headers=headers, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()

    if "tokens" in data:
        token = data["tokens"]["skypeToken"]
        expires_in = data["tokens"].get("expiresIn")
    elif "skypeToken" in data:
        token = data["skypeToken"]["skypetoken"]
        expires_in = data["skypeToken"].get("expiresIn")
    else:
        raise RuntimeError(f"no skype token in authz response: keys={list(data)}")

    region = data.get("region")
    log.info("skype_token_acquired", expires_in=expires_in, region=region)
    return {"skype_token": token, "expires_in": expires_in, "region": region}


def persist_skype_token(settings: Settings, token_data: dict[str, Any]) -> Path:
    path = settings.config_dir / "skype_token.json"
    path.write_text(json.dumps(token_data, indent=2))
    path.chmod(0o600)  # Security: bearer-equivalent secret — owner-only.
    return path
