"""024-AC8: the launch file is gone; its setting must not come back in code, tests or docs."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_launch_file_setting_anywhere() -> None:
    files = [p for d in ("src", "tests") for p in sorted((ROOT / d).rglob("*")) if p.is_file()]
    files += [ROOT / ".env.example", ROOT / "README.md"]
    hits = [
        (str(path.relative_to(ROOT)), line.strip())
        for path in files
        if "__pycache__" not in path.parts and path != Path(__file__).resolve()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if re.search(r"launch_file|MINITEAMS_LAUNCH_FILE", line)
    ]
    assert hits == []
