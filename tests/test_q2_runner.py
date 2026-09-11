from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from microgrid.cases import required_decisions
from microgrid.problem.contracts import (
    BatteryAction,
    BatteryState,
    CaseContext,
    CaseResult,
    CostBreakdown,
    IntervalResult,
)
from microgrid.problem.q2 import run
from microgrid.problem.q2_engine import Q2EngineeringResult
from microgrid.problem.q2_inputs import Q2InputBundle
from microgrid.problem.result_io import load_case_result
from microgrid.schemas import InputError, PendingDecisionError


def _write_decisions(repo: Path, *, blocked_id: str | None = None) -> None:
    config_dir = repo / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    for decision_id in required_decisions("q2"):
        status = "proposed" if decision_id == blocked_id else "approved"
        confirmed_by = "" if status == "proposed" else "tester"
        confirmed_at = "" if status == "proposed" else "2026-09-11T00:00:00+08:00"
        blocks.extend(
            [
                f"[decisions.{decision_id}]",
                f'status = "{status}"',
                'choice = "test fixture"',
                'rationale = "test fixture"',
                f'confirmed_by = "{confirmed_by}"',
                f'confirmed_at = "{confirmed_at}"',
                'source = "test fixture"',
                "",
            ]
        )
    (config_dir / "decisions.toml").write_text("\n".join(blocks), encoding="utf-8")


def _fake_engineering_result() -> Q2EngineeringResult:
    day = dt.date(2025, 2, 1)
    state = BatteryState(6000.0)
    intervals = tuple(
        IntervalResult(
            day=day,
            slot=slot,
            load_kw=0.0,
            pv_kw=0.0,
            planned_purchase_kwh=0.0,
            adjusted_purchase_kwh=0.0,
            emergency_purchase_kwh=0.0,
            action=BatteryAction(),
            state_start=state,
            state_end=state,
            source_ref="synthetic",
        )
        for slot in range(144)
    )
    return Q2EngineeringResult(
        intervals=intervals,
        costs=CostBreakdown(),
        forecast_records=(),
        solver_records=(),
        metadata={"validation": {"ok": True}},
        is_synthetic=True,
    )


def test_q2_runner_writes_internal_synthetic_artifact_chain(tmp_path: Path, monkeypatch) -> None:
    import microgrid.problem.q2 as q2

    repo = tmp_path / "repo"
    _write_decisions(repo)
    attachment1 = tmp_path / "attachment1.xlsx"
    attachment2 = tmp_path / "attachment2.xlsx"
    attachment1.write_bytes(b"synthetic attachment 1")
    attachment2.write_bytes(b"synthetic attachment 2")
    monkeypatch.setattr(q2, "load_q2_inputs", lambda **kwargs: Q2InputBundle((), (), ()))
    monkeypatch.setattr(q2, "run_q2_engineering", lambda bundle, config: _fake_engineering_result())

    output_dir = tmp_path / "run"
    result = run(
        CaseContext(
            repo_root=repo,
            case_id="q2",
            run_id="synthetic-q2",
            output_dir=output_dir,
            is_synthetic=True,
            metadata={
                "attachment1_path": str(attachment1),
                "attachment2_path": str(attachment2),
                "load_sheet_name": "负载",
                "pv_sheet_name": "光伏",
                "action_start_day": "2025-02-01",
                "action_end_day": "2025-02-01",
            },
        )
    )

    assert isinstance(result, CaseResult)
    assert result.is_synthetic is True
    for name in (
        "manifest.json",
        "input_snapshot.json",
        "domain_result.json",
        "validation.json",
        "summary.json",
        "solver.log",
    ):
        assert (output_dir / name).is_file()
    assert not list(output_dir.glob("*.xlsx"))
    assert load_case_result(output_dir / "domain_result.json").intervals == result.intervals


def test_q2_runner_stops_at_proposed_decision_before_allocating_output(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_decisions(repo, blocked_id="D_MPC")
    with pytest.raises(PendingDecisionError):
        run(CaseContext(repo_root=repo, case_id="q2", run_id="blocked"))
    assert not (repo / "outputs").exists()


def test_q2_runner_records_synthetic_missing_input_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_decisions(repo)
    output_dir = tmp_path / "failed-run"
    with pytest.raises(InputError, match="attachment1"):
        run(
            CaseContext(
                repo_root=repo,
                case_id="q2",
                run_id="missing-input",
                output_dir=output_dir,
                is_synthetic=True,
                metadata={
                    "attachment1_path": str(tmp_path / "missing-1.xlsx"),
                    "attachment2_path": str(tmp_path / "missing-2.xlsx"),
                },
            )
        )
    assert (output_dir / "manifest.json").is_file()
    assert (output_dir / "failure.json").is_file()
