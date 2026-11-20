"""Phase 9: failure-timeline and CPU-comparison plot tests."""

from scripts import build_report_plots as rp


def test_event_timeline_marks_kill_down_and_recovery_for_each_case():
    cases = [{
        "label": "custom-round_robin-50", "kill_at_s": 30, "detection_s": 4.7,
        "duration_s": 60, "recovery_s": 61.2, "failed_detection": 158,
        "failed_after_kill_30s": 23460,
    }]
    svg = rp.event_timeline_svg(cases)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "custom-round_robin-50" in svg
    assert "kill" in svg and "DOWN" in svg and "UP" in svg
    assert "158" in svg and "23460" in svg
    # one horizontal lifeline plus markers at kill / DOWN / load end / restart / UP
    assert svg.count("<circle") >= 5


def test_cpu_comparison_groups_by_strategy_and_shows_the_caveat():
    medians = [
        {"mode": "custom", "strategy": "round_robin", "clients": "50",
         "lb_cpu_pct_mean": "84.3", "wrk_cpu_pct_mean": "5.3"},
        {"mode": "haproxy", "strategy": "roundrobin", "clients": "50",
         "lb_cpu_pct_mean": "164.6", "wrk_cpu_pct_mean": "33.6"},
    ]
    svg = rp.cpu_comparison_svg(medians)
    assert svg.startswith("<svg")
    assert svg.count("<rect") == 4  # 2 strategies x 2 implementations
    assert "self-reported" in svg and "docker stats" in svg
    assert "Indicative only" in svg


def test_timeline_cases_are_derived_from_failures_and_raw_timelines(tmp_path):
    failures = tmp_path / "failures.csv"
    failures.write_text(
        "run_id,mode,strategy,clients,kill_at_s,detection_s,recovery_s,"
        "failed_detection,failed_after_kill_30s\n"
        "f1,custom,round_robin,50,30,4.7,61.2,158,23460\n",
        encoding="utf-8",
    )
    timeline = tmp_path / "failure_f1.txt"
    timeline.write_text(
        "run_id=f1\ncase=custom-round_robin-50\nkill_at_s=30\nduration_s=60\n"
        "detection_s=4.7\nrecovery_s=61.2\n", encoding="utf-8",
    )
    cases = rp.timeline_cases(failures, {"f1": timeline})
    assert len(cases) == 1
    assert cases[0]["label"] == "custom-round_robin-50"
    assert cases[0]["failed_detection"] == 158
    assert cases[0]["duration_s"] == 60
