"""Run snapshot collection and export (Phase 6).

One module owns the metric definitions the benchmark pipeline uses (see
docs/metric_definitions.md — the same definitions apply to HAProxy runs
in Phase 7):

- percentile(): linear interpolation, idx = (p/100) * (n-1) on the
  sorted sample list; None for an empty sample set.
- share_pct: a backend's delivered requests divided by the total
  delivered across ALL backends (a backend that served traffic before
  dying keeps its true share).
- summary CSV: one row per run; status_counts flattened into a single
  lossless "status:count;status:count" column so the column set stays
  stable regardless of which statuses a run produced.
- backends CSV: one row per (run, backend).
"""

import csv
import json
import time
from pathlib import Path


def percentile(samples: list[float], p: float) -> float | None:
    """p-th percentile with linear interpolation; None when no samples."""
    if not samples:
        return None
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    idx = (p / 100.0) * (len(ordered) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return ordered[lo] + frac * (ordered[hi] - ordered[lo])


def _latency_block(latencies_ms: list[float]) -> dict:
    return {
        "samples": len(latencies_ms),
        "mean_ms": (
            round(sum(latencies_ms) / len(latencies_ms), 3) if latencies_ms else None
        ),
        "p50": percentile(latencies_ms, 50),
        "p95": percentile(latencies_ms, 95),
        "p99": percentile(latencies_ms, 99),
    }


def collect_run_snapshot(
    proxy,
    registry,
    strategy_name: str,
    started_at: float | None = None,
) -> dict:
    """Gather one run's full metric picture from a live proxy + registry."""
    backends = registry.snapshot()
    total_delivered = sum(b["total_requests"] for b in backends)
    for b in backends:
        b["share_pct"] = (
            round(100.0 * b["total_requests"] / total_delivered, 2)
            if total_delivered
            else 0.0
        )
    meta = {
        "strategy": strategy_name,
        "run_started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
    }
    if started_at is not None:
        meta["duration_s"] = round(time.time() - started_at, 3)
    return {
        "meta": meta,
        "proxy": {
            "counters": dict(proxy.counters),
            "latency_ms": _latency_block(proxy.latencies_ms),
        },
        "backends": backends,
    }


def write_json(snapshot: dict, path: Path) -> None:
    Path(path).write_text(json.dumps(snapshot, indent=2), encoding="utf-8")


def _flatten_status_counts(counts: dict) -> str:
    return ";".join(f"{s}:{c}" for s, c in sorted(counts.items()))


SUMMARY_HEADER = [
    "run_id",
    "run_started_at",
    "strategy",
    "requests_total",
    "connections_total",
    "responses_5xx",
    "errors",
    "status_counts",
    "latency_samples",
    "p50_ms",
    "p95_ms",
    "p99_ms",
    "mean_ms",
]


def append_summary_csv(snapshot: dict, path: Path, run_id: str) -> None:
    """Append one summary row; writes the header on the first call only."""
    path = Path(path)
    new = not path.exists()
    lat = snapshot["proxy"]["latency_ms"]
    counters = snapshot["proxy"]["counters"]
    row = [
        run_id,
        snapshot["meta"]["run_started_at"],
        snapshot["meta"]["strategy"],
        counters.get("requests_total", 0),
        counters.get("connections_total", 0),
        counters.get("responses_5xx", 0),
        counters.get("errors", 0),
        _flatten_status_counts(counters.get("status_counts", {})),
        lat["samples"],
        lat["p50"],
        lat["p95"],
        lat["p99"],
        lat["mean_ms"],
    ]
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(SUMMARY_HEADER)
        writer.writerow(row)


BACKENDS_HEADER = [
    "run_id",
    "backend_id",
    "host",
    "port",
    "healthy",
    "requests",
    "active_requests",
    "share_pct",
    "transitions",
]


def _flatten_transitions(transitions: list[dict]) -> str:
    return "|".join(f"{e['state']}@{e['timestamp']:.3f}" for e in transitions)


def append_backends_csv(snapshot: dict, path: Path, run_id: str) -> None:
    """Append one row per backend; writes the header on the first call only."""
    path = Path(path)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(BACKENDS_HEADER)
        for b in snapshot["backends"]:
            writer.writerow(
                [
                    run_id,
                    b["backend_id"],
                    b["host"],
                    b["port"],
                    b["healthy"],
                    b["total_requests"],
                    b["active_requests"],
                    b["share_pct"],
                    _flatten_transitions(b["state_transitions"]),
                ]
            )
