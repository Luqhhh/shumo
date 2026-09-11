from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.contracts import INITIAL_SOC_KWH, BatteryState
from microgrid.problem.q2_inputs import ActualInterval
from microgrid.problem.q2_replay import (
    Q2ReplayError,
    replay_q2_actions,
    rows_to_interval_results,
    summarize_q2_cost,
)


def _actual(*, slot: int, load_kw: float, pv_kw: float) -> ActualInterval:
    day = dt.date(2025, 2, 1)
    start = dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=slot * 10)
    return ActualInterval(
        day=day,
        slot=slot,
        start=start,
        end=start + dt.timedelta(minutes=10),
        load_kw=load_kw,
        pv_kw=pv_kw,
        load_source_ref=f"load-{slot}",
        pv_source_ref=f"pv-{slot}",
    )


def test_replay_separates_planned_and_emergency_costs() -> None:
    actual = _actual(slot=0, load_kw=600.0, pv_kw=0.0)
    rows = replay_q2_actions(
        (actual,),
        prices={(actual.day, actual.slot): 2.0},
        planned={(actual.day, actual.slot): (100.0, 0.0, 0.0)},
        initial_state=BatteryState(INITIAL_SOC_KWH),
    )

    row = rows[0]
    assert row.purchase_plan.planned_kwh == pytest.approx(100.0)
    assert row.purchase_plan.adjusted_kwh == pytest.approx(100.0)
    assert row.purchase_plan.emergency_kwh == pytest.approx(0.0)
    assert row.costs.planned_cost_cny == pytest.approx(200.0)
    assert row.costs.adjustment_cost_cny == pytest.approx(0.0)
    assert row.costs.emergency_cost_cny == pytest.approx(0.0)


def test_replay_uses_actual_pv_before_emergency_purchase() -> None:
    actual = _actual(slot=0, load_kw=120.0, pv_kw=60.0)
    row = replay_q2_actions(
        (actual,),
        prices={(actual.day, actual.slot): 2.0},
        planned={(actual.day, actual.slot): (5.0, 0.0, 0.0)},
        initial_state=BatteryState(INITIAL_SOC_KWH),
    )[0]

    assert row.pv_used_kwh == pytest.approx(10.0)
    assert row.purchase_plan.emergency_kwh == pytest.approx(5.0)
    assert row.costs.emergency_cost_cny == pytest.approx(50.0)


def test_replay_derives_grid_spill_and_preserves_soc_continuity() -> None:
    actuals = (_actual(slot=0, load_kw=600.0, pv_kw=0.0), _actual(slot=1, load_kw=600.0, pv_kw=0.0))
    prices = {(item.day, item.slot): 1.0 for item in actuals}
    rows = replay_q2_actions(
        actuals,
        prices=prices,
        planned={(actuals[0].day, 0): (150.0, 50.0, 0.0), (actuals[1].day, 1): (200.0, 0.0, 45.0)},
        initial_state=BatteryState(INITIAL_SOC_KWH),
    )

    assert rows[0].grid_spill_kwh == pytest.approx(0.0)
    assert rows[0].state_end.energy_kwh == pytest.approx(6045.0)
    assert rows[1].state_start == rows[0].state_end
    assert rows[1].state_end.energy_kwh == pytest.approx(5995.0)

    results = rows_to_interval_results(rows)
    assert results[0].state_end == rows[0].state_end
    summary = summarize_q2_cost(rows)
    assert summary.planned_cost_cny == pytest.approx(350.0)


def test_replay_rejects_invalid_battery_action() -> None:
    actual = _actual(slot=0, load_kw=1.0, pv_kw=0.0)
    with pytest.raises(Q2ReplayError, match="slot=0"):
        replay_q2_actions(
            (actual,),
            prices={(actual.day, actual.slot): 1.0},
            planned={(actual.day, actual.slot): (1.0, -1.0, 0.0)},
            initial_state=BatteryState(INITIAL_SOC_KWH),
        )


def test_pv_curtailment_is_not_double_counted_in_the_energy_ledger() -> None:
    actual = _actual(slot=0, load_kw=30.0, pv_kw=600.0)
    row = replay_q2_actions(
        (actual,),
        prices={(actual.day, actual.slot): 1.0},
        planned={(actual.day, actual.slot): (0.0, 0.0, 0.0)},
        initial_state=BatteryState(INITIAL_SOC_KWH),
    )[0]

    left = row.purchase_plan.adjusted_kwh + row.purchase_plan.emergency_kwh + row.pv_used_kwh
    right = actual.load_kwh + row.grid_spill_kwh
    assert left == pytest.approx(right)
    assert row.pv_used_kwh + row.pv_curtail_kwh == pytest.approx(actual.pv_kwh)
    assert left != pytest.approx(right + row.pv_curtail_kwh)
