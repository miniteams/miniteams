"""Send any file the way Teams does: upload it to the sender's OneDrive chat files folder, share it
with the chat's members, post a message whose `properties.files` draws Teams' file card.

SharePoint's own APIs on the sender's `-my` host with that host's bearer (`/_api/web` and the
OneDrive `/_api/v2.0` endpoints SharePoint serves itself), never Microsoft Graph. Shapes confirmed
by live trials on a test chat (spec 025).
"""

import contextlib
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, BinaryIO
from urllib.parse import quote, urlsplit

import httpx
import structlog

from .auth import token_source, token_tenant
from .config import Settings
from .send import NOTES_THREAD, _fail, send_message

log = structlog.get_logger()

_CLIENT = httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))

MAX_FILES = 10
MAX_FILE_BYTES = 100 << 20
SIMPLE_MAX = 4 << 20  # above this, an upload session: one request per chunk
CHUNK = 5 * 320 << 10  # upload sessions take multiples of 320 KiB

# Teams names the folder in the user's language when it first creates it, and nothing in SharePoint
# marks it: the known names are probed, the most recently used one wins.
CHAT_FOLDERS = ("Microsoft Teams Chat Files", "Fichiers de conversation Microsoft Teams")

# Control characters and what OneDrive refuses in a name; `/` would also reach outside the folder.
_BAD_NAME = re.compile(r'[\x00-\x1f\x7f"*:<>?/\\|]')

_CONFLICT = "@microsoft.graph.conflictBehavior"  # OneDrive API property name, not a Graph call


@dataclass(frozen=True)
class OneDrive:
    site: str  # https://<tenant>-my.sharepoint.com/personal/<user>
    token: str

    @property
    def host(self) -> str:
        return urlsplit(self.site).netloc

    @property
    def drive(self) -> str:
        return f"{self.site}/_api/v2.0/drive"

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}


def _ok(resp: httpx.Response, action: str) -> Any:
    if not resp.is_success:  # a 3xx is no answer either: redirects are not followed
        try:  # OneDrive's envelope: {"error": {"code", "message"}}
            reason = str(resp.json()["error"]["message"])
        except ValueError, KeyError, TypeError:
            _fail(resp, action)
        raise httpx.HTTPStatusError(
            f"{action} failed: HTTP {resp.status_code} {reason}"[:300], request=resp.request, response=resp
        )
    return resp.json() if resp.content else {}


def check_files(files: list[tuple[str, int]]) -> None:
    """ValueError naming the first (name, size) over this version's limits."""
    if len(files) > MAX_FILES:
        raise ValueError(f"{len(files)} files: at most {MAX_FILES} per message")
    for name, size in files:
        if size > MAX_FILE_BYTES:
            raise ValueError(f"{name}: over {MAX_FILE_BYTES >> 20} MiB")
        if name in ("", ".", "..") or name != name.strip(" ") or _BAD_NAME.search(name):
            raise ValueError(f"{name!r}: not a file name OneDrive takes")


def onedrive(settings: Settings, aad_token: str, sp_token: Callable[[str], str | None]) -> OneDrive:
    """The sender's OneDrive: the tenant's SharePoint prefix is its initial `<prefix>.onmicrosoft.com`
    domain (Teams' middle tier), the personal site is what SharePoint says it is."""
    tenants = _ok(
        _CLIENT.get(settings.tenants_url, headers={"Authorization": f"Bearer {aad_token}"}), "tenant lookup"
    )
    # The token's own tenant: settings.tenant_id may be a verified domain, and GUIDs vary in case.
    tid = token_tenant(aad_token).lower()
    domain = next(
        (
            str(t.get("tenantPrimaryDomainName") or "")
            for t in tenants
            if tid and str(t.get("tenantId") or "").lower() == tid
        ),
        "",
    )
    prefix, _, rest = domain.partition(".")
    # ponytail: a tenant renamed in SharePoint keeps its old onmicrosoft prefix; add a setting if one shows up
    if rest != "onmicrosoft.com" or not (prefix.isascii() and prefix.isalnum()):
        raise RuntimeError(f"no OneDrive host for primary domain {domain!r}")
    host = f"{prefix.lower()}-my.sharepoint.com"
    token = sp_token(host)
    if not token:
        raise RuntimeError(f"no SharePoint token for {host} (sign in again)")
    me = _ok(
        _CLIENT.get(
            f"https://{host}/_api/SP.UserProfiles.PeopleManager/GetMyProperties?$select=PersonalUrl",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json;odata=nometadata"},
        ),
        "OneDrive lookup",
    )
    site = str(me.get("PersonalUrl") or "").rstrip("/")
    if urlsplit(site).netloc != host or urlsplit(site).scheme != "https":
        raise RuntimeError("OneDrive lookup returned no personal site on its own host")
    return OneDrive(site, token)


def chat_folder(drive: OneDrive) -> str:
    """Name of the user's Teams chat files folder; created under Teams' English name if none exists."""
    found: list[tuple[str, str]] = []
    for name in CHAT_FOLDERS:
        resp = _CLIENT.get(
            f"{drive.drive}/root:/{quote(name)}?$select=name,folder,lastModifiedDateTime",
            headers=drive.headers(),
        )
        if resp.status_code == 404:
            continue
        item = _ok(resp, "chat folder lookup")
        if "folder" in item:
            found.append((str(item.get("lastModifiedDateTime") or ""), name))
    if found:
        return max(found)[1]
    name = CHAT_FOLDERS[0]
    resp = _CLIENT.post(
        f"{drive.drive}/root/children",
        headers=drive.headers(),
        json={"name": name, "folder": {}, _CONFLICT: "fail"},
    )
    if resp.status_code == 409:  # created meanwhile by Teams: use it
        return name
    _ok(resp, "chat folder create")
    log.info("chat_folder_created", name=name)
    return name


def _read(fh: BinaryIO, size: int, *, last: bool) -> bytes:
    """`size` bytes of `fh`; with `last`, nothing may follow them. A size from fstat can be wrong
    (/proc files say 0) or go stale (a file still being written): never upload a truncated file."""
    data = fh.read(size)
    if len(data) != size or (last and fh.read(1)):
        raise RuntimeError("file changed while uploading")
    return data


@contextlib.contextmanager
def _may_land(folder: str, name: str) -> Iterator[None]:
    """Around the request that creates the file and its check: a transport failure, a 5xx (a gateway
    giving up on a slow store) or a 2xx we cannot parse may still have stored it."""
    try:
        yield
    except (httpx.HTTPError, ValueError) as exc:
        if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code >= 500:
            log.warning("upload_outcome_unknown", folder=folder, name=name)
        raise


def upload(drive: OneDrive, folder: str, name: str, fh: BinaryIO, size: int) -> dict[str, Any]:
    """Upload `size` bytes of `fh` as `folder/name` (renamed by OneDrive on a clash); the new item.

    Read a chunk at a time above SIMPLE_MAX: ten 100 MiB files never sit in memory together."""
    path = f"{drive.drive}/root:/{quote(folder)}/{quote(name)}:"
    item: dict[str, Any]
    if size <= SIMPLE_MAX:
        data = _read(fh, size, last=True)
        with _may_land(folder, name):
            resp = _CLIENT.put(f"{path}/content?{_CONFLICT}=rename", headers=drive.headers(), content=data)
            item = _ok(resp, "upload")
            if not isinstance(item, dict) or "id" not in item:
                raise ValueError("upload returned no item")
    else:
        item = _upload_session(drive, folder, name, fh, size)
    log.info("file_uploaded", size=size, chunked=size > SIMPLE_MAX)
    return item


def _upload_session(drive: OneDrive, folder: str, name: str, fh: BinaryIO, size: int) -> dict[str, Any]:
    path = f"{drive.drive}/root:/{quote(folder)}/{quote(name)}:"
    session = _ok(
        _CLIENT.post(
            f"{path}/createUploadSession", headers=drive.headers(), json={"item": {_CONFLICT: "rename"}}
        ),
        "upload session",
    )
    url = str(session.get("uploadUrl") or "")
    # The upload URL is pre-authorised: it gets no bearer, and the bytes go nowhere but the drive's host.
    if urlsplit(url).netloc != drive.host or urlsplit(url).scheme != "https":
        raise RuntimeError("upload session URL is not on the OneDrive host")
    try:
        item: dict[str, Any] = {}
        for start in range(0, size, CHUNK):
            last = start + CHUNK >= size
            chunk = _read(fh, min(CHUNK, size - start), last=last)
            end = start + len(chunk) - 1
            with _may_land(folder, name) if last else contextlib.nullcontext():
                resp = _CLIENT.put(
                    url, content=chunk, headers={"Content-Range": f"bytes {start}-{end}/{size}"}
                )
                item = _ok(resp, "upload chunk")
        if "id" not in item:
            raise RuntimeError("upload session ended without an item")
        return item
    except BaseException:
        # Best effort, and never in place of the real failure: an abandoned session expires anyway.
        with contextlib.suppress(Exception):
            _CLIENT.delete(url)
        raise


def item_ids(drive: OneDrive, item_id: str) -> dict[str, Any]:
    """`webUrl`, `name` and `sharepointIds.listItemUniqueId` (the card's `itemid`) of an item."""
    return dict(
        _ok(
            _CLIENT.get(
                f"{drive.drive}/items/{quote(item_id)}?$select=id,name,webUrl,sharepointIds",
                headers=drive.headers(),
            ),
            "item lookup",
        )
    )


def share(drive: OneDrive, item_id: str, members: list[str]) -> dict[str, Any]:
    """An edit link for exactly `members` (AAD object ids), as Teams' "people in this chat"."""
    body = {"type": "edit", "scope": "users", "recipients": [{"objectId": m} for m in members]}
    return dict(
        _ok(
            _CLIENT.post(
                f"{drive.drive}/items/{quote(item_id)}/createLink", headers=drive.headers(), json=body
            ),
            "share",
        )
    )


def delete(drive: OneDrive, item_id: str) -> None:
    _ok(_CLIENT.delete(f"{drive.drive}/items/{quote(item_id)}", headers=drive.headers()), "delete")


def file_entry(drive: OneDrive, item: dict[str, Any], link: dict[str, Any]) -> dict[str, Any]:
    """One `properties.files` entry, in the shape Teams posts for a chat upload."""
    name = str(item["name"])
    ext = PurePosixPath(name).suffix.lstrip(".").lower()
    guid = str((item.get("sharepointIds") or {}).get("listItemUniqueId") or "")
    url, site = str(item["webUrl"]), f"{drive.site}/"
    return {
        "@type": "http://schema.skype.com/File",
        "version": 2,
        "id": guid,
        "itemid": guid,
        "fileName": name,
        "title": name,
        "type": ext,
        "fileType": ext,
        "state": "active",
        "fileChicletState": {"serviceName": "p2p", "state": "active"},
        "objectUrl": url,
        "baseUrl": site,
        "openUrl": None,
        "fileInfo": {
            "itemId": None,
            "fileUrl": url,
            "siteUrl": site,
            "serverRelativeUrl": "",
            # The link comes back with `?email=<one recipient>`: the card shows it to the whole chat.
            "shareUrl": str((link.get("link") or {}).get("webUrl") or "").partition("?")[0],
            "shareId": str(link.get("id") or ""),
        },
        "permissionScope": "SpecificPeople",
        "filePreview": {"previewUrl": "", "previewHeight": 0, "previewWidth": 0},
        "sharepointIds": {"listItemUniqueId": guid},
        "botFileProperties": {},
        "providerData": "",
        "chicletBreadcrumbs": None,
        "isUploadError": None,
        "progressComplete": None,
        "publication": None,
        "site": None,
    }


def chat_members(settings: Settings, skype_token: str, thread_id: str, me: str) -> list[str]:
    """AAD object ids of the chat's people but `me` (`8:orgid:<guid>` MRIs); apps are left out."""
    if thread_id == NOTES_THREAD:  # no roster, nobody to share with
        return []
    resp = _CLIENT.get(
        f"https://{settings.contacts_host}/v1/threads/{quote(thread_id, safe='')}?view=msnp24Equivalent",
        headers={
            "X-Skypetoken": skype_token,
            "Accept": "application/json; ver=1.0;",
            "User-Agent": settings.user_agent,
        },
    )
    ids = [str(m.get("id") or "") for m in _ok(resp, "chat roster").get("members") or []]
    return [mri.removeprefix("8:orgid:") for mri in ids if mri.startswith("8:orgid:") and mri != me]


def _discard(drive: OneDrive, ids: list[str]) -> None:
    for item_id in ids:
        try:
            delete(drive, item_id)
        except Exception as exc:  # noqa: BLE001 — the original failure is the one to report
            log.warning("orphan_delete_failed", item=item_id, error=str(exc)[:200])


def my_onedrive(settings: Settings) -> OneDrive:
    """The signed-in user's OneDrive, from the cached sign-in: silent, it never prompts."""
    source = token_source(settings)
    return onedrive(settings, str(source.refresh()["access_token"]), source.sharepoint_token)


def upload_files(
    settings: Settings,
    skype_token: str,
    drive: OneDrive,
    me: str,
    thread_id: str,
    files: list[tuple[str, BinaryIO, int]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Upload `files` ((name, readable file, size)) and share them with the chat's people but `me`:
    their `properties.files` entries and item ids. Whatever was uploaded is deleted again on failure."""
    check_files([(name, size) for name, _, size in files])
    members = chat_members(settings, skype_token, thread_id, me)
    folder = chat_folder(drive)
    uploaded: list[str] = []
    try:
        entries = []
        for name, fh, size in files:
            item_id = str(upload(drive, folder, name, fh, size)["id"])
            uploaded.append(item_id)
            link = share(drive, item_id, members) if members else {}
            entries.append(file_entry(drive, item_ids(drive, item_id), link))
    except BaseException:
        _discard(drive, uploaded)
        raise
    return entries, uploaded


def post_files(
    settings: Settings,
    skype_token: str,
    thread_id: str,
    text: str,
    display_name: str,
    *,
    drive: OneDrive,
    entries: list[dict[str, Any]],
    uploaded: list[str],
    properties: dict[str, Any] | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """send_message with the uploaded files' cards. A rejected post deletes them; a post that may have
    landed (timeout, dropped connection) keeps them: deleting them would break a posted message's cards."""
    try:
        return send_message(
            settings,
            skype_token,
            thread_id,
            text,
            display_name,
            properties={**(properties or {}), "files": json.dumps(entries)},
            **kw,
        )
    except BaseException as exc:
        # Only a 4xx or an error envelope says nothing was posted: a gateway 5xx can follow a stored message.
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 0
        if isinstance(exc, RuntimeError) or 400 <= status < 500:
            _discard(drive, uploaded)
        else:
            log.warning("files_kept_post_unknown", thread=thread_id, items=uploaded)
        raise


def send_files(
    settings: Settings,
    skype_token: str,
    drive: OneDrive,
    me: str,
    thread_id: str,
    text: str,
    files: list[tuple[str, BinaryIO, int]],
    display_name: str,
    *,
    is_html: bool = False,
) -> dict[str, Any]:
    """One message from `me` (MRI): `text` and `files` ((name, readable file, size)) as file cards.
    Nothing is posted unless every file is uploaded and shared (upload_files, post_files)."""
    entries, uploaded = upload_files(settings, skype_token, drive, me, thread_id, files)
    return post_files(
        settings,
        skype_token,
        thread_id,
        text,
        display_name,
        drive=drive,
        entries=entries,
        uploaded=uploaded,
        is_html=is_html,
    )
