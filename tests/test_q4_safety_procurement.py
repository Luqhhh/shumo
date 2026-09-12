from __future__ import annotations

import datetime as dt
from dataclasses import replace

import numpy as np
import pytest
from test_q4_pv_blend import toy_inputs

from microgrid.problem.contracts import BatteryState, InfoSet
from microgrid.problem.dispatch_milp import (
    build_dispatch_problem,
    solve_dispatch,
    validate_dispatch,
)
from microgrid.problem.purchase_ledger import PurchaseLedger
from microgrid.problem.q4_common import ACTION_START, SAFETY_MODEL_VERSION, STEP
from microgrid.problem.q4_risk_history import JointRiskHistory
from microgrid.problem.rolling_engine import _initialize_forecaster


def test_risk_uses_original_realized_joint_errors_and_linear_quantile():
    risk = JointRiskHistory()
    trace = risk.apply(ACTION_START, [600.0] * 144, [60.0] * 144, [1.0] * 144)
    assert all(point["margin_kwh"] == 0 for point in trace["points"])
    assert (
        risk.apply(ACTION_START, [600.0] * 144, [60.0] * 144, [1.0] * 144) == trace
        and risk.pending_count == 144
    )
    risk.observe(ACTION_START + STEP, 660.0, 60.0, 2.0)
    risk.observe(ACTION_START + 2 * STEP, 720.0, 60.0, 3.0)
    trace = risk.apply(
        ACTION_START + dt.timedelta(days=7), [600.0] * 144, [60.0] * 144, [1.0] * 144
    )
    assert (
        trace["points"][0]["q80_net_error_kwh"] == 18.0 and trace["points"][0]["margin_kwh"] == 18.0
    )
    assert trace["points"][0]["sample_count"] == 2
    assert trace["points"][36]["margin_kwh"] == 0.0
    assert tuple(risk.realized)[0][-1] == 1.0  # joint price error preserved


def test_risk_exact_lower_window_is_excluded_and_negative_margin_clipped():
    risk = JointRiskHistory()
    risk.apply(ACTION_START, [600.0] * 144, [60.0] * 144, [1.0] * 144)
    target = ACTION_START + dt.timedelta(hours=6)
    risk.observe(target, 540.0, 60.0, 1.0)
    risk.observe(target + STEP, 500.0, 60.0, 1.0)
    trace = risk.apply(target + dt.timedelta(days=28), [600.0] * 144, [60.0] * 144, [1.0] * 144)
    assert len(risk.realized) == 1 and trace["points"][0]["sample_count"] == 0
    assert trace["points"][36]["margin_kwh"] == 0 and trace["points"][36]["q80_net_error_kwh"] < 0


@pytest.mark.parametrize("case", ["q4_2", "q4_3"])
def test_safety_cold_problem_is_exactly_v3_and_warm_floor_is_independent(case):
    inputs = toy_inputs()
    service = _initialize_forecaster(
        inputs, case, "main", ACTION_START, model_version=SAFETY_MODEL_VERSION
    )
    candidate = service.refresh(InfoSet.from_raw(ACTION_START, ()))
    baseline = _initialize_forecaster(inputs, case, "main", ACTION_START).refresh(
        InfoSet.from_raw(ACTION_START, ())
    )
    assert (
        baseline.load_kwh == candidate.load_kwh
        and baseline.pv_kwh == candidate.pv_kwh
        and baseline.prices == candidate.prices
        and baseline.terminal_value == candidate.terminal_value
    )
    ledger = PurchaseLedger(case)
    state = BatteryState(6000.0)
    a, b = (build_dispatch_problem(state, forecast, ledger) for forecast in [baseline, candidate])
    for field in ["objective", "integrality", "lower", "upper", "rlo", "rhi"]:
        np.testing.assert_array_equal(getattr(a, field), getattr(b, field))
    np.testing.assert_array_equal(a.matrix.indptr, b.matrix.indptr)
    np.testing.assert_array_equal(a.matrix.indices, b.matrix.indices)
    np.testing.assert_array_equal(a.matrix.data, b.matrix.data)
    trace = candidate.traces["safety_procurement"]
    trace["points"][0]["margin_kwh"] = 200.0
    one = replace(
        candidate,
        slots=candidate.slots[:1],
        load_kwh=candidate.load_kwh[:1],
        pv_kwh=candidate.pv_kwh[:1],
        prices=candidate.prices[:1],
        terminal_value=0.0,
    )
    plan = solve_dispatch(state, one, ledger)
    assert (
        plan.flows[0][0] + plan.flows[0][2]
        >= max(0.0, one.load_kwh[0] - one.pv_kwh[0] + 200.0) - 1e-6
    )
    broken = replace(
        plan, flows=(tuple(0.0 if i == 0 else value for i, value in enumerate(plan.flows[0])),)
    )
    assert any(
        "safety_procurement_floor" in issue
        for issue in validate_dispatch(broken, state, one, ledger)
    )


def test_safety_observed_future_actual_cannot_train_policy():
    from copy import deepcopy

    from microgrid.problem.contracts import InfoItem

    service = _initialize_forecaster(
        toy_inputs(), "q4_3", "main", ACTION_START, model_version=SAFETY_MODEL_VERSION
    )
    forecast = service.refresh(InfoSet.from_raw(ACTION_START, ()))
    future = ACTION_START + STEP
    other = deepcopy(service)
    other.ingest(
        InfoSet.from_raw(
            ACTION_START,
            tuple(
                InfoItem(kind, future, future, 99999)
                for kind in ["load_actual_kw", "pv_actual_kw", "price_actual"]
            ),
        )
    )
    assert (
        other.training_state() == service.training_state()
        and other.refresh(InfoSet.from_raw(ACTION_START, ())) == forecast
    )


def test_safety_frozen_contract_does_not_gain_procurement_permission():
    from copy import deepcopy

    from microgrid.problem.purchase_ledger import ContractVersion

    inputs = toy_inputs()
    forecast = _initialize_forecaster(
        inputs, "q4_2", "main", ACTION_START, model_version=SAFETY_MODEL_VERSION
    ).refresh(InfoSet.from_raw(ACTION_START, ()))
    trace = deepcopy(forecast.traces)
    trace["safety_procurement"]["points"][1]["margin_kwh"] = 1000.0
    forecast = replace(
        forecast,
        slots=forecast.slots[1:2],
        load_kwh=forecast.load_kwh[1:2],
        pv_kwh=forecast.pv_kwh[1:2],
        prices=forecast.prices[1:2],
        traces=trace,
        terminal_value=0.0,
    )
    ledger = PurchaseLedger("q4_2")
    ledger.versions[ACTION_START.date()] = [
        ContractVersion(
            ACTION_START.date(), ACTION_START, 0, (0.0,) * 144, (0.0,) * 144, (0.0,) * 144, 1.0
        )
    ]
    plan = solve_dispatch(BatteryState(1200.0), forecast, ledger)
    assert plan.flows[0][0] == 0.0 and plan.flows[0][3] > 0.0


def test_safety_floor_removal_rejects_parent_bound_and_mask_tamper():
    from collections import Counter
    from copy import deepcopy

    from microgrid.problem.dispatch_milp import reuse_tail

    service = _initialize_forecaster(
        toy_inputs(), "q4_2", "main", ACTION_START, model_version=SAFETY_MODEL_VERSION
    )
    forecast = service.refresh(InfoSet.from_raw(ACTION_START, ()))
    for point in forecast.traces["safety_procurement"]["points"][:4]:
        point["margin_kwh"] = 25.0
    ledger = PurchaseLedger("q4_2")
    plan = solve_dispatch(BatteryState(6000.0), forecast, ledger)
    assert plan.solver_record["procurement_floor_active"][:4] == [True] * 4
    broken = deepcopy(plan.solver_record)
    broken["procurement_floor_active"] = [False] * 144
    assert "procurement_floor_mask_binding" in validate_dispatch(
        replace(plan, solver_record=broken), BatteryState(6000.0), forecast, ledger
    )
    ledger.submit(ACTION_START, forecast.slots, tuple(flow[0] for flow in plan.flows))
    window = forecast.sliced(ACTION_START + STEP)
    diagnostics = Counter()
    assert (
        reuse_tail(plan, BatteryState(plan.energy[1]), window, ledger, diagnostics=diagnostics)
        is None
    )
    assert diagnostics["tail_reuse_rejected:procurement_floor_relaxed"] == 1
    fresh = solve_dispatch(BatteryState(plan.energy[1]), window, ledger)
    assert not any(fresh.solver_record["procurement_floor_active"])
    assert validate_dispatch(fresh, BatteryState(plan.energy[1]), window, ledger) == ()
