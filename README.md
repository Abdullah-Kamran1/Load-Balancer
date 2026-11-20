# Async Load Balancer

An **educational L7 HTTP reverse proxy** built with Python `asyncio` and
`h11`, compared against **HAProxy 3.4** under a controlled benchmark matrix.
The project deliberately keeps the proxy small enough to read end-to-end, then
measures how its three selection strategies behave against a mature load
balancer on an intentionally unequal backend farm.

## Introduction

### What this project demonstrates

- A real HTTP/1.1 reverse proxy: request/response framing, persistent client
  connections, hop-by-hop header handling, bounded bodies, timeouts, and
  400/413/502/503 error paths.
- Three selection strategies behind one interface:
  **round robin**, **least active requests**, and **IP hash**.
- Active health checking: `GET /health` every second, DOWN after 3 failures,
  re-admission after 2 successes, with timestamped state transitions.
- Observability: per-status counters, proxy-side latency samples, atomic JSON
  snapshots, and a shared 24-column CSV schema used by both implementations.
- A fair-comparison harness: 18 cases (2 implementations × 3 algorithms ×
  50/200/500 clients), 3 × 60 s runs per case, a timed backend-kill test,
  median tables, SVG charts, and a final report that answers the plan's
  ten analysis questions.

### Architecture snapshot

| Piece | Role |
|---|---|
| `backend/` | Four FastAPI backends with different delay/concurrency capacities (25 ms/16, 100 ms/8, 300 ms/4, 50 ms/6). |
| `load_balancer/` | asyncio proxy, algorithms, health monitor, metrics package. |
| `haproxy/` | HAProxy 3.4 reference config with three frontends and stats on :8404. |
| `scripts/` | Acceptance, benchmark, chart, report-data and validation tooling. |
| `results/` | Raw evidence (gitignored) plus tracked CSVs and SVG charts. |
| `docs/` | This documentation suite plus four topic documents. |

Ports: custom proxy `:8080`; HAProxy `:8091` (round robin), `:8092` (least
connections), `:8093` (source hash), `:8404` (stats); backends `:8001`–`:8004`.
**Run one load balancer at a time** — the comparison rule is part of the data.

The full phase-by-phase specification is in `load_balancer_project_plan.md`.

## Installation

Prerequisites: Python 3.11+ (recorded runs used 3.15.2), Docker Desktop with
Linux containers/WSL2, and PowerShell on Windows.

```powershell
git clone <your-repo-url> LoadBalancer
Set-Location LoadBalancer

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

docker compose up -d --build
docker build -t lb/wrk docker/wrk
```

Optional (HAProxy reference environment):

```powershell
docker compose --profile haproxy up -d
docker exec lb-haproxy haproxy -vv
```

Verify the farm: `curl http://127.0.0.1:8001/health` should return HTTP 200.
The backend environment template is `.env.example`; all proxy knobs are
documented in the [runbook](docs/RUNBOOK.md#configuration).

## Quickstart

Start the custom proxy with all four backends:

```powershell
$env:LB_BACKENDS = 'backend-1:127.0.0.1:8001,backend-2:127.0.0.1:8002,backend-3:127.0.0.1:8003,backend-4:127.0.0.1:8004'
$env:LB_STRATEGY = 'round_robin'
.\.venv\Scripts\python.exe -m load_balancer.main
```

In another shell, send a request and a short load test:

```powershell
curl http://127.0.0.1:8080/work?delay_ms=5
docker run --rm lb/wrk -t2 -c10 -d8s --latency `
  "http://host.docker.internal:8080/work?delay_ms=5"
```

Run the test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Backend endpoints (`GET`):

| Endpoint | Behavior |
|---|---|
| `/` | Backend ID, request ID, timestamp |
| `/health` | HTTP 200 while alive |
| `/work?delay_ms=N` | Artificial work; 503 when the semaphore is full |
| `/stats` | Local request/in-flight/busy counters |

The farm's capacity differs only by configuration, so any performance
difference comes from the load-balancing behavior, not different code.

## Documentation

| Document | What it answers |
|---|---|
| [FILE_REFERENCE.md](docs/FILE_REFERENCE.md) | What each created file does, its functions, and who calls it. |
| [ARCHITECTURE_FLOW.md](docs/ARCHITECTURE_FLOW.md) | State machines for requests, health checks, metrics and the report pipeline. |
| [RUNBOOK.md](docs/RUNBOOK.md) | Every command, configuration knob, test and troubleshooting step. |
| [benchmark_matrix.md](docs/benchmark_matrix.md) | How the baseline/failure datasets were produced and their controls. |
| [haproxy_setup.md](docs/haproxy_setup.md) | HAProxy configuration, knob mapping and recorded differences. |
| [metric_definitions.md](docs/metric_definitions.md) | Shared metric contract for both implementations. |
| [final_report.md](docs/final_report.md) | Results, ten analysis answers, charts, limitations and conclusions. |

### Important caveats

- The farm has only 34 concurrency slots; the published benchmark is a
  **saturation** comparison, not a pure proxy-efficiency measurement. Most 5xx
  responses are backend 503s.
- wrk `--latency` emits p50/p75/p90/p99, not p95; the report states the gap
  explicitly instead of inventing a p95.
- HAProxy's `leastconn` counts connections and custom's
  `least_active_requests` counts in-flight requests — related, not equivalent.
- Optional extensions (upstream pooling, weights, consistent hashing,
  draining, TLS, dashboards) are listed in the plan but were not implemented.

Start with the [runbook](docs/RUNBOOK.md) to run things, the
[flow overview](docs/ARCHITECTURE_FLOW.md) to understand them, and the
[final report](docs/final_report.md) to see what the measurements showed.
