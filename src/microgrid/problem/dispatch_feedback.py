"""Fixed local protection; measurements never select an economic action."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .contracts import (
    ENERGY_ABS_TOL_KWH as TOL,
)
from .contracts import (
    MAX_BUS_ENERGY_KWH,
    BatteryAction,
    BatteryState,
    apply_battery_action,
)
from .q4_common import nonnegative


@dataclass(frozen=True)
class Measurement:
    load_kwh: float
    pv_kwh: float

    def __post_init__(self):
        nonnegative("load", self.load_kwh)
        nonnegative("PV", self.pv_kwh)


@dataclass(frozen=True)
class ExecutionRecord:
    intent: BatteryAction
    action: BatteryAction
    state_start: BatteryState
    state_end: BatteryState
    grid_kwh: float
    load_kwh: float
    pv_kwh: float
    emergency_kwh: float
    pv_used_kwh: float
    grid_used_kwh: float
    unused_grid_kwh: float
    curtailed_pv_kwh: float
    charge_reduction_kwh: float
    discharge_reduction_kwh: float
    timely_control: bool
    status: str
    reasons: tuple[str, ...]
    schema_version: int = 1


def validate_execution(record: ExecutionRecord) -> tuple[str, ...]:
    """Independently recompute the approved feedback and all actual flows."""
    r = record
    c, d, u = r.action.charge_kwh, r.action.discharge_kwh, r.emergency_kwh
    issues = []
    if r.status != "success":
        issues.append("execution_status")
    if not isinstance(r.timely_control, bool):
        issues.append("invalid_timely_control")
    if max(r.intent.charge_kwh, r.intent.discharge_kwh) > MAX_BUS_ENERGY_KWH + TOL:
        issues.append("intention_power_bound")
    for state in (r.state_start, r.state_end):
        if not 1200 - TOL <= state.energy_kwh <= 10800 + TOL or (
            state.capacity_kwh,
            state.min_energy_kwh,
            state.max_energy_kwh,
        ) != (12000.0, 1200.0, 10800.0):
            issues.append("state_bounds_or_parameters")
    expected_c, expected_d = r.intent.charge_kwh, r.intent.discharge_kwh
    if r.timely_control:
        expected_c = min(expected_c, max(r.grid_kwh + r.pv_kwh - r.load_kwh, 0.0))
        expected_d = min(expected_d, r.load_kwh)
    expected_u = max(r.load_kwh + expected_c - r.grid_kwh - r.pv_kwh - expected_d, 0.0)
    expected_pv = min(r.pv_kwh, r.load_kwh + expected_c - expected_d)
    expected_grid = r.load_kwh + expected_c - expected_d - expected_u - expected_pv
    for name, actual, expected_value in (
        ("feedback_charge", c, expected_c),
        ("feedback_discharge", d, expected_d),
        ("charge_reduction", r.charge_reduction_kwh, r.intent.charge_kwh - expected_c),
        ("discharge_reduction", r.discharge_reduction_kwh, r.intent.discharge_kwh - expected_d),
        ("feedback_emergency", u, expected_u),
        ("feedback_pv_used", r.pv_used_kwh, expected_pv),
        ("feedback_grid_used", r.grid_used_kwh, expected_grid),
        ("feedback_unused_grid", r.unused_grid_kwh, r.grid_kwh - expected_grid),
        ("feedback_curtailed_pv", r.curtailed_pv_kwh, r.pv_kwh - expected_pv),
    ):
        if not math.isfinite(actual) or abs(actual - expected_value) > TOL:
            issues.append(name)
    try:
        apply_battery_action(r.state_start, r.intent)
    except ValueError:
        issues.append("illegal_intention")
    flows = (
        c,
        d,
        u,
        r.pv_used_kwh,
        r.grid_used_kwh,
        r.unused_grid_kwh,
        r.curtailed_pv_kwh,
        r.grid_kwh,
        r.load_kwh,
        r.pv_kwh,
        r.charge_reduction_kwh,
        r.discharge_reduction_kwh,
    )
    if not all(math.isfinite(value) for value in flows):
        issues.append("nonfinite_flow")
    if any(value < -TOL for value in flows):
        issues.append("negative_flow")
    if abs(r.grid_kwh + r.pv_used_kwh + d + u - r.load_kwh - c - r.unused_grid_kwh) > TOL:
        issues.append("energy_balance")
    if abs(r.pv_used_kwh + r.curtailed_pv_kwh - r.pv_kwh) > TOL:
        issues.append("pv_balance")
    if abs(r.grid_used_kwh + r.unused_grid_kwh - r.grid_kwh) > TOL:
        issues.append("grid_balance")
    if r.grid_used_kwh > r.grid_kwh + TOL or r.pv_used_kwh > r.pv_kwh + TOL:
        issues.append("utilization_bound")
    if max(c, d) > MAX_BUS_ENERGY_KWH + TOL or (c > TOL and d > TOL):
        issues.append("battery_power_or_mode")
    if d > r.load_kwh + TOL:
        issues.append("discharge_exceeds_load")
    if u > TOL and max(c, r.unused_grid_kwh, r.curtailed_pv_kwh) > TOL:
        issues.append("emergency_complementarity")
    expected = r.state_start.energy_kwh + 0.9 * c - d / 0.9
    if abs(r.state_end.energy_kwh - expected) > TOL:
        issues.append("soc_transition")
    if not 1200 - TOL <= r.state_end.energy_kwh <= 10800 + TOL:
        issues.append("soc_bound")
    if c > r.intent.charge_kwh + TOL or d > r.intent.discharge_kwh + TOL:
        issues.append("protection_increased_action")
    expected_reasons = tuple(
        reason
        for amount, reason in (
            (r.intent.charge_kwh - expected_c, "insufficient_surplus"),
            (r.intent.discharge_kwh - expected_d, "load_limit"),
        )
        if amount > TOL
    )
    if r.status == "success" and r.reasons != expected_reasons:
        issues.append("feedback_reasons")
    return tuple(issues)


def apply_feedback(
    intent: BatteryAction,
    state: BatteryState,
    committed_grid: float,
    current_measurement: Measurement,
    *,
    timely_control: bool = True,
) -> ExecutionRecord:
    nonnegative("committed_grid", committed_grid)
    # Reject illegal intentions BEFORE protection; no SOC/power repair.
    apply_battery_action(state, intent)
    load, pv, grid = current_measurement.load_kwh, current_measurement.pv_kwh, committed_grid
    charge, discharge = intent.charge_kwh, intent.discharge_kwh
    if timely_control:
        charge = min(charge, max(grid + pv - load, 0.0))
        discharge = min(discharge, load)
    action = BatteryAction(charge, discharge)
    emergency = max(load + charge - grid - pv - discharge, 0.0)
    pv_used = min(pv, load + charge - discharge)
    grid_used = load + charge - discharge - emergency - pv_used
    reasons = []
    if intent.charge_kwh - charge > TOL:
        reasons.append("insufficient_surplus")
    if intent.discharge_kwh - discharge > TOL:
        reasons.append("load_limit")
    record = ExecutionRecord(
        intent,
        action,
        state,
        apply_battery_action(state, action),
        grid,
        load,
        pv,
        emergency,
        pv_used,
        grid_used,
        grid - grid_used,
        pv - pv_used,
        intent.charge_kwh - charge,
        intent.discharge_kwh - discharge,
        timely_control,
        "success",
        tuple(reasons),
    )
    issues = validate_execution(record)
    if issues:
        from dataclasses import replace

        record = replace(record, status="execution_infeasible", reasons=tuple(reasons) + issues)
    return record
