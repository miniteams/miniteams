"""Conversation icons + member avatars (best-effort, endpoints verified live).

- Group icon: the thread's `properties.picture` is `URL@<AMS url>`; fetch it with the skype-token
  cookie, like any AMS object.
- Member / 1:1 avatar: `.../users/<mri>/profilepicturev2` returns the picture for a bearer-auth'd
  request. A user with no photo 404s — that's fine, we just skip.

All failures are swallowed: avatars enrich the archive, they never gate it.
"""

from pathlib import Path

import httpx
import structlog

log = structlog.get_logger()

_AVATAR_ENDPOINT = "https://teams.microsoft.com/api/mt/beta/users/{mri}/profilepicturev2"
_CTYPE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}


def _safe(mri: str) -> str:
    return mri.replace("/", "_").replace(":", "_")


async def _download(client: httpx.AsyncClient, dest_stem: Path, **get_kwargs: object) -> str | None:
    """GET → write bytes to dest_stem+ext (from content-type). Skip if a file already exists."""
    for existing in dest_stem.parent.glob(f"{dest_stem.name}.*"):
        if not existing.name.endswith(".part"):
            return str(existing)
    resp = await client.get(**get_kwargs)  # type: ignore[arg-type]
    resp.raise_for_status()
    ext = _CTYPE_EXT.get((resp.headers.get("content-type") or "").split(";")[0], ".img")
    dest_stem.parent.mkdir(parents=True, exist_ok=True)
    dest = dest_stem.with_name(f"{dest_stem.name}{ext}")
    tmp = dest_stem.with_name(f"{dest_stem.name}.part")
    tmp.write_bytes(resp.content)  # avatars are tiny (KBs) — no chunking needed
    tmp.rename(dest)
    return str(dest)


async def fetch_group_icon(
    client: httpx.AsyncClient, picture_field: str, skype_token: str, avatars_dir: Path
) -> str | None:
    """Download a group chat's custom picture from its `properties.picture` value."""
    url = picture_field.split("URL@", 1)[-1] if "URL@" in picture_field else picture_field
    if not url.startswith("http"):
        return None
    try:
        return await _download(
            client,
            avatars_dir / "group",
            url=url,
            headers={"Accept": "*/*"},
            cookies={"skypetoken_asm": skype_token},
        )
    except Exception as exc:  # noqa: BLE001 — best-effort enrichment
        log.debug("group_icon_failed", url=url, error=str(exc))
        return None


async def fetch_user_avatar(
    client: httpx.AsyncClient, mri: str, bearer: str, avatars_dir: Path
) -> str | None:
    """Download one member's profile picture; None if they have none (404) or on any error."""
    if not mri.startswith("8:orgid:"):
        return None  # bots / PSTN / federated ids have no org profile picture
    try:
        return await _download(
            client,
            avatars_dir / _safe(mri),
            url=_AVATAR_ENDPOINT.format(mri=mri),
            headers={"Authorization": f"Bearer {bearer}"},
        )
    except Exception as exc:  # noqa: BLE001 — 404 (no photo) is the common case
        log.debug("user_avatar_failed", mri=mri, error=str(exc))
        return None
