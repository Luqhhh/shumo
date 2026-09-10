"""Complete-run validators for the shared interval result chain.

Per-interval constructors validate local consistency; this module validates
coverage, ordering, continuity, and optional daily closure for a full run.
No optimisation model is selected here.
"""

from __future__ import annotations

import datetime as _dt
import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from .contracts import ENERGY_ABS_TOL_KWH, ENERGY_REL_TOL, STEPS_PER_DAY, IntervalResult


@dataclass(frozen=True)
class RunValidation:
    ok: bool
    issues: tuple[str, ...]
    max_abs_violation_kwh: float
    checked_intervals: int


def _close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=ENERGY_REL_TOL, abs_tol=ENERGY_ABS_TOL_KWH)


def validate_complete_run(
    intervals: Iterable[IntervalResult],
    expected_days: Iterable[_dt.date],
    *,
    require_daily_equal_ends: bool = False,
) -> RunValidation:
    """Validate slot coverage, state continuity, and optional daily closure."""

    interval_list = list(intervals)
    expected = sorted(set(expected_days))
    issues: list[str] = []
    max_violation = 0.0

    keys = [interval.interval_key for interval in interval_list]
    if keys != sorted(keys):
        issues.append("intervals are not ordered by (day, slot)")
    if len(keys) != len(set(keys)):
        issues.append("intervals contain duplicate (day, slot) keys")

    by_day: dict[_dt.date, list[IntervalResult]] = defaultdict(list)
    for interval in interval_list:
        by_day[interval.day].append(interval)

    for day in expected:
        day_intervals = sorted(by_day.get(day, []), key=lambda item: item.slot)
        slots = [item.slot for item in day_intervals]
        if slots != list(range(STEPS_PER_DAY)):
            missing = sorted(set(range(STEPS_PER_DAY)) - set(slots))
            duplicates = sorted({slot for slot in slots if slots.count(slot) > 1})
            if missing:
                issues.append(f"{day.isoformat()}: missing slots {missing[:8]}")
            if duplicates:
                issues.append(f"{day.isoformat()}: duplicate slots {duplicates[:8]}")
            if len(day_intervals) != STEPS_PER_DAY:
                issues.append(
                    f"{day.isoformat()}: expected {STEPS_PER_DAY} intervals, got {len(day_intervals)}"
                )

    for previous, current in zip(interval_list, interval_list[1:], strict=False):
        if (
            previous.state_end.capacity_kwh,
            previous.state_end.min_energy_kwh,
            previous.state_end.max_energy_kwh,
        ) != (
            current.state_start.capacity_kwh,
            current.state_start.min_energy_kwh,
            current.state_start.max_energy_kwh,
        ):
            issues.append(
                f"{current.day.isoformat()}#{current.slot}: battery parameters changed across intervals"
            )
        violation = abs(previous.state_end.energy_kwh - current.state_start.energy_kwh)
        max_violation = max(max_violation, violation)
        if not _close(previous.state_end.energy_kwh, current.state_start.energy_kwh):
            issues.append(
                f"{current.day.isoformat()}#{current.slot}: state discontinuity "
                f"{previous.state_end.energy_kwh:.6f} -> {current.state_start.energy_kwh:.6f} kWh"
            )

    if require_daily_equal_ends:
        for day in expected:
            day_intervals = sorted(by_day.get(day, []), key=lambda item: item.slot)
            if len(day_intervals) != STEPS_PER_DAY:
                issues.append(
                    f"{day.isoformat()}: cannot check daily closure without 144 intervals"
                )
                continue
            first = day_intervals[0].state_start.energy_kwh
            last = day_intervals[-1].state_end.energy_kwh
            violation = abs(first - last)
            max_violation = max(max_violation, violation)
            if not _close(first, last):
                issues.append(
                    f"{day.isoformat()}: daily closure failed {first:.6f} != {last:.6f} kWh"
                )

    return RunValidation(
        ok=not issues,
        issues=tuple(issues),
        max_abs_violation_kwh=max_violation,
        checked_intervals=len(interval_list),
    )


def require_valid_complete_run(
    intervals: Iterable[IntervalResult],
    expected_days: Iterable[_dt.date],
    *,
    require_daily_equal_ends: bool = False,
) -> RunValidation:
    report = validate_complete_run(
        intervals, expected_days, require_daily_equal_ends=require_daily_equal_ends
    )
    if not report.ok:
        raise ValueError("; ".join(report.issues))
    return report
