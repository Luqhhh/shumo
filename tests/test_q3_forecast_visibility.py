from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.contracts import InfoItem, InfoSet


def test_q3_0600_info_set_exposes_only_information_available_by_decision_time():
    decision_time = dt.datetime(2025, 2, 1, 6, 0)

    forecast_0000 = InfoItem(
        "pv_forecast_kw",
        available_at=dt.datetime(2025, 2, 1, 0, 0),
        valid_time=dt.datetime(2025, 2, 1, 7, 0),
        value=100.0,
        source_ref="attachment3:2025-02-01T00:00:lead7",
    )
    forecast_0600 = InfoItem(
        "pv_forecast_kw",
        available_at=decision_time,
        valid_time=dt.datetime(2025, 2, 1, 7, 0),
        value=110.0,
        source_ref="attachment3:2025-02-01T06:00:lead1",
    )
    forecast_1200 = InfoItem(
        "pv_forecast_kw",
        available_at=dt.datetime(2025, 2, 1, 12, 0),
        valid_time=dt.datetime(2025, 2, 1, 13, 0),
        value=200.0,
        source_ref="attachment3:2025-02-01T12:00:lead1",
    )
    forecast_1800 = InfoItem(
        "pv_forecast_kw",
        available_at=dt.datetime(2025, 2, 1, 18, 0),
        valid_time=dt.datetime(2025, 2, 1, 19, 0),
        value=50.0,
        source_ref="attachment3:2025-02-01T18:00:lead1",
    )

    load_actual_through_0600 = InfoItem(
        "load_actual_kw",
        available_at=decision_time,
        valid_time=decision_time,
        value=3_000.0,
        source_ref="attachment2:interval-ending-2025-02-01T06:00",
    )
    load_actual_after_0600 = InfoItem(
        "load_actual_kw",
        available_at=dt.datetime(2025, 2, 1, 6, 10),
        valid_time=dt.datetime(2025, 2, 1, 6, 10),
        value=3_100.0,
        source_ref="attachment2:interval-ending-2025-02-01T06:10",
    )
    pv_actual_after_0600 = InfoItem(
        "pv_actual_kw",
        available_at=dt.datetime(2025, 2, 1, 6, 10),
        valid_time=dt.datetime(2025, 2, 1, 6, 10),
        value=20.0,
        source_ref="attachment2:interval-ending-2025-02-01T06:10",
    )

    raw_items = (
        forecast_0000,
        forecast_0600,
        forecast_1200,
        forecast_1800,
        load_actual_through_0600,
        load_actual_after_0600,
        pv_actual_after_0600,
    )
    info = InfoSet.from_raw(decision_time, raw_items)

    assert info.visible_items == (
        forecast_0000,
        forecast_0600,
        load_actual_through_0600,
    )
    assert info.is_visible(forecast_0600)
    assert not info.is_visible(forecast_1200)
    assert not info.is_visible(forecast_1800)
    assert not info.is_visible(load_actual_after_0600)
    assert not info.is_visible(pv_actual_after_0600)


@pytest.mark.parametrize("issue_hour", [0, 6, 12, 18])
def test_q3_visibility_at_all_four_issue_boundaries(issue_hour: int) -> None:
    day = dt.date(2025, 2, 2)
    decision_time = dt.datetime.combine(day, dt.time(hour=issue_hour))
    issue_times = tuple(dt.datetime.combine(day, dt.time(hour=hour)) for hour in (0, 6, 12, 18))
    forecasts = tuple(
        InfoItem(
            "pv_forecast_kw",
            available_at=issue_time,
            valid_time=issue_time + dt.timedelta(hours=1),
            value=float(issue_time.hour),
            source_ref=f"attachment3:{issue_time.isoformat()}",
        )
        for issue_time in issue_times
    )

    info = InfoSet.from_raw(decision_time, forecasts)

    assert tuple(item.available_at for item in info.visible_items) == tuple(
        issue_time for issue_time in issue_times if issue_time <= decision_time
    )


def test_actual_interval_is_visible_only_at_its_right_endpoint() -> None:
    interval_start = dt.datetime(2025, 2, 1, 6, 0)
    interval_end = interval_start + dt.timedelta(minutes=10)
    actual = InfoItem(
        "load_actual_kw",
        available_at=interval_end,
        valid_time=interval_end,
        value=3_000.0,
        source_ref="attachment2:interval-ending-2025-02-01T06:10",
    )

    assert InfoSet.from_raw(interval_start, (actual,)).visible_items == ()
    assert InfoSet.from_raw(interval_end, (actual,)).visible_items == (actual,)


def test_q3_visibility_across_midnight_uses_real_datetimes() -> None:
    decision_time = dt.datetime(2025, 2, 2, 0, 0)
    previous_interval = InfoItem(
        "pv_actual_kw",
        available_at=decision_time,
        valid_time=decision_time,
        value=20.0,
        source_ref="attachment2:interval-ending-2025-02-02T00:00",
    )
    next_interval = InfoItem(
        "pv_actual_kw",
        available_at=decision_time + dt.timedelta(minutes=10),
        valid_time=decision_time + dt.timedelta(minutes=10),
        value=30.0,
        source_ref="attachment2:interval-ending-2025-02-02T00:10",
    )

    info = InfoSet.from_raw(decision_time, (previous_interval, next_interval))

    assert info.visible_items == (previous_interval,)
