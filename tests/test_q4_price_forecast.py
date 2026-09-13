from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.q2_inputs import VariablePriceBundle, VariablePricePoint
from microgrid.problem.q4_price_forecast import (
    Q4PriceForecastBuilder,
    evaluate_causal_price_forecast,
)


def _price(day: dt.date, slot: int, value: float) -> VariablePricePoint:
    start = dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=slot * 10)
    return VariablePricePoint(
        day=day,
        slot=slot,
        start=start,
        end=start + dt.timedelta(minutes=10),
        price_cny_per_kwh=value,
        source_ref=f"{day}-{slot}",
    )


def test_price_forecast_is_causal_and_uses_same_slot_lags_plus_ar1() -> None:
    builder = Q4PriceForecastBuilder()
    start_day = dt.date(2025, 1, 1)
    for offset in range(31):
        day = start_day + dt.timedelta(days=offset)
        builder.add(_price(day, 0, float(offset)))

    decision_time = dt.datetime(2025, 2, 1)
    forecast = builder.build(
        decision_time=decision_time,
        horizon_start=decision_time,
        horizon_steps=1,
    )

    point = forecast[0]
    assert point.valid_time == decision_time + dt.timedelta(minutes=10)
    assert point.available_at == decision_time
    assert point.training_cutoff == dt.datetime(2025, 1, 31, 0, 10)
    assert point.baseline_cny_per_kwh == pytest.approx((24 + 17 + 10 + 3) / 4)
    assert point.predicted_cny_per_kwh > point.baseline_cny_per_kwh
    assert point.model_version == "q4-price-same-slot-ar1-v1"


def test_price_forecaster_rejects_history_not_yet_available() -> None:
    builder = Q4PriceForecastBuilder()
    decision_time = dt.datetime(2025, 2, 1)
    builder.add(_price(dt.date(2025, 2, 1), 0, 99.0))

    with pytest.raises(ValueError, match="after decision_time"):
        builder.build(
            decision_time=decision_time,
            horizon_start=decision_time,
            horizon_steps=1,
        )


def test_causal_backtest_rejects_duplicate_leads() -> None:
    with pytest.raises(ValueError, match="leads must be unique"):
        evaluate_causal_price_forecast(
            VariablePriceBundle(prices=(), input_hashes=()),
            start_day=dt.date(2025, 2, 1),
            end_day=dt.date(2025, 2, 1),
            leads=(1, 1),
        )
