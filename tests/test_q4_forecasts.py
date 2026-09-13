from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.contracts import InfoItem, InfoSet
from microgrid.problem.q4_common import ACTION_START, STEP, YEAR_END, YEAR_START, Q4Error
from microgrid.problem.q4_forecasts import LagAR, Q4Forecaster


def service_at(time, case="q4_2", future_value=1):
    service = Q4Forecaster(case, {"actual": "hash"})
    count = int((time - YEAR_START) / STEP)
    history = []
    for index in range(count + 144):
        end = YEAR_START + (index + 1) * STEP
        for kind, value in (
            ("load_actual_kw", 600 + index % 7),
            ("pv_actual_kw", 50 + index % 11),
            ("price_actual", 0.5 + index % 13 / 100),
        ):
            history.append(InfoItem(kind, end, end, value if end <= time else future_value))
    if case == "q4_3":
        issue = YEAR_START
        while issue <= time:
            for lead in range(1, 25):
                history.append(
                    InfoItem("pv_forecast_kw", issue, issue + dt.timedelta(hours=lead), 60 + lead)
                )
            issue += dt.timedelta(hours=6)
    service.ingest(InfoSet.from_raw(time, tuple(history)))
    return service


def test_future_actual_perturbation_does_not_change_forecast():
    a = service_at(ACTION_START, future_value=1000).refresh(InfoSet.from_raw(ACTION_START, ()))
    b = service_at(ACTION_START, future_value=999999).refresh(InfoSet.from_raw(ACTION_START, ()))
    assert a == b


def test_residual_starts_only_after_all_lags_and_does_not_span_gap():
    lag = LagAR((2, 4), (0.5, 0.5))
    for index, value in enumerate((1, 3, 2, 4)):
        lag.add(YEAR_START + (index + 1) * STEP, value)
    assert lag.latest_residual is None
    lag.add(YEAR_START + 5 * STEP, 10)
    assert lag.latest_residual == 8.5 and lag.pairs == 0
    lag.add(YEAR_START + 6 * STEP, 12)
    assert lag.pairs == 1
    with pytest.raises(Q4Error, match="missing_history"):
        lag.add(YEAR_START + 8 * STEP, 0)


def test_saved_snapshot_shrinks_without_refreshing():
    snapshot = service_at(ACTION_START).refresh(InfoSet.from_raw(ACTION_START, ()))
    tail = snapshot.sliced(ACTION_START + 35 * STEP)
    assert len(tail.slots) == 109 and tail.slots[-1] + STEP == ACTION_START + dt.timedelta(days=1)
    assert (
        tail.snapshot_id == snapshot.snapshot_id and tail.terminal_value == snapshot.terminal_value
    )


def test_official_weights_use_current_history_and_hit_hourly_knots():
    service = service_at(ACTION_START, "q4_3")
    snapshot = service.refresh(InfoSet.from_raw(ACTION_START, ()))
    assert all(service.errors[h][1] > 0 for h in range(1, 25))
    for hour, trace in enumerate(snapshot.traces["pv"], 1):
        assert snapshot.pv_kwh[hour * 6 - 1] * 6 == pytest.approx(trace["prediction_value"])
        assert sum(v["weight"] for v in trace["versions"]) == pytest.approx(1)
    service.errors[1][0] += 100000
    assert service.refresh(InfoSet.from_raw(ACTION_START, ())).pv_kwh != snapshot.pv_kwh


def test_year_end_only_needed_forecasts_and_zero_terminal_value():
    time = YEAR_END - dt.timedelta(hours=6)
    snapshot = service_at(time, "q4_3").refresh(InfoSet.from_raw(time, ()))
    assert len(snapshot.slots) == 36 and snapshot.slots[-1] + STEP == YEAR_END
    assert snapshot.terminal_value == 0


def test_missing_bucket_or_forecast_fails_explicitly():
    service = service_at(ACTION_START, "q4_3")
    service.errors[1] = [0, 0]
    with pytest.raises(Q4Error, match="missing_forecast_history"):
        service.refresh(InfoSet.from_raw(ACTION_START, ()))
    service = service_at(ACTION_START, "q4_3")
    service.official.pop(ACTION_START + dt.timedelta(hours=24))
    with pytest.raises(Q4Error, match="missing_forecast"):
        service.refresh(InfoSet.from_raw(ACTION_START, ()))
