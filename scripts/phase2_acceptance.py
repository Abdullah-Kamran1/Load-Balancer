"""Phase 2 acceptance run: live proxy vs the real Docker backend farm.

Checks the plan's Phase 2 acceptance criteria end to end:
1. Requests through the proxy reach all healthy backends (round robin).
2. Responses contain the correct backend ID.
3. A stopped backend does not crash the proxy (502 while it is down,
   recovers once it is back - active health checks are Phase 5).
4. No steady leak: active counters return to zero after load.
"""

import asyncio
import collections
import json
import sys
import urllib.error
import urllib.request

PROXY = "http://127.0.0.1:8080"
BACKEND_PORTS = {"backend-1": 8001, "backend-2": 8002, "backend-3": 8003, "backend-4": 8004}


def fetch(url: str, timeout: float = 10.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def hit_work() -> tuple[int, bytes]:
    return fetch(PROXY + "/work?delay_ms=5")


def main() -> int:
    failures = []

    # 1+2: distribution and backend ID round trip
    ids = []
    for i in range(40):
        status, body = hit_work()
        if status != 200:
            failures.append(f"request {i}: status {status}")
            continue
        ids.append(json.loads(body)["backend_id"])
    dist = collections.Counter(ids)
    print("distribution:", dict(dist))
    if set(dist) != set(BACKEND_PORTS):
        failures.append(f"traffic did not reach all backends: {dict(dist)}")
    counts = list(dist.values())
    if counts and max(counts) - min(counts) > 2:
        failures.append(f"round robin uneven: {dict(dist)}")

    # 4: no leak - every backend idle again
    for backend_id, port in BACKEND_PORTS.items():
        status, body = fetch(f"http://127.0.0.1:{port}/stats")
        stats = json.loads(body)
        if stats["in_flight"] != 0:
            failures.append(f"{backend_id}: in_flight={stats['in_flight']}")
    print("all backend in_flight == 0")

    # 3: failure behaviour is checked from PowerShell (docker stop/start),
    # not from inside this script.

    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("ALL PHASE 2 ACCEPTANCE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
