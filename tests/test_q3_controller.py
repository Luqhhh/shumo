from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.contracts import BatteryState, TimeGrid
from microgrid.problem.q2_inputs import ActualInterval
from microgrid.problem.q3_controller import execute_q3_window_step
from microgrid.problem.q3_plan_ledger import Q3PlanLedger
from microgrid.problem.q3_sidecars import settlement_rows
from microgrid.problem.q3_solver import solve_q3_window_milp
from microgrid.problem.q3_window import Q3WindowInput, Q3WindowPoint
from microgrid.schemas import InputError


def _point(
    decision: dt.datetime,
    index: int,
    *,
    load_kwh: float,
    pv_kwh: float,
    current_mode: str,
    previous_kwh: float | None,
) -> Q3WindowPoint:
    start = decision + dt.timedelta(minutes=10 * index)
    end = start + dt.timedelta(minutes=10)
    day = start.date()
    is_current_day = day == decision.date()
    mode = current_mode if is_current_day else "lookahead_only"
    previous = previous_kwh if is_current_day else None
    return Q3WindowPoint(
        interval_start=start,
        interval_end=end,
        day=day,
        slot=(start.hour * 60 + start.minute) // 10,
        load_forecast_id=f"load:{decision.isoformat()}:{end.isoformat()}",
        pv_forecast_id=f"pv:{decision.isoformat()}:{end.isoformat()}",
        pv_forecast_kind="pv_attachment3",
        load_kw=load_kwh * 6,
        load_kwh=load_kwh,
        pv_kw=pv_kwh * 6,
        pv_kwh=pv_kwh,
        base_price_cny_per_kwh=1.0,
        price_source_ref="attachment1:fixed-price",
        purchase_mode=mode,
        previous_committed_kwh=previous,
    )


def _base_window() -> Q3WindowInput:
    decision = dt.datetime(2025, 12, 31)
    return Q3WindowInput(
        decision_time=decision,
        battery_state=BatteryState(6_000.0),
        load_snapshot_issue_time=decision,
        pv_snapshot_issue_time=decision,
        planning_event="base_plan",
        previous_plan_version=None,
        transaction_price_cny_per_kwh=None,
        terminal_mode="year_end_equality",
        terminal_value_cny_per_kwh=0.0,
        terminal_target_kwh=6_000.0,
        points=tuple(
            _point(
                decision,
                index,
                load_kwh=100.0,
                pv_kwh=50.0,
                current_mode="new_commitment",
                previous_kwh=None,
            )
            for index in range(144)
        ),
    )


def _revision_window(ledger: Q3PlanLedger) -> Q3WindowInput:
    decision = dt.datetime.combine(ledger.day, dt.time(6))
    return Q3WindowInput(
        decision_time=decision,
        battery_state=BatteryState(1_200.0),
        load_snapshot_issue_time=decision,
        pv_snapshot_issue_time=decision,
        planning_event="revision",
        previous_plan_version=ledger.current.version,
        transaction_price_cny_per_kwh=2.0,
        terminal_mode="terminal_value",
        terminal_value_cny_per_kwh=0.0,
        terminal_target_kwh=None,
        points=tuple(
            _point(
                decision,
                index,
                load_kwh=10.0,
                pv_kwh=0.0,
                current_mode="adjustable_commitment",
                previous_kwh=0.0,
            )
            for index in range(144)
        ),
    )


def _actual(day: dt.date, slot: int, *, load_kw: float, pv_kw: float) -> ActualInterval:
    interval = TimeGrid().interval(day, slot)
    return ActualInterval(
        day=day,
        slot=slot,
        start=interval.start,
        end=interval.end,
        load_kw=load_kw,
        pv_kw=pv_kw,
        load_source_ref="attachment2:load",
        pv_source_ref="attachment2:pv",
    )


def test_base_plan_executes_only_first_slot_and_records_actual_emergency() -> None:
    window = _base_window()
    solution = solve_q3_window_milp(window)
    executed = execute_q3_window_step(
        window=window,
        solution=solution,
        actual=_actual(window.decision_time.date(), 0, load_kw=660.0, pv_kw=180.0),
        current_ledger=None,
    )

    assert executed.ledger_after.current.version == 0
    assert executed.planned_purchase_kwh == pytest.approx(50.0)
    assert executed.confirmed_purchase_kwh == pytest.approx(50.0)
    assert executed.replay.emergency_purchase_kwh == pytest.approx(30.0)
    assert executed.replay.state_end.energy_kwh == pytest.approx(6_000.0)
    assert executed.emergency_settlement is not None
    assert executed.emergency_settlement.cost_cny == pytest.approx(150.0)

    interval = executed.to_interval_result()
    assert interval.planned_purchase_kwh == pytest.approx(50.0)
    assert interval.adjusted_purchase_kwh == pytest.approx(50.0)
    assert interval.emergency_purchase_kwh == pytest.approx(30.0)
    assert interval.pv_used_kwh == pytest.approx(30.0)
    assert interval.action == executed.replay.executed_action
    assert interval.state_end == executed.replay.state_end

    rows = settlement_rows(
        (executed.ledger_after,),
        emergency_entries=(executed.emergency_settlement,),
    )
    assert sum(row["record_type"] == "base_plan" for row in rows) == 144
    emergency = next(row for row in rows if row["record_type"] == "emergency")
    assert emergency["issue_time"] == "2025-12-31T00:10:00"
    assert emergency["cost_cny"] == pytest.approx(150.0)


def test_revision_updates_only_unfrozen_today_slots_and_executes_first_action() -> None:
    day = dt.date(2025, 2, 1)
    ledger = Q3PlanLedger.start(
        day,
        initial_commitments_kwh=(0.0,) * 144,
        base_prices_cny_per_kwh=(1.0,) * 144,
    )
    window = _revision_window(ledger)
    solution = solve_q3_window_milp(window)
    executed = execute_q3_window_step(
        window=window,
        solution=solution,
        actual=_actual(day, 36, load_kw=60.0, pv_kw=0.0),
        current_ledger=ledger,
    )

    assert executed.ledger_after.current.version == 1
    assert executed.ledger_after.current.committed_kwh[:36] == (0.0,) * 36
    assert executed.ledger_after.current.committed_kwh[36:] == pytest.approx((10.0,) * 108)
    assert executed.confirmed_purchase_kwh == pytest.approx(10.0)
    assert executed.replay.emergency_purchase_kwh == pytest.approx(0.0)
    assert executed.emergency_settlement is None


def test_controller_interval_result_uses_attr_pv_first() -> None:
    window = _base_window()
    solution = solve_q3_window_milp(window)
    executed = execute_q3_window_step(
        window=window,
        solution=solution,
        actual=_actual(window.decision_time.date(), 0, load_kw=480.0, pv_kw=600.0),
        current_ledger=None,
    )

    interval = executed.to_interval_result()
    assert executed.replay.grid_spill_kwh == pytest.approx(50.0)
    assert executed.replay.pv_curtailment_kwh == pytest.approx(20.0)
    assert interval.pv_used_kwh == pytest.approx(80.0)


def test_controller_rejects_actual_from_a_different_slot() -> None:
    window = _base_window()
    solution = solve_q3_window_milp(window)

    with pytest.raises(InputError, match="actual interval"):
        execute_q3_window_step(
            window=window,
            solution=solution,
            actual=_actual(window.decision_time.date(), 1, load_kw=600.0, pv_kw=300.0),
            current_ledger=None,
        )
