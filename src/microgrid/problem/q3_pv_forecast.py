"""Approved causal Q3 PV forecast-vintage combination.

This module implements the ``VERSION-B + WEIGHT-B + HISTORY-A`` portion of
``D_MODEL_Q3``.  It consumes an already filtered :class:`InfoSet`; future
actuals and unissued forecasts therefore cannot be reached by the combiner.
Hourly-to-ten-minute conversion remains in the separately approved
``q3_resampling`` module.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from ..schemas import InputError
from .contracts import InfoItem, InfoSet
from .q3_resampling import CombinedHourlyPVPoint

PV_FORECAST_KIND = "pv_forecast_kw"
PV_ACTUAL_KIND = "pv_actual_kw"
Q3_ISSUE_HOURS = (0, 6, 12, 18)
PV_FORECAST_HOURS = 24
DEFAULT_WEIGHT_EPSILON_KW = 1.0
PV_COMBINATION_MODEL_VERSION = "VERSION-B+WEIGHT-B+HISTORY-A/v1"


def _finite_non_negative(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{name} must be numeric")
    clean = float(value)
    if not math.isfinite(clean) or clean < 0:
        raise InputError(f"{name} must be finite and non-negative")
    return clean


def _lead_hours(item: InfoItem) -> int:
    delta = item.valid_time - item.available_at
    seconds = delta.total_seconds()
    if seconds <= 0 or seconds % 3600 != 0:
        raise InputError("PV forecast lead must be a positive whole number of hours")
    lead = int(seconds // 3600)
    if not 1 <= lead <= PV_FORECAST_HOURS:
        raise InputError("PV forecast lead must be in 1..24 hours")
    return lead


@dataclass(frozen=True)
class PVVersionContribution:
    """One visible forecast vintage and its approved causal weight evidence."""

    issue_time: dt.datetime
    valid_time: dt.datetime
    lead_hours: int
    forecast_power_kw: float
    historical_mae_kw: float
    history_count: int
    raw_weight: float
    normalized_weight: float
    source_ref: str

    def __post_init__(self) -> None:
        if self.valid_time != self.issue_time + dt.timedelta(hours=self.lead_hours):
            raise ValueError("PV contribution lead and valid_time are inconsistent")
        if not 1 <= self.lead_hours <= PV_FORECAST_HOURS:
            raise ValueError("PV contribution lead must be in 1..24")
        for name in (
            "forecast_power_kw",
            "historical_mae_kw",
            "raw_weight",
            "normalized_weight",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.history_count <= 0:
            raise ValueError("history_count must be positive")


@dataclass(frozen=True)
class CombinedPVForecast:
    """Twenty-four combined hourly PV points plus auditable contributions."""

    decision_time: dt.datetime
    epsilon_kw: float
    points: tuple[CombinedHourlyPVPoint, ...]
    contributions: tuple[PVVersionContribution, ...]
    model_version: str = PV_COMBINATION_MODEL_VERSION

    def __post_init__(self) -> None:
        if not math.isfinite(self.epsilon_kw) or self.epsilon_kw <= 0:
            raise ValueError("epsilon_kw must be finite and positive")
        if len(self.points) != PV_FORECAST_HOURS:
            raise ValueError("combined PV forecast must contain 24 hourly points")
        for lead, point in enumerate(self.points, start=1):
            if point.valid_time != self.decision_time + dt.timedelta(hours=lead):
                raise ValueError("combined PV points must cover decision_time +1h through +24h")
            rows = tuple(row for row in self.contributions if row.valid_time == point.valid_time)
            if not rows:
                raise ValueError("each combined PV point must retain its vintage contributions")
            weight_sum = sum(row.normalized_weight for row in rows)
            if not math.isclose(weight_sum, 1.0, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError("PV contribution weights must sum to one for each valid_time")


def _visible_items(info: InfoSet, kind: str) -> tuple[InfoItem, ...]:
    return tuple(item for item in info.visible_items if item.kind == kind)


def combine_visible_pv_forecasts(
    info: InfoSet,
    *,
    epsilon_kw: float = DEFAULT_WEIGHT_EPSILON_KW,
) -> CombinedPVForecast:
    """Combine all visible vintages using approved lead-specific inverse MAE² weights.

    Historical errors use only forecasts whose valid time has already occurred
    and the matching actual-PV item visible in ``info``.  Missing candidates or
    causal history fail explicitly; this function never falls back to an
    unapproved predictor.
    """

    decision_time = info.decision_time
    if decision_time.time() not in tuple(dt.time(hour=hour) for hour in Q3_ISSUE_HOURS):
        raise InputError("Q3 PV combination is only created at 00:00/06:00/12:00/18:00")
    if not math.isfinite(epsilon_kw) or epsilon_kw <= 0:
        raise InputError("epsilon_kw must be finite and positive")

    forecasts = _visible_items(info, PV_FORECAST_KIND)
    actual_items = _visible_items(info, PV_ACTUAL_KIND)
    if not forecasts:
        raise InputError("InfoSet contains no visible PV forecasts")
    if not actual_items:
        raise InputError("InfoSet contains no visible actual PV history")

    actual_by_time: dict[dt.datetime, float] = {}
    for item in actual_items:
        if item.available_at != item.valid_time:
            raise InputError("actual PV must become available at its interval right endpoint")
        value = _finite_non_negative("actual PV", item.value)
        if item.valid_time in actual_by_time:
            raise InputError(f"duplicate actual PV at {item.valid_time}")
        actual_by_time[item.valid_time] = value

    forecast_rows: list[tuple[InfoItem, int, float]] = []
    for item in forecasts:
        if item.available_at.minute or item.available_at.second or item.available_at.microsecond:
            raise InputError("PV forecast issue_time must be on an exact hour")
        if item.available_at.hour not in Q3_ISSUE_HOURS:
            raise InputError("PV forecast issue_time must be 00:00/06:00/12:00/18:00")
        forecast_rows.append(
            (item, _lead_hours(item), _finite_non_negative("PV forecast", item.value))
        )

    errors_by_lead: dict[int, list[float]] = {lead: [] for lead in range(1, 25)}
    for item, lead, forecast_kw in forecast_rows:
        if item.valid_time > decision_time:
            continue
        actual_kw = actual_by_time.get(item.valid_time)
        if actual_kw is None:
            raise InputError(
                f"missing realized actual PV for forecast valid_time {item.valid_time}"
            )
        errors_by_lead[lead].append(abs(forecast_kw - actual_kw))

    points: list[CombinedHourlyPVPoint] = []
    contributions: list[PVVersionContribution] = []
    for target_lead in range(1, PV_FORECAST_HOURS + 1):
        valid_time = decision_time + dt.timedelta(hours=target_lead)
        candidates = [row for row in forecast_rows if row[0].valid_time == valid_time]
        if not candidates:
            raise InputError(f"no visible PV forecast for {valid_time}")

        raw_rows: list[tuple[InfoItem, int, float, float, int, float]] = []
        for item, lead, forecast_kw in candidates:
            history = errors_by_lead[lead]
            if not history:
                raise InputError(
                    f"no realized causal error history for PV lead {lead} at {decision_time}"
                )
            mae_kw = sum(history) / len(history)
            raw_weight = 1.0 / (mae_kw + epsilon_kw) ** 2
            raw_rows.append((item, lead, forecast_kw, mae_kw, len(history), raw_weight))

        total_weight = sum(row[5] for row in raw_rows)
        if not math.isfinite(total_weight) or total_weight <= 0:
            raise InputError(f"invalid PV weight total for {valid_time}")
        combined_kw = 0.0
        for item, lead, forecast_kw, mae_kw, count, raw_weight in raw_rows:
            normalized = raw_weight / total_weight
            combined_kw += normalized * forecast_kw
            contributions.append(
                PVVersionContribution(
                    issue_time=item.available_at,
                    valid_time=valid_time,
                    lead_hours=lead,
                    forecast_power_kw=forecast_kw,
                    historical_mae_kw=mae_kw,
                    history_count=count,
                    raw_weight=raw_weight,
                    normalized_weight=normalized,
                    source_ref=item.source_ref,
                )
            )
        points.append(
            CombinedHourlyPVPoint(
                valid_time=valid_time,
                power_kw=combined_kw,
                source_ref=(
                    f"{PV_COMBINATION_MODEL_VERSION};decision={decision_time.isoformat()};"
                    f"epsilon_kw={epsilon_kw:g}"
                ),
            )
        )

    return CombinedPVForecast(
        decision_time=decision_time,
        epsilon_kw=epsilon_kw,
        points=tuple(points),
        contributions=tuple(contributions),
    )
