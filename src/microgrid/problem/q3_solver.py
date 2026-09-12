"""Approved single-window Q3 MILP and independent solution validator."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from ..schemas import InputError
from .contracts import (
    CHARGE_EFFICIENCY,
    DISCHARGE_EFFICIENCY,
    ENERGY_ABS_TOL_KWH,
    MAX_BUS_ENERGY_KWH,
    SOC_MAX_KWH,
    SOC_MIN_KWH,
)
from .q3_window import Q3WindowInput

COST_ABS_TOL_CNY = 1e-5


class Q3WindowSolveError(InputError):
    """The approved Q3 window MILP did not return an acceptable solution."""


@dataclass(frozen=True)
class Q3WindowSolution:
    decision_time: dt.datetime
    purchase_kwh: tuple[float, ...]
    delta_plus_kwh: tuple[float, ...]
    delta_minus_kwh: tuple[float, ...]
    charge_kwh: tuple[float, ...]
    discharge_kwh: tuple[float, ...]
    pv_used_kwh: tuple[float, ...]
    grid_spill_kwh: tuple[float, ...]
    predicted_emergency_kwh: tuple[float, ...]
    energy_kwh: tuple[float, ...]
    objective_cny: float
    economic_components_cny: dict[str, float]
    solver_status: int
    solver_message: str
    solver_metadata: dict[str, Any]


def _slices(n: int) -> dict[str, slice]:
    names = ("q", "dp", "dm", "c", "d", "pv", "spill", "emergency")
    result = {name: slice(index * n, (index + 1) * n) for index, name in enumerate(names)}
    result["e"] = slice(8 * n, 9 * n + 1)
    result["z"] = slice(9 * n + 1, 10 * n + 1)
    result["y"] = slice(10 * n + 1, 11 * n + 1)
    result["w"] = slice(11 * n + 1, 12 * n + 1)
    return result


class _ConstraintRows:
    def __init__(self, variable_count: int) -> None:
        self.variable_count = variable_count
        self.rows: list[int] = []
        self.columns: list[int] = []
        self.values: list[float] = []
        self.lower: list[float] = []
        self.upper: list[float] = []

    def add(self, terms: tuple[tuple[int, float], ...], lower: float, upper: float) -> None:
        row = len(self.lower)
        for column, value in terms:
            self.rows.append(row)
            self.columns.append(column)
            self.values.append(value)
        self.lower.append(lower)
        self.upper.append(upper)

    def build(self) -> LinearConstraint:
        matrix = coo_matrix(
            (self.values, (self.rows, self.columns)),
            shape=(len(self.lower), self.variable_count),
            dtype=float,
        ).tocsr()
        return LinearConstraint(
            matrix,
            np.asarray(self.lower, dtype=float),
            np.asarray(self.upper, dtype=float),
        )


def _slot(index: dict[str, slice], name: str, offset: int) -> int:
    return index[name].start + offset


def _objective_components(
    window: Q3WindowInput,
    *,
    purchase: tuple[float, ...],
    delta_plus: tuple[float, ...],
    delta_minus: tuple[float, ...],
    emergency: tuple[float, ...],
    terminal_energy: float,
) -> dict[str, float]:
    new_plan = math.fsum(
        point.base_price_cny_per_kwh * purchase[k]
        for k, point in enumerate(window.points)
        if point.purchase_mode == "new_commitment"
    )
    lookahead = math.fsum(
        point.base_price_cny_per_kwh * purchase[k]
        for k, point in enumerate(window.points)
        if point.purchase_mode == "lookahead_only"
    )
    adjustment = 0.0
    if window.planning_event == "revision":
        price = float(window.transaction_price_cny_per_kwh)
        adjustment = math.fsum(
            price * (1.5 * delta_plus[k] + 0.5 * delta_minus[k])
            for k, point in enumerate(window.points)
            if point.purchase_mode == "adjustable_commitment"
        )
    predicted_emergency = math.fsum(
        5.0 * point.base_price_cny_per_kwh * emergency[k] for k, point in enumerate(window.points)
    )
    terminal_credit = -window.terminal_value_cny_per_kwh * terminal_energy
    return {
        "new_plan_cost_cny": new_plan,
        "adjustment_cost_cny": adjustment,
        "lookahead_surrogate_cost_cny": lookahead,
        "predicted_emergency_cost_cny": predicted_emergency,
        "terminal_value_credit_cny": terminal_credit,
    }


def solve_q3_window_milp(
    window: Q3WindowInput,
    *,
    time_limit_s: float | None = None,
) -> Q3WindowSolution:
    """Solve one approved rolling window without mutating plan or replay state."""

    n = len(window.points)
    index = _slices(n)
    variable_count = 12 * n + 1
    objective = np.zeros(variable_count, dtype=float)
    lower = np.zeros(variable_count, dtype=float)
    upper = np.full(variable_count, np.inf, dtype=float)
    integrality = np.zeros(variable_count, dtype=int)
    rows = _ConstraintRows(variable_count)

    lower[index["e"]] = SOC_MIN_KWH
    upper[index["e"]] = SOC_MAX_KWH
    lower[index["e"].start] = upper[index["e"].start] = window.battery_state.energy_kwh
    if window.terminal_mode == "year_end_equality":
        target = float(window.terminal_target_kwh)
        lower[index["e"].stop - 1] = upper[index["e"].stop - 1] = target
    else:
        objective[index["e"].stop - 1] = -window.terminal_value_cny_per_kwh

    for k, point in enumerate(window.points):
        q = _slot(index, "q", k)
        dp = _slot(index, "dp", k)
        dm = _slot(index, "dm", k)
        charge = _slot(index, "c", k)
        discharge = _slot(index, "d", k)
        pv_used = _slot(index, "pv", k)
        spill = _slot(index, "spill", k)
        emergency = _slot(index, "emergency", k)
        energy = _slot(index, "e", k)
        energy_next = _slot(index, "e", k + 1)
        charge_mode = _slot(index, "z", k)
        emergency_mode = _slot(index, "y", k)
        adjustment_mode = _slot(index, "w", k)

        previous = point.previous_committed_kwh or 0.0
        useful_purchase_upper = point.load_kwh + MAX_BUS_ENERGY_KWH
        purchase_upper = max(previous, useful_purchase_upper)
        upper[q] = purchase_upper
        upper[charge] = MAX_BUS_ENERGY_KWH
        upper[discharge] = MAX_BUS_ENERGY_KWH
        upper[pv_used] = point.pv_kwh
        upper[spill] = purchase_upper
        upper[emergency] = point.load_kwh
        upper[charge_mode] = upper[emergency_mode] = 1.0
        integrality[charge_mode] = integrality[emergency_mode] = 1

        if point.purchase_mode == "fixed_commitment":
            lower[q] = upper[q] = previous
            upper[dp] = upper[dm] = upper[adjustment_mode] = 0.0
        elif point.purchase_mode == "adjustable_commitment":
            upper[dp] = purchase_upper
            upper[dm] = previous
            upper[adjustment_mode] = 1.0
            integrality[adjustment_mode] = 1
            trade_price = float(window.transaction_price_cny_per_kwh)
            objective[dp] = 1.5 * trade_price
            objective[dm] = 0.5 * trade_price
            rows.add(((q, 1.0), (dp, -1.0), (dm, 1.0)), previous, previous)
            rows.add(((dp, 1.0), (adjustment_mode, -purchase_upper)), -np.inf, 0.0)
            rows.add(
                ((dm, 1.0), (adjustment_mode, previous)),
                -np.inf,
                previous,
            )
        else:
            upper[dp] = upper[dm] = upper[adjustment_mode] = 0.0
            objective[q] = point.base_price_cny_per_kwh

        objective[emergency] = 5.0 * point.base_price_cny_per_kwh

        rows.add(
            (
                (q, 1.0),
                (pv_used, 1.0),
                (discharge, 1.0),
                (emergency, 1.0),
                (charge, -1.0),
                (spill, -1.0),
            ),
            point.load_kwh,
            point.load_kwh,
        )
        rows.add(
            (
                (energy_next, 1.0),
                (energy, -1.0),
                (charge, -CHARGE_EFFICIENCY),
                (discharge, 1.0 / DISCHARGE_EFFICIENCY),
            ),
            0.0,
            0.0,
        )
        rows.add(((charge, 1.0), (charge_mode, -MAX_BUS_ENERGY_KWH)), -np.inf, 0.0)
        rows.add(
            ((discharge, 1.0), (charge_mode, MAX_BUS_ENERGY_KWH)),
            -np.inf,
            MAX_BUS_ENERGY_KWH,
        )
        rows.add(((spill, 1.0), (q, -1.0)), -np.inf, 0.0)
        rows.add(((emergency, 1.0), (emergency_mode, -point.load_kwh)), -np.inf, 0.0)
        rows.add(
            ((charge, 1.0), (emergency_mode, MAX_BUS_ENERGY_KWH)),
            -np.inf,
            MAX_BUS_ENERGY_KWH,
        )
        rows.add(
            ((spill, 1.0), (emergency_mode, purchase_upper)),
            -np.inf,
            purchase_upper,
        )
        rows.add(((pv_used, -1.0), (emergency_mode, point.pv_kwh)), -np.inf, 0.0)

    options: dict[str, float] = {}
    if time_limit_s is not None:
        if not math.isfinite(time_limit_s) or time_limit_s <= 0:
            raise InputError("Q3 time_limit_s must be finite and positive")
        options["time_limit"] = float(time_limit_s)
    result = milp(
        c=objective,
        integrality=integrality,
        bounds=Bounds(lower, upper),
        constraints=rows.build(),
        options=options or None,
    )
    if result.status != 0 or result.x is None:
        raise Q3WindowSolveError(
            f"Q3 window MILP failed: status={result.status} message={result.message!r}"
        )

    vector = np.asarray(result.x, dtype=float)

    def values(name: str) -> tuple[float, ...]:
        return tuple(float(value) for value in vector[index[name]])

    purchase = values("q")
    delta_plus = values("dp")
    delta_minus = values("dm")
    charge = values("c")
    discharge = values("d")
    pv_used = values("pv")
    spill = values("spill")
    emergency = values("emergency")
    energy = values("e")
    components = _objective_components(
        window,
        purchase=purchase,
        delta_plus=delta_plus,
        delta_minus=delta_minus,
        emergency=emergency,
        terminal_energy=energy[-1],
    )
    objective_cny = math.fsum(components.values())
    solver_objective = float(result.fun) if result.fun is not None else objective_cny
    if not math.isclose(
        solver_objective,
        objective_cny,
        rel_tol=0.0,
        abs_tol=COST_ABS_TOL_CNY,
    ):
        raise Q3WindowSolveError("Q3 solver objective disagrees with independent cost components")
    return Q3WindowSolution(
        decision_time=window.decision_time,
        purchase_kwh=purchase,
        delta_plus_kwh=delta_plus,
        delta_minus_kwh=delta_minus,
        charge_kwh=charge,
        discharge_kwh=discharge,
        pv_used_kwh=pv_used,
        grid_spill_kwh=spill,
        predicted_emergency_kwh=emergency,
        energy_kwh=energy,
        objective_cny=objective_cny,
        economic_components_cny=components,
        solver_status=int(result.status),
        solver_message=str(result.message),
        solver_metadata={
            "status": int(result.status),
            "success": bool(getattr(result, "success", False)),
            "message": str(result.message),
            "mip_gap": getattr(result, "mip_gap", None),
            "mip_node_count": getattr(result, "mip_node_count", None),
            "mip_dual_bound": getattr(result, "mip_dual_bound", None),
            "solver_objective_cny": solver_objective,
        },
    )


def validate_q3_window_solution(
    window: Q3WindowInput,
    solution: Q3WindowSolution,
) -> dict[str, Any]:
    """Recompute every approved physical and economic window constraint."""

    n = len(window.points)
    issues: list[str] = []
    if solution.decision_time != window.decision_time:
        issues.append("solution decision_time does not match window")
    if not math.isfinite(solution.objective_cny):
        issues.append("reported objective is not finite")
    for name, value in solution.economic_components_cny.items():
        if not math.isfinite(value):
            issues.append(f"economic component {name!r} is not finite")
    arrays = {
        "purchase_kwh": (solution.purchase_kwh, n),
        "delta_plus_kwh": (solution.delta_plus_kwh, n),
        "delta_minus_kwh": (solution.delta_minus_kwh, n),
        "charge_kwh": (solution.charge_kwh, n),
        "discharge_kwh": (solution.discharge_kwh, n),
        "pv_used_kwh": (solution.pv_used_kwh, n),
        "grid_spill_kwh": (solution.grid_spill_kwh, n),
        "predicted_emergency_kwh": (solution.predicted_emergency_kwh, n),
        "energy_kwh": (solution.energy_kwh, n + 1),
    }
    for name, (values, expected) in arrays.items():
        if len(values) != expected:
            issues.append(f"{name} length {len(values)} != {expected}")
        for k, value in enumerate(values):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                issues.append(f"{name}[{k}] is not finite")
    if issues:
        return {"ok": False, "issues": issues}

    max_balance = 0.0
    max_dynamics = 0.0
    for k, point in enumerate(window.points):
        q = solution.purchase_kwh[k]
        dp = solution.delta_plus_kwh[k]
        dm = solution.delta_minus_kwh[k]
        c = solution.charge_kwh[k]
        d = solution.discharge_kwh[k]
        pv = solution.pv_used_kwh[k]
        spill = solution.grid_spill_kwh[k]
        emergency = solution.predicted_emergency_kwh[k]
        values = (q, dp, dm, c, d, pv, spill, emergency)
        if any(value < -ENERGY_ABS_TOL_KWH for value in values):
            issues.append(f"slot {k}: a non-negative variable is negative")
        if (
            c > MAX_BUS_ENERGY_KWH + ENERGY_ABS_TOL_KWH
            or d > MAX_BUS_ENERGY_KWH + ENERGY_ABS_TOL_KWH
        ):
            issues.append(f"slot {k}: battery action exceeds the bus limit")
        if c > ENERGY_ABS_TOL_KWH and d > ENERGY_ABS_TOL_KWH:
            issues.append(f"slot {k}: simultaneous charge and discharge")
        if pv > point.pv_kwh + ENERGY_ABS_TOL_KWH:
            issues.append(f"slot {k}: PV use exceeds forecast")
        if spill > q + ENERGY_ABS_TOL_KWH:
            issues.append(f"slot {k}: grid spill exceeds contracted purchase")
        if emergency > ENERGY_ABS_TOL_KWH and (
            c > ENERGY_ABS_TOL_KWH
            or spill > ENERGY_ABS_TOL_KWH
            or point.pv_kwh - pv > ENERGY_ABS_TOL_KWH
        ):
            issues.append(f"slot {k}: emergency violates residual-shortage semantics")

        if point.purchase_mode == "fixed_commitment":
            previous = float(point.previous_committed_kwh)
            if (
                abs(q - previous) > ENERGY_ABS_TOL_KWH
                or dp > ENERGY_ABS_TOL_KWH
                or dm > ENERGY_ABS_TOL_KWH
            ):
                issues.append(f"slot {k}: fixed commitment changed")
        elif point.purchase_mode == "adjustable_commitment":
            previous = float(point.previous_committed_kwh)
            if abs(q - (previous + dp - dm)) > ENERGY_ABS_TOL_KWH:
                issues.append(f"slot {k}: adjustment deltas do not reconstruct purchase")
            if dp > ENERGY_ABS_TOL_KWH and dm > ENERGY_ABS_TOL_KWH:
                issues.append(f"slot {k}: delta_plus and delta_minus coexist")
        elif dp > ENERGY_ABS_TOL_KWH or dm > ENERGY_ABS_TOL_KWH:
            issues.append(f"slot {k}: non-adjustable purchase has adjustment deltas")

        balance = q + pv + d + emergency - point.load_kwh - c - spill
        dynamics = solution.energy_kwh[k + 1] - (
            solution.energy_kwh[k] + CHARGE_EFFICIENCY * c - d / DISCHARGE_EFFICIENCY
        )
        max_balance = max(max_balance, abs(balance))
        max_dynamics = max(max_dynamics, abs(dynamics))
        if abs(balance) > ENERGY_ABS_TOL_KWH:
            issues.append(f"slot {k}: supply balance residual {balance:.6g} kWh")
        if abs(dynamics) > ENERGY_ABS_TOL_KWH:
            issues.append(f"slot {k}: battery dynamics residual {dynamics:.6g} kWh")

    for k, energy in enumerate(solution.energy_kwh):
        if energy < SOC_MIN_KWH - ENERGY_ABS_TOL_KWH or energy > SOC_MAX_KWH + ENERGY_ABS_TOL_KWH:
            issues.append(f"energy[{k}] is outside approved bounds")
    if abs(solution.energy_kwh[0] - window.battery_state.energy_kwh) > ENERGY_ABS_TOL_KWH:
        issues.append("initial battery state does not match window")
    if (
        window.terminal_mode == "year_end_equality"
        and abs(solution.energy_kwh[-1] - float(window.terminal_target_kwh)) > ENERGY_ABS_TOL_KWH
    ):
        issues.append("year-end battery target is not met")

    components = _objective_components(
        window,
        purchase=solution.purchase_kwh,
        delta_plus=solution.delta_plus_kwh,
        delta_minus=solution.delta_minus_kwh,
        emergency=solution.predicted_emergency_kwh,
        terminal_energy=solution.energy_kwh[-1],
    )
    independent_objective = math.fsum(components.values())
    cost_gap = abs(independent_objective - solution.objective_cny)
    if cost_gap > COST_ABS_TOL_CNY:
        issues.append(f"objective mismatch {cost_gap:.6g} CNY")
    if set(solution.economic_components_cny) != set(components):
        issues.append("reported economic component keys do not match the approved objective")
    else:
        for name, value in components.items():
            if abs(solution.economic_components_cny[name] - value) > COST_ABS_TOL_CNY:
                issues.append(f"economic component {name!r} does not match independent value")
    return {
        "ok": not issues,
        "issues": issues,
        "max_balance_residual_kwh": max_balance,
        "max_dynamics_residual_kwh": max_dynamics,
        "independent_objective_cny": independent_objective,
        "reported_objective_cny": solution.objective_cny,
        "cost_gap_cny": cost_gap,
        "economic_components_cny": components,
    }
