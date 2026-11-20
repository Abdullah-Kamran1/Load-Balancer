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
    manifest = _write_csv(
        tmp_path / "runs.csv", bm.MANIFEST_HEADER,
        [
            ["r1", "baseline", "custom", "round_robin", 50, 4, 60, "a", "b", ""],
            ["r2", "baseline", "custom", "round_robin", 50, 4, 60, "a", "b", ""],
            ["r3", "smoke", "haproxy", "roundrobin", 50, 4, 8, "a", "b", ""],
        ],
    )

    def combined_row(run_id, rps, p99):
        row = [""] * 24
        row[0] = run_id
        row[7] = "1000"
        row[8] = str(rps)
        row[9] = str(p99 / 2)
        row[10] = str(p99 * 0.8)
        row[11] = str(p99)
        return row

    combined = _write_csv(
        tmp_path / "combined.csv", __import__(
            "load_balancer.metrics.wrk_parser", fromlist=["COMBINED_HEADER"]
        ).COMBINED_HEADER,
        [combined_row("r1", 100.0, 10.0),
         combined_row("r2", 200.0, 20.0),
         combined_row("r3", 999.0, 99.0)],
    )
    rows = bc.baseline_rows(manifest, combined)
    assert len(rows) == 2
    assert {(r["mode"], r["rps"], r["p99"]) for r in rows} == {
        ("custom", 100.0, 10.0), ("custom", 200.0, 20.0)
    }


def test_aggregate_medians_uses_the_middle_run_per_case():
    rows = [
        {"mode": "custom", "strategy": "round_robin", "clients": 50, "threads": 4,
         "rps": 100.0, "p50": 5.0, "p95": 8.0, "p99": 10.0,
         "requests": 1000, "errors": 0,
         "shares": {"backend-1": 25.0, "backend-2": 25.0, "backend-3": 25.0, "backend-4": 25.0}},
        {"mode": "custom", "strategy": "round_robin", "clients": 50, "threads": 4,
         "rps": 300.0, "p50": 7.0, "p95": 12.0, "p99": 30.0,
         "requests": 3000, "errors": 2,
         "shares": {"backend-1": 25.0, "backend-2": 25.0, "backend-3": 25.0, "backend-4": 25.0}},
        {"mode": "custom", "strategy": "round_robin", "clients": 50, "threads": 4,
         "rps": 200.0, "p50": 6.0, "p95": 10.0, "p99": 20.0,
         "requests": 2000, "errors": 1,
         "shares": {"backend-1": 25.0, "backend-2": 25.0, "backend-3": 25.0, "backend-4": 25.0}},
    ]
    summary = bc.aggregate_medians(rows)
    assert len(summary) == 1
    row = summary[0]
    assert (row["runs"], row["rps"], row["p50"], row["p95"], row["p99"]) == (
        3, 200.0, 6.0, 10.0, 20.0
    )
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
        [["f1", "haproxy", "leastconn", 200, "backend-2", 30, 5.2, 7, 0, 2.1, True]],
    )
    rows = bc.failure_rows(path)
    assert rows[0]["detection_s"] == "5.2"
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
