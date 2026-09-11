"""Adapter from attachment 1 observations to the approved Q1 internal grid.

Only approved D_TIME_INTERNAL semantics are used here.  No objective,
constraint beyond completeness, or solver is selected.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass
from pathlib import Path

from ..dataio import read_attachment1, sha256_file, utc_now
from ..schemas import InputError, ObservationRecord
from .contracts import STEP_MINUTES, STEPS_PER_DAY, TimeGrid, TimeInterval

Q1_KINDS = ("price_cny_per_kwh", "load_kw", "pv_forecast_kw")


@dataclass(frozen=True)
class Q1IntervalInput:
    interval: TimeInterval
    price_cny_per_kwh: float
    load_kw: float
    pv_forecast_kw: float
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("price_cny_per_kwh", "load_kw", "pv_forecast_kw"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InputError(f"Q1 input {name} must be numeric")
            if not math.isfinite(float(value)):
                raise InputError(f"Q1 input {name} must be finite")

    @property
    def slot(self) -> int:
        return self.interval.index

    @property
    def start(self) -> _dt.datetime:
        return self.interval.start

    @property
    def end(self) -> _dt.datetime:
        return self.interval.end

    @property
    def load_kwh(self) -> float:
        return TimeGrid.power_to_energy_kwh(self.load_kw)

    @property
    def pv_forecast_kwh(self) -> float:
        return TimeGrid.power_to_energy_kwh(self.pv_forecast_kw)


@dataclass(frozen=True)
class Q1InputSnapshot:
    reference_day: _dt.date
    source_file: str
    source_sha256: str
    generated_at: str
    intervals: tuple[Q1IntervalInput, ...]

    def __post_init__(self) -> None:
        if len(self.intervals) != STEPS_PER_DAY:
            raise InputError(
                f"Q1 input snapshot needs {STEPS_PER_DAY} intervals, got {len(self.intervals)}"
            )
        slots = [point.slot for point in self.intervals]
        if slots != list(range(STEPS_PER_DAY)):
            raise InputError("Q1 input slots must be ordered 0..143 exactly once")

    @property
    def source_hash(self) -> str:
        return self.source_sha256


def _interval_from_record(record: ObservationRecord, reference_day: _dt.date) -> TimeInterval:
    if record.parsed_day_offset is None or record.parsed_minute_of_day is None:
        raise InputError(
            f"attachment1 record has no parsed time: {record.cell_ref} {record.raw_time_label!r}"
        )
    end = _dt.datetime.combine(reference_day, _dt.time()) + _dt.timedelta(
        days=record.parsed_day_offset,
        minutes=record.parsed_minute_of_day,
    )
    start = end - _dt.timedelta(minutes=STEP_MINUTES)
    start_minutes = start.hour * 60 + start.minute
    if start_minutes % STEP_MINUTES != 0:
        raise InputError(
            f"attachment1 right endpoint is not on the ten-minute grid: {record.raw_time_label!r}"
        )
    interval = TimeInterval(
        day=start.date(),
        index=start_minutes // STEP_MINUTES,
        start=start,
        end=end,
    )
    if interval.day != reference_day:
        raise InputError(
            f"attachment1 record does not belong to reference_day {reference_day}: "
            f"{record.cell_ref} {record.raw_time_label!r}"
        )
    return interval


def load_q1_inputs(
    attachment1_path: str | Path,
    *,
    reference_day: _dt.date,
    expected_kinds: tuple[str, ...] = Q1_KINDS,
) -> Q1InputSnapshot:
    """Read attachment 1 into 144 aligned Q1 input slots.

    Attachment 1 has no calendar date, so the caller must pass an explicit
    internal ``reference_day``.  The original ``source_date`` remains absent.
    """

    path = Path(attachment1_path)
    records = read_attachment1(path)
    by_slot: dict[int, dict[str, ObservationRecord]] = {}
    for record in records:
        if record.kind not in expected_kinds:
            continue
        interval = _interval_from_record(record, reference_day)
        slot_map = by_slot.setdefault(interval.index, {})
        if record.kind in slot_map:
            raise InputError(f"attachment1 has duplicate {record.kind} at slot {interval.index}")
        slot_map[record.kind] = record

    missing: list[str] = []
    intervals: list[Q1IntervalInput] = []
    grid = TimeGrid()
    for slot in range(STEPS_PER_DAY):
        slot_map = by_slot.get(slot, {})
        missing_kinds = [kind for kind in expected_kinds if kind not in slot_map]
        if missing_kinds:
            missing.append(f"slot {slot}: {', '.join(missing_kinds)}")
            continue
        intervals.append(
            Q1IntervalInput(
                interval=grid.interval(reference_day, slot),
                price_cny_per_kwh=float(slot_map["price_cny_per_kwh"].value),
                load_kw=float(slot_map["load_kw"].value),
                pv_forecast_kw=float(slot_map["pv_forecast_kw"].value),
                source_refs=tuple(
                    f"{record.source_file}!{record.sheet_name}!{record.cell_ref}"
                    for record in slot_map.values()
                ),
            )
        )

    if len(intervals) != STEPS_PER_DAY:
        raise InputError("attachment1 is incomplete: " + "; ".join(missing[:8]))

    return Q1InputSnapshot(
        reference_day=reference_day,
        source_file=path.name,
        source_sha256=sha256_file(path),
        generated_at=utc_now(),
        intervals=tuple(intervals),
    )
