from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.contracts import InfoItem, InfoSet
from microgrid.problem.q3_load_forecast import (
    LOAD_FORECAST_MODEL_VERSION,
    LOAD_LAG_WEIGHTS,
    forecast_q3_load,
)
from microgrid.schemas import InputError


def _load_item(valid_time: dt.datetime, value: float = 1_000.0) -> InfoItem:
    return InfoItem(
        "load_actual_kw",
        available_at=valid_time,
        valid_time=valid_time,
        value=value,
        source_ref=f"attachment2:{valid_time.isoformat()}",
    )


def _constant_history(decision_time: dt.datetime) -> tuple[InfoItem, ...]:
    start = decision_time - dt.timedelta(days=35) + dt.timedelta(minutes=10)
    count = 35 * 24 * 6
    return tuple(_load_item(start + dt.timedelta(minutes=10 * index)) for index in range(count))


def test_approved_load_forecast_produces_24_hours_with_full_provenance() -> None:
    decision_time = dt.datetime(2025, 2, 5, 0, 0)
    info = InfoSet.from_raw(decision_time, _constant_history(decision_time))

    forecast = forecast_q3_load(info, data_version="attachment2-sha256")

    assert len(forecast) == 144
    assert forecast[0].valid_time == decision_time + dt.timedelta(minutes=10)
    assert forecast[-1].valid_time == decision_time + dt.timedelta(hours=24)
    assert all(point.power_kw == pytest.approx(1_000.0) for point in forecast)
    assert all(point.energy_kwh == pytest.approx(1_000.0 / 6.0) for point in forecast)
    assert all(point.ar1_phi == 0.0 for point in forecast)
    assert all(point.lag_weights == LOAD_LAG_WEIGHTS for point in forecast)
    assert all(point.model_version == LOAD_FORECAST_MODEL_VERSION for point in forecast)
    assert all(point.training_cutoff == decision_time for point in forecast)
    assert all(point.available_at == decision_time for point in forecast)
    assert all(point.data_version == "attachment2-sha256" for point in forecast)
    assert forecast[0].lag_values_kw == (1_000.0, 1_000.0, 1_000.0, 1_000.0)
    assert len(forecast[0].source_refs) == 4


def test_load_forecast_cannot_see_actual_after_decision_time() -> None:
    decision_time = dt.datetime(2025, 2, 5, 6, 0)
    history = _constant_history(decision_time)
    future = _load_item(decision_time + dt.timedelta(minutes=10), 99_999.0)
    base_info = InfoSet.from_raw(decision_time, history)
    future_filtered = InfoSet.from_raw(decision_time, (*history, future))

    assert future not in future_filtered.visible_items
    assert forecast_q3_load(base_info, data_version="v") == forecast_q3_load(
        future_filtered,
        data_version="v",
    )


def test_load_forecast_fits_the_approved_expanding_no_intercept_ar1() -> None:
    decision_time = dt.datetime(2025, 2, 5, 6, 0)
    history = list(_constant_history(decision_time))
    history[-2] = _load_item(decision_time - dt.timedelta(minutes=10), 1_100.0)
    history[-1] = _load_item(decision_time, 1_050.0)

    forecast = forecast_q3_load(
        InfoSet.from_raw(decision_time, tuple(history)),
        data_version="v",
        horizon_steps=2,
    )

    assert forecast[0].ar1_phi == pytest.approx(0.5)
    assert forecast[0].latest_visible_residual_kw == pytest.approx(50.0)
    assert forecast[0].raw_power_kw == pytest.approx(1_025.0)
    assert forecast[1].raw_power_kw == pytest.approx(1_012.5)


def test_load_forecast_rejects_a_gap_in_visible_history() -> None:
    decision_time = dt.datetime(2025, 2, 5, 12, 0)
    history = list(_constant_history(decision_time))
    del history[100]

    with pytest.raises(InputError, match="history gap"):
        forecast_q3_load(
            InfoSet.from_raw(decision_time, tuple(history)),
            data_version="v",
        )


def test_load_forecast_fails_when_four_week_history_is_unavailable() -> None:
    decision_time = dt.datetime(2025, 2, 5, 18, 0)
    history = _constant_history(decision_time)[-(27 * 24 * 6) :]

    with pytest.raises(InputError, match="not enough causal history"):
        forecast_q3_load(
            InfoSet.from_raw(decision_time, history),
            data_version="v",
        )


@pytest.mark.parametrize(
    "decision_time", [dt.datetime(2025, 2, 5, 6, 10), dt.datetime(2025, 2, 5, 7, 0)]
)
def test_load_forecast_is_only_updated_at_approved_release_times(
    decision_time: dt.datetime,
) -> None:
    with pytest.raises(InputError, match="only updated"):
        forecast_q3_load(
            InfoSet(decision_time=decision_time),
            data_version="v",
        )
