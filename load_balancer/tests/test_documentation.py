"""Documentation-suite artifact tests (Phase: comprehensive docs)."""

from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def created_source_files(root: Path) -> list[str]:
    skip_dirs = {".venv", ".git", ".pytest_cache", ".superpowers", "results", "__pycache__"}
    names = {"Dockerfile", "run.ps1", ".env.example", "haproxy.cfg",
             "requirements-dev.txt", "requirements.txt"}
    found = []
    for path in root.rglob("*"):
        if not path.is_file() or any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix in {".py", ".yml", ".ini"} or path.name in names:
            found.append(path.relative_to(root).as_posix())
    return sorted(found)


def test_created_files_have_reference_sections():
    root = repo_root()
    reference = (root / "docs" / "FILE_REFERENCE.md").read_text(encoding="utf-8")
    created = created_source_files(root)
    assert created, "no files discovered"
    missing = [path for path in created if f"`{path}`" not in reference]
    assert missing == []


def test_flow_overview_covers_all_states_and_pipelines():
    root = repo_root()
    flow = (root / "docs" / "ARCHITECTURE_FLOW.md").read_text(encoding="utf-8")
    required = [
        "START", "READ_REQUEST", "SELECT_BACKEND", "FORWARD", "DRAIN_RESPONSE",
        "UNHEALTHY", "RECOVERING", "UP", "DOWN", "UP n/m", "DOWN n/m",
        "metrics dump", "combined.csv", "baseline_medians.csv",
        "failure_timeline.svg", "final_report.md",
    ]
    assert [token for token in required if token not in flow] == []
    assert flow.count("```mermaid") >= 2
