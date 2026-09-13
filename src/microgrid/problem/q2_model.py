"""Rolling Q2 MILP window and solver."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from ..schemas import InputError
from .contracts import (
    CHARGE_EFFICIENCY,
    DISCHARGE_EFFICIENCY,
    ENERGY_ABS_TOL_KWH,
    MAX_BUS_ENERGY_KWH,
    SOC_MAX_KWH,
    SOC_MIN_KWH,
)

COST_ABS_TOL_CNY = 1e-6
# HiGHS can return a tiny binary-feasibility tail on the charge/discharge
# exclusivity rows, whose M coefficient is MAX_BUS_ENERGY_KWH. A relative
# tail of 1e-6 on those rows is worth up to ~8e-4 kWh, so the post-solve
# normalization threshold is set an order of magnitude above that while
# staying far below any physically meaningful simultaneous action.
SOLVER_NUMERIC_TOL_KWH = 1e-2
SOLVER_ACTION_NORMALIZATION_TOL_KWH = SOLVER_NUMERIC_TOL_KWH


def _finite_nonnegative(name: str, values: tuple[float, ...]) -> None:
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must contain real numbers")
        if not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError(f"{name} must contain finite non-negative values")


@dataclass(frozen=True)
class Q2WindowInput:
    valid_times: tuple[dt.datetime, ...]
    price_cny_per_kwh: tuple[float, ...]
    load_forecast_kwh: tuple[float, ...]
    pv_forecast_kwh: tuple[float, ...]
    initial_soc_kwh: float
    terminal_value_cny_per_kwh: float
    annual_terminal_soc_kwh: float | None = None
    fixed_purchase_kwh: tuple[float | None, ...] = ()
    annual_terminal_step: int | None = None
    is_annual_endpoint: bool = False

    def __post_init__(self) -> None:
        size = len(self.valid_times)
        if size == 0:
            raise ValueError("valid_times must not be empty")
        for previous, current in zip(self.valid_times, self.valid_times[1:], strict=False):
            if not isinstance(previous, dt.datetime) or not isinstance(current, dt.datetime):
                raise ValueError("valid_times must contain datetimes")
            if current <= previous:
                raise ValueError("valid_times must be strictly increasing")
        for name, values in (
            ("price_cny_per_kwh", self.price_cny_per_kwh),
            ("load_forecast_kwh", self.load_forecast_kwh),
            ("pv_forecast_kwh", self.pv_forecast_kwh),
        ):
            if len(values) != size:
                raise ValueError(f"{name} length must equal valid_times")
            _finite_nonnegative(name, values)
        if not self.fixed_purchase_kwh:
            object.__setattr__(self, "fixed_purchase_kwh", (None,) * size)
        elif len(self.fixed_purchase_kwh) != size:
            raise ValueError("fixed_purchase_kwh length must equal valid_times")
        for value in self.fixed_purchase_kwh:
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("fixed_purchase_kwh must contain numbers or None")
            if not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError(
                    "fixed_purchase_kwh must contain finite non-negative values or None"
                )
        for name, value in (
            ("initial_soc_kwh", self.initial_soc_kwh),
            ("terminal_value_cny_per_kwh", self.terminal_value_cny_per_kwh),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a real number")
            if not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if not SOC_MIN_KWH <= self.initial_soc_kwh <= SOC_MAX_KWH:
            raise ValueError("initial_soc_kwh is outside approved SOC bounds")
        if self.annual_terminal_soc_kwh is not None and not (
            math.isfinite(float(self.annual_terminal_soc_kwh))
            and SOC_MIN_KWH <= self.annual_terminal_soc_kwh <= SOC_MAX_KWH
        ):
            raise ValueError("annual_terminal_soc_kwh is outside approved SOC bounds")
        if self.annual_terminal_step is not None:
            if (
                isinstance(self.annual_terminal_step, bool)
                or not isinstance(self.annual_terminal_step, int)
                or not 1 <= self.annual_terminal_step <= size
            ):
                raise ValueError("annual_terminal_step must be inside the planning window")
            if self.annual_terminal_soc_kwh is None:
                raise ValueError("annual_terminal_step requires annual_terminal_soc_kwh")


@dataclass(frozen=True)
class Q2ModelConfig:
    horizon_steps: int = 144
    time_limit_s: float | None = None
    terminal_value_multiplier: float = 1.0

    def __post_init__(self) -> None:
        if self.horizon_steps <= 0:
            raise ValueError("horizon_steps must be positive")
        if self.time_limit_s is not None and self.time_limit_s <= 0:
            raise ValueError("time_limit_s must be positive")
        if not math.isfinite(float(self.terminal_value_multiplier)):
            raise ValueError("terminal_value_multiplier must be finite")


@dataclass(frozen=True)
class Q2Plan:
    planned_purchase_kwh: tuple[float, ...]
    charge_kwh: tuple[float, ...]
    discharge_kwh: tuple[float, ...]
    pv_used_kwh: tuple[float, ...]
    soc_kwh: tuple[float, ...]
    objective_cny: float
    solver_status: int
    solver_message: str
    solver_metadata: dict[str, Any]
    e_plan_kwh: tuple[float, ...] = ()


@dataclass(frozen=True)
class Q2ValidationReport:
    ok: bool
    issues: tuple[str, ...]
    max_balance_residual_kwh: float
    max_dynamics_residual_kwh: float
    cost_gap_cny: float
    violations: tuple[str, ...]


class Q2SolveError(InputError):
    """The Q2 MILP did not return a successful solution."""


def _index(horizon_steps: int) -> dict[str, slice]:
    return {
        "q": slice(0, horizon_steps),
        "c": slice(horizon_steps, 2 * horizon_steps),
        "d": slice(2 * horizon_steps, 3 * horizon_steps),
        "pv": slice(3 * horizon_steps, 4 * horizon_steps),
        "soc": slice(4 * horizon_steps, 5 * horizon_steps + 1),
        "z": slice(5 * horizon_steps + 1, 6 * horizon_steps + 1),
        "e_plan": slice(6 * horizon_steps + 1, 7 * horizon_steps + 1),
    }


def _normalize_solver_bounds(
    name: str,
    values: tuple[float, ...],
    *,
    upper_bound: float | None = None,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    normalized: list[float] = []
    adjustments: list[float] = []
    for slot, value in enumerate(values):
        if value < 0.0:
            excess = -value
            if excess > ENERGY_ABS_TOL_KWH:
                raise Q2SolveError(f"{name}[{slot}] exceeds lower bound by {excess} kWh: {value}")
            normalized.append(0.0)
            adjustments.append(excess)
            continue
        if upper_bound is None or value <= upper_bound:
            normalized.append(value)
            continue
        excess = value - upper_bound
        if excess > ENERGY_ABS_TOL_KWH:
            raise Q2SolveError(f"{name}[{slot}] exceeds upper bound by {excess} kWh: {value}")
        normalized.append(upper_bound)
        adjustments.append(excess)
    return tuple(normalized), tuple(adjustments)


def _normalize_first_action_soc(
    initial_soc_kwh: float,
    charge_kwh: tuple[float, ...],
    discharge_kwh: tuple[float, ...],
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]:
    charge = list(charge_kwh)
    discharge = list(discharge_kwh)
    adjustments: list[float] = []
    next_soc_kwh = (
        initial_soc_kwh + CHARGE_EFFICIENCY * charge[0] - discharge[0] / DISCHARGE_EFFICIENCY
    )
    if next_soc_kwh < SOC_MIN_KWH:
        violation = SOC_MIN_KWH - next_soc_kwh
        if violation > ENERGY_ABS_TOL_KWH:
            raise Q2SolveError(
                f"first action exceeds SOC lower bound by {violation} kWh: {next_soc_kwh}"
            )
        corrected = (
            initial_soc_kwh + CHARGE_EFFICIENCY * charge[0] - SOC_MIN_KWH
        ) * DISCHARGE_EFFICIENCY
        adjustments.append(abs(discharge[0] - corrected))
        discharge[0] = corrected
    elif next_soc_kwh > SOC_MAX_KWH:
        violation = next_soc_kwh - SOC_MAX_KWH
        if violation > ENERGY_ABS_TOL_KWH:
            raise Q2SolveError(
                f"first action exceeds SOC upper bound by {violation} kWh: {next_soc_kwh}"
            )
        corrected = (
            SOC_MAX_KWH - initial_soc_kwh + discharge[0] / DISCHARGE_EFFICIENCY
        ) / CHARGE_EFFICIENCY
        adjustments.append(abs(charge[0] - corrected))
        charge[0] = corrected
    return tuple(charge), tuple(discharge), tuple(adjustments)


def _normalize_simultaneous_actions(
    charge_kwh: tuple[float, ...],
    discharge_kwh: tuple[float, ...],
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]:
    charge = list(charge_kwh)
    discharge = list(discharge_kwh)
    adjustments: list[float] = []
    for slot, (charge_value, discharge_value) in enumerate(zip(charge, discharge, strict=True)):
        if charge_value <= 0.0 or discharge_value <= 0.0:
            continue
        if min(charge_value, discharge_value) > SOLVER_ACTION_NORMALIZATION_TOL_KWH:
            raise Q2SolveError(
                f"slot={slot} solver returned simultaneous charge/discharge: "
                f"charge={charge_value}, discharge={discharge_value}"
            )
        internal_delta = CHARGE_EFFICIENCY * charge_value - discharge_value / DISCHARGE_EFFICIENCY
        if internal_delta >= 0.0:
            corrected_charge = internal_delta / CHARGE_EFFICIENCY
            adjustments.extend((abs(charge_value - corrected_charge), discharge_value))
            charge[slot] = corrected_charge
            discharge[slot] = 0.0
        else:
            corrected_discharge = -internal_delta * DISCHARGE_EFFICIENCY
            adjustments.extend((charge_value, abs(discharge_value - corrected_discharge)))
            charge[slot] = 0.0
            discharge[slot] = corrected_discharge
    return tuple(charge), tuple(discharge), tuple(adjustments)


def solve_q2_window(window: Q2WindowInput, config: Q2ModelConfig) -> Q2Plan:
    """Solve one typed Q2 rolling window with SciPy/HiGHS."""

    horizon = len(window.valid_times)
    if config.horizon_steps != horizon:
        raise ValueError("config.horizon_steps must equal the window length")
    index = _index(horizon)
    variable_count = 7 * horizon + 1
    objective = np.zeros(variable_count, dtype=float)
    lower = np.zeros(variable_count, dtype=float)
    upper = np.full(variable_count, np.inf, dtype=float)
    integrality = np.zeros(variable_count, dtype=int)

    for step in range(horizon):
        objective[index["q"].start + step] = window.price_cny_per_kwh[step]
        objective[index["e_plan"].start + step] = 5.0 * window.price_cny_per_kwh[step]
        upper[index["q"].start + step] = window.load_forecast_kwh[step] + MAX_BUS_ENERGY_KWH
        fixed_purchase = window.fixed_purchase_kwh[step]
        if fixed_purchase is not None:
            lower[index["q"].start + step] = float(fixed_purchase)
            upper[index["q"].start + step] = float(fixed_purchase)
        upper[index["c"].start + step] = MAX_BUS_ENERGY_KWH
        upper[index["d"].start + step] = MAX_BUS_ENERGY_KWH
        upper[index["pv"].start + step] = window.pv_forecast_kwh[step]
        lower[index["soc"].start + step] = SOC_MIN_KWH
        upper[index["soc"].start + step] = SOC_MAX_KWH
        integrality[index["z"].start + step] = 1
    lower[index["soc"].start] = upper[index["soc"].start] = window.initial_soc_kwh
    lower[index["soc"].start + horizon] = SOC_MIN_KWH
    upper[index["soc"].start + horizon] = SOC_MAX_KWH
    annual_terminal_step = window.annual_terminal_step
    if annual_terminal_step is None and window.is_annual_endpoint:
        annual_terminal_step = horizon
    if annual_terminal_step is not None and window.annual_terminal_soc_kwh is not None:
        terminal_index = index["soc"].start + annual_terminal_step
        lower[terminal_index] = window.annual_terminal_soc_kwh
        upper[terminal_index] = window.annual_terminal_soc_kwh

    row_count = 4 * horizon
    matrix = np.zeros((row_count, variable_count), dtype=float)
    constraint_lower = np.full(row_count, -np.inf, dtype=float)
    constraint_upper = np.full(row_count, np.inf, dtype=float)
    for step in range(horizon):
        row = step
        matrix[row, index["q"].start + step] = 1.0
        matrix[row, index["e_plan"].start + step] = 1.0
        matrix[row, index["d"].start + step] = 1.0
        matrix[row, index["pv"].start + step] = 1.0
        matrix[row, index["c"].start + step] = -1.0
        constraint_lower[row] = window.load_forecast_kwh[step]

        row = horizon + step
        matrix[row, index["soc"].start + step + 1] = 1.0
        matrix[row, index["soc"].start + step] = -1.0
        matrix[row, index["c"].start + step] = -CHARGE_EFFICIENCY
        matrix[row, index["d"].start + step] = 1.0 / DISCHARGE_EFFICIENCY
        constraint_lower[row] = constraint_upper[row] = 0.0

        row = 2 * horizon + step
        matrix[row, index["c"].start + step] = 1.0
        matrix[row, index["z"].start + step] = -MAX_BUS_ENERGY_KWH
        constraint_upper[row] = 0.0

        row = 3 * horizon + step
        matrix[row, index["d"].start + step] = 1.0
        matrix[row, index["z"].start + step] = MAX_BUS_ENERGY_KWH
        constraint_upper[row] = MAX_BUS_ENERGY_KWH

    if annual_terminal_step is None:
        objective[index["soc"].start + horizon] = (
            -window.terminal_value_cny_per_kwh * config.terminal_value_multiplier
        )
    options: dict[str, float] = {}
    if config.time_limit_s is not None:
        options["time_limit"] = config.time_limit_s
    result = milp(
        c=objective,
        integrality=integrality,
        bounds=Bounds(lower, upper),
        constraints=LinearConstraint(matrix, constraint_lower, constraint_upper),
        options=options or None,
    )
    if result.status != 0 or result.x is None:
        raise Q2SolveError(f"Q2 MILP failed: status={result.status} message={result.message!r}")

    values = np.asarray(result.x, dtype=float)

    def series(name: str, size: int) -> tuple[float, ...]:
        start = index[name].start
        return tuple(float(values[start + offset]) for offset in range(size))

    planned, planned_adjustments = _normalize_solver_bounds(
        "planned_purchase_kwh", series("q", horizon)
    )
    charge, charge_adjustments = _normalize_solver_bounds(
        "charge_kwh", series("c", horizon), upper_bound=MAX_BUS_ENERGY_KWH
    )
    discharge, discharge_adjustments = _normalize_solver_bounds(
        "discharge_kwh", series("d", horizon), upper_bound=MAX_BUS_ENERGY_KWH
    )
    charge, discharge, exclusivity_adjustments = _normalize_simultaneous_actions(charge, discharge)
    charge, discharge, soc_action_adjustments = _normalize_first_action_soc(
        window.initial_soc_kwh,
        charge,
        discharge,
    )
    pv_used = series("pv", horizon)
    e_plan = series("e_plan", horizon) if len(values) >= index["e_plan"].stop else (0.0,) * horizon
    soc = series("soc", horizon + 1)
    objective_cny = sum(
        price * quantity for price, quantity in zip(window.price_cny_per_kwh, planned, strict=True)
    )
    objective_cny += sum(
        5.0 * price * quantity
        for price, quantity in zip(window.price_cny_per_kwh, e_plan, strict=True)
    )
    if annual_terminal_step is None:
        objective_cny -= (
            window.terminal_value_cny_per_kwh * config.terminal_value_multiplier * soc[-1]
        )
    bound_adjustments = (
        planned_adjustments
        + charge_adjustments
        + discharge_adjustments
        + exclusivity_adjustments
        + soc_action_adjustments
    )
    metadata: dict[str, Any] = {
        "status": int(result.status),
        "success": bool(getattr(result, "success", False)),
        "message": str(result.message),
        "mip_gap": getattr(result, "mip_gap", None),
        "mip_node_count": getattr(result, "mip_node_count", None),
        "mip_dual_bound": getattr(result, "mip_dual_bound", None),
        "energy_abs_tol_kwh": ENERGY_ABS_TOL_KWH,
        "cost_abs_tol_cny": COST_ABS_TOL_CNY,
        "bound_normalization_count": len(bound_adjustments),
        "max_bound_normalization_kwh": max(bound_adjustments, default=0.0),
    }
    return Q2Plan(
        planned_purchase_kwh=planned,
        charge_kwh=charge,
        discharge_kwh=discharge,
        pv_used_kwh=pv_used,
        soc_kwh=soc,
        objective_cny=float(objective_cny),
        solver_status=int(result.status),
        solver_message=str(result.message),
        solver_metadata=metadata,
        e_plan_kwh=e_plan,
    )


def validate_q2_plan(window: Q2WindowInput, plan: Q2Plan) -> Q2ValidationReport:
    """Recompute Q2 feasibility and cost invariants independently."""

    issues: list[str] = []
    violations: list[str] = []
    horizon = len(window.valid_times)
    e_plan = plan.e_plan_kwh or (0.0,) * horizon
    if len(e_plan) != horizon:
        issues.append(f"e_plan_kwh length={len(e_plan)} expected={horizon}")
        violations.append(issues[-1])
    series_fields = (
        ("planned_purchase_kwh", plan.planned_purchase_kwh, horizon),
        ("charge_kwh", plan.charge_kwh, horizon),
        ("discharge_kwh", plan.discharge_kwh, horizon),
        ("pv_used_kwh", plan.pv_used_kwh, horizon),
        ("soc_kwh", plan.soc_kwh, horizon + 1),
    )
    for name, values, expected_length in series_fields:
        if len(values) != expected_length:
            message = f"{name} length={len(values)} expected={expected_length}"
            issues.append(message)
            violations.append(message)

    max_balance = 0.0
    max_dynamics = 0.0
    finite_series = True
    for name, values, _expected_length in series_fields:
        for index, value in enumerate(values):
            if not math.isfinite(float(value)):
                finite_series = False
                message = f"slot={index} nonfinite {name}"
                issues.append(message)
                violations.append(message)
            elif name != "soc_kwh" and value < -ENERGY_ABS_TOL_KWH:
                message = f"slot={index} negative {name}"
                issues.append(message)
                violations.append(message)

    if len(plan.soc_kwh) == horizon + 1 and finite_series:
        if abs(plan.soc_kwh[0] - window.initial_soc_kwh) > ENERGY_ABS_TOL_KWH:
            message = "initial SOC mismatch"
            issues.append(message)
            violations.append(message)
        for index in range(horizon + 1):
            if (
                not SOC_MIN_KWH - ENERGY_ABS_TOL_KWH
                <= plan.soc_kwh[index]
                <= SOC_MAX_KWH + ENERGY_ABS_TOL_KWH
            ):
                message = f"slot={index} SOC bounds"
                issues.append(message)
                violations.append(message)
        annual_terminal_step = window.annual_terminal_step
        if annual_terminal_step is None and window.is_annual_endpoint:
            annual_terminal_step = horizon
        if annual_terminal_step is not None and window.annual_terminal_soc_kwh is not None:
            if (
                abs(plan.soc_kwh[annual_terminal_step] - window.annual_terminal_soc_kwh)
                > ENERGY_ABS_TOL_KWH
            ):
                message = "annual terminal SOC mismatch"
                issues.append(message)
                violations.append(message)

    if all(len(values) == horizon for _name, values, _expected_length in series_fields[:4]):
        for index, (quantity, charge, discharge, pv_used, load, pv_forecast) in enumerate(
            zip(
                plan.planned_purchase_kwh,
                plan.charge_kwh,
                plan.discharge_kwh,
                plan.pv_used_kwh,
                window.load_forecast_kwh,
                window.pv_forecast_kwh,
                strict=True,
            )
        ):
            if all(
                math.isfinite(float(value))
                for value in (
                    quantity,
                    charge,
                    discharge,
                    pv_used,
                    load,
                    pv_forecast,
                    e_plan[index],
                )
            ):
                fixed_purchase = window.fixed_purchase_kwh[index]
                if (
                    fixed_purchase is not None
                    and abs(quantity - fixed_purchase) > ENERGY_ABS_TOL_KWH
                ):
                    message = f"slot={index} fixed purchase mismatch"
                    issues.append(message)
                    violations.append(message)

                balance_deficit = load + charge - quantity - e_plan[index] - discharge - pv_used
                max_balance = max(max_balance, max(0.0, balance_deficit))
                if balance_deficit > ENERGY_ABS_TOL_KWH:
                    message = f"slot={index} supply inequality"
                    issues.append(message)
                    violations.append(message)
                if pv_used > pv_forecast + ENERGY_ABS_TOL_KWH:
                    message = f"slot={index} pv upper bound"
                    issues.append(message)
                    violations.append(message)
                if charge > MAX_BUS_ENERGY_KWH + ENERGY_ABS_TOL_KWH:
                    message = f"slot={index} power_upper_bound charge_kwh"
                    issues.append(message)
                    violations.append(message)
                if discharge > MAX_BUS_ENERGY_KWH + ENERGY_ABS_TOL_KWH:
                    message = f"slot={index} power_upper_bound discharge_kwh"
                    issues.append(message)
                    violations.append(message)
                if charge > ENERGY_ABS_TOL_KWH and discharge > ENERGY_ABS_TOL_KWH:
                    message = f"slot={index} simultaneous charge/discharge"
                    issues.append(message)
                    violations.append(message)
        if len(plan.soc_kwh) == horizon + 1 and finite_series:
            for index, (previous, charge, discharge, current) in enumerate(
                zip(
                    plan.soc_kwh[:-1],
                    plan.charge_kwh,
                    plan.discharge_kwh,
                    plan.soc_kwh[1:],
                    strict=True,
                )
            ):
                residual = current - (
                    previous + CHARGE_EFFICIENCY * charge - discharge / DISCHARGE_EFFICIENCY
                )
                max_dynamics = max(max_dynamics, abs(residual))
                # The solver SOC series is compared against the normalized
                # actions, so the residual is bounded by the same tail used
                # to normalize those actions.
                if abs(residual) > SOLVER_NUMERIC_TOL_KWH:
                    message = f"slot={index} SOC dynamics"
                    issues.append(message)
                    violations.append(message)

    cost_gap = float("inf")
    if finite_series and math.isfinite(float(plan.objective_cny)):
        recomputed = sum(
            price * quantity
            for price, quantity in zip(
                window.price_cny_per_kwh, plan.planned_purchase_kwh, strict=True
            )
        )
        recomputed += sum(
            5.0 * price * quantity
            for price, quantity in zip(window.price_cny_per_kwh, e_plan, strict=True)
        )
        annual_terminal_step = window.annual_terminal_step
        if annual_terminal_step is None and window.is_annual_endpoint:
            annual_terminal_step = horizon
        if annual_terminal_step is None:
            recomputed -= window.terminal_value_cny_per_kwh * plan.soc_kwh[-1]
        cost_gap = abs(float(plan.objective_cny) - recomputed)
        if cost_gap > COST_ABS_TOL_CNY:
            message = f"objective cost mismatch gap={cost_gap}"
            issues.append(message)
            violations.append("objective cost mismatch")

    if plan.solver_status != 0:
        message = f"solver status={plan.solver_status}"
        issues.append(message)
        violations.append(message)
    return Q2ValidationReport(
        ok=not issues,
        issues=tuple(issues),
        max_balance_residual_kwh=max_balance,
        max_dynamics_residual_kwh=max_dynamics,
        cost_gap_cny=cost_gap,
        violations=tuple(violations),
    )
