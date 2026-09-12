from __future__ import annotations

from dataclasses import replace

import pytest

from microgrid.problem.contracts import BatteryAction, BatteryState
from microgrid.problem.dispatch_feedback import Measurement, apply_feedback
from microgrid.problem.q4_common import ACTION_START, STEP, JsonlWriter, jsonable
from microgrid.problem.q4_diagnostics import Errors, constraint_tags, historical_pv
from microgrid.problem.q4_evidence import full_snapshot_sha256, prefix_sha256
from microgrid.problem.q4_forecasts import Q4Forecaster
from microgrid.problem.q4_inputs import Q4Inputs
from microgrid.problem.rolling_engine import _initialize_forecaster
from microgrid.schemas import InputError


def test_writer_commits_durable_prefix_and_closes_after_flush_failure(tmp_path, monkeypatch):
    import microgrid.problem.q4_common as module

    calls = []
    monkeypatch.setattr(module.os, "fsync", lambda fd: calls.append(fd))
    writer = JsonlWriter(tmp_path, ("first", "second"))
    writer.write("first", {"value": "中文"})
    writer.write("second", {"value": 1})
    writer.flush()
    assert len(calls) == 2
    committed = (tmp_path / "first.jsonl").read_bytes()
    digest = prefix_sha256(tmp_path / "first.jsonl", len(committed))
    writer.write("first", {"uncommitted": True})
    writer.flush()
    assert prefix_sha256(tmp_path / "first.jsonl", len(committed)) == digest
    monkeypatch.setattr(module.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk error")))
    with pytest.raises(OSError, match="disk error"):
        writer.close()
    assert writer.closed and all(stream.closed for stream in writer.streams.values())


def test_full_snapshot_hash_covers_provenance_omitted_from_snapshot_id(tmp_path):
    inputs = Q4Inputs((600.0,) * 52560, (0.0,) * 52560, (1.0,) * 52560, (), {}, {})
    service: Q4Forecaster = _initialize_forecaster(inputs, "q4_2", "main", ACTION_START)
    from microgrid.problem.contracts import InfoSet

    snapshot = service.refresh(InfoSet.from_raw(ACTION_START, ()))
    changed = replace(snapshot, traces={**snapshot.traces, "load_unit": "wrong"})
    assert changed.snapshot_id == snapshot.snapshot_id
    assert full_snapshot_sha256(changed) != full_snapshot_sha256(snapshot)


def test_net_load_error_can_cancel_and_constraint_cost_tags_overlap():
    errors = Errors()
    # Each individual power forecast is 60 kW too high, while net demand is exact.
    errors.add((600 - 120) / 6, (660 - 180) / 6, 1.0)
    assert errors.summary()["mae"] == 0
    record = apply_feedback(BatteryAction(), BatteryState(1200), 0, Measurement(100, 0))
    tags = constraint_tags(ACTION_START + STEP, jsonable(record), 100, "q4_2", None)
    assert {
        "contract_frozen_at_control_time",
        "soc_within_100kwh_of_lower_bound",
        "positive_net_load_error",
    } <= set(tags)
    assert record.emergency_kwh == 100


def test_historical_diagnostic_pv_never_uses_future_actual():
    inputs = Q4Inputs(
        (600.0,) * 52560, tuple(float(i % 144) for i in range(52560)), (1.0,) * 52560, (), {}, {}
    )
    issue = inputs.index(ACTION_START)
    target = issue + 143
    expected = historical_pv(inputs, issue, target)
    altered = replace(inputs, pv_kw=inputs.pv_kw[:issue] + (1e9,) * (52560 - issue))
    assert historical_pv(altered, issue, target) == expected
    with pytest.raises(InputError, match="not visible"):
        historical_pv(inputs, issue, issue + 144)


def test_failed_log_persistence_never_publishes_checkpoint(tmp_path, monkeypatch):
    import microgrid.problem.q4_common as common
    import microgrid.problem.rolling_engine as module
    from microgrid.problem.contracts import CaseContext

    inputs = Q4Inputs((600.0,) * 52560, (0.0,) * 52560, (1.0,) * 52560, (), {}, {})
    monkeypatch.setattr(module, "load_q4_inputs", lambda *args: inputs)
    monkeypatch.setattr(module, "require_approved_decisions", lambda *args: None)
    monkeypatch.setattr(common.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk error")))
    run = tmp_path / "failure"
    context = CaseContext(tmp_path, "q4_2", "test", run, {"end_time": "2025-02-02"}, True)
    with pytest.raises(OSError, match="disk error"):
        module.run_q4(context)
    from microgrid.problem.q4_evidence import read_json

    assert not (run / "checkpoint.json").exists()
    assert read_json(run / "manifest.json")["status"] == "failed"
    assert read_json(run / "failure.json")["stage"] == "log_persistence"
    assert len((run / "cost_ledger.jsonl").read_text().splitlines()) == 36
