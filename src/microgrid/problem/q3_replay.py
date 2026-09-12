"""Q3 charge-curtailment recourse and ATTR-PV-FIRST accounting.

The actual replay may reduce planned charging enough to keep the current
interval feasible. It must not add discharge, reverse direction, re-optimize,
or use emergency energy to charge the battery. Under the user-approved
ATTR-PV-FIRST rule, realized surplus is attributed to unused contracted grid
energy before any remaining surplus is recorded as PV curtailment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..schemas import InputError
from .contracts import (
    ENERGY_ABS_TOL_KWH,
    BatteryAction,
    BatteryState,
    apply_battery_action,
)


def _non_negative(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{name} must be numeric")
    clean = float(value)
    if not math.isfinite(clean) or clean < -ENERGY_ABS_TOL_KWH:
        raise InputError(f"{name} must be finite and non-negative")
    return max(0.0, clean)


@dataclass(frozen=True)
class ChargeCurtailmentResult:
    """Physical current-slot result with ATTR-PV-FIRST spill attribution."""

    state_start: BatteryState
    state_end: BatteryState
    planned_action: BatteryAction
    executed_action: BatteryAction
    confirmed_purchase_kwh: float
    actual_load_kwh: float
    actual_pv_kwh: float
    pv_used_kwh: float
    pv_curtailment_kwh: float
    grid_spill_kwh: float
    emergency_purchase_kwh: float

    def __post_init__(self) -> None:
        for name in (
            "confirmed_purchase_kwh",
            "actual_load_kwh",
            "actual_pv_kwh",
            "pv_used_kwh",
            "pv_curtailment_kwh",
            "grid_spill_kwh",
            "emergency_purchase_kwh",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < -ENERGY_ABS_TOL_KWH:
                raise ValueError(f"{name} must be finite and non-negative")
        if not math.isclose(
            self.executed_action.discharge_kwh,
            self.planned_action.discharge_kwh,
            rel_tol=0.0,
            abs_tol=ENERGY_ABS_TOL_KWH,
        ):
            raise ValueError("charge recourse cannot change planned discharge")
        if self.executed_action.charge_kwh > self.planned_action.charge_kwh:
            raise ValueError("charge recourse cannot increase planned charge")
        if (
            self.emergency_purchase_kwh > ENERGY_ABS_TOL_KWH
            and self.executed_action.charge_kwh > ENERGY_ABS_TOL_KWH
        ):
            raise ValueError("emergency purchase and executed charge cannot coexist")
        if self.grid_spill_kwh > self.confirmed_purchase_kwh + ENERGY_ABS_TOL_KWH:
            raise ValueError("grid spill exceeds confirmed contracted energy")
        if not math.isclose(
            self.pv_used_kwh + self.pv_curtailment_kwh,
            self.actual_pv_kwh,
            rel_tol=0.0,
            abs_tol=ENERGY_ABS_TOL_KWH,
        ):
            raise ValueError("PV use and curtailment do not sum to actual PV")
        if self.emergency_purchase_kwh > ENERGY_ABS_TOL_KWH and (
            self.grid_spill_kwh > ENERGY_ABS_TOL_KWH or self.pv_curtailment_kwh > ENERGY_ABS_TOL_KWH
        ):
            raise ValueError("emergency purchase and surplus cannot coexist")

        left = (
            self.confirmed_purchase_kwh
            + self.pv_used_kwh
            + self.executed_action.discharge_kwh
            + self.emergency_purchase_kwh
        )
        right = self.actual_load_kwh + self.executed_action.charge_kwh + self.grid_spill_kwh
        if not math.isclose(left, right, rel_tol=0.0, abs_tol=ENERGY_ABS_TOL_KWH):
            raise ValueError("charge-curtailment replay does not balance")
        expected_state = apply_battery_action(self.state_start, self.executed_action)
        if not math.isclose(
            expected_state.energy_kwh,
            self.state_end.energy_kwh,
            rel_tol=0.0,
            abs_tol=ENERGY_ABS_TOL_KWH,
        ):
            raise ValueError("charge-curtailment state transition is inconsistent")


def apply_charge_curtailment(
    *,
    state_start: BatteryState,
    planned_action: BatteryAction,
    confirmed_purchase_kwh: float,
    actual_load_kwh: float,
    actual_pv_kwh: float,
) -> ChargeCurtailmentResult:
    """Apply the frozen minimum recourse to one realized ten-minute slot."""

    purchase = _non_negative("confirmed_purchase_kwh", confirmed_purchase_kwh)
    load = _non_negative("actual_load_kwh", actual_load_kwh)
    pv = _non_negative("actual_pv_kwh", actual_pv_kwh)
    normal_supply = purchase + pv + planned_action.discharge_kwh
    executed_charge = min(
        planned_action.charge_kwh,
        max(normal_supply - load, 0.0),
    )
    emergency = max(load - normal_supply, 0.0)
    surplus = max(normal_supply - load - executed_charge, 0.0)
    grid_spill = min(purchase, surplus)
    surplus_after_grid = max(0.0, surplus - grid_spill)
    pv_curtailment = min(pv, surplus_after_grid)
    unaccounted_surplus = max(0.0, surplus_after_grid - pv_curtailment)
    if unaccounted_surplus > ENERGY_ABS_TOL_KWH:
        raise InputError(
            "Q3 replay is infeasible under fixed planned discharge: surplus remains "
            "after all contracted grid energy and actual PV are curtailed"
        )
    pv_used = max(0.0, pv - pv_curtailment)
    executed_action = BatteryAction(
        charge_kwh=executed_charge,
        discharge_kwh=planned_action.discharge_kwh,
        max_bus_energy_kwh=planned_action.max_bus_energy_kwh,
    )
    state_end = apply_battery_action(state_start, executed_action)
    return ChargeCurtailmentResult(
        state_start=state_start,
        state_end=state_end,
        planned_action=planned_action,
        executed_action=executed_action,
        confirmed_purchase_kwh=purchase,
        actual_load_kwh=load,
        actual_pv_kwh=pv,
        pv_used_kwh=pv_used,
        pv_curtailment_kwh=pv_curtailment,
        grid_spill_kwh=grid_spill,
        emergency_purchase_kwh=emergency,
    )
