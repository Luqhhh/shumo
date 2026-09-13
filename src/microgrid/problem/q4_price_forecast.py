"""Strictly causal same-slot price forecasts for Q4-2."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from dataclasses import dataclass

from ..schemas import InputError
from .q2_inputs import VariablePriceBundle, VariablePricePoint

PRICE_LAGS_DAYS = (7, 14, 21, 28)
PRICE_MODEL_VERSION = "q4-price-same-slot-ar1-v1"
MAX_AR1_PHI = 0.99
_STEP = dt.timedelta(minutes=10)


@dataclass(frozen=True)
class Q4PriceForecastPoint:
    valid_time: dt.datetime
    available_at: dt.datetime
    baseline_cny_per_kwh: float
    residual_adjustment_cny_per_kwh: float
    predicted_cny_per_kwh: float
    training_cutoff: dt.datetime
    ar1_phi: float
    model_version: str = PRICE_MODEL_VERSION
    data_version: str = ""


class Q4PriceForecastBuilder:
    """Incremental 7/14/21/28-day same-slot forecast with causal AR(1)."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[dt.date, int], VariablePricePoint] = {}
        self._latest_end: dt.datetime | None = None
        self._latest_residual: tuple[dt.datetime, float] | None = None
        self._residual_count = 0
        self._phi_numerator = 0.0
        self._phi_denominator = 0.0
        self._digest = hashlib.sha256()

    def _baseline(self, start: dt.datetime) -> float | None:
        slot = start.hour * 6 + start.minute // 10
        points = [
            self._by_key.get((start.date() - dt.timedelta(days=lag), slot))
            for lag in PRICE_LAGS_DAYS
        ]
        if any(point is None for point in points):
            return None
        return sum(point.price_cny_per_kwh for point in points if point is not None) / len(points)

    def add(self, point: VariablePricePoint) -> None:
        key = (point.day, point.slot)
        if key in self._by_key:
            raise ValueError(f"duplicate Q4 price history key {key}")
        if self._latest_end is not None and point.end <= self._latest_end:
            raise ValueError("Q4 price history must be added in increasing interval-end order")
        baseline = self._baseline(point.start)
        if baseline is not None:
            residual = point.price_cny_per_kwh - baseline
            if self._latest_residual is not None:
                previous = self._latest_residual[1]
                self._phi_numerator += residual * previous
                self._phi_denominator += previous * previous
            self._residual_count += 1
            self._latest_residual = (point.end, residual)
        self._by_key[key] = point
        self._latest_end = point.end
        self._digest.update(
            f"{point.day.isoformat()},{point.slot},{point.price_cny_per_kwh:.12g}\n".encode()
        )

    @property
    def training_cutoff(self) -> dt.datetime | None:
        return self._latest_end

    @property
    def ar1_phi(self) -> float:
        if self._residual_count < 2 or self._phi_denominator == 0:
            return 0.0
        estimate = self._phi_numerator / self._phi_denominator
        return max(-MAX_AR1_PHI, min(MAX_AR1_PHI, estimate))

    def build(
        self,
        *,
        decision_time: dt.datetime,
        horizon_start: dt.datetime,
        horizon_steps: int,
    ) -> tuple[Q4PriceForecastPoint, ...]:
        for name, value in (("decision_time", decision_time), ("horizon_start", horizon_start)):
            if (
                not isinstance(value, dt.datetime)
                or value.second
                or value.microsecond
                or value.minute % 10
            ):
                raise ValueError(f"{name} must be aligned to the ten-minute grid")
        if horizon_steps < 1:
            raise ValueError("horizon_steps must be positive")
        if horizon_start < decision_time:
            raise ValueError("horizon_start must not precede decision_time")
        if self._latest_end is not None and self._latest_end > decision_time:
            raise ValueError("Q4 price history contains data after decision_time")
        if self._latest_end is None:
            raise InputError("Q4 price forecast has no visible price history")

        phi = self.ar1_phi
        latest_residual = self._latest_residual
        digest = self._digest.copy().hexdigest()
        output: list[Q4PriceForecastPoint] = []
        for index in range(horizon_steps):
            start = horizon_start + index * _STEP
            baseline = self._baseline(start)
            if baseline is None:
                raise InputError(
                    f"Q4 price forecast missing 7/14/21/28-day lags for {start.isoformat()}"
                )
            valid_time = start + _STEP
            correction = 0.0
            if latest_residual is not None:
                elapsed_steps = max(1, math.ceil((valid_time - latest_residual[0]) / _STEP))
                correction = latest_residual[1] * phi**elapsed_steps
            predicted = max(0.0, baseline + correction)
            output.append(
                Q4PriceForecastPoint(
                    valid_time=valid_time,
                    available_at=decision_time,
                    baseline_cny_per_kwh=baseline,
                    residual_adjustment_cny_per_kwh=correction,
                    predicted_cny_per_kwh=predicted,
                    training_cutoff=self._latest_end,
                    ar1_phi=phi,
                    data_version=digest,
                )
            )
        return tuple(output)


def evaluate_causal_price_forecast(
    bundle: VariablePriceBundle,
    *,
    start_day: dt.date,
    end_day: dt.date,
    leads: tuple[int, ...] = (1, 6, 36, 72, 144),
) -> dict[str, object]:
    """Evaluate daily-ahead forecasts using only prices visible at each midnight.

    Both the bare 7/14/21/28 same-slot baseline and the AR(1)-adjusted forecast
    are scored on the same causal vintages, so the correction is credited or
    blamed on equal footing.  A price enters the training history only once its
    interval has ended, so no future price can leak into a vintage.
    """

    if end_day < start_day or any(lead < 1 or lead > 144 for lead in leads):
        raise ValueError("invalid Q4 price forecast evaluation range or lead")
    if len(set(leads)) != len(leads):
        raise ValueError(
            f"leads must be unique, duplicate leads would overwrite each other: {leads}"
        )
    prices = tuple(sorted(bundle.prices, key=lambda item: (item.day, item.slot)))
    actual = {(point.day, point.slot): point.price_cny_per_kwh for point in prices}
    builder = Q4PriceForecastBuilder()
    cursor = 0
    # One pass over the evaluation days: each midnight vintage is scored against
    # both the AR(1) forecast and its baseline.  This used to rebuild every
    # vintage a second time in a parallel loop purely to recover the baseline.
    errors: dict[int, list[tuple[float, float]]] = {lead: [] for lead in leads}
    baseline_errors: dict[int, list[tuple[float, float]]] = {lead: [] for lead in leads}
    for day_offset in range((end_day - start_day).days + 1):
        day = start_day + dt.timedelta(days=day_offset)
        decision_time = dt.datetime.combine(day, dt.time())
        while cursor < len(prices) and prices[cursor].end <= decision_time:
            builder.add(prices[cursor])
            cursor += 1
        forecast = builder.build(
            decision_time=decision_time,
            horizon_start=decision_time,
            horizon_steps=144,
        )
        for lead in leads:
            point = forecast[lead - 1]
            target_start = point.valid_time - _STEP
            target_key = (target_start.date(), target_start.hour * 6 + target_start.minute // 10)
            observed = actual.get(target_key)
            if observed is not None:
                errors[lead].append((observed, point.predicted_cny_per_kwh))
                baseline_errors[lead].append((observed, point.baseline_cny_per_kwh))

    def error_stats(pairs: list[tuple[float, float]]) -> tuple[float | None, float | None]:
        if not pairs:
            return None, None
        residuals = [observed - predicted for observed, predicted in pairs]
        mae = sum(map(abs, residuals)) / len(residuals)
        rmse = math.sqrt(sum(value * value for value in residuals) / len(residuals))
        return mae, rmse

    results: dict[str, object] = {}
    for lead in leads:
        forecast_mae, forecast_rmse = error_stats(errors[lead])
        baseline_mae, baseline_rmse = error_stats(baseline_errors[lead])
        results[f"{lead * 10}min"] = {
            "count": len(errors[lead]),
            "baseline_mae": baseline_mae,
            "forecast_mae": forecast_mae,
            "baseline_rmse": baseline_rmse,
            "forecast_rmse": forecast_rmse,
        }
    return {
        "model_version": PRICE_MODEL_VERSION,
        "training_policy": "all observed prices with interval_end <= decision_time",
        "evaluation_start": start_day.isoformat(),
        "evaluation_end": end_day.isoformat(),
        "metrics_by_lead": results,
    }
