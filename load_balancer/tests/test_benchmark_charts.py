"""Phase 8: dataset aggregation and stdlib SVG chart tests."""

import csv
from pathlib import Path

from scripts import build_charts as bc
from scripts import benchmark_matrix as bm
from scripts.failure_benchmark import FAILURES_HEADER


def _write_csv(path: Path, header: list[str], rows: list[list]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def test_baseline_rows_join_manifest_to_combined_by_run_id(tmp_path):
    from load_balancer.metrics.wrk_parser import COMBINED_HEADER

    manifest = _write_csv(
        tmp_path / "runs.csv", bm.MANIFEST_HEADER,
        [
            ["r1", "baseline", "custom", "round_robin", 50, 4, 60, "a", "b", ""],
            ["r2", "baseline", "custom", "round_robin", 50, 4, 60, "a", "b", ""],
            ["r3", "smoke", "haproxy", "roundrobin", 50, 4, 8, "a", "b", ""],
        ],
    )

    def combined_row(run_id, rps, p99, status="200:100;503:5"):
        values = {name: "" for name in COMBINED_HEADER}
        values.update({
            "run_id": run_id,
            "wrk_requests": "1000",
            "wrk_requests_per_sec": str(rps),
            "wrk_p50_ms": "5",
            "wrk_p75_ms": "8",
            "wrk_p90_ms": "15",
            "wrk_p99_ms": str(p99),
            "proxy_responses_5xx": "2",
            "proxy_status_counts": status,
            "backend_shares": "backend-1:25.0",
        })
        return [values[name] for name in COMBINED_HEADER]

    combined = _write_csv(
        tmp_path / "combined.csv", COMBINED_HEADER,
        [combined_row("r1", 100.0, 20.0),
         combined_row("r2", 200.0, 40.0),
         combined_row("r3", 999.0, 99.0)],
    )
    rows = bc.baseline_rows(manifest, combined)
    assert len(rows) == 2
    by_id = {r["run_id"]: r for r in rows}
    assert by_id["r1"]["rps"] == 100.0
    # wrk prints 50/75/90/99 -- the old positional mapping reported p75/p90
    # as p95/p99; assert each percentile by column name.
    assert (by_id["r1"]["p50"], by_id["r1"]["p75"],
            by_id["r1"]["p90"], by_id["r1"]["p99"]) == (5.0, 8.0, 15.0, 20.0)
    assert by_id["r1"]["errors"] == 5  # 5xx class from proxy_status_counts


def test_aggregate_medians_uses_the_middle_run_per_case():
    rows = [
        {"mode": "custom", "strategy": "round_robin", "clients": 50, "threads": 4,
         "rps": 100.0, "p50": 5.0, "p75": 6.0, "p90": 7.0, "p95": 8.0, "p99": 10.0,
         "requests": 1000, "errors": 0,
         "shares": {"backend-1": 25.0, "backend-2": 25.0, "backend-3": 25.0, "backend-4": 25.0}},
        {"mode": "custom", "strategy": "round_robin", "clients": 50, "threads": 4,
         "rps": 300.0, "p50": 7.0, "p75": 9.0, "p90": 11.0, "p95": 12.0, "p99": 30.0,
         "requests": 3000, "errors": 2,
         "shares": {"backend-1": 25.0, "backend-2": 25.0, "backend-3": 25.0, "backend-4": 25.0}},
        {"mode": "custom", "strategy": "round_robin", "clients": 50, "threads": 4,
         "rps": 200.0, "p50": 6.0, "p75": 8.0, "p90": 10.0, "p95": 10.0, "p99": 20.0,
         "requests": 2000, "errors": 1,
         "shares": {"backend-1": 25.0, "backend-2": 25.0, "backend-3": 25.0, "backend-4": 25.0}},
    ]
    summary = bc.aggregate_medians(rows)
    assert len(summary) == 1
    row = summary[0]
    assert (row["runs"], row["rps"], row["p50"], row["p75"], row["p90"],
            row["p99"]) == (3, 200.0, 6.0, 8.0, 10.0, 20.0)
    assert row["errors"] == 1


def test_resource_means_separate_lb_from_wrk_generator(tmp_path):
    path = _write_csv(
        tmp_path / "resources.csv", bm.RESOURCES_HEADER,
        [
            ["r1", "custom", "round_robin", 50, "dump.json", 1, 0, 0, 10],
            ["r1", "custom", "round_robin", 50, "dump.json", 2, 2, 20, 14],
            ["r1", "custom", "round_robin", 50, "lb-wrk-r1", 1, 0, 4, 5],
        ],
    )
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    assert bc.mean_resource(rows, {"r1"}, role="lb") == (10.0, 12.0)
    assert bc.mean_resource(rows, {"r1"}, role="wrk") == (4.0, 5.0)


def test_failure_rows_are_parsed_from_the_tracked_csv(tmp_path):
    path = _write_csv(
        tmp_path / "failures.csv", FAILURES_HEADER,
        [["f1", "haproxy", "leastconn", 50, "backend-2", 30, 5.2, 3, 7, 0, 2.1, True]],
    )
    rows = bc.failure_rows(path)
    assert rows[0]["detection_s"] == "5.2"
    assert rows[0]["failed_detection"] == "3"
    assert rows[0]["readmitted"] == "True"


def test_bar_chart_emits_one_rect_per_value_and_labels():
    svg = bc.bar_chart_svg(
        "RPS", ["50", "200"], [("custom", [100, 200], "#123456"),
                              ("haproxy", [300, 400], "#abcdef")]
    )
    assert svg.startswith("<svg")
    assert "RPS" in svg
    assert svg.count("<rect") == 4
    assert "custom" in svg and "haproxy" in svg
    assert svg.endswith("</svg>")


def test_line_chart_emits_one_polyline_per_series():
    svg = bc.line_chart_svg(
        "p99", ["50", "200"], [("custom", [10, 20]), ("haproxy", [5, 8])]
    )
    assert svg.startswith("<svg")
    assert svg.count("<polyline") == 2
    assert "p99" in svg


def test_share_chart_uses_four_backends_from_snapshot(tmp_path):
    summary = [{
        "run_id": "r1", "mode": "custom", "strategy": "round_robin", "clients": 200,
        "shares": {"backend-1": 25.0, "backend-2": 25.0,
                   "backend-3": 25.0, "backend-4": 25.0},
    }]
    svg = bc.share_chart_svg(summary, clients=200)
    assert svg.count("<rect") == 4
    for backend in ("backend-1", "backend-2", "backend-3", "backend-4"):
        assert backend in svg


def test_tracked_dataset_invariants():
    from collections import Counter

    root = Path(__file__).resolve().parents[2] / "results" / "structured"
    manifest = [r for r in bc._read_csv(root / "benchmark_runs.csv")
                if r["phase"] == "baseline"]
    counts = Counter((r["mode"], r["strategy"], r["clients"]) for r in manifest)
    assert len(counts) == 18
    assert set(counts.values()) == {3}
    medians = bc._read_csv(root / "baseline_medians.csv")
    assert len(medians) == 18
    for row in medians:
        p50, p75, p90, p99 = (float(row[k]) for k in
                              ("p50_median", "p75_median", "p90_median", "p99_median"))
        assert p50 <= p75 <= p90 <= p99, row["run_id"]
