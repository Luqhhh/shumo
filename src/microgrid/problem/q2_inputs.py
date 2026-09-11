"""Typed, causal input adaptation for Q2 and Q4-2.

This module interprets already-read source records on the shared internal
time grid. It does not choose a forecast, optimization, or settlement model.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from ..schemas import InputError
from .contracts import TimeGrid


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
