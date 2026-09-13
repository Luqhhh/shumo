from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace

import pytest

from microgrid.problem.contracts import STEPS_PER_DAY, BatteryState, CaseContext, TimeGrid
from microgrid.problem.q2_inputs import ActualInterval
from microgrid.problem.q3 import _write_failure_evidence
from microgrid.problem.q3_plan_ledger import Q3PlanLedger
from microgrid.problem.q3_rolling import run_q3_day, run_q3_period
from microgrid.problem.q3_solver import Q3WindowSolution, Q3WindowSolveError
from microgrid.problem.q3_window import Q3WindowInput, Q3WindowPoint
from microgrid.schemas import InputError


def _latest_release(decision: dt.datetime) -> dt.datetime:
    release_hour = max(hour for hour in (0, 6, 12, 18) if hour <= decision.hour)
    return dt.datetime.combine(decision.date(), dt.time(release_hour))


def _event(decision: dt.datetime) -> str:
    if decision.time() == dt.time():
        return "base_plan"
    if decision.time() in (dt.time(6), dt.time(12), dt.time(18)):
        return "revision"
    return "dispatch_only"


def _window_factory(
    *,
    decision_time: dt.datetime,
    battery_state: BatteryState,
    current_ledger: Q3PlanLedger | None,
) -> Q3WindowInput:
    event = _event(decision_time)
    issue = _latest_release(decision_time)
    points: list[Q3WindowPoint] = []
    for index in range(STEPS_PER_DAY):
        start = decision_time + dt.timedelta(minutes=10 * index)
        end = start + dt.timedelta(minutes=10)
        day = start.date()
        slot = (start.hour * 60 + start.minute) // 10
        if day != decision_time.date():
            mode = "lookahead_only"
            previous = None
        elif event == "base_plan":
            mode = "new_commitment"
            previous = None
        elif event == "revision":
            mode = "adjustable_commitment"
            previous = current_ledger.current.committed_kwh[slot]
        else:
            mode = "fixed_commitment"
            previous = current_ledger.current.committed_kwh[slot]
        points.append(
            Q3WindowPoint(
                interval_start=start,
                interval_end=end,
                day=day,
                slot=slot,
                load_forecast_id=f"load:{issue.isoformat()}:{end.isoformat()}",
                pv_forecast_id=f"pv:{issue.isoformat()}:{end.isoformat()}",
                pv_forecast_kind="pv_attachment3",
                load_kw=0.0,
                load_kwh=0.0,
                pv_kw=0.0,
                pv_kwh=0.0,
                base_price_cny_per_kwh=1.0,
                price_source_ref=f"synthetic:price:{slot}",
                purchase_mode=mode,
                previous_committed_kwh=previous,
            )
        )
    return Q3WindowInput(
        decision_time=decision_time,
        battery_state=battery_state,
        load_snapshot_issue_time=issue,
        pv_snapshot_issue_time=issue,
        planning_event=event,
        previous_plan_version=(None if current_ledger is None else current_ledger.current.version),
        transaction_price_cny_per_kwh=1.0 if event == "revision" else None,
        terminal_mode="terminal_value",
        terminal_value_cny_per_kwh=0.0,
        terminal_target_kwh=None,
        points=tuple(points),
    )


def _zero_solution(window: Q3WindowInput) -> Q3WindowSolution:
    zeros = (0.0,) * len(window.points)
    return Q3WindowSolution(
        decision_time=window.decision_time,
        purchase_kwh=zeros,
        delta_plus_kwh=zeros,
        delta_minus_kwh=zeros,
        charge_kwh=zeros,
        discharge_kwh=zeros,
        pv_used_kwh=zeros,
        grid_spill_kwh=zeros,
        predicted_emergency_kwh=zeros,
        energy_kwh=(window.battery_state.energy_kwh,) * (len(window.points) + 1),
        objective_cny=0.0,
        economic_components_cny={
            "new_plan_cost_cny": 0.0,
            "adjustment_cost_cny": 0.0,
            "lookahead_surrogate_cost_cny": 0.0,
            "predicted_emergency_cost_cny": 0.0,
            "terminal_value_credit_cny": 0.0,
        },
        solver_status=0,
        solver_message="synthetic orchestration fixture",
        solver_metadata={"is_synthetic": True},
    )


def _actuals(day: dt.date, *, emergency_slot: int | None = None) -> tuple[ActualInterval, ...]:
    grid = TimeGrid()
    rows: list[ActualInterval] = []
    for slot in range(STEPS_PER_DAY):
        interval = grid.interval(day, slot)
        rows.append(
            ActualInterval(
                day=day,
                slot=slot,
                start=interval.start,
                end=interval.end,
                load_kw=60.0 if slot == emergency_slot else 0.0,
                pv_kw=0.0,
                load_source_ref=f"synthetic:load:{slot}",
                pv_source_ref=f"synthetic:pv:{slot}",
            )
        )
    return tuple(rows)


def test_one_day_driver_keeps_causal_order_and_all_four_plan_versions() -> None:
    day = dt.date(2025, 2, 1)
    calls: list[dt.datetime] = []

    def factory(**kwargs):
        calls.append(kwargs["decision_time"])
        return _window_factory(**kwargs)

    result = run_q3_day(
        day=day,
        state_start=BatteryState(6_000.0),
        actuals=_actuals(day, emergency_slot=72),
        window_factory=factory,
        solver=_zero_solution,
    )

    assert len(calls) == STEPS_PER_DAY
    assert calls[0] == dt.datetime(2025, 2, 1)
    assert calls[-1] == dt.datetime(2025, 2, 1, 23, 50)
    assert len(result.intervals) == STEPS_PER_DAY
    assert tuple(version.issue_time.hour for version in result.ledger.versions) == (0, 6, 12, 18)
    assert len(result.forecast_links) == 4 * STEPS_PER_DAY
    assert result.intervals[72].emergency_purchase_kwh == pytest.approx(10.0)
    assert result.cost_breakdown.planned_cost_cny == pytest.approx(0.0)
    assert result.cost_breakdown.adjustment_cost_cny == pytest.approx(0.0)
    assert result.cost_breakdown.emergency_cost_cny == pytest.approx(50.0)
    assert result.cost_breakdown.total_cost_cny == pytest.approx(50.0)
    assert result.state_end.energy_kwh == pytest.approx(6_000.0)

    version_one_past = next(
        link for link in result.forecast_links if link.version == 1 and link.target_slot == 0
    )
    version_zero = next(
        link for link in result.forecast_links if link.version == 0 and link.target_slot == 0
    )
    assert version_one_past.forecast_ids == version_zero.forecast_ids


def test_one_day_driver_rejects_incomplete_actual_day() -> None:
    day = dt.date(2025, 2, 1)
    with pytest.raises(InputError, match="slots 0..143"):
        run_q3_day(
            day=day,
            state_start=BatteryState(6_000.0),
            actuals=_actuals(day)[:-1],
            window_factory=_window_factory,
            solver=_zero_solution,
        )


def test_one_day_driver_adds_interval_key_to_execution_failure(monkeypatch) -> None:
    day = dt.date(2025, 2, 1)

    def fail_execution(**_kwargs):
        raise InputError("synthetic replay failure")

    monkeypatch.setattr(
        "microgrid.problem.q3_rolling.execute_q3_window_step",
        fail_execution,
    )
    with pytest.raises(
        InputError,
        match=r"2025-02-01T00:00:00 \(day=2025-02-01, slot=0\).*synthetic replay failure",
    ):
        run_q3_day(
            day=day,
            state_start=BatteryState(6_000.0),
            actuals=_actuals(day),
            window_factory=_window_factory,
            solver=_zero_solution,
        )


@pytest.mark.parametrize("terminal_mode", ["terminal_value", "year_end_equality"])
def test_one_day_driver_adds_context_to_solver_failure(terminal_mode: str, tmp_path) -> None:
    day = dt.date(2025, 12, 31)
    failing_slot = 143 if terminal_mode == "year_end_equality" else 34
    decision_time = dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=10 * failing_slot)
    cause = Q3WindowSolveError("MILP failed: status=2 HiGHS Status 8: Infeasible")

    def factory(**kwargs):
        window = _window_factory(**kwargs)
        if terminal_mode == "year_end_equality" and window.decision_time == decision_time:
            window = replace(
                window,
                terminal_mode=terminal_mode,
                terminal_target_kwh=6_000.0,
                points=tuple(point for point in window.points if point.day == day),
            )
        return window

    def fail_solver(window):
        if window.decision_time == decision_time:
            raise cause
        return _zero_solution(window)

    with pytest.raises(Q3WindowSolveError) as caught:
        run_q3_day(
            day=day,
            state_start=BatteryState(6_123.456789),
            actuals=_actuals(day),
            window_factory=factory,
            solver=fail_solver,
        )

    message = str(caught.value)
    assert f"decision_time={decision_time.isoformat()}" in message
    assert f"day={day.isoformat()}, slot={failing_slot}" in message
    assert "soc_kwh=6123.456789" in message
    assert f"terminal_mode={terminal_mode}" in message
    assert f"window_length={1 if terminal_mode == 'year_end_equality' else 144}" in message
    assert str(cause) in message
    assert caught.value.__cause__ is cause
    assert caught.value.exit_code == cause.exit_code

    run_id = "synthetic-solver-failure-context"
    run_dir = tmp_path / "outputs" / "runs" / "q3" / run_id
    _write_failure_evidence(
        CaseContext(repo_root=tmp_path, case_id="q3", run_id=run_id, is_synthetic=True),
        run_id,
        run_dir,
        stage="rolling_solve",
        exc=caught.value,
    )
    for filename in ("failure.json", "manifest.json"):
        evidence = json.loads((run_dir / filename).read_text(encoding="utf-8"))
        assert evidence["error_message"] == message
        assert evidence["error_type"] == "Q3WindowSolveError"
        assert evidence["failure_stage"] == "rolling_solve"
    assert not (run_dir / "summary.json").exists()


def test_period_driver_carries_soc_and_resets_only_daily_contract_ledger() -> None:
    first = dt.date(2025, 2, 1)
    second = first + dt.timedelta(days=1)
    result = run_q3_period(
        days=(first, second),
        state_start=BatteryState(6_000.0),
        actuals=(*_actuals(first, emergency_slot=72), *_actuals(second)),
        window_factory=_window_factory,
        solver=_zero_solution,
    )

    assert len(result.days) == 2
    assert len(result.intervals) == 288
    assert result.days[0].state_end == result.days[1].state_start
    assert result.days[1].state_start.energy_kwh == pytest.approx(6_000.0)
    assert tuple(ledger.day for ledger in result.ledgers) == (first, second)
    assert all(
        tuple(version.issue_time.hour for version in ledger.versions) == (0, 6, 12, 18)
        for ledger in result.ledgers
    )
    assert len(result.forecast_links) == 8 * STEPS_PER_DAY
    assert result.cost_breakdown.emergency_cost_cny == pytest.approx(50.0)


def test_period_driver_rejects_a_calendar_gap() -> None:
    first = dt.date(2025, 2, 1)
    third = first + dt.timedelta(days=2)
    with pytest.raises(InputError, match="consecutive"):
        run_q3_period(
            days=(first, third),
            state_start=BatteryState(6_000.0),
            actuals=(*_actuals(first), *_actuals(third)),
            window_factory=_window_factory,
            solver=_zero_solution,
        )
