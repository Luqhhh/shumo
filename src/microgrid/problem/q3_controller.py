"""One-step Q3 rolling controller over approved solver and replay contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..schemas import InputError
from .contracts import ENERGY_ABS_TOL_KWH, BatteryAction
from .q2_inputs import ActualInterval
from .q3_plan_ledger import Q3EmergencySettlementEntry, Q3PlanLedger
from .q3_replay import ChargeCurtailmentResult, apply_charge_curtailment
from .q3_solver import Q3WindowSolution, validate_q3_window_solution
from .q3_window import Q3WindowInput


@dataclass(frozen=True)
class Q3ExecutedStep:
    """One causal solve/commit/execute result before surplus attribution."""

    window: Q3WindowInput
    solution: Q3WindowSolution
    ledger_after: Q3PlanLedger
    actual: ActualInterval
    planned_action: BatteryAction
    replay: ChargeCurtailmentResult
    planned_purchase_kwh: float
    confirmed_purchase_kwh: float
    emergency_settlement: Q3EmergencySettlementEntry | None

    def __post_init__(self) -> None:
        first = self.window.points[0]
        if (self.actual.start, self.actual.end) != (first.interval_start, first.interval_end):
            raise ValueError("executed actual interval does not match the solved first slot")
        if not math.isclose(
            self.confirmed_purchase_kwh,
            self.replay.confirmed_purchase_kwh,
            rel_tol=0.0,
            abs_tol=ENERGY_ABS_TOL_KWH,
        ):
            raise ValueError("executed commitment and replay purchase disagree")
        if self.replay.emergency_purchase_kwh > ENERGY_ABS_TOL_KWH:
            if self.emergency_settlement is None:
                raise ValueError("positive emergency energy requires a settlement entry")
        elif self.emergency_settlement is not None:
            raise ValueError("zero emergency energy must not create a settlement entry")


def _base_ledger(window: Q3WindowInput, solution: Q3WindowSolution) -> Q3PlanLedger:
    day = window.decision_time.date()
    current_day = [
        (point.slot, solution.purchase_kwh[k], point.base_price_cny_per_kwh)
        for k, point in enumerate(window.points)
        if point.day == day
    ]
    if [slot for slot, _quantity, _price in current_day] != list(range(144)):
        raise InputError("00:00 Q3 base plan must materialize all 144 slots of the day")
    return Q3PlanLedger.start(
        day,
        initial_commitments_kwh=tuple(quantity for _slot, quantity, _price in current_day),
        base_prices_cny_per_kwh=tuple(price for _slot, _quantity, price in current_day),
    )


def _revised_ledger(
    window: Q3WindowInput,
    solution: Q3WindowSolution,
    current_ledger: Q3PlanLedger,
) -> Q3PlanLedger:
    if current_ledger.current.version != window.previous_plan_version:
        raise InputError("Q3 revision does not follow the window's previous plan version")
    commitments = list(current_ledger.current.committed_kwh)
    for k, point in enumerate(window.points):
        if point.day == current_ledger.day:
            if point.purchase_mode != "adjustable_commitment":
                raise InputError("Q3 revision window has a non-adjustable current-day slot")
            commitments[point.slot] = solution.purchase_kwh[k]
    return current_ledger.revise(
        window.decision_time,
        new_commitments_kwh=tuple(commitments),
        transaction_price_cny_per_kwh=float(window.transaction_price_cny_per_kwh),
    )


def execute_q3_window_step(
    *,
    window: Q3WindowInput,
    solution: Q3WindowSolution,
    actual: ActualInterval,
    current_ledger: Q3PlanLedger | None,
) -> Q3ExecutedStep:
    """Commit an allowed plan version and execute only the first battery action."""

    validation = validate_q3_window_solution(window, solution)
    if not validation["ok"]:
        raise InputError("invalid Q3 window solution: " + "; ".join(validation["issues"][:8]))
    first = window.points[0]
    if (actual.day, actual.slot, actual.start, actual.end) != (
        first.day,
        first.slot,
        first.interval_start,
        first.interval_end,
    ):
        raise InputError("actual interval does not match the Q3 window's first slot")

    if window.planning_event == "base_plan":
        if current_ledger is not None:
            raise InputError("Q3 base planning cannot overwrite an existing daily ledger")
        ledger_after = _base_ledger(window, solution)
    elif window.planning_event == "revision":
        if current_ledger is None:
            raise InputError("Q3 revision requires the previous daily ledger")
        ledger_after = _revised_ledger(window, solution, current_ledger)
    else:
        if current_ledger is None or current_ledger.current.version != window.previous_plan_version:
            raise InputError("Q3 dispatch requires the latest confirmed daily ledger")
        ledger_after = current_ledger

    confirmed = ledger_after.current.committed_kwh[first.slot]
    if not math.isclose(
        confirmed,
        solution.purchase_kwh[0],
        rel_tol=0.0,
        abs_tol=ENERGY_ABS_TOL_KWH,
    ):
        raise InputError("first-slot solver purchase does not match the confirmed ledger")
    action = BatteryAction(
        charge_kwh=solution.charge_kwh[0],
        discharge_kwh=solution.discharge_kwh[0],
    )
    replay = apply_charge_curtailment(
        state_start=window.battery_state,
        planned_action=action,
        confirmed_purchase_kwh=confirmed,
        actual_load_kwh=actual.load_kwh,
        actual_pv_kwh=actual.pv_kwh,
    )
    emergency_entry = None
    if replay.emergency_purchase_kwh > ENERGY_ABS_TOL_KWH:
        emergency_entry = Q3EmergencySettlementEntry(
            day=actual.day,
            slot=actual.slot,
            settled_at=actual.end,
            energy_kwh=replay.emergency_purchase_kwh,
            price_cny_per_kwh=first.base_price_cny_per_kwh,
            cost_cny=(5.0 * first.base_price_cny_per_kwh * replay.emergency_purchase_kwh),
        )
    return Q3ExecutedStep(
        window=window,
        solution=solution,
        ledger_after=ledger_after,
        actual=actual,
        planned_action=action,
        replay=replay,
        planned_purchase_kwh=ledger_after.versions[0].committed_kwh[first.slot],
        confirmed_purchase_kwh=confirmed,
        emergency_settlement=emergency_entry,
    )
