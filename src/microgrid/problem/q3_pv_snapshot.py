"""HORIZON-B interface: immutable attachment-3 snapshots plus a tail protocol.

Only 00/06/12/18 publications may build a snapshot.  Intermediate ten-minute
MPC decisions slice that snapshot and request exactly the uncovered suffix
from a ``PVTailBaseline``.  No tail algorithm is implemented here while
``D_PV_TAIL_BASELINE`` remains pending.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Protocol

from ..schemas import InputError, PendingDecisionError
from .contracts import STEP_MINUTES, InfoSet, TimeGrid
from .q3_pv_forecast import Q3_ISSUE_HOURS, CombinedPVForecast
from .q3_resampling import (
    PVBoundaryProxy,
    ResamplingMethod,
    resample_combined_hourly_pv,
)

WINDOW_STEPS = 24 * 60 // STEP_MINUTES
SNAPSHOT_REFRESH_HOURS = 6
TAIL_FALLBACK_REASON = "attachment3_horizon_exhausted"
TAIL_DECISION_ID = "D-PV-TAIL-BASELINE"


def _require_ten_minute_time(name: str, value: dt.datetime) -> None:
    if value.second or value.microsecond or value.minute % STEP_MINUTES:
        raise InputError(f"{name} must be on a ten-minute boundary")


def _forecast_id(kind: str, decision_time: dt.datetime, valid_time: dt.datetime) -> str:
    return f"{kind}|decision={decision_time.isoformat()}|valid={valid_time.isoformat()}"


@dataclass(frozen=True)
class PVForecastSnapshotPoint:
    """One immutable resampled value from a real attachment-3 publication."""

    forecast_id: str
    valid_time: dt.datetime
    power_kw: float
    energy_kwh: float

    def __post_init__(self) -> None:
        if not self.forecast_id:
            raise ValueError("snapshot forecast_id must be non-empty")
        _require_ten_minute_time("snapshot valid_time", self.valid_time)
        if not math.isfinite(self.power_kw) or self.power_kw < 0:
            raise ValueError("snapshot power_kw must be finite and non-negative")
        expected = TimeGrid.power_to_energy_kwh(self.power_kw)
        if not math.isclose(self.energy_kwh, expected, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("snapshot energy_kwh must equal power_kw * 1/6 h")


@dataclass(frozen=True)
class PVForecastSnapshot:
    """The one resampling result produced at an actual forecast release."""

    issue_time: dt.datetime
    combined_forecast: CombinedPVForecast
    boundary_proxy: PVBoundaryProxy
    resampling_method: ResamplingMethod
    points: tuple[PVForecastSnapshotPoint, ...]

    def __post_init__(self) -> None:
        if self.issue_time.time() not in tuple(dt.time(hour) for hour in Q3_ISSUE_HOURS):
            raise ValueError("snapshot issue_time must be 00:00/06:00/12:00/18:00")
        if self.combined_forecast.decision_time != self.issue_time:
            raise ValueError("snapshot and combined forecast issue times differ")
        if self.boundary_proxy.interval_end != self.issue_time:
            raise ValueError("snapshot boundary proxy must end at issue_time")
        if not self.boundary_proxy.source_ref:
            raise ValueError("snapshot boundary proxy must retain a source_ref")
        if not self.combined_forecast.model_version:
            raise ValueError("snapshot combined forecast must retain a model_version")
        if any(not item.source_ref for item in self.combined_forecast.contributions):
            raise ValueError("snapshot forecast contributions must retain source_refs")
        if len(self.points) != WINDOW_STEPS:
            raise ValueError("PV snapshot must contain 144 ten-minute points")
        for step, point in enumerate(self.points, start=1):
            expected = self.issue_time + dt.timedelta(minutes=STEP_MINUTES * step)
            if point.valid_time != expected:
                raise ValueError("PV snapshot points must cover issue+10min through issue+24h")
        ids = tuple(point.forecast_id for point in self.points)
        if len(ids) != len(set(ids)):
            raise ValueError("PV snapshot forecast IDs must be unique")

    @property
    def coverage_end(self) -> dt.datetime:
        return self.points[-1].valid_time


def create_pv_forecast_snapshot(
    combined_forecast: CombinedPVForecast,
    *,
    boundary_proxy: PVBoundaryProxy,
    method: ResamplingMethod = "linear",
) -> PVForecastSnapshot:
    """Combine/resample exactly once at a real 00/06/12/18 release."""

    issue_time = combined_forecast.decision_time
    if issue_time.time() not in tuple(dt.time(hour) for hour in Q3_ISSUE_HOURS):
        raise InputError("PV snapshots can only be created at 00:00/06:00/12:00/18:00")
    resampled = resample_combined_hourly_pv(
        decision_time=issue_time,
        boundary_proxy=boundary_proxy,
        hourly_points=combined_forecast.points,
        method=method,
    )
    points = tuple(
        PVForecastSnapshotPoint(
            forecast_id=_forecast_id("pv_attachment3", issue_time, point.slot_end),
            valid_time=point.slot_end,
            power_kw=point.power_kw,
            energy_kwh=point.energy_kwh,
        )
        for point in resampled
    )
    return PVForecastSnapshot(
        issue_time=issue_time,
        combined_forecast=combined_forecast,
        boundary_proxy=boundary_proxy,
        resampling_method=method,
        points=points,
    )


@dataclass(frozen=True)
class TailForecastPoint:
    """One ten-minute tail value returned by a future approved implementation."""

    forecast_id: str
    decision_time: dt.datetime
    valid_time: dt.datetime
    available_at: dt.datetime
    power_kw: float
    energy_kwh: float
    model_version: str
    training_cutoff: dt.datetime
    source_refs: tuple[str, ...]
    fallback_reason: str = TAIL_FALLBACK_REASON

    def __post_init__(self) -> None:
        if not self.forecast_id or not self.model_version:
            raise ValueError("tail forecast_id and model_version must be non-empty")
        _require_ten_minute_time("tail decision_time", self.decision_time)
        _require_ten_minute_time("tail valid_time", self.valid_time)
        if self.valid_time <= self.decision_time:
            raise ValueError("tail valid_time must be after decision_time")
        if self.available_at > self.decision_time or self.training_cutoff > self.decision_time:
            raise ValueError("tail forecast cannot use information after decision_time")
        if not math.isfinite(self.power_kw) or self.power_kw < 0:
            raise ValueError("tail power_kw must be finite and non-negative")
        expected = TimeGrid.power_to_energy_kwh(self.power_kw)
        if not math.isclose(self.energy_kwh, expected, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("tail energy_kwh must equal power_kw * 1/6 h")
        if not self.source_refs or any(not ref for ref in self.source_refs):
            raise ValueError("tail source_refs must be non-empty")
        if self.fallback_reason != TAIL_FALLBACK_REASON:
            raise ValueError(f"tail fallback_reason must be {TAIL_FALLBACK_REASON!r}")


@dataclass(frozen=True)
class TailForecast:
    decision_time: dt.datetime
    points: tuple[TailForecastPoint, ...]

    def __post_init__(self) -> None:
        if not self.points:
            raise ValueError("tail forecast cannot be empty")
        if any(point.decision_time != self.decision_time for point in self.points):
            raise ValueError("tail points must share their decision_time")
        valid_times = tuple(point.valid_time for point in self.points)
        if valid_times != tuple(sorted(valid_times)) or len(valid_times) != len(set(valid_times)):
            raise ValueError("tail valid_times must be unique and increasing")
        forecast_ids = tuple(point.forecast_id for point in self.points)
        if len(forecast_ids) != len(set(forecast_ids)):
            raise ValueError("tail forecast IDs must be unique")


class PVTailBaseline(Protocol):
    """Algorithm-neutral pending interface; no implementation is selected."""

    def predict(
        self,
        *,
        decision_time: dt.datetime,
        target_slot_ends: tuple[dt.datetime, ...],
        info_set: InfoSet,
    ) -> TailForecast: ...


@dataclass(frozen=True)
class Q3PVWindowPoint:
    forecast_id: str
    kind: str
    valid_time: dt.datetime
    power_kw: float
    energy_kwh: float

    def __post_init__(self) -> None:
        if not self.forecast_id:
            raise ValueError("Q3 PV window forecast_id must be non-empty")
        if self.kind not in ("pv_attachment3", "pv_tail"):
            raise ValueError("Q3 PV window kind is unsupported")
        if not math.isfinite(self.power_kw) or self.power_kw < 0:
            raise ValueError("Q3 PV window power_kw must be finite and non-negative")
        expected = TimeGrid.power_to_energy_kwh(self.power_kw)
        if not math.isclose(self.energy_kwh, expected, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("Q3 PV window energy_kwh must equal power_kw * 1/6 h")


@dataclass(frozen=True)
class Q3PVWindow:
    decision_time: dt.datetime
    snapshot_issue_time: dt.datetime
    points: tuple[Q3PVWindowPoint, ...]
    attachment3_point_count: int
    tail_point_count: int

    def __post_init__(self) -> None:
        if len(self.points) != WINDOW_STEPS:
            raise ValueError("Q3 PV MPC window must contain 144 points")
        if self.attachment3_point_count + self.tail_point_count != len(self.points):
            raise ValueError("Q3 PV window source counts are inconsistent")
        forecast_ids = tuple(point.forecast_id for point in self.points)
        if len(forecast_ids) != len(set(forecast_ids)):
            raise ValueError("Q3 PV window forecast IDs must be unique")
        for step, point in enumerate(self.points, start=1):
            expected = self.decision_time + dt.timedelta(minutes=STEP_MINUTES * step)
            if point.valid_time != expected:
                raise ValueError("Q3 PV window must cover decision+10min through decision+24h")


def build_q3_pv_window(
    *,
    decision_time: dt.datetime,
    snapshot: PVForecastSnapshot,
    info_set: InfoSet,
    tail_baseline: PVTailBaseline | None = None,
) -> Q3PVWindow:
    """Slice the latest snapshot and fill only its uncovered suffix."""

    _require_ten_minute_time("decision_time", decision_time)
    if info_set.decision_time != decision_time:
        raise InputError("PV window InfoSet must match decision_time")
    age = decision_time - snapshot.issue_time
    if age < dt.timedelta(0) or age >= dt.timedelta(hours=SNAPSHOT_REFRESH_HOURS):
        raise InputError("PV window must use the latest visible 6-hour snapshot")

    targets = tuple(
        decision_time + dt.timedelta(minutes=STEP_MINUTES * step)
        for step in range(1, WINDOW_STEPS + 1)
    )
    snapshot_by_time = {point.valid_time: point for point in snapshot.points}
    attachment_points: list[Q3PVWindowPoint] = []
    tail_targets: list[dt.datetime] = []
    for target in targets:
        point = snapshot_by_time.get(target)
        if point is None:
            if target <= snapshot.coverage_end:
                raise InputError("PV snapshot has an internal coverage gap")
            tail_targets.append(target)
        else:
            attachment_points.append(
                Q3PVWindowPoint(
                    forecast_id=point.forecast_id,
                    kind="pv_attachment3",
                    valid_time=point.valid_time,
                    power_kw=point.power_kw,
                    energy_kwh=point.energy_kwh,
                )
            )

    tail_points: list[Q3PVWindowPoint] = []
    if tail_targets:
        if tail_baseline is None:
            raise PendingDecisionError(
                [TAIL_DECISION_ID],
                "HORIZON-B requires an approved PV tail baseline for the uncovered suffix",
            )
        predicted = tail_baseline.predict(
            decision_time=decision_time,
            target_slot_ends=tuple(tail_targets),
            info_set=info_set,
        )
        if predicted.decision_time != decision_time:
            raise InputError("tail baseline returned a different decision_time")
        returned_targets = tuple(point.valid_time for point in predicted.points)
        if returned_targets != tuple(tail_targets):
            raise InputError("tail baseline must return exactly the requested uncovered suffix")
        for point in predicted.points:
            if point.valid_time <= snapshot.coverage_end:
                raise InputError("tail baseline cannot overwrite attachment-3 coverage")
            tail_points.append(
                Q3PVWindowPoint(
                    forecast_id=point.forecast_id,
                    kind="pv_tail",
                    valid_time=point.valid_time,
                    power_kw=point.power_kw,
                    energy_kwh=point.energy_kwh,
                )
            )

    by_time = {point.valid_time: point for point in (*attachment_points, *tail_points)}
    points = tuple(by_time[target] for target in targets)
    return Q3PVWindow(
        decision_time=decision_time,
        snapshot_issue_time=snapshot.issue_time,
        points=points,
        attachment3_point_count=len(attachment_points),
        tail_point_count=len(tail_points),
    )
