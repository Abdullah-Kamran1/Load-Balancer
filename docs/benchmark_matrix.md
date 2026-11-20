# Phase 8 benchmark matrix

This document records how the Phase 8 dataset was produced and how to
regenerate it. The plan's baseline matrix, failure test and metric list are
in `load_balancer_project_plan.md` (lines 503-562).

## Environment

| Item | Value |
|---|---|
| Host | 12th Gen Intel Core i7-12700H, 20 logical CPUs, 15.7 GiB RAM |
| Python | 3.15.2 (`.venv`), asyncio single-thread custom proxy |
| Docker | 29.9.3 client/server, Desktop on Windows 11 |
| HAProxy | `haproxy:3.4-alpine@sha256:b9fe316f…` → 3.4.7-8872a26 (per-run `haproxy_environment_<run_id>.txt`) |
| Backend farm | same four containers as Phases 1-7: 25 ms/16, 100 ms/8, 300 ms/4, 50 ms/6 slots |
| Workload | `GET /work?delay_ms=25` (fixed path + fixed artificial delay) |
| Duration | 60 s measured per run, 10 s warm-up per run, 3 runs per case, median reported |

`results/structured/benchmark_environment.txt` is regenerated with the
dataset and holds the exact versions/config hashes used.

## Cases and controls

18 baseline cases: {custom proxy, HAProxy} × {3 algorithms} ×
{50, 200, 500 clients}. Algorithms: custom `round_robin`,
`least_active_requests`, `ip_hash`; HAProxy `roundrobin`, `leastconn`,
`source` (:8091/:8092/:8093). Their semantics are **not** claimed equivalent
(see `docs/haproxy_setup.md`).

- One load balancer serves traffic at a time; each case starts from a fresh
  process/container so proxy counters begin at zero. For HAProxy a warm-up
  run is followed by a restart before the measured run (stats are cumulative).
- wrk runs in the local `lb/wrk` container against `host.docker.internal`.
  Threads: 4 at 50/200 clients, 8 at 500. Its CPU is sampled alongside the
  LB's; low wrk CPU (<42% of one core) confirms the generator is not the
  bottleneck.
- Fixed request path/payload/delay, same machine and Docker configuration for
  both LBs, identical health checks/timeouts (see `docs/haproxy_setup.md`).
- CPU is sampled every 2 s: the custom proxy reports its own
  `process_time()`/RSS in its 1 s metrics dump (Windows reports 0 CPU for
  harness-spawned processes via Get-Process/WMI); HAProxy/wrk use
  `docker stats`. The units are both "% of one core" and MiB but the
  accounting mechanisms differ; compare cautiously.
- Raw wrk output, proxy snapshots and resources stay on disk under
  `results/raw/` (gitignored) so charts can be regenerated; summaries and
  per-run environment evidence are tracked under `results/structured/`.

## Reproduce

```powershell
# farm must be up; Docker Desktop must be running
.\.venv\Scripts\python.exe scripts\benchmark_matrix.py --dry-run
.\.venv\Scripts\python.exe scripts\benchmark_matrix.py --phase baseline --runs 3

# a single case/run (cases are listed by --dry-run, 1-based --start)
.\.venv\Scripts\python.exe scripts\benchmark_matrix.py --phase baseline --start 2 --limit 1 --runs 1

# timed failure + recovery: 60 s load, backend-2 killed at t=30 s
.\.venv\Scripts\python.exe scripts\failure_benchmark.py

# medians, environment and SVG charts
.\.venv\Scripts\python.exe scripts\build_charts.py
```

Outputs:
- `results/structured/benchmark_runs.csv` — manifest linking each run id to
  its case and raw files;
- `results/structured/combined.csv`, `backends.csv` — per-run proxy metrics;
- `results/structured/resources.csv` — LB/wrk CPU and memory samples;
- `results/structured/baseline_medians.csv` — one median row per case;
- `results/structured/failures.csv` — detection/failure/recovery per case;
- `results/plots/*.svg` — comparison charts (tracked deliverables);
- `results/raw/benchmark_baseline.log`, `benchmark_errors.csv`,
  `failure_<run_id>.txt`, `wrk_<run_id>.txt`, `snapshot_<run_id>.json`.

## Failure and recovery test

For each of the six mode/algorithm pairs at **50 clients**, a 60 s wrk run is
started, `lb-backend-2` is killed at exactly t=30 s, and the load continues.
A watcher records the first DOWN/UP from the proxy's health state (custom
metrics dump) or the HAProxy stats CSV; HAProxy transitional states
(`UP 2/3`, `DOWN 1/2`) are handled as documented in `docs/haproxy_setup.md`.
The watcher is armed only at the kill, so a stale DOWN before the run cannot
produce a negative detection time. `failed_detection` is the 5xx-class delta
from the kill to the first DOWN; `failed_after_kill_30s` is the delta over the
whole remaining run (both are reported because most 5xx are capacity 503s).
wrk's `non-2xx` total is recorded as a cross-check.

After the run the backend is restarted in a separate recovery phase: direct
`/health`, all backends healthy again, LB health UP, and **40** follow-up
requests confirm re-admission. For sticky algorithms (`ip_hash`/`source`) the
observed health state decides re-admission, because one client IP need not
map to the recovered server. The victim and timing are identical across both
LB implementations.

## Results

All values are medians of three 60 s runs. "5xx-class median" is summed from
`proxy_status_counts` for both LBs (custom emits exact statuses such as
`503:…`, HAProxy emits the `5xx:…` class). It includes forwarded backend 503s
for both implementations; `proxy_responses_5xx` is not used because for the
custom proxy it counts only proxy-generated errors. The farm's 34 concurrency
slots saturate under wrk, so most 5xx are backend 503s, not LB failures.

| Mode | Strategy | Clients | RPS median | p50 ms | p75 ms | p90 ms | p99 ms | 5xx-class median | LB CPU% | wrk CPU% |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| custom | ip_hash | 50 | 360.05 | 128.43 | 195.71 | 296.47 | 558.06 | 16623 | 86.8 | 2.09 |
| custom | ip_hash | 200 | 389.8 | 479.99 | 715.98 | 1020.0 | 1650.0 | 20876 | 79.11 | 2.62 |
| custom | ip_hash | 500 | 129.35 | 236.27 | 286.06 | 388.07 | 1960.0 | 7061 | 16.2 | 20.52 |
| custom | least_active_requests | 50 | 518.95 | 99.41 | 182.46 | 289.76 | 559.62 | 21302 | 86.72 | 2.62 |
| custom | least_active_requests | 200 | 488.57 | 128.64 | 649.14 | 1050.0 | 1750.0 | 25815 | 78.93 | 3.03 |
| custom | least_active_requests | 500 | 180.24 | 265.47 | 321.61 | 430.22 | 1860.0 | 8455 | 18.69 | 22.32 |
| custom | round_robin | 50 | 943.07 | 31.7 | 103.73 | 198.42 | 647.4 | 49388 | 84.31 | 5.3 |
| custom | round_robin | 200 | 242.68 | 553.23 | 600.58 | 644.85 | 1480.0 | 6038 | 59.34 | 4.48 |
| custom | round_robin | 500 | 171.1 | 257.25 | 337.65 | 801.57 | 1840.0 | 7599 | 20.74 | 27.16 |
| haproxy | leastconn | 50 | 3585.22 | 8.29 | 20.22 | 31.49 | 35.39 | 152629 | 170.31 | 33.13 |
| haproxy | leastconn | 200 | 3726.16 | 48.25 | 65.46 | 72.65 | 81.46 | 150851 | 203.99 | 39.14 |
| haproxy | leastconn | 500 | 3599.87 | 133.05 | 148.88 | 158.84 | 181.77 | 143832 | 194.45 | 41.27 |
| haproxy | roundrobin | 50 | 3698.23 | 14.14 | 27.43 | 30.0 | 36.27 | 146566 | 164.57 | 33.57 |
| haproxy | roundrobin | 200 | 4137.79 | 44.93 | 86.45 | 124.04 | 167.11 | 173841 | 186.15 | 36.7 |
| haproxy | roundrobin | 500 | 4107.09 | 97.61 | 279.18 | 394.0 | 461.64 | 172164 | 186.25 | 40.21 |
| haproxy | source | 50 | 1784.65 | 22.49 | 24.54 | 47.66 | 51.61 | 88809 | 56.27 | 13.04 |
| haproxy | source | 200 | 1904.72 | 101.84 | 109.8 | 125.47 | 144.39 | 96068 | 57.67 | 13.32 |
| haproxy | source | 500 | 1790.7 | 270.48 | 285.85 | 298.16 | 331.17 | 89282 | 58.16 | 14.12 |

**p95 caveat:** wrk `--latency` prints only p50/p75/p90/p99. The p95 named in
the plan has no common client-side source, so the tables report the four
percentiles wrk actually emits (p90 is the closest below p95). Custom
snapshots contain a proxy-internal p95, but HAProxy's stats expose no
equivalent percentile, so it is not used for comparison.

Backend shares (`baseline_medians.csv`) show round robin and
least-active/leastconn near 25% each, while custom `ip_hash` pins one client
IP and HAProxy `source` pins 100% to backend-2. These are different hash
functions; no equivalence is claimed.

### Failure and recovery (50 clients, backend-2 killed at t=30 s)

| Mode | Strategy | Detection s | 5xx window | 5xx 30 s | wrk non-2xx | Recovery s | Re-admitted |
|---|---|---:|---:|---:|---:|---:|---|
| custom | round_robin | 4.721 | 158 | 23460 | 24019 | 61.207 | True |
| custom | least_active_requests | 5.687 | 290 | 16122 | 17173 | 57.082 | True |
| custom | ip_hash | 5.944 | 366 | 11410 | 14241 | 4.01 | True |
| haproxy | roundrobin | 4.785 | 1678 | 50146 | 103586 | 2.468 | True |
| haproxy | leastconn | 4.418 | 10488 | 57196 | 115294 | 1.947 | True |
| haproxy | source | 4.51 | 4429 | 38550 | 74688 | 1.944 | True |

"5xx window" is the 5xx-class delta from the kill to the first DOWN;
"5xx 30 s" is the delta over the rest of the run. Both are dominated by
capacity 503s; the window figure is the closer analogue of "failures during
backend failure". Detection is 4.4-5.9 s for both implementations; HAProxy
recovers its health state in ~2 s while the custom proxy's post-load health
probes took 4-61 s. All six cases re-admitted the recovered backend.

### Interpretation limits

- The farm is intentionally small. At every tested concurrency wrk pushes
  more traffic than the backends can serve, so throughput mostly measures how
  quickly 503s are returned. This dataset is a **saturation** comparison;
  Phase 9 should not read it as a pure proxy-efficiency comparison.
- HAProxy is multi-threaded and runs in a container; the custom proxy is one
  asyncio host process. CPU percentages are indicative, not directly
  comparable (self-reported `process_time()` vs `docker stats`).
- wrk CPU stayed below ~42% of one core, so the load generator was not the
  bottleneck. Python/Docker/HAProxy versions, machine specs and config hashes
  are in `results/structured/benchmark_environment.txt`.

### Charts

`results/plots/`: `rps_by_load.svg`, `p50_by_load.svg`, `p75_by_load.svg`,
`p90_by_load.svg`, `p99_by_load.svg`, `backend_shares.svg`,
`failure_detection.svg`, `failure_requests.svg`, `failure_detection_window.svg`.
