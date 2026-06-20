"""Settings properties and low-level stdout helpers."""

from miniteams._io import emit, force_blocking_stdout
from miniteams.config import Settings


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
