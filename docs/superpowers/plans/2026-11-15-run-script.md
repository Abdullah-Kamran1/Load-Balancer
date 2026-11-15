# run.ps1 Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One PowerShell script that starts the whole stack (Docker backend farm + proxy) from an interactive menu, and stops/statuses it via `-Stop` / `-Status`.

**Architecture:** Thin orchestration over already-proven commands: `docker compose up -d` + health wait for the farm; `Start-Process .venv\Scripts\python.exe -m load_balancer.main` in its own console window with `LB_*` env vars for the proxy; PID lookup by listening port for stop. No new failure modes.

**Tech Stack:** PowerShell 5.1 (no `&&`, no ternary), Docker Compose, repo `.venv`.

**Spec:** `docs/superpowers/specs/2026-11-15-run-script-design.md`

## Global Constraints

- PowerShell 5.1 compatible syntax only.
- Proxy command exactly: `.venv\Scripts\python.exe -m load_balancer.main` with `LB_PROXY_HOST`, `LB_PROXY_PORT`, `LB_BACKENDS`, `LB_STRATEGY` set in the child environment.
- `LB_BACKENDS` default: `backend-1:127.0.0.1:8001,backend-2:127.0.0.1:8002,backend-3:127.0.0.1:8003,backend-4:127.0.0.1:8004`.
- `-Stop` uses `docker compose stop` (containers kept, not removed).
- Health wait cap ~30 s; on Docker daemon unreachable, print the Docker Desktop exe path and exit non-zero.

## Review Focus

- Proxy already running when start is invoked → expect: detect the listener on the chosen port, refuse with a clear message rather than double-start. (Pinned in Task 2 verification.)
- Farm containers exist but are stopped/unhealthy → expect: start mode still brings them up and waits, does not assume "up" means "healthy". (Pinned in Task 1 verification.)
- User enters garbage backend string (wrong number of `:` parts) → expect: menu loop re-prompts, validated against the same 3-part rule as `config.parse_backends`. (Pinned in Task 1 verification.)
- `-Stop` when nothing is running → expect: reports "not running" for proxy and idempotent compose stop, exit 0. (Pinned in Task 2 verification.)

---

### Task 1: Start mode (menu + farm + proxy launch)

**Files:**
- Create: `run.ps1`

**Interfaces:**
- Consumes: `docker-compose.yml` (service names backend-1..4, ports 8001–8004), `.venv\Scripts\python.exe -m load_balancer.main`, env vars `LB_PROXY_HOST/LB_PROXY_PORT/LB_BACKENDS/LB_STRATEGY`.
- Produces: param block `param([switch]$Stop, [switch]$Status)` used by Task 2; functions `Show-Menu`, `Wait-FarmHealthy`, `Start-Proxy`, `Assert-PortListening`.

- [ ] **Step 1: Write `run.ps1` start path** — param block; `Show-Menu` for strategy (1/2/3, default 1), proxy port (default 8080), backend set (Enter = 4-farm default, `c` = custom with 3-part validation loop); `Wait-FarmHealthy` polls `docker compose ps --format json` (or `docker inspect`) until all selected backends healthy or 30 s; `Start-Proxy` sets `$env:LB_*` then `Start-Process` the venv python with `-PassThru`, saving PID to `.lb-run.pid`; probe `http://127.0.0.1:<port>/` via `Invoke-WebRequest` (up to ~10 s); print summary (strategy, port, URLs, PID). If the port is already listening → refuse with message, exit 1, before touching Docker.
- [ ] **Step 2: Verify with real runs** — from `D:\Work\LoadBalancer`:
  - `docker compose stop` first so the "bring the farm up" path is exercised.
  - Run `.\run.ps1`, choose defaults. Expected: farm shows `(healthy)` for 4 containers, a new proxy console window opens, summary prints, and `.\.venv\Scripts\python.exe -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/').status)"` prints `200`.
  - Re-run garbage backend input (`x:1`) at the prompt first: expect re-prompt, not a crash.
  - Run `.\run.ps1` again while the proxy is up: expect refusal message.
  - `.\run.ps1 -Stop`, then verify `Invoke-WebRequest http://127.0.0.1:8080/` fails and `docker compose ps` shows no running backends.
- [ ] **Step 3: Commit** — `git add run.ps1; git commit -m "tooling: run.ps1 interactive launcher (start mode)"`.

### Task 2: Stop and Status modes

**Files:**
- Modify: `run.ps1` (param block already exists; add the two branches)

**Interfaces:**
- Consumes: `.lb-run.pid` written by Task 1's `Start-Process -PassThru` (primary stop mechanism); fallback = `Get-NetTCPConnection -LocalPort <port> -State Listen` owner PID.
- Produces: complete script.

- [ ] **Step 1: Implement `-Stop`** — read PID file if present, else port owner; `Stop-Process -Force` with a "not running" message when absent; then `docker compose stop`; print final state.
- [ ] **Step 2: Implement `-Status`** — print per-container `docker compose ps` status lines + whether the proxy port is listening (and PID when it is).
- [ ] **Step 3: Verify with real runs** —
  - Start via `.\run.ps1` (defaults), then `.\run.ps1 -Status`: expected proxy "listening (pid ...)" + 4 healthy containers. `.\run.ps1 -Stop`: expected proxy stopped + farm stopped. `.\run.ps1 -Stop` again with nothing running: expected "not running" messages, exit 0.
  - Full pytest suite still 74/74 (script must not touch package code).
- [ ] **Step 4: Commit** — `git add run.ps1; git commit -m "tooling: run.ps1 stop and status modes"`.
