from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace

import pytest

from microgrid.artifacts import file_digest
from microgrid.problem.contracts import BatteryAction, BatteryState, CaseContext, IntervalResult
from microgrid.problem.q2_inputs import ActualInterval
from microgrid.problem.q3 import _write_failure_evidence
from microgrid.problem.q3_controller import execute_q3_window_step
from microgrid.problem.q3_plan_ledger import Q3PlanLedger
from microgrid.problem.q3_solver import (
    Q3WindowSolveError,
    solve_q3_window_milp,
    validate_q3_window_solution,
)
from microgrid.problem.q3_terminal_reserve import (
    Q3TerminalReserveError,
    q3_soc_lower_bound,
    validate_q3_terminal_reserve,
)
from microgrid.problem.q3_window import Q3WindowInput, Q3WindowPoint
from microgrid.schemas import InputError


def _window(
    decision: dt.datetime,
    *,
    state: float,
    steps: int = 1,
    commitment: float = 0.0,
    load: float = 0.0,
    year_end: bool = False,
) -> Q3WindowInput:
    issue = decision.replace(hour=max(h for h in (0, 6, 12, 18) if h <= decision.hour), minute=0)
    return Q3WindowInput(
        decision_time=decision,
        battery_state=BatteryState(state),
        load_snapshot_issue_time=issue,
        pv_snapshot_issue_time=issue,
        planning_event="dispatch_only",
        previous_plan_version=3,
        transaction_price_cny_per_kwh=None,
        terminal_mode="year_end_equality" if year_end else "terminal_value",
        terminal_value_cny_per_kwh=0.0,
        terminal_target_kwh=6000.0 if year_end else None,
        points=tuple(
            Q3WindowPoint(
                interval_start=(start := decision + dt.timedelta(minutes=10 * k)),
                interval_end=start + dt.timedelta(minutes=10),
                day=start.date(),
                slot=(start.hour * 60 + start.minute) // 10,
                load_forecast_id=f"load:synthetic:{k}",
                pv_forecast_id=f"pv:synthetic:{k}",
                pv_forecast_kind="pv_attachment3",
                load_kw=load * 6,
                load_kwh=load,
                pv_kw=0.0,
                pv_kwh=0.0,
                base_price_cny_per_kwh=1.0,
                price_source_ref="synthetic:price",
                purchase_mode="fixed_commitment"
                if start.date() == decision.date()
                else "lookahead_only",
                previous_committed_kwh=commitment if start.date() == decision.date() else None,
            )
            for k in range(steps)
        ),
    )


@pytest.mark.parametrize(
    "time,expected",
    [
        (dt.datetime(2025, 12, 30, 23, 50), 1200.0),
        (dt.datetime(2025, 12, 31), 6000.0),
        (dt.datetime(2025, 12, 31, 23, 50), 6000.0),
        (dt.datetime(2026, 1, 1), 6000.0),
        (dt.datetime(2026, 1, 1, 0, 10), 1200.0),
    ],
)
def test_floor_is_inclusive_only_in_approved_absolute_period(time, expected) -> None:
    assert q3_soc_lower_bound(time) == expected


def test_initial_state_cannot_overwrite_reserve_constraint() -> None:
    window = _window(dt.datetime(2025, 12, 31, 22, 10), state=5999.0)
    with pytest.raises(Q3WindowSolveError, match="MILP failed"):
        solve_q3_window_milp(window)


def test_before_reserve_window_preserves_original_1200_floor() -> None:
    window = _window(dt.datetime(2025, 12, 29, 22, 10), state=1200.0)
    solution = solve_q3_window_milp(window)
    assert validate_q3_window_solution(window, solution)["ok"]
    assert solution.energy_kwh == pytest.approx((1200.0, 1200.0))


def test_future_reserve_boundary_applies_before_current_decision_enters_period() -> None:
    window = _window(
        dt.datetime(2025, 12, 30, 23, 50), state=5250.0, steps=2, commitment=5000.0 / 6
    )
    solution = solve_q3_window_milp(window)
    assert validate_q3_window_solution(window, solution)["ok"]
    assert solution.energy_kwh[0] == 5250.0
    assert min(solution.energy_kwh[1:]) >= 6000.0 - 1e-6
    assert solution.charge_kwh[0] == pytest.approx(5000.0 / 6)
    assert solution.purchase_kwh[0] == window.points[0].previous_committed_kwh
    # Shift a physically/economically consistent trajectory below the floor;
    # the independent validator must reject its future boundary, not just E_0.
    shifted = replace(solution, energy_kwh=tuple(e - 1 for e in solution.energy_kwh))
    shifted_window = replace(window, battery_state=BatteryState(5249.0))
    report = validate_q3_window_solution(shifted_window, shifted)
    assert not report["ok"]
    assert any("energy[1] violates Q3 terminal reserve" in issue for issue in report["issues"])


def test_validator_checks_initial_state_and_annual_equality_separately() -> None:
    window = _window(dt.datetime(2025, 12, 31, 23, 50), state=6900.0, load=810.0, year_end=True)
    solution = solve_q3_window_milp(window)
    assert validate_q3_window_solution(window, solution)["ok"]
    assert solution.energy_kwh[-1] == pytest.approx(6000.0)
    high_endpoint = replace(solution, energy_kwh=(6900.0, 6001.0))
    assert (
        "year-end battery target is not met"
        in validate_q3_window_solution(window, high_endpoint)["issues"]
    )

    flat = _window(dt.datetime(2025, 12, 31, 22, 10), state=6000.0)
    flat_solution = solve_q3_window_milp(flat)
    low_start = replace(flat, battery_state=BatteryState(5999.0))
    low_solution = replace(flat_solution, energy_kwh=(5999.0, 5999.0))
    assert any(
        "energy[0] violates Q3 terminal reserve" in issue
        for issue in validate_q3_window_solution(low_start, low_solution)["issues"]
    )


@pytest.mark.parametrize("actual_load", [100.0, 1000.0])
def test_actual_crossing_violation_keeps_executed_action_and_settlement(
    actual_load, tmp_path
) -> None:
    decision = dt.datetime(2025, 12, 30, 23, 50)
    commitment = 5000.0 / 6
    quantities = (0.0,) * 143 + (commitment,)
    ledger = Q3PlanLedger.start(
        decision.date(), initial_commitments_kwh=quantities, base_prices_cny_per_kwh=(1.0,) * 144
    )
    for hour in (6, 12, 18):
        ledger = ledger.revise(
            decision.replace(hour=hour, minute=0),
            new_commitments_kwh=quantities,
            transaction_price_cny_per_kwh=1.0,
        )
    window = _window(decision, state=5250.0, steps=2, commitment=commitment)
    solution = solve_q3_window_milp(window)
    actual = ActualInterval(
        day=decision.date(),
        slot=143,
        start=decision,
        end=decision + dt.timedelta(minutes=10),
        load_kw=actual_load * 6,
        pv_kw=0.0,
        load_source_ref="synthetic:load",
        pv_source_ref="synthetic:pv",
    )
    with pytest.raises(Q3TerminalReserveError) as caught:
        execute_q3_window_step(
            window=window, solution=solution, actual=actual, current_ledger=ledger
        )
    evidence = caught.value.evidence
    expected_charge = max(0.0, commitment - actual_load)
    expected_emergency = max(0.0, actual_load - commitment)
    assert evidence["replay"]["executed_action"]["charge_kwh"] == pytest.approx(expected_charge)
    assert evidence["replay"]["state_end"]["energy_kwh"] == pytest.approx(
        5250.0 + 0.9 * expected_charge
    )
    assert evidence["replay"]["emergency_purchase_kwh"] == pytest.approx(expected_emergency)
    assert evidence["ledger_after"]["versions"][-1]["committed_kwh"][143] == commitment
    if expected_emergency > 0:
        assert evidence["emergency_settlement"]["cost_cny"] == pytest.approx(5 * expected_emergency)
    else:
        assert evidence["emergency_settlement"] is None
    assert "boundary_time=2025-12-31T00:00:00" in str(caught.value)

    wrapped = InputError(f"execution failed: {caught.value}")
    wrapped.__cause__ = caught.value
    run_id = "synthetic-reserve-failure"
    run_dir = tmp_path / "outputs" / "runs" / "q3" / run_id
    _write_failure_evidence(
        CaseContext(repo_root=tmp_path, case_id="q3", run_id=run_id, is_synthetic=True),
        run_id,
        run_dir,
        stage="rolling_solve",
        exc=wrapped,
    )
    saved = run_dir / "terminal_reserve_failure_step.json"
    assert json.loads(saved.read_text()) == evidence
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["diagnostic_sha256"][saved.name] == file_digest(saved)
    assert not manifest["result_files"]
    assert (run_dir / "failure.json").is_file()
    assert not (run_dir / "summary.json").exists()


def _interval(day: dt.date, slot: int, energy: float = 6000.0) -> IntervalResult:
    return IntervalResult(
        day=day,
        slot=slot,
        load_kw=0.0,
        pv_kw=0.0,
        planned_purchase_kwh=0.0,
        adjusted_purchase_kwh=0.0,
        emergency_purchase_kwh=0.0,
        action=BatteryAction(),
        state_start=BatteryState(energy),
        state_end=BatteryState(energy),
    )


def test_realized_audit_checks_145_unique_boundaries_and_both_edges() -> None:
    day = dt.date(2025, 12, 31)
    intervals = tuple(_interval(day, slot) for slot in range(144))
    audit = validate_q3_terminal_reserve(intervals)
    assert audit["ok"]
    assert audit["checked_boundary_count"] == 145
    assert audit["minimum_state_kwh"] == 6000.0
    assert (
        validate_q3_terminal_reserve((_interval(dt.date(2025, 12, 29), 0, 1200),))[
            "checked_boundary_count"
        ]
        == 0
    )
    assert not validate_q3_terminal_reserve((_interval(dt.date(2025, 12, 30), 143, 5999),))["ok"]
    assert not validate_q3_terminal_reserve((_interval(dt.date(2026, 1, 1), 0, 5999),))["ok"]
    discontinuous = (_interval(day, 0), _interval(day, 1, 6001))
    assert any(
        "discontinuous" in issue for issue in validate_q3_terminal_reserve(discontinuous)["issues"]
    )
