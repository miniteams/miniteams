import json
import subprocess
import sys

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
