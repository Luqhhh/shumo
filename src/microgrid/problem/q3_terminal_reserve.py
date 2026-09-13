"""User-approved Q3 final-24-hour SOC floor, without changing terminal equality."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

from ..schemas import InputError
from .contracts import ENERGY_ABS_TOL_KWH, INITIAL_SOC_KWH, SOC_MIN_KWH, IntervalResult, TimeGrid

Q3_RESERVE_START = dt.datetime(2025, 12, 31)
Q3_RESERVE_END = dt.datetime(2026, 1, 1)
Q3_RESERVE_POLICY = "Q3-TERMINAL-RESERVE-24H"


class Q3TerminalReserveError(InputError):
    """A realized reserve violation, retaining the unmodified executed step."""

    def __init__(self, message: str, *, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence


def q3_soc_lower_bound(boundary_time: dt.datetime) -> float:
    if Q3_RESERVE_START <= boundary_time <= Q3_RESERVE_END:
        return INITIAL_SOC_KWH
    return SOC_MIN_KWH


def validate_q3_terminal_reserve(intervals: Iterable[IntervalResult]) -> dict[str, Any]:
    """Independently audit unique realized state boundaries in the reserve period."""

    boundaries: dict[dt.datetime, float] = {}
    issues: list[str] = []
    for interval in intervals:
        grid_interval = TimeGrid().interval(interval.day, interval.slot)
        for time, state in (
            (grid_interval.start, interval.state_start),
            (grid_interval.end, interval.state_end),
        ):
            if not Q3_RESERVE_START <= time <= Q3_RESERVE_END:
                continue
            energy = state.energy_kwh
            if time in boundaries and abs(boundaries[time] - energy) > ENERGY_ABS_TOL_KWH:
                issues.append(f"reserve state is discontinuous at {time.isoformat()}")
            boundaries[time] = energy
            if energy < INITIAL_SOC_KWH - ENERGY_ABS_TOL_KWH:
                issues.append(f"reserve SOC below 6000 at {time.isoformat()}: {energy!r}")
    minimum = min(boundaries.values(), default=None)
    return {
        "policy": Q3_RESERVE_POLICY,
        "start": Q3_RESERVE_START.isoformat(),
        "end": Q3_RESERVE_END.isoformat(),
        "floor_kwh": INITIAL_SOC_KWH,
        "checked_boundary_count": len(boundaries),
        "minimum_state_kwh": minimum,
        "max_shortfall_kwh": 0.0 if minimum is None else max(0.0, INITIAL_SOC_KWH - minimum),
        "ok": not issues,
        "issues": issues,
    }
