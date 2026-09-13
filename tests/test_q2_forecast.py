from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.q2_forecast import (
    ForecastConfig,
    build_q2_forecast,
    estimate_ar1_phi,
)
from microgrid.problem.q2_inputs import ActualInterval
from microgrid.schemas import InputError

DECISION_TIME = dt.datetime(2025, 2, 1)


def _actuals(*, future_value: float | None = None) -> tuple[ActualInterval, ...]:
    first_day = dt.date(2025, 1, 1)
    records: list[ActualInterval] = []
    for day_index in range(35):
        day = first_day + dt.timedelta(days=day_index)
        for slot in range(144):
            interval_start = dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=slot * 10)
            interval_end = interval_start + dt.timedelta(minutes=10)
            load = 100.0 + 10.0 * day_index + 0.5 * slot
            pv = 50.0 + 3.0 * day_index + 0.2 * slot
            if future_value is not None and day >= DECISION_TIME.date():
                load = future_value
                pv = future_value
            records.append(
                ActualInterval(
                    day=day,
                    slot=slot,
                    start=interval_start,
                    end=interval_end,
                    load_kw=load,
                    pv_kw=pv,
                    load_source_ref=f"actual-load-{day}-{slot}",
                    pv_source_ref=f"actual-pv-{day}-{slot}",
                )
            )
    return tuple(records)


def _config(**kwargs: object) -> ForecastConfig:
    values: dict[str, object] = {
        "weights": (0.25, 0.25, 0.25, 0.25),
        "ar1_phi": 0.0,
    }
    values.update(kwargs)
    return ForecastConfig(**values)


def test_forecast_uses_same_slot_from_four_weekly_lags() -> None:
    forecast = build_q2_forecast(
        _actuals(),
        decision_time=DECISION_TIME,
        horizon_start=DECISION_TIME,
        config=_config(),
    )

    assert len(forecast) == 144
    assert forecast[0].valid_time == dt.datetime(2025, 2, 1, 0, 10)
    assert forecast[0].load_kw == pytest.approx((340.0 + 270.0 + 200.0 + 130.0) / 4)
    assert forecast[0].pv_kw == pytest.approx((122.0 + 101.0 + 80.0 + 59.0) / 4)
    assert forecast[0].available_at == DECISION_TIME
    assert forecast[0].training_cutoff == DECISION_TIME
    assert forecast[0].fallback_reason == ""


def test_forecast_is_unchanged_when_future_actuals_are_perturbed() -> None:
    config = _config()
    baseline = build_q2_forecast(
        _actuals(),
        decision_time=DECISION_TIME,
        horizon_start=DECISION_TIME,
        config=config,
    )
    perturbed = build_q2_forecast(
        _actuals(future_value=999_999.0),
        decision_time=DECISION_TIME,
        horizon_start=DECISION_TIME,
        config=config,
    )

    assert perturbed == baseline


@pytest.mark.parametrize(
    "weights",
    [(-0.1, 0.4, 0.3, 0.4), (0.0, 0.0, 0.0, 0.0), (float("nan"), 1.0, 0.0, 0.0)],
)
def test_forecast_rejects_invalid_lag_weights(weights: tuple[float, ...]) -> None:
    with pytest.raises(ValueError, match="weights"):
        build_q2_forecast(
            _actuals(),
            decision_time=DECISION_TIME,
            horizon_start=DECISION_TIME,
            config=_config(weights=weights),
        )


def test_forecast_rejects_non_finite_ar1_phi() -> None:
    with pytest.raises(ValueError, match="ar1_phi"):
        build_q2_forecast(
            _actuals(),
            decision_time=DECISION_TIME,
            horizon_start=DECISION_TIME,
            config=_config(ar1_phi=float("inf")),
        )


def test_estimate_ar1_phi_is_bounded() -> None:
    assert -0.99 <= estimate_ar1_phi([-1000.0, 1000.0, -1000.0, 1000.0]) <= 0.99


def test_forecast_clips_negative_ar1_adjustment_to_zero() -> None:
    actuals = list(_actuals())
    visible_last_index = 31 * 144 - 1
    last = actuals[visible_last_index]
    actuals[visible_last_index] = ActualInterval(
        day=last.day,
        slot=last.slot,
        start=last.start,
        end=last.end,
        load_kw=0.0,
        pv_kw=last.pv_kw,
        load_source_ref=last.load_source_ref,
        pv_source_ref=last.pv_source_ref,
    )

    forecast = build_q2_forecast(
        tuple(actuals),
        decision_time=DECISION_TIME,
        horizon_start=DECISION_TIME,
        config=_config(ar1_phi=0.99),
    )

    assert forecast[0].load_kw == 0.0


def test_short_history_requires_explicit_fallback_mode() -> None:
    short_actuals = _actuals()[: 14 * 144]
    with pytest.raises(InputError, match="missing weekly lags"):
        build_q2_forecast(
            short_actuals,
            decision_time=dt.datetime(2025, 1, 15),
            horizon_start=dt.datetime(2025, 1, 15),
            config=_config(),
        )

    forecast = build_q2_forecast(
        short_actuals,
        decision_time=dt.datetime(2025, 1, 15),
        horizon_start=dt.datetime(2025, 1, 15),
        config=_config(allow_short_history=True),
    )
    assert forecast[0].fallback_reason
    assert "missing" in forecast[0].fallback_reason
