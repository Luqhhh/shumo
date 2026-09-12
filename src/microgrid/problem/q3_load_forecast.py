"""Approved causal ``LOAD-A + LF-A`` production predictor for Q3."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from ..schemas import InputError
from .contracts import STEP_MINUTES, InfoSet, TimeGrid

LOAD_ACTUAL_KIND = "load_actual_kw"
Q3_ISSUE_HOURS = (0, 6, 12, 18)
LOAD_LAG_DAYS = (7, 14, 21, 28)
LOAD_LAG_WEIGHTS = (8 / 15, 4 / 15, 2 / 15, 1 / 15)
LOAD_FORECAST_MODEL_VERSION = "LOAD-A+LF-A+LOAD-HORIZON-A/v2"
LOAD_VERSION_HORIZON_HOURS = 30
LOAD_VERSION_HORIZON_STEPS = LOAD_VERSION_HORIZON_HOURS * 60 // STEP_MINUTES
DEFAULT_LOAD_HORIZON_STEPS = LOAD_VERSION_HORIZON_STEPS
AR_DENOMINATOR_ZERO_TOL_KW2 = 1e-18


def _finite_non_negative(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{name} must be numeric")
    clean = float(value)
    if not math.isfinite(clean) or clean < 0:
        raise InputError(f"{name} must be finite and non-negative")
    return clean


@dataclass(frozen=True)
class LoadForecastPoint:
    """One Q3 load forecast with the provenance required by D_LOAD_FORECAST."""

    decision_time: dt.datetime
    valid_time: dt.datetime
    available_at: dt.datetime
    training_cutoff: dt.datetime
    power_kw: float
    energy_kwh: float
    raw_power_kw: float
    lag_values_kw: tuple[float, float, float, float]
    lag_weights: tuple[float, float, float, float]
    ar1_phi: float
    latest_visible_residual_kw: float
    was_clipped: bool
    model_version: str
    data_version: str
    source_refs: tuple[str, str, str, str]

    def __post_init__(self) -> None:
        if not (self.available_at == self.training_cutoff == self.decision_time < self.valid_time):
            raise ValueError("load forecast availability/training/valid times are inconsistent")
        for name in ("power_kw", "energy_kwh", "lag_values_kw"):
            values = getattr(self, name)
            iterable = values if isinstance(values, tuple) else (values,)
            if any(not math.isfinite(float(value)) or float(value) < 0 for value in iterable):
                raise ValueError(f"{name} must be finite and non-negative")
        if not math.isfinite(self.raw_power_kw):
            raise ValueError("raw_power_kw must be finite")
        if self.was_clipped != (self.raw_power_kw < 0):
            raise ValueError("was_clipped must match raw_power_kw")
        if not math.isclose(
            self.power_kw,
            max(0.0, self.raw_power_kw),
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("power_kw must equal max(0, raw_power_kw)")
        if not 0 <= self.ar1_phi <= 0.999:
            raise ValueError("ar1_phi must be in [0, 0.999]")
        if not math.isfinite(self.latest_visible_residual_kw):
            raise ValueError("latest_visible_residual_kw must be finite")
        if not math.isclose(sum(self.lag_weights), 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("lag_weights must sum to one")
        if len(self.source_refs) != len(LOAD_LAG_DAYS):
            raise ValueError("source_refs must match the four approved weekly lags")
        expected_energy = TimeGrid.power_to_energy_kwh(self.power_kw)
        if not math.isclose(self.energy_kwh, expected_energy, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("energy_kwh must equal power_kw * 1/6 h")
        if not self.model_version or not self.data_version:
            raise ValueError("model_version and data_version must be non-empty")


def load_forecast_id(point: LoadForecastPoint) -> str:
    """Stable domain ID shared by window inputs and provenance sidecars."""

    return f"load|decision={point.decision_time.isoformat()}|valid={point.valid_time.isoformat()}"


def _load_history(info: InfoSet) -> dict[dt.datetime, tuple[float, str]]:
    history: dict[dt.datetime, tuple[float, str]] = {}
    for item in info.visible_items:
        if item.kind != LOAD_ACTUAL_KIND:
            continue
        if item.available_at != item.valid_time:
            raise InputError("actual load must become available at its interval right endpoint")
        if item.valid_time.second or item.valid_time.microsecond:
            raise InputError("actual load timestamps must not contain seconds")
        if item.valid_time.minute % STEP_MINUTES:
            raise InputError("actual load timestamps must be on ten-minute endpoints")
        if item.valid_time in history:
            raise InputError(f"duplicate actual load at {item.valid_time}")
        history[item.valid_time] = (
            _finite_non_negative("actual load", item.value),
            item.source_ref,
        )
    if not history:
        raise InputError("InfoSet contains no visible actual load history")
    return history


def _require_continuous_history(
    history: dict[dt.datetime, tuple[float, str]], decision_time: dt.datetime
) -> None:
    times = sorted(history)
    if times[-1] != decision_time:
        raise InputError("load history must include the interval ending at decision_time")
    step = dt.timedelta(minutes=STEP_MINUTES)
    for previous, current in zip(times[:-1], times[1:], strict=True):
        if current - previous != step:
            raise InputError(f"actual load history gap between {previous} and {current}")


def _base_at(
    timestamp: dt.datetime,
    history: dict[dt.datetime, tuple[float, str]],
) -> tuple[float, tuple[float, float, float, float], tuple[str, str, str, str]]:
    values: list[float] = []
    refs: list[str] = []
    for lag_days in LOAD_LAG_DAYS:
        lag_time = timestamp - dt.timedelta(days=lag_days)
        row = history.get(lag_time)
        if row is None:
            raise InputError(f"missing {lag_days}-day load lag for target {timestamp}")
        values.append(row[0])
        refs.append(row[1])
    lag_values = (values[0], values[1], values[2], values[3])
    source_refs = (refs[0], refs[1], refs[2], refs[3])
    base_kw = math.fsum(
        weight * value for weight, value in zip(LOAD_LAG_WEIGHTS, lag_values, strict=True)
    )
    return base_kw, lag_values, source_refs


def forecast_q3_load(
    info: InfoSet,
    *,
    data_version: str,
) -> tuple[LoadForecastPoint, ...]:
    """Generate one approved immutable 30-hour LF-A version at 00/06/12/18.

    The predictor uses only load actuals retained by ``info``.  It computes
    expanding residual history from the first timestamp with all four weekly
    lags, then fits the approved no-intercept AR(1) at ``decision_time``.
    """

    decision_time = info.decision_time
    if decision_time.time() not in tuple(dt.time(hour=hour) for hour in Q3_ISSUE_HOURS):
        raise InputError("Q3 load forecast is only updated at 00:00/06:00/12:00/18:00")
    if not data_version.strip():
        raise InputError("data_version must be non-empty")
    history = _load_history(info)
    _require_continuous_history(history, decision_time)
    residuals: dict[dt.datetime, float] = {}
    for timestamp in sorted(history):
        try:
            base_kw, _lag_values, _refs = _base_at(timestamp, history)
        except InputError:
            continue
        residuals[timestamp] = history[timestamp][0] - base_kw

    latest_residual = residuals.get(decision_time)
    if latest_residual is None:
        raise InputError("not enough causal history to compute the decision-time load residual")

    numerator = 0.0
    denominator = 0.0
    step = dt.timedelta(minutes=STEP_MINUTES)
    for timestamp, current in residuals.items():
        previous = residuals.get(timestamp - step)
        if previous is not None:
            numerator += previous * current
            denominator += previous * previous
    phi = (
        0.0
        if denominator <= AR_DENOMINATOR_ZERO_TOL_KW2
        else min(0.999, max(0.0, numerator / denominator))
    )

    points: list[LoadForecastPoint] = []
    for horizon_step in range(1, DEFAULT_LOAD_HORIZON_STEPS + 1):
        valid_time = decision_time + horizon_step * step
        base_kw, lag_values, source_refs = _base_at(valid_time, history)
        raw_kw = base_kw + phi**horizon_step * latest_residual
        power_kw = max(0.0, raw_kw)
        points.append(
            LoadForecastPoint(
                decision_time=decision_time,
                valid_time=valid_time,
                available_at=decision_time,
                training_cutoff=decision_time,
                power_kw=power_kw,
                energy_kwh=TimeGrid.power_to_energy_kwh(power_kw),
                raw_power_kw=raw_kw,
                lag_values_kw=lag_values,
                lag_weights=LOAD_LAG_WEIGHTS,
                ar1_phi=phi,
                latest_visible_residual_kw=latest_residual,
                was_clipped=raw_kw < 0,
                model_version=LOAD_FORECAST_MODEL_VERSION,
                data_version=data_version,
                source_refs=source_refs,
            )
        )
    return tuple(points)
