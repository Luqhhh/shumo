"""Approved sparse Q4 MILP with independently checked tail certificates."""

from __future__ import annotations

import hashlib
import json
import time
import warnings
from dataclasses import dataclass, replace
from functools import lru_cache

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from .contracts import ENERGY_ABS_TOL_KWH as TOL
from .contracts import MAX_BUS_ENERGY_KWH as M
from .contracts import BatteryAction, BatteryState
from .purchase_ledger import PurchaseLedger
from .q4_common import (
    RESERVE_START,
    STEP,
    YEAR_END,
    Q4Error,
    energy_lower_bound,
    reserve_start_from_config,
)
from .q4_forecasts import ForecastSnapshot

FIELDS = (
    "grid",
    "charge",
    "discharge",
    "emergency",
    "pv_used",
    "pv_spill",
    "grid_spill",
    "z",
    "y",
    "plus",
    "minus",
    "w",
)
G, C, D, U, P, S, V, Z, Y, A, B, W = range(len(FIELDS))
WIDTH = len(FIELDS)


def forecast_reserve_start(forecast: ForecastSnapshot):
    return reserve_start_from_config(
        {
            "model_version": forecast.model_version,
            "terminal_reserve": None
            if forecast.model_version == "q4-v2"
            else {
                "start": str(RESERVE_START),
                "minimum_energy_kwh": 6000.0,
            },
        }
    )


@lru_cache(maxsize=144)
def variable_indices(count: int):
    """Cache the variable index structure shared across forecast versions."""
    return tuple(tuple(WIDTH * k + field for field in range(WIDTH)) for k in range(count))


@dataclass(frozen=True)
class DispatchPlan:
    solve_id: str
    forecast_id: str
    flows: tuple[tuple[float, ...], ...]
    energy: tuple[float, ...]
    objective: float
    absolute_gap: float
    solver_record: dict
    shift_count: int = 0

    def intention(self) -> BatteryAction:
        return BatteryAction(self.flows[0][C], self.flows[0][D])


def contract_hash(ledger: PurchaseLedger, snapshot: ForecastSnapshot) -> str:
    values = [
        (str(slot), ledger.committed(slot))
        for slot in snapshot.slots
        if slot.date() in ledger.versions
    ]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def objective_value(flows, energy, forecast, permissions):
    value = sum(
        p * flow[G]
        for p, flow, permission in zip(forecast.prices, flows, permissions, strict=True)
        if permission in ("new_day", "lookahead")
    )
    value += forecast.prices[0] * sum(
        1.5 * flow[A] + 0.5 * flow[B]
        for flow, permission in zip(flows, permissions, strict=True)
        if permission == "adjustable"
    )
    value += sum(5 * p * flow[U] for p, flow in zip(forecast.prices, flows, strict=True))
    return value - forecast.terminal_value * energy[-1]


def validate_dispatch(
    plan: DispatchPlan,
    state: BatteryState,
    forecast: ForecastSnapshot,
    ledger: PurchaseLedger,
    *,
    reserve_start=RESERVE_START,
) -> tuple[str, ...]:
    """Recompute full equations/bounds without using the solver matrix."""
    issues = []
    try:
        if reserve_start != forecast_reserve_start(forecast):
            issues.append("model_reserve_binding")
    except Q4Error:
        issues.append("model_reserve_binding")
    n = len(forecast.slots)
    if (
        len(plan.flows) != n
        or len(plan.energy) != n + 1
        or plan.forecast_id != forecast.snapshot_id
    ):
        return ("plan_shape_or_forecast",)
    if abs(plan.energy[0] - state.energy_kwh) > TOL:
        issues.append("initial_state")
    if plan.energy[0] < energy_lower_bound(forecast.slots[0], reserve_start) - TOL:
        issues.append("initial_energy_lower_bound")
    permissions = ledger.permissions(forecast.slots[0], forecast.slots)
    for k, (flow, load, pv, permission, slot) in enumerate(
        zip(
            plan.flows, forecast.load_kwh, forecast.pv_kwh, permissions, forecast.slots, strict=True
        )
    ):
        if len(flow) != WIDTH or not all(np.isfinite(flow)) or not np.isfinite(plan.energy[k + 1]):
            issues.append(f"{k}:nonfinite_or_shape")
            continue
        grid, c, d, u, p, sp, sg, z, y, plus, minus, w = flow
        previous = ledger.committed(slot) if permission in ("fixed", "adjustable") else 0.0
        bound = previous if permission == "fixed" else max(previous, load + M)
        residuals = (
            abs(grid + p + d + u - load - c - sg),
            abs(p + sp - pv),
            abs(plan.energy[k + 1] - plan.energy[k] - 0.9 * c + d / 0.9),
            -min(flow),
            grid - bound,
            sg - grid,
            d - load,
            c - M * z,
            d - M * (1 - z),
            u - load * y,
            c - M * (1 - y),
            sg - bound * (1 - y),
            sp - pv * (1 - y),
            energy_lower_bound(slot + STEP, reserve_start) - plan.energy[k + 1],
            plan.energy[k + 1] - 10800,
        )
        if max(residuals) > TOL:
            issues.append(f"{k}:physical_constraint")
        if c > TOL and d > TOL:
            issues.append(f"{k}:charge_discharge_complementarity")
        if u > TOL and max(c, sp, sg) > TOL:
            issues.append(f"{k}:emergency_complementarity")
        if any(min(abs(mode), abs(mode - 1)) > TOL for mode in (z, y, w)):
            issues.append(f"{k}:nonbinary_mode")
        if permission == "fixed" and abs(grid - previous) > TOL:
            issues.append(f"{k}:fixed_contract")
        if permission == "adjustable":
            if plus > TOL and minus > TOL:
                issues.append(f"{k}:trade_direction_complementarity")
            if (
                max(
                    abs(grid - previous - plus + minus),
                    plus - bound * w,
                    minus - previous * (1 - w),
                )
                > TOL
            ):
                issues.append(f"{k}:adjustment_constraint")
        elif max(plus, minus, w) > TOL:
            issues.append(f"{k}:nontrade_delta")
        remaining = int((YEAR_END - (slot + STEP)) / STEP)
        if (
            plan.energy[k + 1] < 6000 - remaining * 0.9 * M - TOL
            or plan.energy[k + 1] > 6000 + remaining * M / 0.9 + TOL
        ):
            issues.append(f"{k}:annual_reachability")
    if forecast.slots[-1] + STEP == YEAR_END and abs(plan.energy[-1] - 6000) > TOL:
        issues.append("terminal_state")
    expected = objective_value(plan.flows, plan.energy, forecast, permissions)
    if abs(expected - plan.objective) > 0.00001:
        issues.append("objective_recalculation")
    return tuple(issues)


def solve_dispatch(
    state: BatteryState,
    forecast: ForecastSnapshot,
    ledger: PurchaseLedger,
    *,
    reserve_start=RESERVE_START,
    performance=None,
) -> DispatchPlan:
    matrix_started = time.perf_counter() if performance is not None else None
    n = len(forecast.slots)
    if reserve_start != forecast_reserve_start(forecast):
        raise Q4Error("config_mismatch", "dispatch reserve does not match forecast model version")
    if state.energy_kwh < energy_lower_bound(forecast.slots[0], reserve_start) - TOL:
        raise Q4Error(
            "terminal_reserve_infeasible", "current actual SOC below approved lower bound"
        )
    permissions = ledger.permissions(forecast.slots[0], forecast.slots)
    columns = variable_indices(n)
    size = WIDTH * n + n + 1
    lower, upper = np.zeros(size), np.full(size, np.inf)
    objective, integrality = np.zeros(size), np.zeros(size, dtype=int)
    row_index, col_index, coefficients, rlo, rhi = [], [], [], [], []

    def row(terms, lo=-np.inf, hi=np.inf):
        index = len(rlo)
        for column, coefficient in terms:
            row_index.append(index)
            col_index.append(column)
            coefficients.append(coefficient)
        rlo.append(lo)
        rhi.append(hi)

    ebase = WIDTH * n
    lower[ebase:] = 1200
    upper[ebase:] = 10800
    lower[ebase] = upper[ebase] = state.energy_kwh
    objective[-1] = -forecast.terminal_value
    for k, (load, pv, price, permission, slot) in enumerate(
        zip(
            forecast.load_kwh,
            forecast.pv_kwh,
            forecast.prices,
            permissions,
            forecast.slots,
            strict=True,
        )
    ):

        def idx(field, column=columns[k]):
            return column[field]

        previous = ledger.committed(slot) if permission in ("fixed", "adjustable") else 0.0
        gmax = previous if permission == "fixed" else max(previous, load + M)
        upper[idx(G)] = gmax
        if permission == "fixed":
            lower[idx(G)] = previous
        if permission in ("new_day", "lookahead"):
            objective[idx(G)] = price
        upper[idx(C)] = upper[idx(D)] = M
        upper[idx(D)] = min(M, load)
        upper[idx(U)] = load
        upper[idx(P)] = upper[idx(S)] = pv
        upper[idx(V)] = gmax
        for field in (Z, Y, W):
            upper[idx(field)] = 1
            integrality[idx(field)] = 1
        upper[idx(A)] = upper[idx(B)] = upper[idx(W)] = 0
        if permission == "adjustable":
            upper[idx(A)], upper[idx(B)], upper[idx(W)] = gmax, previous, 1
            objective[idx(A)], objective[idx(B)] = (
                1.5 * forecast.prices[0],
                0.5 * forecast.prices[0],
            )
            row(((idx(G), 1), (idx(A), -1), (idx(B), 1)), previous, previous)
            row(((idx(A), 1), (idx(W), -gmax)), hi=0)
            row(((idx(B), 1), (idx(W), previous)), hi=previous)
        objective[idx(U)] = 5 * price
        row(
            ((idx(G), 1), (idx(P), 1), (idx(D), 1), (idx(U), 1), (idx(C), -1), (idx(V), -1)),
            load,
            load,
        )
        row(((idx(P), 1), (idx(S), 1)), pv, pv)
        row(((ebase + k + 1, 1), (ebase + k, -1), (idx(C), -0.9), (idx(D), 1 / 0.9)), 0, 0)
        row(((idx(V), 1), (idx(G), -1)), hi=0)
        row(((idx(C), 1), (idx(Z), -M)), hi=0)
        row(((idx(D), 1), (idx(Z), M)), hi=M)
        row(((idx(U), 1), (idx(Y), -load)), hi=0)
        row(((idx(C), 1), (idx(Y), M)), hi=M)
        row(((idx(V), 1), (idx(Y), gmax)), hi=gmax)
        row(((idx(S), 1), (idx(Y), pv)), hi=pv)
        remaining = int((YEAR_END - (slot + STEP)) / STEP)
        lower[ebase + k + 1] = max(
            energy_lower_bound(slot + STEP, reserve_start), 6000 - remaining * 0.9 * M
        )
        upper[ebase + k + 1] = min(10800, 6000 + remaining * M / 0.9)
    if forecast.slots[-1] + STEP == YEAR_END:
        lower[-1] = upper[-1] = 6000
    matrix = coo_matrix((coefficients, (row_index, col_index)), shape=(len(rlo), size)).tocsc()
    constraints = LinearConstraint(matrix, np.array(rlo), np.array(rhi))
    if performance is not None:
        performance.record("matrix_build", time.perf_counter() - matrix_started)
    attempts = []
    result = None
    for limit in (10.0, 60.0):
        started = time.perf_counter()
        # Locked SciPy forwards these native HiGHS options verbatim. Tighten
        # integrality so its error amplified by M stays below the kWh tolerance.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message="Unrecognized options detected:.*", category=RuntimeWarning
            )
            result = milp(
                objective,
                integrality=integrality,
                bounds=Bounds(lower, upper),
                constraints=constraints,
                options={
                    "presolve": True,
                    "mip_rel_gap": 1e-4,
                    "time_limit": limit,
                    "mip_feasibility_tolerance": 1e-9,
                    "primal_feasibility_tolerance": 1e-8,
                },
            )
        attempts.append(
            {
                "time_limit": limit,
                "elapsed_seconds": time.perf_counter() - started,
                "status": int(result.status),
                "message": str(result.message),
            }
        )
        if performance is not None:
            performance.record("solver", attempts[-1]["elapsed_seconds"])
        if result.status != 1:
            break
    record = {
        "status": int(result.status),
        "message": str(result.message),
        "attempts": attempts,
        "elapsed_seconds": sum(item["elapsed_seconds"] for item in attempts),
        "objective": float(result.fun) if result.fun is not None else None,
        "gap": float(result.mip_gap) if getattr(result, "mip_gap", None) is not None else None,
        "dual_bound": float(result.mip_dual_bound)
        if getattr(result, "mip_dual_bound", None) is not None
        else None,
        "nodes": int(result.mip_node_count)
        if getattr(result, "mip_node_count", None) is not None
        and np.isfinite(result.mip_node_count)
        else None,
        "reused_tail": False,
        "shift_count": 0,
        "forecast_id": forecast.snapshot_id,
        "window_start": str(forecast.slots[0]),
        "window_end": str(forecast.slots[-1] + STEP),
        "contract_hash": contract_hash(ledger, forecast),
        "schema_version": 1,
    }
    # HiGHS may report an infinite relative gap when the optimum is zero.
    # Preserve this undefined metric as text; strict JSON must stay writable.
    nonfinite_metrics = {}
    for key in ("objective", "gap", "dual_bound"):
        value = record[key]
        if value is not None and not np.isfinite(value):
            nonfinite_metrics[key] = str(value)
            record[key] = None
    if nonfinite_metrics:
        record["nonfinite_solver_metrics"] = nonfinite_metrics
    if result.status != 0 or result.x is None:
        exc = Q4Error("solver_failed", f"HiGHS status {result.status}: {result.message}")
        exc.solver_record = record
        raise exc
    if record["objective"] is None or record["dual_bound"] is None:
        exc = Q4Error("solver_validation_failed", "successful solve lacks finite objective/bound")
        exc.solver_record = record
        raise exc
    validation_started = time.perf_counter() if performance is not None else None
    x = np.asarray(result.x)
    residual = max(
        float(np.max(np.maximum(np.array(rlo) - matrix @ x, 0))),
        float(np.max(np.maximum(matrix @ x - np.array(rhi), 0))),
        float(np.max(np.maximum(lower - x, 0))),
        float(np.max(np.maximum(x - upper, 0))),
    )
    record["constraint_residual_kwh"] = residual
    if residual > TOL:
        exc = Q4Error("solver_validation_failed", f"matrix residual {residual}")
        exc.solver_record = record
        raise exc
    flows = tuple(tuple(float(v) for v in x[k * WIDTH : (k + 1) * WIDTH]) for k in range(n))
    corrections = []
    clean_flows = []
    for k, flow in enumerate(flows):
        cleaned = []
        for field, value in enumerate(flow):
            if -1e-9 <= value < 0:
                corrections.append(
                    {"slot": k, "field": FIELDS[field], "raw": value, "canonical": 0.0}
                )
                value = 0.0
            cleaned.append(value)
        clean_flows.append(tuple(cleaned))
    flows = tuple(clean_flows)
    # Use execution's arithmetic so unchanged actions produce exactly the same
    # state; this removes the need to approximate SOC equality in certificates.
    energy_values = [state.energy_kwh]
    for flow in flows:
        energy_values.append(energy_values[-1] + 0.9 * flow[C] - flow[D] / 0.9)
    energy = tuple(energy_values)
    value = objective_value(flows, energy, forecast, permissions)
    numeric_cost_error = abs(value - float(result.fun))
    if numeric_cost_error > 0.00001:
        raise Q4Error(
            "solver_validation_failed", "roundoff objective change exceeds numerical tolerance"
        )
    gap = max(0.0, float(result.fun) - float(result.mip_dual_bound)) + numeric_cost_error
    record["roundoff_corrections"] = corrections
    record["objective_raw_solver"] = float(result.fun)
    record["objective"] = value
    solve_id = hashlib.sha256(
        (
            forecast.snapshot_id
            + str(forecast.slots[0])
            + str(state.energy_kwh)
            + record["contract_hash"]
        ).encode()
    ).hexdigest()
    record["solve_id"] = solve_id
    plan = DispatchPlan(solve_id, forecast.snapshot_id, flows, energy, value, gap, record)
    issues = validate_dispatch(plan, state, forecast, ledger, reserve_start=reserve_start)
    if issues:
        record["validation_issues"] = issues
        record["candidate_flows"] = flows
        record["candidate_energy"] = energy
        exc = Q4Error("solver_validation_failed", "; ".join(issues[:8]))
        exc.solver_record = record
        raise exc
    if performance is not None:
        performance.record("dispatch_validation", time.perf_counter() - validation_started)
    return plan


def reuse_tail(
    previous: DispatchPlan,
    state: BatteryState,
    forecast: ForecastSnapshot,
    ledger: PurchaseLedger,
    *,
    reserve_start=RESERVE_START,
    diagnostics=None,
) -> DispatchPlan | None:
    """Only reuse feasible tails with a recomputed absolute-bound certificate."""

    def reject(reason):
        if diagnostics is not None:
            diagnostics["tail_reuse_rejected:" + reason] += 1
        return None

    if reserve_start != forecast_reserve_start(forecast):
        return reject("reserve_version")
    if (
        previous.forecast_id != forecast.snapshot_id
        or len(previous.flows) != len(forecast.slots) + 1
    ):
        return reject("forecast_or_window")
    if previous.energy[1] != state.energy_kwh:
        return reject("actual_soc")
    permissions = ledger.permissions(forecast.slots[0], forecast.slots)
    if any(permission in ("new_day", "adjustable") for permission in permissions):
        return reject("contract_permissions")
    flows = tuple(
        tuple(0.0 if i in (A, B, W) else value for i, value in enumerate(flow))
        for flow in previous.flows[1:]
    )
    energy = (state.energy_kwh, *previous.energy[2:])
    value = objective_value(flows, energy, forecast, permissions)
    # The parent's absolute suboptimality bound survives conditioning on its
    # fixed prefix. Paid contracts and elapsed trade/emergency terms are removed
    # by recomputing this tail objective. Never transplant its relative gap.
    if previous.absolute_gap > 1e-4 * max(abs(value), 1e-10):
        return reject("absolute_gap")
    shift = previous.shift_count + 1
    record = {
        **previous.solver_record,
        "reused_tail": True,
        "parent_solve_id": previous.solve_id,
        "shift_count": shift,
        "elapsed_seconds": 0.0,
        "objective": value,
        "gap": previous.absolute_gap / max(abs(value), 1e-10),
        "dual_bound": value - previous.absolute_gap,
        "absolute_gap_bound": previous.absolute_gap,
        "window_start": str(forecast.slots[0]),
        "contract_hash": contract_hash(ledger, forecast),
    }
    plan = replace(
        previous,
        flows=flows,
        energy=energy,
        objective=value,
        solver_record=record,
        shift_count=shift,
    )
    if validate_dispatch(plan, state, forecast, ledger, reserve_start=reserve_start):
        return reject("constraint_validation")
    return plan
