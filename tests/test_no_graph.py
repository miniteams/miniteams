"""Project rule: no Microsoft Graph — native endpoints, Trouter and purple-teams only."""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
# The browser login's AAD seed scope: an audience asked of AAD, never a Graph API call. Changing it
# needs a live interactive login to prove the FOCI seed still works.
_SEED = ("miniteams/config.py", 'seed_scope: str = "https://graph.microsoft.com/.default"')


def test_no_graph_host_under_src() -> None:
    hits = [
        (str(path.relative_to(SRC)), line.strip())
        for path in sorted(SRC.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if re.search(r"graph\.microsoft\.com", line, re.IGNORECASE)
    ]
    assert hits == [_SEED]
