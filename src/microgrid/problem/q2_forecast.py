"""Causal weekly-lag and AR(1) forecast helpers for Q2."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass

from ..schemas import InputError
from .q2_inputs import ActualInterval

_WEEKLY_LAGS = (7, 14, 21, 28)
_MAX_AR1_PHI = 0.99


@dataclass(frozen=True)
class ForecastConfig:
    weights: tuple[float, float, float, float]
    ar1_phi: float | None = None
    model_version: str = "q2-weekly-ar1-v1"
    allow_short_history: bool = False

    def __post_init__(self) -> None:
        if len(self.weights) != len(_WEEKLY_LAGS):
            raise ValueError("weights must contain four weekly-lag values")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
            for value in self.weights
        ):
            raise ValueError("weights must be finite and non-negative")
        if sum(float(value) for value in self.weights) <= 0:
            raise ValueError("weights must not be all zero")
        if self.ar1_phi is not None and (
            isinstance(self.ar1_phi, bool)
            or not isinstance(self.ar1_phi, (int, float))
            or not math.isfinite(float(self.ar1_phi))
            or not -_MAX_AR1_PHI <= float(self.ar1_phi) <= _MAX_AR1_PHI
        ):
            raise ValueError("ar1_phi must be finite and in [-0.99, 0.99]")
        if not self.model_version:
            raise ValueError("model_version must not be empty")


@dataclass(frozen=True)
class ForecastPoint:
    valid_time: dt.datetime
    available_at: dt.datetime
    load_kw: float
    pv_kw: float
    training_cutoff: dt.datetime
    model_version: str
    data_version: str
    fallback_reason: str


def estimate_ar1_phi(residuals: Sequence[float]) -> float:
    """Estimate a zero-intercept AR(1) coefficient and bound it."""

    values = [float(value) for value in residuals if math.isfinite(float(value))]
    if len(values) < 2:
        return 0.0
    denominator = sum(value * value for value in values[:-1])
    if denominator == 0:
        return 0.0
    coefficient = sum(
        current * previous for previous, current in zip(values, values[1:], strict=False)
    )
    coefficient /= denominator
    return max(-_MAX_AR1_PHI, min(_MAX_AR1_PHI, coefficient))


def _validate_datetime(name: str, value: dt.datetime) -> None:
    if not isinstance(value, dt.datetime):
        raise ValueError(f"{name} must be a datetime")
    if value.second or value.microsecond or value.minute % 10:
        raise ValueError(f"{name} must be aligned to the ten-minute grid")


def _slot(start: dt.datetime) -> int:
    return (start.hour * 60 + start.minute) // 10


def _normalise_weights(weights: Sequence[float]) -> tuple[float, ...]:
    total = sum(float(value) for value in weights)
    return tuple(float(value) / total for value in weights)


def _weighted_lag(
    *,
    target_start: dt.datetime,
    by_key: dict[tuple[dt.date, int], ActualInterval],
    weights: Sequence[float],
    allow_short_history: bool,
) -> tuple[float, float, str, tuple[ActualInterval, ...]]:
    target_slot = _slot(target_start)
    lag_items: list[ActualInterval | None] = [
        by_key.get((target_start.date() - dt.timedelta(days=days), target_slot))
        for days in _WEEKLY_LAGS
    ]
    missing = [
        f"{days}d" for days, item in zip(_WEEKLY_LAGS, lag_items, strict=True) if item is None
    ]
    if missing and not allow_short_history:
        raise InputError("missing weekly lags: " + ", ".join(missing))

    present = [
        (weight, item) for weight, item in zip(weights, lag_items, strict=True) if item is not None
    ]
    if not present:
        raise InputError("missing weekly lags: " + ", ".join(missing))
    weight_total = sum(weight for weight, _item in present)
    if weight_total <= 0:
        raise InputError("available weekly-lag weights are all zero")
    load = sum(weight * item.load_kw for weight, item in present) / weight_total
    pv = sum(weight * item.pv_kw for weight, item in present) / weight_total
    reason = "" if not missing else "missing weekly lags: " + ", ".join(missing)
    return load, pv, reason, tuple(item for _weight, item in present)


def _visible_data_version(actuals: Sequence[ActualInterval]) -> str:
    payload = "|".join(
        f"{item.day.isoformat()},{item.slot},{item.load_kw:.12g},{item.pv_kw:.12g}"
        for item in actuals
    )
    return "visible-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build_q2_forecast(
    actuals: tuple[ActualInterval, ...],
    decision_time: dt.datetime,
    horizon_start: dt.datetime,
    config: ForecastConfig,
    horizon_steps: int = 144,
) -> tuple[ForecastPoint, ...]:
    """Build a strictly causal forecast for ten-minute horizon intervals."""

    _validate_datetime("decision_time", decision_time)
    _validate_datetime("horizon_start", horizon_start)
    if horizon_steps <= 0:
        raise ValueError("horizon_steps must be positive")

    ordered = tuple(sorted(actuals, key=lambda item: (item.day, item.slot)))
    visible = tuple(item for item in ordered if item.end <= decision_time)
    by_key = {(item.day, item.slot): item for item in visible}
    if len(by_key) != len(visible):
        raise InputError("actual history contains duplicate day/slot keys")
    if not visible:
        raise InputError("no actual history is visible at decision_time")

    weights = _normalise_weights(config.weights)
    residual_load: list[tuple[dt.datetime, float]] = []
    residual_pv: list[tuple[dt.datetime, float]] = []
    for item in visible:
        target_slot = _slot(item.start)
        lag_keys = [(item.day - dt.timedelta(days=days), target_slot) for days in _WEEKLY_LAGS]
        if any(key not in by_key for key in lag_keys):
            continue
        baseline_load, baseline_pv, _reason, lag_items = _weighted_lag(
            target_start=item.start,
            by_key=by_key,
            weights=weights,
            allow_short_history=True,
        )
        if len(lag_items) == len(_WEEKLY_LAGS):
            residual_load.append((item.end, item.load_kw - baseline_load))
            residual_pv.append((item.end, item.pv_kw - baseline_pv))

    phi = (
        float(config.ar1_phi)
        if config.ar1_phi is not None
        else estimate_ar1_phi([value for _time, value in residual_load])
    )
    training_cutoff = max(item.end for item in visible)
    data_version = _visible_data_version(visible)
    latest_load_residual = residual_load[-1] if residual_load else None
    latest_pv_residual = residual_pv[-1] if residual_pv else None

    points: list[ForecastPoint] = []
    for step in range(horizon_steps):
        target_start = horizon_start + dt.timedelta(minutes=10 * step)
        load, pv, fallback_reason, _lag_items = _weighted_lag(
            target_start=target_start,
            by_key=by_key,
            weights=weights,
            allow_short_history=config.allow_short_history,
        )
        if latest_load_residual is not None:
            age_steps = max(
                0,
                int(
                    (
                        target_start + dt.timedelta(minutes=10) - latest_load_residual[0]
                    ).total_seconds()
                    // 600
                ),
            )
            load += latest_load_residual[1] * (phi**age_steps)
        if latest_pv_residual is not None:
            age_steps = max(
                0,
                int(
                    (
                        target_start + dt.timedelta(minutes=10) - latest_pv_residual[0]
                    ).total_seconds()
                    // 600
                ),
            )
            pv += latest_pv_residual[1] * (phi**age_steps)
        points.append(
            ForecastPoint(
                valid_time=target_start + dt.timedelta(minutes=10),
                available_at=decision_time,
                load_kw=max(0.0, load),
                pv_kw=max(0.0, pv),
                training_cutoff=training_cutoff,
                model_version=config.model_version,
                data_version=data_version,
                fallback_reason=fallback_reason,
            )
        )
    return tuple(points)
