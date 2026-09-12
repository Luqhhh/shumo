from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.contracts import BatteryState
from microgrid.problem.q2_inputs import FixedPricePoint
from microgrid.problem.q3_load_forecast import LOAD_FORECAST_MODEL_VERSION, LoadForecastPoint
from microgrid.problem.q3_load_snapshot import Q3LoadWindow
from microgrid.problem.q3_plan_ledger import Q3PlanLedger
from microgrid.problem.q3_pv_snapshot import Q3PVWindow, Q3PVWindowPoint
from microgrid.problem.q3_window import build_q3_window_input
from microgrid.schemas import InputError


def _latest_release(decision: dt.datetime) -> dt.datetime:
    hour = max(value for value in (0, 6, 12, 18) if value <= decision.hour)
    return dt.datetime.combine(decision.date(), dt.time(hour))


def _forecast_windows(
    decision: dt.datetime,
    *,
    steps: int = 144,
    snapshot_issue: dt.datetime | None = None,
) -> tuple[Q3LoadWindow, Q3PVWindow]:
    issue = snapshot_issue or _latest_release(decision)
    load_points: list[LoadForecastPoint] = []
    pv_points: list[Q3PVWindowPoint] = []
    for step in range(1, steps + 1):
        valid = decision + dt.timedelta(minutes=10 * step)
        load_points.append(
            LoadForecastPoint(
                decision_time=issue,
                valid_time=valid,
                available_at=issue,
                training_cutoff=issue,
                power_kw=600.0,
                energy_kwh=100.0,
                raw_power_kw=600.0,
                lag_values_kw=(600.0, 600.0, 600.0, 600.0),
                lag_weights=(8 / 15, 4 / 15, 2 / 15, 1 / 15),
                ar1_phi=0.0,
                latest_visible_residual_kw=0.0,
                was_clipped=False,
                model_version=LOAD_FORECAST_MODEL_VERSION,
                data_version="attachment2-sha",
                source_refs=("lag7", "lag14", "lag21", "lag28"),
            )
        )
        pv_points.append(
            Q3PVWindowPoint(
                forecast_id=f"pv:{issue.isoformat()}:{valid.isoformat()}",
                kind="pv_attachment3",
                valid_time=valid,
                power_kw=300.0,
                energy_kwh=50.0,
            )
        )
    return (
        Q3LoadWindow(
            decision_time=decision,
            snapshot_issue_time=issue,
            points=tuple(load_points),
        ),
        Q3PVWindow(
            decision_time=decision,
            snapshot_issue_time=issue,
            points=tuple(pv_points),
            attachment3_point_count=steps,
            tail_point_count=0,
        ),
    )


def _prices() -> tuple[FixedPricePoint, ...]:
    return tuple(
        FixedPricePoint(
            slot=slot,
            price_cny_per_kwh=1.0 + slot / 1_000,
            source_ref=f"attachment1:price:{slot}",
        )
        for slot in range(144)
    )


def _ledger_before_revision(day: dt.date, hour: int) -> Q3PlanLedger:
    ledger = Q3PlanLedger.start(
        day,
        initial_commitments_kwh=(10.0,) * 144,
        base_prices_cny_per_kwh=tuple(point.price_cny_per_kwh for point in _prices()),
    )
    for revision_hour in (6, 12, 18):
        if revision_hour >= hour:
            break
        commitments = list(ledger.current.committed_kwh)
        for slot in range(revision_hour * 6, 144):
            commitments[slot] = 10.0 + revision_hour
        ledger = ledger.revise(
            dt.datetime.combine(day, dt.time(revision_hour)),
            new_commitments_kwh=tuple(commitments),
            transaction_price_cny_per_kwh=2.0,
        )
    return ledger


def _build(
    decision: dt.datetime,
    *,
    ledger: Q3PlanLedger | None,
    steps: int = 144,
    evaluation_end: dt.datetime | None = None,
    snapshot_issue: dt.datetime | None = None,
):
    load, pv = _forecast_windows(decision, steps=steps, snapshot_issue=snapshot_issue)
    return build_q3_window_input(
        decision_time=decision,
        battery_state=BatteryState(6_000.0),
        load_window=load,
        pv_window=pv,
        fixed_prices=_prices(),
        current_ledger=ledger,
        evaluation_end=evaluation_end,
    )


def test_midnight_builds_144_new_commitments_with_periodic_prices() -> None:
    decision = dt.datetime(2025, 2, 1)
    window = _build(decision, ledger=None)

    assert window.planning_event == "base_plan"
    assert window.previous_plan_version is None
    assert window.transaction_price_cny_per_kwh is None
    assert len(window.points) == 144
    assert {point.purchase_mode for point in window.points} == {"new_commitment"}
    assert window.points[0].slot == 0
    assert window.points[-1].slot == 143
    assert window.points[0].load_kwh == pytest.approx(100.0)
    assert window.points[0].pv_kwh == pytest.approx(50.0)
    assert window.terminal_mode == "terminal_value"
    assert window.terminal_value_cny_per_kwh == pytest.approx(
        0.9 * sum(point.price_cny_per_kwh for point in _prices()) / 144
    )


def test_0600_revision_separates_adjustable_today_from_tomorrow_lookahead() -> None:
    decision = dt.datetime(2025, 2, 1, 6)
    ledger = _ledger_before_revision(decision.date(), 6)
    window = _build(decision, ledger=ledger)

    assert window.planning_event == "revision"
    assert window.previous_plan_version == 0
    assert window.transaction_price_cny_per_kwh == pytest.approx(_prices()[36].price_cny_per_kwh)
    assert sum(point.purchase_mode == "adjustable_commitment" for point in window.points) == 108
    assert sum(point.purchase_mode == "lookahead_only" for point in window.points) == 36
    assert all(
        point.previous_committed_kwh == pytest.approx(10.0)
        for point in window.points
        if point.purchase_mode == "adjustable_commitment"
    )


def test_intermediate_mpc_fixes_today_and_requires_latest_confirmed_plan() -> None:
    decision = dt.datetime(2025, 2, 1, 11, 50)
    current = _ledger_before_revision(decision.date(), 12)
    window = _build(decision, ledger=current)

    assert window.planning_event == "dispatch_only"
    assert window.previous_plan_version == 1
    assert window.transaction_price_cny_per_kwh is None
    assert sum(point.purchase_mode == "fixed_commitment" for point in window.points) == 73
    assert sum(point.purchase_mode == "lookahead_only" for point in window.points) == 71

    stale = _ledger_before_revision(decision.date(), 6)
    with pytest.raises(InputError, match="latest version"):
        _build(decision, ledger=stale)


def test_future_forecast_snapshot_cannot_even_form_a_window() -> None:
    decision = dt.datetime(2025, 2, 1, 11, 50)
    future_issue = dt.datetime(2025, 2, 1, 12)

    with pytest.raises(ValueError, match="availability/training/valid"):
        _build(
            decision,
            ledger=_ledger_before_revision(decision.date(), 12),
            snapshot_issue=future_issue,
        )


def test_year_end_window_uses_hard_6000_target_without_terminal_value() -> None:
    decision = dt.datetime(2025, 12, 31, 18)
    evaluation_end = dt.datetime(2026, 1, 1)
    window = _build(
        decision,
        ledger=_ledger_before_revision(decision.date(), 18),
        steps=36,
        evaluation_end=evaluation_end,
    )

    assert len(window.points) == 36
    assert window.points[-1].interval_end == evaluation_end
    assert window.terminal_mode == "year_end_equality"
    assert window.terminal_target_kwh == 6_000.0
    assert window.terminal_value_cny_per_kwh == 0.0


def test_untruncated_forecast_windows_fail_closed_at_year_end() -> None:
    decision = dt.datetime(2025, 12, 31, 18)
    with pytest.raises(InputError, match="evaluation-end horizon"):
        _build(
            decision,
            ledger=_ledger_before_revision(decision.date(), 18),
            evaluation_end=dt.datetime(2026, 1, 1),
        )
