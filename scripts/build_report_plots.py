"""Phase 9: the two plots still missing from the Phase 8 chart set."""

from __future__ import annotations

import argparse
import csv
import html
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.build_charts import bar_chart_svg
from scripts.build_report_data import parse_timeline

STRUCTURED = REPO_ROOT / "results" / "structured"
RAW = REPO_ROOT / "results" / "raw"
PLOTS = REPO_ROOT / "results" / "plots"


def _read_csv(path: Path) -> list[dict]:
    return list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))


def timeline_cases(failures: Path, timelines: dict[str, Path]) -> list[dict]:
    cases = []
    for row in _read_csv(failures):
        run_id = row["run_id"]
        events = parse_timeline(timelines[run_id]) if run_id in timelines else {}
        cases.append({
            "label": events.get("case", f"{row['mode']}-{row['strategy']}-{row['clients']}"),
            "kill_at_s": float(row["kill_at_s"]),
            "detection_s": float(row["detection_s"] or 0),
            "duration_s": float(events.get("duration_s", 60)),
            "recovery_s": float(row["recovery_s"] or 0),
            "failed_detection": int(row["failed_detection"] or 0),
            "failed_after_kill_30s": int(row["failed_after_kill_30s"] or 0),
        })
    return cases


def event_timeline_svg(cases: list[dict]) -> str:
    """Event-based timeline: kill -> DOWN -> load end -> restart -> UP."""
    width, left, right, row_h = 1180, 210, 1160, 96
    height = 90 + row_h * max(len(cases), 1)
    axis_max = 130.0
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<text x="20" y="28" font-family="sans-serif" font-size="18">'
        'Backend failure timeline (events from raw failure_*.txt)</text>',
    ]
    for tick in range(0, 131, 10):
        x = left + (tick / axis_max) * (right - left)
        out.append(
            f'<line x1="{x:.1f}" y1="45" x2="{x:.1f}" y2="{height-20}" '
            f'stroke="#eee"/>'
        )
        out.append(
            f'<text x="{x:.1f}" y="58" text-anchor="middle" font-family="sans-serif" '
            f'font-size="10">{tick}s</text>'
        )

    def x_at(seconds: float) -> float:
        return left + (max(0.0, min(seconds, axis_max)) / axis_max) * (right - left)

    palette = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf"]
    for i, case in enumerate(cases):
        y = 95 + i * row_h
        color = palette[i % len(palette)]
        kill = case["kill_at_s"]
        down = kill + case["detection_s"]
        end = case["duration_s"]
        restart = end + 2  # raw timelines have no restart timestamp: illustrative
        up = restart + case["recovery_s"]  # recovery_s is measured from restart
        out.append(
            f'<text x="20" y="{y+5}" font-family="sans-serif" font-size="12">'
            f'{html.escape(str(case["label"]))}</text>'
        )
        out.append(
            f'<line x1="{x_at(0):.1f}" y1="{y}" x2="{x_at(max(up,end)+6):.1f}" '
            f'y2="{y}" stroke="{color}" stroke-width="3"/>'
        )
        markers = [
            (kill, f"kill t={kill:g}", "#d62728"),
            (down, f"DOWN +{case['detection_s']:g}s ({case['failed_detection']} 5xx)", "#ff7f0e"),
            (end, f"load end ({case['failed_after_kill_30s']} 5xx)", "#1f77b4"),
            (restart, "restart (illustrative +2s)", "#9467bd"),
            (up, f"UP +{case['recovery_s']:g}s after restart", "#2ca02c"),
        ]
        for seconds, label, marker_color in markers:
            x = x_at(seconds)
            out.append(f'<circle cx="{x:.1f}" cy="{y}" r="5" fill="{marker_color}"/>')
            out.append(
                f'<text x="{x:.1f}" y="{y-10}" text-anchor="middle" '
                f'font-family="sans-serif" font-size="9">{html.escape(label)}</text>'
            )
    out.append(
        f'<text x="20" y="{height-4}" font-family="sans-serif" font-size="10">'
        'Event-based: per-request timestamps were not captured; markers are kill/DOWN/'
        'load-end/restart/UP, not a continuous curve.</text>'
    )
    out.append("</svg>")
    return "\n".join(out)


def cpu_comparison_svg(medians: list[dict]) -> str:
    # Custom and HAProxy use different strategy names for the same intent;
    # normalise the axis so each group has two real values (no fabricated 0).
    aliases = {
        "round_robin": "round robin", "roundrobin": "round robin",
        "least_active_requests": "least active / leastconn",
        "leastconn": "least active / leastconn",
        "ip_hash": "ip hash / source", "source": "ip hash / source",
    }
    groups: dict[str, dict[str, list[float]]] = {}
    for row in medians:
        label = aliases.get(row["strategy"], row["strategy"])
        groups.setdefault(label, {"custom": [], "haproxy": []})
        groups[label][row["mode"]].append(float(row["lb_cpu_pct_mean"]))
    strategies = sorted(groups)
    series = []
    for mode, color, label in (
        ("custom", "#1f77b4", "custom (self-reported)"),
        ("haproxy", "#d62728", "haproxy (docker stats)"),
    ):
        values = [
            round(sum(groups[s][mode]) / len(groups[s][mode]), 2)
            if groups[s][mode] else 0.0
            for s in strategies
        ]
        series.append((label, values, color))
    svg = bar_chart_svg("Mean LB CPU by algorithm (% of one core)", strategies, series)
    caveat = (
        '<text x="70" y="416" font-family="sans-serif" font-size="10">'
        'Indicative only: custom CPU is self-reported process_time(); HAProxy/wrk use'
        ' docker stats. Different accounting models.</text>'
    )
    return svg.replace("</svg>", caveat + "\n</svg>")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=PLOTS)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    failures = STRUCTURED / "failures.csv"
    timelines = {
        row["run_id"]: RAW / f"failure_{row['run_id']}.txt"
        for row in _read_csv(failures)
    }
    cases = timeline_cases(failures, {k: v for k, v in timelines.items() if v.exists()})
    (args.out / "failure_timeline.svg").write_text(
        event_timeline_svg(cases), encoding="utf-8"
    )
    (args.out / "cpu_comparison.svg").write_text(
        cpu_comparison_svg(_read_csv(STRUCTURED / "baseline_medians.csv")),
        encoding="utf-8",
    )
    print(f"wrote failure_timeline.svg ({len(cases)} cases) and cpu_comparison.svg")
    return 0


if __name__ == "__main__":
    sys.exit(main())
