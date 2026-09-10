from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from microgrid.problem.contracts import (
    INITIAL_SOC_KWH,
    BatteryAction,
    BatteryState,
    CaseResult,
    IntervalResult,
    apply_battery_action,
)
from microgrid.problem.result_io import (
    case_result_from_dict,
    case_result_to_dict,
    load_case_result,
    save_case_result,
)
from microgrid.problem.validation import validate_complete_run
from microgrid.schemas import InputError


def _day_intervals(
    day: dt.date, *, charge_kwh: float = 0.0, count: int = 144
) -> tuple[IntervalResult, ...]:
    state = BatteryState(INITIAL_SOC_KWH)
    action = BatteryAction(charge_kwh=charge_kwh)
    intervals: list[IntervalResult] = []
    for slot in range(count):
        end = apply_battery_action(state, action)
        intervals.append(
            IntervalResult(
                day=day,
                slot=slot,
                load_kw=1_000.0,
                pv_kw=0.0,
                planned_purchase_kwh=900.0,
                adjusted_purchase_kwh=900.0,
                emergency_purchase_kwh=0.0,
                action=action,
                state_start=state,
                state_end=end,
            )
        )
        state = end
    return tuple(intervals)


def test_complete_run_accepts_full_day() -> None:
    day = dt.date(2025, 2, 1)
    report = validate_complete_run(_day_intervals(day), (day,))
    assert report.ok
    assert report.checked_intervals == 144
    assert report.max_abs_violation_kwh == pytest.approx(0.0)


def test_complete_run_rejects_missing_slot_and_closure() -> None:
    day = dt.date(2025, 2, 1)
    report = validate_complete_run(_day_intervals(day, count=143), (day,))
    assert not report.ok
    assert any("missing slots" in issue for issue in report.issues)

    closure = validate_complete_run(
        _day_intervals(day, charge_kwh=1.0), (day,), require_daily_equal_ends=True
    )
    assert not closure.ok
    assert any("daily closure" in issue for issue in closure.issues)

    closed = validate_complete_run(
        _day_intervals(day, charge_kwh=0.0), (day,), require_daily_equal_ends=True
    )
    assert closed.ok


def test_result_io_roundtrip_and_synthetic_flag(tmp_path: Path) -> None:
    day = dt.date(2025, 2, 1)
    result = CaseResult(
        case_id="q1",
        run_id="run-1",
        status="success",
        is_synthetic=True,
        result_files=(Path("results/result1.xlsx"),),
        intervals=_day_intervals(day)[:3],
        metadata={"note": "roundtrip"},
    )
    target = tmp_path / "domain_result.json"
    save_case_result(target, result)
    loaded = load_case_result(target)
    assert case_result_to_dict(loaded) == case_result_to_dict(result)
    assert loaded.is_synthetic is True
    with pytest.raises(InputError):
        save_case_result(target, result)


def test_result_io_validates_expected_days_on_load(tmp_path: Path) -> None:
    day = dt.date(2025, 2, 1)
    result = CaseResult(
        case_id="q1",
        run_id="run-1",
        status="success",
        intervals=_day_intervals(day, count=143),
    )
    target = tmp_path / "domain_result.json"
    save_case_result(target, result)
    with pytest.raises(InputError):
        load_case_result(target, expected_days=(day,))


def test_result_io_rejects_bad_schema() -> None:
    with pytest.raises(InputError):
        case_result_from_dict({"schema_version": 999})
    payload = case_result_to_dict(
        CaseResult(case_id="q1", run_id="run-1", status="success", intervals=())
    )
    payload["intervals"] = "not-a-list"
    with pytest.raises((InputError, TypeError, KeyError)):
        case_result_from_dict(payload)
    assert "NaN" not in json.dumps(payload, allow_nan=False)
