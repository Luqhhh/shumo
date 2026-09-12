"""In-memory Q3 plan-version ledger for approved ``SETTLE-A-v2``.

The records here preserve version history and calculate plan/adjustment cost.
They deliberately do not choose whether formal runs serialize the ledger as a
sidecar or add it to ``CaseResult``; that shared artifact decision belongs to A.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from ..schemas import InputError
from .contracts import ENERGY_ABS_TOL_KWH, STEP_MINUTES, STEPS_PER_DAY

Q3_REVISION_HOURS = (6, 12, 18)


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{name} must be numeric")
    clean = float(value)
    if not math.isfinite(clean):
        raise InputError(f"{name} must be finite")
    return clean


def _commitments(values: tuple[float, ...]) -> tuple[float, ...]:
    if len(values) != STEPS_PER_DAY:
        raise InputError(f"Q3 daily commitment must contain {STEPS_PER_DAY} slots")
    clean = tuple(_finite("committed_kwh", value) for value in values)
    if any(value < -ENERGY_ABS_TOL_KWH for value in clean):
        raise InputError("Q3 committed energy must be non-negative")
    return tuple(max(0.0, value) for value in clean)


def _prices(values: tuple[float, ...]) -> tuple[float, ...]:
    if len(values) != STEPS_PER_DAY:
        raise InputError(f"Q3 daily base prices must contain {STEPS_PER_DAY} slots")
    return tuple(_finite("base_price_cny_per_kwh", value) for value in values)


def _slot_at(issue_time: dt.datetime) -> int:
    return (issue_time.hour * 60 + issue_time.minute) // STEP_MINUTES


@dataclass(frozen=True)
class Q3PlanVersion:
    """One immutable, fully materialized daily contract schedule."""

    day: dt.date
    version: int
    issue_time: dt.datetime
    committed_kwh: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.version < 0:
            raise ValueError("plan version must be non-negative")
        if self.issue_time.date() != self.day:
            raise ValueError("plan issue_time must be on the plan day")
        clean = _commitments(self.committed_kwh)
        if clean != self.committed_kwh:
            object.__setattr__(self, "committed_kwh", clean)


@dataclass(frozen=True)
class Q3AdjustmentEntry:
    """One target-slot comparison against the previous confirmed version."""

    issue_time: dt.datetime
    target_slot: int
    previous_committed_kwh: float
    new_committed_kwh: float
    delta_plus_kwh: float
    delta_minus_kwh: float
    transaction_price_cny_per_kwh: float
    adjustment_cost_cny: float
    is_frozen: bool

    def __post_init__(self) -> None:
        if not 0 <= self.target_slot < STEPS_PER_DAY:
            raise ValueError("target_slot is outside the natural-day grid")
        for name in (
            "previous_committed_kwh",
            "new_committed_kwh",
            "delta_plus_kwh",
            "delta_minus_kwh",
            "transaction_price_cny_per_kwh",
            "adjustment_cost_cny",
        ):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"{name} must be finite")
        if any(
            value < -ENERGY_ABS_TOL_KWH
            for value in (
                self.previous_committed_kwh,
                self.new_committed_kwh,
                self.delta_plus_kwh,
                self.delta_minus_kwh,
            )
        ):
            raise ValueError("commitments and adjustment deltas must be non-negative")
        if self.delta_plus_kwh > ENERGY_ABS_TOL_KWH and self.delta_minus_kwh > ENERGY_ABS_TOL_KWH:
            raise ValueError("delta_plus and delta_minus cannot both be positive")
        expected_delta = self.new_committed_kwh - self.previous_committed_kwh
        if not math.isclose(
            self.delta_plus_kwh - self.delta_minus_kwh,
            expected_delta,
            rel_tol=0.0,
            abs_tol=ENERGY_ABS_TOL_KWH,
        ):
            raise ValueError("adjustment deltas do not reconstruct the new commitment")
        expected_cost = self.transaction_price_cny_per_kwh * (
            1.5 * self.delta_plus_kwh + 0.5 * self.delta_minus_kwh
        )
        if not math.isclose(
            self.adjustment_cost_cny,
            expected_cost,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("adjustment cost does not follow SETTLE-A-v2")
        if self.is_frozen and (
            self.delta_plus_kwh > ENERGY_ABS_TOL_KWH or self.delta_minus_kwh > ENERGY_ABS_TOL_KWH
        ):
            raise ValueError("a frozen slot cannot contain an adjustment")


@dataclass(frozen=True)
class Q3PlanLedger:
    """Immutable version chain and its non-netted SETTLE-A-v2 transactions."""

    day: dt.date
    base_prices_cny_per_kwh: tuple[float, ...]
    versions: tuple[Q3PlanVersion, ...]
    adjustment_entries: tuple[Q3AdjustmentEntry, ...] = ()

    def __post_init__(self) -> None:
        clean_prices = _prices(self.base_prices_cny_per_kwh)
        if clean_prices != self.base_prices_cny_per_kwh:
            object.__setattr__(self, "base_prices_cny_per_kwh", clean_prices)
        if not self.versions:
            raise ValueError("Q3 plan ledger must contain version 0")
        first = self.versions[0]
        if first.day != self.day or first.version != 0:
            raise ValueError("Q3 plan ledger must start with version 0 for its day")
        if first.issue_time != dt.datetime.combine(self.day, dt.time()):
            raise ValueError("Q3 version 0 must be issued at 00:00")
        for expected_version, version in enumerate(self.versions):
            if version.day != self.day or version.version != expected_version:
                raise ValueError("Q3 plan versions must be consecutive and on one day")
            if expected_version > 0 and (
                version.issue_time.hour not in Q3_REVISION_HOURS
                or version.issue_time.minute
                or version.issue_time.second
                or version.issue_time.microsecond
            ):
                raise ValueError("Q3 revision versions must be issued at 06:00/12:00/18:00")
        issue_times = tuple(version.issue_time for version in self.versions)
        if issue_times != tuple(sorted(issue_times)) or len(issue_times) != len(set(issue_times)):
            raise ValueError("Q3 plan issue times must be unique and increasing")
        expected_entry_count = (len(self.versions) - 1) * STEPS_PER_DAY
        if len(self.adjustment_entries) != expected_entry_count:
            raise ValueError("Q3 plan ledger must retain one adjustment row per revision and slot")
        for version_index in range(1, len(self.versions)):
            previous = self.versions[version_index - 1]
            current = self.versions[version_index]
            offset = (version_index - 1) * STEPS_PER_DAY
            rows = self.adjustment_entries[offset : offset + STEPS_PER_DAY]
            frozen_before = _slot_at(current.issue_time)
            for slot, row in enumerate(rows):
                if row.issue_time != current.issue_time or row.target_slot != slot:
                    raise ValueError("Q3 adjustment rows do not match their plan revision")
                if row.is_frozen != (slot < frozen_before):
                    raise ValueError("Q3 adjustment frozen flag is inconsistent with issue_time")
                if not math.isclose(
                    row.previous_committed_kwh,
                    previous.committed_kwh[slot],
                    rel_tol=0.0,
                    abs_tol=ENERGY_ABS_TOL_KWH,
                ) or not math.isclose(
                    row.new_committed_kwh,
                    current.committed_kwh[slot],
                    rel_tol=0.0,
                    abs_tol=ENERGY_ABS_TOL_KWH,
                ):
                    raise ValueError("Q3 adjustment row does not reconstruct adjacent versions")

    @classmethod
    def start(
        cls,
        day: dt.date,
        *,
        initial_commitments_kwh: tuple[float, ...],
        base_prices_cny_per_kwh: tuple[float, ...],
    ) -> Q3PlanLedger:
        """Create the 00:00 version whose quantities incur planned cost once."""

        version_zero = Q3PlanVersion(
            day=day,
            version=0,
            issue_time=dt.datetime.combine(day, dt.time()),
            committed_kwh=_commitments(initial_commitments_kwh),
        )
        return cls(
            day=day,
            base_prices_cny_per_kwh=_prices(base_prices_cny_per_kwh),
            versions=(version_zero,),
        )

    @property
    def current(self) -> Q3PlanVersion:
        return self.versions[-1]

    @property
    def planned_cost_cny(self) -> float:
        return math.fsum(
            quantity * price
            for quantity, price in zip(
                self.versions[0].committed_kwh,
                self.base_prices_cny_per_kwh,
                strict=True,
            )
        )

    @property
    def adjustment_cost_cny(self) -> float:
        return math.fsum(entry.adjustment_cost_cny for entry in self.adjustment_entries)

    def revise(
        self,
        issue_time: dt.datetime,
        *,
        new_commitments_kwh: tuple[float, ...],
        transaction_price_cny_per_kwh: float,
    ) -> Q3PlanLedger:
        """Append one release-time revision while freezing already-started slots."""

        if issue_time.date() != self.day:
            raise InputError("Q3 cannot revise a different day's contract")
        if (
            issue_time.hour not in Q3_REVISION_HOURS
            or issue_time.minute
            or issue_time.second
            or issue_time.microsecond
        ):
            raise InputError("Q3 revisions are allowed only at 06:00/12:00/18:00")
        if issue_time <= self.current.issue_time:
            raise InputError("Q3 revision issue times must be strictly increasing")

        new_commitments = _commitments(new_commitments_kwh)
        transaction_price = _finite("transaction_price_cny_per_kwh", transaction_price_cny_per_kwh)
        frozen_before = _slot_at(issue_time)
        entries: list[Q3AdjustmentEntry] = []
        for slot, (previous, new) in enumerate(
            zip(self.current.committed_kwh, new_commitments, strict=True)
        ):
            is_frozen = slot < frozen_before
            if is_frozen and not math.isclose(
                previous,
                new,
                rel_tol=0.0,
                abs_tol=ENERGY_ABS_TOL_KWH,
            ):
                raise InputError(f"Q3 slot {slot} is already frozen at {issue_time}")
            delta = new - previous
            delta_plus = max(delta, 0.0)
            delta_minus = max(-delta, 0.0)
            entries.append(
                Q3AdjustmentEntry(
                    issue_time=issue_time,
                    target_slot=slot,
                    previous_committed_kwh=previous,
                    new_committed_kwh=new,
                    delta_plus_kwh=delta_plus,
                    delta_minus_kwh=delta_minus,
                    transaction_price_cny_per_kwh=transaction_price,
                    adjustment_cost_cny=transaction_price * (1.5 * delta_plus + 0.5 * delta_minus),
                    is_frozen=is_frozen,
                )
            )

        version = Q3PlanVersion(
            day=self.day,
            version=len(self.versions),
            issue_time=issue_time,
            committed_kwh=new_commitments,
        )
        return Q3PlanLedger(
            day=self.day,
            base_prices_cny_per_kwh=self.base_prices_cny_per_kwh,
            versions=(*self.versions, version),
            adjustment_entries=(*self.adjustment_entries, *entries),
        )
