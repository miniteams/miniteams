"""Skype-token exchange.

POST the AAD bearer token to the authz endpoint; the response carries the skype token used
as `X-Skypetoken` for every trouter/registrar call. Two response shapes exist in the wild
(handled per purple-teams teams_login.c).
"""

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
    # The chat service lives in the account's region: the APAC default costs ~2 s a call from the EU.
    # An explicit MINITEAMS_CONTACTS_HOST still wins.
    gtms = data.get("regionGtms") or {}
    chat_host = urlparse(str(gtms.get("chatService") or "")).hostname
    if chat_host and "contacts_host" not in settings.model_fields_set:
        settings.contacts_host = chat_host
    csa_url = str(gtms.get("chatSvcAggAfd") or "").rstrip("/")
    if csa_url.startswith("https://") and "csa_url" not in settings.model_fields_set:
        settings.csa_url = csa_url
    log.info("skype_token_acquired", expires_in=expires_in, region=region, chat_host=settings.contacts_host)
    return {"skype_token": token, "expires_in": expires_in, "region": region}


def persist_skype_token(settings: Settings, token_data: dict[str, Any]) -> Path:
    path = settings.config_dir / "skype_token.json"
    path.write_text(json.dumps(token_data, indent=2))
    path.chmod(0o600)  # Security: bearer-equivalent secret — owner-only.
    return path
