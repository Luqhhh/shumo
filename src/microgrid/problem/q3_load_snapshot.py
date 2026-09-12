"""Approved LOAD-HORIZON-A snapshot and 24-hour MPC window contract."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..schemas import InputError
from .contracts import STEP_MINUTES, InfoSet
from .q3_load_forecast import (
    LOAD_VERSION_HORIZON_STEPS,
    Q3_ISSUE_HOURS,
    LoadForecastPoint,
    forecast_q3_load,
)

LOAD_MPC_WINDOW_STEPS = 24 * 60 // STEP_MINUTES
LOAD_SNAPSHOT_REFRESH_HOURS = 6
_STEP = dt.timedelta(minutes=STEP_MINUTES)


def _require_ten_minute_time(name: str, value: dt.datetime) -> None:
    if value.second or value.microsecond or value.minute % STEP_MINUTES:
        raise InputError(f"{name} must be on a ten-minute boundary")


@dataclass(frozen=True)
class LoadForecastSnapshot:
    """One immutable 30-hour LOAD-A version issued at 00/06/12/18."""

    issue_time: dt.datetime
    points: tuple[LoadForecastPoint, ...]

    def __post_init__(self) -> None:
        if self.issue_time.time() not in tuple(dt.time(hour) for hour in Q3_ISSUE_HOURS):
            raise ValueError("load snapshot issue_time must be 00:00/06:00/12:00/18:00")
        if len(self.points) != LOAD_VERSION_HORIZON_STEPS:
            raise ValueError("load snapshot must contain 180 ten-minute points")
        for step, point in enumerate(self.points, start=1):
            if point.decision_time != self.issue_time or point.available_at != self.issue_time:
                raise ValueError("load snapshot points must belong to their issue version")
            expected = self.issue_time + step * _STEP
            if point.valid_time != expected:
                raise ValueError("load snapshot must cover issue+10min through issue+30h")

    @property
    def coverage_end(self) -> dt.datetime:
        return self.points[-1].valid_time


def create_load_forecast_snapshot(
    info_set: InfoSet,
    *,
    data_version: str,
) -> LoadForecastSnapshot:
    """Forecast once at a permitted publication time and freeze all 30 hours."""

    points = forecast_q3_load(
        info_set,
        data_version=data_version,
    )
    return LoadForecastSnapshot(issue_time=info_set.decision_time, points=points)


@dataclass(frozen=True)
class Q3LoadWindow:
    """The next 24 hours sliced from the latest immutable load version."""

    decision_time: dt.datetime
    snapshot_issue_time: dt.datetime
    points: tuple[LoadForecastPoint, ...]

    def __post_init__(self) -> None:
        if len(self.points) != LOAD_MPC_WINDOW_STEPS:
            raise ValueError("Q3 load MPC window must contain 144 ten-minute points")
        for step, point in enumerate(self.points, start=1):
            expected = self.decision_time + step * _STEP
            if point.valid_time != expected:
                raise ValueError("Q3 load window must cover decision+10min through decision+24h")
            if point.decision_time != self.snapshot_issue_time:
                raise ValueError("Q3 load window cannot mix forecast versions")


def build_q3_load_window(
    *,
    decision_time: dt.datetime,
    snapshot: LoadForecastSnapshot,
) -> Q3LoadWindow:
    """Slice without reforecasting or accepting newly observed actual load."""

    _require_ten_minute_time("decision_time", decision_time)
    age = decision_time - snapshot.issue_time
    if age < dt.timedelta(0) or age >= dt.timedelta(hours=LOAD_SNAPSHOT_REFRESH_HOURS):
        raise InputError("load window must use the latest visible 6-hour snapshot")

    by_time = {point.valid_time: point for point in snapshot.points}
    targets = tuple(decision_time + step * _STEP for step in range(1, LOAD_MPC_WINDOW_STEPS + 1))
    try:
        points = tuple(by_time[target] for target in targets)
    except KeyError as exc:
        raise InputError("latest load snapshot does not cover the complete 24-hour window") from exc
    return Q3LoadWindow(
        decision_time=decision_time,
        snapshot_issue_time=snapshot.issue_time,
        points=points,
    )
