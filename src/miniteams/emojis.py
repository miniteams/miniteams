"""The organisation's custom emojis: name, reaction key, creator, creation date, image.

Listed by the chat-service aggregator (CSA) `customemoji/metadata`, the call Teams web makes for
its picker. Graph beta `teamwork/messaging/customEmojis` has the same data but needs the
`TeamworkCustomEmoji.Read` scope, which the Teams FOCI token does not carry. Each image is an AMS
object (`documentId`), fetched like a chat image.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from ._io import emit, force_blocking_stdout
from .attachments import fetch_image
from .config import Settings
from .directory import Directory

log = structlog.get_logger()

_IMAGE_URL = "https://api.asm.skype.com/v1/objects/{}/views/imgpsh_fullsize"


def fetch_metadata(settings: Settings, skype_token: str, csa_token: str) -> dict[str, Any]:
    resp = httpx.get(
        f"{settings.csa_url}/api/v1/customemoji/metadata",
        headers={
            "Authentication": f"skypetoken={skype_token}",
            "Authorization": f"Bearer {csa_token}",
            "User-Agent": settings.user_agent,
            "Origin": "https://teams.microsoft.com",
            "Referer": "https://teams.microsoft.com/",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return data


def rows(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Live emojis, oldest first, as {name, key, created, creator_mri, document_id}."""
    out = []
    for category in metadata.get("categories") or []:
        for emo in category.get("emoticons") or []:
            if emo.get("isDeleted") or not emo.get("id"):
                continue
            created = emo.get("createdOn")
            out.append(
                {
                    "name": (emo.get("shortcuts") or [emo["id"].split(";")[0]])[0],
                    "key": emo["id"],  # the reaction key: `miniteams react <msg> '<key>'`
                    "created": (
                        datetime.fromtimestamp(created / 1000, UTC).isoformat(timespec="seconds")
                        if isinstance(created, int | float)
                        else ""
                    ),
                    "creator_mri": _mri(str(emo.get("creator") or "")),
                    "document_id": emo.get("documentId") or "",
                }
            )
    return sorted(out, key=lambda r: (r["created"], r["name"]))


def _mri(creator: str) -> str:
    # Older entries give a bare AAD object id, newer ones the MRI itself (of any kind).
    return creator if not creator or ":" in creator else f"8:orgid:{creator}"


async def list_emojis(
    settings: Settings,
    skype_token: str,
    bearer: str,
    csa_token: str,
    *,
    jsonl: bool = False,
    download: bool = False,
) -> None:
    force_blocking_stdout()  # inside the running loop (see _io); guards `--jsonl | jq` backpressure
    emojis = rows(fetch_metadata(settings, skype_token, csa_token))
    directory = Directory(settings)
    directory.set_token(skype_token, bearer)
    await directory.resolve([r["creator_mri"] for r in emojis], refresh=True)
    dest = settings.cache_dir / "emojis"
    if download:
        dest.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=30.0) as client:
        for r in emojis:
            r["creator"] = directory.name_for(r["creator_mri"]) if r["creator_mri"] else ""
            if download and r["document_id"]:
                r["image"] = await _image(client, skype_token, r["document_id"], dest)
            if jsonl:
                await emit(json.dumps(r, ensure_ascii=False) + "\n")
            else:
                image = f"  {r['image']}" if r.get("image") else ""
                await emit(f"{r['created'][:10]}  {r['name']}  ({r['creator']}){image}\n")
    log.info("custom_emojis_listed", count=len(emojis))


async def _image(client: httpx.AsyncClient, skype_token: str, document_id: str, dest: Path) -> str:
    try:
        url = _IMAGE_URL.format(quote(document_id, safe=""))
        return await fetch_image(client, skype_token, url, dest, suffix="")
    except Exception as exc:  # noqa: BLE001 — one missing image must not end the listing
        log.warning("emoji_image_failed", document_id=document_id, error=str(exc))
        return ""
