"""Settings properties, owner-only file and directory helpers, and the low-level stdout helpers."""

import os
import stat
from pathlib import Path

import pytest

from miniteams._io import emit, force_blocking_stdout
from miniteams.config import Settings, write_private


def test_authority_and_scope() -> None:
    s = Settings(tenant_id="abc")
    assert s.authority == "https://login.microsoftonline.com/abc"
    assert s.scope_list == ["https://api.spaces.skype.com/.default"]


def test_media_dir_under_cache(tmp_path) -> None:
    s = Settings(tenant_id="abc")
    s.cache_dir = tmp_path
    assert s.media_dir == tmp_path / "media"


def test_force_blocking_stdout_never_raises() -> None:
    # stdout under pytest capture has no real fileno → must be suppressed, not raised.
    force_blocking_stdout()


async def test_emit_writes_to_stdout(capsys) -> None:
    await emit("hello\n")
    assert capsys.readouterr().out == "hello\n"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_private_new_parent_is_0700_existing_left_alone(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        write_private(tmp_path / "a" / "b" / "f", "x")
    finally:
        os.umask(old)
    assert _mode(tmp_path / "a" / "b") == 0o700
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o755)
    write_private(loose / "f", "x")
    assert _mode(loose) == 0o755


def test_launch_file_defaults_under_config_dir_and_is_absolute(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # Settings reads ./.env: a local MINITEAMS_LAUNCH_FILE must not leak in
    monkeypatch.delenv("MINITEAMS_LAUNCH_FILE", raising=False)
    assert Settings(tenant_id="t", config_dir=tmp_path).launch_file == tmp_path / "open.html"
    monkeypatch.setenv("MINITEAMS_LAUNCH_FILE", "rel/open.html")
    assert Settings(tenant_id="t").launch_file == tmp_path / "rel" / "open.html"
    monkeypatch.setenv("MINITEAMS_LAUNCH_FILE", "~/x.html")
    assert Settings(tenant_id="t").launch_file == Path.home() / "x.html"


def test_write_private_replaces_atomically_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "new" / "f"
    old = os.umask(0)
    try:
        write_private(target, "one")
        inode = target.stat().st_ino
        write_private(target, "two")
    finally:
        os.umask(old)
    assert target.read_text() == "two" and _mode(target) == 0o600
    assert target.stat().st_ino != inode  # swapped in, never rewritten in place
    assert _mode(target.parent) == 0o700 and os.listdir(target.parent) == ["f"]


def test_write_private_failed_replace_keeps_old_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f"
    write_private(target, "one")

    def failing(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing)
    with pytest.raises(OSError, match="disk full"):
        write_private(target, "two")
    assert target.read_text() == "one" and os.listdir(tmp_path) == ["f"]


def test_write_private_into_read_only_dir_raises_and_leaves_nothing(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("running as root")
        with pytest.raises(PermissionError):
            write_private(locked / "f", "x")
        assert os.listdir(locked) == []
    finally:
        locked.chmod(0o700)
