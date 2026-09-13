"""Solver-independent replay, contract and actual cost validation for Q4."""

from __future__ import annotations

import datetime as dt
import math

from .contracts import ENERGY_ABS_TOL_KWH as TOL
from .dispatch_feedback import ExecutionRecord, validate_execution
from .purchase_ledger import PurchaseLedger
from .q4_common import ACTION_START, STEP, YEAR_END
from .validation import validate_complete_run


def validate_q4_run(
    intervals,
    executions: tuple[ExecutionRecord, ...],
    ledger: PurchaseLedger,
    *,
    end_time: dt.datetime,
    actual_inputs=None,
    reserve_start: dt.datetime | None = None,
    timely_control: bool = True,
):
    issues = []
    days = tuple(
        (ACTION_START + i * dt.timedelta(days=1)).date()
        for i in range((end_time - ACTION_START).days)
    )
    expected_count = int((end_time - ACTION_START) / STEP)
    if len(intervals) != expected_count or len(executions) != expected_count:
        issues.append("interval/execution coverage mismatch")
    report = validate_complete_run(intervals, days)
    issues.extend(report.issues)
    if not intervals or abs(intervals[0].state_start.energy_kwh - 6000) > TOL:
        issues.append("initial SOC not 6000")
    if end_time == YEAR_END and intervals and abs(intervals[-1].state_end.energy_kwh - 6000) > TOL:
        issues.append("terminal_infeasible")
    planned, adjustments, emergency = [], [], []
    for k, (interval, execution) in enumerate(zip(intervals, executions, strict=False)):
        slot = ACTION_START + k * STEP
        if reserve_start is not None:
            for boundary, state in (
                (slot, interval.state_start),
                (slot + STEP, interval.state_end),
            ):
                if boundary >= reserve_start and state.energy_kwh < 6000 - TOL:
                    issues.append(f"{boundary}:terminal_reserve_infeasible")
        issues.extend(f"{slot}:{issue}" for issue in validate_execution(execution))
        if execution.timely_control is not timely_control:
            issues.append(f"{slot}:timely_control differs from approved configuration")
        if interval.interval_key != (slot.date(), (slot.hour * 60 + slot.minute) // 10):
            issues.append(f"{slot}:interval key mismatch")
        if abs(interval.planned_purchase_kwh - ledger.committed(slot, initial=True)) > TOL:
            issues.append(f"{slot}:G0 mismatch")
        versions = [v for v in ledger.versions.get(slot.date(), []) if v.event_time <= slot]
        index = interval.slot
        if (
            not versions
            or abs(interval.adjusted_purchase_kwh - versions[-1].quantities[index]) > TOL
        ):
            issues.append(f"{slot}:executed contract mismatch")
        if (
            interval.action != execution.action
            or interval.state_start != execution.state_start
            or interval.state_end != execution.state_end
        ):
            issues.append(f"{slot}:actual action/state mismatch")
        if (
            max(
                abs(interval.emergency_purchase_kwh - execution.emergency_kwh),
                abs(interval.pv_used_kwh - execution.pv_used_kwh),
                abs(interval.load_kw / 6 - execution.load_kwh),
                abs(interval.pv_kw / 6 - execution.pv_kwh),
                abs(interval.adjusted_purchase_kwh - execution.grid_kwh),
            )
            > TOL
        ):
            issues.append(f"{slot}:actual flow mismatch")
        bill = ledger.bills.get(slot)
        if bill is None:
            issues.append(f"{slot}:missing actual bill")
            continue
        planned.append(interval.planned_purchase_kwh * bill.execution_price)
        if actual_inputs is not None:
            index = actual_inputs.index(slot)
            if (
                interval.load_kw != actual_inputs.load_kw[index]
                or interval.pv_kw != actual_inputs.pv_kw[index]
                or bill.execution_price != actual_inputs.prices[index]
            ):
                issues.append(f"{slot}:actual source mismatch")
        emergency.append(5 * execution.emergency_kwh * bill.execution_price)
        expected_adjustment = 0.0
        for version in ledger.versions.get(slot.date(), []):
            if version.version and version.event_time == slot:
                expected_adjustment += bill.execution_price * sum(
                    1.5 * p + 0.5 * m for p, m in zip(version.plus, version.minus, strict=True)
                )
                if version.trade_price != bill.execution_price:
                    issues.append(f"{slot}:trade execution price mismatch")
        adjustments.append(expected_adjustment)
        if (
            max(
                abs(planned[-1] - bill.planned_cost_cny),
                abs(emergency[-1] - bill.emergency_cost_cny),
                abs(expected_adjustment - bill.adjustment_cost_cny),
            )
            > 0.01
        ):
            issues.append(f"{slot}:bill mismatch")
    for day, versions in ledger.versions.items():
        if (
            not versions
            or versions[0].version != 0
            or versions[0].event_time != dt.datetime.combine(day, dt.time())
        ):
            issues.append(f"{day}:missing midnight G0")
            continue
        if len(versions) != (4 if ledger.case_id == "q4_3" else 1):
            issues.append(f"{day}:contract event coverage mismatch")
        for number, version in enumerate(versions):
            if version.day != day or version.version != number or len(version.quantities) != 144:
                issues.append(f"{day}:version identity/shape mismatch")
            if number:
                if ledger.case_id != "q4_3" or version.event_time.time() != dt.time(number * 6):
                    issues.append(f"{day}:illegal adjustment event")
                previous = versions[number - 1]
                for k, (old, new, p, m) in enumerate(
                    zip(
                        previous.quantities,
                        version.quantities,
                        version.plus,
                        version.minus,
                        strict=True,
                    )
                ):
                    if max(abs(p - max(new - old, 0)), abs(m - max(old - new, 0))) > TOL:
                        issues.append(f"{day}:adjacent delta mismatch")
                    if (
                        dt.datetime.combine(day, dt.time()) + k * STEP < version.event_time
                        and abs(new - old) > TOL
                    ):
                        issues.append(f"{day}:past slot changed")
    if len(ledger.bills) != expected_count:
        issues.append("billing coverage mismatch")
    recomputed = {
        "planned_cost_cny": math.fsum(planned),
        "adjustment_cost_cny": math.fsum(adjustments),
        "emergency_cost_cny": math.fsum(emergency),
    }
    recomputed["total_cost_cny"] = math.fsum(recomputed.values())
    if abs(recomputed["total_cost_cny"] - ledger.costs().total_cost_cny) > 0.01:
        issues.append("annual cost difference exceeds 0.01 CNY")
    if ledger.case_id == "q4_2" and recomputed["adjustment_cost_cny"] != 0:
        issues.append("Q4-2 adjustment cost must be zero")
    return {
        "schema_version": 2,
        "ok": not issues,
        "feedback_rule_verified": True,
        "timely_control": timely_control,
        "violations": issues,
        "checked_intervals": len(intervals),
        "full_annual": end_time == YEAR_END,
        "end_time": str(end_time),
        "terminal_reserve_start": str(reserve_start) if reserve_start is not None else None,
        "costs": recomputed,
        "energy_abs_tolerance_kwh": TOL,
        "energy_rel_tolerance": 0.0,
        "max_soc_discontinuity_kwh": report.max_abs_violation_kwh,
    }
