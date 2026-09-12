from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from microgrid.problem.contracts import BatteryState
from microgrid.problem.q3_solver import (
    Q3WindowSolveError,
    solve_q3_window_milp,
    validate_q3_window_solution,
)
from microgrid.problem.q3_window import Q3WindowInput, Q3WindowPoint


def _window(
    *,
    load_kwh: float,
    pv_kwh: float,
    purchase_mode: str,
    previous_kwh: float | None = None,
    planning_event: str = "base_plan",
    state_kwh: float = 1_200.0,
    terminal_target_kwh: float | None = None,
    price: float = 1.0,
    transaction_price: float | None = None,
    steps: int = 1,
) -> Q3WindowInput:
    if planning_event == "base_plan":
        decision = dt.datetime(2025, 12, 31)
    elif planning_event == "revision":
        decision = dt.datetime(2025, 12, 31, 6)
    else:
        decision = dt.datetime(2025, 12, 31, 6, 10)
    snapshot_issue = dt.datetime(2025, 12, 31, 6) if decision.hour == 6 else decision
    points = tuple(
        Q3WindowPoint(
            interval_start=(interval_start := decision + dt.timedelta(minutes=10 * index)),
            interval_end=interval_start + dt.timedelta(minutes=10),
            day=interval_start.date(),
            slot=(interval_start.hour * 60 + interval_start.minute) // 10,
            load_forecast_id=f"load:test:{index}",
            pv_forecast_id=f"pv:test:{index}",
            pv_forecast_kind="pv_attachment3",
            load_kw=load_kwh * 6,
            load_kwh=load_kwh,
            pv_kw=pv_kwh * 6,
            pv_kwh=pv_kwh,
            base_price_cny_per_kwh=price,
            price_source_ref="attachment1:test",
            purchase_mode=purchase_mode,
            previous_committed_kwh=previous_kwh,
        )
        for index in range(steps)
    )
    year_end = terminal_target_kwh is not None
    return Q3WindowInput(
        decision_time=decision,
        battery_state=BatteryState(state_kwh),
        load_snapshot_issue_time=snapshot_issue,
        pv_snapshot_issue_time=snapshot_issue,
        planning_event=planning_event,
        previous_plan_version=None if planning_event == "base_plan" else 0,
        transaction_price_cny_per_kwh=transaction_price,
        terminal_mode="year_end_equality" if year_end else "terminal_value",
        terminal_value_cny_per_kwh=0.0,
        terminal_target_kwh=terminal_target_kwh,
        points=points,
    )


def test_base_plan_uses_contract_and_pv_before_costly_emergency() -> None:
    window = _window(load_kwh=100.0, pv_kwh=50.0, purchase_mode="new_commitment")
    solution = solve_q3_window_milp(window)
    report = validate_q3_window_solution(window, solution)

    assert report["ok"] is True
    assert solution.purchase_kwh == pytest.approx((50.0,))
    assert solution.pv_used_kwh == pytest.approx((50.0,))
    assert solution.predicted_emergency_kwh == pytest.approx((0.0,))
    assert solution.charge_kwh == pytest.approx((0.0,))
    assert solution.discharge_kwh == pytest.approx((0.0,))
    assert solution.objective_cny == pytest.approx(50.0)


def test_fixed_shortage_becomes_residual_emergency_without_charging_or_curtailment() -> None:
    window = _window(
        load_kwh=100.0,
        pv_kwh=20.0,
        purchase_mode="fixed_commitment",
        previous_kwh=0.0,
        planning_event="dispatch_only",
    )
    solution = solve_q3_window_milp(window)
    report = validate_q3_window_solution(window, solution)

    assert report["ok"] is True
    assert solution.purchase_kwh == pytest.approx((0.0,))
    assert solution.pv_used_kwh == pytest.approx((20.0,))
    assert solution.predicted_emergency_kwh == pytest.approx((80.0,))
    assert solution.charge_kwh == pytest.approx((0.0,))
    assert solution.grid_spill_kwh == pytest.approx((0.0,))
    assert solution.objective_cny == pytest.approx(400.0)


def test_revision_deltas_reconstruct_previous_commitment_and_use_trade_price() -> None:
    window = _window(
        load_kwh=80.0,
        pv_kwh=0.0,
        purchase_mode="adjustable_commitment",
        previous_kwh=0.0,
        planning_event="revision",
        transaction_price=2.0,
    )
    solution = solve_q3_window_milp(window)
    report = validate_q3_window_solution(window, solution)

    assert report["ok"] is True
    assert solution.purchase_kwh == pytest.approx((80.0,))
    assert solution.delta_plus_kwh == pytest.approx((80.0,))
    assert solution.delta_minus_kwh == pytest.approx((0.0,))
    assert solution.economic_components_cny["adjustment_cost_cny"] == pytest.approx(240.0)
    assert solution.objective_cny == pytest.approx(240.0)


def test_lookahead_purchase_has_surrogate_cost_but_no_adjustment_delta() -> None:
    window = _window(
        load_kwh=40.0,
        pv_kwh=0.0,
        purchase_mode="lookahead_only",
        planning_event="dispatch_only",
        price=1.5,
    )
    solution = solve_q3_window_milp(window)

    assert validate_q3_window_solution(window, solution)["ok"] is True
    assert solution.purchase_kwh == pytest.approx((40.0,))
    assert solution.delta_plus_kwh == pytest.approx((0.0,))
    assert solution.delta_minus_kwh == pytest.approx((0.0,))
    assert solution.economic_components_cny["lookahead_surrogate_cost_cny"] == pytest.approx(60.0)


def test_independent_validator_detects_emergency_charge_and_balance_corruption() -> None:
    window = _window(
        load_kwh=100.0,
        pv_kwh=0.0,
        purchase_mode="fixed_commitment",
        previous_kwh=0.0,
        planning_event="dispatch_only",
    )
    solution = solve_q3_window_milp(window)
    broken = replace(solution, charge_kwh=(1.0,))
    report = validate_q3_window_solution(window, broken)

    assert report["ok"] is False
    assert any("emergency violates" in issue for issue in report["issues"])
    assert any("supply balance" in issue for issue in report["issues"])


def test_unreachable_year_end_6000_target_fails_explicitly() -> None:
    window = _window(
        load_kwh=0.0,
        pv_kwh=0.0,
        purchase_mode="fixed_commitment",
        previous_kwh=0.0,
        planning_event="dispatch_only",
        state_kwh=1_200.0,
        terminal_target_kwh=6_000.0,
    )

    with pytest.raises(Q3WindowSolveError, match="MILP failed"):
        solve_q3_window_milp(window)


def test_full_144_slot_window_solves_and_passes_independent_validation() -> None:
    window = _window(
        load_kwh=100.0,
        pv_kwh=50.0,
        purchase_mode="new_commitment",
        state_kwh=6_000.0,
        terminal_target_kwh=6_000.0,
        steps=144,
    )

    solution = solve_q3_window_milp(window, time_limit_s=10.0)
    report = validate_q3_window_solution(window, solution)
    assert report["ok"] is True
    assert len(solution.purchase_kwh) == 144
    assert sum(solution.purchase_kwh) == pytest.approx(144 * 50.0)
    assert solution.energy_kwh[-1] == pytest.approx(6_000.0)
