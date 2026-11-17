"""wrk output parser and the combined CSV row (Phase 6).

Parses the text `wrk` prints (with or without --latency) into numbers,
then combines one wrk run with one proxy snapshot into a single CSV row
with a stable schema (COMBINED_HEADER). The `mode` column distinguishes
custom-proxy runs from HAProxy runs (Phase 7) so both share one CSV.

Latency values are normalised to milliseconds (wrk may print us/ms/s).
"""

import csv
import re
from pathlib import Path

from .snapshot import _flatten_status_counts

_MS = {"us": 0.001, "ms": 1.0, "s": 1000.0}


def _to_ms(value: str, unit: str) -> float:
    return float(value) * _MS[unit]


def parse_wrk_output(text: str) -> dict:
    """Parse one wrk run's console output into a metric dict.

    Missing sections (e.g. no --latency, no errors) parse as None so the
    combined CSV schema stays stable.
    """
    metrics: dict = {
        "threads": None,
        "connections": None,
        "requests_total": None,
        "duration_s": None,
        "requests_per_sec": None,
        "latency_ms": {"p50": None, "p75": None, "p90": None, "p99": None},
        "thread_latency_avg_ms": None,
        "socket_errors": None,
        "non_2xx": None,
    }

    m = re.search(r"(\d+) threads and (\d+) connections", text)
    if m:
        metrics["threads"] = int(m.group(1))
        metrics["connections"] = int(m.group(2))

    # wrk's format_time_us switches to base-60 units at ~51s: the
    # requests line reads "42000 requests in 1.50m" (fractional minutes).
    m = re.search(r"(\d+) requests in ([\d.]+)(ms|s|m)\b", text)
    if m:
        metrics["requests_total"] = int(m.group(1))
        unit = m.group(3)
        if unit == "m":
            metrics["duration_s"] = float(m.group(2)) * 60.0
        else:
            metrics["duration_s"] = _to_ms(m.group(2), unit) / 1000.0

    m = re.search(r"Requests/sec:\s+([\d.]+)", text)
    if m:
        metrics["requests_per_sec"] = float(m.group(1))

    m = re.search(
        r"Socket errors: connect (\d+), read (\d+), write (\d+), timeout (\d+)", text
    )
    if m:
        metrics["socket_errors"] = {
            "connect": int(m.group(1)),
            "read": int(m.group(2)),
            "write": int(m.group(3)),
            "timeout": int(m.group(4)),
        }

    m = re.search(r"Non-2xx or 3xx responses:\s+(\d+)", text)
    if m:
        metrics["non_2xx"] = int(m.group(1))

    # thread-stats latency line (indented once, under "Thread Stats")
    m = re.search(r"^\s+Latency\s+([\d.]+)(us|ms|s)\s", text, re.MULTILINE)
    if m:
        metrics["thread_latency_avg_ms"] = round(_to_ms(m.group(1), m.group(2)), 4)

    # latency distribution: only lines like "  50%   23.83ms"
    for m in re.finditer(r"^\s*(\d{1,3})%\s+([\d.]+)(us|ms|s)\s*$", text, re.MULTILINE):
        pct = int(m.group(1))
        value = round(_to_ms(m.group(2), m.group(3)), 4)
        key = f"p{pct}"
        if key in metrics["latency_ms"]:
            metrics["latency_ms"][key] = value

    return metrics


COMBINED_HEADER = [
    "run_id",
    "run_started_at",
    "mode",
    "strategy",
    "threads",
    "connections",
    "duration_s",
    "wrk_requests",
    "wrk_requests_per_sec",
    "wrk_p50_ms",
    "wrk_p75_ms",
    "wrk_p90_ms",
    "wrk_p99_ms",
    "wrk_thread_latency_avg_ms",
    "wrk_socket_errors",
    "wrk_non_2xx",
    "proxy_requests_total",
    "proxy_responses_5xx",
    "proxy_errors",
    "proxy_status_counts",
    "proxy_p50_ms",
    "proxy_p95_ms",
    "proxy_p99_ms",
    "backend_shares",
]


def _flatten_socket_errors(errs: dict | None) -> str:
    if errs is None:
        return ""
    return "c:%d;r:%d;w:%d;t:%d" % (
        errs["connect"], errs["read"], errs["write"], errs["timeout"]
    )


def append_combined_csv(
    wrk: dict, snapshot: dict, path: Path, run_id: str, mode: str
) -> None:
    """Append one wrk+proxy combined row; header on the first call only."""
    path = Path(path)
    new = not path.exists()
    lat = snapshot["proxy"]["latency_ms"]
    counters = snapshot["proxy"]["counters"]
    shares = ";".join(
        f"{b['backend_id']}:{b['share_pct']}" for b in snapshot["backends"]
    )
    row = [
        run_id,
        snapshot["meta"]["run_started_at"],
        mode,
        snapshot["meta"]["strategy"],
        wrk["threads"],
        wrk["connections"],
        wrk["duration_s"],
        wrk["requests_total"],
        wrk["requests_per_sec"],
        wrk["latency_ms"]["p50"],
        wrk["latency_ms"]["p75"],
        wrk["latency_ms"]["p90"],
        wrk["latency_ms"]["p99"],
        wrk["thread_latency_avg_ms"],
        _flatten_socket_errors(wrk["socket_errors"]),
        wrk["non_2xx"],
        counters.get("requests_total", 0),
        counters.get("responses_5xx", 0),
        counters.get("errors", 0),
        _flatten_status_counts(counters.get("status_counts", {})),
        lat["p50"],
        lat["p95"],
        lat["p99"],
        shares,
    ]
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(COMBINED_HEADER)
        writer.writerow(row)
