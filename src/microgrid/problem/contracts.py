"""Shared Stage 1 domain contracts.

These contracts encode the pre-division shared semantics supplied by the team:

* natural-day grid: 144 ten-minute action intervals and 145 boundaries;
* source power is aligned to the interval *right* endpoint and treated as the
  representative average power of the preceding ten minutes;
* bus-side charge/discharge energy, battery-internal state of charge,
  one-way efficiency 0.9 in each direction;
* January is a historical initialization period with battery standby
  (zero action) starting from 6000 kWh; February onwards is continuous,
  with no daily reset;
* causal information sets: an item is visible only when
  ``available_at <= decision_time``.

Template mapping details remain a separate, formally unapproved question.
These contracts are engineering-level shared semantics, not a Q1-Q4 model.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..timekeys import parse_time_label

STEPS_PER_DAY = 144
STEP_MINUTES = 10
STEP_HOURS = STEP_MINUTES / 60.0
ENERGY_CAPACITY_KWH = 12_000.0
SOC_MIN_KWH = 1_200.0
SOC_MAX_KWH = 10_800.0
MAX_BUS_POWER_KW = 5_000.0
MAX_BUS_ENERGY_KWH = MAX_BUS_POWER_KW * STEP_HOURS
CHARGE_EFFICIENCY = 0.9
DISCHARGE_EFFICIENCY = 0.9
ROUND_TRIP_EFFICIENCY = CHARGE_EFFICIENCY * DISCHARGE_EFFICIENCY
INITIAL_SOC_KWH = 6_000.0
ENERGY_ABS_TOL_KWH = 1e-6
ENERGY_REL_TOL = 0.0  # explicit policy: absolute kWh tolerance only


def _require_finite(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a real number")
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite, got {value!r}")


@dataclass(frozen=True)
class CaseContext:
    """Immutable inputs available to one formal case runner."""

    repo_root: Path
    case_id: str
    run_id: str | None = None
    output_dir: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    is_synthetic: bool = False

    def resolved_output_dir(self) -> Path:
        if self.output_dir is not None:
            return self.output_dir
        run_part = self.run_id or "unassigned"
        return self.repo_root / "outputs" / "runs" / self.case_id / run_part


@dataclass(frozen=True)
class CaseResult:
    """Formal case result metadata plus a uniform interval-level payload.

    The actual result files are kept as paths so the release guard can verify
    the selected run manifest and files independently of this object.  Model
    code must put purchase, battery action and storage trajectory data into the
    common ``IntervalResult`` sequence, not into ad-hoc ``metadata`` shapes.
    """

    case_id: str
    run_id: str
    status: str
    is_synthetic: bool = False
    result_files: tuple[Path, ...] = ()
    intervals: tuple[IntervalResult, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        keys = [(interval.day, interval.slot) for interval in self.intervals]
        if keys != sorted(keys):
            raise ValueError("intervals must be ordered by (day, slot)")
        if len(keys) != len(set(keys)):
            raise ValueError("intervals contain duplicate (day, slot) keys")


class CaseRunner(Protocol):
    """Callable signature for a problem runner entry point."""

    def __call__(self, context: CaseContext) -> CaseResult: ...


@dataclass(frozen=True)
class TimeInterval:
    """One ten-minute interval on the natural-day grid."""

    day: _dt.date
    index: int
    start: _dt.datetime
    end: _dt.datetime

    def __post_init__(self) -> None:
        if not 0 <= self.index < STEPS_PER_DAY:
            raise ValueError(f"interval index must be in [0, {STEPS_PER_DAY})")
        expected_start = _dt.datetime.combine(self.day, _dt.time()) + _dt.timedelta(
            minutes=STEP_MINUTES * self.index
        )
        if self.start != expected_start:
            raise ValueError("interval start is not aligned with its day and slot index")
        if self.end - self.start != _dt.timedelta(minutes=STEP_MINUTES):
            raise ValueError("interval duration must be exactly ten minutes")


class TimeGrid:
    """Natural-day 144-interval grid and right-endpoint input alignment."""

    steps_per_day = STEPS_PER_DAY
    step_minutes = STEP_MINUTES
    step_hours = STEP_HOURS

    def boundary(self, day: _dt.date, boundary_index: int) -> _dt.datetime:
        if not 0 <= boundary_index <= STEPS_PER_DAY:
            raise ValueError(f"boundary index must be in [0, {STEPS_PER_DAY}]")
        return _dt.datetime.combine(day, _dt.time()) + _dt.timedelta(
            minutes=STEP_MINUTES * boundary_index
        )

    def interval(self, day: _dt.date, interval_index: int) -> TimeInterval:
        if not 0 <= interval_index < STEPS_PER_DAY:
            raise ValueError(f"interval index must be in [0, {STEPS_PER_DAY})")
        return TimeInterval(
            day=day,
            index=interval_index,
            start=self.boundary(day, interval_index),
            end=self.boundary(day, interval_index + 1),
        )

    def intervals_for_day(self, day: _dt.date) -> tuple[TimeInterval, ...]:
        return tuple(self.interval(day, index) for index in range(STEPS_PER_DAY))

    def boundaries_for_day(self, day: _dt.date) -> tuple[_dt.datetime, ...]:
        return tuple(self.boundary(day, index) for index in range(STEPS_PER_DAY + 1))

    def interval_from_right_endpoint(self, day: _dt.date, raw_label: str) -> TimeInterval:
        """Align an input label to the interval whose *right* endpoint it is.

        Examples from the team's D_TIME_INTERNAL decision::

            0:10   -> [00:00, 00:10)
            0:20   -> [00:10, 00:20)
            0:00+1 -> [23:50, 次日 00:00)

        This is input alignment only.  It says nothing about official result
        template labels such as ``0:10-0:20`` and ``0:00-0:10+1``.
        """

        parsed = parse_time_label(raw_label, base_date=day)
        end = _dt.datetime.combine(day, _dt.time()) + _dt.timedelta(
            days=parsed.day_offset, minutes=parsed.minute_of_day
        )
        start = end - _dt.timedelta(minutes=STEP_MINUTES)
        start_minutes = start.hour * 60 + start.minute
        if start_minutes % STEP_MINUTES != 0:
            raise ValueError(f"right endpoint is not on the 10-minute grid: {raw_label!r}")
        return TimeInterval(
            day=start.date(),
            index=start_minutes // STEP_MINUTES,
            start=start,
            end=end,
        )

    @staticmethod
    def power_to_energy_kwh(power_kw: float, minutes: int = STEP_MINUTES) -> float:
        if minutes <= 0:
            raise ValueError("minutes must be positive")
        _require_finite("power_kw", power_kw)
        return float(power_kw) * minutes / 60.0


@dataclass(frozen=True)
class BatteryState:
    """Battery-internal energy at an interval boundary, in kWh."""

    energy_kwh: float
    capacity_kwh: float = ENERGY_CAPACITY_KWH
    min_energy_kwh: float = SOC_MIN_KWH
    max_energy_kwh: float = SOC_MAX_KWH

    def __post_init__(self) -> None:
        for name in ("energy_kwh", "capacity_kwh", "min_energy_kwh", "max_energy_kwh"):
            _require_finite(name, getattr(self, name))
        if self.capacity_kwh <= 0:
            raise ValueError("capacity_kwh must be positive")
        if self.min_energy_kwh < 0:
            raise ValueError("min_energy_kwh must be non-negative")
        if self.max_energy_kwh > self.capacity_kwh:
            raise ValueError("max_energy_kwh cannot exceed capacity_kwh")
        if self.min_energy_kwh > self.max_energy_kwh:
            raise ValueError("min_energy_kwh cannot exceed max_energy_kwh")
        if not self.min_energy_kwh <= self.energy_kwh <= self.max_energy_kwh:
            raise ValueError(
                "battery energy is outside the approved bounds: "
                f"{self.energy_kwh} not in [{self.min_energy_kwh}, {self.max_energy_kwh}]"
            )

    @property
    def soc(self) -> float:
        """State of charge as a dimensionless ratio, not a percentage."""

        return self.energy_kwh / self.capacity_kwh


@dataclass(frozen=True)
class BatteryAction:
    """Bus-side interval charge/discharge energy, both non-negative.

    Simultaneous charge and discharge is rejected here rather than being left
    ambiguous.  The 5000 kW limit is interpreted on the bus side, so each
    direction is bounded by 5000 kW * 1/6 h.
    """

    charge_kwh: float = 0.0
    discharge_kwh: float = 0.0
    max_bus_energy_kwh: float = MAX_BUS_ENERGY_KWH

    def __post_init__(self) -> None:
        for name in ("charge_kwh", "discharge_kwh", "max_bus_energy_kwh"):
            _require_finite(name, getattr(self, name))
        if self.charge_kwh < 0 or self.discharge_kwh < 0:
            raise ValueError("charge_kwh and discharge_kwh must be non-negative")
        if self.charge_kwh > self.max_bus_energy_kwh:
            raise ValueError("charge_kwh exceeds the bus-side power limit")
        if self.discharge_kwh > self.max_bus_energy_kwh:
            raise ValueError("discharge_kwh exceeds the bus-side power limit")
        if self.charge_kwh > 0 and self.discharge_kwh > 0:
            raise ValueError("simultaneous charge and discharge is not allowed in this contract")

    @property
    def charge_kw(self) -> float:
        return self.charge_kwh / STEP_HOURS

    @property
    def discharge_kw(self) -> float:
        return self.discharge_kwh / STEP_HOURS


def apply_battery_action(
    state: BatteryState,
    action: BatteryAction,
    *,
    charge_efficiency: float = CHARGE_EFFICIENCY,
    discharge_efficiency: float = DISCHARGE_EFFICIENCY,
) -> BatteryState:
    """Apply the approved bus-side/battery-internal transition, without clipping.

    ``E_next = E + eta_c * c - d / eta_d``.  If the result violates the
    approved bounds, the returned state construction raises; callers may not
    silently clip it.
    """

    if not 0 < charge_efficiency <= 1:
        raise ValueError("charge_efficiency must be in (0, 1]")
    if not 0 < discharge_efficiency <= 1:
        raise ValueError("discharge_efficiency must be in (0, 1]")
    next_energy = (
        state.energy_kwh
        + charge_efficiency * action.charge_kwh
        - action.discharge_kwh / discharge_efficiency
    )
    return BatteryState(
        energy_kwh=next_energy,
        capacity_kwh=state.capacity_kwh,
        min_energy_kwh=state.min_energy_kwh,
        max_energy_kwh=state.max_energy_kwh,
    )


@dataclass(frozen=True)
class IntervalResult:
    """One ten-minute result row on the shared natural-day grid.

    This is the uniform carrier for planned/adjusted/emergency purchase,
    bus-side battery action and the state trajectory.  It intentionally does
    not prescribe an optimisation model.
    """

    day: _dt.date
    slot: int
    load_kw: float
    pv_kw: float
    planned_purchase_kwh: float
    adjusted_purchase_kwh: float
    emergency_purchase_kwh: float
    action: BatteryAction
    state_start: BatteryState
    state_end: BatteryState
    source_ref: str = ""
    pv_used_kwh: float = 0.0

    def __post_init__(self) -> None:
        if not 0 <= self.slot < STEPS_PER_DAY:
            raise ValueError(f"slot must be in [0, {STEPS_PER_DAY})")
        for name in (
            "load_kw",
            "pv_kw",
            "planned_purchase_kwh",
            "adjusted_purchase_kwh",
            "emergency_purchase_kwh",
            "pv_used_kwh",
        ):
            _require_finite(name, getattr(self, name))
        if self.load_kw < 0 or self.pv_kw < 0:
            raise ValueError("load_kw and pv_kw must be non-negative")
        for name in ("planned_purchase_kwh", "adjusted_purchase_kwh", "emergency_purchase_kwh"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.pv_used_kwh < 0:
            raise ValueError("pv_used_kwh must be non-negative")
        if self.pv_used_kwh > self.pv_kw / 6.0 + ENERGY_ABS_TOL_KWH:
            raise ValueError("pv_used_kwh exceeds the interval PV forecast")
        if (
            self.state_start.capacity_kwh,
            self.state_start.min_energy_kwh,
            self.state_start.max_energy_kwh,
        ) != (
            self.state_end.capacity_kwh,
            self.state_end.min_energy_kwh,
            self.state_end.max_energy_kwh,
        ):
            raise ValueError("state_start and state_end must use the same battery parameters")
        expected = apply_battery_action(self.state_start, self.action)
        if not math.isclose(
            expected.energy_kwh,
            self.state_end.energy_kwh,
            rel_tol=ENERGY_REL_TOL,
            abs_tol=ENERGY_ABS_TOL_KWH,
        ):
            raise ValueError("state_end is inconsistent with the shared battery transition")

    @property
    def interval_key(self) -> tuple[_dt.date, int]:
        return (self.day, self.slot)

    @property
    def adjustment_delta_kwh(self) -> float:
        return self.adjusted_purchase_kwh - self.planned_purchase_kwh


@dataclass(frozen=True)
class InfoItem:
    """One information item with explicit availability and validity times."""

    kind: str
    available_at: _dt.datetime
    valid_time: _dt.datetime
    value: float | int | str | None = None
    source_ref: str = ""


@dataclass(frozen=True)
class InfoSet:
    """A causal *view* that cannot store future items.

    Construct planner inputs with :meth:`from_raw`; it filters first and only
    the visible subset is stored.  Direct construction with a future item is
    rejected.  This is intentionally a stronger guarantee than a filtering
    method on top of an object that still exposes all raw records.
    """

    decision_time: _dt.datetime
    visible_items: tuple[InfoItem, ...] = ()

    def __post_init__(self) -> None:
        for item in self.visible_items:
            if item.available_at > self.decision_time:
                raise ValueError(
                    f"future item {item.kind!r} is not available at {self.decision_time}"
                )

    @classmethod
    def from_raw(cls, decision_time: _dt.datetime, items: tuple[InfoItem, ...]) -> InfoSet:
        visible = tuple(item for item in items if item.available_at <= decision_time)
        return cls(decision_time=decision_time, visible_items=visible)

    def is_visible(self, item: InfoItem) -> bool:
        return item.available_at <= self.decision_time


@dataclass(frozen=True)
class PurchasePlan:
    """Separate purchase quantities; adjusted and delta are not mixed."""

    planned_kwh: float
    adjusted_kwh: float
    emergency_kwh: float = 0.0

    def __post_init__(self) -> None:
        for name in ("planned_kwh", "adjusted_kwh", "emergency_kwh"):
            _require_finite(name, getattr(self, name))
        if self.planned_kwh < 0 or self.adjusted_kwh < 0 or self.emergency_kwh < 0:
            raise ValueError("purchase quantities must be non-negative")

    @property
    def adjustment_delta_kwh(self) -> float:
        """Signed adjustment increment, not the adjusted total."""

        return self.adjusted_kwh - self.planned_kwh


@dataclass(frozen=True)
class CostBreakdown:
    """Cost components in CNY; this object does not choose a pricing formula."""

    planned_cost_cny: float = 0.0
    adjustment_cost_cny: float = 0.0
    emergency_cost_cny: float = 0.0

    def __post_init__(self) -> None:
        for name in ("planned_cost_cny", "adjustment_cost_cny", "emergency_cost_cny"):
            _require_finite(name, getattr(self, name))

    @property
    def total_cost_cny(self) -> float:
        return self.planned_cost_cny + self.adjustment_cost_cny + self.emergency_cost_cny
