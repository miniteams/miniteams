import json
import subprocess
import sys
from pathlib import Path

# A child process: setup_logging configures structlog globally, which would leak into other tests.
_SCRIPT = """
import structlog
from miniteams.logging import setup_logging
setup_logging()
try:
    1 / 0
except ZeroDivisionError:
    structlog.get_logger().warning("boom", exc_info=True)
"""


def test_json_logs_carry_the_traceback_on_one_line() -> None:
    err = subprocess.run([sys.executable, "-c", _SCRIPT], capture_output=True, text=True, check=True).stderr
    lines = err.strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "boom" and "ZeroDivisionError" in record["exception"]


_FULL_DISK = """
import structlog
from miniteams.logging import setup_logging
setup_logging()
log = structlog.get_logger()
for write in (log.info, log.warning, log.error, log.exception, log.debug, log.critical):
    write("lost", n=1)
print("survived", flush=True)
"""


def test_a_log_line_that_cannot_be_written_is_lost_not_raised(tmp_path: Path) -> None:
    """A full disk (or a stale NFS handle) under the follower's log: logging never raises into the code
    that must still hang up."""
    with open("/dev/full", "w") as full:
        out = subprocess.run(
            [sys.executable, "-c", _FULL_DISK], stderr=full, stdout=subprocess.PIPE, text=True, timeout=60
        )
    assert out.stdout == "survived\n"  # (exit 120: the interpreter's own stderr flush at exit fails too)
