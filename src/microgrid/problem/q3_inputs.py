"""Method-neutral Q3 adapter for the versioned attachment-3 forecasts.

This module preserves every ``issue_time`` / ``valid_time`` pair and exposes
only an ``InfoSet``-filtered view.  It deliberately does not choose between
multiple visible versions for the same valid time and does not resample hourly
forecasts to ten-minute slots; both remain behind human decisions.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass
from pathlib import Path

from ..dataio import read_forecasts, sha256_file
from ..schemas import ForecastRecord, InputError
from .contracts import InfoItem, InfoSet

EXPECTED_FORECAST_LEADS = tuple(range(1, 25))


@dataclass(frozen=True)
class Q3ForecastVersion:
    """One complete 24-hour PV forecast published at one issue time."""

    issue_time: _dt.datetime
    records: tuple[ForecastRecord, ...]

    def __post_init__(self) -> None:
        if not self.records:
            raise InputError(f"forecast version {self.issue_time} is empty")
        if any(record.issue_time != self.issue_time for record in self.records):
            raise InputError("forecast version mixes multiple issue times")

        leads = tuple(record.lead_hours for record in self.records)
        if len(leads) != len(set(leads)):
            raise InputError(f"forecast version {self.issue_time} contains duplicate leads")
        if leads != EXPECTED_FORECAST_LEADS:
            raise InputError(
                f"forecast version {self.issue_time} must contain ordered leads 1..24, got {leads}"
            )

        for record in self.records:
            expected_valid_time = self.issue_time + _dt.timedelta(hours=record.lead_hours)
            if record.valid_time != expected_valid_time:
                raise InputError(
                    f"forecast lead {record.lead_hours} at {self.issue_time} has inconsistent "
                    f"valid_time {record.valid_time}"
                )
            if not math.isfinite(record.pv_forecast_kw):
                raise InputError(
                    f"forecast lead {record.lead_hours} at {self.issue_time} is not finite"
                )

    def as_info_items(self) -> tuple[InfoItem, ...]:
        """Convert without selecting, interpolating or changing forecast values."""

        return tuple(
            InfoItem(
                kind="pv_forecast_kw",
                available_at=record.issue_time,
                valid_time=record.valid_time,
                value=record.pv_forecast_kw,
                source_ref=record.source_ref,
            )
            for record in self.records
        )


@dataclass(frozen=True)
class Q3ForecastArchive:
    """All attachment-3 forecast versions, ordered by issue time."""

    source_file: str
    source_sha256: str
    versions: tuple[Q3ForecastVersion, ...]

    def __post_init__(self) -> None:
        issue_times = tuple(version.issue_time for version in self.versions)
        if not issue_times:
            raise InputError("attachment3 contains no usable forecast versions")
        if issue_times != tuple(sorted(issue_times)):
            raise InputError("forecast versions must be ordered by issue_time")
        if len(issue_times) != len(set(issue_times)):
            raise InputError("forecast archive contains duplicate issue_time versions")

    def raw_info_items(self) -> tuple[InfoItem, ...]:
        """Return all versions so ``InfoSet`` remains the only visibility filter."""

        return tuple(item for version in self.versions for item in version.as_info_items())

    def info_set_at(self, decision_time: _dt.datetime) -> InfoSet:
        """Build a causal view without choosing a preferred visible version."""

        return InfoSet.from_raw(decision_time, self.raw_info_items())


def load_q3_forecast_archive(attachment3_path: str | Path) -> Q3ForecastArchive:
    """Read attachment 3 and preserve every complete forecast publication."""

    path = Path(attachment3_path)
    records = read_forecasts(path)
    grouped: dict[_dt.datetime, list[ForecastRecord]] = {}
    for record in records:
        grouped.setdefault(record.issue_time, []).append(record)

    versions = tuple(
        Q3ForecastVersion(
            issue_time=issue_time,
            records=tuple(sorted(grouped[issue_time], key=lambda record: record.lead_hours)),
        )
        for issue_time in sorted(grouped)
    )
    return Q3ForecastArchive(
        source_file=path.name,
        source_sha256=sha256_file(path),
        versions=versions,
    )
