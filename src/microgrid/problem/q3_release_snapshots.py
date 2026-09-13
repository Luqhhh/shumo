"""Build auditable Q3 load/PV snapshots at approved release times."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..schemas import InputError
from .contracts import InfoItem, InfoSet
from .q3_load_snapshot import LoadForecastSnapshot, create_load_forecast_snapshot
from .q3_pv_forecast import PV_ACTUAL_KIND, combine_visible_pv_forecasts
from .q3_pv_snapshot import PVForecastSnapshot, create_pv_forecast_snapshot
from .q3_resampling import PVBoundaryProxy

_RELEASE_HOURS = (0, 6, 12, 18)


@dataclass(frozen=True)
class Q3ReleaseSnapshots:
    """The immutable load and attachment-3 PV versions issued together."""

    issue_time: dt.datetime
    load: LoadForecastSnapshot
    pv: PVForecastSnapshot

    def __post_init__(self) -> None:
        if self.load.issue_time != self.issue_time or self.pv.issue_time != self.issue_time:
            raise ValueError("Q3 release snapshots must share one issue time")


def _boundary_proxy(info_set: InfoSet) -> PVBoundaryProxy:
    candidates = tuple(
        item
        for item in info_set.visible_items
        if item.kind == PV_ACTUAL_KIND and item.valid_time == info_set.decision_time
    )
    if len(candidates) != 1:
        raise InputError("Q3 release requires exactly one actual-PV boundary observation")
    item = candidates[0]
    if item.available_at != item.valid_time:
        raise InputError("actual PV boundary must become visible at its right endpoint")
    return PVBoundaryProxy(
        interval_end=info_set.decision_time,
        mean_power_kw=float(item.value),
        source_ref=item.source_ref,
    )


def build_q3_release_snapshots(
    *,
    issue_time: dt.datetime,
    raw_info_items: tuple[InfoItem, ...],
    load_data_version: str,
) -> Q3ReleaseSnapshots:
    """Create one LOAD-A and VERSION-B/WEIGHT-B/RESAMPLE-LIN release.

    The raw archive is reduced to ``available_at <= issue_time`` before any
    predictor, weight calculation, boundary selection or resampling occurs.
    """

    if issue_time.time() not in tuple(dt.time(hour) for hour in _RELEASE_HOURS):
        raise InputError("Q3 snapshots can only be issued at 00:00/06:00/12:00/18:00")
    if not load_data_version.strip():
        raise InputError("Q3 load_data_version must be non-empty")
    info_set = InfoSet.from_raw(issue_time, raw_info_items)
    load = create_load_forecast_snapshot(info_set, data_version=load_data_version)
    combined_pv = combine_visible_pv_forecasts(info_set)
    pv = create_pv_forecast_snapshot(
        combined_pv,
        boundary_proxy=_boundary_proxy(info_set),
        method="linear",
    )
    return Q3ReleaseSnapshots(issue_time=issue_time, load=load, pv=pv)


def build_q3_snapshot_catalog(
    *,
    issue_times: tuple[dt.datetime, ...],
    raw_info_items: tuple[InfoItem, ...],
    load_data_version: str,
) -> tuple[Q3ReleaseSnapshots, ...]:
    """Build an ordered set of independently causal release snapshots."""

    if not issue_times:
        raise InputError("Q3 snapshot catalog requires at least one issue time")
    if issue_times != tuple(sorted(issue_times)) or len(issue_times) != len(set(issue_times)):
        raise InputError("Q3 snapshot issue times must be unique and increasing")
    return tuple(
        build_q3_release_snapshots(
            issue_time=issue,
            raw_info_items=raw_info_items,
            load_data_version=load_data_version,
        )
        for issue in issue_times
    )
