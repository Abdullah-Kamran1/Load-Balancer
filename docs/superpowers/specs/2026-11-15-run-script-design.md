# run.ps1 — one-command launcher for the load balancer stack

Date: 2026-11-15
Status: approved (conversation 2026-11-15)

## Intent and why

Running the system has been manual: `docker compose up -d`, wait for health,
then start the proxy with four env vars in a shell. The user wants one
script that does all of it and exposes the tunables (algorithm, ports,
backend set) as an interactive menu.

## Goals

- `.\run.ps1` — interactive start: pick algorithm / proxy port / backend set
  with defaults; bring the Docker farm up and wait for `(healthy)`; start the
  proxy in its own console window with the chosen env; verify it answers;
  print a summary with URLs to try.
- `.\run.ps1 -Stop` — stop the proxy (PID listening on the port) and
  `docker compose stop`.
- `.\run.ps1 -Status` — container health + proxy port state.

## Non-goals / anti-goals

- No new failure modes: the script only wraps the exact commands and env
  vars already used manually (docker compose + LB_* env vars).
- No config persistence, no daemonizing, no log files to manage — the
  proxy window IS the log, Ctrl+C/Closing it stops the proxy.
- Not a substitute for the acceptance scripts (scripts/phase*_acceptance.py).

## Constraints

- Windows host, PowerShell 5.1 (no ternary, no `&&`), Docker Desktop must
  be running (script points at the exe if the daemon is down).
- Proxy runs from repo `.venv`; backends come from `docker-compose.yml`.

## Approaches considered

1. Single `run.ps1` with `-Stop`/`-Status` switches (CHOSEN — one file,
   one mental model, default action is the common one: start).
2. Separate `run.ps1` + `stop.ps1` (rejected: two files to keep in sync).
3. Python CLI with flags (rejected by user preference: interactive menu).

## Left to the builder

- Exact menu wording; health-wait timeout (~30 s); whether `-Stop` uses
  `docker compose stop` (chosen: yes — keeps containers on disk for fast
  restart) vs `down`.
- Note: verification is by real runs of each mode (no Pester harness exists;
  the script is thin orchestration over already-proven commands).
