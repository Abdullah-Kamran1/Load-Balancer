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
