"""Avatar/icon download: content-type extension, skip-if-exists, no-photo/no-org-id handling."""

from pathlib import Path
from typing import Any

from miniteams import avatars as AV


class _Resp:
    def __init__(self, content: bytes, ct: str, status: int = 200) -> None:
        self.content = content
        self.headers = {"content-type": ct}
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")


class _Client:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp
        self.urls: list[str] = []

    async def get(self, url: str = "", headers: Any = None, cookies: Any = None) -> _Resp:
        self.urls.append(url)
        return self._resp


async def test_group_icon_strips_url_prefix_and_names_by_ctype(tmp_path: Path) -> None:
    client = _Client(_Resp(b"PNGDATA", "image/png"))
    dest = await AV.fetch_group_icon(
        client, "URL@https://asyncgw/objects/g1/views/avatar_fullsize", "sk", tmp_path
    )
    assert dest is not None and dest.endswith("group.png")
    assert Path(dest).read_bytes() == b"PNGDATA"
    assert client.urls == ["https://asyncgw/objects/g1/views/avatar_fullsize"]  # URL@ stripped


async def test_user_avatar_skips_non_orgid(tmp_path: Path) -> None:
    client = _Client(_Resp(b"x", "image/jpeg"))
    assert await AV.fetch_user_avatar(client, "8:bot:foo", "b", tmp_path) is None
    assert client.urls == []  # never fetched


async def test_user_avatar_404_returns_none(tmp_path: Path) -> None:
    client = _Client(_Resp(b"", "application/json", status=404))
    assert await AV.fetch_user_avatar(client, "8:orgid:x", "b", tmp_path) is None


async def test_skip_if_exists(tmp_path: Path) -> None:
    (tmp_path / "group.jpg").write_bytes(b"old")
    client = _Client(_Resp(b"new", "image/png"))
    dest = await AV.fetch_group_icon(client, "https://asyncgw/objects/g1", "sk", tmp_path)
    assert dest == str(tmp_path / "group.jpg")  # returned existing
    assert client.urls == []  # not re-downloaded
    assert (tmp_path / "group.jpg").read_bytes() == b"old"
