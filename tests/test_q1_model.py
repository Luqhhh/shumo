from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import pytest
from openpyxl import Workbook

from microgrid.problem import q1 as q1_module
from microgrid.problem.contracts import CaseContext
from microgrid.problem.q1 import Q1Solution, run, validate_q1_solution
from microgrid.problem.q1_inputs import load_q1_inputs
from microgrid.schemas import PendingDecisionError


def _right_endpoint_label(slot: int) -> str:
    end_minutes = (slot + 1) * 10
    if end_minutes == 24 * 60:
        return "0:00+1"
    return f"{end_minutes // 60}:{end_minutes % 60:02d}"


def _write_attachment1(path: Path, *, load_kw: float = 100.0, pv_kw: float = 0.0) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["时间", "电价", "小区负载", "光伏发电预测功率"])
    for slot in range(144):
        label = _right_endpoint_label(slot)
        time_value = ((slot + 1) * 10) / (24 * 60) if slot < 20 and label != "0:00+1" else label
        ws.append([time_value, 0.5, load_kw, pv_kw])
    wb.save(path)


def _feasible_mock_solution(snapshot) -> Q1Solution:
    purchase: list[float] = []
    pv_used: list[float] = []
    for point in snapshot.intervals:
        used = min(point.pv_forecast_kwh, point.load_kwh)
        pv_used.append(used)
        purchase.append(point.load_kwh - used)
    objective = sum(
        point.price_cny_per_kwh * purchase[k] for k, point in enumerate(snapshot.intervals)
    )
    n = len(snapshot.intervals)
    return Q1Solution(
        reference_day=snapshot.reference_day,
        purchase_kwh=tuple(purchase),
        charge_kwh=(0.0,) * n,
        discharge_kwh=(0.0,) * n,
        pv_used_kwh=tuple(pv_used),
        energy_kwh=(6000.0,) * (n + 1),
        objective_cny=objective,
        solver_status=0,
        solver_message="mock optimal",
        solver_metadata={"mock": True},
    )


def _synthetic_snapshot(tmp_path: Path, *, load_kw: float = 100.0, pv_kw: float = 0.0):
    path = tmp_path / "附件1.xlsx"
    _write_attachment1(path, load_kw=load_kw, pv_kw=pv_kw)
    return path, load_q1_inputs(path, reference_day=dt.date(2025, 1, 1))


def test_validate_q1_solution_accepts_handmade_feasible_solution(tmp_path):
    _, snapshot = _synthetic_snapshot(tmp_path)
    solution = _feasible_mock_solution(snapshot)
    report = validate_q1_solution(snapshot, solution)
    assert report["ok"] is True
    assert report["cost_gap_cny"] == pytest.approx(0.0)

    broken = Q1Solution(
        reference_day=solution.reference_day,
        purchase_kwh=(solution.purchase_kwh[0] + 1.0,) + solution.purchase_kwh[1:],
        charge_kwh=solution.charge_kwh,
        discharge_kwh=solution.discharge_kwh,
        pv_used_kwh=solution.pv_used_kwh,
        energy_kwh=solution.energy_kwh,
        objective_cny=solution.objective_cny,
        solver_status=solution.solver_status,
        solver_message=solution.solver_message,
        solver_metadata=solution.solver_metadata,
    )
    report = validate_q1_solution(snapshot, broken)
    assert report["ok"] is False
    assert any("supply balance" in issue for issue in report["issues"])


def test_mock_solver_run_writes_internal_artifacts(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    raw_dir = repo / "data" / "raw"
    raw_dir.mkdir(parents=True)
    source = raw_dir / "附件1.xlsx"
    _write_attachment1(source)
    (repo / "configs").mkdir()
    (repo / "configs" / "decisions.toml").write_text(
        "\n".join(
            [
                "[decisions.D_MODEL_Q1]",
                'status = "approved"',
                'choice = "mock"',
                'rationale = "mock"',
                'confirmed_by = "tester"',
                'confirmed_at = "2026-09-10"',
                'source = "test"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo / "records").mkdir()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    (repo / "records" / "inputs_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "items": [
                    {
                        "repository_path": "data/raw/附件1.xlsx",
                        "sha256": digest,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def mock_solver(snapshot, **kwargs):
        return _feasible_mock_solution(snapshot)

    monkeypatch.setattr(q1_module, "solve_q1_milp", mock_solver)
    result = run(CaseContext(repo_root=repo, case_id="q1", run_id="run-mock"))
    assert result.status == "success"
    assert len(result.intervals) == 144
    run_dir = repo / "outputs" / "runs" / "q1" / "run-mock"
    for name in (
        "manifest.json",
        "input_snapshot.json",
        "domain_result.json",
        "validation.json",
        "summary.json",
        "solver.log",
    ):
        assert (run_dir / name).is_file()
    assert not (run_dir / "results" / "result1.xlsx").exists()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["is_synthetic"] is False
    assert manifest["result_files"] == {}


def test_q1_run_requires_model_decision(tmp_path):
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    (repo / "configs" / "decisions.toml").write_text(
        '[decisions.D_MODEL_Q1]\nstatus = "pending"\n', encoding="utf-8"
    )
    with pytest.raises(PendingDecisionError):
        run(CaseContext(repo_root=repo, case_id="q1", run_id="run-gated"))


@pytest.mark.skipif(
    os.environ.get("MICROGRID_RUN_SOLVER_TESTS") != "1",
    reason="set MICROGRID_RUN_SOLVER_TESTS=1 to run the real SciPy/HiGHS Q1 MILP",
)
def test_real_solver_on_synthetic_q1_snapshot(tmp_path):
    _, snapshot = _synthetic_snapshot(tmp_path, load_kw=600.0, pv_kw=0.0)
    solution = q1_module.solve_q1_milp(snapshot)
    report = validate_q1_solution(snapshot, solution)
    assert solution.solver_status == 0
    assert report["ok"] is True
