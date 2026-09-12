from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.contracts import InfoSet, TimeGrid
from microgrid.problem.q3_pv_forecast import (
    PV_COMBINATION_MODEL_VERSION,
    CombinedPVForecast,
    PVVersionContribution,
)
from microgrid.problem.q3_pv_snapshot import (
    TAIL_FALLBACK_REASON,
    PVForecastSnapshot,
    TailForecast,
    TailForecastPoint,
    build_q3_pv_window,
    create_pv_forecast_snapshot,
)
from microgrid.problem.q3_resampling import CombinedHourlyPVPoint, PVBoundaryProxy
from microgrid.schemas import InputError, PendingDecisionError


def _combined(issue_time: dt.datetime) -> CombinedPVForecast:
    points = tuple(
        CombinedHourlyPVPoint(
            valid_time=issue_time + dt.timedelta(hours=lead),
            power_kw=float(lead * 10),
            source_ref=f"combined:{lead}",
        )
        for lead in range(1, 25)
    )
    contributions = tuple(
        PVVersionContribution(
            issue_time=issue_time,
            valid_time=point.valid_time,
            lead_hours=lead,
            forecast_power_kw=point.power_kw,
            historical_mae_kw=float(lead),
            history_count=20,
            raw_weight=1.0 / (lead + 1) ** 2,
            normalized_weight=1.0,
            source_ref=f"attachment3:lead={lead}",
        )
        for lead, point in enumerate(points, start=1)
    )
    return CombinedPVForecast(
        decision_time=issue_time,
        epsilon_kw=1.0,
        points=points,
        contributions=contributions,
        model_version=PV_COMBINATION_MODEL_VERSION,
    )


def _snapshot(issue_time: dt.datetime) -> PVForecastSnapshot:
    return create_pv_forecast_snapshot(
        _combined(issue_time),
        boundary_proxy=PVBoundaryProxy(
            interval_end=issue_time,
            mean_power_kw=0.0,
            source_ref="actual-pv-boundary",
        ),
    )


class _RecordingTailBaseline:
    def __init__(self) -> None:
        self.requested: tuple[dt.datetime, ...] = ()

    def predict(
        self,
        *,
        decision_time: dt.datetime,
        target_slot_ends: tuple[dt.datetime, ...],
        info_set: InfoSet,
    ) -> TailForecast:
        assert info_set.decision_time == decision_time
        self.requested = target_slot_ends
        return TailForecast(
            decision_time=decision_time,
            points=tuple(
                TailForecastPoint(
                    forecast_id=(
                        f"pv_tail|decision={decision_time.isoformat()}|valid={valid.isoformat()}"
                    ),
                    decision_time=decision_time,
                    valid_time=valid,
                    available_at=decision_time,
                    power_kw=42.0,
                    energy_kwh=TimeGrid.power_to_energy_kwh(42.0),
                    model_version="synthetic-interface-double",
                    training_cutoff=decision_time,
                    source_refs=("synthetic-visible-history",),
                    fallback_reason=TAIL_FALLBACK_REASON,
                )
                for valid in target_slot_ends
            ),
        )


class _ForbiddenTailBaseline:
    def predict(self, **_kwargs) -> TailForecast:
        pytest.fail("tail baseline must not be called inside attachment-3 coverage")


def test_release_builds_one_immutable_144_point_snapshot() -> None:
    issue_time = dt.datetime(2025, 2, 1, 6)
    snapshot = _snapshot(issue_time)

    assert snapshot.issue_time == issue_time
    assert len(snapshot.points) == 144
    assert snapshot.points[0].valid_time == dt.datetime(2025, 2, 1, 6, 10)
    assert snapshot.coverage_end == dt.datetime(2025, 2, 2, 6)
    assert snapshot.points[5].power_kw == pytest.approx(10.0)
    assert len({point.forecast_id for point in snapshot.points}) == 144


def test_release_window_uses_snapshot_only() -> None:
    decision_time = dt.datetime(2025, 2, 1, 6)
    window = build_q3_pv_window(
        decision_time=decision_time,
        snapshot=_snapshot(decision_time),
        info_set=InfoSet(decision_time=decision_time),
        tail_baseline=_ForbiddenTailBaseline(),
    )

    assert window.attachment3_point_count == 144
    assert window.tail_point_count == 0


def test_year_end_truncation_does_not_request_points_after_horizon_end() -> None:
    decision_time = dt.datetime(2025, 12, 31, 18)
    horizon_end = dt.datetime(2026, 1, 1)
    window = build_q3_pv_window(
        decision_time=decision_time,
        snapshot=_snapshot(decision_time),
        info_set=InfoSet(decision_time=decision_time),
        tail_baseline=_ForbiddenTailBaseline(),
        horizon_end=horizon_end,
    )

    assert len(window.points) == 36
    assert window.points[-1].valid_time == horizon_end
    assert window.tail_point_count == 0


def test_1150_window_slices_snapshot_and_requests_only_the_5h50_tail(monkeypatch) -> None:
    issue_time = dt.datetime(2025, 2, 1, 6)
    snapshot = _snapshot(issue_time)

    def forbidden_resample(**_kwargs):
        pytest.fail("intermediate MPC must not reconstruct or resample attachment-3 forecasts")

    monkeypatch.setattr(
        "microgrid.problem.q3_pv_snapshot.resample_combined_hourly_pv",
        forbidden_resample,
    )
    decision_time = dt.datetime(2025, 2, 1, 11, 50)
    baseline = _RecordingTailBaseline()
    window = build_q3_pv_window(
        decision_time=decision_time,
        snapshot=snapshot,
        info_set=InfoSet(decision_time=decision_time),
        tail_baseline=baseline,
    )

    assert len(window.points) == 144
    assert window.attachment3_point_count == 109
    assert window.tail_point_count == 35
    assert window.points[0].valid_time == dt.datetime(2025, 2, 1, 12)
    assert window.points[108].valid_time == snapshot.coverage_end
    assert window.points[108].kind == "pv_attachment3"
    assert window.points[109].valid_time == dt.datetime(2025, 2, 2, 6, 10)
    assert window.points[109].kind == "pv_tail"
    assert window.points[-1].valid_time == dt.datetime(2025, 2, 2, 11, 50)
    assert baseline.requested[0] == dt.datetime(2025, 2, 2, 6, 10)
    assert baseline.requested[-1] == dt.datetime(2025, 2, 2, 11, 50)


def test_uncovered_tail_blocks_when_no_algorithm_has_been_approved() -> None:
    issue_time = dt.datetime(2025, 2, 1, 6)
    decision_time = dt.datetime(2025, 2, 1, 6, 10)

    with pytest.raises(PendingDecisionError) as excinfo:
        build_q3_pv_window(
            decision_time=decision_time,
            snapshot=_snapshot(issue_time),
            info_set=InfoSet(decision_time=decision_time),
        )

    assert excinfo.value.decision_ids == ["D-PV-TAIL-BASELINE"]


def test_window_rejects_an_old_snapshot_after_the_next_release() -> None:
    issue_time = dt.datetime(2025, 2, 1, 6)
    decision_time = dt.datetime(2025, 2, 1, 12)

    with pytest.raises(InputError, match="latest visible"):
        build_q3_pv_window(
            decision_time=decision_time,
            snapshot=_snapshot(issue_time),
            info_set=InfoSet(decision_time=decision_time),
            tail_baseline=_RecordingTailBaseline(),
        )


def test_non_release_time_cannot_form_attachment3_hourly_nodes() -> None:
    issue_time = dt.datetime(2025, 2, 1, 6, 10)

    with pytest.raises(ValueError, match="exact hour"):
        _snapshot(issue_time)


def test_tail_point_rejects_future_training_cutoff() -> None:
    decision_time = dt.datetime(2025, 2, 1, 6, 10)

    with pytest.raises(ValueError, match="after decision_time"):
        TailForecastPoint(
            forecast_id="future-training",
            decision_time=decision_time,
            valid_time=decision_time + dt.timedelta(minutes=10),
            available_at=decision_time,
            power_kw=1.0,
            energy_kwh=TimeGrid.power_to_energy_kwh(1.0),
            model_version="test",
            training_cutoff=decision_time + dt.timedelta(minutes=10),
            source_refs=("future",),
        )
