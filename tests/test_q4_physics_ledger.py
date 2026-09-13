from __future__ import annotations

import datetime as dt
import random

import pytest

from microgrid.problem.contracts import BatteryAction, BatteryState
from microgrid.problem.dispatch_feedback import Measurement, apply_feedback, validate_execution
from microgrid.problem.purchase_ledger import PurchaseLedger
from microgrid.problem.q4_common import ACTION_START, STEP, Q4Error


@pytest.mark.parametrize(("load", "emergency"), [(100, 0), (130, 30)])
def test_load_priority_limits_charge(load, emergency):
    r = apply_feedback(BatteryAction(50), BatteryState(6000), 100, Measurement(load, 0))
    assert r.action.charge_kwh == 0 and r.emergency_kwh == emergency
    assert r.state_end.energy_kwh == 6000 and validate_execution(r) == ()


def test_illegal_intention_is_not_repaired():
    with pytest.raises(ValueError, match="outside"):
        apply_feedback(BatteryAction(discharge_kwh=100), BatteryState(1200), 100, Measurement(0, 0))


def test_unresponsive_control_preserves_action_and_failure():
    r = apply_feedback(
        BatteryAction(50), BatteryState(6000), 100, Measurement(100, 0), timely_control=False
    )
    assert r.action.charge_kwh == 50 and r.emergency_kwh == 50
    assert r.status == "execution_infeasible" and "emergency_complementarity" in r.reasons


def test_feedback_random_legal_intentions_and_soc():
    rng = random.Random(20260912)
    for _ in range(2000):
        energy = rng.uniform(1200, 10800)
        state = BatteryState(energy)
        intent = (
            BatteryAction(rng.uniform(0, min(5000 / 6, (10800 - energy) / 0.9)))
            if rng.random() < 0.5
            else BatteryAction(discharge_kwh=rng.uniform(0, min(5000 / 6, (energy - 1200) * 0.9)))
        )
        r = apply_feedback(
            intent,
            state,
            rng.uniform(0, 1500),
            Measurement(rng.uniform(0, 1500), rng.uniform(0, 1500)),
        )
        assert r.status == "success" and validate_execution(r) == ()


def slots(time, count=144):
    return tuple(time + i * STEP for i in range(count))


def test_adjacent_versions_35_5_and_actual_price_availability():
    ledger, time = PurchaseLedger("q4_3"), ACTION_START
    grid = [0.0] * 144
    grid[108] = 10
    ledger.submit(time, slots(time), tuple(grid))
    for hour, quantity, price in ((6, 8, 1), (12, 9, 3)):
        event = time + dt.timedelta(hours=hour)
        remaining = slots(event, 144 - hour * 6)
        current = [ledger.committed(slot) for slot in remaining]
        current[108 - hour * 6] = quantity
        version = ledger.submit(event, remaining, tuple(current))
        assert version.trade_price is None
        with pytest.raises(Q4Error, match="premature_settlement"):
            ledger.settle(event, available_at=event, actual_price=price, emergency_kwh=0)
        ledger.settle(event, available_at=event + STEP, actual_price=price, emergency_kwh=0)
    event = time + dt.timedelta(hours=18)
    ledger.settle(event, available_at=event + STEP, actual_price=2, emergency_kwh=1)
    assert ledger.costs().planned_cost_cny == 20
    assert ledger.costs().adjustment_cost_cny == 5.5
    assert ledger.costs().emergency_cost_cny == 10
    assert ledger.costs().total_cost_cny == 35.5
    with pytest.raises(Q4Error, match="duplicate_settlement"):
        ledger.settle(event, available_at=event + STEP, actual_price=2, emergency_kwh=1)


def test_contract_freeze_complete_g0_and_virtual_tail():
    time, ledger = ACTION_START, PurchaseLedger("q4_2")
    with pytest.raises(Q4Error, match="incomplete_g0"):
        ledger.submit(time, slots(time, 143), (10.0,) * 143)
    ledger.submit(time, slots(time, 180), (10.0,) * 180)
    assert len(ledger.versions) == 1
    with pytest.raises(Q4Error, match="duplicate_contract_event"):
        ledger.submit(time, slots(time), (10.0,) * 144)
    event = time + dt.timedelta(hours=6)
    assert ledger.permissions(event, slots(event)) == ("fixed",) * 108 + ("lookahead",) * 36
    with pytest.raises(Q4Error, match="contract_frozen"):
        ledger.submit(event, slots(event), (11.0,) * 144)


def test_adjustment_preserves_past_prefix():
    time, ledger = ACTION_START, PurchaseLedger("q4_3")
    ledger.submit(time, slots(time), (10.0,) * 144)
    event = time + dt.timedelta(hours=6)
    version = ledger.submit(event, slots(event), (20.0,) * 144)
    assert version.quantities[:36] == (10.0,) * 36
    assert version.quantities[36:] == (20.0,) * 108
