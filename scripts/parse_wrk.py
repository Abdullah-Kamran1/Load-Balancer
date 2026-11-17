"""Combine one wrk raw output file with one proxy snapshot JSON into the
structured combined CSV (Phase 6 step 4).

Usage:
  python scripts/parse_wrk.py --wrk results/raw/<run>.txt \
      --snapshot results/raw/<run>_snapshot.json \
      --run-id 20261117-163000 --mode custom \
      [--csv results/structured/combined.csv]

The snapshot JSON is produced by the proxy process (LB_METRICS_DUMP_PATH
in a live run, or collect_run_snapshot + write_json in an in-process
script). The combined CSV gains one row per invocation; the header is
written on the first call only.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from load_balancer.metrics.wrk_parser import append_combined_csv, parse_wrk_output


def _read_text(path: Path) -> str:
    """Read wrk output; PowerShell 5.1 redirects are UTF-16, so try that too."""
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrk", required=True, help="raw wrk output file")
    parser.add_argument("--snapshot", required=True, help="proxy snapshot JSON")
    parser.add_argument("--run-id", required=True, help="unique run id, e.g. 20261117-163000")
    parser.add_argument("--mode", default="custom", help="custom | haproxy (Phase 7)")
    parser.add_argument(
        "--csv", default="results/structured/combined.csv",
        help="combined CSV path (appended, never overwritten)",
    )
    args = parser.parse_args()

    wrk_text = _read_text(Path(args.wrk))
    wrk = parse_wrk_output(wrk_text)
    if wrk["requests_total"] is None:
        print(f"error: {args.wrk} does not look like wrk output "
              f"(no 'requests in ...' line)", file=sys.stderr)
        return 1

    import json

    snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
    append_combined_csv(wrk, snapshot, Path(args.csv), run_id=args.run_id, mode=args.mode)
    print(f"combined row appended to {args.csv} (run_id={args.run_id}, mode={args.mode})")
    print(f"  wrk: requests={wrk['requests_total']} rps={wrk['requests_per_sec']} "
          f"p99={wrk['latency_ms']['p99']}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
