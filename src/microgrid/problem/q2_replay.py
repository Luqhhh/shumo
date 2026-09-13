"""Actual replay and settlement accounting for Q2 committed actions."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from dataclasses import dataclass

from ..schemas import InputError
from .contracts import (
    ENERGY_ABS_TOL_KWH,
    BatteryAction,
    BatteryState,
    CostBreakdown,
    IntervalResult,
    PurchasePlan,
    apply_battery_action,
)
from .q2_inputs import ActualInterval


class Q2ReplayError(InputError):
    """A committed Q2 action cannot be replayed consistently."""


# Frozen attribution rule for the PV audit quantities. Grid purchase and battery
# discharge are booked to the load first, so `pv_used_kwh` is whatever residual
# demand is left for PV to cover and `pv_curtail_kwh` is its complement. Both are
# attribution-rule artifacts, NOT an unconditional "actual PV utilization" or a
# true curtailment measurement; they must not be used to argue PV performance
# until the dispatch semantics themselves are revisited.
PV_ACCOUNTING_POLICY = "grid_and_discharge_first_residual_pv"


@dataclass(frozen=True)
class Q2ReplayRow:
    actual: ActualInterval
    purchase_plan: PurchasePlan
    action: BatteryAction
    state_start: BatteryState
    state_end: BatteryState
    pv_used_kwh: float
    grid_spill_kwh: float
    pv_curtail_kwh: float
    pv_partition_residual_kwh: float
    ledger_residual_kwh: float
    costs: CostBreakdown


def _quantity(name: str, value: object, *, key: tuple[dt.date, int]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Q2ReplayError(f"slot={key[1]} {name} must be numeric")
    if not math.isfinite(float(value)) or float(value) < 0:
        raise Q2ReplayError(f"slot={key[1]} {name} must be finite and non-negative")
    return float(value)


def _price(value: object, *, key: tuple[dt.date, int]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Q2ReplayError(f"slot={key[1]} price must be numeric")
    if not math.isfinite(float(value)) or float(value) < 0:
        raise Q2ReplayError(f"slot={key[1]} price must be finite and non-negative")
    return float(value)


def replay_q2_actions(
    actuals: tuple[ActualInterval, ...],
    prices: Mapping[tuple[dt.date, int], float],
    planned: Mapping[tuple[dt.date, int], tuple[float, float, float]],
    initial_state: BatteryState,
) -> tuple[Q2ReplayRow, ...]:
    """Replay committed Q2 actions against actual load/PV intervals."""

    ordered = tuple(sorted(actuals, key=lambda item: (item.day, item.slot)))
    keys = tuple((item.day, item.slot) for item in ordered)
    if len(keys) != len(set(keys)):
        raise Q2ReplayError("actuals contain duplicate day/slot keys")
    missing_prices = [key for key in keys if key not in prices]
    missing_plans = [key for key in keys if key not in planned]
    if missing_prices:
        raise Q2ReplayError(f"missing prices for {missing_prices[:5]}")
    if missing_plans:
        raise Q2ReplayError(f"missing planned actions for {missing_plans[:5]}")

    rows: list[Q2ReplayRow] = []
    state = initial_state
    for actual in ordered:
        key = (actual.day, actual.slot)
        raw_plan = planned[key]
        if len(raw_plan) != 3:
            raise Q2ReplayError(
                f"slot={actual.slot} planned tuple must be (purchase, charge, discharge)"
            )
        purchase_kwh = _quantity("planned_purchase_kwh", raw_plan[0], key=key)
        charge_kwh = _quantity("charge_kwh", raw_plan[1], key=key)
        discharge_kwh = _quantity("discharge_kwh", raw_plan[2], key=key)
        price = _price(prices[key], key=key)
        load_kwh = actual.load_kwh
        available_pv_kwh = actual.pv_kwh
        available_supply_kwh = purchase_kwh + available_pv_kwh + discharge_kwh
        # D_SETTLE: a committed charge is executed in full. The battery action is
        # a commitment, not a request, so a supply shortfall must not silently
        # cancel it -- it becomes emergency purchase below. Curtailing instead
        # would let the plan the MILP proved feasible diverge from the action
        # actually settled, and the annual terminal SOC hard constraint (which
        # the MILP enforces on the *plan*) could then never be met.
        try:
            action = BatteryAction(charge_kwh=charge_kwh, discharge_kwh=discharge_kwh)
            state_end = apply_battery_action(state, action)
        except (ValueError, TypeError) as exc:
            raise Q2ReplayError(f"slot={actual.slot} invalid battery action: {exc}") from exc

        # e_realized is the emergency purchase the replay actually had to make
        # against realised load/PV. It is the only emergency quantity that gets
        # billed (once, at 5x the interval price) and the only one carried into
        # IntervalResult.emergency_purchase_kwh. The MILP's planning-side
        # counterpart is Q2Plan.e_plan_kwh and never reaches settlement.
        e_realized_kwh = max(load_kwh + charge_kwh - available_supply_kwh, 0.0)
        pv_used_kwh = min(
            available_pv_kwh,
            max(load_kwh + charge_kwh - purchase_kwh - discharge_kwh, 0.0),
        )
        grid_spill_kwh = max(
            purchase_kwh + e_realized_kwh + pv_used_kwh + discharge_kwh - load_kwh - charge_kwh,
            0.0,
        )
        pv_curtail_kwh = available_pv_kwh - pv_used_kwh
        # Bus ledger. PV enters through pv_used_kwh only; curtailed PV is not
        # supply and must never appear on the right-hand side, or PV would be
        # double-counted and the identity would only hold when nothing is curtailed.
        ledger_residual = (
            purchase_kwh
            + e_realized_kwh
            + pv_used_kwh
            + discharge_kwh
            - load_kwh
            - charge_kwh
            - grid_spill_kwh
        )
        if abs(ledger_residual) > ENERGY_ABS_TOL_KWH:
            raise Q2ReplayError(f"slot={actual.slot} energy ledger residual={ledger_residual}")
        # Independent PV partition check: available PV splits into used plus
        # curtailed. Under PV_ACCOUNTING_POLICY the split is definitional, so
        # this holds by construction; it is a guard that fires if either side is
        # ever recomputed separately from the other.
        pv_partition_residual_kwh = available_pv_kwh - pv_used_kwh - pv_curtail_kwh
        if abs(pv_partition_residual_kwh) > ENERGY_ABS_TOL_KWH:
            raise Q2ReplayError(
                f"slot={actual.slot} PV partition residual={pv_partition_residual_kwh}"
            )

        rows.append(
            Q2ReplayRow(
                actual=actual,
                purchase_plan=PurchasePlan(
                    planned_kwh=purchase_kwh,
                    adjusted_kwh=purchase_kwh,
                    emergency_kwh=e_realized_kwh,
                ),
                action=action,
                state_start=state,
                state_end=state_end,
                pv_used_kwh=pv_used_kwh,
                grid_spill_kwh=grid_spill_kwh,
                pv_curtail_kwh=pv_curtail_kwh,
                pv_partition_residual_kwh=pv_partition_residual_kwh,
                ledger_residual_kwh=ledger_residual,
                costs=CostBreakdown(
                    planned_cost_cny=purchase_kwh * price,
                    adjustment_cost_cny=0.0,
                    emergency_cost_cny=e_realized_kwh * 5.0 * price,
                ),
            )
        )
        state = state_end
    return tuple(rows)


def rows_to_interval_results(rows: tuple[Q2ReplayRow, ...]) -> tuple[IntervalResult, ...]:
    """Map replay rows to the shared interval result contract.

    `emergency_purchase_kwh` on the shared contract carries the REALIZED
    emergency purchase (``e_realized``), i.e. the replay's committed first
    action settled against actuals. The MILP's planning-side ``e_plan`` is not
    a settlement quantity and is deliberately never mapped here.
    """

    return tuple(
        IntervalResult(
            day=row.actual.day,
            slot=row.actual.slot,
            load_kw=row.actual.load_kw,
            pv_kw=row.actual.pv_kw,
            planned_purchase_kwh=row.purchase_plan.planned_kwh,
            adjusted_purchase_kwh=row.purchase_plan.adjusted_kwh,
            emergency_purchase_kwh=row.purchase_plan.emergency_kwh,
            action=row.action,
            state_start=row.state_start,
            state_end=row.state_end,
            source_ref=f"{row.actual.load_source_ref}; {row.actual.pv_source_ref}",
            pv_used_kwh=row.pv_used_kwh,
        )
        for row in rows
    )


def summarize_q2_cost(rows: tuple[Q2ReplayRow, ...]) -> CostBreakdown:
    """Sum the three independent Q2 settlement cost components."""

    return CostBreakdown(
        planned_cost_cny=sum(row.costs.planned_cost_cny for row in rows),
        adjustment_cost_cny=sum(row.costs.adjustment_cost_cny for row in rows),
        emergency_cost_cny=sum(row.costs.emergency_cost_cny for row in rows),
    )
