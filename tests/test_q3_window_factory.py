from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.contracts import BatteryState, InfoItem, InfoSet
from microgrid.problem.q2_inputs import FixedPricePoint
from microgrid.problem.q3_load_forecast import (
    LOAD_FORECAST_MODEL_VERSION,
    LOAD_LAG_WEIGHTS,
    LoadForecastPoint,
)
from microgrid.problem.q3_load_snapshot import LoadForecastSnapshot
from microgrid.problem.q3_plan_ledger import Q3PlanLedger
from microgrid.problem.q3_pv_forecast import (
    PV_COMBINATION_MODEL_VERSION,
    CombinedPVForecast,
    PVVersionContribution,
)
from microgrid.problem.q3_pv_snapshot import (
    TAIL_FALLBACK_REASON,
    TailForecast,
    TailForecastPoint,
    create_pv_forecast_snapshot,
)
from microgrid.problem.q3_resampling import CombinedHourlyPVPoint, PVBoundaryProxy
from microgrid.problem.q3_window_factory import Q3SnapshotWindowFactory
from microgrid.schemas import InputError


def _load_snapshot(issue: dt.datetime) -> LoadForecastSnapshot:
    return LoadForecastSnapshot(
        issue_time=issue,
        points=tuple(
            LoadForecastPoint(
                decision_time=issue,
                valid_time=issue + dt.timedelta(minutes=10 * step),
                available_at=issue,
                training_cutoff=issue,
                power_kw=60.0,
                energy_kwh=10.0,
                raw_power_kw=60.0,
                lag_values_kw=(60.0, 60.0, 60.0, 60.0),
                lag_weights=LOAD_LAG_WEIGHTS,
                ar1_phi=0.0,
                latest_visible_residual_kw=0.0,
                was_clipped=False,
                model_version=LOAD_FORECAST_MODEL_VERSION,
                data_version="synthetic-data-v1",
                source_refs=("lag7", "lag14", "lag21", "lag28"),
            )
            for step in range(1, 181)
        ),
    )


def _pv_snapshot(issue: dt.datetime):
    points = tuple(
        CombinedHourlyPVPoint(
            valid_time=issue + dt.timedelta(hours=lead),
            power_kw=30.0,
            source_ref=f"combined:{issue.isoformat()}:{lead}",
        )
        for lead in range(1, 25)
    )
    contributions = tuple(
        PVVersionContribution(
            issue_time=issue,
            valid_time=point.valid_time,
            lead_hours=lead,
            forecast_power_kw=30.0,
            historical_mae_kw=1.0,
            history_count=10,
            raw_weight=0.25,
            normalized_weight=1.0,
            source_ref=f"attachment3:{issue.isoformat()}:{lead}",
        )
        for lead, point in enumerate(points, start=1)
    )
    return create_pv_forecast_snapshot(
        CombinedPVForecast(
            decision_time=issue,
            epsilon_kw=1.0,
            points=points,
            contributions=contributions,
            model_version=PV_COMBINATION_MODEL_VERSION,
        ),
        boundary_proxy=PVBoundaryProxy(
            interval_end=issue,
            mean_power_kw=0.0,
            source_ref=f"actual-pv-boundary:{issue.isoformat()}",
        ),
    )


def _prices() -> tuple[FixedPricePoint, ...]:
    return tuple(
        FixedPricePoint(
            slot=slot,
            price_cny_per_kwh=1.0,
            source_ref=f"attachment1:price:{slot}",
        )
        for slot in range(144)
    )


def _ledger(day: dt.date) -> Q3PlanLedger:
    return Q3PlanLedger.start(
        day,
        initial_commitments_kwh=(5.0,) * 144,
        base_prices_cny_per_kwh=(1.0,) * 144,
    )


class _RecordingTail:
    def __init__(self) -> None:
        self.visible_times: tuple[dt.datetime, ...] = ()

    def predict(
        self,
        *,
        decision_time: dt.datetime,
        target_slot_ends: tuple[dt.datetime, ...],
        info_set: InfoSet,
    ) -> TailForecast:
        self.visible_times = tuple(item.valid_time for item in info_set.visible_items)
        return TailForecast(
            decision_time=decision_time,
            points=tuple(
                TailForecastPoint(
                    forecast_id=f"tail:{decision_time.isoformat()}:{valid.isoformat()}",
                    decision_time=decision_time,
                    valid_time=valid,
                    available_at=decision_time,
                    power_kw=12.0,
                    energy_kwh=2.0,
                    model_version="synthetic-tail",
                    training_cutoff=decision_time,
                    source_refs=("visible-history",),
                    fallback_reason=TAIL_FALLBACK_REASON,
                )
                for valid in target_slot_ends
            ),
        )


def _factory(day: dt.date, tail: _RecordingTail) -> Q3SnapshotWindowFactory:
    issues = tuple(dt.datetime.combine(day, dt.time(hour)) for hour in (0, 6, 12, 18))
    future = dt.datetime.combine(day, dt.time(6, 10))
    return Q3SnapshotWindowFactory(
        load_snapshots=tuple(_load_snapshot(issue) for issue in issues),
        pv_snapshots=tuple(_pv_snapshot(issue) for issue in issues),
        fixed_prices=_prices(),
        raw_info_items=(
            InfoItem(
                kind="pv_actual_kw",
                available_at=future,
                valid_time=future,
                value=999_999.0,
                source_ref="future-actual-must-be-hidden",
            ),
        ),
        tail_baseline=tail,
    )


def test_factory_uses_latest_release_and_records_only_uncovered_tail() -> None:
    day = dt.date(2025, 2, 1)
    tail = _RecordingTail()
    factory = _factory(day, tail)
    decision = dt.datetime.combine(day, dt.time(5, 50))

    window = factory(
        decision_time=decision,
        battery_state=BatteryState(6_000.0),
        current_ledger=_ledger(day),
    )

    assert window.load_snapshot_issue_time == dt.datetime.combine(day, dt.time())
    assert window.pv_snapshot_issue_time == dt.datetime.combine(day, dt.time())
    assert sum(point.pv_forecast_kind == "pv_attachment3" for point in window.points) == 109
    assert sum(point.pv_forecast_kind == "pv_tail" for point in window.points) == 35
    assert len(factory.used_tail_forecasts) == 1
    assert len(factory.used_tail_forecasts[0].points) == 35
    assert all(visible <= decision for visible in tail.visible_times)
    assert dt.datetime.combine(day, dt.time(6, 10)) not in tail.visible_times
    assert tuple(snapshot.issue_time.hour for snapshot in factory.used_load_snapshots) == (0,)


def test_factory_switches_to_0600_snapshot_without_recomputing_0510_version() -> None:
    day = dt.date(2025, 2, 1)
    factory = _factory(day, _RecordingTail())
    decision = dt.datetime.combine(day, dt.time(6))

    window = factory(
        decision_time=decision,
        battery_state=BatteryState(6_000.0),
        current_ledger=_ledger(day),
    )

    assert window.planning_event == "revision"
    assert window.load_snapshot_issue_time == decision
    assert window.pv_snapshot_issue_time == decision
    assert all(point.pv_forecast_kind == "pv_attachment3" for point in window.points)
    assert factory.used_tail_forecasts == ()


def test_factory_fails_when_the_required_release_snapshot_is_missing() -> None:
    day = dt.date(2025, 2, 1)
    midnight = dt.datetime.combine(day, dt.time())
    factory = Q3SnapshotWindowFactory(
        load_snapshots=(_load_snapshot(midnight),),
        pv_snapshots=(_pv_snapshot(midnight),),
        fixed_prices=_prices(),
        raw_info_items=(),
        tail_baseline=_RecordingTail(),
    )

    with pytest.raises(InputError, match="no immutable forecast snapshot"):
        factory(
            decision_time=dt.datetime.combine(day, dt.time(6)),
            battery_state=BatteryState(6_000.0),
            current_ledger=_ledger(day),
        )
