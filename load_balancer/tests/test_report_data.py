"""Phase 9: report-data derivation tests."""

import csv
from pathlib import Path

from scripts import build_report_data as br


def test_evenness_is_the_largest_deviation_from_an_equal_share():
    assert br.evenness_pct({
        "backend-1": 25.0, "backend-2": 25.0,
        "backend-3": 25.0, "backend-4": 25.0,
    }) == 0.0
    assert br.evenness_pct({
        "backend-1": 30.0, "backend-2": 23.33,
        "backend-3": 23.33, "backend-4": 23.34,
    }) == 5.0


def test_max_share_detects_stickiness():
    assert br.max_share_pct({"backend-1": 25.0, "backend-2": 25.0,
                             "backend-3": 25.0, "backend-4": 25.0}) == 25.0
    assert br.max_share_pct({"backend-1": 100.0, "backend-2": 0.0}) == 100.0


def test_read_wrk_text_decodes_utf16_raw_files(tmp_path):
    path = tmp_path / "wrk.txt"
    path.write_text("100 requests in 8.01s\nRequests/sec: 12.50\n",
                    encoding="utf-16")
    text = br.read_wrk_text(path)
    assert "Requests/sec: 12.50" in text


def test_parse_timeline_extracts_key_events(tmp_path):
    path = tmp_path / "timeline.txt"
    path.write_text(
        "run_id=f1\ncase=custom-round_robin-50\nkill_at_s=30\nduration_s=60\n"
        "detection_s=4.7\nfailed_detection=158\nrecovery_s=61.2\nreadmitted=True\n",
        encoding="utf-8",
    )
    events = br.parse_timeline(path)
    assert events["kill_at_s"] == 30.0
    assert events["detection_s"] == 4.7
    assert events["failed_detection"] == 158
    assert events["readmitted"] is True


def test_build_rows_derives_evenness_failure_and_keepalive_metrics(tmp_path):
    medians = tmp_path / "baseline_medians.csv"
    with medians.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "run_id", "mode", "strategy", "clients", "rps_median",
            "p50_median", "p75_median", "p90_median", "p99_median",
            "responses_5xx_median", "lb_cpu_pct_mean", "wrk_cpu_pct_mean",
            "backend_shares",
        ])
        w.writeheader()
        w.writerow({
            "run_id": "median-custom-round_robin-50", "mode": "custom",
            "strategy": "round_robin", "clients": "50", "rps_median": "943",
            "p50_median": "31", "p75_median": "103", "p90_median": "198",
            "p99_median": "647", "responses_5xx_median": "49388",
            "lb_cpu_pct_mean": "84.3", "wrk_cpu_pct_mean": "5.3",
            "backend_shares": "backend-1:25;backend-2:25;backend-3:25;backend-4:25",
        })
    failures = tmp_path / "failures.csv"
    with failures.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "run_id", "mode", "strategy", "clients", "detection_s",
            "failed_detection", "failed_after_kill_30s", "wrk_non_2xx",
            "recovery_s", "readmitted",
        ])
        w.writeheader()
        w.writerow({
            "run_id": "f1", "mode": "custom", "strategy": "round_robin",
            "clients": "50", "detection_s": "4.7", "failed_detection": "158",
            "failed_after_kill_30s": "23460", "wrk_non_2xx": "24019",
            "recovery_s": "61.2", "readmitted": "True",
        })
    ka = tmp_path / "ka.txt"
    ka.write_text(
        "2 threads and 10 connections\n3402 requests in 8.01s\n"
        "Requests/sec:   424.77\n    Latency    23.50ms\n  50%   23.83ms\n", encoding="utf-16"
    )
    close = tmp_path / "close.txt"
    close.write_text(
        "2 threads and 10 connections\n2721 requests in 8.01s\n"
        "Requests/sec:   339.83\n    Latency    27.35ms\n  50%   27.31ms\n", encoding="utf-16"
    )
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        '{"proxy":{"counters":{"connections_total":50,"requests_total":59109}}}',
        encoding="utf-8",
    )
    rows = br.build_rows(medians, failures, ka, close, snapshot, {})
    by_metric = {(r["metric"], r["mode"], r["strategy"]): r["value"] for r in rows}
    assert by_metric[("round_robin_evenness_pct", "custom", "round_robin")] == 0.0
    assert by_metric[("failure_detection_s", "custom", "round_robin")] == 4.7
    assert by_metric[("failed_detection", "custom", "round_robin")] == 158
    assert round(by_metric[("keepalive_rps_ratio", "custom", "keep_alive")], 2) == 1.25
    assert by_metric[("keepalive_p50_delta_ms", "custom", "keep_alive")] == 3.48
    assert round(by_metric[("requests_per_connection", "custom", "keep_alive")], 1) == 340.2
    assert round(by_metric[("snapshot_requests_per_connection", "custom", "persistent")], 0) == 1182
