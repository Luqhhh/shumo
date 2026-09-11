"""Q1 runner: approved MILP model over the shared energy contracts.

The model card is ``docs/q1_model.md``.  This module solves Q1, performs an
independent consistency check, and writes internal run artifacts.  It never
writes the official result1.xlsx; that remains behind D_TIME_TEMPLATE_EXPORT.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from ..approvals import require_approved_decisions
from ..artifacts import (
    build_manifest,
    ensure_run_id_available,
    new_run_id,
    verify_imported_inputs,
    write_json,
    write_manifest,
)
from ..dataio import ensure_dir
from ..schemas import InputError
from .contracts import (
    ENERGY_ABS_TOL_KWH,
    MAX_BUS_ENERGY_KWH,
    BatteryAction,
    BatteryState,
    CaseContext,
    CaseResult,
    IntervalResult,
    apply_battery_action,
)
from .q1_inputs import Q1InputSnapshot, load_q1_inputs
from .result_io import save_case_result
from .validation import validate_complete_run

CASE_ID = "q1"
MODEL_DECISION_ID = "D_MODEL_Q1"
DEFAULT_REFERENCE_DAY = _dt.date(2025, 1, 1)
COST_ABS_TOL_CNY = 1e-6


class Q1SolveError(InputError):
    """The approved Q1 MILP did not return an acceptable solution."""


@dataclass(frozen=True)
class Q1Solution:
    reference_day: _dt.date
    purchase_kwh: tuple[float, ...]
    charge_kwh: tuple[float, ...]
    discharge_kwh: tuple[float, ...]
    pv_used_kwh: tuple[float, ...]
    energy_kwh: tuple[float, ...]
    objective_cny: float
    solver_status: int
    solver_message: str
    solver_metadata: dict[str, Any]


def _reference_day(context: CaseContext) -> _dt.date:
    raw = context.metadata.get("reference_day")
    if raw is None:
        return DEFAULT_REFERENCE_DAY
    if isinstance(raw, _dt.date):
        return raw
    return _dt.date.fromisoformat(str(raw))


def _problem_size(n: int) -> dict[str, slice]:
    g = slice(0, n)
    c = slice(n, 2 * n)
    d = slice(2 * n, 3 * n)
    u = slice(3 * n, 4 * n)
    e = slice(4 * n, 5 * n + 1)
    z = slice(5 * n + 1, 6 * n + 1)
    return {"g": g, "c": c, "d": d, "u": u, "e": e, "z": z}


def _build_constraints(
    snapshot: Q1InputSnapshot,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, slice]]:
    n = len(snapshot.intervals)
    index = _problem_size(n)
    variable_count = 6 * n + 1
    row_count = 4 * n
    matrix = np.zeros((row_count, variable_count), dtype=float)
    lower = np.zeros(row_count, dtype=float)
    upper = np.zeros(row_count, dtype=float)

    for k, point in enumerate(snapshot.intervals):
        row = k
        matrix[row, index["g"].start + k] = 1.0
        matrix[row, index["d"].start + k] = 1.0
        matrix[row, index["u"].start + k] = 1.0
        matrix[row, index["c"].start + k] = -1.0
        lower[row] = upper[row] = point.load_kwh

        row = n + k
        matrix[row, index["e"].start + k + 1] = 1.0
        matrix[row, index["e"].start + k] = -1.0
        matrix[row, index["c"].start + k] = -0.9
        matrix[row, index["d"].start + k] = 1.0 / 0.9
        lower[row] = upper[row] = 0.0

        row = 2 * n + k
        matrix[row, index["c"].start + k] = 1.0
        matrix[row, index["z"].start + k] = -MAX_BUS_ENERGY_KWH
        lower[row] = -np.inf
        upper[row] = 0.0

        row = 3 * n + k
        matrix[row, index["d"].start + k] = 1.0
        matrix[row, index["z"].start + k] = MAX_BUS_ENERGY_KWH
        lower[row] = -np.inf
        upper[row] = MAX_BUS_ENERGY_KWH

    return matrix, lower, upper, index


def solve_q1_milp(
    snapshot: Q1InputSnapshot,
    *,
    time_limit_s: float | None = None,
) -> Q1Solution:
    """Solve the approved Q1 MILP with SciPy/HiGHS.

    The MILP uses binary variables only to enforce charge/discharge mutual
    exclusion.  It does not add or remove any objective term beyond the model
    card.
    """

    n = len(snapshot.intervals)
    index = _problem_size(n)
    variable_count = 6 * n + 1
    objective = np.zeros(variable_count, dtype=float)
    lower = np.full(variable_count, 0.0, dtype=float)
    upper = np.full(variable_count, np.inf, dtype=float)
    integrality = np.zeros(variable_count, dtype=int)

    for k, point in enumerate(snapshot.intervals):
        objective[index["g"].start + k] = point.price_cny_per_kwh
        lower[index["c"].start + k] = 0.0
        upper[index["c"].start + k] = MAX_BUS_ENERGY_KWH
        lower[index["d"].start + k] = 0.0
        upper[index["d"].start + k] = MAX_BUS_ENERGY_KWH
        lower[index["u"].start + k] = 0.0
        upper[index["u"].start + k] = point.pv_forecast_kwh
        lower[index["e"].start + k] = 1200.0
        upper[index["e"].start + k] = 10800.0
        integrality[index["z"].start + k] = 1

    # E_0 and E_144 are fixed by the approved Q1 model card.
    lower[index["e"].start] = upper[index["e"].start] = 6000.0
    lower[index["e"].start + n] = upper[index["e"].start + n] = 6000.0
    lower[index["z"]] = 0.0
    upper[index["z"]] = 1.0

    matrix, constraint_lower, constraint_upper, _ = _build_constraints(snapshot)
    constraints = LinearConstraint(matrix, constraint_lower, constraint_upper)
    bounds = Bounds(lower, upper)
    options: dict[str, Any] = {}
    if time_limit_s is not None:
        options["time_limit"] = time_limit_s

    result = milp(
        c=objective,
        integrality=integrality,
        bounds=bounds,
        constraints=constraints,
        options=options or None,
    )
    if result.status != 0 or result.x is None:
        raise Q1SolveError(f"Q1 MILP failed: status={result.status} message={result.message!r}")

    solution_vector = np.asarray(result.x, dtype=float)

    def value(name: str, offset: int) -> float:
        return float(solution_vector[index[name].start + offset])

    purchase = tuple(value("g", k) for k in range(n))
    charge = tuple(value("c", k) for k in range(n))
    discharge = tuple(value("d", k) for k in range(n))
    pv_used = tuple(value("u", k) for k in range(n))
    energy = tuple(value("e", k) for k in range(n + 1))
    recomputed = sum(
        point.price_cny_per_kwh * purchase[k] for k, point in enumerate(snapshot.intervals)
    )
    solver_fun = float(result.fun) if result.fun is not None else recomputed
    metadata: dict[str, Any] = {
        "status": int(result.status),
        "success": bool(getattr(result, "success", False)),
        "message": str(result.message),
        "mip_gap": getattr(result, "mip_gap", None),
        "mip_node_count": getattr(result, "mip_node_count", None),
        "mip_dual_bound": getattr(result, "mip_dual_bound", None),
        "objective": solver_fun,
    }
    return Q1Solution(
        reference_day=snapshot.reference_day,
        purchase_kwh=purchase,
        charge_kwh=charge,
        discharge_kwh=discharge,
        pv_used_kwh=pv_used,
        energy_kwh=energy,
        objective_cny=solver_fun,
        solver_status=int(result.status),
        solver_message=str(result.message),
        solver_metadata=metadata,
    )


def validate_q1_solution(snapshot: Q1InputSnapshot, solution: Q1Solution) -> dict[str, Any]:
    """Independent residual checks; no solver return value is trusted blindly."""

    max_balance = 0.0
    max_dynamics = 0.0
    max_power = 0.0
    max_state = 0.0
    max_curtailment = 0.0
    for k, point in enumerate(snapshot.intervals):
        balance = (
            solution.purchase_kwh[k]
            + solution.discharge_kwh[k]
            + solution.pv_used_kwh[k]
            - point.load_kwh
            - solution.charge_kwh[k]
        )
        max_balance = max(max_balance, abs(balance))
        dynamics = solution.energy_kwh[k + 1] - (
            solution.energy_kwh[k] + 0.9 * solution.charge_kwh[k] - solution.discharge_kwh[k] / 0.9
        )
        max_dynamics = max(max_dynamics, abs(dynamics))
        max_power = max(
            max_power,
            abs(solution.charge_kwh[k]),
            abs(solution.discharge_kwh[k]),
        )
        max_state = max(
            max_state,
            abs(solution.energy_kwh[k]),
            abs(solution.energy_kwh[k + 1]),
        )
        max_curtailment = max(
            max_curtailment,
            max(0.0, solution.pv_used_kwh[k] - point.pv_forecast_kwh),
        )

    independent_cost = sum(
        point.price_cny_per_kwh * solution.purchase_kwh[k]
        for k, point in enumerate(snapshot.intervals)
    )
    cost_gap = abs(independent_cost - solution.objective_cny)
    issues: list[str] = []
    if max_balance > ENERGY_ABS_TOL_KWH:
        issues.append(f"supply balance residual {max_balance:.6g} kWh")
    if max_dynamics > ENERGY_ABS_TOL_KWH:
        issues.append(f"battery dynamics residual {max_dynamics:.6g} kWh")
    if max_power > MAX_BUS_ENERGY_KWH + ENERGY_ABS_TOL_KWH:
        issues.append(f"bus-side power limit violated: {max_power:.6g} kWh")
    if abs(solution.energy_kwh[0] - 6000.0) > ENERGY_ABS_TOL_KWH:
        issues.append("initial energy is not 6000 kWh")
    if abs(solution.energy_kwh[-1] - 6000.0) > ENERGY_ABS_TOL_KWH:
        issues.append("terminal energy is not 6000 kWh")
    if any(
        energy < 1200.0 - ENERGY_ABS_TOL_KWH or energy > 10800.0 + ENERGY_ABS_TOL_KWH
        for energy in solution.energy_kwh
    ):
        issues.append("state of charge out of [1200, 10800] kWh")
    if max_curtailment > ENERGY_ABS_TOL_KWH:
        issues.append(f"PV used exceeds forecast by {max_curtailment:.6g} kWh")
    if cost_gap > COST_ABS_TOL_CNY:
        issues.append(f"objective mismatch {cost_gap:.6g} CNY")

    return {
        "ok": not issues,
        "issues": issues,
        "max_balance_residual_kwh": max_balance,
        "max_dynamics_residual_kwh": max_dynamics,
        "max_bus_energy_kwh": max_power,
        "max_abs_state_kwh": max_state,
        "cost_gap_cny": cost_gap,
        "independent_cost_cny": independent_cost,
        "solver_objective_cny": solution.objective_cny,
    }


def _build_interval_results(
    snapshot: Q1InputSnapshot, solution: Q1Solution
) -> tuple[IntervalResult, ...]:
    intervals: list[IntervalResult] = []
    for k, point in enumerate(snapshot.intervals):
        action = BatteryAction(
            charge_kwh=solution.charge_kwh[k],
            discharge_kwh=solution.discharge_kwh[k],
        )
        state_start = BatteryState(solution.energy_kwh[k])
        state_end = apply_battery_action(state_start, action)
        intervals.append(
            IntervalResult(
                day=snapshot.reference_day,
                slot=k,
                load_kw=point.load_kw,
                pv_kw=point.pv_forecast_kw,
                planned_purchase_kwh=solution.purchase_kwh[k],
                adjusted_purchase_kwh=solution.purchase_kwh[k],
                emergency_purchase_kwh=0.0,
                action=action,
                state_start=state_start,
                state_end=state_end,
                source_ref=point.source_refs[0] if point.source_refs else snapshot.source_file,
            )
        )
    return tuple(intervals)


def _snapshot_to_dict(snapshot: Q1InputSnapshot) -> dict[str, Any]:
    return {
        "reference_day": snapshot.reference_day.isoformat(),
        "source_file": snapshot.source_file,
        "source_sha256": snapshot.source_sha256,
        "generated_at": snapshot.generated_at,
        "intervals": [
            {
                "slot": point.slot,
                "start": point.start.isoformat(sep=" "),
                "end": point.end.isoformat(sep=" "),
                "price_cny_per_kwh": point.price_cny_per_kwh,
                "load_kw": point.load_kw,
                "pv_forecast_kw": point.pv_forecast_kw,
                "load_kwh": point.load_kwh,
                "pv_forecast_kwh": point.pv_forecast_kwh,
                "source_refs": list(point.source_refs),
            }
            for point in snapshot.intervals
        ],
    }


def _summary(
    snapshot: Q1InputSnapshot, solution: Q1Solution, validation: dict[str, Any]
) -> dict[str, Any]:
    return {
        "case_id": CASE_ID,
        "reference_day": snapshot.reference_day.isoformat(),
        "source_sha256": snapshot.source_sha256,
        "interval_count": len(snapshot.intervals),
        "total_load_kwh": sum(point.load_kwh for point in snapshot.intervals),
        "total_pv_forecast_kwh": sum(point.pv_forecast_kwh for point in snapshot.intervals),
        "total_pv_used_kwh": sum(solution.pv_used_kwh),
        "total_pv_curtailed_kwh": sum(
            point.pv_forecast_kwh - solution.pv_used_kwh[k]
            for k, point in enumerate(snapshot.intervals)
        ),
        "total_charge_kwh": sum(solution.charge_kwh),
        "total_discharge_kwh": sum(solution.discharge_kwh),
        "total_planned_purchase_kwh": sum(solution.purchase_kwh),
        "total_planned_cost_cny": solution.objective_cny,
        "initial_energy_kwh": solution.energy_kwh[0],
        "terminal_energy_kwh": solution.energy_kwh[-1],
        "validation_ok": validation["ok"],
        "solver_status": solution.solver_status,
        "solver_message": solution.solver_message,
        "solver_metadata": solution.solver_metadata,
    }


def run(context: CaseContext) -> CaseResult:
    """Execute the approved Q1 model and write internal run artifacts."""

    require_approved_decisions(context.repo_root, (MODEL_DECISION_ID,))
    reference_day = _reference_day(context)
    source_path = context.repo_root / "data" / "raw" / "附件1.xlsx"
    if not source_path.is_file():
        raise InputError(f"Q1 input not found: {source_path}; run ingest first")

    input_verification_issues = verify_imported_inputs(context.repo_root)
    if input_verification_issues:
        raise InputError("input provenance check failed: " + "; ".join(input_verification_issues))

    snapshot = load_q1_inputs(source_path, reference_day=reference_day)
    solution = solve_q1_milp(snapshot)
    validation = validate_q1_solution(snapshot, solution)
    intervals = _build_interval_results(snapshot, solution)

    run_id = context.run_id or new_run_id("q1", context.repo_root)
    run_dir = ensure_run_id_available(context.repo_root, CASE_ID, run_id)
    ensure_dir(run_dir)

    result = CaseResult(
        case_id=CASE_ID,
        run_id=run_id,
        status="success",
        is_synthetic=False,
        intervals=intervals,
        metadata={
            "model": "q1_milp",
            "model_decision": MODEL_DECISION_ID,
            "reference_day": reference_day.isoformat(),
            "solver_status": solution.solver_status,
            "objective_cny": solution.objective_cny,
            "validation_ok": validation["ok"],
        },
    )
    run_validation = validate_complete_run(
        result.intervals,
        (reference_day,),
        require_daily_equal_ends=True,
    )
    if not run_validation.ok or not validation["ok"]:
        raise Q1SolveError(
            "Q1 result validation failed: "
            + "; ".join(list(validation["issues"]) + list(run_validation.issues))
        )

    write_json(run_dir / "input_snapshot.json", _snapshot_to_dict(snapshot))
    save_case_result(run_dir / "domain_result.json", result)
    write_json(run_dir / "validation.json", validation)
    write_json(run_dir / "summary.json", _summary(snapshot, solution, validation))
    (run_dir / "solver.log").write_text(
        "\n".join(
            [
                "solver=scipy.optimize.milp (HiGHS)",
                f"status={solution.solver_status}",
                f"message={solution.solver_message}",
                f"objective_cny={solution.objective_cny:.9f}",
                f"metadata={json.dumps(solution.solver_metadata, ensure_ascii=False, sort_keys=True)}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = build_manifest(
        context.repo_root,
        run_id=run_id,
        case_id=CASE_ID,
        command=context.metadata.get(
            "command", ["python", "-m", "microgrid", "run", "--case", "q1"]
        ),
        status="success",
        is_synthetic=False,
        result_files={},
        result_sha256={},
        verify_inputs=True,
    )
    write_manifest(run_dir, manifest)
    return result
