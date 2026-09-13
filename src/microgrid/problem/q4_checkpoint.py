"""Compact ledger witness and restoration from verified committed JSONL prefixes."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

from ..schemas import InputError
from .purchase_ledger import Bill, PurchaseLedger
from .q4_common import ACTION_START, STEP, jsonable, nonnegative

CHECKPOINT_SCHEMA = 3


def ledger_witness(ledger: PurchaseLedger) -> dict:
    """Keep counts and the current contract, without scanning historic bills."""
    latest_day = next(reversed(ledger.versions), None)
    return {
        "case_id": ledger.case_id,
        "bill_count": len(ledger.bills),
        "contract_day_count": len(ledger.versions),
        "latest_contract": ledger.versions[latest_day][-1] if latest_day else None,
    }


def restore_ledger(run_dir: Path, case_id: str, time_at: dt.datetime, witness: dict):
    """Caller must first verify hashes and truncate all logs to committed prefixes."""
    ledger = PurchaseLedger(case_id)
    expected_event = ACTION_START
    event_step = dt.timedelta(hours=6 if case_id == "q4_3" else 24)
    with (run_dir / "contracts.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            event = dt.datetime.fromisoformat(record["event_time"])
            if event != expected_event or event >= time_at:
                raise InputError("checkpoint contract event coverage mismatch")
            midnight = dt.datetime.combine(event.date(), dt.time())
            start = int((event - midnight) / STEP)
            slots = tuple(midnight + k * STEP for k in range(start, 144))
            version = ledger.submit(event, slots, tuple(record["quantities"][start:]))
            if jsonable(version) != record:
                raise InputError("checkpoint contract version mismatch")
            expected_event += event_step
    if expected_event < time_at:
        raise InputError("checkpoint missing committed contract event")
    expected_slot = ACTION_START
    with (run_dir / "cost_ledger.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            record["slot_start"] = dt.datetime.fromisoformat(record["slot_start"])
            bill = Bill(**record)
            if bill.slot_start != expected_slot or bill.slot_start >= time_at:
                raise InputError("checkpoint bill coverage mismatch")
            for field in (
                "planned_cost_cny",
                "adjustment_cost_cny",
                "emergency_cost_cny",
                "execution_price",
            ):
                nonnegative(field, getattr(bill, field))
            ledger.bills[bill.slot_start] = bill
            expected_slot += STEP
    if expected_slot != time_at:
        raise InputError("checkpoint missing committed bill")
    for versions in ledger.versions.values():
        for index, version in enumerate(versions):
            if version.version:
                bill = ledger.bills.get(version.event_time)
                if bill is None:
                    raise InputError("checkpoint missing committed trade settlement")
                versions[index] = replace(version, trade_price=bill.execution_price)
    if jsonable(ledger_witness(ledger)) != witness:
        raise InputError("checkpoint ledger witness mismatch")
    return ledger
