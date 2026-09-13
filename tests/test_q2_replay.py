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


def test_replay_closes_the_ledger_and_the_pv_partition() -> None:
    """Both replay identities hold: the bus ledger, and available = used + curtailed."""

    actuals = (
        _actual(slot=0, load_kw=120.0, pv_kw=60.0),
        _actual(slot=1, load_kw=30.0, pv_kw=200.0),
        _actual(slot=2, load_kw=600.0, pv_kw=0.0),
    )
    rows = replay_q2_actions(
        actuals,
        prices={(item.day, item.slot): 2.0 for item in actuals},
        planned={
            (actuals[0].day, 0): (5.0, 0.0, 0.0),
            (actuals[1].day, 1): (10.0, 0.0, 0.0),
            (actuals[2].day, 2): (0.0, 0.0, 0.0),
        },
        initial_state=BatteryState(INITIAL_SOC_KWH),
    )

    for row in rows:
        assert row.pv_partition_residual_kwh == pytest.approx(0.0)
        assert row.ledger_residual_kwh == pytest.approx(0.0)
        assert row.pv_used_kwh + row.pv_curtail_kwh == pytest.approx(row.actual.pv_kwh)
    # Slot 0: 5 kWh of grid leaves 15 kWh of residual demand, so all 10 kWh of
    # PV is used and the remaining 5 kWh becomes realized emergency purchase.
    assert rows[0].pv_used_kwh == pytest.approx(10.0)
    assert rows[0].pv_curtail_kwh == pytest.approx(0.0)
    assert rows[0].purchase_plan.emergency_kwh == pytest.approx(5.0)
    # Slot 1: grid alone over-covers the 5 kWh load, so none of the 200/6 kWh of
    # PV is used and all of it is curtailed -- curtailment here is an artifact of
    # the grid-first attribution rule, not wasted generation in any physical sense.
    assert rows[1].pv_used_kwh == pytest.approx(0.0)
    assert rows[1].pv_curtail_kwh == pytest.approx(200.0 / 6.0)
    assert rows[1].purchase_plan.emergency_kwh == pytest.approx(0.0)


def test_replay_treats_pv_curtailment_as_a_rule_artifact_not_supply() -> None:
    """Curtailed PV is never supply, and PV covers only residual demand."""

    actual = _actual(slot=0, load_kw=30.0, pv_kw=200.0)
    row = replay_q2_actions(
        (actual,),
        prices={(actual.day, actual.slot): 1.0},
        planned={(actual.day, actual.slot): (0.0, 0.0, 0.0)},
        initial_state=BatteryState(INITIAL_SOC_KWH),
    )[0]

    # 200 kW over ten minutes is 200/6 kWh of available PV against a 5 kWh load.
    assert row.pv_used_kwh == pytest.approx(5.0)
    assert row.pv_curtail_kwh == pytest.approx(200.0 / 6.0 - 5.0)
    assert row.purchase_plan.emergency_kwh == pytest.approx(0.0)
    assert row.grid_spill_kwh == pytest.approx(0.0)
    assert row.pv_partition_residual_kwh == pytest.approx(0.0)


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


def test_replay_executes_the_committed_charge_and_bills_the_shortfall() -> None:
    """D_SETTLE: a committed charge is performed, never silently curtailed.

    The MILP proves the plan feasible against its *forecast*; if the realised
    interval falls short, the charge still happens and the gap is settled as
    emergency purchase. Curtailing it instead would let the settled action
    diverge from the planned one, which the annual terminal SOC hard constraint
    depends on.
    """

    actual = _actual(slot=0, load_kw=600.0, pv_kw=0.0)
    row = replay_q2_actions(
        (actual,),
        prices={(actual.day, actual.slot): 2.0},
        planned={(actual.day, actual.slot): (40.0, 20.0, 0.0)},
        initial_state=BatteryState(INITIAL_SOC_KWH),
    )[0]

    # 100 kWh of load plus the 20 kWh committed charge, against 40 kWh of
    # planned purchase: the 80 kWh gap is emergency purchased at 5x.
    assert row.action.charge_kwh == pytest.approx(20.0)
    assert row.purchase_plan.planned_kwh == pytest.approx(40.0)
    assert row.costs.planned_cost_cny == pytest.approx(80.0)
    assert row.purchase_plan.emergency_kwh == pytest.approx(80.0)
    assert row.costs.emergency_cost_cny == pytest.approx(800.0)
    assert row.state_end.energy_kwh == pytest.approx(INITIAL_SOC_KWH + 20.0 * 0.9)


def test_replay_charges_through_a_marginal_supply_gap() -> None:
    actual = _actual(slot=0, load_kw=600.0, pv_kw=0.0)
    row = replay_q2_actions(
        (actual,),
        prices={(actual.day, actual.slot): 1.0},
        planned={(actual.day, actual.slot): (120.0, 30.0, 0.0)},
        initial_state=BatteryState(INITIAL_SOC_KWH),
    )[0]

    # 100 kWh of load and 30 kWh of charge against 120 kWh of purchase: the
    # 10 kWh gap is bought as emergency power so the charge still happens.
    assert row.action.charge_kwh == pytest.approx(30.0)
    assert row.purchase_plan.emergency_kwh == pytest.approx(10.0)
    assert row.costs.emergency_cost_cny == pytest.approx(50.0)
    assert row.state_end.energy_kwh == pytest.approx(INITIAL_SOC_KWH + 30.0 * 0.9)
