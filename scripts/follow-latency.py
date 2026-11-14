#!/usr/bin/env python3
"""Line latency of a followed call (spec 044, AC7): end of speech to line written, from the follower log.

Usage: scripts/follow-latency.py <data-dir>/follow/<call>.log
Reads the log's `line_done` events (structlog JSON, one per line; other lines are skipped) and prints
{"lines", "median_s", "p90_s", "max_s"} as one JSON line. Exit 1 when the log has no line_done event.
"""

import json
import math
import statistics
import sys


def latencies(lines: list[str]) -> list[float]:
    out = []
    for raw in lines:
        try:
            event = json.loads(raw)
        except ValueError:
            continue  # a traceback or a native library's own output
        if isinstance(event, dict) and event.get("event") == "line_done":
            value = event.get("latency_s")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out.append(float(value))
    return out


def summary(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    # Nearest rank: the p90 is a latency some line actually had.
    p90 = ordered[math.ceil(0.9 * len(ordered)) - 1]
    return {
        "lines": len(ordered),
        "median_s": round(statistics.median(ordered), 3),
        "p90_s": p90,
        "max_s": ordered[-1],
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <follower log>", file=sys.stderr)
        return 2
    try:
        with open(argv[1], encoding="utf-8", errors="replace") as f:
            values = latencies(f.read().splitlines())
    except OSError as exc:
        print(f"follow-latency: {exc}", file=sys.stderr)
        return 1
    if not values:
        print(f"follow-latency: no line_done event with latency_s in {argv[1]}", file=sys.stderr)
        return 1
    print(json.dumps(summary(values)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
