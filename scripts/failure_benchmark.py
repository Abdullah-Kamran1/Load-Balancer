"""Phase 8: timed backend-failure and recovery benchmark.

For each mode/algorithm at 200 clients: start a 60 s wrk run, kill
lb-backend-2 at t=30 s, keep the load running, capture health transitions
and request failures, then restart the backend in a separate recovery
check. The victim and timing are identical for the custom proxy and
HAProxy (plan lines 524-534).

Usage: python scripts/failure_benchmark.py [--runs 1] [--duration 60]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from load_balancer.metrics.haproxy_stats import synthesize_snapshot
from scripts import benchmark_matrix as bm
from scripts.haproxy_acceptance import get_stats, server_status

VICTIM = "backend-2"
VICTIM_CONTAINER = "lb-backend-2"
FAILURE_CLIENTS = 50
RAW_DIR = REPO_ROOT / "results" / "raw"
STRUCTURED_DIR = REPO_ROOT / "results" / "structured"
FAILURES_HEADER = [
    "run_id", "mode", "strategy", "clients", "victim", "kill_at_s",
    "detection_s", "failed_requests", "wrk_non_2xx", "recovery_s", "readmitted",
]


def failure_cases() -> list:
    return [c for c in bm.build_cases() if c.clients == FAILURE_CLIENTS]


def kill_at(duration_s: int) -> int:
    return duration_s // 2


def custom_transitions(snapshot: dict, backend_id: str) -> list[tuple[str, float]]:
    for backend in snapshot.get("backends", []):
        if backend["backend_id"] == backend_id:
            return [
                (t["state"], float(t["timestamp"]))
                for t in backend.get("state_transitions", [])
            ]
    return []


def is_down(status: str) -> bool:
    # HAProxy reports "UP 2/3" / "UP 1/3" while the server still receives
    # traffic; only DOWN means out of rotation.
    return status.startswith("DOWN")


def is_fully_up(status: str) -> bool:
    return status == "UP"


def status_counter_delta(before: dict, after: dict, statuses) -> int:
    return sum(after.get(s, 0) - before.get(s, 0) for s in statuses)


def recovery_ok(counts: dict, victim: str, min_hits: int = 5,
                sticky: bool = False) -> bool:
    # Sticky strategies pin a client IP to one backend; a healthy victim
    # need not receive any traffic from that single test client.
    if sticky:
        return True
    return counts.get(victim, 0) >= min_hits


def http_get(url: str, timeout: float = 3.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, str(exc.reason)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, f"error:{type(exc).__name__}"


class HealthWatcher:
    """Record the first DOWN/UP of the victim with monotonic timestamps."""

    def __init__(self, case, dump: Path | None):
        self.case = case
        self.dump = dump
        self.down_at: float | None = None
        self.up_at: float | None = None
        self.last_status = "UP"
        self.armed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _status(self) -> str:
        if self.case.mode == "custom":
            if self.dump is None or not self.dump.exists():
                return "MISSING"
            snap = json.loads(self.dump.read_text(encoding="utf-8"))
            for backend in snap.get("backends", []):
                if backend["backend_id"] == VICTIM:
                    return "UP" if backend.get("healthy") else "DOWN"
            return "MISSING"
        stats = get_stats()
        return server_status(stats, self.case.pxname, VICTIM)

    def record(self, status: str) -> None:
        """Only statuses observed after arm() count toward detection."""
        if not self.armed or status == self.last_status:
            return
        now = time.monotonic()
        if self.down_at is None and is_down(status):
            self.down_at = now
        if self.down_at is not None and self.up_at is None and is_fully_up(status):
            self.up_at = now
        self.last_status = status

    def arm(self, status: str) -> None:
        self.last_status = status
        self.armed = True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.record(self._status())
            except Exception:  # transient stats/dump read while polling
                pass
            self._stop.wait(0.1)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def finish(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def snapshot_now(case, dump: Path | None) -> dict:
    if case.mode == "custom":
        if dump is None or not dump.exists():
            raise RuntimeError("custom snapshot dump missing")
        return json.loads(dump.read_text(encoding="utf-8"))
    return synthesize_snapshot(get_stats(), **bm.snapshot_kwargs(case))


def _error_count(case, snapshot: dict) -> int:
    if case.mode == "custom":
        counts = snapshot["proxy"]["counters"].get("status_counts", {})
        # JSON object keys are strings ("502"), not ints.
        return sum(
            value for key, value in counts.items()
            if str(key).isdigit() and int(key) in (500, 502, 503, 504)
        )
    return snapshot["proxy"]["counters"].get("responses_5xx", 0)


def all_backends_healthy(case, dump: Path | None) -> bool:
    if case.mode == "custom":
        if dump is None or not dump.exists():
            return False
        snap = json.loads(dump.read_text(encoding="utf-8"))
        backends = snap.get("backends", [])
        return len(backends) == 4 and all(b.get("healthy") for b in backends)
    stats = get_stats()
    return all(
        server_status(stats, case.pxname, f"backend-{i}") == "UP"
        for i in range(1, 5)
    )


def _write_timeline(run_id: str, lines: list[str]) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"failure_{run_id}.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_failure_case(case, run_id: str, duration_s: int = 60) -> None:
    kill_s = kill_at(duration_s)
    lines = [f"run_id={run_id}", f"case={case.slug}", f"victim={VICTIM}",
             f"kill_at_s={kill_s}", f"duration_s={duration_s}"]
    proc = log = dump = None
    watcher = None
    wrk_proc = None
    try:
        if case.mode == "custom":
            proc, log, dump = bm.start_custom_proxy(case, run_id)
        else:
            bm.restart_haproxy()
        watcher = HealthWatcher(case, dump)
        watcher.start()
        if not bm.wait_for(lambda: bm._tcp_open("127.0.0.1", case.port),
                           timeout=30, step=0.5, what=f"LB port {case.port}"):
            raise RuntimeError(f"load balancer never opened port {case.port}")
        if not bm.wait_for(lambda: all_backends_healthy(case, dump),
                           timeout=60, step=0.5, what="all backends healthy at start"):
            raise RuntimeError("backends were not all healthy before the failure run")
        lines.append("precondition=all backends healthy, LB port open")

        cmd = bm.wrk_command(case, run_id, duration_s)
        wrk_path = RAW_DIR / f"wrk_failure_{run_id}.txt"
        wrk_path.parent.mkdir(parents=True, exist_ok=True)
        wrk_log = wrk_path.open("w", encoding="utf-8")
        wrk_proc = subprocess.Popen(
            cmd, cwd=REPO_ROOT, stdout=wrk_log, stderr=subprocess.STDOUT
        )
        lines.append(f"t0={time.strftime('%Y-%m-%dT%H:%M:%S')}: wrk started")

        time.sleep(kill_s)
        before = snapshot_now(case, dump)
        watcher.arm(watcher._status())  # ignore any pre-kill health flips
        t_kill = time.monotonic()
        killed = bm.run(["docker", "kill", VICTIM_CONTAINER])
        lines.append(f"t+{kill_s:.2f}s: docker kill {VICTIM_CONTAINER}")
        if killed.returncode != 0:
            raise RuntimeError(f"docker kill failed: {killed.stderr.strip()}")
        wrk_proc.wait(timeout=duration_s + 120)
        wrk_log.close()
        wrk_text = wrk_path.read_text(encoding="utf-8")
        if "requests in" not in wrk_text:
            raise RuntimeError(f"wrk produced no load: {wrk_text[:200]!r}")
        wrk_metrics = bm.parse_wrk_output(wrk_text)
        after = snapshot_now(case, dump)
        watcher.finish()

        detection = (
            round(watcher.down_at - t_kill, 3) if watcher.down_at is not None else None
        )
        failed = _error_count(case, after) - _error_count(case, before)
        lines += [
            f"detection_s={detection}",
            f"failed_requests={failed}",
            f"wrk_non_2xx={wrk_metrics['non_2xx']}",
            f"custom_transitions={custom_transitions(before, VICTIM)} -> "
            f"{custom_transitions(after, VICTIM)}" if case.mode == "custom"
            else f"haproxy_victim_status={server_status(get_stats(), case.pxname, VICTIM)}",
        ]

        # --- separate recovery test after the load has ended
        t_restart = time.monotonic()
        started = bm.run(["docker", "start", VICTIM_CONTAINER])
        if started.returncode != 0:
            raise RuntimeError(f"docker start failed: {started.stderr.strip()}")
        direct_up = False
        for _ in range(60):
            if http_get(f"http://127.0.0.1:8002/health")[0] == 200:
                direct_up = True
                break
            time.sleep(0.5)
        lines.append(
            f"direct_health_after_start={round(time.monotonic() - t_restart, 3)}s "
            f"ok={direct_up}"
        )
        all_up_after = bm.wait_for(lambda: all_backends_healthy(case, dump),
                                   timeout=60, step=0.5, what="all backends healthy in recovery")
        up = all_up_after and bm.wait_for(
            (lambda: bool(next((b for b in snapshot_now(case, dump).get("backends", [])
                                if b["backend_id"] == VICTIM), {}).get("healthy")))
            if case.mode == "custom"
            else (lambda: is_fully_up(server_status(get_stats(), case.pxname, VICTIM))),
            timeout=20, step=0.2, what="victim UP in recovery"
        )
        recovery_s = round(time.monotonic() - t_restart, 3) if up else None
        hits: dict[str, int] = {}
        for _ in range(40):
            status, body = http_get(
                f"http://127.0.0.1:{case.port}/work?delay_ms=5"
            )
            backend_id = "error"
            if status == 200:
                try:
                    backend_id = json.loads(body).get("backend_id", "unknown")
                except ValueError:
                    backend_id = "unparseable"
            hits[backend_id] = hits.get(backend_id, 0) + 1
        readmitted = recovery_ok(
            hits, VICTIM, sticky=case.strategy in ("ip_hash", "source")
        )
        lines += [f"all_backends_healthy_after_recovery={all_up_after}",
                  f"recovery_s={recovery_s}", f"post_recovery_hits={hits}",
                  f"readmitted={readmitted}"]

        bm.append_row(
            STRUCTURED_DIR / "failures.csv", FAILURES_HEADER,
            [run_id, case.mode, case.strategy, case.clients, VICTIM, kill_s,
             detection, failed, wrk_metrics["non_2xx"], recovery_s, readmitted],
        )
        print(f"[{run_id}] detection={detection}s failed={failed} "
              f"recovery={recovery_s}s readmitted={readmitted}", flush=True)
    finally:
        if wrk_proc is not None and wrk_proc.poll() is None:
            wrk_proc.kill()
            wrk_proc.wait(timeout=10)
        if watcher is not None:
            watcher.finish()
        bm.stop_custom_proxy(proc, log)
        _write_timeline(run_id, lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--smoke", action="store_true", help="8 s runs, kill at 4 s")
    args = parser.parse_args()
    cases = bm.select_cases(failure_cases(), args.start, args.limit)
    duration = 8 if args.smoke else args.duration
    for case in cases:
        for index in range(1, args.runs + 1):
            run_id = bm.make_run_id(case, index, datetime.now())
            run_failure_case(case, run_id, duration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
