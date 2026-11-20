"""Phase 9: derive report figures from the tracked Phase 8 dataset.

The report must not hand-copy numbers: every figure it quotes comes from this
script's `results/structured/report_data.csv`, which names its source file.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys_path = str(REPO_ROOT)

import sys

sys.path.insert(0, sys_path)

from load_balancer.metrics.wrk_parser import parse_wrk_output

STRUCTURED = REPO_ROOT / "results" / "structured"
RAW = REPO_ROOT / "results" / "raw"
FIELDS = ["metric", "mode", "strategy", "clients", "value", "evidence"]


def _read_csv(path: Path) -> list[dict]:
    return list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))


def evenness_pct(shares: dict[str, float]) -> float:
    """Largest absolute deviation from an even split (percent points)."""
    if not shares:
        return 0.0
    target = 100.0 / len(shares)
    return round(max(abs(value - target) for value in shares.values()), 2)


def max_share_pct(shares: dict[str, float]) -> float:
    return round(max(shares.values()), 2) if shares else 0.0


def parse_shares(text: str) -> dict[str, float]:
    shares = {}
    for part in (text or "").split(";"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        try:
            shares[key] = float(value)
        except ValueError:
            pass
    return shares


def read_wrk_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def parse_timeline(path: Path) -> dict:
    events = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        try:
            events[key] = int(value)
        except ValueError:
            try:
                events[key] = float(value)
            except ValueError:
                events[key] = value == "True" if value in ("True", "False") else value
    return events


def _row(metric: str, mode: str, strategy: str, clients, value, evidence: str) -> dict:
    return {
        "metric": metric, "mode": mode, "strategy": strategy,
        "clients": clients, "value": value, "evidence": evidence,
    }


def build_rows(medians: Path, failures: Path, keepalive: Path, close: Path,
               snapshot: Path, timelines: dict[str, Path]) -> list[dict]:
    rows: list[dict] = []
    for run in _read_csv(medians):
        shares = parse_shares(run["backend_shares"])
        mode, strategy, clients, evidence = (
            run["mode"], run["strategy"], run["clients"],
            f"results/structured/baseline_medians.csv:{run['run_id']}",
        )
        rows.append(_row("round_robin_evenness_pct", mode, strategy, clients,
                         evenness_pct(shares), evidence))
        rows.append(_row("max_backend_share_pct", mode, strategy, clients,
                         max_share_pct(shares), evidence))
        for metric in ("rps_median", "p50_median", "p75_median", "p90_median",
                       "p99_median", "responses_5xx_median", "lb_cpu_pct_mean",
                       "wrk_cpu_pct_mean"):
            rows.append(_row(metric, mode, strategy, clients, float(run[metric]),
                             evidence))

    for failure in _read_csv(failures):
        mode, strategy, clients = (
            failure["mode"], failure["strategy"], failure["clients"]
        )
        evidence = f"results/structured/failures.csv:{failure['run_id']}"
        for metric in ("detection_s", "failed_detection", "failed_after_kill_30s",
                       "wrk_non_2xx"):
            name = f"failure_{metric}" if metric == "detection_s" else metric
            rows.append(_row(name, mode, strategy, clients,
                             float(failure[metric]), evidence))
        rows.append(_row("recovery_s", mode, strategy, clients,
                         float(failure["recovery_s"]) if failure["recovery_s"] else None,
                         evidence))

    ka = parse_wrk_output(read_wrk_text(keepalive))
    cl = parse_wrk_output(read_wrk_text(close))
    ka_ev = "results/raw/phase3_wrk_keepalive.txt:load_balancer/tests/test_persistent_connection.py"
    cl_ev = "results/raw/phase3_wrk_close.txt"
    rows.append(_row("keepalive_rps_ratio", "custom", "keep_alive", "",
                     round(ka["requests_per_sec"] / cl["requests_per_sec"], 3), ka_ev))
    rows.append(_row("keepalive_p50_delta_ms", "custom", "keep_alive", "",
                     round(cl["latency_ms"]["p50"] - ka["latency_ms"]["p50"], 3),
                     f"{ka_ev};{cl_ev}"))
    rows.append(_row("requests_per_connection", "custom", "keep_alive", "",
                     round(ka["requests_total"] / ka["connections"], 2), ka_ev))
    snap = json.loads(snapshot.read_text(encoding="utf-8"))
    counters = snap["proxy"]["counters"]
    rows.append(_row("snapshot_requests_per_connection", "custom", "persistent", "",
                     round(counters["requests_total"] / counters["connections_total"], 2),
                     "results/raw/custom_20261120-151537-custom-round_robin-50-r1_snapshot.json"))

    for run_id, path in timelines.items():
        events = parse_timeline(path)
        for key in ("kill_at_s", "detection_s", "failed_detection",
                    "failed_after_kill_30s", "recovery_s"):
            if key in events:
                rows.append(_row(f"timeline_{key}", "", "", "", events[key],
                                 f"results/raw/{path.name}"))
        rows.append(_row("timeline_readmitted", "", "", "",
                         events.get("readmitted"), f"results/raw/{path.name}"))
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=STRUCTURED / "report_data.csv")
    args = parser.parse_args()
    failures = _read_csv(STRUCTURED / "failures.csv")
    timelines = {
        row["run_id"]: RAW / f"failure_{row['run_id']}.txt" for row in failures
    }
    timelines = {k: v for k, v in timelines.items() if v.exists()}
    rows = build_rows(
        STRUCTURED / "baseline_medians.csv",
        STRUCTURED / "failures.csv",
        RAW / "phase3_wrk_keepalive.txt",
        RAW / "phase3_wrk_close.txt",
        RAW / "custom_20261120-151537-custom-round_robin-50-r1_snapshot.json",
        timelines,
    )
    write_csv(args.out, rows)
    print(f"wrote {len(rows)} report figures to {args.out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
