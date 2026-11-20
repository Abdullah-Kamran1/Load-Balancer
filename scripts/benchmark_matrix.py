"""Phase 8: controlled benchmark matrix runner.

Runs one load balancer at a time over the shared four-backend farm:
  18 cases = {custom, haproxy} x {3 algorithms} x {50, 200, 500 clients}
  each case: 10 s warm-up, then 3 measured 60 s wrk runs (median reported)

Every measured run writes:
  results/raw/wrk_<run_id>.txt           raw wrk output
  results/raw/<snapshot>.json            proxy snapshot (custom dump / HAProxy stats)
  results/structured/combined.csv        appended wrk+proxy row (24-column schema)
  results/structured/backends.csv        appended per-backend rows
  results/structured/resources.csv       sampled LB + wrk CPU/memory
  results/structured/benchmark_runs.csv  manifest linking run_id -> case and raw files

Usage:
  python scripts/benchmark_matrix.py --dry-run          # show cases/commands only
  python scripts/benchmark_matrix.py --phase baseline --runs 3
  python scripts/benchmark_matrix.py --phase baseline --runs 3 --limit 1
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from load_balancer.metrics import append_backends_csv, write_json
from load_balancer.metrics.haproxy_stats import synthesize_snapshot
from load_balancer.metrics.wrk_parser import append_combined_csv, parse_wrk_output
from scripts.haproxy_acceptance import (
    ensure_stack,
    get_stats,
    save_environment,
    server_status,
    wait_for,
)

WORKLOAD = "/work?delay_ms=25"
WARMUP_S = 10
SAMPLE_INTERVAL_S = 2.0
BACKEND_COUNT = 4
CUSTOM_PORT = 8080
WRK_IMAGE = "lb/wrk"

RAW_DIR = REPO_ROOT / "results" / "raw"
STRUCTURED_DIR = REPO_ROOT / "results" / "structured"

RESOURCES_HEADER = [
    "run_id", "mode", "strategy", "clients", "target", "sample_no",
    "elapsed_s", "cpu_pct", "mem_mib",
]
MANIFEST_HEADER = [
    "run_id", "phase", "mode", "strategy", "clients", "threads",
    "duration_s", "wrk_path", "snapshot_path", "notes",
]
FAILURES_HEADER = [
    "run_id", "mode", "strategy", "clients", "victim", "kill_at_s",
    "detection_s", "failed_detection", "failed_after_kill_30s", "wrk_non_2xx",
    "recovery_s", "readmitted",
]


@dataclass(frozen=True)
class Case:
    mode: str
    strategy: str
    clients: int
    port: int
    pxname: str = ""
    frontend: str = ""

    @property
    def slug(self) -> str:
        return f"{self.mode}-{self.strategy}-{self.clients}"


def build_cases() -> list[Case]:
    """The 18 cases from the plan's baseline matrix (plan lines 511-520)."""
    custom = [
        ("round_robin", CUSTOM_PORT, "", ""),
        ("least_active_requests", CUSTOM_PORT, "", ""),
        ("ip_hash", CUSTOM_PORT, "", ""),
    ]
    haproxy = [
        ("roundrobin", 8091, "be_roundrobin", "fe_roundrobin"),
        ("leastconn", 8092, "be_leastconn", "fe_leastconn"),
        ("source", 8093, "be_source", "fe_source"),
    ]
    cases: list[Case] = []
    for mode, strategies in (("custom", custom), ("haproxy", haproxy)):
        for strategy, port, pxname, frontend in strategies:
            for clients in (50, 200, 500):
                cases.append(Case(mode, strategy, clients, port, pxname, frontend))
    return cases


def wrk_threads(clients: int) -> int:
    return 8 if clients >= 500 else 4


def make_run_id(case: Case, run_index: int, now: datetime) -> str:
    return f"{now:%Y%m%d-%H%M%S}-{case.slug}-r{run_index}"


def median_of(values: list[float]) -> float:
    return statistics.median(values)


def select_cases(cases: list[Case], start: int = 1, limit: int = 0) -> list[Case]:
    """Apply 1-based start before the optional count (--start/--limit)."""
    selected = cases[start - 1:]
    return selected[:limit] if limit else selected


def run_case_safe(case: Case, index: int, log_path: Path, **kwargs) -> str | None:
    """Keep an unattended matrix going after one failed run."""
    try:
        return run_case(case, index, **kwargs)
    except Exception as exc:  # noqa: BLE001 - one case must not abort 54 min of runs
        append_row(
            log_path,
            ["case", "run_index", "error_type", "error"],
            [case.slug, index, type(exc).__name__, str(exc)],
        )
        print(f"[{case.slug} r{index}] FAILED: {type(exc).__name__}: {exc}", flush=True)
        return None


def _bytes_to_mib(value: float, unit: str) -> float:
    unit = unit.upper().strip()
    factors = {"B": 1 / 1048576, "KB": 1 / 1024, "KIB": 1 / 1024,
               "MB": 1 / 1.024, "MIB": 1.0, "GB": 1024 / 1.024, "GIB": 1024.0}
    return value * factors.get(unit, 1.0)


def parse_docker_stats(text: str) -> tuple[float, float]:
    """Parse `docker stats --format '{{.CPUPerc}},{{.MemUsage}}'`."""
    parts = text.strip().split(",")
    cpu = float(parts[0].strip().rstrip("%"))
    mem_token = parts[1].split("/")[0].strip().split()[0]
    match = re.match(r"([\d.]+)([A-Za-z]*)", mem_token)
    if not match:
        raise ValueError(f"unparseable memory usage: {text!r}")
    return cpu, _bytes_to_mib(float(match.group(1)), match.group(2))


def cpu_percent_delta(prev_cpu: float, cpu: float, prev_t: float, now_t: float) -> float:
    """CPU time consumed per wall-second, as a percentage of one core."""
    if now_t <= prev_t:
        return 0.0
    return (cpu - prev_cpu) * 100.0 / (now_t - prev_t)


def wrk_command(case: Case, run_id: str, duration_s: int) -> list[str]:
    return [
        "docker", "run", "--rm", "--name", f"lb-wrk-{run_id}", WRK_IMAGE,
        f"-t{wrk_threads(case.clients)}", f"-c{case.clients}", f"-d{duration_s}s",
        "--latency", f"http://host.docker.internal:{case.port}{WORKLOAD}",
    ]


def snapshot_kwargs(case: Case) -> dict:
    if case.mode == "custom":
        return {}
    return {
        "strategy_name": case.strategy,
        "pxname": case.pxname,
        "frontend_pxname": case.frontend,
    }


def append_row(path: Path, header: list[str], row: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(header)
        writer.writerow(row)


# --- live orchestration -------------------------------------------------

def run(cmd: list[str], *, timeout: float = 180.0, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout, **kwargs
    )


def http_ok(url: str, timeout: float = 2.0) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_farm() -> None:
    up = run(["docker", "compose", "up", "-d"], timeout=300)
    if up.returncode != 0:
        raise RuntimeError(f"docker compose up failed: {up.stderr.strip()}")
    for port in range(8001, 8001 + BACKEND_COUNT):
        if not wait_for(lambda p=port: http_ok(f"http://127.0.0.1:{p}/health"),
                        timeout=60, step=0.5, what=f"backend on {port}"):
            raise RuntimeError(f"backend on port {port} never became healthy")


def start_custom_proxy(case: Case, run_id: str) -> tuple[subprocess.Popen, object, Path]:
    # Windows keeps wrk's connections in TIME_WAIT on port 8080; a restart
    # inside that window cannot bind. Wait for a successful fresh bind.
    if not wait_for(lambda: port_available(CUSTOM_PORT), timeout=90, step=1,
                    what="custom proxy port free"):
        raise RuntimeError(f"port {CUSTOM_PORT} never became available for the proxy")
    dump = RAW_DIR / f"custom_{run_id}_snapshot.json"
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.unlink(missing_ok=True)
    log_path = RAW_DIR / f"custom_{run_id}.log"
    log = log_path.open("a", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        LB_PROXY_HOST="127.0.0.1",
        LB_PROXY_PORT=str(CUSTOM_PORT),
        LB_BACKENDS=",".join(
            f"backend-{i}:127.0.0.1:{8000 + i}" for i in range(1, BACKEND_COUNT + 1)
        ),
        LB_STRATEGY=case.strategy,
        LB_METRICS_DUMP_PATH=str(dump.relative_to(REPO_ROOT)),
    )
    proc = subprocess.Popen(
        [str(REPO_ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "load_balancer.main"],
        cwd=REPO_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    if not wait_for(lambda: _tcp_open("127.0.0.1", CUSTOM_PORT),
                    timeout=30, step=0.1, what="custom proxy listener"):
        stop_custom_proxy(proc, log)
        raise RuntimeError("custom proxy never opened its listener")
    return proc, log, dump


def port_available(port: int, host: str = "127.0.0.1") -> bool:
    """True when a fresh bind succeeds (no LISTEN/TIME_WAIT squatting)."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


def _tcp_open(host: str, port: int) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def stop_custom_proxy(proc: subprocess.Popen | None, log=None) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if log is not None:
        log.close()


def restart_haproxy() -> None:
    restarted = run(["docker", "restart", "lb-haproxy"], timeout=90)
    if restarted.returncode != 0:
        raise RuntimeError(f"docker restart lb-haproxy failed: {restarted.stderr.strip()}")

    def all_up() -> bool:
        stats = get_stats()
        return all(
            server_status(stats, "be_roundrobin", f"backend-{i}") == "UP"
            for i in range(1, BACKEND_COUNT + 1)
        )

    if not wait_for(all_up, timeout=60, step=0.2, what="all HAProxy backends UP"):
        raise RuntimeError("HAProxy never saw all backends UP after restart")
    # Published ports can lag the stats socket briefly; require a real HTTP
    # response through each frontend before any benchmark starts.
    for port in (8091, 8092, 8093):
        if not wait_for(lambda p=port: http_ok(f"http://127.0.0.1:{p}/health"),
                        timeout=30, step=0.5, what=f"HAProxy frontend :{port}"):
            raise RuntimeError(f"HAProxy frontend :{port} never served HTTP")


def run_wrk(case: Case, run_id: str, duration_s: int, *, warmup: bool = False) -> dict:
    label = f"warmup-{run_id}" if warmup else run_id
    cmd = wrk_command(case, label, duration_s)
    # A crashed earlier run could leave a container of the same name behind.
    run(["docker", "rm", "-f", f"lb-wrk-{label}"], timeout=30)
    result = run(cmd, timeout=duration_s + 120)
    if result.returncode != 0:
        raise RuntimeError(f"wrk failed ({result.returncode}): {result.stderr.strip()}")
    path = RAW_DIR / f"wrk_{label}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.stdout, encoding="utf-8")
    return {"metrics": parse_wrk_output(result.stdout), "path": path}


class ResourceSampler:
    """Sample LB (and wrk) CPU/memory every 2 s without extra dependencies.

    For the custom proxy, `target` is the path of its 1 s metrics dump;
    for containers it is the container name.
    """

    def __init__(self, run_id: str, case: Case, target: str, *, container: bool,
                 wrk_label: str | None = None):
        self.run_id = run_id
        self.case = case
        self.target = target
        self.container = container
        self.wrk_label = wrk_label
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.rows: list[list] = []
        self.lb_prev: tuple[float, float] | None = None

    def _sample_one(self, ident: str, container: bool,
                    prev: tuple[float, float] | None):
        try:
            if container:
                result = run(
                    ["docker", "stats", "--no-stream", "--format",
                     "{{.CPUPerc}},{{.MemUsage}}", ident], timeout=20
                )
                if result.returncode != 0:
                    return None, prev
                cpu, mem = parse_docker_stats(result.stdout)
                return (cpu, mem), prev
            if not container:
                # Windows process CPU counters can read 0 for spawned
                # processes; the proxy records its own process_time() in the
                # 1 s dump (meta.process_cpu_seconds/process_rss_mib).
                dump = json.loads(Path(ident).read_text(encoding="utf-8"))
                meta = dump["meta"]
                cpu_seconds = float(meta["process_cpu_seconds"])
                mem = float(meta["process_rss_mib"])
                now = time.monotonic()
                if prev is None:
                    return (0.0, mem), (cpu_seconds, now)
                return (cpu_percent_delta(prev[0], cpu_seconds, prev[1], now), mem), \
                    (cpu_seconds, now)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            # A transient docker/powershell hiccup must not kill the sampler
            # thread (which would silently drop all remaining samples).
            print(f"[sampler] skipped {ident}: {type(exc).__name__}: {exc}", flush=True)
            return None, prev

    def _loop(self) -> None:
        started = time.monotonic()
        sample_no = 0
        while not self._stop.is_set():
            sample_no += 1
            elapsed = time.monotonic() - started
            if not self.container:
                sample, self.lb_prev = self._sample_one(self.target, False, self.lb_prev)
            else:
                sample, _ = self._sample_one(self.target, True, None)
            if sample is not None:
                self.rows.append([
                    self.run_id, self.case.mode, self.case.strategy, self.case.clients,
                    self.target, sample_no, round(elapsed, 2),
                    round(sample[0], 2), round(sample[1], 2),
                ])
            if self.wrk_label:
                wrk_sample, _ = self._sample_one(self.wrk_label, True, None)
                if wrk_sample is not None:
                    self.rows.append([
                        self.run_id, self.case.mode, self.case.strategy, self.case.clients,
                        self.wrk_label, sample_no, round(elapsed, 2),
                        round(wrk_sample[0], 2), round(wrk_sample[1], 2),
                    ])
            self._stop.wait(SAMPLE_INTERVAL_S)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def finish(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15)
        for row in self.rows:
            append_row(STRUCTURED_DIR / "resources.csv", RESOURCES_HEADER, row)


def _snapshot_for(case: Case, run_id: str, dump: Path | None) -> dict:
    if case.mode == "custom":
        if dump is None or not dump.exists():
            raise RuntimeError(f"custom snapshot missing for {run_id}")
        return json.loads(dump.read_text(encoding="utf-8"))
    return synthesize_snapshot(get_stats(), **snapshot_kwargs(case))


def _write_run_result(case: Case, run_id: str, wrk: dict, snapshot: dict,
                      phase: str, notes: str = "") -> None:
    append_combined_csv(
        wrk["metrics"], snapshot, STRUCTURED_DIR / "combined.csv",
        run_id=run_id, mode=case.mode,
    )
    append_backends_csv(snapshot, STRUCTURED_DIR / "backends.csv", run_id=run_id)
    write_json(snapshot, RAW_DIR / f"snapshot_{run_id}.json")
    append_row(
        STRUCTURED_DIR / "benchmark_runs.csv", MANIFEST_HEADER,
        [run_id, phase, case.mode, case.strategy, case.clients,
         wrk_threads(case.clients), wrk["metrics"]["duration_s"],
         wrk["path"].relative_to(REPO_ROOT).as_posix(),
         (RAW_DIR / f"snapshot_{run_id}.json").relative_to(REPO_ROOT).as_posix(),
         notes],
    )


def run_case(case: Case, run_index: int, *, phase: str = "baseline",
             duration_s: int = 60, warmup_s: int = WARMUP_S) -> str:
    run_id = make_run_id(case, run_index, datetime.now())
    proc = log = dump = None
    try:
        if warmup_s > 0:
            print(f"[{run_id}] warm-up {warmup_s}s", flush=True)
            if case.mode == "custom":
                proc, log, dump = start_custom_proxy(case, f"warmup-{case.slug}")
            else:
                restart_haproxy()
            run_wrk(case, f"warmup-{case.slug}-{run_index}", warmup_s, warmup=True)
            if case.mode == "custom":
                stop_custom_proxy(proc, log)
                proc = log = None

        print(f"[{run_id}] measured {duration_s}s", flush=True)
        if case.mode == "custom":
            proc, log, dump = start_custom_proxy(case, run_id)
            sampler = ResourceSampler(run_id, case, str(dump), container=False,
                                      wrk_label=f"lb-wrk-{run_id}")
        else:
            restart_haproxy()  # zero cumulative stats after the warm-up
            sampler = ResourceSampler(run_id, case, "lb-haproxy", container=True,
                                      wrk_label=f"lb-wrk-{run_id}")
        sampler.start()
        try:
            wrk = run_wrk(case, run_id, duration_s)
            time.sleep(1.2)  # let the 1 s custom dump / stats refresh catch up
        finally:
            sampler.finish()
        snapshot = _snapshot_for(case, run_id, dump)
        _write_run_result(case, run_id, wrk, snapshot, phase)
        if case.mode == "haproxy":
            save_environment(run_id)
        print(f"[{run_id}] done: wrk={wrk['metrics']['requests_total']} "
              f"rps={wrk['metrics']['requests_per_sec']}", flush=True)
        return run_id
    finally:
        stop_custom_proxy(proc, log)
        run(["docker", "rm", "-f", f"lb-wrk-{run_id}"], timeout=30)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print cases/commands only")
    parser.add_argument("--phase", default="baseline", help="manifest phase label")
    parser.add_argument("--runs", type=int, default=3, help="measured runs per case")
    parser.add_argument("--limit", type=int, default=0, help="run only the first N cases")
    parser.add_argument("--start", type=int, default=1, help="1-based first case number")
    parser.add_argument("--duration", type=int, default=60, help="measured run seconds")
    parser.add_argument("--warmup", type=int, default=WARMUP_S, help="warm-up seconds")
    parser.add_argument("--smoke", action="store_true", help="8 s runs, no warm-up")
    args = parser.parse_args()

    if args.smoke:
        args.duration, args.warmup = 8, 0
    cases = select_cases(build_cases(), args.start, args.limit)

    if args.dry_run:
        for case in cases:
            print(case, "->", " ".join(wrk_command(case, "run-id", args.duration)))
        return 0

    ensure_farm()
    error_log = RAW_DIR / "benchmark_errors.csv"
    for case in cases:
        for index in range(1, args.runs + 1):
            run_case_safe(
                case, index, error_log, phase=args.phase,
                duration_s=args.duration, warmup_s=args.warmup,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
