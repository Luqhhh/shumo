"""Approved Q3 hourly-to-ten-minute PV forecast resampling contract.

This module starts *after* forecast-vintage combination.  It accepts one
already-combined forecast value for each of the next 24 hourly valid times and
therefore cannot select vintages, estimate weights, or read forecast-error
history.  ``D_MODEL_Q3`` remains outside this module.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy.interpolate import PchipInterpolator

from .contracts import STEP_MINUTES, TimeGrid

ResamplingMethod = Literal["linear", "pchip"]
HOURS_PER_FORECAST = 24
TEN_MINUTE_POINTS = HOURS_PER_FORECAST * 60 // STEP_MINUTES


def _require_non_negative_finite(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a real number")
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _require_ten_minute_boundary(name: str, value: dt.datetime) -> None:
    if value.second != 0 or value.microsecond != 0 or value.minute % STEP_MINUTES != 0:
        raise ValueError(f"{name} must be aligned to a ten-minute boundary")


@dataclass(frozen=True)
class CombinedHourlyPVPoint:
    """One hourly point produced by an upstream forecast-combination layer."""

    valid_time: dt.datetime
    power_kw: float
    source_ref: str = ""

    def __post_init__(self) -> None:
        _require_non_negative_finite("power_kw", self.power_kw)
        if self.valid_time.minute != 0 or self.valid_time.second != 0:
            raise ValueError("hourly forecast valid_time must be on an exact hour")
        if self.valid_time.microsecond != 0:
            raise ValueError("hourly forecast valid_time must not contain microseconds")


@dataclass(frozen=True)
class PVBoundaryProxy:
    """Mean actual PV of the ten-minute interval ending at ``interval_end``."""

    interval_end: dt.datetime
    mean_power_kw: float
    source_ref: str = ""

    def __post_init__(self) -> None:
        _require_ten_minute_boundary("interval_end", self.interval_end)
        _require_non_negative_finite("mean_power_kw", self.mean_power_kw)


@dataclass(frozen=True)
class TenMinutePVPoint:
    """PV forecast assigned to the interval ending at ``slot_end``."""

    slot_end: dt.datetime
    power_kw: float
    energy_kwh: float

    def __post_init__(self) -> None:
        _require_ten_minute_boundary("slot_end", self.slot_end)
        _require_non_negative_finite("power_kw", self.power_kw)
        _require_non_negative_finite("energy_kwh", self.energy_kwh)
        expected_energy = TimeGrid.power_to_energy_kwh(self.power_kw)
        if not math.isclose(self.energy_kwh, expected_energy, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("energy_kwh must equal power_kw * 1/6 h")


def _validate_hourly_points(
    decision_time: dt.datetime,
    hourly_points: tuple[CombinedHourlyPVPoint, ...],
) -> None:
    _require_ten_minute_boundary("decision_time", decision_time)
    if len(hourly_points) != HOURS_PER_FORECAST:
        raise ValueError(f"hourly_points must contain exactly {HOURS_PER_FORECAST} combined values")
    for lead_hour, point in enumerate(hourly_points, start=1):
        expected_valid_time = decision_time + dt.timedelta(hours=lead_hour)
        if point.valid_time != expected_valid_time:
            raise ValueError(
                "hourly_points must be ordered at decision_time + 1h through +24h; "
                f"lead {lead_hour} has {point.valid_time}, expected {expected_valid_time}"
            )


def resample_combined_hourly_pv(
    *,
    decision_time: dt.datetime,
    boundary_proxy: PVBoundaryProxy,
    hourly_points: tuple[CombinedHourlyPVPoint, ...],
    method: ResamplingMethod = "linear",
) -> tuple[TenMinutePVPoint, ...]:
    """Resample one combined hourly forecast to ten-minute right endpoints.

    ``boundary_proxy`` is the mean power of the most recently completed
    ten-minute actual-PV interval, used only as a boundary proxy.  Output point
    one has ``slot_end = decision_time + 10 minutes``; output point 144 has
    ``slot_end = decision_time + 24 hours``.
    """

    _validate_hourly_points(decision_time, hourly_points)
    if boundary_proxy.interval_end != decision_time:
        raise ValueError("boundary proxy interval_end must equal decision_time")
    if method not in ("linear", "pchip"):
        raise ValueError(f"unsupported resampling method: {method!r}")

    hourly_x = np.arange(HOURS_PER_FORECAST + 1, dtype=float)
    hourly_values = np.array(
        [
            float(boundary_proxy.mean_power_kw),
            *(float(point.power_kw) for point in hourly_points),
        ],
        dtype=float,
    )
    ten_minute_x = np.arange(1, TEN_MINUTE_POINTS + 1, dtype=float) / (60 // STEP_MINUTES)
    if method == "linear":
        resampled_kw = np.interp(ten_minute_x, hourly_x, hourly_values)
    else:
        resampled_kw = PchipInterpolator(hourly_x, hourly_values)(ten_minute_x)

    points = []
    for step, power_kw in enumerate(resampled_kw, start=1):
        # PCHIP is shape-preserving for these non-negative knots.  Reject a
        # material violation instead of silently changing an approved method.
        if power_kw < -1e-9:
            raise ValueError("resampling produced a negative PV forecast")
        clean_power_kw = max(0.0, float(power_kw))
        points.append(
            TenMinutePVPoint(
                slot_end=decision_time + dt.timedelta(minutes=STEP_MINUTES * step),
                power_kw=clean_power_kw,
                energy_kwh=TimeGrid.power_to_energy_kwh(clean_power_kw),
            )
        )
    return tuple(points)
