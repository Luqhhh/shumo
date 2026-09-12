from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.contracts import InfoItem, InfoSet
from microgrid.problem.q3_pv_forecast import (
    PV_COMBINATION_MODEL_VERSION,
    combine_visible_pv_forecasts,
)
from microgrid.schemas import InputError


def _forecast_item(issue_time: dt.datetime, lead: int, value: float) -> InfoItem:
    return InfoItem(
        "pv_forecast_kw",
        available_at=issue_time,
        valid_time=issue_time + dt.timedelta(hours=lead),
        value=value,
        source_ref=f"forecast:{issue_time.isoformat()}:lead={lead}",
    )


def _actual_item(valid_time: dt.datetime, value: float) -> InfoItem:
    return InfoItem(
        "pv_actual_kw",
        available_at=valid_time,
        valid_time=valid_time,
        value=value,
        source_ref=f"actual:{valid_time.isoformat()}",
    )


def _complete_combination_info(decision_time: dt.datetime) -> InfoSet:
    items: list[InfoItem] = []

    # One realized historical error for every lead: MAE_h = h kW.
    history_issue = decision_time - dt.timedelta(hours=48)
    for lead in range(1, 25):
        valid_time = history_issue + dt.timedelta(hours=lead)
        items.append(_forecast_item(history_issue, lead, 100.0 + lead))
        items.append(_actual_item(valid_time, 100.0))

    # Current publication supplies all next-24-hour targets.
    for lead in range(1, 25):
        items.append(_forecast_item(decision_time, lead, 100.0 + lead))

    # The preceding 00:00 publication overlaps the first 18 targets.
    previous_issue = decision_time - dt.timedelta(hours=6)
    for lead in range(7, 25):
        items.append(_forecast_item(previous_issue, lead, 200.0 + (lead - 6)))

    return InfoSet.from_raw(decision_time, tuple(items))


def test_approved_pv_combination_uses_all_visible_vintages_and_inverse_mae_squared() -> None:
    decision_time = dt.datetime(2025, 2, 1, 6, 0)
    combined = combine_visible_pv_forecasts(_complete_combination_info(decision_time))

    assert combined.model_version == PV_COMBINATION_MODEL_VERSION
    assert combined.epsilon_kw == 1.0
    assert len(combined.points) == 24
    assert combined.points[0].valid_time == decision_time + dt.timedelta(hours=1)
    assert combined.points[-1].valid_time == decision_time + dt.timedelta(hours=24)

    first_rows = [
        row
        for row in combined.contributions
        if row.valid_time == decision_time + dt.timedelta(hours=1)
    ]
    assert [row.lead_hours for row in first_rows] == [1, 7]
    assert [row.historical_mae_kw for row in first_rows] == pytest.approx([1.0, 7.0])
    assert [row.normalized_weight for row in first_rows] == pytest.approx([16 / 17, 1 / 17])
    assert combined.points[0].power_kw == pytest.approx((16 * 101.0 + 201.0) / 17)

    last_rows = [
        row for row in combined.contributions if row.valid_time == combined.points[-1].valid_time
    ]
    assert len(last_rows) == 1
    assert last_rows[0].normalized_weight == pytest.approx(1.0)


def test_pv_combination_cannot_see_future_actuals() -> None:
    decision_time = dt.datetime(2025, 2, 1, 12, 0)
    base_info = _complete_combination_info(decision_time)
    future_actual = _actual_item(decision_time + dt.timedelta(hours=1), 99_999.0)
    with_future_raw = InfoSet.from_raw(
        decision_time,
        (*base_info.visible_items, future_actual),
    )

    assert future_actual not in with_future_raw.visible_items
    assert combine_visible_pv_forecasts(base_info) == combine_visible_pv_forecasts(with_future_raw)


def test_pv_combination_fails_without_realized_error_history() -> None:
    decision_time = dt.datetime(2025, 2, 1, 0, 0)
    items = tuple(_forecast_item(decision_time, lead, 100.0) for lead in range(1, 25))
    items += (_actual_item(decision_time, 0.0),)

    with pytest.raises(InputError, match="no realized causal error history"):
        combine_visible_pv_forecasts(InfoSet.from_raw(decision_time, items))


def test_pv_combination_rejects_incomplete_realized_history() -> None:
    decision_time = dt.datetime(2025, 2, 1, 6, 0)
    info = _complete_combination_info(decision_time)
    missing_time = decision_time - dt.timedelta(hours=47)
    incomplete = tuple(
        item
        for item in info.visible_items
        if not (item.kind == "pv_actual_kw" and item.valid_time == missing_time)
    )

    with pytest.raises(InputError, match="missing realized actual PV"):
        combine_visible_pv_forecasts(InfoSet.from_raw(decision_time, incomplete))


@pytest.mark.parametrize(
    "decision_time", [dt.datetime(2025, 2, 1, 6, 10), dt.datetime(2025, 2, 1, 7, 0)]
)
def test_pv_combination_is_only_created_at_approved_release_times(
    decision_time: dt.datetime,
) -> None:
    with pytest.raises(InputError, match="only created"):
        combine_visible_pv_forecasts(InfoSet(decision_time=decision_time))
