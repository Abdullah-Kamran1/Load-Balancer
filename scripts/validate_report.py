"""Phase 9: cheap structural validation of the final report."""

from __future__ import annotations

import re
import sys
from pathlib import Path

LINK_RE = re.compile(r"\[[^\]]+\]\((\.\./results/[^)]+)\)")
REQUIRED_PLOTS = [
    "rps_by_load.svg", "p50_by_load.svg", "p75_by_load.svg",
    "p90_by_load.svg", "p99_by_load.svg", "backend_shares.svg",
    "failure_timeline.svg", "cpu_comparison.svg",
]


def linked_result_paths(text: str) -> list[str]:
    return LINK_RE.findall(text)


def validate(report: Path, repo_root: Path) -> list[str]:
    text = report.read_text(encoding="utf-8")
    errors = []
    for rel in linked_result_paths(text):
        if not (report.parent / rel).resolve().exists():
            errors.append(f"missing artifact: {rel}")
    for number in range(1, 11):
        if not re.search(rf"^###\s+{number}\.", text, re.MULTILINE):
            errors.append(f"missing question {number}")
    plots = repo_root / "results" / "plots"
    for name in REQUIRED_PLOTS:
        if not (plots / name).exists():
            errors.append(f"missing required plot: {name}")
    return errors


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    errors = validate(repo / "docs" / "final_report.md", repo)
    if errors:
        for error in errors:
            print(error)
        return 1
    print("final report: all links, questions and required plots present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
