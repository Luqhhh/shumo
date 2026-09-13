from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.contracts import InfoItem
from microgrid.problem.q3_load_forecast import LOAD_FORECAST_MODEL_VERSION
from microgrid.problem.q3_pv_forecast import PV_COMBINATION_MODEL_VERSION
from microgrid.problem.q3_release_snapshots import (
    build_q3_release_snapshots,
    build_q3_snapshot_catalog,
)
from microgrid.schemas import InputError


def _actual(kind: str, valid: dt.datetime, value: float) -> InfoItem:
    return InfoItem(
        kind=kind,
        available_at=valid,
        valid_time=valid,
        value=value,
        source_ref=f"attachment2:{kind}:{valid.isoformat()}",
    )


def _forecast(issue: dt.datetime, lead: int, value: float) -> InfoItem:
    valid = issue + dt.timedelta(hours=lead)
    return InfoItem(
        kind="pv_forecast_kw",
        available_at=issue,
        valid_time=valid,
        value=value,
        source_ref=f"attachment3:{issue.isoformat()}:lead={lead}",
    )


def _raw_info(issue: dt.datetime, *, add_future_noise: bool = False) -> tuple[InfoItem, ...]:
    items: list[InfoItem] = []
    history_start = issue - dt.timedelta(days=35) + dt.timedelta(minutes=10)
    for index in range(35 * 24 * 6):
        valid = history_start + dt.timedelta(minutes=10 * index)
        items.append(_actual("load_actual_kw", valid, 600.0))

    pv_start = issue - dt.timedelta(days=8) + dt.timedelta(minutes=10)
    for index in range(8 * 24 * 6):
        valid = pv_start + dt.timedelta(minutes=10 * index)
        items.append(_actual("pv_actual_kw", valid, 120.0))

    historical_issue = issue - dt.timedelta(days=2)
    items.extend(_forecast(historical_issue, lead, 100.0) for lead in range(1, 25))
    items.extend(_forecast(issue, lead, 110.0) for lead in range(1, 25))
    if add_future_noise:
        future = issue + dt.timedelta(minutes=10)
        items.extend(
            (
                _actual("load_actual_kw", future, 999_999.0),
                _actual("pv_actual_kw", future, 999_999.0),
            )
        )
    return tuple(items)


def test_release_builds_both_approved_snapshots_from_one_causal_info_set() -> None:
    issue = dt.datetime(2025, 2, 5)
    result = build_q3_release_snapshots(
        issue_time=issue,
        raw_info_items=_raw_info(issue),
        load_data_version="attachment2-sha",
    )

    assert result.issue_time == issue
    assert len(result.load.points) == 180
    assert len(result.pv.points) == 144
    assert result.load.points[0].model_version == LOAD_FORECAST_MODEL_VERSION
    assert result.pv.combined_forecast.model_version == PV_COMBINATION_MODEL_VERSION
    assert result.pv.resampling_method == "linear"
    assert result.pv.boundary_proxy.interval_end == issue
    assert result.pv.boundary_proxy.mean_power_kw == pytest.approx(120.0)


def test_future_actual_noise_cannot_change_a_release_snapshot() -> None:
    issue = dt.datetime(2025, 2, 5)
    clean = build_q3_release_snapshots(
        issue_time=issue,
        raw_info_items=_raw_info(issue),
        load_data_version="attachment2-sha",
    )
    noisy = build_q3_release_snapshots(
        issue_time=issue,
        raw_info_items=_raw_info(issue, add_future_noise=True),
        load_data_version="attachment2-sha",
    )

    assert noisy == clean


def test_catalog_rejects_duplicate_or_unsorted_release_times() -> None:
    issue = dt.datetime(2025, 2, 5)
    with pytest.raises(InputError, match="unique and increasing"):
        build_q3_snapshot_catalog(
            issue_times=(issue, issue),
            raw_info_items=_raw_info(issue),
            load_data_version="attachment2-sha",
        )


def test_release_rejects_non_publication_time() -> None:
    issue = dt.datetime(2025, 2, 5, 0, 10)
    with pytest.raises(InputError, match="00:00/06:00/12:00/18:00"):
        build_q3_release_snapshots(
            issue_time=issue,
            raw_info_items=(),
            load_data_version="attachment2-sha",
        )
