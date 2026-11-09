"""Phase 1 acceptance test: verify the /work concurrency limit.

Fires 20 parallel 500 ms /work requests at backend-3 (MAX_CONCURRENCY=4).
Expected result class: 200 responses succeed (in two waves of ~4), and
requests over the limit receive 503 busy responses instead of queueing.
"""

import concurrent.futures
import urllib.error
import urllib.request

URL = "http://localhost:8003/work?delay_ms=500"


def hit(_):
    try:
        with urllib.request.urlopen(URL, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return "ERR"


def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(hit, range(20)))

    counts = {}
    for status in results:
        counts[status] = counts.get(status, 0) + 1
    for status, count in sorted(counts.items(), key=lambda kv: str(kv[0])):
        print(f"HTTP {status} x {count}")


if __name__ == "__main__":
    main()
