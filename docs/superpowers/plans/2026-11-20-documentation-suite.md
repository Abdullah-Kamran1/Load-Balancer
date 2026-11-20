# Documentation Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four linked documentation deliverables — a per-file reference, a state-machine flow overview, a runbook, and a finished README — without changing runtime behavior.

**Architecture:** Four Markdown documents are written in dependency order (reference → flow → runbook → README), each verified by an artifact test in `load_balancer/tests/test_documentation.py` that reads the real tree. The README becomes the single entry point linking the new docs and the four existing topic docs.

**Tech Stack:** Markdown, Python stdlib (`pathlib`, `re`) for verification tests, pytest.

**Spec:** `docs/superpowers/specs/2026-11-20-documentation-suite-design.md`

## Global Constraints

- Docs only: no runtime code, config, or dataset changes.
- Branch: `docs-comprehensive`; one commit per task.
- Do not duplicate the existing topic docs (`benchmark_matrix.md`, `final_report.md`, `haproxy_setup.md`, `metric_definitions.md`); link them.
- Do not claim optional extensions (pooling, TLS, weights, draining, dashboards) exist.
- All paths in docs are relative to the repository root; Windows commands use PowerShell syntax.

## Review Focus

1. **A created source file has no reference section** — test enumerates `*.py`, `*.cfg`, `*.yml`, `Dockerfile`, `run.ps1`, `pytest.ini`, `.env.example` from the current tree and asserts each path appears in `docs/FILE_REFERENCE.md`.
2. **A documented config default drifts from code** — test reads `load_balancer/config.py` `LB_*` names/defaults and asserts each appears in `docs/RUNBOOK.md` with its default value.
3. **The flow overview omits a real health state** — test asserts `UP`, `DOWN`, `UP n/m`, `DOWN n/m` and the snapshot/CSV/report pipeline stages appear in `docs/ARCHITECTURE_FLOW.md`.
4. **A runbook command points to a nonexistent script** — test extracts `python scripts/<name>.py` tokens from `docs/RUNBOOK.md` and asserts each file exists.
5. **README has broken links or missing install steps** — test extracts all local Markdown links from `README.md`, asserts targets exist, and asserts headings for introduction, installation, quickstart and documentation index.

---

### Task 1: Per-file reference

**Files:**
- Create: `docs/FILE_REFERENCE.md`
- Create/extend: `load_balancer/tests/test_documentation.py`

**Interfaces:**
- Consumes: current repository tree and function signatures from the source files.
- Produces: `docs/FILE_REFERENCE.md` sections keyed by exact repo-relative paths; `test_created_files_have_reference_sections`.

- [ ] **Step 1: Write the failing test**

```python
def test_created_files_have_reference_sections(repo_root):
    reference = (repo_root / "docs" / "FILE_REFERENCE.md").read_text(encoding="utf-8")
    created = sorted(
        p.relative_to(repo_root).as_posix()
        for p in repo_root.rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".yml", ".ini"}
        or p.name in {"Dockerfile", "run.ps1", ".env.example", "haproxy.cfg"}
        and ".venv" not in p.parts and "results" not in p.parts
    )
    assert created, "no files discovered"
    missing = [p for p in created if p not in reference]
    assert missing == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest load_balancer/tests/test_documentation.py -q`
Expected: FAIL — `FILE_REFERENCE.md` does not exist.

- [ ] **Step 3: Write `docs/FILE_REFERENCE.md`**

One section per file discovered by the test, grouped: backend app (`backend/`), core (`load_balancer/*.py`, `algorithms/`, `health/`), metrics (`load_balancer/metrics/`), scripts (`scripts/`), tests (`load_balancer/tests/`), infra (`docker-compose.yml`, `docker/wrk/Dockerfile`, `backend/Dockerfile`, `haproxy/haproxy.cfg`, `run.ps1`, `pytest.ini`, `.env.example`, `requirements-dev.txt`), docs. Each section: purpose, functions/classes with signatures and one-line behavior, "Called by" / "Calls" lines. Generated `results/structured/*.csv` and `results/plots/*.svg` get one catalogue section listing what each contains and which script writes it.

- [ ] **Step 4: Run the test and full suite**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: 187 passed (one new test).

- [ ] **Step 5: Commit**

`git add docs/FILE_REFERENCE.md load_balancer/tests/test_documentation.py && git commit -m "docs: per-file reference for every created file"`

### Task 2: State-machine flow overview

**Files:**
- Create: `docs/ARCHITECTURE_FLOW.md`
- Extend: `load_balancer/tests/test_documentation.py`

**Interfaces:**
- Consumes: file reference paths from Task 1.
- Produces: `docs/ARCHITECTURE_FLOW.md`; `test_flow_overview_covers_all_states_and_pipelines`.

- [ ] **Step 1: Write the failing test**

Assert the flow doc contains: `START`, `READ_REQUEST`, `SELECT_BACKEND`, `FORWARD`, `DRAIN_RESPONSE`, `UNHEALTHY`, `RECOVERING`, `UP`, `DOWN`, `UP n/m`, `DOWN n/m`, `metrics dump`, `combined.csv`, `baseline_medians.csv`, `failure_timeline.svg`, `final_report.md`, and at least two ```mermaid fences.

- [ ] **Step 2: Run it**

Expected: FAIL — file missing.

- [ ] **Step 3: Write `docs/ARCHITECTURE_FLOW.md`**

State diagrams (Mermaid + ASCII fallback) for: (a) client request lifecycle through `main.py → proxy.py → http_connection.py → algorithms → health registry → backend → response`, including 400/413/502/503 edges; (b) backend health state machine `UP → UP n/m → DOWN → DOWN n/m → UP`, naming `fall 3`/`rise 2` and the 1 s interval; (c) metrics pipeline `proxy counters → dump_loop → snapshot.json → parse_wrk.py → combined.csv/backends.csv`; (d) HAProxy stats pipeline `stats;csv → haproxy_stats.py → synthesize_snapshot → combined.csv (mode=haproxy)`; (e) benchmark/report pipeline `benchmark_matrix.py → manifest+resources → build_charts.py → build_report_data.py → build_report_plots.py → final_report.md → validate_report.py`. Link the file reference and topic docs.

- [ ] **Step 4: Run the test and full suite**

Expected: 188 passed.

- [ ] **Step 5: Commit**

`git commit -m "docs: state-machine overview of system data flow"`

### Task 3: Runbook

**Files:**
- Create: `docs/RUNBOOK.md`
- Extend: `load_balancer/tests/test_documentation.py`

**Interfaces:**
- Consumes: config defaults from `load_balancer/config.py`; script names from `scripts/`.
- Produces: `docs/RUNBOOK.md`; `test_runbook_documents_config_and_valid_commands`.

- [ ] **Step 1: Write the failing test**

Read every `LB_[A-Z_]+` name and its default from `load_balancer/config.py`; assert each name and default appears in `docs/RUNBOOK.md`. Extract `python scripts/<name>.py` and `.venv\Scripts\python.exe scripts\<name>.py` tokens; assert corresponding files exist. Assert sections "Prerequisites", "Running", "Configuration", "Testing", "Troubleshooting".

- [ ] **Step 2: Run it**

Expected: FAIL — file missing.

- [ ] **Step 3: Write `docs/RUNBOOK.md`**

PowerShell commands for: creating venv/installing `requirements-dev.txt` + backend deps; `docker compose up -d --build`; optional `--profile haproxy`; starting custom proxy with env overrides; running each acceptance/benchmark/report script; `pytest` commands; wrk container invocation and digest-pinned image. Full env-var table with defaults (host, port, backends, strategy, health interval/timeout/failures/recoveries, metrics dump path). Test matrix (unit, acceptance, full suite) and troubleshooting (Docker daemon down, port 8080 TIME_WAIT, HAProxy static backend DNS, p95 gap, saturation caveat).

- [ ] **Step 4: Run the test and full suite**

Expected: 189 passed.

- [ ] **Step 5: Commit**

`git commit -m "docs: runbook for commands configuration and tests"`

### Task 4: README as entry point

**Files:**
- Modify: `README.md`
- Extend: `load_balancer/tests/test_documentation.py`

**Interfaces:**
- Consumes: docs from Tasks 1–3, existing topic docs.
- Produces: finished `README.md`; `test_readme_links_and_sections`.

- [ ] **Step 1: Write the failing test**

Assert README contains headings matching `Introduction|About`, `Installation`, `Quickstart`, `Documentation`, that every local Markdown link target exists, and that the file reference, flow overview, runbook and four topic docs are linked.

- [ ] **Step 2: Run it**

Expected: FAIL on headings/links.

- [ ] **Step 3: Rewrite `README.md`**

~120–180 lines: what the project demonstrates and why; architecture snapshot (custom asyncio proxy, FastAPI farm, HAProxy reference, metrics/benchmarks/report); prerequisites (Windows/WSL2 note, Python 3.11+, Docker Desktop, wrk image); install steps (clone, venv, pip, backend deps, compose build, wrk image build, optional HAProxy profile); quickstart (start farm + proxy + one curl/wrk request, run tests); documentation index with links; config pointer to runbook; limitations pointer to final report.

- [ ] **Step 4: Run the test and full suite**

Expected: 190 passed.

- [ ] **Step 5: Commit**

`git commit -m "docs: complete README with install and quickstart"`

### Final gate

- [ ] Run `.venv\Scripts\python.exe -m pytest -q` (190 passed) and `.venv\Scripts\python.exe scripts\validate_report.py` (unchanged).
- [ ] Dispatch one fresh whole-branch reviewer over the four docs; fix any factual drift, then present the merge menu.
