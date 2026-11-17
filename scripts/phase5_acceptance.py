"""Phase 5 acceptance run: active health checks + backend state management.

Plan's Phase 5 acceptance criteria:
1. Kill one backend while traffic is running -> it is marked DOWN after
   the consecutive-failure threshold.
2. New requests stop being assigned to the DOWN backend.
3. Record how many requests fail during the detection window.
4. Restart the backend -> it returns to rotation after the recovery rule
   (2 consecutive successful /health probes).

Modes:
  python scripts/phase5_acceptance.py          -> in-process: real asyncio
      stub backends + real ProxyServer + real HealthMonitor over TCP,
      fast knobs (interval 0.2 s, 3 failures -> DOWN in ~0.6 s).
  python scripts/phase5_acceptance.py --live   -> against an already-running
      proxy on 127.0.0.1:8080 backed by the real Docker farm. Kills
      lb-backend-2 with `docker kill`, measures detection time, restarts
      it with `docker start`, verifies re-admission. The proxy must be
      running with its default health knobs (LB_HEALTH_* unset).
"""

import asyncio
import collections
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from load_balancer.algorithms.round_robin import RoundRobinStrategy
from load_balancer.health.monitor import HealthMonitor
from load_balancer.health.registry import BackendRegistry
from load_balancer.models import Backend
from load_balancer.proxy import ProxyServer

LIVE_BACKEND_TO_KILL = "backend-2"  # docker service lb-backend-2, port 8002


class StubBackend:
    """Close-framed HTTP/1.1 stub serving 200 on every target (incl. /health)."""

    def __init__(self, backend_id: str, port: int = 0):
        self.backend_id = backend_id
        self._bind_port = port
        self._server: asyncio.AbstractServer | None = None
        self.port = port

    async def start(self) -> "StubBackend":
        for _ in range(20):  # freshly-closed ports can lag on Windows
            try:
                self._server = await asyncio.start_server(
                    self._handle, "127.0.0.1", self._bind_port
                )
                break
            except OSError:
                await asyncio.sleep(0.05)
        else:
            raise OSError(f"could not bind port {self._bind_port}")
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            while b"\r\n\r\n" not in await reader.read(65536):
                pass
            body = json.dumps({"backend_id": self.backend_id}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                + f"content-length: {len(body)}\r\nconnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


async def one_request(port: int, timeout: float = 5.0) -> tuple[int, str]:
    """One fresh connection, one request; returns (status, backend_id or error)."""
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError as e:
        return 0, f"connect-error:{e.errno}"
    try:
        writer.write(b"GET / HTTP/1.1\r\nhost: t\r\nconnection: close\r\n\r\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), timeout)
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ")[1])
        return status, json.loads(body)["backend_id"]
    except Exception as e:  # timeout, reset, malformed -> a failed request
        return 0, f"error:{type(e).__name__}"
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def wait_until(pred, timeout: float = 5.0, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


async def traffic_loop(port: int, log: list, stop: asyncio.Event) -> None:
    """Hammer the proxy with sequential requests until stop is set."""
    while not stop.is_set():
        t = time.time()
        status, info = await one_request(port)
        log.append({"t": t, "status": status, "info": info})


async def run_acceptance() -> int:
    failures: list[str] = []
    stubs = [await StubBackend(f"backend-{i}").start() for i in range(1, 5)]
    try:
        backends = [
            Backend(backend_id=s.backend_id, host="127.0.0.1", port=s.port)
            for s in stubs
        ]
        ports = {b.backend_id: b.port for b in backends}
        registry = BackendRegistry(backends)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        # fast knobs: detection ~= 3 * 0.2 s = 0.6 s
        monitor = HealthMonitor(
            registry, interval=0.2, timeout=0.5,
            failure_threshold=3, recovery_threshold=2,
        )
        async with await proxy.start():
            await monitor.start()
            log: list[dict] = []
            stop = asyncio.Event()
            task = asyncio.create_task(traffic_loop(proxy.port, log, stop))

            victim = backends[0]  # backend-1
            # --- warm-up: traffic spreads over all four backends, no failures
            if not await wait_until(
                lambda: {e["info"] for e in log if e["status"] == 200}
                >= {b.backend_id for b in backends}
            ):
                failures.append(f"warm-up never reached all 4 backends: "
                                f"{dict(collections.Counter(e['info'] for e in log))}")
            pre = [e for e in log]
            if any(e["status"] != 200 for e in pre):
                failures.append("requests failed before the kill")

            # --- 1: kill the backend under traffic, measure kill -> DOWN
            t_kill = time.time()
            await stubs[0].close()
            if not await wait_until(lambda: victim.healthy is False, timeout=5):
                failures.append("backend-1 was never marked DOWN after the kill")
            t_down = victim.state_transitions[-1]["timestamp"]
            detection = t_down - t_kill
            # On this Windows host a connect to a dead port is silently
            # dropped (no RST), so each failing probe consumes the full
            # probe timeout: detection ~= threshold * (timeout + interval)
            # = 3 * (0.5 + 0.2) ~= 2.1s. On hosts with instant RST it is
            # ~= threshold * interval = 0.6s.
            print(f"backend-1 killed at t0; marked DOWN after {detection:.3f}s "
                  f"(expected ~= 3 * (0.5s probe timeout + 0.2s interval) "
                  f"on a silent-drop host, 3 * 0.2s when refuses are instant)")
            if not 0 < detection < 2.5:
                failures.append(f"detection time out of range: {detection:.3f}s")

            window = [e for e in log if t_kill < e["t"] <= t_down]
            failed_in_window = [e for e in window if e["status"] != 200]
            print(f"requests failed during the detection window: "
                  f"{len(failed_in_window)} (of {len(window)} issued)")
            if not failed_in_window:
                failures.append("no request failed during the detection window "
                                "(traffic did not reach the dead backend)")

            # --- 2: after DOWN, new requests never go to backend-1.
            # Sequential fixed batch: deterministic even though a straggler
            # request aimed at the dead port can still be draining.
            stop.set()
            await task
            post_down = [(await one_request(proxy.port)) for _ in range(12)]
            bad = [r for r in post_down if r[0] != 200 or r[1] == "backend-1"]
            if bad:
                failures.append(f"{len(bad)} requests after DOWN hit backend-1 "
                                f"or failed: {bad[:3]}")
            else:
                print(f"post-DOWN batch (12 sequential requests): zero failures, "
                      f"zero assignments to backend-1: OK")

            # --- 4: restart the backend on the same port -> recovery rule
            t_restart = time.time()
            stubs[0] = await StubBackend("backend-1", port=ports["backend-1"]).start()
            if not await wait_until(lambda: victim.healthy is True, timeout=10):
                failures.append("backend-1 never returned to healthy after restart")
            t_up = victim.state_transitions[-1]["timestamp"]
            states = [e["state"] for e in victim.state_transitions]
            if states != ["down", "up"]:
                failures.append(f"backend-1 transition history wrong: {states}")
            recovery = t_up - t_restart
            print(f"backend-1 restarted; marked UP after {recovery:.3f}s "
                  f"(recovery = 2 consecutive successes * 0.2s interval)")

            # --- and it is back in rotation
            stop2 = asyncio.Event()
            log2: list[dict] = []
            task2 = asyncio.create_task(traffic_loop(proxy.port, log2, stop2))
            back_in = await wait_until(
                lambda: any(e["info"] == "backend-1" and e["status"] == 200 for e in log2),
                timeout=10,
            )
            stop2.set()
            await task2
            if not back_in:
                failures.append("backend-1 never reappeared in rotation after recovery")
            else:
                dist = dict(collections.Counter(e["info"] for e in log2))
                print(f"backend-1 back in rotation: {dist}")
            await monitor.stop()
    finally:
        for s in stubs:
            await s.close()

    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("ALL PHASE 5 ACCEPTANCE CHECKS PASSED")
    return 0


# ------------------------------------------------------------------ live


def http_get(url: str, timeout: float = 4.0) -> tuple[int, str]:
    """Blocking GET; returns (status, body-or-error-tag)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.reason
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, f"error:{type(e).__name__}"


def run_live() -> int:
    failures = []
    proxy_url = "http://127.0.0.1:8080/"
    kill_container = f"lb-{LIVE_BACKEND_TO_KILL}"

    # --- pre-flight: farm up, proxy up, all four backends serving
    statuses = collections.Counter()
    ids = []
    for _ in range(40):
        status, body = http_get(proxy_url + "work?delay_ms=5")
        statuses[status] += 1
        if status == 200:
            ids.append(json.loads(body)["backend_id"])
    print("pre-kill distribution:", dict(collections.Counter(ids)), dict(statuses))
    if set(statuses) != {200} or set(ids) != {f"backend-{i}" for i in range(1, 5)}:
        failures.append("pre-kill state not clean (proxy/farm not fully up)")
        print("FAILURES:", failures)
        return 1

    log: list[dict] = []

    def hit() -> None:
        t = time.time()
        status, body = http_get(proxy_url + "work?delay_ms=5")
        info = "error"
        if status == 200:
            try:
                info = json.loads(body)["backend_id"]
            except (ValueError, KeyError):
                info = "unparseable"
        log.append({"t": t, "status": status, "info": info})

    # --- 1: kill the backend under running traffic
    t_kill = time.time()
    subprocess.run(["docker", "kill", kill_container], check=True,
                   capture_output=True, text=True)
    print(f"{kill_container} killed at t0 (docker kill); traffic continues")

    # detection, measured observationally: the LAST failed request marks the
    # last time the proxy still assigned the dead backend (there is no retry
    # and no /lb endpoint to read registry state from). Exclusion is
    # confirmed by 4 consecutive successful hits without the victim -- a
    # full round-robin cycle over the survivors.
    last_fail = None
    clean_streak = 0
    while time.time() - t_kill < 15:
        hit()
        e = log[-1]
        if e["status"] != 200:
            last_fail = e["t"]
            clean_streak = 0
        elif e["info"] != LIVE_BACKEND_TO_KILL:
            clean_streak += 1
            if last_fail is not None and clean_streak >= 4:
                break
        else:
            clean_streak = 0  # 200 from the victim: kill race, keep going
    if last_fail is None:
        failures.append("no request ever failed after the kill (traffic "
                        "never reached the dead backend)")
    else:
        detection = last_fail - t_kill
        # expected: 3 consecutive probe failures x 1s interval ~= 2-3.5s
        print(f"last failed assignment to {LIVE_BACKEND_TO_KILL} at "
              f"t0+{detection:.2f}s; exclusion confirmed by 4 clean hits "
              f"(expected ~= 3 probe failures x 1s interval)")
        if not 1.0 < detection < 8.0:
            failures.append(f"detection time out of range: {detection:.2f}s")

        window = [e for e in log if t_kill < e["t"] <= last_fail]
        failed_in_window = [e for e in window if e["status"] != 200]
        print(f"requests failed while the dead backend was still assigned: "
              f"{len(failed_in_window)} (of {len(window)} issued)")
        if not failed_in_window:
            failures.append("no request failed during the detection window")

        # --- 2: after exclusion, a fixed batch must be all clean
        post = []
        for _ in range(12):
            t = time.time()
            status, body = http_get(proxy_url + "work?delay_ms=5")
            info = json.loads(body)["backend_id"] if status == 200 else "error"
            post.append((status, info))
        bad = [r for r in post if r[0] != 200 or r[1] == LIVE_BACKEND_TO_KILL]
        if bad:
            failures.append(f"{len(bad)} bad requests post-DOWN: {bad[:3]}")
        else:
            print("post-DOWN batch (12 sequential requests): zero failures, "
                  f"zero assignments to {LIVE_BACKEND_TO_KILL}: OK")

    # --- 4: restart the backend; verify re-admission after recovery rule
    subprocess.run(["docker", "start", kill_container], check=True,
                   capture_output=True, text=True)
    t_restart = time.time()
    direct_ok = None
    while time.time() - t_restart < 30:
        status, _ = http_get(
            f"http://127.0.0.1:800{int(LIVE_BACKEND_TO_KILL[-1])}/health", 2.0
        )
        if status == 200:
            direct_ok = time.time()
            break
        time.sleep(0.5)
    if direct_ok is None:
        failures.append("restarted container never served /health directly (30s)")
    else:
        print(f"container serving again {direct_ok - t_restart:.2f}s after "
              f"docker start; waiting for the proxy to re-admit it "
              f"(2 consecutive successful probes)...")
        re_admitted = None
        while time.time() - direct_ok < 30:
            hit()
            # only entries AFTER the container came back count: the log also
            # holds pre-kill hits that legitimately contain the victim
            since = [e for e in log if e["t"] > direct_ok]
            if any(e["info"] == LIVE_BACKEND_TO_KILL and e["status"] == 200
                   for e in since[-20:]):
                re_admitted = time.time()
                break
        if re_admitted is None:
            failures.append("restarted backend never returned to rotation (30s)")
        else:
            print(f"re-admitted {re_admitted - direct_ok:.2f}s after the container "
                  f"started serving (recovery rule: 2 consecutive successes)")

            # sustained rotation: the re-admitted backend keeps its ~1/4 share
            t_sustain = time.time()
            while time.time() - t_sustain < 2:
                hit()
            since = [e for e in log if e["t"] > re_admitted and e["status"] == 200]
            dist = collections.Counter(e["info"] for e in since)
            print("post-recovery distribution:", dict(dist))
            if dist.get(LIVE_BACKEND_TO_KILL, 0) < 5:
                failures.append(f"re-admitted backend not sustained in rotation: "
                                f"{dict(dist)}")

    # raw evidence for results/raw: timeline from just before the kill
    print("--- raw log (t = offset from kill) ---")
    for e in log:
        if e["t"] > t_kill - 1:
            print(f"  t0+{e['t'] - t_kill:7.3f}  status={e['status']}  {e['info']}")

    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("ALL LIVE PHASE 5 ACCEPTANCE CHECKS PASSED")
    return 0


def main() -> int:
    if "--live" in sys.argv:
        return run_live()
    return asyncio.run(run_acceptance())


if __name__ == "__main__":
    sys.exit(main())
