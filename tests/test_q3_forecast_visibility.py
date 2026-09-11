from __future__ import annotations

import datetime as dt

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
