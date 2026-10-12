"""The organisation's custom emojis: name, reaction key, creator, creation date, image.

Listed by the chat-service aggregator (CSA) `customemoji/metadata`, the call Teams web makes for
its picker. Graph beta `teamwork/messaging/customEmojis` has the same data but needs the
`TeamworkCustomEmoji.Read` scope, which the Teams FOCI token does not carry. Each image is an AMS
object (`documentId`), fetched like a chat image.
"""

import asyncio
import functools
import json
import mimetypes
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from ._io import emit, force_blocking_stdout
from .attachments import fetch_image
from .auth import token_source
from .compose import EMOJI_NAME_RE
from .config import Settings
from .directory import Directory
from .http import failure

log = structlog.get_logger()

_IMAGE_URL = "https://api.asm.skype.com/v1/objects/{}/views/imgpsh_fullsize"
_REFRESH = 6 * 3600.0
_RETRY = 120.0  # first retry after a failed set fetch, doubled up to _RETRY_MAX
_RETRY_MAX = 3600.0
_FAILED_TTL = 900.0  # seconds a failed image download is not retried: the page asks per keystroke
_FAILED_MAX = 512
_IMAGE_MAX = 50 * 1024 * 1024
_ID_RE = re.compile(r"[\w-]{1,128}", re.ASCII)


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


def ids_by_name(emojis: list[dict[str, Any]]) -> dict[str, str]:
    """`rows()` → {name: AMS object id}; the id is the key's tail when the entry has no documentId."""
    out: dict[str, str] = {}
    for r in emojis:
        oid = r["document_id"] or r["key"].partition(";")[2]
        if r["name"] and oid:
            out.setdefault(r["name"], oid)
    return out


async def cached_image(settings: Settings, skype_token: str, document_id: str) -> Path | None:
    """The emoji's image under the cache dir, downloaded on first use; None when it cannot be had."""
    dest = settings.cache_dir / "emojis"
    await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=30.0) as client:
        found = await _image(client, skype_token, document_id, dest)
    return Path(found) if found else None


async def _image(client: httpx.AsyncClient, skype_token: str, document_id: str, dest: Path) -> str:
    try:
        url = _IMAGE_URL.format(quote(document_id, safe=""))
        return await fetch_image(client, skype_token, url, dest, suffix="")
    except Exception as exc:  # noqa: BLE001 — one missing image must not end the listing
        log.warning("emoji_image_failed", document_id=document_id, error=str(exc))
        return ""


class EmojiCache:
    """The organisation's custom emojis for the composer: {name: AMS object id}, refreshed in the
    background, and their images served from the local cache."""

    def __init__(self, directory: Directory | None) -> None:
        self.directory = directory
        self.names: dict[str, str] = {}  # empty until fetched
        self._failed: dict[str, float] = {}  # image id → when its download failed
        self._downloads: dict[str, asyncio.Task[Path | None]] = {}  # image id → download in flight

    def custom(self) -> frozenset[str]:
        """Object ids of the custom emojis `/emoji/<id>` serves."""
        return frozenset(self.names.values())

    async def refresh(
        self, interval: float = _REFRESH, retry: float = _RETRY, retry_max: float = _RETRY_MAX
    ) -> None:
        """Fetch the set now and every `interval`; a failure keeps the last set and is retried after
        `retry`, doubled per failure up to `retry_max`."""
        wait = retry
        while True:
            pause = interval
            if self.directory is not None:
                try:
                    settings, token = self.directory.settings, self.directory.skype_token
                    self.names = await asyncio.to_thread(_custom_names, settings, token)
                    log.info("custom_emojis_loaded", count=len(self.names))
                    wait = retry
                except Exception as exc:  # noqa: BLE001 — the built-in set still completes
                    log.warning(
                        "custom_emojis_unavailable",
                        error=failure(exc),
                        error_type=type(exc).__name__,
                        retry_in=wait,
                    )
                    pause, wait = wait, min(wait * 2, retry_max)
            await asyncio.sleep(pause)

    async def image(self, oid: str) -> tuple[bytes, str] | None:
        """(body, content type) of a known custom emoji's image, None for anything else."""
        if not _ID_RE.fullmatch(oid) or oid not in self.custom() or self.directory is None:
            return None
        now = time.monotonic()
        failed_at = self._failed.get(oid)
        if failed_at is not None and now - failed_at < _FAILED_TTL:
            return None
        settings = self.directory.settings
        task = self._downloads.get(oid)
        if task is None:
            task = asyncio.create_task(cached_image(settings, self.directory.skype_token, oid))
            self._downloads[oid] = task
            task.add_done_callback(functools.partial(self._downloaded, oid))
        # Shielded: the HTTP handler runs under the socket's open timeout, the download and its memo don't.
        path = await asyncio.shield(task)
        if path is None:
            return None
        return await asyncio.to_thread(_cached_body, settings.cache_dir / "emojis", path)

    def _downloaded(self, oid: str, task: asyncio.Task[Path | None]) -> None:
        self._downloads.pop(oid, None)
        if not task.cancelled() and task.exception() is None and task.result() is not None:
            return
        now = time.monotonic()
        self._failed = {k: at for k, at in self._failed.items() if now - at < _FAILED_TTL and k != oid}
        self._failed[oid] = now
        while len(self._failed) > _FAILED_MAX:
            del self._failed[next(iter(self._failed))]  # oldest first


def _custom_names(settings: Settings, skype_token: str) -> dict[str, str]:
    """{name: AMS object id} of the names the composer expands. Blocking: worker thread."""
    csa_token = token_source(settings).csa_token()
    if not csa_token:
        raise RuntimeError("no chat-service token (run `miniteams login`)")
    found = ids_by_name(rows(fetch_metadata(settings, skype_token, csa_token)))
    return {name: oid for name, oid in found.items() if EMOJI_NAME_RE.fullmatch(name)}


def _cached_body(cache: Path, path: Path) -> tuple[bytes, str] | None:
    try:
        target = path.resolve(strict=True)
        if target.parent != cache.resolve(strict=True) or target.name.endswith(".part"):
            return None
        if target.stat().st_size > _IMAGE_MAX:
            return None
        return target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    except OSError:
        return None
