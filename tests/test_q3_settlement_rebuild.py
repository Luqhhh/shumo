from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

from microgrid.problem.contracts import TimeGrid
from microgrid.schemas import InputError

_SPEC = importlib.util.spec_from_file_location(
    "q3_settlement_rebuild_script",
    Path(__file__).parents[1] / "scripts/rebuild_q3_settlement_evidence.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_SCRIPT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _SCRIPT
_SPEC.loader.exec_module(_SCRIPT)


def _plans():
    day = dt.date(2025, 2, 1)
    grid = TimeGrid()
    rows = []
    for hour in (0, 6, 12, 18):
        for slot in range(144):
            interval = grid.interval(day, slot)
            rows.append(
                {
                    "issue_time": dt.datetime.combine(day, dt.time(hour)).isoformat(),
                    "target_slot_start": interval.start.isoformat(),
                    "target_slot_end": interval.end.isoformat(),
                    "committed_kwh": 100.0 + (1e-7 if hour == 6 and slot == 120 else 0),
                    "previous_committed_kwh": None
                    if hour == 0
                    else 100.0 + (1e-7 if hour == 12 and slot == 120 else 0),
                    "purchase_is_fixed": slot < hour * 6,
                }
            )
    return rows


def test_rebuild_uses_all_complete_versions_without_netting_micro_transactions():
    ledgers = _SCRIPT.ledgers_from_plan_rows(tuple(_plans()), (1.0,) * 144)
    assert len(ledgers) == 1 and len(ledgers[0].versions) == 4
    assert ledgers[0].current.committed_kwh == (100.0,) * 144
    assert ledgers[0].adjustment_cost_cny > 0


@pytest.mark.parametrize("invalid", ["missing", "duplicate", "previous", "frozen"])
def test_rebuild_refuses_incomplete_or_inconsistent_primary_versions(invalid):
    rows = _plans()
    if invalid == "missing":
        rows.pop()
    elif invalid == "duplicate":
        rows.append(dict(rows[-1]))
    elif invalid == "previous":
        rows[144]["previous_committed_kwh"] = 99.0
    else:
        rows[144]["purchase_is_fixed"] = False
    with pytest.raises(InputError):
        _SCRIPT.ledgers_from_plan_rows(tuple(rows), (1.0,) * 144)


@pytest.mark.parametrize(
    "source,target", [("same", "same"), ("../escape", "target"), ("source", "/tmp/escape")]
)
def test_rebuild_refuses_source_overwrite_or_directory_escape(tmp_path, source, target):
    with pytest.raises(InputError):
        _SCRIPT.rebuild(tmp_path, source_run_id=source, run_id=target)
