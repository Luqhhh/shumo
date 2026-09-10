from __future__ import annotations

from pathlib import Path

import pytest

from microgrid.cases import run_case
from microgrid.checks import assert_release_ready, collect_blockers
from microgrid.cli import main
from microgrid.schemas import PendingDecisionError, ReleaseBlockedError


def _write_pending_decisions(repo: Path) -> None:
    (repo / "configs").mkdir(parents=True, exist_ok=True)
    (repo / "paper").mkdir(parents=True, exist_ok=True)
    (repo / "paper" / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}x\\end{document}\n", encoding="utf-8"
    )
    (repo / "configs" / "decisions.toml").write_text(
        "\n".join(
            [
                "[decisions.D_TIME]",
                'status = "pending"',
                'choice = ""',
                'rationale = ""',
                'confirmed_by = ""',
                'confirmed_at = ""',
                'source = "test"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_pending_decision_blocks_case(tmp_path):
    repo = tmp_path / "repo"
    _write_pending_decisions(repo)
    with pytest.raises(PendingDecisionError) as excinfo:
        run_case("q1", repo)
    assert "D-TIME" in excinfo.value.decision_ids


def test_final_collect_blockers_is_non_empty(tmp_path):
    repo = tmp_path / "repo"
    _write_pending_decisions(repo)
    blockers = collect_blockers(repo, mode="final")
    text = "\n".join(blockers)
    assert "decision D-TIME" in text
    assert "model_implementation=not_implemented" in text
    assert "missing formal result file: result1.xlsx" in text
    with pytest.raises(ReleaseBlockedError):
        assert_release_ready(repo, mode="final")


def test_cli_run_case_returns_pending_code(tmp_path, capsys):
    repo = tmp_path / "repo"
    _write_pending_decisions(repo)
    code = main(["--repo", str(repo), "run", "--case", "q1"])
    assert code == 4
    captured = capsys.readouterr()
    assert "pending" in (captured.out + captured.err)
