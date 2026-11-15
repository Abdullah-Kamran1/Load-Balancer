"""Phase 4 acceptance run: all three balancing algorithms.

Plan's Phase 4 acceptance criteria:
1. Each strategy selects only healthy backends.
2. Round robin cycles through available backends.
3. Least active requests responds to different active request counts.
4. IP hash is deterministic for the same client IP and backend set.

Modes:
  python scripts/phase4_acceptance.py          -> in-process acceptance:
      spins up real asyncio HTTP stub backends + the real ProxyServer and
      drives everything over real TCP sockets.
  python scripts/phase4_acceptance.py --live   -> distribution check against
      an already-running proxy on 127.0.0.1:8080 backed by the real Docker
      farm (start it with the LB_STRATEGY you want measured).
"""

import asyncio
import collections
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from load_balancer.algorithms.ip_hash import IpHashStrategy
from load_balancer.algorithms.least_active import LeastActiveRequestsStrategy
from load_balancer.algorithms.round_robin import RoundRobinStrategy
from load_balancer.health.registry import BackendRegistry
from load_balancer.models import Backend
from load_balancer.proxy import ProxyServer


class StubBackend:
    """Minimal close-framed HTTP/1.1 server for acceptance driving."""

    def __init__(self, backend_id: str, delay_ms: int = 0):
        self.backend_id = backend_id
        self.delay_ms = delay_ms
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> "StubBackend":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
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
            if self.delay_ms:
                await asyncio.sleep(self.delay_ms / 1000)
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


async def one_request(port: int, timeout: float = 5.0) -> tuple[int, bytes]:
    """One fresh connection, one request, read to EOF (close-framed)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(b"GET / HTTP/1.1\r\nhost: t\r\nconnection: close\r\n\r\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), timeout)
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ")[1])
        return status, body
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


def backend_ids(bodies) -> list[str]:
    return [json.loads(b)["backend_id"] for b in bodies]


async def run_acceptance() -> int:
    failures: list[str] = []
    stubs = [await StubBackend(f"stub-{i}").start() for i in range(1, 5)]
    try:
        # --- 1+2: each strategy selects only healthy backends; round robin
        # cycles evenly through the available (healthy) ones.
        for name, strategy in [
            ("round_robin", RoundRobinStrategy()),
            ("least_active_requests", LeastActiveRequestsStrategy()),
            ("ip_hash", IpHashStrategy()),
        ]:
            backends = [
                Backend(backend_id=s.backend_id, host="127.0.0.1", port=s.port)
                for s in stubs
            ]
            registry = BackendRegistry(backends)
            registry.mark_down(backends[1])  # stub-2 is DOWN
            proxy = ProxyServer(registry=registry, strategy=strategy)
            async with await proxy.start():
                results = [await one_request(proxy.port) for _ in range(12)]
            statuses = {s for s, _ in results}
            ids = backend_ids([b for _, b in results])
            if statuses != {200}:
                failures.append(f"{name}: non-200 statuses {statuses}")
            if "stub-2" in ids:
                failures.append(f"{name}: routed to unhealthy stub-2")
            print(f"{name}: healthy-only distribution {dict(collections.Counter(ids))}")

        backends = [
            Backend(backend_id=s.backend_id, host="127.0.0.1", port=s.port)
            for s in stubs
        ]
        registry = BackendRegistry(backends)
        registry.mark_down(backends[1])
        rr = RoundRobinStrategy()
        proxy = ProxyServer(registry=registry, strategy=rr)
        async with await proxy.start():
            cycle = [await one_request(proxy.port) for _ in range(9)]
        ids = backend_ids([b for _, b in cycle])
        expected = ["stub-1", "stub-3", "stub-4"] * 3
        if ids != expected:
            failures.append(f"round robin cycle wrong: {ids}")
        else:
            print("round robin cycles the healthy set in order: OK")

        # --- 3: least active responds to different active request counts.
        slow = await StubBackend("slow", delay_ms=700).start()
        idle = await StubBackend("idle").start()
        try:
            pair = [
                Backend(backend_id=s.backend_id, host="127.0.0.1", port=s.port)
                for s in (slow, idle)
            ]
            proxy = ProxyServer(
                registry=BackendRegistry(pair),
                strategy=LeastActiveRequestsStrategy(),
            )
            async with await proxy.start():
                # Hold one request in flight on `slow` (tie at 0 broke to the
                # first backend; its delay keeps active_requests at 1).
                held_r, held_w = await asyncio.open_connection("127.0.0.1", proxy.port)
                held_w.write(b"GET / HTTP/1.1\r\nhost: t\r\nconnection: close\r\n\r\n")
                await held_w.drain()
                await asyncio.sleep(0.15)
                if pair[0].active_requests != 1:
                    failures.append(
                        f"least active: expected in-flight on 'slow', got {pair[0].active_requests}"
                    )
                status, body = await one_request(proxy.port)
                if json.loads(body)["backend_id"] != "idle":
                    failures.append(
                        f"least active did not avoid the busy backend: {body!r}"
                    )
                held_raw = await asyncio.wait_for(held_r.read(-1), 5)
                if b'"slow"' not in held_raw:
                    failures.append("held request did not complete on 'slow'")
                held_w.close()
                try:
                    await held_w.wait_closed()
                except (ConnectionError, OSError):
                    pass
            print("least active avoids busy backend and returns after release: OK")
        finally:
            await slow.close()
            await idle.close()

        # --- 4: ip hash deterministic for same client IP and backend set.
        proxy = ProxyServer(
            registry=BackendRegistry(
                [
                    Backend(backend_id=s.backend_id, host="127.0.0.1", port=s.port)
                    for s in stubs
                ]
            ),
            strategy=IpHashStrategy(),
        )
        async with await proxy.start():
            ids = backend_ids([(await one_request(proxy.port))[1] for _ in range(6)])
        if len(set(ids)) != 1:
            failures.append(f"ip hash not deterministic for 127.0.0.1: {ids}")
        else:
            print(f"ip hash deterministic for 127.0.0.1 -> {ids[0]}: OK")
    finally:
        for s in stubs:
            await s.close()

    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("ALL PHASE 4 ACCEPTANCE CHECKS PASSED")
    return 0


def run_live() -> int:
    """Distribution check against an already-running proxy + real farm."""
    failures = []
    ids = []
    statuses = collections.Counter()
    for _ in range(40):
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:8080/work?delay_ms=5", timeout=10
            ) as resp:
                statuses[resp.status] += 1
                ids.append(json.loads(resp.read())["backend_id"])
        except urllib.error.HTTPError as e:
            statuses[e.code] += 1
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            statuses[f"error:{e}"] += 1
    dist = collections.Counter(ids)
    print("live statuses:", dict(statuses))
    print("live distribution:", dict(dist))
    if set(statuses) != {200}:
        failures.append(f"non-200 statuses: {dict(statuses)}")
    if set(dist) - {"backend-1", "backend-2", "backend-3", "backend-4"}:
        failures.append(f"unexpected backend ids: {dict(dist)}")
    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("LIVE DISTRIBUTION CHECK PASSED")
    return 0


def main() -> int:
    if "--live" in sys.argv:
        return run_live()
    return asyncio.run(run_acceptance())


if __name__ == "__main__":
    sys.exit(main())
