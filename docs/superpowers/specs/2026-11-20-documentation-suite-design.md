# Design: comprehensive documentation suite (approved 2026-11-20)

## Intent

The project is functionally complete (Phases 0–9 merged to `main`) but its
documentation is split across phase reports. The human partner wants one
coherent set of docs that explains every created file, the whole system's
data flow, installation/setup, and how to run/configure/test it.

## Goals

- A per-file reference: purpose, functions/classes, behavior, callers.
- A finished README as the project entry point (intro + install + quickstart).
- A state-machine-style flow document showing how requests, health checks,
  metrics, and benchmark data move through the system.
- A runbook of commands, configuration knobs, and test procedures.
- Links (not copies) to the existing topic docs.

## Non-goals / anti-goals

- No changes to runtime code, algorithms, configs, or the dataset.
- No one-tiny-file-per-source-file explosion; no duplicated metric definitions.
- No claim that missing optional extensions exist (pooling, TLS, etc).

## Constraints

- Docs only, on branch `docs-comprehensive`, committed per deliverable.
- Existing generated CSV/SVG artifacts are referenced, not re-explained individually.
- Must remain accurate against the current code and the Phase 9 findings.

## Approaches considered

1. **One Markdown file per source file.** Maximum granularity, but ~50 tiny
   files with duplicated context and heavy navigation cost. Rejected.
2. **One consolidated `docs/FILE_REFERENCE.md` with a section per file.**
   Easy to search/scan, one place for caller maps, generated artifacts get a
   catalogue. Chosen.
3. **Docstrings-in-code only.** Keeps docs next to code but provides no
   system-level view, README, or runbook. Rejected.

## Design (chosen)

| Deliverable | Path | Content |
|---|---|---|
| File reference | `docs/FILE_REFERENCE.md` | Grouped sections: backend app, core proxy (config/models/algorithms/http/proxy/main), health, metrics, scripts, tests, configs, generated artifacts. Each source section: purpose, key functions/signatures, behavior, called by / calls. |
| README | `README.md` | Intro, what the project demonstrates, architecture snapshot, prerequisites, install (venv + deps + Docker + wrk image + HAProxy profile), quickstart, test command, doc index. |
| Flow overview | `docs/ARCHITECTURE_FLOW.md` | State-machine diagrams (text/Mermaid) for request lifecycle, health monitor states, strategy selection, metrics snapshot/CSV pipeline, benchmark/failure pipeline, report generation. |
| Runbook | `docs/RUNBOOK.md` | Every command (farm, custom proxy, HAProxy, wrk, benchmarks, charts, validator), all env vars/config knobs with defaults, testing matrix, troubleshooting. |

Work order: file reference → flow overview → runbook → README (so the README
can link to finished docs). Each doc verified by reading the referenced code,
then a final whole-branch review before merge.

## Sizing

Small-to-medium: four Markdown files, no executable behavior. A short design
doc is sufficient; no builder-check round is needed.
