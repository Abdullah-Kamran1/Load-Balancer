"""Phase 7 acceptance run: HAProxy equivalent balancing + health checks.

Plan acceptance criteria (load_balancer_project_plan.md, Phase 7):
1. HAProxy routes requests to all healthy backends.
2. It removes a failed backend from rotation.
3. It recovers a backend after restart.
4. Its configuration and version are saved with benchmark results.

What this script does (against Docker Desktop + the compose farm):
- starts the farm + HAProxy (profile "haproxy"),
- verifies each frontend's routing contract:
    :8091 roundrobin  -> every healthy backend is used,
    :8092 leastconn   -> every healthy backend is used (sequential traffic),
    :8093 source      -> one client IP stays on one backend,
- kills lb-backend-2 under traffic on :8091, polls the stats CSV until the
  server is no longer UP, records detection time + failed requests,
- verifies the post-DOWN batch never touches the victim,
- restarts the container, waits until the stats report UP and traffic
  returns to it,
- writes results/structured/haproxy_environment.txt (version + full
  config + hashes) and results/raw/haproxy_acceptance_<run_id>.txt
  (timeline + synthesized snapshot).

Usage: python scripts/haproxy_acceptance.py [--no-start]
"""

import argparse
import collections
import hashlib
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from load_balancer.metrics.haproxy_stats import (  # noqa: E402
    parse_haproxy_stats_csv,
    synthesize_snapshot,
)

STATS_CSV_URL = "http://127.0.0.1:8404/stats;csv"
STATS_PAGE_URL = "http://127.0.0.1:8404/stats"
FRONTENDS = {
    "roundrobin": 8091,
    "leastconn": 8092,
    "source": 8093,
}
VICTIM = "backend-2"
VICTIM_CONTAINER = "lb-backend-2"
VICTIM_DIRECT = "http://127.0.0.1:8002/health"
CONFIG_PATH = REPO_ROOT / "haproxy" / "haproxy.cfg"
ENV_PATH = REPO_ROOT / "results" / "structured" / "haproxy_environment.txt"
RAW_DIR = REPO_ROOT / "results" / "raw"


def run(cmd: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout
    )


def http_get(url: str, timeout: float = 4.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.reason
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, f"error:{type(e).__name__}"


def get_stats() -> dict:
    status, body = http_get(STATS_CSV_URL)
    if status != 200:
        raise RuntimeError(f"stats CSV unavailable: {status} {body}")
    return parse_haproxy_stats_csv(body)


def server_status(stats: dict, pxname: str, server: str) -> str:
    return stats["servers"].get(pxname, {}).get(server, {}).get("status", "MISSING")


def wait_for(pred, timeout: float, step: float = 0.1, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if pred():
                return True
        except Exception:  # transient stats/docker hiccups while polling
            pass
        time.sleep(step)
    return False


def ensure_stack() -> None:
    info = run(["docker", "info"])
    if info.returncode != 0:
        raise RuntimeError("Docker daemon not reachable")
    up = run(["docker", "compose", "--profile", "haproxy", "up", "-d"])
    if up.returncode != 0:
        raise RuntimeError(f"docker compose up failed: {up.stderr.strip()}")

    def all_up() -> bool:
        stats = get_stats()
        return all(
            server_status(stats, "be_roundrobin", f"backend-{i}") == "UP"
            for i in range(1, 5)
        )

    if not wait_for(all_up, timeout=90, what="all backends UP in HAProxy"):
        stats = get_stats()
        raise RuntimeError(
            "HAProxy never saw all four backends UP: "
            f"{ {s: server_status(stats, 'be_roundrobin', s) for s in stats['servers'].get('be_roundrobin', {})} }"
        )


def hit(port: int) -> tuple[int, str]:
    status, body = http_get(f"http://127.0.0.1:{port}/work?delay_ms=5")
    if status == 200:
        try:
            return status, json.loads(body).get("backend_id", "unknown")
        except (ValueError, AttributeError):
            return status, "unparseable"
    # failure: keep the exception/status detail for diagnostics
    return status, body


def frontend_checks(log: list[str]) -> list[str]:
    failures: list[str] = []
    for mode, port in FRONTENDS.items():
        ids = []
        bad = []
        for _ in range(24):
            status, backend = hit(port)
            ids.append(backend)
            if status != 200:
                bad.append((status, backend))
        dist = dict(collections.Counter(ids))
        log.append(f"frontend :{port} ({mode}): {dist}")

        if bad:
            failures.append(f"{mode} frontend had failed requests: {bad[:3]}")
        if mode == "source":
            if len(set(ids)) != 1:
                failures.append(
                    f"source frontend not sticky: {dist} (expected one backend)"
                )
        elif set(ids) != {f"backend-{i}" for i in range(1, 5)}:
            failures.append(f"{mode} frontend did not use all 4 backends: {dist}")
    return failures


def save_environment(run_id: str) -> str:
    version = run(["docker", "exec", "lb-haproxy", "haproxy", "-vv"]).stdout
    if not version.strip():
        version = run(["docker", "run", "--rm", "haproxy:3.4-alpine", "haproxy", "-vv"]).stdout
    digest = run(
        ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", "haproxy:3.4-alpine"]
    ).stdout.strip()
    docker_version = run(
        ["docker", "version", "--format", "client={{.Client.Version}} server={{.Server.Version}}"]
    ).stdout.strip()
    git_head = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    config_text = CONFIG_PATH.read_text(encoding="utf-8")
    config_sha = hashlib.sha256(config_text.encode("utf-8")).hexdigest()

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text(
        "\n".join(
            [
                f"run_id={run_id}",
                f"captured_at={time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime())}",
                "haproxy_image=haproxy:3.4-alpine",
                f"haproxy_image_digest={digest}",
                f"config_path={CONFIG_PATH.relative_to(REPO_ROOT).as_posix()}",
                f"config_sha256={config_sha}",
                f"git_head={git_head}",
                f"docker_version={docker_version}",
                "---- haproxy -vv ----",
                version.strip(),
                "---- configuration (verbatim copy) ----",
                config_text,
            ]
        ),
        encoding="utf-8",
    )
    return version.splitlines()[0] if version.strip() else "unknown"


def acceptance(log: list[str]) -> list[str]:
    failures: list[str] = []
    port = FRONTENDS["roundrobin"]

    # --- warm-up under live traffic
    request_log: list[dict] = []
    stop = threading.Event()

    def traffic() -> None:
        while not stop.is_set():
            t = time.time()
            status, backend = hit(port)
            request_log.append({"t": t, "status": status, "backend": backend})

    thread = threading.Thread(target=traffic, daemon=True)
    thread.start()
    warm = wait_for(
        lambda: {e["backend"] for e in request_log if e["status"] == 200}
        >= {f"backend-{i}" for i in range(1, 5)},
        timeout=20,
    )
    if not warm:
        failures.append(f"warm-up never reached all 4 backends: {request_log[-5:]}")
    if any(e["status"] != 200 for e in request_log):
        failures.append("requests failed before the kill")

    # --- 1: kill the backend under traffic
    t_kill = time.time()
    killed = run(["docker", "kill", VICTIM_CONTAINER])
    if killed.returncode != 0:
        stop.set()
        thread.join(timeout=5)
        return failures + [f"docker kill failed: {killed.stderr.strip()}"]
    log.append(f"t0: docker kill {VICTIM_CONTAINER}")

    detected = wait_for(
        # HAProxy reports transitional "UP 2/3" / "UP 1/3" while the fall
        # counter drains; only status "DOWN" means removed from rotation.
        lambda: server_status(get_stats(), "be_roundrobin", VICTIM).startswith("DOWN"),
        timeout=20,
        step=0.1,
    )
    t_down = time.time()
    if not detected:
        failures.append(f"{VICTIM} was never removed from rotation (20s)")
    else:
        log.append(
            f"t0+{t_down - t_kill:.3f}s: stats report {VICTIM} DOWN "
            f"(fall 3; failed checks time out: timeout 1s + inter 1s each)"
        )

    stop.set()
    thread.join(timeout=10)

    # --- 2: it is out of rotation. Small settle window: requests assigned
    # just before the health check flipped the server DOWN are still
    # draining; the criterion is about NEW assignments after DOWN.
    time.sleep(0.5)
    post = [hit(port) for _ in range(12)]
    bad = [r for r in post if r[0] != 200 or r[1] == VICTIM]
    if bad:
        failures.append(f"{len(bad)} post-DOWN requests failed or hit {VICTIM}: {bad[:3]}")
    else:
        log.append("post-DOWN batch (12 sequential requests): all 200, victim unused")

    window = [e for e in request_log if t_kill < e["t"] <= t_down]
    failed_in_window = [e for e in window if e["status"] != 200]
    log.append(
        f"detection window: {len(window)} requests, "
        f"{len(failed_in_window)} failed"
    )
    if detected and not failed_in_window:
        failures.append("no request failed during the detection window")

    # --- 3: restart -> re-admission
    t_restart = time.time()
    started = run(["docker", "start", VICTIM_CONTAINER])
    if started.returncode != 0:
        return failures + [f"docker start failed: {started.stderr.strip()}"]
    log.append(f"t0+{time.time() - t_kill:.3f}s: docker start {VICTIM_CONTAINER}")

    direct_ok = wait_for(
        lambda: http_get(VICTIM_DIRECT, timeout=2.0)[0] == 200,
        timeout=30,
        step=0.5,
    )
    if not direct_ok:
        failures.append("restarted container never served /health directly (30s)")
    else:
        t_direct = time.time()
        log.append(
            f"t0+{t_direct - t_kill:.3f}s: container serves /health directly"
        )
        up = wait_for(
            lambda: server_status(get_stats(), "be_roundrobin", VICTIM) == "UP",
            timeout=15,
            step=0.1,
        )
        if not up:
            failures.append(f"{VICTIM} never returned to UP after restart (15s)")
        else:
            log.append(
                f"t0+{time.time() - t_kill:.3f}s: stats report {VICTIM} UP "
                f"(rise 2 x inter 1s)"
            )

        # sustained rotation after re-admission
        since = time.time()
        back = []
        while time.time() - since < 2.0:
            back.append(hit(port))
        dist = collections.Counter(b for _, b in back)
        log.append(f"post-recovery distribution: {dict(dist)}")
        if sum(1 for s, b in back if s == 200 and b == VICTIM) < 5:
            failures.append(f"re-admitted backend not sustained in rotation: {dict(dist)}")

    log.append(f"detection time: {t_down - t_kill:.3f}s")
    return failures


def write_raw(run_id: str, log: list[str]) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / f"haproxy_acceptance_{run_id}.txt"
    path.write_text("\n".join(log) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-start", action="store_true", help="do not start the stack")
    args = parser.parse_args()

    run_id = time.strftime("%Y%m%d-%H%M%S")
    log: list[str] = [f"run_id={run_id}", "Phase 7 HAProxy acceptance"]
    failures: list[str] = []

    try:
        if not args.no_start:
            ensure_stack()
        stats = get_stats()
        log.append(
            "HAProxy stats reachable; roundrobin servers: "
            + ", ".join(
                f"{s}={v['status']}"
                for s, v in stats["servers"]["be_roundrobin"].items()
            )
        )
        failures += frontend_checks(log)
        failures += acceptance(log)

        # snapshot after the run (raw evidence; combined CSV rows are written
        # by scripts/parse_wrk.py --mode haproxy)
        snap = synthesize_snapshot(get_stats(), "roundrobin", "be_roundrobin")
        snap_path = RAW_DIR / f"haproxy_snapshot_{run_id}.json"
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(json.dumps(snap, indent=2), encoding="utf-8")
        log.append(f"snapshot: {snap_path.relative_to(REPO_ROOT).as_posix()}")
    except Exception as e:  # noqa: BLE001 - report and keep the evidence
        failures.append(f"exception: {type(e).__name__}: {e}")

    try:
        version = save_environment(run_id)
        log.append(f"environment saved: {ENV_PATH.relative_to(REPO_ROOT).as_posix()}")
        log.append(f"HAProxy version: {version}")
    except Exception as e:  # noqa: BLE001
        failures.append(f"could not save environment: {type(e).__name__}: {e}")

    for line in log:
        print(line)
    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
    else:
        print("ALL PHASE 7 ACCEPTANCE CHECKS PASSED")

    raw = write_raw(run_id, log + (["FAILURES:"] + failures if failures else []))
    print(f"raw timeline: {raw.relative_to(REPO_ROOT).as_posix()}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
