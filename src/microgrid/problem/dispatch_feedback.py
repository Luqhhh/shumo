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
    """Independent full flow and SOC checks, including complementarity."""
    r = record
    c, d, u = r.action.charge_kwh, r.action.discharge_kwh, r.emergency_kwh
    issues = []
    flows = (c, d, u, r.pv_used_kwh, r.grid_used_kwh, r.unused_grid_kwh, r.curtailed_pv_kwh)
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
