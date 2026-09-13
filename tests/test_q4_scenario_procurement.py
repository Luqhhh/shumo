from __future__ import annotations

import datetime as dt
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest
from test_q4_pv_blend import toy_inputs

from microgrid.problem.contracts import BatteryState, InfoItem, InfoSet
from microgrid.problem.dispatch_milp import (
    build_dispatch_problem,
    reuse_tail,
    solve_dispatch,
    validate_dispatch,
)
from microgrid.problem.purchase_ledger import PurchaseLedger
from microgrid.problem.q4_common import ACTION_START, SCENARIO_MODEL_VERSION, STEP
from microgrid.problem.q4_risk_history import JointRiskHistory
from microgrid.problem.q4_scenarios import (
    certificate_digest,
    compact_solver_record,
    scenario_forecasts,
    scenario_value,
)
from microgrid.problem.rolling_engine import _initialize_forecaster


def test_scenario_keeps_joint_sample_and_nominal_zero():
    risk = JointRiskHistory("scenario")
    risk.apply(ACTION_START, [600.0] * 144, [60.0] * 144, [1.0] * 144)
    risk.observe(ACTION_START + STEP, 660.0, 60.0, 2.0)
    risk.observe(ACTION_START + 2 * STEP, 720.0, 60.0, 3.0)
    trace = risk.apply(
        ACTION_START + dt.timedelta(days=7), [600.0] * 144, [60.0] * 144, [1.0] * 144
    )
    a, nominal, b = trace["points"][0]["joint_error_scenarios"]
    assert trace["active"] and a["load_error_kw"] == 60 and a["price_error"] == 1
    assert b["load_error_kw"] == 120 and b["price_error"] == 2
    assert a["net_error_quantile_kwh"] == 12 and b["net_error_quantile_kwh"] == 18
    assert nominal["load_error_kw"] == nominal["pv_error_kw"] == nominal["price_error"] == 0
    assert trace["points"][36]["sample_count"] == 0


@pytest.mark.parametrize("case", ["q4_2", "q4_3"])
def test_scenario_cold_problem_is_exact_v3(case):
    inputs = toy_inputs()
    snapshots = [
        _initialize_forecaster(inputs, case, "main", ACTION_START, model_version=version).refresh(
            InfoSet.from_raw(ACTION_START, ())
        )
        for version in ["q4-v3-reserve", SCENARIO_MODEL_VERSION]
    ]
    assert not snapshots[1].traces["scenario_procurement"]["active"]
    ledger = PurchaseLedger(case)
    a, b = [build_dispatch_problem(BatteryState(6000), snapshot, ledger) for snapshot in snapshots]
    for field in ["objective", "integrality", "lower", "upper", "rlo", "rhi"]:
        np.testing.assert_array_equal(getattr(a, field), getattr(b, field))
    for field in ["indptr", "indices", "data"]:
        np.testing.assert_array_equal(getattr(a.matrix, field), getattr(b.matrix, field))


def warm_snapshot(case="q4_3"):
    service = _initialize_forecaster(
        toy_inputs(), case, "main", ACTION_START, model_version=SCENARIO_MODEL_VERSION
    )
    original = service.refresh(InfoSet.from_raw(ACTION_START, ()))
    risk = JointRiskHistory("scenario")
    loads = [v * 6 for v in original.load_kwh]
    pvs = [v * 6 for v in original.pv_kwh]
    risk.apply(ACTION_START, loads, pvs, original.prices)
    risk.observe(ACTION_START + STEP, loads[0] + 180, pvs[0] + 30, original.prices[0] + 0.5)
    risk.observe(ACTION_START + 2 * STEP, loads[1] + 360, pvs[1] + 60, original.prices[1] + 1)
    time = ACTION_START + dt.timedelta(days=7)
    trace = risk.apply(time, loads, pvs, original.prices)
    return replace(
        original,
        issue_time=time,
        slots=tuple(time + k * STEP for k in range(4)),
        load_kwh=original.load_kwh[:4],
        pv_kwh=original.pv_kwh[:4],
        prices=original.prices[:4],
        terminal_value=0.0,
        traces={**original.traces, "scenario_procurement": trace},
    )


def test_scenario_weighted_objective_shared_actions_and_certificate_tamper():
    forecast = warm_snapshot()
    state = BatteryState(6000)
    ledger = PurchaseLedger("q4_3")
    plan = solve_dispatch(state, forecast, ledger)
    assert validate_dispatch(plan, state, forecast, ledger) == ()
    certificate = plan.solver_record["scenario_certificate"]
    assert plan.objective == scenario_value(certificate, forecast, ledger)
    assert scenario_forecasts(forecast)[1].load_kwh == forecast.load_kwh
    for component in certificate["components"]:
        assert component["flows"][0][:3] == list(plan.flows[0][:3])
    broken = deepcopy(plan.solver_record)
    broken["scenario_certificate"]["components"][0]["flows"][0][1] += 1
    assert validate_dispatch(replace(plan, solver_record=broken), state, forecast, ledger)
    row = compact_solver_record(plan.solver_record)
    assert "scenario_certificate" not in row and row[
        "scenario_certificate_sha256"
    ] == certificate_digest(certificate)
    assert compact_solver_record({"objective": 1}) == {"objective": 1}


def test_scenario_shift_rebuilds_all_leaves_and_absolute_bound():
    forecast = warm_snapshot("q4_2")
    state = BatteryState(6000)
    ledger = PurchaseLedger("q4_2")
    ledger.submit(
        forecast.slots[0], tuple(forecast.slots[0] + k * STEP for k in range(144)), (1000.0,) * 144
    )
    whole = forecast
    forecast = whole.sliced(whole.slots[1])
    plan = solve_dispatch(state, forecast, ledger)
    window = whole.sliced(whole.slots[2])
    shifted = reuse_tail(plan, BatteryState(plan.energy[1]), window, ledger)
    assert (
        shifted is not None
        and validate_dispatch(shifted, BatteryState(plan.energy[1]), window, ledger) == ()
    )
    assert len(shifted.solver_record["scenario_certificate"]["components"][0]["flows"]) == 2
    assert shifted.absolute_gap == plan.absolute_gap
    assert reuse_tail(plan, BatteryState(plan.energy[1] + 1), window, ledger) is None


def test_scenario_future_actual_cannot_train():
    service = _initialize_forecaster(
        toy_inputs(), "q4_3", "main", ACTION_START, model_version=SCENARIO_MODEL_VERSION
    )
    original = service.refresh(InfoSet.from_raw(ACTION_START, ()))
    other = deepcopy(service)
    future = ACTION_START + STEP
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
        service.training_state() == other.training_state()
        and other.refresh(InfoSet.from_raw(ACTION_START, ())) == original
    )
