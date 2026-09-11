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
    }


def solve_q2_window(window: Q2WindowInput, config: Q2ModelConfig) -> Q2Plan:
    """Solve one typed Q2 rolling window with SciPy/HiGHS."""

    horizon = len(window.valid_times)
    if config.horizon_steps != horizon:
        raise ValueError("config.horizon_steps must equal the window length")
    index = _index(horizon)
    variable_count = 6 * horizon + 1
    objective = np.zeros(variable_count, dtype=float)
    lower = np.zeros(variable_count, dtype=float)
    upper = np.full(variable_count, np.inf, dtype=float)
    integrality = np.zeros(variable_count, dtype=int)

    for step in range(horizon):
        objective[index["q"].start + step] = window.price_cny_per_kwh[step]
        upper[index["q"].start + step] = window.load_forecast_kwh[step] + MAX_BUS_ENERGY_KWH
        upper[index["c"].start + step] = MAX_BUS_ENERGY_KWH
        upper[index["d"].start + step] = MAX_BUS_ENERGY_KWH
        upper[index["pv"].start + step] = window.pv_forecast_kwh[step]
        lower[index["soc"].start + step] = SOC_MIN_KWH
        upper[index["soc"].start + step] = SOC_MAX_KWH
        integrality[index["z"].start + step] = 1
    lower[index["soc"].start] = upper[index["soc"].start] = window.initial_soc_kwh
    lower[index["soc"].start + horizon] = SOC_MIN_KWH
    upper[index["soc"].start + horizon] = SOC_MAX_KWH
    if window.is_annual_endpoint and window.annual_terminal_soc_kwh is not None:
        lower[index["soc"].start + horizon] = window.annual_terminal_soc_kwh
        upper[index["soc"].start + horizon] = window.annual_terminal_soc_kwh

    row_count = 4 * horizon
    matrix = np.zeros((row_count, variable_count), dtype=float)
    constraint_lower = np.full(row_count, -np.inf, dtype=float)
    constraint_upper = np.full(row_count, np.inf, dtype=float)
    for step in range(horizon):
        row = step
        matrix[row, index["q"].start + step] = 1.0
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

    planned = series("q", horizon)
    charge = series("c", horizon)
    discharge = series("d", horizon)
    pv_used = series("pv", horizon)
    soc = series("soc", horizon + 1)
    objective_cny = (
        sum(
            price * quantity
            for price, quantity in zip(window.price_cny_per_kwh, planned, strict=True)
        )
        - window.terminal_value_cny_per_kwh * config.terminal_value_multiplier * soc[-1]
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
    )
