"""Causal one-day Q3 rolling orchestration over approved domain components."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ..schemas import InputError
from .contracts import (
    ENERGY_ABS_TOL_KWH,
    STEP_MINUTES,
    STEPS_PER_DAY,
    BatteryState,
    CostBreakdown,
    IntervalResult,
)
from .q2_inputs import ActualInterval
from .q3_controller import execute_q3_window_step
from .q3_plan_ledger import Q3EmergencySettlementEntry, Q3PlanLedger
from .q3_sidecars import PlanSlotForecastLink
from .q3_solver import Q3WindowSolution, solve_q3_window_milp
from .q3_window import Q3WindowInput


class Q3WindowFactory(Protocol):
    """Build one causal window without access to the realized interval."""

    def __call__(
        self,
        *,
        decision_time: dt.datetime,
        battery_state: BatteryState,
        current_ledger: Q3PlanLedger | None,
    ) -> Q3WindowInput: ...


Q3WindowSolver = Callable[[Q3WindowInput], Q3WindowSolution]


@dataclass(frozen=True)
class Q3DayRun:
    """One complete natural-day execution before artifact serialization."""

    day: dt.date
    state_start: BatteryState
    state_end: BatteryState
    intervals: tuple[IntervalResult, ...]
    ledger: Q3PlanLedger
    forecast_links: tuple[PlanSlotForecastLink, ...]
    emergency_entries: tuple[Q3EmergencySettlementEntry, ...]

    def __post_init__(self) -> None:
        keys = tuple((interval.day, interval.slot) for interval in self.intervals)
        expected = tuple((self.day, slot) for slot in range(STEPS_PER_DAY))
        if keys != expected:
            raise ValueError("Q3 day run must contain the ordered 144-slot natural day")
        if self.ledger.day != self.day:
            raise ValueError("Q3 day run ledger is for a different day")
        if tuple(version.issue_time.hour for version in self.ledger.versions) != (0, 6, 12, 18):
            raise ValueError("Q3 day run must retain the 00/06/12/18 plan versions")
        if self.intervals[0].state_start != self.state_start:
            raise ValueError("Q3 day run initial state does not match its first interval")
        if self.intervals[-1].state_end != self.state_end:
            raise ValueError("Q3 day run final state does not match its last interval")
        for previous, current in zip(self.intervals, self.intervals[1:], strict=False):
            if previous.state_end != current.state_start:
                raise ValueError("Q3 day run battery state is discontinuous")

        expected_link_keys = {
            (self.day, version.version, slot)
            for version in self.ledger.versions
            for slot in range(STEPS_PER_DAY)
        }
        actual_link_keys = {
            (link.day, link.version, link.target_slot) for link in self.forecast_links
        }
        if len(actual_link_keys) != len(self.forecast_links):
            raise ValueError("Q3 day run contains duplicate plan forecast links")
        if actual_link_keys != expected_link_keys:
            raise ValueError("Q3 day run forecast links do not cover every plan version")

        emergency_by_slot = {entry.slot: entry for entry in self.emergency_entries}
        if len(emergency_by_slot) != len(self.emergency_entries):
            raise ValueError("Q3 day run contains duplicate emergency entries")
        if any(entry.day != self.day for entry in self.emergency_entries):
            raise ValueError("Q3 day run contains an emergency entry for a different day")
        for interval in self.intervals:
            entry = emergency_by_slot.get(interval.slot)
            if interval.emergency_purchase_kwh > ENERGY_ABS_TOL_KWH:
                if entry is None or not math.isclose(
                    entry.energy_kwh,
                    interval.emergency_purchase_kwh,
                    rel_tol=0.0,
                    abs_tol=ENERGY_ABS_TOL_KWH,
                ):
                    raise ValueError("Q3 day run emergency interval and ledger disagree")
            elif entry is not None:
                raise ValueError("Q3 day run has an emergency ledger entry for zero energy")

    @property
    def cost_breakdown(self) -> CostBreakdown:
        return CostBreakdown(
            planned_cost_cny=self.ledger.planned_cost_cny,
            adjustment_cost_cny=self.ledger.adjustment_cost_cny,
            emergency_cost_cny=math.fsum(entry.cost_cny for entry in self.emergency_entries),
        )


@dataclass(frozen=True)
class Q3PeriodRun:
    """Several consecutive Q3 days with continuous battery state."""

    days: tuple[Q3DayRun, ...]

    def __post_init__(self) -> None:
        if not self.days:
            raise ValueError("Q3 period run must contain at least one day")
        for previous, current in zip(self.days, self.days[1:], strict=False):
            if current.day != previous.day + dt.timedelta(days=1):
                raise ValueError("Q3 period run days must be consecutive")
            if current.state_start != previous.state_end:
                raise ValueError("Q3 period run cannot reset battery state at midnight")

    @property
    def intervals(self) -> tuple[IntervalResult, ...]:
        return tuple(interval for day in self.days for interval in day.intervals)

    @property
    def ledgers(self) -> tuple[Q3PlanLedger, ...]:
        return tuple(day.ledger for day in self.days)

    @property
    def forecast_links(self) -> tuple[PlanSlotForecastLink, ...]:
        return tuple(link for day in self.days for link in day.forecast_links)

    @property
    def emergency_entries(self) -> tuple[Q3EmergencySettlementEntry, ...]:
        return tuple(entry for day in self.days for entry in day.emergency_entries)

    @property
    def state_start(self) -> BatteryState:
        return self.days[0].state_start

    @property
    def state_end(self) -> BatteryState:
        return self.days[-1].state_end

    @property
    def cost_breakdown(self) -> CostBreakdown:
        return CostBreakdown(
            planned_cost_cny=math.fsum(day.cost_breakdown.planned_cost_cny for day in self.days),
            adjustment_cost_cny=math.fsum(
                day.cost_breakdown.adjustment_cost_cny for day in self.days
            ),
            emergency_cost_cny=math.fsum(
                day.cost_breakdown.emergency_cost_cny for day in self.days
            ),
        )


def _actual_day(
    day: dt.date,
    actuals: tuple[ActualInterval, ...],
) -> tuple[ActualInterval, ...]:
    expected = tuple((day, slot) for slot in range(STEPS_PER_DAY))
    actual_keys = tuple((actual.day, actual.slot) for actual in actuals)
    if actual_keys != expected:
        raise InputError("Q3 daily replay requires ordered actual slots 0..143 for one day")
    return actuals


def _links_for_new_version(
    *,
    window: Q3WindowInput,
    ledger: Q3PlanLedger,
    existing: tuple[PlanSlotForecastLink, ...],
) -> tuple[PlanSlotForecastLink, ...]:
    version = ledger.current.version
    points = {point.slot: point for point in window.points if point.day == ledger.day}
    prior = {
        link.target_slot: link
        for link in existing
        if link.day == ledger.day and link.version == version - 1
    }
    issue_slot = (window.decision_time.hour * 60 + window.decision_time.minute) // STEP_MINUTES
    links: list[PlanSlotForecastLink] = []
    for slot in range(STEPS_PER_DAY):
        if version > 0 and slot < issue_slot:
            previous = prior.get(slot)
            if previous is None:
                raise InputError("Q3 revision cannot trace a frozen slot to its prior forecasts")
            forecast_ids = previous.forecast_ids
        else:
            point = points.get(slot)
            if point is None:
                raise InputError("Q3 plan version is missing a forecast for a committed slot")
            forecast_ids = (point.load_forecast_id, point.pv_forecast_id)
        links.append(
            PlanSlotForecastLink(
                day=ledger.day,
                version=version,
                target_slot=slot,
                forecast_ids=forecast_ids,
            )
        )
    return tuple(links)


def run_q3_day(
    *,
    day: dt.date,
    state_start: BatteryState,
    actuals: tuple[ActualInterval, ...],
    window_factory: Q3WindowFactory,
    solver: Q3WindowSolver = solve_q3_window_milp,
) -> Q3DayRun:
    """Execute 144 causal solve/first-action/replay steps for one day.

    The window factory is intentionally called before the current actual is
    supplied to the replay layer. The driver does not pass realized load or PV
    through the factory interface; the factory must additionally enforce the
    approved InfoSet visibility contract for its own data sources.
    """

    ordered_actuals = _actual_day(day, actuals)
    state = state_start
    ledger: Q3PlanLedger | None = None
    intervals: list[IntervalResult] = []
    links: tuple[PlanSlotForecastLink, ...] = ()
    emergencies: list[Q3EmergencySettlementEntry] = []

    for actual in ordered_actuals:
        window = window_factory(
            decision_time=actual.start,
            battery_state=state,
            current_ledger=ledger,
        )
        if window.decision_time != actual.start or window.battery_state != state:
            raise InputError("Q3 window factory changed the requested decision time or state")
        previous_version = None if ledger is None else ledger.current.version
        solution = solver(window)
        try:
            executed = execute_q3_window_step(
                window=window,
                solution=solution,
                actual=actual,
                current_ledger=ledger,
            )
        except InputError as exc:
            raise InputError(
                "Q3 execution failed at "
                f"{actual.start.isoformat()} (day={actual.day.isoformat()}, slot={actual.slot}): "
                f"{exc}"
            ) from exc
        ledger = executed.ledger_after
        if previous_version is None or ledger.current.version != previous_version:
            links = (
                *links,
                *_links_for_new_version(window=window, ledger=ledger, existing=links),
            )
        interval = executed.to_interval_result()
        intervals.append(interval)
        state = interval.state_end
        if executed.emergency_settlement is not None:
            emergencies.append(executed.emergency_settlement)

    if ledger is None:
        raise InputError("Q3 daily replay did not create a plan ledger")
    return Q3DayRun(
        day=day,
        state_start=state_start,
        state_end=state,
        intervals=tuple(intervals),
        ledger=ledger,
        forecast_links=links,
        emergency_entries=tuple(emergencies),
    )


def run_q3_period(
    *,
    days: tuple[dt.date, ...],
    state_start: BatteryState,
    actuals: tuple[ActualInterval, ...],
    window_factory: Q3WindowFactory,
    solver: Q3WindowSolver = solve_q3_window_milp,
) -> Q3PeriodRun:
    """Run consecutive days while carrying the physical state across midnight."""

    if not days:
        raise InputError("Q3 period requires at least one day")
    if days != tuple(sorted(days)) or len(days) != len(set(days)):
        raise InputError("Q3 period days must be unique and increasing")
    for previous, current in zip(days, days[1:], strict=False):
        if current != previous + dt.timedelta(days=1):
            raise InputError("Q3 period days must be consecutive")
    expected_keys = tuple((day, slot) for day in days for slot in range(STEPS_PER_DAY))
    actual_keys = tuple((actual.day, actual.slot) for actual in actuals)
    if actual_keys != expected_keys:
        raise InputError("Q3 period actuals do not exactly cover every requested day and slot")

    state = state_start
    results: list[Q3DayRun] = []
    offset = 0
    for day in days:
        day_actuals = actuals[offset : offset + STEPS_PER_DAY]
        result = run_q3_day(
            day=day,
            state_start=state,
            actuals=day_actuals,
            window_factory=window_factory,
            solver=solver,
        )
        results.append(result)
        state = result.state_end
        offset += STEPS_PER_DAY
    return Q3PeriodRun(days=tuple(results))
