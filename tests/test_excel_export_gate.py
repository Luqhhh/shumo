from __future__ import annotations

from pathlib import Path

import pytest

from microgrid.excel_export import TEMPLATE_EXPORT_DECISION_ID, export_case_result
from microgrid.problem.contracts import CaseResult
from microgrid.schemas import PendingDecisionError


def _write_export_decision(
    repo: Path, *, status: str, confirmed_by: str = "tester", confirmed_at: str = "2026-09-10"
) -> None:
    (repo / "configs").mkdir(parents=True, exist_ok=True)
    (repo / "configs" / "decisions.toml").write_text(
        "\n".join(
            [
                "[decisions.D_TIME_TEMPLATE_EXPORT]",
                f'status = "{status}"',
                'choice = ""',
                'rationale = ""',
                f'confirmed_by = "{confirmed_by}"',
                f'confirmed_at = "{confirmed_at}"',
                'source = "test"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_template_export_is_gated_separately_from_internal_time(tmp_path):
    repo = tmp_path / "repo"
    _write_export_decision(repo, status="pending")
    result = CaseResult(case_id="q1", run_id="run-1", status="success")
    with pytest.raises(PendingDecisionError) as excinfo:
        export_case_result(repo, result)
    assert TEMPLATE_EXPORT_DECISION_ID in excinfo.value.decision_ids


def test_approved_status_without_confirmation_still_blocks_export(tmp_path):
    repo = tmp_path / "repo"
    _write_export_decision(repo, status="approved", confirmed_by="", confirmed_at="")
    result = CaseResult(case_id="q1", run_id="run-1", status="success")
    with pytest.raises(PendingDecisionError):
        export_case_result(repo, result)


def test_approved_template_gate_does_not_fake_numeric_export(tmp_path):
    repo = tmp_path / "repo"
    _write_export_decision(repo, status="approved")
    result = CaseResult(case_id="q1", run_id="run-1", status="success")
    with pytest.raises(NotImplementedError):
        export_case_result(repo, result)
