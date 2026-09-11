"""Typed, causal input adaptation for Q2 and Q4-2.

This module interprets already-read source records on the shared internal
time grid. It does not choose a forecast, optimization, or settlement model.
"""

from __future__ import annotations

import datetime as dt
import math
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from openpyxl.utils.exceptions import InvalidFileException

from ..dataio import read_attachment1, read_wide_attachment, sha256_file
from ..schemas import InputError
from .contracts import InfoItem, InfoSet, TimeGrid

_FIXED_PRICE_SENTINEL_DAY = dt.date(2000, 1, 1)
_T = TypeVar("_T")


def _require_nonnegative_finite(name: str, value: float, *, source_ref: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{name} must be numeric at {source_ref}")
    if not math.isfinite(float(value)) or float(value) < 0:
        raise InputError(f"{name} must be finite and non-negative at {source_ref}")


@dataclass(frozen=True)
class FixedPricePoint:
    slot: int
    price_cny_per_kwh: float
    source_ref: str

    def __post_init__(self) -> None:
        if not 0 <= self.slot < TimeGrid.steps_per_day:
            raise InputError(f"fixed-price slot out of range: {self.slot}")
        _require_nonnegative_finite(
            "price_cny_per_kwh",
            self.price_cny_per_kwh,
            source_ref=self.source_ref,
        )


@dataclass(frozen=True)
class ActualInterval:
    day: dt.date
    slot: int
    start: dt.datetime
    end: dt.datetime
    load_kw: float
    pv_kw: float
    load_source_ref: str
    pv_source_ref: str

    def __post_init__(self) -> None:
        interval = TimeGrid().interval(self.day, self.slot)
        if (self.start, self.end) != (interval.start, interval.end):
            raise InputError(f"actual interval is not aligned: {(self.day, self.slot)}")
        _require_nonnegative_finite(
            "load_kw",
            self.load_kw,
            source_ref=self.load_source_ref,
        )
        _require_nonnegative_finite(
            "pv_kw",
            self.pv_kw,
            source_ref=self.pv_source_ref,
        )

    @property
    def load_kwh(self) -> float:
        return TimeGrid.power_to_energy_kwh(self.load_kw, minutes=10)

    @property
    def pv_kwh(self) -> float:
        return TimeGrid.power_to_energy_kwh(self.pv_kw, minutes=10)


@dataclass(frozen=True)
class VariablePricePoint:
    day: dt.date
    slot: int
    start: dt.datetime
    end: dt.datetime
    price_cny_per_kwh: float
    source_ref: str

    def __post_init__(self) -> None:
        interval = TimeGrid().interval(self.day, self.slot)
        if (self.start, self.end) != (interval.start, interval.end):
            raise InputError(f"variable-price interval is not aligned: {(self.day, self.slot)}")
        _require_nonnegative_finite(
            "price_cny_per_kwh",
            self.price_cny_per_kwh,
            source_ref=self.source_ref,
        )


@dataclass(frozen=True)
class Q2InputBundle:
    fixed_prices: tuple[FixedPricePoint, ...]
    actuals: tuple[ActualInterval, ...]
    input_hashes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class VariablePriceBundle:
    prices: tuple[VariablePricePoint, ...]
    input_hashes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _WideValue:
    day: dt.date
    slot: int
    start: dt.datetime
    end: dt.datetime
    value: float
    source_ref: str


def _require_file(logical_name: str, path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_file():
        raise InputError(f"{logical_name} path is not a file: {candidate}")
    return candidate


def _source_ref(record: dict[str, Any] | object) -> str:
    if isinstance(record, dict):
        source_file = str(record["source_file"])
        sheet = str(record["sheet_name"])
        cell = str(record["cell_ref"])
        digest = str(record["source_hash"])
    else:
        source_file = str(record.source_file)
        sheet = str(record.sheet_name)
        cell = str(record.cell_ref)
        digest = str(record.source_hash)
    return f"{Path(source_file).name}!{sheet}!{cell} sha256={digest[:12]}"


def _call_reader(logical_name: str, path: Path, reader: Callable[[], _T]) -> _T:
    try:
        return reader()
    except InputError as exc:
        raise InputError(f"{logical_name} path={path}: {exc}") from exc
    except (
        OSError,
        KeyError,
        ValueError,
        zipfile.BadZipFile,
        InvalidFileException,
    ) as exc:
        raise InputError(f"{logical_name} path={path}: {exc}") from exc


def _parse_source_day(logical_name: str, record: dict[str, Any]) -> dt.date:
    raw = record.get("source_date")
    try:
        return dt.date.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise InputError(
            f"{logical_name} has invalid source_date={raw!r} at {_source_ref(record)}"
        ) from exc


def _wide_values(
    *,
    logical_name: str,
    path: Path,
    sheet_name: str,
    kind: str,
    unit: str,
) -> dict[tuple[dt.date, int], _WideValue]:
    records = _call_reader(
        logical_name,
        path,
        lambda: read_wide_attachment(
            path,
            sheet_name=sheet_name,
            kind=kind,
            unit=unit,
        ),
    )
    if not records:
        raise InputError(f"{logical_name} path={path} sheet={sheet_name}: no records")

    grid = TimeGrid()
    values: dict[tuple[dt.date, int], _WideValue] = {}
    for record in records:
        ref = _source_ref(record)
        day = _parse_source_day(logical_name, record)
        raw_label = str(record.get("raw_time_label", ""))
        try:
            interval = grid.interval_from_right_endpoint(day, raw_label)
        except (InputError, ValueError) as exc:
            raise InputError(f"{logical_name} has bad time label at {ref}: {exc}") from exc
        if interval.day != day:
            raise InputError(
                f"{logical_name} right endpoint crosses before its source day at {ref}"
            )
        parsed_raw = record.get("parsed_timestamp")
        if parsed_raw is None:
            raise InputError(f"{logical_name} missing parsed_timestamp at {ref}")
        try:
            parsed_end = dt.datetime.fromisoformat(str(parsed_raw))
        except ValueError as exc:
            raise InputError(f"{logical_name} has invalid parsed_timestamp at {ref}") from exc
        if parsed_end != interval.end:
            raise InputError(
                f"{logical_name} parsed_timestamp disagrees with right endpoint at {ref}"
            )

        raw_value = record.get("value")
        _require_nonnegative_finite(logical_name, raw_value, source_ref=ref)
        key = (interval.day, interval.index)
        if key in values:
            raise InputError(f"{logical_name} duplicate key={key} at {ref}")
        values[key] = _WideValue(
            day=interval.day,
            slot=interval.index,
            start=interval.start,
            end=interval.end,
            value=float(raw_value),
            source_ref=ref,
        )

    _require_complete_days(logical_name, path, sheet_name, values)
    return values


def _require_complete_days(
    logical_name: str,
    path: Path,
    sheet_name: str,
    values: dict[tuple[dt.date, int], _WideValue],
) -> None:
    expected = set(range(TimeGrid.steps_per_day))
    days = sorted({day for day, _slot in values})
    for day in days:
        observed = {slot for value_day, slot in values if value_day == day}
        if observed != expected:
            missing = sorted(expected - observed)
            extra = sorted(observed - expected)
            raise InputError(
                f"{logical_name} path={path} sheet={sheet_name} day={day} "
                f"missing_slots={missing} extra_slots={extra}"
            )


def _fixed_prices(path: Path) -> tuple[FixedPricePoint, ...]:
    records = _call_reader("attachment1", path, lambda: read_attachment1(path))
    price_records = [record for record in records if record.kind == "price_cny_per_kwh"]
    points: dict[int, FixedPricePoint] = {}
    grid = TimeGrid()
    for record in price_records:
        ref = _source_ref(record)
        try:
            interval = grid.interval_from_right_endpoint(
                _FIXED_PRICE_SENTINEL_DAY,
                record.raw_time_label,
            )
        except (InputError, ValueError) as exc:
            raise InputError(f"attachment1 has bad time label at {ref}: {exc}") from exc
        if interval.day != _FIXED_PRICE_SENTINEL_DAY:
            raise InputError(f"attachment1 right endpoint crosses before the sentinel day at {ref}")
        point = FixedPricePoint(
            slot=interval.index,
            price_cny_per_kwh=record.value,
            source_ref=ref,
        )
        if point.slot in points:
            raise InputError(f"attachment1 duplicate slot={point.slot} at {ref}")
        points[point.slot] = point

    expected = set(range(TimeGrid.steps_per_day))
    if set(points) != expected:
        raise InputError(
            f"attachment1 path={path} missing_slots={sorted(expected - set(points))} "
            f"extra_slots={sorted(set(points) - expected)}"
        )
    return tuple(points[slot] for slot in range(TimeGrid.steps_per_day))


def load_q2_inputs(
    *,
    attachment1_path: str | Path,
    attachment2_path: str | Path,
    load_sheet_name: str,
    pv_sheet_name: str,
) -> Q2InputBundle:
    attachment1 = _require_file("attachment1", attachment1_path)
    attachment2 = _require_file("attachment2", attachment2_path)
    fixed_prices = _fixed_prices(attachment1)
    loads = _wide_values(
        logical_name="attachment2 load",
        path=attachment2,
        sheet_name=load_sheet_name,
        kind="load_kw",
        unit="kW",
    )
    pvs = _wide_values(
        logical_name="attachment2 pv",
        path=attachment2,
        sheet_name=pv_sheet_name,
        kind="pv_actual_kw",
        unit="kW",
    )
    if set(loads) != set(pvs):
        raise InputError(
            f"attachment2 load/pv grid mismatch path={attachment2} "
            f"load_only={sorted(set(loads) - set(pvs))[:10]} "
            f"pv_only={sorted(set(pvs) - set(loads))[:10]}"
        )

    actuals = tuple(
        ActualInterval(
            day=loads[key].day,
            slot=loads[key].slot,
            start=loads[key].start,
            end=loads[key].end,
            load_kw=loads[key].value,
            pv_kw=pvs[key].value,
            load_source_ref=loads[key].source_ref,
            pv_source_ref=pvs[key].source_ref,
        )
        for key in sorted(loads)
    )
    return Q2InputBundle(
        fixed_prices=fixed_prices,
        actuals=actuals,
        input_hashes=tuple(
            sorted(
                (
                    ("attachment1", sha256_file(attachment1)),
                    ("attachment2", sha256_file(attachment2)),
                )
            )
        ),
    )


def load_q4_2_prices(
    *,
    attachment4_path: str | Path,
    price_sheet_name: str,
) -> VariablePriceBundle:
    attachment4 = _require_file("attachment4", attachment4_path)
    values = _wide_values(
        logical_name="attachment4 price",
        path=attachment4,
        sheet_name=price_sheet_name,
        kind="price_actual_cny_per_kwh",
        unit="元/kWh",
    )
    prices = tuple(
        VariablePricePoint(
            day=value.day,
            slot=value.slot,
            start=value.start,
            end=value.end,
            price_cny_per_kwh=value.value,
            source_ref=value.source_ref,
        )
        for _key, value in sorted(values.items())
    )
    return VariablePriceBundle(
        prices=prices,
        input_hashes=(("attachment4", sha256_file(attachment4)),),
    )


def require_matching_q4_2_grid(
    q2_inputs: Q2InputBundle,
    variable_prices: VariablePriceBundle,
) -> None:
    actual_keys = {(item.day, item.slot) for item in q2_inputs.actuals}
    price_keys = {(item.day, item.slot) for item in variable_prices.prices}
    if actual_keys != price_keys:
        raise InputError(
            "Q2/Attachment 4 grid mismatch: "
            f"actual_only={sorted(actual_keys - price_keys)[:10]} "
            f"price_only={sorted(price_keys - actual_keys)[:10]}"
        )


def historical_info_items(
    q2_inputs: Q2InputBundle,
    *,
    variable_prices: VariablePriceBundle | None = None,
) -> tuple[InfoItem, ...]:
    items: list[InfoItem] = []
    for actual in q2_inputs.actuals:
        items.extend(
            (
                InfoItem(
                    kind="load_actual_kw",
                    available_at=actual.end,
                    valid_time=actual.end,
                    value=actual.load_kw,
                    source_ref=actual.load_source_ref,
                ),
                InfoItem(
                    kind="pv_actual_kw",
                    available_at=actual.end,
                    valid_time=actual.end,
                    value=actual.pv_kw,
                    source_ref=actual.pv_source_ref,
                ),
            )
        )
    if variable_prices is not None:
        for price in variable_prices.prices:
            items.append(
                InfoItem(
                    kind="price_actual_cny_per_kwh",
                    available_at=price.end,
                    valid_time=price.end,
                    value=price.price_cny_per_kwh,
                    source_ref=price.source_ref,
                )
            )
    return tuple(sorted(items, key=lambda item: (item.valid_time, item.kind, item.source_ref)))


def info_set_at(
    decision_time: dt.datetime,
    items: tuple[InfoItem, ...],
) -> InfoSet:
    return InfoSet.from_raw(decision_time, items)
