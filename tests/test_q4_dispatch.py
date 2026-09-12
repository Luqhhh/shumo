from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import replace

import pytest

from microgrid.problem.contracts import BatteryState
from microgrid.problem.dispatch_feedback import Measurement, apply_feedback
from microgrid.problem.dispatch_milp import (
    DispatchPlan,
    reuse_tail,
    solve_dispatch,
    validate_dispatch,
)
from microgrid.problem.purchase_ledger import PurchaseLedger
from microgrid.problem.q4_common import ACTION_START, RESERVE_START, STEP, YEAR_END, Q4Error
from microgrid.problem.q4_forecasts import ForecastSnapshot


def forecast(time=ACTION_START, count=144, load=100, pv=0, price=1):
    return ForecastSnapshot(
        "test-id",
        "q4_2",
        time,
        tuple(time + i * STEP for i in range(count)),
        (float(load),) * count,
        (float(pv),) * count,
        (float(price),) * count,
        0,
        "main",
        {},
        {},
    )


def test_perfect_prediction_has_no_protection_and_tail_is_certified():
    snapshot, ledger, state = forecast(), PurchaseLedger("q4_2"), BatteryState(6000)
    plan = solve_dispatch(state, snapshot, ledger)
    assert validate_dispatch(plan, state, snapshot, ledger) == ()
    ledger.submit(ACTION_START, snapshot.slots, tuple(flow[0] for flow in plan.flows))
    record = apply_feedback(plan.intention(), state, plan.flows[0][0], Measurement(100, 0))
    assert record.action == record.intent and record.reasons == ()
    tail = reuse_tail(plan, record.state_end, snapshot.sliced(ACTION_START + STEP), ledger)
    assert tail is not None and tail.solver_record["reused_tail"]
    assert tail.solver_record["dual_bound"] <= tail.objective + 1e-6
    assert (
        reuse_tail(
            plan,
            BatteryState(record.state_end.energy_kwh + 10),
            snapshot.sliced(ACTION_START + STEP),
            ledger,
        )
        is None
    )
    assert (
        reuse_tail(
            replace(plan, absolute_gap=1e9),
            record.state_end,
            snapshot.sliced(ACTION_START + STEP),
            ledger,
        )
        is None
    )


def test_annual_terminal_and_reachability_in_real_solver():
    time = YEAR_END - dt.timedelta(hours=1)
    snapshot = forecast(time, 6)
    ledger = PurchaseLedger("q4_2")
    midnight = dt.datetime.combine(time.date(), dt.time())
    ledger.submit(midnight, tuple(midnight + i * STEP for i in range(144)), (1000.0,) * 144)
    plan = solve_dispatch(BatteryState(6000), snapshot, ledger)
    assert plan.energy[-1] == pytest.approx(6000, abs=1e-6)
    assert validate_dispatch(plan, BatteryState(6000), snapshot, ledger) == ()


def test_solver_failure_is_not_success_and_has_record(monkeypatch):
    import microgrid.problem.dispatch_milp as module

    class Failure:
        status, message, x, fun = 2, "infeasible", None, None

    monkeypatch.setattr(module, "milp", lambda *args, **kwargs: Failure())
    with pytest.raises(Q4Error, match="solver_failed") as exc:
        solve_dispatch(BatteryState(6000), forecast(), PurchaseLedger("q4_2"))
    assert exc.value.solver_record["status"] == 2


def test_feasible_but_wrong_mode_is_detected_independently():
    snapshot = forecast(count=1, load=0)
    flow = (0.0,) * 7 + (0.5, 0.0, 0.0, 0.0, 0.0)
    plan = DispatchPlan("id", "test-id", (flow,), (6000, 6000), 0, 0, {})
    assert "0:nonbinary_mode" in validate_dispatch(
        plan, BatteryState(6000), snapshot, PurchaseLedger("q4_2")
    )


def test_nearly_integer_mode_cannot_bypass_physical_mutual_exclusion():
    snapshot = forecast(count=1, load=100)
    c, d, z = 100.0, 5e-6, 1 - 5e-6 / (5000 / 6)
    flow = (100 + c - d, c, d, 0, 0, 0, 0, z, 0, 0, 0, 0)
    plan = DispatchPlan(
        "id", "test-id", (flow,), (6000, 6000 + 0.9 * c - d / 0.9), 100 + c - d, 0, {}
    )
    assert "0:charge_discharge_complementarity" in validate_dispatch(
        plan, BatteryState(6000), snapshot, PurchaseLedger("q4_2")
    )


def test_zero_objective_infinite_relative_gap_remains_writable(monkeypatch):
    import microgrid.problem.dispatch_milp as module

    original = module.milp

    def with_undefined_gap(*args, **kwargs):
        result = original(*args, **kwargs)
        result.mip_gap = float("inf")
        return result

    monkeypatch.setattr(module, "milp", with_undefined_gap)
    snapshot = forecast(YEAR_END - STEP, count=1)
    ledger = PurchaseLedger("q4_2")
    midnight = dt.datetime.combine(snapshot.slots[0].date(), dt.time())
    ledger.submit(midnight, tuple(midnight + k * STEP for k in range(144)), (100.0,) * 144)
    plan = solve_dispatch(BatteryState(6000), snapshot, ledger)
    assert plan.solver_record["gap"] is None
    assert plan.solver_record["nonfinite_solver_metrics"]["gap"] == "inf"
    assert math.isfinite(plan.absolute_gap)
    assert validate_dispatch(plan, BatteryState(6000), snapshot, ledger) == ()
    json.dumps(plan.solver_record, allow_nan=False)


def test_failed_nonfinite_solver_metrics_preserve_failure_record(monkeypatch):
    import microgrid.problem.dispatch_milp as module

    class Failure:
        status, message, x = 2, "infeasible", None
        fun, mip_gap, mip_dual_bound, mip_node_count = (
            float("inf"),
            float("inf"),
            -float("inf"),
            None,
        )

    monkeypatch.setattr(module, "milp", lambda *args, **kwargs: Failure())
    with pytest.raises(Q4Error, match="solver_failed") as exc:
        solve_dispatch(BatteryState(6000), forecast(), PurchaseLedger("q4_2"))
    assert exc.value.solver_record["objective"] is None
    assert exc.value.solver_record["nonfinite_solver_metrics"]["dual_bound"] == "-inf"
    json.dumps(exc.value.solver_record, allow_nan=False)


def test_reserve_applies_to_future_boundaries_before_last_day():
    snapshot = forecast(RESERVE_START - dt.timedelta(hours=1), count=12)
    ledger, state = PurchaseLedger("q4_2"), BatteryState(2000)
    midnight = RESERVE_START - dt.timedelta(days=1)
    ledger.submit(midnight, tuple(midnight + i * STEP for i in range(144)), (1000.0,) * 144)
    plan = solve_dispatch(state, snapshot, ledger)
    assert plan.energy[0] == 2000
    assert min(plan.energy[6:]) >= 6000 - 1e-6
    assert validate_dispatch(plan, state, snapshot, ledger) == ()


def test_reserve_checks_path_even_when_energy_recovers_and_rejects_cached_tail():
    snapshot, ledger = forecast(RESERVE_START, count=2, load=1000), PurchaseLedger("q4_2")
    flows = (
        (910, 0, 90, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        (1000 + 100 / 0.9, 100 / 0.9, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0),
    )
    plan = DispatchPlan("id", "test-id", flows, (6000, 5900, 6000), sum(f[0] for f in flows), 0, {})
    assert "0:physical_constraint" in validate_dispatch(plan, BatteryState(6000), snapshot, ledger)
    ledger.submit(
        RESERVE_START,
        tuple(RESERVE_START + i * STEP for i in range(144)),
        tuple(f[0] for f in flows) + (1000.0,) * 142,
    )
    assert (
        reuse_tail(plan, BatteryState(5900), snapshot.sliced(RESERVE_START + STEP), ledger) is None
    )
    with pytest.raises(Q4Error, match="terminal_reserve_infeasible"):
        solve_dispatch(BatteryState(5999), snapshot, ledger)
