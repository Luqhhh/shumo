"""Validated attachment 1/2/3 input bundle for the formal Q3 runner."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from ..schemas import InputError
from .contracts import STEPS_PER_DAY, InfoItem
from .q2_inputs import (
    ActualInterval,
    FixedPricePoint,
    historical_info_items,
    load_q2_inputs,
)
from .q3_inputs import Q3ForecastArchive, load_q3_forecast_archive

ATTACHMENT2_LOAD_SHEET = "小区负载"
ATTACHMENT2_PV_SHEET = "光伏发电实际功率"
Q3_HISTORY_START = dt.date(2025, 1, 1)
Q3_EVALUATION_START = dt.date(2025, 2, 1)
Q3_EVALUATION_END = dt.date(2025, 12, 31)
Q3_STATE_END = dt.datetime(2026, 1, 1)
Q3_RELEASE_HOURS = (0, 6, 12, 18)


def inclusive_days(start: dt.date, end: dt.date) -> tuple[dt.date, ...]:
    if end < start:
        raise InputError("Q3 end day cannot precede start day")
    return tuple(start + dt.timedelta(days=offset) for offset in range((end - start).days + 1))


@dataclass(frozen=True)
class Q3RuntimeInputs:
    fixed_prices: tuple[FixedPricePoint, ...]
    actuals: tuple[ActualInterval, ...]
    forecast_archive: Q3ForecastArchive
    raw_info_items: tuple[InfoItem, ...]
    actual_days: tuple[dt.date, ...]
    input_hashes: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if len(self.fixed_prices) != STEPS_PER_DAY:
            raise ValueError("Q3 runtime requires the complete 144-slot fixed price table")
        expected_actual_keys = tuple(
            (day, slot) for day in self.actual_days for slot in range(STEPS_PER_DAY)
        )
        actual_keys = tuple((item.day, item.slot) for item in self.actuals)
        if actual_keys != expected_actual_keys:
            raise ValueError("Q3 runtime actuals do not exactly cover the declared history days")
        expected_issues = tuple(
            dt.datetime.combine(day, dt.time(hour))
            for day in self.actual_days
            for hour in Q3_RELEASE_HOURS
        )
        actual_issues = tuple(version.issue_time for version in self.forecast_archive.versions)
        if actual_issues != expected_issues:
            raise ValueError("Q3 forecast archive does not contain four releases per history day")
        if not self.raw_info_items:
            raise ValueError("Q3 runtime causal information archive cannot be empty")
        if len(dict(self.input_hashes)) != len(self.input_hashes):
            raise ValueError("Q3 runtime input hashes contain duplicate names")

    def actuals_for(self, days: tuple[dt.date, ...]) -> tuple[ActualInterval, ...]:
        requested = set(days)
        if not requested.issubset(set(self.actual_days)):
            raise InputError("Q3 requested evaluation day is outside the actual archive")
        result = tuple(item for item in self.actuals if item.day in requested)
        expected = tuple((day, slot) for day in days for slot in range(STEPS_PER_DAY))
        if tuple((item.day, item.slot) for item in result) != expected:
            raise InputError("Q3 selected actual intervals are not ordered and complete")
        return result


def release_times(days: tuple[dt.date, ...]) -> tuple[dt.datetime, ...]:
    return tuple(
        dt.datetime.combine(day, dt.time(hour)) for day in days for hour in Q3_RELEASE_HOURS
    )


def load_q3_runtime_inputs(
    *,
    attachment1_path: str | Path,
    attachment2_path: str | Path,
    attachment3_path: str | Path,
    actual_days: tuple[dt.date, ...] | None = None,
) -> Q3RuntimeInputs:
    """Read and cross-check the three official Q3 input attachments."""

    days = actual_days or inclusive_days(Q3_HISTORY_START, Q3_EVALUATION_END)
    if not days or days != tuple(sorted(days)) or len(days) != len(set(days)):
        raise InputError("Q3 actual_days must be unique and increasing")
    q2 = load_q2_inputs(
        attachment1_path=attachment1_path,
        attachment2_path=attachment2_path,
        load_sheet_name=ATTACHMENT2_LOAD_SHEET,
        pv_sheet_name=ATTACHMENT2_PV_SHEET,
        expected_days=days,
    )
    archive = load_q3_forecast_archive(attachment3_path)
    info_items = (*historical_info_items(q2), *archive.raw_info_items())
    input_hashes = tuple(sorted((*q2.input_hashes, ("attachment3", archive.source_sha256))))
    return Q3RuntimeInputs(
        fixed_prices=q2.fixed_prices,
        actuals=q2.actuals,
        forecast_archive=archive,
        raw_info_items=info_items,
        actual_days=days,
        input_hashes=input_hashes,
    )
