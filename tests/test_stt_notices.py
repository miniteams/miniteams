"""044-AC16: every file copied from grill-room names its origin and keeps the Apache-2.0 notice; the
ones changed here say so (Apache-2.0 section 4(b))."""

import hashlib
import itertools
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_ORIGIN = re.compile(r"^# Copied from grill-room `stt/[\w/]+\.py` @ [0-9a-f]{7,40} \(spec 044, AC16\)")
# Markers a changed copy's first line carries after its origin.
_CHANGE = re.compile(r"\), (modified|trimmed)\b")
# Partial copies and derived helpers: origin commit and licence anywhere in the file, close together.
_MENTION = re.compile(r"grill-room[^\n]*@ [0-9a-f]{7,40}[\s\S]{0,200}?Apache-2\.0")
# sha256 of each body below its header, as copied: an edit fails here and moves the file to CHANGED.
AS_IS = {
    "src/miniteams/stt/line.py": (
        "7875ec259ff4f97e5844e6977b2335ba248b49f8a78cd65a2107507ae8506e4d"  # pragma: allowlist secret
    ),
    "src/miniteams/stt/parakeet.py": (
        "f6ca006a817f7adf1e7c169fd2e1e6b50a0af98cd9a3522e6e93b4d7f737b664"  # pragma: allowlist secret
    ),
    "src/miniteams/stt/transcribe.py": (
        "4fd0ed827ca2fc00b0e001759a851ecbc67b4b2687ff11e87f303251300a6e94"  # pragma: allowlist secret
    ),
}
CHANGED = [
    "src/miniteams/stt/vad.py",
    "src/miniteams/stt/engines.py",
    "src/miniteams/stt/pipeline.py",
    "tests/stt_fake_engine.py",
    "tests/test_stt_vad.py",
    "tests/test_stt_parakeet.py",
    "tests/test_stt_pipeline.py",
    "tests/test_stt_plausibility.py",
    "tests/test_stt_pool.py",
]
NOTICED = [
    "src/miniteams/stt/__init__.py",
    "src/miniteams/config.py",
    "tests/test_stt_real.py",
    "tests/stt_helpers.py",
]


def _header(path: str) -> str:
    """The leading comment block: a licence line moved below it does not count."""
    lines = (ROOT / path).read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(itertools.takewhile(lambda line: line.startswith("#"), lines))


@pytest.mark.parametrize("path", [*AS_IS, *CHANGED])
def test_copied_files_name_their_origin_and_licence(path: str) -> None:
    head = _header(path)
    assert _ORIGIN.match(head), path
    assert "Licensed under the Apache License, Version 2.0" in head, path


@pytest.mark.parametrize("path", CHANGED)
def test_changed_copies_say_what_changed(path: str) -> None:
    assert _CHANGE.search(_header(path).splitlines()[0]), path


@pytest.mark.parametrize("path", NOTICED)
def test_partial_copies_name_their_origin_and_licence(path: str) -> None:
    assert _MENTION.search((ROOT / path).read_text(encoding="utf-8")), path


def test_every_copy_is_listed() -> None:
    marked = {
        str(p.relative_to(ROOT))
        for d in ("src", "tests")
        for p in (ROOT / d).rglob("*.py")
        if p.read_text(encoding="utf-8").startswith("# Copied from grill-room")
    }
    assert marked == {*AS_IS, *CHANGED}


def test_the_fixtures_name_their_origin() -> None:
    voices = json.loads((ROOT / "tests/stt_fixtures/voices.json").read_text(encoding="utf-8"))
    assert "grill-room" in voices["origin"] and "Apache-2.0" in voices["origin"]
    listed = {f["file"] for f in voices["files"] + voices["leak"]}
    assert listed == {p.name for p in (ROOT / "tests/stt_fixtures").glob("*.wav")}


@pytest.mark.parametrize(("path", "digest"), AS_IS.items())
def test_as_is_copies_are_unchanged(path: str, digest: str) -> None:
    lines = (ROOT / path).read_text(encoding="utf-8").splitlines(keepends=True)
    body = "".join(itertools.dropwhile(lambda line: line.startswith("#"), lines))
    assert hashlib.sha256(body.encode()).hexdigest() == digest, f"{path} changed: move it to CHANGED"
