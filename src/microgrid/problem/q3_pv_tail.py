"""Approved causal PV tail baseline for Q3 HORIZON-B windows.

``TAIL-EXP2`` predicts only the suffix no longer covered by the latest
attachment-3 snapshot.  It uses the previous 1--7 days at the same ten-minute
slot with a fixed two-day recency half-life.  Missing history is an error; this
module never silently changes to another forecasting method.
"""

from __future__ import annotations

import datetime as dt
import math

from ..schemas import InputError
from .contracts import STEP_MINUTES, InfoSet, TimeGrid
from .q3_pv_forecast import PV_ACTUAL_KIND
from .q3_pv_snapshot import (
    TAIL_FALLBACK_REASON,
    TailForecast,
    TailForecastPoint,
)

TAIL_LAG_DAYS = (1, 2, 3, 4, 5, 6, 7)
TAIL_HALF_LIFE_DAYS = 2.0
_RAW_WEIGHTS = tuple(2 ** (-(day - 1) / TAIL_HALF_LIFE_DAYS) for day in TAIL_LAG_DAYS)
_WEIGHT_TOTAL = math.fsum(_RAW_WEIGHTS)
TAIL_EXP2_WEIGHTS = tuple(weight / _WEIGHT_TOTAL for weight in _RAW_WEIGHTS)
TAIL_EXP2_MODEL_VERSION = "TAIL-EXP2/v1"
_MAX_TARGET_HORIZON = dt.timedelta(hours=24)
_STEP = dt.timedelta(minutes=STEP_MINUTES)


def _require_ten_minute_time(name: str, value: dt.datetime) -> None:
    if value.second or value.microsecond or value.minute % STEP_MINUTES:
        raise InputError(f"{name} must be on a ten-minute boundary")


def _finite_non_negative(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{name} must be numeric")
    clean = float(value)
    if not math.isfinite(clean) or clean < 0:
        raise InputError(f"{name} must be finite and non-negative")
    return clean


def _visible_actual_history(info_set: InfoSet) -> dict[dt.datetime, tuple[float, str]]:
    history: dict[dt.datetime, tuple[float, str]] = {}
    for item in info_set.visible_items:
        if item.kind != PV_ACTUAL_KIND:
            continue
        _require_ten_minute_time("actual PV valid_time", item.valid_time)
        if item.available_at != item.valid_time:
            raise InputError("actual PV must become available at its interval right endpoint")
        if not item.source_ref:
            raise InputError("actual PV source_ref must be non-empty")
        if item.valid_time in history:
            raise InputError(f"duplicate actual PV at {item.valid_time}")
        history[item.valid_time] = (
            _finite_non_negative("actual PV", item.value),
            item.source_ref,
        )
    if not history:
        raise InputError("InfoSet contains no visible actual PV history")
    return history


class Exp2PVTailBaseline:
    """Production implementation of the approved ``TAIL-EXP2`` decision."""

    model_version = TAIL_EXP2_MODEL_VERSION
    lag_days = TAIL_LAG_DAYS
    weights = TAIL_EXP2_WEIGHTS

    def predict(
        self,
        *,
        decision_time: dt.datetime,
        target_slot_ends: tuple[dt.datetime, ...],
        info_set: InfoSet,
    ) -> TailForecast:
        _require_ten_minute_time("decision_time", decision_time)
        if info_set.decision_time != decision_time:
            raise InputError("tail InfoSet must match decision_time")
        if not target_slot_ends:
            raise InputError("tail target_slot_ends cannot be empty")
        if target_slot_ends != tuple(sorted(target_slot_ends)) or len(set(target_slot_ends)) != len(
            target_slot_ends
        ):
            raise InputError("tail targets must be unique and increasing")
        for previous, current in zip(target_slot_ends[:-1], target_slot_ends[1:], strict=True):
            if current - previous != _STEP:
                raise InputError("tail targets must form one continuous ten-minute suffix")

        history = _visible_actual_history(info_set)
        points: list[TailForecastPoint] = []
        for target in target_slot_ends:
            _require_ten_minute_time("tail target", target)
            if target <= decision_time or target - decision_time > _MAX_TARGET_HORIZON:
                raise InputError("tail target must be within decision+10min through decision+24h")

            lag_values: list[float] = []
            source_refs: list[str] = []
            for lag_days in TAIL_LAG_DAYS:
                lag_time = target - dt.timedelta(days=lag_days)
                if lag_time > decision_time:
                    raise InputError("TAIL-EXP2 would require future actual PV")
                row = history.get(lag_time)
                if row is None:
                    raise InputError(
                        f"missing {lag_days}-day actual PV lag for tail target {target}"
                    )
                lag_values.append(row[0])
                source_refs.append(row[1])

            power_kw = math.fsum(
                weight * value for weight, value in zip(TAIL_EXP2_WEIGHTS, lag_values, strict=True)
            )
            forecast_id = (
                f"pv_tail:{TAIL_EXP2_MODEL_VERSION}|decision={decision_time.isoformat()}"
                f"|valid={target.isoformat()}"
            )
            points.append(
                TailForecastPoint(
                    forecast_id=forecast_id,
                    decision_time=decision_time,
                    valid_time=target,
                    available_at=decision_time,
                    power_kw=power_kw,
                    energy_kwh=TimeGrid.power_to_energy_kwh(power_kw),
                    model_version=TAIL_EXP2_MODEL_VERSION,
                    training_cutoff=decision_time,
                    source_refs=tuple(source_refs),
                    fallback_reason=TAIL_FALLBACK_REASON,
                )
            )
        return TailForecast(decision_time=decision_time, points=tuple(points))
