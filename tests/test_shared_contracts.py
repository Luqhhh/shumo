"""Contract tests for the pre-division shared semantics.

Expectations in this file come from the team-provided D-TIME / D-EFF /
D-STATE / unit conventions.  They test semantic agreement, not model quality.
"""

from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.contracts import (
    CHARGE_EFFICIENCY,
    DISCHARGE_EFFICIENCY,
    INITIAL_SOC_KWH,
    MAX_BUS_ENERGY_KWH,
    ROUND_TRIP_EFFICIENCY,
    BatteryAction,
    BatteryState,
    CaseResult,
    CostBreakdown,
    InfoItem,
    InfoSet,
    IntervalResult,
    PurchasePlan,
    TimeGrid,
    apply_battery_action,
)


def test_time_grid_has_144_intervals_and_145_boundaries():
    grid = TimeGrid()
    day = dt.date(2025, 2, 1)
    intervals = grid.intervals_for_day(day)
    boundaries = grid.boundaries_for_day(day)
    assert len(intervals) == 144
    assert len(boundaries) == 145
    assert intervals[0].start == dt.datetime(2025, 2, 1, 0, 0)
    assert intervals[0].end == dt.datetime(2025, 2, 1, 0, 10)
    assert intervals[-1].start == dt.datetime(2025, 2, 1, 23, 50)
    assert intervals[-1].end == dt.datetime(2025, 2, 2, 0, 0)
    assert boundaries[0] == intervals[0].start
    assert boundaries[-1] == intervals[-1].end


def test_right_endpoint_alignment_matches_team_decision():
    grid = TimeGrid()
    day = dt.date(2025, 1, 1)
    first = grid.interval_from_right_endpoint(day, "0:10")
    assert first.index == 0
    assert first.start == dt.datetime(2025, 1, 1, 0, 0)
    assert first.end == dt.datetime(2025, 1, 1, 0, 10)

    second = grid.interval_from_right_endpoint(day, "0:20")
    assert second.index == 1
    assert second.start == dt.datetime(2025, 1, 1, 0, 10)
    assert second.end == dt.datetime(2025, 1, 1, 0, 20)

    last = grid.interval_from_right_endpoint(day, "0:00+1")
    assert last.index == 143
    assert last.day == day
    assert last.start == dt.datetime(2025, 1, 1, 23, 50)
    assert last.end == dt.datetime(2025, 1, 2, 0, 0)


def test_cross_midnight_is_continuous_not_modulo_24():
    grid = TimeGrid()
    jan31 = dt.date(2025, 1, 31)
    feb1 = dt.date(2025, 2, 1)
    assert grid.boundary(jan31, 144) == grid.boundary(feb1, 0)


def test_power_kw_to_interval_energy_kwh():
    assert TimeGrid.power_to_energy_kwh(600.0) == pytest.approx(100.0)
    assert TimeGrid.power_to_energy_kwh(0.0) == 0.0


def test_battery_state_soc_is_ratio_not_percent():
    state = BatteryState(INITIAL_SOC_KWH)
    assert state.soc == pytest.approx(0.5)
    with pytest.raises(ValueError):
        BatteryState(100.0)  # below 1200 kWh


def test_battery_action_rejects_negative_and_simultaneous_charge_discharge():
    with pytest.raises(ValueError):
        BatteryAction(charge_kwh=-1.0)
    with pytest.raises(ValueError):
        BatteryAction(charge_kwh=1.0, discharge_kwh=1.0)
    with pytest.raises(ValueError):
        BatteryAction(charge_kwh=MAX_BUS_ENERGY_KWH + 1e-9)


def test_battery_transition_numbers_match_team_decision():
    start = BatteryState(INITIAL_SOC_KWH)

    charged = apply_battery_action(start, BatteryAction(charge_kwh=100.0))
    assert charged.energy_kwh == pytest.approx(6_090.0)

    discharged = apply_battery_action(start, BatteryAction(discharge_kwh=100.0))
    assert discharged.energy_kwh == pytest.approx(6_000.0 - 100.0 / DISCHARGE_EFFICIENCY)
    assert discharged.energy_kwh == pytest.approx(5_888.888888888889)

    # Charge 100 kWh from the bus, later discharge to the original internal energy:
    # at most 100 * 0.9 * 0.9 = 81 kWh can be returned to the bus.
    restored = apply_battery_action(charged, BatteryAction(discharge_kwh=81.0))
    assert restored.energy_kwh == pytest.approx(6_000.0)
    assert ROUND_TRIP_EFFICIENCY == pytest.approx(0.81)
    assert CHARGE_EFFICIENCY == pytest.approx(0.9)
    assert DISCHARGE_EFFICIENCY == pytest.approx(0.9)


def test_zero_action_preserves_energy_for_january_standby():
    state = BatteryState(INITIAL_SOC_KWH)
    for _ in range(TimeGrid.steps_per_day):
        state = apply_battery_action(state, BatteryAction())
    assert state.energy_kwh == pytest.approx(INITIAL_SOC_KWH)


def test_battery_transition_raises_instead_of_clipping():
    at_max = BatteryState(10_800.0)
    with pytest.raises(ValueError):
        apply_battery_action(at_max, BatteryAction(charge_kwh=100.0))
    at_min = BatteryState(1_200.0)
    with pytest.raises(ValueError):
        apply_battery_action(at_min, BatteryAction(discharge_kwh=100.0))


def test_info_set_is_causal_and_can_exclude_unissued_forecasts():
    decision_time = dt.datetime(2025, 2, 1, 6, 0)
    issued = InfoItem(
        "pv_forecast_kw",
        available_at=decision_time,
        valid_time=dt.datetime(2025, 2, 1, 7, 0),
        value=100,
    )
    future_actual = InfoItem(
        "pv_actual_kw",
        available_at=dt.datetime(2025, 2, 1, 6, 10),
        valid_time=dt.datetime(2025, 2, 1, 6, 0),
        value=120,
    )
    info = InfoSet.from_raw(decision_time, (issued, future_actual))
    assert info.visible_items == (issued,)
    assert not hasattr(info, "items")
    assert info.is_visible(issued) is True
    assert info.is_visible(future_actual) is False
    with pytest.raises(ValueError):
        InfoSet(decision_time=decision_time, visible_items=(future_actual,))


def _interval_result(
    *, slot: int = 0, start_energy: float = INITIAL_SOC_KWH, charge: float = 100.0
) -> IntervalResult:
    start = BatteryState(start_energy)
    action = BatteryAction(charge_kwh=charge)
    end = apply_battery_action(start, action)
    return IntervalResult(
        day=dt.date(2025, 2, 1),
        slot=slot,
        load_kw=1_000.0,
        pv_kw=0.0,
        planned_purchase_kwh=900.0,
        adjusted_purchase_kwh=950.0,
        emergency_purchase_kwh=5.0,
        action=action,
        state_start=start,
        state_end=end,
    )


def test_interval_result_carries_purchase_action_and_state_trajectory():
    interval = _interval_result()
    assert interval.interval_key == (dt.date(2025, 2, 1), 0)
    assert interval.adjustment_delta_kwh == pytest.approx(50.0)
    assert interval.state_start.energy_kwh == pytest.approx(6_000.0)
    assert interval.state_end.energy_kwh == pytest.approx(6_090.0)
    assert interval.action.charge_kwh == pytest.approx(100.0)


def test_interval_result_rejects_inconsistent_state_end():
    interval = _interval_result()
    with pytest.raises(ValueError):
        IntervalResult(
            day=interval.day,
            slot=interval.slot,
            load_kw=interval.load_kw,
            pv_kw=interval.pv_kw,
            planned_purchase_kwh=interval.planned_purchase_kwh,
            adjusted_purchase_kwh=interval.adjusted_purchase_kwh,
            emergency_purchase_kwh=interval.emergency_purchase_kwh,
            action=interval.action,
            state_start=interval.state_start,
            state_end=BatteryState(6_000.0),
        )


def test_case_result_requires_unique_ordered_interval_keys():
    interval = _interval_result()
    result = CaseResult(
        case_id="q1",
        run_id="run-1",
        status="success",
        intervals=(interval,),
    )
    assert result.intervals == (interval,)
    with pytest.raises(ValueError):
        CaseResult(
            case_id="q1",
            run_id="run-1",
            status="success",
            intervals=(interval, interval),
        )


def test_purchase_plan_keeps_adjusted_and_delta_separate():
    plan = PurchasePlan(planned_kwh=100.0, adjusted_kwh=120.0, emergency_kwh=5.0)
    assert plan.planned_kwh == 100.0
    assert plan.adjusted_kwh == 120.0
    assert plan.emergency_kwh == 5.0
    assert plan.adjustment_delta_kwh == pytest.approx(20.0)


def test_cost_breakdown_aggregates_components_without_formula():
    cost = CostBreakdown(planned_cost_cny=10.0, adjustment_cost_cny=1.5, emergency_cost_cny=2.0)
    assert cost.total_cost_cny == pytest.approx(13.5)
