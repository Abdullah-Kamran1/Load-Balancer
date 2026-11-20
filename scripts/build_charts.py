"""Phase 8: aggregate benchmark runs into median tables and SVG charts.

Reads the manifest + tracked CSVs and writes:
  results/structured/baseline_medians.csv
  results/structured/benchmark_environment.txt
  results/plots/*.svg

No third-party plotting dependency: charts are hand-built SVG (stdlib only),
so they are reproducible on this Windows host without matplotlib.
"""

from __future__ import annotations

import csv
import hashlib
import html
import statistics
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from load_balancer.metrics.wrk_parser import COMBINED_HEADER
from scripts import benchmark_matrix as bm

STRUCTURED = REPO_ROOT / "results" / "structured"
PLOTS = REPO_ROOT / "results" / "plots"
BACKENDS = [f"backend-{i}" for i in range(1, 5)]
COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd",
          "#ff7f0e", "#8c564b", "#17becf", "#e377c2"]

MEDIANS_HEADER = [
    "run_id", "mode", "strategy", "clients", "runs", "threads",
    "rps_median", "p50_median", "p95_median", "p99_median",
    "proxy_requests_median", "responses_5xx_median",
    "lb_cpu_pct_mean", "lb_mem_mib_mean", "wrk_cpu_pct_mean", "backend_shares",
]


def _read_csv(path: Path) -> list[dict]:
    return list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))


def _parse_shares(text: str) -> dict[str, float]:
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


def baseline_rows(manifest: Path, combined: Path) -> list[dict]:
    by_run = {row[0]: row for row in _raw_rows(combined)}
    rows = []
    for record in _read_csv(manifest):
        if record["phase"] != "baseline" or record["run_id"] not in by_run:
            continue
        wrk = by_run[record["run_id"]]
        rows.append({
            "run_id": record["run_id"],
            "mode": record["mode"],
            "strategy": record["strategy"],
            "clients": int(record["clients"]),
            "threads": int(record["threads"]),
            "rps": float(wrk[8]),
            "p50": float(wrk[9]),
            "p95": float(wrk[10]),
            "p99": float(wrk[11]),
            "requests": int(wrk[7]),
            "errors": int(wrk[17] or 0),
            "shares": _parse_shares(wrk[23] if len(wrk) > 23 else ""),
        })
    return rows


def _raw_rows(path: Path) -> list[list[str]]:
    return list(csv.reader(path.read_text(encoding="utf-8").splitlines()))[1:]


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 3)


def aggregate_medians(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        groups.setdefault((row["mode"], row["strategy"], row["clients"]), []).append(row)
    summary = []
    for (mode, strategy, clients), group in sorted(groups.items()):
        summary.append({
            "run_id": f"median-{mode}-{strategy}-{clients}",
            "mode": mode, "strategy": strategy, "clients": clients,
            "runs": len(group), "threads": group[0]["threads"],
            "rps": _median([r["rps"] for r in group]),
            "p50": _median([r["p50"] for r in group]),
            "p95": _median([r["p95"] for r in group]),
            "p99": _median([r["p99"] for r in group]),
            "requests": _median([r["requests"] for r in group]),
            "errors": _median([r["errors"] for r in group]),
            "shares": group[0]["shares"],
        })
    return summary


def mean_resource(rows: list[dict], run_ids: set[str], role: str) -> tuple:
    selected = [
        r for r in rows
        if r["run_id"] in run_ids
        and (r["target"].startswith("lb-wrk-") if role == "wrk"
             else not r["target"].startswith("lb-wrk-"))
    ]
    if not selected:
        return None, None
    return (
        round(statistics.mean(float(r["cpu_pct"]) for r in selected), 2),
        round(statistics.mean(float(r["mem_mib"]) for r in selected), 2),
    )


def failure_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return _read_csv(path)


# --- stdlib SVG ---------------------------------------------------------

def _svg_open(title: str, width: int = 900, height: int = 420) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        f'<text x="20" y="28" font-family="sans-serif" font-size="18">{html.escape(title)}</text>',
    ]


def bar_chart_svg(title: str, categories: list[str], series: list[tuple]) -> str:
    """series: (label, values, color) tuples; one rect per value."""
    width, height = 900, 420
    left, bottom = 70, height - 60
    plot_h = bottom - 60
    out = _svg_open(title, width, height)
    values = [v for _, vals, _ in series for v in vals]
    max_v = max(values) if values else 1.0
    max_v = max_v * 1.15 or 1.0
    out += [
        f'<line x1="{left}" y1="50" x2="{left}" y2="{bottom}" stroke="#333"/>',
        f'<line x1="{left}" y1="{bottom}" x2="{width-20}" y2="{bottom}" stroke="#333"/>',
        f'<text x="20" y="55" font-family="sans-serif" font-size="11">0</text>',
        f'<text x="10" y="60" font-family="sans-serif" font-size="11">{max_v:.0f}</text>',
    ]
    group_w = (width - left - 40) / max(len(categories), 1)
    bar_w = group_w / (len(series) + 1)
    for ci, category in enumerate(categories):
        x0 = left + ci * group_w
        for si, (label, vals, color) in enumerate(series):
            value = vals[ci] if ci < len(vals) else 0
            bar_h = (value / max_v) * plot_h
            x = x0 + 8 + si * bar_w
            out.append(
                f'<rect x="{x:.1f}" y="{bottom-bar_h:.1f}" width="{bar_w*0.8:.1f}" '
                f'height="{bar_h:.1f}" fill="{color}"><title>'
                f'{html.escape(label)} {html.escape(str(category))}: {value:g}</title></rect>'
            )
        out.append(
            f'<text x="{x0+group_w/2:.1f}" y="{bottom+18}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="12">{html.escape(str(category))}</text>'
        )
    for si, (label, _, color) in enumerate(series):
        x = left + si * 160
        out.append(f'<circle cx="{x}" cy="{height-22}" r="6" fill="{color}"/>')
        out.append(
            f'<text x="{x+12}" y="{height-18}" font-family="sans-serif" '
            f'font-size="12">{html.escape(label)}</text>'
        )
    out.append("</svg>")
    return "\n".join(out)


def line_chart_svg(title: str, categories: list[str], series: list[tuple]) -> str:
    """series: (label, values) tuples; one polyline per series."""
    width, height = 900, 420
    left, right, top, bottom = 70, width - 20, 60, height - 60
    out = _svg_open(title, width, height)
    values = [v for _, vals in series for v in vals]
    max_v = max(values) if values else 1.0
    max_v = max_v * 1.15 or 1.0
    out += [
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="#333"/>',
        f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="#333"/>',
    ]
    for ci, category in enumerate(categories):
        x = left + ci * (right - left) / max(len(categories) - 1, 1)
        out.append(
            f'<text x="{x:.1f}" y="{bottom+18}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="12">{html.escape(str(category))}</text>'
        )
    for si, (label, vals) in enumerate(series):
        points = []
        for ci, value in enumerate(vals):
            x = left + ci * (right - left) / max(len(categories) - 1, 1)
            y = bottom - (value / max_v) * (bottom - top)
            points.append(f"{x:.1f},{y:.1f}")
        color = COLORS[si % len(COLORS)]
        out.append(
            f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" '
            f'stroke-width="2"/>'
        )
        out.append(f'<circle cx="{left + si*160}" cy="{height-22}" r="6" fill="{color}"/>')
        out.append(
            f'<text x="{left + si*160 + 12}" y="{height-18}" font-family="sans-serif" '
            f'font-size="12">{html.escape(label)}</text>'
        )
    out.append("</svg>")
    return "\n".join(out)


def share_chart_svg(summary: list[dict], clients: int) -> str:
    rows = [r for r in summary if r["clients"] == clients]
    series = [
        (f"{r['mode']}-{r['strategy']}",
         [r["shares"].get(b, 0.0) for b in BACKENDS], COLORS[i % len(COLORS)])
        for i, r in enumerate(rows)
    ]
    return bar_chart_svg(f"Backend request share (%) at {clients} clients",
                         BACKENDS, series)


# --- environment evidence ----------------------------------------------

def write_environment(path: Path) -> None:
    def ps(command: str) -> str:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip()

    haproxy_env = sorted(STRUCTURED.glob("haproxy_environment_*.txt"))
    haproxy_version = "not captured"
    if haproxy_env:
        for line in haproxy_env[-1].read_text(encoding="utf-8").splitlines():
            if line.startswith("haproxy_image="):
                haproxy_version = line.split("=", 1)[1]
                break
    cfg = REPO_ROOT / "haproxy" / "haproxy.cfg"
    lines = [
        f"captured_at={__import__('datetime').datetime.now().isoformat(timespec='seconds')}",
        f"python_version={sys.version.split()[0]}",
        f"docker_version={ps('docker version --format client={{.Client.Version}}')}",
        f"cpu={ps('(Get-CimInstance Win32_Processor | Select-Object -First 1).Name')}",
        f"logical_cpus={ps('(Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors')}",
        f"ram_gib={ps('[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,2)')}",
        f"haproxy_image={haproxy_version}",
        f"haproxy_config_sha256={hashlib.sha256(cfg.read_bytes()).hexdigest()}",
        "workload=GET /work?delay_ms=25",
        "farm=backend-1 25ms/16, backend-2 100ms/8, backend-3 300ms/4, backend-4 50ms/6",
        "custom_proxy=host process on 127.0.0.1:8080, asyncio single thread",
        "haproxy=container lb-haproxy, 3 frontends :8091/:8092/:8093, stats :8404",
        "cpu_note=custom proxy CPU is self-reported process_time delta; "
        "containers use docker stats; neither is normalised across hosts",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    rows = baseline_rows(STRUCTURED / "benchmark_runs.csv", STRUCTURED / "combined.csv")
    summary = aggregate_medians(rows)
    resources = _read_csv(STRUCTURED / "resources.csv") if (
        STRUCTURED / "resources.csv"
    ).exists() else []
    resource_by_case: dict[tuple, list[dict]] = {}
    for row in resources:
        resource_by_case.setdefault((row["mode"], row["strategy"], int(row["clients"])),
                                    []).append(row)
    run_ids = {r["run_id"] for r in rows}
    lb_means: dict[tuple, tuple] = {}
    for key, group in resource_by_case.items():
        lb_means[key] = mean_resource(group, run_ids & {r["run_id"] for r in group}, "lb")
        wrk_means = mean_resource(group, run_ids & {r["run_id"] for r in group}, "wrk")
        lb_means[(key[0], key[1], key[2], "wrk")] = wrk_means

    out_rows = []
    for row in summary:
        key = (row["mode"], row["strategy"], row["clients"])
        lb_cpu, lb_mem = lb_means.get(key, (None, None))
        wrk_cpu, _ = lb_means.get((*key, "wrk"), (None, None))
        out_rows.append([
            row["run_id"], row["mode"], row["strategy"], row["clients"], row["runs"],
            row["threads"], row["rps"], row["p50"], row["p95"], row["p99"],
            row["requests"], row["errors"], lb_cpu, lb_mem, wrk_cpu,
            ";".join(f"{b}:{row['shares'].get(b, 0.0)}" for b in BACKENDS),
        ])
    path = STRUCTED = STRUCTURED / "baseline_medians.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(MEDIANS_HEADER)
        writer.writerows(out_rows)

    write_environment(STRUCTURED / "benchmark_environment.txt")
    PLOTS.mkdir(parents=True, exist_ok=True)
    categories = ["50", "200", "500"]
    for metric in ("rps", "p50", "p95", "p99"):
        series = []
        for mode in ("custom", "haproxy"):
            for strategy in sorted({r["strategy"] for r in summary if r["mode"] == mode}):
                values = [
                    next((r[metric] for r in summary
                          if r["mode"] == mode and r["strategy"] == strategy
                          and r["clients"] == int(c)), 0)
                    for c in categories
                ]
                series.append((f"{mode}-{strategy}", values))
        title = {"rps": "Median requests/sec", "p50": "Median p50 latency (ms)",
                 "p95": "Median p95 latency (ms)", "p99": "Median p99 latency (ms)"}[metric]
        (PLOTS / f"{metric}_by_load.svg").write_text(
            line_chart_svg(title, categories, series), encoding="utf-8"
        )
    (PLOTS / "backend_shares.svg").write_text(
        share_chart_svg(summary, 200), encoding="utf-8"
    )

    failures = failure_rows(STRUCTURED / "failures.csv")
    if failures:
        labels = [f"{r['mode']}-{r['strategy']}" for r in failures]
        (PLOTS / "failure_detection.svg").write_text(
            bar_chart_svg("Backend failure detection time (s)", labels,
                          [("detection_s",
                            [float(r["detection_s"] or 0) for r in failures], "#d62728")]),
            encoding="utf-8",
        )
        (PLOTS / "failure_requests.svg").write_text(
            bar_chart_svg("Failed requests during backend failure", labels,
                          [("5xx/502/503",
                            [float(r["failed_requests"] or 0) for r in failures], "#1f77b4")]),
            encoding="utf-8",
        )
    print(f"wrote {len(out_rows)} median rows, {len(list(PLOTS.glob('*.svg')))} charts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
