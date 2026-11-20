"""Phase 9: the final report must link to real artifacts and answer all questions."""

from pathlib import Path

from scripts import validate_report as vr


def test_linked_result_paths_are_extracted():
    text = (
        "See [chart](../results/plots/rps_by_load.svg) and "
        "[data](../results/structured/report_data.csv) and "
        "[docs](benchmark_matrix.md)."
    )
    assert vr.linked_result_paths(text) == [
        "../results/plots/rps_by_load.svg",
        "../results/structured/report_data.csv",
    ]


def test_validate_detects_missing_artifacts_and_questions(tmp_path):
    report = tmp_path / "report.md"
    report.write_text(
        "## 3. Answers\n### 1. First?\n[bad](../results/plots/missing.svg)\n",
        encoding="utf-8",
    )
    errors = vr.validate(report, tmp_path)
    assert any("missing.svg" in e for e in errors)
    assert any("question 2" in e for e in errors)


def test_validate_detects_untraceable_glance_figure(tmp_path):
    report = tmp_path / "report.md"
    report.write_text(
        "## 2. Results at a glance\n"
        "| custom round robin | 943.07 | 12345.67 |\n"
        "## 3. Answers\n"
        + "".join(f"### {n}. Q\n" for n in range(1, 11)),
        encoding="utf-8",
    )
    (tmp_path / "results" / "structured").mkdir(parents=True)
    (tmp_path / "results" / "structured" / "report_data.csv").write_text(
        "metric,mode,strategy,clients,value,evidence\n"
        "rps_median,custom,round_robin,50,943.07,x\n",
        encoding="utf-8",
    )
    errors = vr.validate(report, tmp_path)
    assert any("12345.67" in e for e in errors)
    assert not any("943.07" in e for e in errors)


def test_tracked_final_report_validates():
    repo = Path(__file__).resolve().parents[2]
    assert vr.validate(repo / "docs" / "final_report.md", repo) == []
