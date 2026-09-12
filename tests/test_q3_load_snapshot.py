from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.contracts import InfoItem, InfoSet
from microgrid.problem.q3_load_forecast import LOAD_VERSION_HORIZON_STEPS
from microgrid.problem.q3_load_snapshot import (
    LOAD_MPC_WINDOW_STEPS,
    build_q3_load_window,
    create_load_forecast_snapshot,
)
from microgrid.schemas import InputError


def _load_item(valid_time: dt.datetime, value: float = 1_000.0) -> InfoItem:
    return InfoItem(
        kind="load_actual_kw",
        available_at=valid_time,
        valid_time=valid_time,
        value=value,
        source_ref=f"attachment2:{valid_time.isoformat()}",
    )


def _info(decision_time: dt.datetime, *, future_noise: bool = False) -> InfoSet:
    start = decision_time - dt.timedelta(days=35) + dt.timedelta(minutes=10)
    items = [_load_item(start + dt.timedelta(minutes=10 * index)) for index in range(35 * 24 * 6)]
    if future_noise:
        items.append(_load_item(decision_time + dt.timedelta(minutes=10), 999_999.0))
    return InfoSet.from_raw(decision_time, tuple(items))


def test_release_freezes_one_30_hour_load_version() -> None:
    issue_time = dt.datetime(2025, 2, 5, 6)
    snapshot = create_load_forecast_snapshot(_info(issue_time), data_version="attachment2-sha")

    assert snapshot.issue_time == issue_time
    assert len(snapshot.points) == LOAD_VERSION_HORIZON_STEPS == 180
    assert snapshot.points[0].valid_time == issue_time + dt.timedelta(minutes=10)
    assert snapshot.coverage_end == issue_time + dt.timedelta(hours=30)
    assert all(point.decision_time == issue_time for point in snapshot.points)
    assert all(point.training_cutoff == issue_time for point in snapshot.points)


def test_1150_mpc_slices_24_hours_from_0600_version_without_reforecast(monkeypatch) -> None:
    issue_time = dt.datetime(2025, 2, 5, 6)
    snapshot = create_load_forecast_snapshot(_info(issue_time), data_version="attachment2-sha")

    def forbidden_reforecast(*_args, **_kwargs):
        pytest.fail("intermediate ten-minute MPC must not rerun LOAD-A")

    monkeypatch.setattr(
        "microgrid.problem.q3_load_snapshot.forecast_q3_load",
        forbidden_reforecast,
    )
    decision_time = dt.datetime(2025, 2, 5, 11, 50)
    window = build_q3_load_window(decision_time=decision_time, snapshot=snapshot)

    assert len(window.points) == LOAD_MPC_WINDOW_STEPS == 144
    assert window.points[0].valid_time == dt.datetime(2025, 2, 5, 12)
    assert window.points[-1].valid_time == dt.datetime(2025, 2, 6, 11, 50)
    assert all(point.decision_time == issue_time for point in window.points)


def test_future_actual_cannot_change_frozen_load_version() -> None:
    issue_time = dt.datetime(2025, 2, 5, 12)
    clean = create_load_forecast_snapshot(_info(issue_time), data_version="v")
    with_future = create_load_forecast_snapshot(
        _info(issue_time, future_noise=True),
        data_version="v",
    )
    assert with_future == clean


def test_next_release_requires_a_new_load_snapshot() -> None:
    issue_time = dt.datetime(2025, 2, 5, 6)
    snapshot = create_load_forecast_snapshot(_info(issue_time), data_version="v")

    with pytest.raises(InputError, match="latest visible"):
        build_q3_load_window(
            decision_time=dt.datetime(2025, 2, 5, 12),
            snapshot=snapshot,
        )


def test_mpc_cannot_read_a_future_load_forecast_version() -> None:
    future_issue = dt.datetime(2025, 2, 5, 12)
    future_snapshot = create_load_forecast_snapshot(_info(future_issue), data_version="v")

    with pytest.raises(InputError, match="latest visible"):
        build_q3_load_window(
            decision_time=dt.datetime(2025, 2, 5, 11, 50),
            snapshot=future_snapshot,
        )


def test_year_end_load_window_is_truncated_without_reforecast() -> None:
    decision_time = dt.datetime(2025, 12, 31, 18)
    snapshot = create_load_forecast_snapshot(_info(decision_time), data_version="v")
    horizon_end = dt.datetime(2026, 1, 1)

    window = build_q3_load_window(
        decision_time=decision_time,
        snapshot=snapshot,
        horizon_end=horizon_end,
    )
    assert len(window.points) == 36
    assert window.points[-1].valid_time == horizon_end
