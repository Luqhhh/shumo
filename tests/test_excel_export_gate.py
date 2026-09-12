from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from openpyxl import load_workbook

from microgrid.excel_export import TEMPLATE_EXPORT_DECISION_ID, export_case_result
from microgrid.problem.contracts import (
    BatteryAction,
    BatteryState,
    CaseResult,
    IntervalResult,
    apply_battery_action,
)
from microgrid.schemas import InputError, PendingDecisionError


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


def _q1_case_result() -> CaseResult:
    day = dt.date(2025, 1, 1)
    state = BatteryState(6000.0)
    intervals: list[IntervalResult] = []
    for slot in range(144):
        start = state
        action = BatteryAction()
        end = apply_battery_action(start, action)
        intervals.append(
            IntervalResult(
                day=day,
                slot=slot,
                load_kw=600.0,
                pv_kw=0.0,
                planned_purchase_kwh=100.0,
                adjusted_purchase_kwh=100.0,
                emergency_purchase_kwh=0.0,
                action=action,
                state_start=start,
                state_end=end,
            )
        )
        state = end
    return CaseResult(
        case_id="q1",
        run_id="run-1",
        status="success",
        intervals=tuple(intervals),
    )


def test_export_rejects_incomplete_q1_result(synthetic_template_repo):
    repo = synthetic_template_repo
    _write_export_decision(repo, status="approved")
    complete = _q1_case_result()
    incomplete = CaseResult(
        case_id="q1",
        run_id="run-1",
        status="success",
        intervals=complete.intervals[:-1],
    )
    with pytest.raises(InputError):
        export_case_result(repo, incomplete, output_path=repo / "bad_result1.xlsx")


def test_approved_template_writer_exports_q1_and_reads_back(synthetic_template_repo):
    repo = synthetic_template_repo
    _write_export_decision(repo, status="approved")
    result = _q1_case_result()
    output = export_case_result(repo, result, output_path=repo / "out_result1.xlsx")
    assert output.is_file()
    wb = load_workbook(output, data_only=False)
    try:
        plan = wb["计划购电量"]
        charge = wb["充放电量"]
        assert plan["A2"].value == "0:10-0:20"
        assert plan["A145"].value == "0:00+1-0:10+1"
        assert plan["B2"].value == pytest.approx(100.0)
        assert charge["E2"].value == pytest.approx(6000.0)
        assert charge["E3"].value == pytest.approx(6000.0)
    finally:
        wb.close()


@pytest.mark.parametrize("case_id", ["q4_2", "q4_3"])
def test_q1_mapping_approval_does_not_authorize_q4_export(tmp_path, case_id):
    _write_export_decision(tmp_path, status="approved")
    result = CaseResult(case_id=case_id, run_id="run-1", status="success")
    with pytest.raises(PendingDecisionError) as excinfo:
        export_case_result(tmp_path, result)
    assert excinfo.value.decision_ids == ["D_TIME_TEMPLATE_EXPORT_Q4"]


@pytest.mark.parametrize("case_id", ["q4_2", "q4_3"])
@pytest.mark.parametrize(
    "broken",
    ["missing", "pending", "proposed", "confirmed_by", "confirmed_at", "scope_cases", "complete"],
)
def test_q4_export_requires_complete_scoped_mapping_and_does_not_fake_writer(
    tmp_path, case_id, broken
):
    status = broken if broken in ("pending", "proposed") else "approved"
    by = " " if broken == "confirmed_by" else "tester"
    at = " " if broken == "confirmed_at" else "2026-09-12"
    scope = '["q3"]' if broken == "scope_cases" else '["q4_2", "q4_3"]'
    (tmp_path / "configs").mkdir()
    lines = (
        []
        if broken == "missing"
        else [
            "[decisions.D_TIME_TEMPLATE_EXPORT_Q4]",
            f'status = "{status}"',
            f'confirmed_by = "{by}"',
            f'confirmed_at = "{at}"',
            f"scope_cases = {scope}",
        ]
    )
    (tmp_path / "configs" / "decisions.toml").write_text("\n".join(lines), encoding="utf-8")
    result = CaseResult(case_id=case_id, run_id="run-1", status="success")
    expected = InputError if broken == "complete" else PendingDecisionError
    with pytest.raises(expected):
        export_case_result(tmp_path, result)
    assert not (tmp_path / "outputs").exists()
