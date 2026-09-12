from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.contracts import STEPS_PER_DAY
from microgrid.problem.q3_plan_ledger import Q3PlanLedger
from microgrid.schemas import InputError


def _schedule(value: float) -> tuple[float, ...]:
    return (value,) * STEPS_PER_DAY


def _replace(schedule: tuple[float, ...], slot: int, value: float) -> tuple[float, ...]:
    updated = list(schedule)
    updated[slot] = value
    return tuple(updated)


def test_settle_a_v2_keeps_each_revision_and_matches_the_approved_hand_bill() -> None:
    day = dt.date(2025, 2, 1)
    target_slot = 120
    ledger = Q3PlanLedger.start(
        day,
        initial_commitments_kwh=_schedule(100.0),
        base_prices_cny_per_kwh=_schedule(1.0),
    )
    p06 = _replace(ledger.current.committed_kwh, target_slot, 110.0)
    ledger = ledger.revise(
        dt.datetime(2025, 2, 1, 6),
        new_commitments_kwh=p06,
        transaction_price_cny_per_kwh=2.0,
    )
    p12 = _replace(ledger.current.committed_kwh, target_slot, 90.0)
    ledger = ledger.revise(
        dt.datetime(2025, 2, 1, 12),
        new_commitments_kwh=p12,
        transaction_price_cny_per_kwh=3.0,
    )
    p18 = _replace(ledger.current.committed_kwh, target_slot, 95.0)
    ledger = ledger.revise(
        dt.datetime(2025, 2, 1, 18),
        new_commitments_kwh=p18,
        transaction_price_cny_per_kwh=4.0,
    )

    assert ledger.planned_cost_cny == pytest.approx(14_400.0)
    assert ledger.adjustment_cost_cny == pytest.approx(90.0)
    assert ledger.current.committed_kwh[target_slot] == pytest.approx(95.0)
    assert len(ledger.versions) == 4
    assert [
        entry.adjustment_cost_cny
        for entry in ledger.adjustment_entries
        if entry.target_slot == target_slot
    ] == pytest.approx([30.0, 30.0, 30.0])


def test_revision_freezes_started_slots_but_allows_future_slots_to_change_again() -> None:
    day = dt.date(2025, 2, 1)
    ledger = Q3PlanLedger.start(
        day,
        initial_commitments_kwh=_schedule(100.0),
        base_prices_cny_per_kwh=_schedule(1.0),
    )
    invalid = _replace(ledger.current.committed_kwh, 35, 90.0)
    with pytest.raises(InputError, match="already frozen"):
        ledger.revise(
            dt.datetime(2025, 2, 1, 6),
            new_commitments_kwh=invalid,
            transaction_price_cny_per_kwh=2.0,
        )

    valid = _replace(ledger.current.committed_kwh, 36, 90.0)
    revised = ledger.revise(
        dt.datetime(2025, 2, 1, 6),
        new_commitments_kwh=valid,
        transaction_price_cny_per_kwh=2.0,
    )
    assert revised.current.committed_kwh[36] == pytest.approx(90.0)
    assert ledger.current.committed_kwh[36] == pytest.approx(100.0)
    assert revised.adjustment_entries[35].is_frozen is True
    assert revised.adjustment_entries[36].is_frozen is False


@pytest.mark.parametrize(
    "issue_time",
    [
        dt.datetime(2025, 2, 1, 0),
        dt.datetime(2025, 2, 1, 6, 10),
        dt.datetime(2025, 2, 2, 6),
    ],
)
def test_revision_rejects_unapproved_times_and_cross_day_contracts(
    issue_time: dt.datetime,
) -> None:
    day = dt.date(2025, 2, 1)
    ledger = Q3PlanLedger.start(
        day,
        initial_commitments_kwh=_schedule(100.0),
        base_prices_cny_per_kwh=_schedule(1.0),
    )

    with pytest.raises(InputError):
        ledger.revise(
            issue_time,
            new_commitments_kwh=_schedule(100.0),
            transaction_price_cny_per_kwh=2.0,
        )
