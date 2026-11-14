"""scripts/follow-latency.py: AC7's latency from a follower log (spec 044, phase 4 DoD)."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "follow-latency.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=30)


def test_median_p90_and_max_of_line_done(tmp_path: Path) -> None:
    """044-AC7: only line_done events count; other events, prose and broken lines are skipped."""
    log = tmp_path / "call.log"
    events = [{"event": "follow_start", "call": "c"}]
    events += [{"event": "line_done", "msi": 1, "latency_s": v} for v in (0.9, 1.4, 1.1, 3.5, 1.2)]
    events += [{"event": "line_done", "msi": 2, "latency_s": None}, {"event": "line_done", "latency_s": True}]
    lines = [json.dumps(e) for e in events] + ["Traceback (most recent call last):", "{not json", "[1, 2]"]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = run(str(log))
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"lines": 5, "median_s": 1.2, "p90_s": 3.5, "max_s": 3.5}


def test_a_log_without_lines_fails_clearly(tmp_path: Path) -> None:
    log = tmp_path / "call.log"
    log.write_text('{"event": "follow_start"}\n', encoding="utf-8")
    out = run(str(log))
    assert out.returncode == 1 and out.stdout == ""
    assert out.stderr.strip() == f"follow-latency: no line_done event with latency_s in {log}"
    missing = run(str(tmp_path / "absent.log"))
    assert missing.returncode == 1 and "No such file" in missing.stderr
    assert run().returncode == 2
