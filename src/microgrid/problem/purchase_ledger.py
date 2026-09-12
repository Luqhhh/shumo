"""Event permissions, adjacent contract versions and delayed actual billing."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from .contracts import CostBreakdown
from .q4_common import STEP, Q4Error, nonnegative


@dataclass(frozen=True)
class ContractVersion:
    day: dt.date
    event_time: dt.datetime
    version: int
    quantities: tuple[float, ...]
    plus: tuple[float, ...]
    minus: tuple[float, ...]
    trade_price: float | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class Bill:
    slot_start: dt.datetime
    planned_cost_cny: float
    adjustment_cost_cny: float
    emergency_cost_cny: float
    execution_price: float
    schema_version: int = 1


class PurchaseLedger:
    def __init__(self, case_id: str):
        if case_id not in ("q4_2", "q4_3"):
            raise ValueError("ledger case must be q4_2 or q4_3")
        self.case_id = case_id
        self.versions: dict[dt.date, list[ContractVersion]] = {}
        self.bills: dict[dt.datetime, Bill] = {}

    def permissions(self, time: dt.datetime, slots: tuple[dt.datetime, ...]) -> tuple[str, ...]:
        if not slots or slots[0] != time or any(s != time + i * STEP for i, s in enumerate(slots)):
            raise ValueError("permission window must be contiguous and start at decision time")
        event = time.time() == dt.time()
        adjustable = self.case_id == "q4_3" and time.time() in tuple(
            dt.time(h) for h in (6, 12, 18)
        )
        result = []
        for slot in slots:
            if slot.date() > time.date():
                result.append("lookahead")
            elif event and time.date() not in self.versions:
                result.append("new_day")
            else:
                if time.date() not in self.versions:
                    raise Q4Error("missing_contract", f"no G0 for {time.date()}")
                result.append("adjustable" if adjustable else "fixed")
        return tuple(result)

    def committed(self, slot: dt.datetime, *, initial: bool = False) -> float:
        versions = self.versions.get(slot.date())
        if not versions:
            raise Q4Error("missing_contract", str(slot))
        index = (slot.hour * 60 + slot.minute) // 10
        return versions[0 if initial else -1].quantities[index]

    def submit(
        self, time: dt.datetime, slots: tuple[dt.datetime, ...], grid: tuple[float, ...]
    ) -> ContractVersion | None:
        if len(slots) != len(grid):
            raise ValueError("contract slot/quantity lengths differ")
        permissions = self.permissions(time, slots)
        for value in grid:
            nonnegative("contract", value)
        new = [i for i, permission in enumerate(permissions) if permission == "new_day"]
        adjustable = [i for i, permission in enumerate(permissions) if permission == "adjustable"]
        previous = self.versions.get(time.date(), [])
        if previous and previous[-1].event_time == time:
            raise Q4Error("duplicate_contract_event", str(time))
        if new:
            if len(new) != 144 or tuple(slots[i] for i in new) != tuple(
                time + i * STEP for i in range(144)
            ):
                raise Q4Error("incomplete_g0", "midnight submission needs all 144 slots")
            quantities = tuple(grid[i] for i in new)
            version = ContractVersion(time.date(), time, 0, quantities, (0.0,) * 144, (0.0,) * 144)
        elif adjustable:
            quantities = list(previous[-1].quantities)
            for i in adjustable:
                slot_index = (slots[i].hour * 60 + slots[i].minute) // 10
                quantities[slot_index] = grid[i]
            old = previous[-1].quantities
            plus = tuple(max(a - b, 0.0) for a, b in zip(quantities, old, strict=True))
            minus = tuple(max(b - a, 0.0) for a, b in zip(quantities, old, strict=True))
            version = ContractVersion(
                time.date(), time, len(previous), tuple(quantities), plus, minus
            )
        else:
            for i, permission in enumerate(permissions):
                if permission == "fixed" and abs(grid[i] - self.committed(slots[i])) > 1e-6:
                    raise Q4Error("contract_frozen", str(slots[i]))
            return None
        self.versions.setdefault(time.date(), []).append(version)
        return version

    def settle(
        self,
        slot: dt.datetime,
        *,
        available_at: dt.datetime,
        actual_price: float,
        emergency_kwh: float,
    ) -> Bill:
        from dataclasses import replace

        if available_at < slot + STEP:
            raise Q4Error("premature_settlement", "actual price not available until right endpoint")
        if slot in self.bills:
            raise Q4Error("duplicate_settlement", str(slot))
        nonnegative("actual_price", actual_price)
        nonnegative("emergency", emergency_kwh)
        planned = actual_price * self.committed(slot, initial=True)
        adjustment = 0.0
        versions = self.versions[slot.date()]
        for index, version in enumerate(versions):
            if version.version and version.event_time == slot:
                if version.trade_price is not None:
                    raise Q4Error("duplicate_trade_settlement", str(slot))
                adjustment += actual_price * (
                    1.5 * math.fsum(version.plus) + 0.5 * math.fsum(version.minus)
                )
                versions[index] = replace(version, trade_price=actual_price)
        bill = Bill(slot, planned, adjustment, 5 * actual_price * emergency_kwh, actual_price)
        self.bills[slot] = bill
        return bill

    def costs(self) -> CostBreakdown:
        return CostBreakdown(
            math.fsum(b.planned_cost_cny for b in self.bills.values()),
            math.fsum(b.adjustment_cost_cny for b in self.bills.values()),
            math.fsum(b.emergency_cost_cny for b in self.bills.values()),
        )
