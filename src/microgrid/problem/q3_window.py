"""Causal, aligned input contract for one approved Q3 rolling-MPC window."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Literal

from ..schemas import InputError
from .contracts import ENERGY_ABS_TOL_KWH, STEP_MINUTES, STEPS_PER_DAY, BatteryState, TimeGrid
from .q2_inputs import FixedPricePoint
from .q3_load_forecast import load_forecast_id
from .q3_load_snapshot import LOAD_MPC_WINDOW_STEPS, Q3LoadWindow
from .q3_plan_ledger import Q3PlanLedger
from .q3_pv_snapshot import Q3PVWindow

PurchaseMode = Literal[
    "new_commitment",
    "adjustable_commitment",
    "fixed_commitment",
    "lookahead_only",
]
PlanningEvent = Literal["base_plan", "revision", "dispatch_only"]
TerminalMode = Literal["terminal_value", "year_end_equality"]
_REVISION_HOURS = (6, 12, 18)
_STEP = dt.timedelta(minutes=STEP_MINUTES)


def _finite_non_negative(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{name} must be numeric")
    clean = float(value)
    if not math.isfinite(clean) or clean < -ENERGY_ABS_TOL_KWH:
        raise InputError(f"{name} must be finite and non-negative")
    return max(0.0, clean)


def _is_exact_hour(value: dt.datetime, hour: int) -> bool:
    return value.time() == dt.time(hour)


def _planning_event(decision_time: dt.datetime) -> PlanningEvent:
    if _is_exact_hour(decision_time, 0):
        return "base_plan"
    if decision_time.time() in tuple(dt.time(hour) for hour in _REVISION_HOURS):
        return "revision"
    return "dispatch_only"


def _expected_previous_plan_issue(decision_time: dt.datetime) -> dt.datetime | None:
    event = _planning_event(decision_time)
    if event == "base_plan":
        return None
    minute_of_day = decision_time.hour * 60 + decision_time.minute
    release_minutes = (0, 6 * 60, 12 * 60, 18 * 60)
    if event == "revision":
        eligible = [minute for minute in release_minutes if minute < minute_of_day]
    else:
        eligible = [minute for minute in release_minutes if minute <= minute_of_day]
    release_minute = max(eligible)
    return dt.datetime.combine(decision_time.date(), dt.time()) + dt.timedelta(
        minutes=release_minute
    )


def _latest_snapshot_issue(decision_time: dt.datetime) -> dt.datetime:
    minute_of_day = decision_time.hour * 60 + decision_time.minute
    release_minute = max(
        minute for minute in (0, 6 * 60, 12 * 60, 18 * 60) if minute <= minute_of_day
    )
    return dt.datetime.combine(decision_time.date(), dt.time()) + dt.timedelta(
        minutes=release_minute
    )


@dataclass(frozen=True)
class Q3WindowPoint:
    interval_start: dt.datetime
    interval_end: dt.datetime
    day: dt.date
    slot: int
    load_forecast_id: str
    pv_forecast_id: str
    pv_forecast_kind: str
    load_kw: float
    load_kwh: float
    pv_kw: float
    pv_kwh: float
    base_price_cny_per_kwh: float
    price_source_ref: str
    purchase_mode: PurchaseMode
    previous_committed_kwh: float | None

    def __post_init__(self) -> None:
        if self.interval_end - self.interval_start != _STEP:
            raise ValueError("Q3 window interval must be exactly ten minutes")
        expected = TimeGrid().interval(self.day, self.slot)
        if (self.interval_start, self.interval_end) != (expected.start, expected.end):
            raise ValueError("Q3 window interval is not aligned to its day/slot")
        for name in ("load_kw", "load_kwh", "pv_kw", "pv_kwh", "base_price_cny_per_kwh"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < -ENERGY_ABS_TOL_KWH:
                raise ValueError(f"{name} must be finite and non-negative")
        if not math.isclose(
            self.load_kwh,
            TimeGrid.power_to_energy_kwh(self.load_kw),
            rel_tol=0.0,
            abs_tol=ENERGY_ABS_TOL_KWH,
        ):
            raise ValueError("load kW/kWh conversion is inconsistent")
        if not math.isclose(
            self.pv_kwh,
            TimeGrid.power_to_energy_kwh(self.pv_kw),
            rel_tol=0.0,
            abs_tol=ENERGY_ABS_TOL_KWH,
        ):
            raise ValueError("PV kW/kWh conversion is inconsistent")
        if not self.load_forecast_id or not self.pv_forecast_id or not self.price_source_ref:
            raise ValueError("Q3 window forecast IDs and price source must be non-empty")
        if self.pv_forecast_kind not in ("pv_attachment3", "pv_tail"):
            raise ValueError("Q3 window PV forecast kind is unsupported")
        if self.purchase_mode in ("fixed_commitment", "adjustable_commitment"):
            if self.previous_committed_kwh is None:
                raise ValueError("existing commitment mode requires a previous quantity")
            _finite_non_negative("previous_committed_kwh", self.previous_committed_kwh)
        elif self.previous_committed_kwh is not None:
            raise ValueError("new/lookahead purchase mode cannot carry a previous commitment")

    @property
    def purchase_is_fixed(self) -> bool:
        return self.purchase_mode == "fixed_commitment"

    @property
    def purchase_is_committable(self) -> bool:
        return self.purchase_mode in ("new_commitment", "adjustable_commitment")

    @property
    def is_lookahead_only(self) -> bool:
        return self.purchase_mode == "lookahead_only"


@dataclass(frozen=True)
class Q3WindowInput:
    decision_time: dt.datetime
    battery_state: BatteryState
    load_snapshot_issue_time: dt.datetime
    pv_snapshot_issue_time: dt.datetime
    planning_event: PlanningEvent
    previous_plan_version: int | None
    transaction_price_cny_per_kwh: float | None
    terminal_mode: TerminalMode
    terminal_value_cny_per_kwh: float
    terminal_target_kwh: float | None
    points: tuple[Q3WindowPoint, ...]

    def __post_init__(self) -> None:
        if self.planning_event != _planning_event(self.decision_time):
            raise ValueError("Q3 planning_event does not match decision_time")
        expected_snapshot = _latest_snapshot_issue(self.decision_time)
        if (
            self.load_snapshot_issue_time != expected_snapshot
            or self.pv_snapshot_issue_time != expected_snapshot
        ):
            raise ValueError("Q3 input must retain the latest visible forecast snapshots")
        if not 1 <= len(self.points) <= LOAD_MPC_WINDOW_STEPS:
            raise ValueError("Q3 window input must contain 1..144 points")
        for step, point in enumerate(self.points, start=1):
            if point.interval_end != self.decision_time + step * _STEP:
                raise ValueError("Q3 window points must be a continuous future suffix")
        if self.previous_plan_version is not None and self.previous_plan_version < 0:
            raise ValueError("previous_plan_version must be non-negative")
        if self.transaction_price_cny_per_kwh is not None and (
            not math.isfinite(self.transaction_price_cny_per_kwh)
            or self.transaction_price_cny_per_kwh < 0
        ):
            raise ValueError("transaction price must be finite and non-negative")
        if (
            not math.isfinite(self.terminal_value_cny_per_kwh)
            or self.terminal_value_cny_per_kwh < 0
        ):
            raise ValueError("terminal value must be finite and non-negative")
        if self.planning_event == "base_plan":
            if (
                self.previous_plan_version is not None
                or self.transaction_price_cny_per_kwh is not None
            ):
                raise ValueError("base plan cannot have a previous version or transaction price")
        elif self.planning_event == "revision":
            if self.previous_plan_version is None or self.transaction_price_cny_per_kwh is None:
                raise ValueError("revision requires a previous version and transaction price")
        elif self.planning_event == "dispatch_only":
            if self.previous_plan_version is None:
                raise ValueError("dispatch-only window requires a previous plan version")
            if self.transaction_price_cny_per_kwh is not None:
                raise ValueError("dispatch-only window cannot contain a transaction price")
        else:
            raise ValueError("unsupported Q3 planning event")
        if self.terminal_mode == "year_end_equality":
            if self.terminal_target_kwh != 6000.0 or self.terminal_value_cny_per_kwh != 0.0:
                raise ValueError("year-end mode requires a target and no terminal value")
        elif self.terminal_mode == "terminal_value":
            if self.terminal_target_kwh is not None:
                raise ValueError("terminal-value mode cannot contain a hard target")
        else:
            raise ValueError("unsupported Q3 terminal mode")


def _fixed_price_map(prices: tuple[FixedPricePoint, ...]) -> dict[int, FixedPricePoint]:
    if len(prices) != STEPS_PER_DAY:
        raise InputError("Q3 fixed-price schedule must contain 144 slots")
    by_slot: dict[int, FixedPricePoint] = {}
    for price in prices:
        if price.slot in by_slot:
            raise InputError(f"Q3 fixed-price schedule duplicates slot {price.slot}")
        by_slot[price.slot] = price
    if set(by_slot) != set(range(STEPS_PER_DAY)):
        raise InputError("Q3 fixed-price schedule must cover slots 0..143")
    return by_slot


def _require_previous_ledger(
    decision_time: dt.datetime,
    ledger: Q3PlanLedger | None,
) -> Q3PlanLedger | None:
    expected_issue = _expected_previous_plan_issue(decision_time)
    if expected_issue is None:
        if ledger is not None:
            raise InputError("00:00 base planning must start before creating today's ledger")
        return None
    if ledger is None or ledger.day != decision_time.date():
        raise InputError("Q3 window requires today's latest confirmed plan ledger")
    if ledger.current.issue_time != expected_issue:
        raise InputError(
            "Q3 plan ledger is not the latest version allowed before this decision: "
            f"expected {expected_issue}, got {ledger.current.issue_time}"
        )
    return ledger


def build_q3_window_input(
    *,
    decision_time: dt.datetime,
    battery_state: BatteryState,
    load_window: Q3LoadWindow,
    pv_window: Q3PVWindow,
    fixed_prices: tuple[FixedPricePoint, ...],
    current_ledger: Q3PlanLedger | None,
    evaluation_end: dt.datetime | None = None,
) -> Q3WindowInput:
    """Align causal forecasts, prices, commitments and state for one solve."""

    if decision_time.second or decision_time.microsecond or decision_time.minute % STEP_MINUTES:
        raise InputError("Q3 decision_time must be on a ten-minute boundary")
    if load_window.decision_time != decision_time or pv_window.decision_time != decision_time:
        raise InputError("Q3 load/PV windows must match decision_time")
    expected_snapshot_issue = _latest_snapshot_issue(decision_time)
    if (
        load_window.snapshot_issue_time != expected_snapshot_issue
        or pv_window.snapshot_issue_time != expected_snapshot_issue
    ):
        raise InputError("Q3 window must use the latest published load/PV forecast versions")
    if len(load_window.points) != len(pv_window.points):
        raise InputError("Q3 load/PV windows must have the same horizon")
    if evaluation_end is not None:
        if evaluation_end <= decision_time or evaluation_end.second or evaluation_end.microsecond:
            raise InputError("Q3 evaluation_end must be a later aligned boundary")
        if evaluation_end.minute % STEP_MINUTES:
            raise InputError("Q3 evaluation_end must be a later aligned boundary")

    ledger = _require_previous_ledger(decision_time, current_ledger)
    event = _planning_event(decision_time)
    prices = _fixed_price_map(fixed_prices)
    points: list[Q3WindowPoint] = []
    for load_point, pv_point in zip(load_window.points, pv_window.points, strict=True):
        if load_point.valid_time != pv_point.valid_time:
            raise InputError("Q3 load/PV forecast times are misaligned")
        interval_end = load_point.valid_time
        interval_start = interval_end - _STEP
        slot = (interval_start.hour * 60 + interval_start.minute) // STEP_MINUTES
        day = interval_start.date()
        price = prices[slot]

        if day != decision_time.date():
            purchase_mode: PurchaseMode = "lookahead_only"
            previous = None
        elif event == "base_plan":
            purchase_mode = "new_commitment"
            previous = None
        elif event == "revision":
            purchase_mode = "adjustable_commitment"
            previous = ledger.current.committed_kwh[slot] if ledger is not None else None
        else:
            purchase_mode = "fixed_commitment"
            previous = ledger.current.committed_kwh[slot] if ledger is not None else None

        points.append(
            Q3WindowPoint(
                interval_start=interval_start,
                interval_end=interval_end,
                day=day,
                slot=slot,
                load_forecast_id=load_forecast_id(load_point),
                pv_forecast_id=pv_point.forecast_id,
                pv_forecast_kind=pv_point.kind,
                load_kw=load_point.power_kw,
                load_kwh=load_point.energy_kwh,
                pv_kw=pv_point.power_kw,
                pv_kwh=pv_point.energy_kwh,
                base_price_cny_per_kwh=price.price_cny_per_kwh,
                price_source_ref=price.source_ref,
                purchase_mode=purchase_mode,
                previous_committed_kwh=previous,
            )
        )

    hits_year_end = evaluation_end is not None and points[-1].interval_end == evaluation_end
    if evaluation_end is not None:
        expected_steps = min(
            LOAD_MPC_WINDOW_STEPS,
            int((evaluation_end - decision_time).total_seconds() // (STEP_MINUTES * 60)),
        )
        if len(points) != expected_steps:
            raise InputError("Q3 forecast windows do not match the evaluation-end horizon")

    transaction_price = None
    if event == "revision":
        transaction_slot = (decision_time.hour * 60 + decision_time.minute) // STEP_MINUTES
        transaction_price = prices[transaction_slot].price_cny_per_kwh
    terminal_value = (
        0.0
        if hits_year_end
        else 0.9 * math.fsum(point.price_cny_per_kwh for point in prices.values()) / STEPS_PER_DAY
    )
    return Q3WindowInput(
        decision_time=decision_time,
        battery_state=battery_state,
        load_snapshot_issue_time=load_window.snapshot_issue_time,
        pv_snapshot_issue_time=pv_window.snapshot_issue_time,
        planning_event=event,
        previous_plan_version=ledger.current.version if ledger is not None else None,
        transaction_price_cny_per_kwh=transaction_price,
        terminal_mode="year_end_equality" if hits_year_end else "terminal_value",
        terminal_value_cny_per_kwh=terminal_value,
        terminal_target_kwh=6000.0 if hits_year_end else None,
        points=tuple(points),
    )
