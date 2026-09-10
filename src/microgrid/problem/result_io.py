"""Serialise and reload the shared CaseResult / IntervalResult carrier."""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any

from ..schemas import InputError
from .contracts import BatteryAction, BatteryState, CaseResult, IntervalResult
from .validation import validate_complete_run

RESULT_SCHEMA_VERSION = 1


def _battery_state_to_dict(state: BatteryState) -> dict[str, float]:
    return {
        "energy_kwh": state.energy_kwh,
        "capacity_kwh": state.capacity_kwh,
        "min_energy_kwh": state.min_energy_kwh,
        "max_energy_kwh": state.max_energy_kwh,
    }


def _battery_state_from_dict(data: dict[str, Any]) -> BatteryState:
    return BatteryState(
        energy_kwh=float(data["energy_kwh"]),
        capacity_kwh=float(data["capacity_kwh"]),
        min_energy_kwh=float(data["min_energy_kwh"]),
        max_energy_kwh=float(data["max_energy_kwh"]),
    )


def _action_to_dict(action: BatteryAction) -> dict[str, float]:
    return {
        "charge_kwh": action.charge_kwh,
        "discharge_kwh": action.discharge_kwh,
        "max_bus_energy_kwh": action.max_bus_energy_kwh,
    }


def _action_from_dict(data: dict[str, Any]) -> BatteryAction:
    return BatteryAction(
        charge_kwh=float(data["charge_kwh"]),
        discharge_kwh=float(data["discharge_kwh"]),
        max_bus_energy_kwh=float(data["max_bus_energy_kwh"]),
    )


def interval_to_dict(interval: IntervalResult) -> dict[str, Any]:
    return {
        "day": interval.day.isoformat(),
        "slot": interval.slot,
        "load_kw": interval.load_kw,
        "pv_kw": interval.pv_kw,
        "planned_purchase_kwh": interval.planned_purchase_kwh,
        "adjusted_purchase_kwh": interval.adjusted_purchase_kwh,
        "emergency_purchase_kwh": interval.emergency_purchase_kwh,
        "action": _action_to_dict(interval.action),
        "state_start": _battery_state_to_dict(interval.state_start),
        "state_end": _battery_state_to_dict(interval.state_end),
        "source_ref": interval.source_ref,
    }


def interval_from_dict(data: dict[str, Any]) -> IntervalResult:
    return IntervalResult(
        day=_dt.date.fromisoformat(str(data["day"])),
        slot=int(data["slot"]),
        load_kw=float(data["load_kw"]),
        pv_kw=float(data["pv_kw"]),
        planned_purchase_kwh=float(data["planned_purchase_kwh"]),
        adjusted_purchase_kwh=float(data["adjusted_purchase_kwh"]),
        emergency_purchase_kwh=float(data["emergency_purchase_kwh"]),
        action=_action_from_dict(data["action"]),
        state_start=_battery_state_from_dict(data["state_start"]),
        state_end=_battery_state_from_dict(data["state_end"]),
        source_ref=str(data.get("source_ref", "")),
    )


def case_result_to_dict(result: CaseResult) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "case_id": result.case_id,
        "run_id": result.run_id,
        "status": result.status,
        "is_synthetic": result.is_synthetic,
        "result_files": [str(path) for path in result.result_files],
        "intervals": [interval_to_dict(interval) for interval in result.intervals],
        "metadata": result.metadata,
    }


def case_result_from_dict(data: dict[str, Any]) -> CaseResult:
    version = int(data.get("schema_version", 0))
    if version != RESULT_SCHEMA_VERSION:
        raise InputError(f"unsupported result schema_version: {version!r}")
    for field in ("case_id", "run_id", "status", "intervals"):
        if field not in data:
            raise InputError(f"result JSON is missing required field: {field}")
    intervals = tuple(interval_from_dict(item) for item in data["intervals"])
    result = CaseResult(
        case_id=str(data["case_id"]),
        run_id=str(data["run_id"]),
        status=str(data["status"]),
        is_synthetic=bool(data.get("is_synthetic", False)),
        result_files=tuple(Path(str(item)) for item in data.get("result_files", [])),
        intervals=intervals,
        metadata=dict(data.get("metadata", {})),
    )
    return result


def save_case_result(path: str | Path, result: CaseResult, *, overwrite: bool = False) -> Path:
    """Atomically write result JSON without allowing accidental overwrite."""

    target = Path(path)
    if target.exists() and not overwrite:
        raise InputError(f"result file already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        case_result_to_dict(result),
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(payload + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def load_case_result(
    path: str | Path,
    *,
    expected_days: tuple[_dt.date, ...] | None = None,
    require_daily_equal_ends: bool = False,
) -> CaseResult:
    target = Path(path)
    if not target.is_file():
        raise InputError(f"result file not found: {target}")
    data = json.loads(target.read_text(encoding="utf-8"))
    result = case_result_from_dict(data)
    if expected_days is not None:
        report = validate_complete_run(
            result.intervals,
            expected_days,
            require_daily_equal_ends=require_daily_equal_ends,
        )
        if not report.ok:
            raise InputError("; ".join(report.issues))
    return result
