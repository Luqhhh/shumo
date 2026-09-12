"""Causal lag-based forecast helpers for Q2.

The day-ahead contract is frozen at 00:00 for the whole day, so what matters is
a 10-minute-to-24-hour-ahead forecast built only from intervals that have
already ended.  Two series want different treatment on this data:

* **Load** carries a strong weekday effect.  The four most recent days that
  share the target's weekday (lags 7/14/21/28) predict it far better than the
  last few days do -- on the Q2 action period the same-weekday mean has an RMSE
  of 262 kW against 1128 kW for lag-1 -- but the level drifts slowly, so adding
  older weeks hurts.  The residual against that weekday mean is itself
  persistent over a few days, and correcting by its recent average takes the
  RMSE to 182 kW.
* **PV** is driven by weather, which is not weekly, so the last few days
  predict it much better: RMSE 285-305 kW for lags 1-5 against 447 kW for the
  same-weekday mean.

Every predictor here is strictly causal: the value for slot *s* of day *D* may
only use intervals that ended at or before the decision time.  Intervals are
always visible as a prefix of the ordered history, which is what lets
:class:`Q2ForecastBuilder` maintain its state incrementally.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass

from ..schemas import InputError
from .q2_inputs import ActualInterval

DEFAULT_LAGS = (7, 14, 21, 28)
_MAX_AR1_PHI = 0.99
_STEPS_PER_DAY = 144


def _slot_of(start: dt.datetime) -> int:
    return (start.hour * 60 + start.minute) // 10


@dataclass(frozen=True)
class ForecastConfig:
    """Base lag set, plus an optional per-series override and bias correction.

    ``lags``/``weights`` describe the load forecast.  ``pv_lags``/``pv_weights``
    override them for PV; leaving them ``None`` reuses the load specification,
    which is what every pre-existing caller relies on.
    """

    weights: tuple[float, ...] = (0.25, 0.25, 0.25, 0.25)
    ar1_phi: float | None = None
    model_version: str = "q2-weekly-ar1-v2"
    allow_short_history: bool = False
    lags: tuple[int, ...] = DEFAULT_LAGS
    pv_lags: tuple[int, ...] | None = None
    pv_weights: tuple[float, ...] | None = None
    load_bias_window: int = 0
    load_bias_scale: float = 0.0

    def __post_init__(self) -> None:
        _require_weights("weights", self.weights, self.lags)
        if self.pv_lags is not None or self.pv_weights is not None:
            pv_lags = self.pv_lags if self.pv_lags is not None else self.lags
            pv_weights = self.pv_weights if self.pv_weights is not None else self.weights
            _require_weights("pv_weights", pv_weights, pv_lags)
        if self.ar1_phi is not None and (
            isinstance(self.ar1_phi, bool)
            or not isinstance(self.ar1_phi, (int, float))
            or not math.isfinite(float(self.ar1_phi))
            or not -_MAX_AR1_PHI <= float(self.ar1_phi) <= _MAX_AR1_PHI
        ):
            raise ValueError("ar1_phi must be finite and in [-0.99, 0.99]")
        if not self.model_version:
            raise ValueError("model_version must not be empty")
        if isinstance(self.load_bias_window, bool) or self.load_bias_window < 0:
            raise ValueError("load_bias_window must be a non-negative integer")
        if not math.isfinite(float(self.load_bias_scale)):
            raise ValueError("load_bias_scale must be finite")
        if self.load_bias_window == 0 and float(self.load_bias_scale) != 0.0:
            raise ValueError("load_bias_scale is meaningless with load_bias_window == 0")


def _require_weights(name: str, weights: Sequence[float], lags: Sequence[int]) -> None:
    if len(weights) != len(lags):
        raise ValueError(f"{name} must contain one value per lag")
    if not lags or any(isinstance(lag, bool) or lag < 1 for lag in lags):
        raise ValueError("lags must be positive integers")
    if len(set(lags)) != len(lags):
        raise ValueError("lags must be distinct")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
        for value in weights
    ):
        raise ValueError(f"{name} must be finite and non-negative")
    if sum(float(value) for value in weights) <= 0:
        raise ValueError(f"{name} must not be all zero")


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


def _normalise_weights(weights: Sequence[float]) -> tuple[float, ...]:
    total = sum(float(value) for value in weights)
    return tuple(float(value) / total for value in weights)


def _lag_sum(
    *,
    target_start: dt.datetime,
    target_slot: int,
    by_key: dict[tuple[dt.date, int], ActualInterval],
    lags: Sequence[int],
    weights: Sequence[float],
    field: str,
    allow_short_history: bool,
) -> tuple[float | None, str]:
    """Weighted mean of this series over ``lags`` days back, at the same slot."""

    lag_items: list[ActualInterval | None] = [
        by_key.get((target_start.date() - dt.timedelta(days=days), target_slot)) for days in lags
    ]
    missing = [f"{days}d" for days, item in zip(lags, lag_items, strict=True) if item is None]
    if missing and not allow_short_history:
        raise InputError("missing weekly lags: " + ", ".join(missing))

    present = [
        (weight, item)
        for weight, item in zip(weights, lag_items, strict=True)
        if item is not None
    ]
    if not present:
        raise InputError("missing weekly lags: " + ", ".join(missing))
    weight_total = sum(weight for weight, _item in present)
    if weight_total <= 0:
        raise InputError("available lag weights are all zero")
    value = sum(weight * float(getattr(item, field)) for weight, item in present) / weight_total
    reason = "" if not missing else f"{field}: missing lags: " + ", ".join(missing)
    return value, reason


def _history_chunk(item: ActualInterval) -> str:
    return f"{item.day.isoformat()},{item.slot},{item.load_kw:.12g},{item.pv_kw:.12g}"


class Q2ForecastBuilder:
    """Incremental state for a history that only ever grows.

    Rebuilding the forecast from the full visible history on every decision is
    quadratic over a year, so a rolling run spends most of its time recomputing
    sort orders, the day/slot index, the weekly-lag residuals, the AR(1) sums
    and the SHA-256 data version that it already computed one window earlier.

    Feed intervals in ``(day, slot)`` order with :meth:`add` and call
    :meth:`build` once per decision time; each newly visible interval then costs
    O(1) amortised instead of O(len(history)).

    The equivalence with :func:`build_q2_forecast` rests on one property that
    ``ActualInterval.__post_init__`` already enforces: ``end`` is a strictly
    increasing function of ``(day, slot)``, so the visible history is always a
    *prefix* of the ordered history. That makes two things decidable at
    :meth:`add` time and permanent thereafter -- whether the interval is in the
    visible set, and whether all of its lags are visible (the lag keys sort
    strictly before the interval itself, so they are either already present or
    never will be).
    """

    def __init__(self, config: ForecastConfig) -> None:
        self._config = config
        self._lags = tuple(config.lags)
        self._weights = _normalise_weights(config.weights)
        self._pv_lags = tuple(config.pv_lags if config.pv_lags is not None else config.lags)
        self._pv_weights = _normalise_weights(
            config.pv_weights if config.pv_weights is not None else config.weights
        )
        self._by_key: dict[tuple[dt.date, int], ActualInterval] = {}
        self._training_cutoff: dt.datetime | None = None
        self._digest = hashlib.sha256()
        self._ar1_count = 0
        self._ar1_previous = 0.0
        self._ar1_numerator = 0.0
        self._ar1_denominator = 0.0
        self._latest_load_residual: tuple[dt.datetime, float] | None = None
        self._latest_pv_residual: tuple[dt.datetime, float] | None = None
        self._bias_cache: dict[dt.date, tuple[float, ...]] = {}

    def add(self, item: ActualInterval) -> None:
        """Absorb one newly visible interval."""

        key = (item.day, item.slot)
        if key in self._by_key:
            raise InputError("actual history contains duplicate day/slot keys")
        chunk = _history_chunk(item)
        # The digest must match hashing the "|"-joined payload in one shot;
        # sha256 streams the same byte sequence either way.
        self._digest.update((chunk if not self._by_key else "|" + chunk).encode("utf-8"))
        self._by_key[key] = item
        self._bias_cache.clear()

        if self._training_cutoff is None or item.end > self._training_cutoff:
            self._training_cutoff = item.end

        target_slot = _slot_of(item.start)
        self._absorb_residual(
            item,
            target_slot,
            self._lags,
            self._weights,
            "load_kw",
            "load",
        )
        self._absorb_residual(
            item,
            target_slot,
            self._pv_lags,
            self._pv_weights,
            "pv_kw",
            "pv",
        )

    def _absorb_residual(
        self,
        item: ActualInterval,
        target_slot: int,
        lags: Sequence[int],
        weights: Sequence[float],
        field: str,
        which: str,
    ) -> None:
        lag_keys = [
            (item.day - dt.timedelta(days=days), target_slot) for days in lags
        ]
        if any(lag_key not in self._by_key for lag_key in lag_keys):
            return
        baseline, _reason = _lag_sum(
            target_start=item.start,
            target_slot=target_slot,
            by_key=self._by_key,
            lags=lags,
            weights=weights,
            field=field,
            allow_short_history=True,
        )
        if baseline is None:
            return
        residual = float(getattr(item, field)) - baseline
        if which == "load":
            self._latest_load_residual = (item.end, residual)
            self._absorb_ar1(residual)
        else:
            self._latest_pv_residual = (item.end, residual)

    def _absorb_ar1(self, value: float) -> None:
        """Extend the running AR(1) sums used by :func:`estimate_ar1_phi`.

        The sums are accumulated in the same left-to-right order as the list
        comprehension they replace, so the floating-point result is bit-identical.
        """

        residual = float(value)
        if not math.isfinite(residual):
            return
        if self._ar1_count:
            self._ar1_denominator += self._ar1_previous * self._ar1_previous
            self._ar1_numerator += residual * self._ar1_previous
        self._ar1_previous = residual
        self._ar1_count += 1

    def _ar1_phi(self) -> float:
        if self._ar1_count < 2 or self._ar1_denominator == 0:
            return 0.0
        coefficient = self._ar1_numerator / self._ar1_denominator
        return max(-_MAX_AR1_PHI, min(_MAX_AR1_PHI, coefficient))

    def _bias_row(self, decision_day: dt.date) -> tuple[float, ...]:
        """Mean recent load residual, by slot, of the weekday-mean forecast.

        The residual of the weekday-mean forecast is persistent over a few days
        even though the 24-hour-ahead AR(1) weight has decayed to nothing, so
        averaging it over the last ``load_bias_window`` *fully visible* days
        tracks a drifting level.  Only days that ended strictly before
        ``decision_day`` are used, so a mid-day decision cannot see a partial
        day through the residual, and the row is the same for every slot of the
        horizon being planned.
        """

        cached = self._bias_cache.get(decision_day)
        if cached is not None:
            return cached
        window = int(self._config.load_bias_window)
        zeros = (0.0,) * _STEPS_PER_DAY
        if window <= 0:
            return zeros

        totals = [0.0] * _STEPS_PER_DAY
        counts = [0] * _STEPS_PER_DAY
        for lag in range(1, window + 1):
            past_day = decision_day - dt.timedelta(days=lag)
            for slot in range(_STEPS_PER_DAY):
                item = self._by_key.get((past_day, slot))
                if item is None:
                    continue
                baseline, _reason = _lag_sum(
                    target_start=item.start,
                    target_slot=slot,
                    by_key=self._by_key,
                    lags=self._lags,
                    weights=self._weights,
                    field="load_kw",
                    allow_short_history=True,
                )
                if baseline is None:
                    continue
                totals[slot] += item.load_kw - baseline
                counts[slot] += 1
        row = tuple(
            (totals[slot] / counts[slot]) if counts[slot] else 0.0
            for slot in range(_STEPS_PER_DAY)
        )
        self._bias_cache[decision_day] = row
        return row

    def build(
        self,
        decision_time: dt.datetime,
        horizon_start: dt.datetime,
        horizon_steps: int = 144,
    ) -> tuple[ForecastPoint, ...]:
        """Build the strictly causal forecast for ten-minute horizon intervals."""

        _validate_datetime("decision_time", decision_time)
        _validate_datetime("horizon_start", horizon_start)
        if horizon_steps <= 0:
            raise ValueError("horizon_steps must be positive")
        if not self._by_key:
            raise InputError("no actual history is visible at decision_time")

        config = self._config
        phi = float(config.ar1_phi) if config.ar1_phi is not None else self._ar1_phi()
        bias_scale = float(config.load_bias_scale)
        training_cutoff = self._training_cutoff
        data_version = "visible-" + self._digest.hexdigest()[:16]
        latest_load_residual = self._latest_load_residual
        latest_pv_residual = self._latest_pv_residual
        decision_day = decision_time.date()

        points: list[ForecastPoint] = []
        for step in range(horizon_steps):
            target_start = horizon_start + dt.timedelta(minutes=10 * step)
            target_slot = _slot_of(target_start)
            load, load_reason = _lag_sum(
                target_start=target_start,
                target_slot=target_slot,
                by_key=self._by_key,
                lags=self._lags,
                weights=self._weights,
                field="load_kw",
                allow_short_history=config.allow_short_history,
            )
            pv, pv_reason = _lag_sum(
                target_start=target_start,
                target_slot=target_slot,
                by_key=self._by_key,
                lags=self._pv_lags,
                weights=self._pv_weights,
                field="pv_kw",
                allow_short_history=config.allow_short_history,
            )
            if latest_load_residual is not None:
                load += latest_load_residual[1] * (
                    phi ** _age_steps(target_start, latest_load_residual[0])
                )
            if latest_pv_residual is not None:
                pv += latest_pv_residual[1] * (
                    phi ** _age_steps(target_start, latest_pv_residual[0])
                )
            if bias_scale != 0.0:
                bias_row = self._bias_row(decision_day)
                load += bias_scale * bias_row[target_slot]
            reason = "; ".join(part for part in (load_reason, pv_reason) if part)
            points.append(
                ForecastPoint(
                    valid_time=target_start + dt.timedelta(minutes=10),
                    available_at=decision_time,
                    load_kw=max(0.0, load),
                    pv_kw=max(0.0, pv),
                    training_cutoff=training_cutoff,
                    model_version=config.model_version,
                    data_version=data_version,
                    fallback_reason=reason,
                )
            )
        return tuple(points)


def _age_steps(target_start: dt.datetime, residual_end: dt.datetime) -> int:
    return max(0, int((target_start + dt.timedelta(minutes=10) - residual_end).total_seconds() // 600))


def build_q2_forecast(
    actuals: Sequence[ActualInterval],
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

    ordered = sorted(actuals, key=lambda item: (item.day, item.slot))
    builder = Q2ForecastBuilder(config)
    for item in ordered:
        if item.end <= decision_time:
            builder.add(item)
    return builder.build(
        decision_time=decision_time,
        horizon_start=horizon_start,
        horizon_steps=horizon_steps,
    )
