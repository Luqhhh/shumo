from __future__ import annotations

import pytest

from microgrid.problem.contracts import BatteryAction, BatteryState
from microgrid.problem.q3_replay import apply_charge_curtailment
from microgrid.schemas import InputError


def test_full_planned_charge_is_kept_when_realized_supply_is_sufficient() -> None:
    result = apply_charge_curtailment(
        state_start=BatteryState(6_000.0),
        planned_action=BatteryAction(charge_kwh=40.0),
        confirmed_purchase_kwh=100.0,
        actual_load_kwh=80.0,
        actual_pv_kwh=20.0,
    )

    assert result.executed_action.charge_kwh == pytest.approx(40.0)
    assert result.emergency_purchase_kwh == pytest.approx(0.0)
    assert result.unattributed_surplus_kwh == pytest.approx(0.0)
    assert result.state_end.energy_kwh == pytest.approx(6_036.0)


def test_supply_shortfall_first_reduces_planned_charge() -> None:
    result = apply_charge_curtailment(
        state_start=BatteryState(6_000.0),
        planned_action=BatteryAction(charge_kwh=30.0),
        confirmed_purchase_kwh=100.0,
        actual_load_kwh=90.0,
        actual_pv_kwh=0.0,
    )

    assert result.executed_action.charge_kwh == pytest.approx(10.0)
    assert result.executed_action.discharge_kwh == pytest.approx(0.0)
    assert result.emergency_purchase_kwh == pytest.approx(0.0)
    assert result.state_end.energy_kwh == pytest.approx(6_009.0)


def test_emergency_only_fills_load_after_charge_reaches_zero() -> None:
    result = apply_charge_curtailment(
        state_start=BatteryState(6_000.0),
        planned_action=BatteryAction(charge_kwh=30.0),
        confirmed_purchase_kwh=50.0,
        actual_load_kwh=80.0,
        actual_pv_kwh=10.0,
    )

    assert result.executed_action.charge_kwh == pytest.approx(0.0)
    assert result.executed_action.discharge_kwh == pytest.approx(0.0)
    assert result.emergency_purchase_kwh == pytest.approx(20.0)
    assert result.unattributed_surplus_kwh == pytest.approx(0.0)
    assert result.state_end.energy_kwh == pytest.approx(6_000.0)


def test_recourse_never_increases_planned_discharge() -> None:
    result = apply_charge_curtailment(
        state_start=BatteryState(6_000.0),
        planned_action=BatteryAction(discharge_kwh=10.0),
        confirmed_purchase_kwh=0.0,
        actual_load_kwh=100.0,
        actual_pv_kwh=0.0,
    )

    assert result.executed_action.discharge_kwh == pytest.approx(10.0)
    assert result.emergency_purchase_kwh == pytest.approx(90.0)
    assert result.state_end.energy_kwh == pytest.approx(6_000.0 - 10.0 / 0.9)


def test_surplus_is_retained_without_choosing_grid_or_pv_attribution() -> None:
    result = apply_charge_curtailment(
        state_start=BatteryState(6_000.0),
        planned_action=BatteryAction(),
        confirmed_purchase_kwh=100.0,
        actual_load_kwh=50.0,
        actual_pv_kwh=100.0,
    )

    assert result.emergency_purchase_kwh == pytest.approx(0.0)
    assert result.unattributed_surplus_kwh == pytest.approx(150.0)
    assert not hasattr(result, "grid_spill_kwh")
    assert not hasattr(result, "pv_curtailment_kwh")


def test_replay_rejects_negative_realized_energy() -> None:
    with pytest.raises(InputError, match="actual_pv_kwh"):
        apply_charge_curtailment(
            state_start=BatteryState(6_000.0),
            planned_action=BatteryAction(),
            confirmed_purchase_kwh=0.0,
            actual_load_kwh=0.0,
            actual_pv_kwh=-1.0,
        )
