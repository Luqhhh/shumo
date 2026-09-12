from __future__ import annotations

import json
from dataclasses import replace

import pytest

from microgrid.problem.contracts import CaseContext
from microgrid.problem.q4_common import Q4Error
from microgrid.problem.q4_inputs import Q4Inputs
from microgrid.problem.result_io import case_result_to_dict
from microgrid.problem.rolling_engine import run_q4
from microgrid.schemas import InputError


def test_checkpoint_resume_is_equivalent_and_rejects_source_change(tmp_path, monkeypatch):
    import microgrid.problem.rolling_engine as module

    inputs = Q4Inputs(
        (600.0,) * 52560,
        (0.0,) * 52560,
        (1.0,) * 52560,
        (),
        {"actual": "hash"},
        {"schema_version": 1},
    )
    monkeypatch.setattr(module, "load_q4_inputs", lambda *args: inputs)
    monkeypatch.setattr(module, "require_approved_decisions", lambda *args: None)
    metadata = {"end_time": "2025-02-02"}
    context = CaseContext(tmp_path, "q4_2", "test", tmp_path / "continuous", metadata, True)
    continuous = run_q4(context)
    assert continuous.status == "diagnostic_success" and continuous.is_synthetic
    assert (tmp_path / "continuous" / "validation.json").is_file()
    timing = json.loads((tmp_path / "continuous" / "performance_summary.json").read_text())
    solver_records = [
        json.loads(row)
        for row in (tmp_path / "continuous" / "solver_records.jsonl").read_text().splitlines()
    ]
    native_calls = sum(len(row["attempts"]) for row in solver_records if not row["reused_tail"])
    assert timing["stages"]["solver"]["count"] == native_calls < 144
    assert timing["stages"]["forecast_evidence_hash"]["count"] == 4
    assert timing["stages"]["controller_chain_audit"]["count"] == 1
    assert (
        abs(
            sum(
                timing[name]
                for name in (
                    "simulation_seconds",
                    "validation_seconds",
                    "report_seconds",
                    "export_seconds",
                )
            )
            - timing["end_to_end_seconds"]
        )
        < 1e-6
    )
    interrupted = replace(context, output_dir=tmp_path / "interrupted")
    original = module.atomic_json

    def stop_after_checkpoint(path, data, **kwargs):
        original(path, data, **kwargs)
        if path.name == "checkpoint.json":
            raise Q4Error("test_interruption", "checkpoint committed")

    monkeypatch.setattr(module, "atomic_json", stop_after_checkpoint)
    with pytest.raises(Q4Error, match="test_interruption"):
        run_q4(interrupted)
    monkeypatch.setattr(module, "atomic_json", original)
    resumed = replace(interrupted, metadata={**metadata, "resume": True})
    monkeypatch.setattr(
        module, "load_q4_inputs", lambda *args: replace(inputs, source_hashes={"actual": "changed"})
    )
    with pytest.raises(InputError, match="mismatch"):
        run_q4(resumed)
    monkeypatch.setattr(module, "load_q4_inputs", lambda *args: inputs)
    log_path = tmp_path / "interrupted" / "solver_records.jsonl"
    original_bytes = log_path.read_bytes()
    altered = original_bytes.replace(b'"status": 0', b'"status": 9', 1)
    assert altered != original_bytes and len(altered) == len(original_bytes)
    log_path.write_bytes(altered)
    with pytest.raises(InputError, match="committed log prefix hash mismatch"):
        run_q4(resumed)
    log_path.write_bytes(original_bytes)
    # These uncommitted tails must be discarded after the committed prefixes validate.
    for name in module.LOG_NAMES:
        with (tmp_path / "interrupted" / f"{name}.jsonl").open("ab") as stream:
            stream.write(b'{"uncommitted": true}\n')
    recovered = run_q4(resumed)
    assert case_result_to_dict(recovered) == case_result_to_dict(continuous)
    validation = json.loads((tmp_path / "interrupted" / "validation.json").read_text())
    assert validation["ok"] and validation["checked_intervals"] == 144

    from microgrid.problem.q4_evidence import audit_controller_chain

    directory = tmp_path / "interrupted"
    for name, change, expected in (
        ("forecasts", lambda r: r["prices"].__setitem__(0, 99), "causal history replay"),
        ("solver_records", lambda r: r.__setitem__("forecast_id", "wrong"), "identity or status"),
        ("solver_records", lambda r: r["intent"].__setitem__("charge_kwh", 99), "intent binding"),
        (
            "forecasts",
            lambda r: r["traces"].__setitem__("load_unit", "wrong"),
            "causal history replay",
        ),
        (
            "solver_records",
            lambda r: r["forecast_view"].__setitem__("slice_start", 1),
            "slice reference mismatch",
        ),
        (
            "solver_records",
            lambda r: r["forecast_view"].__setitem__("slice_start", False),
            "slice reference mismatch",
        ),
        (
            "solver_records",
            lambda r: r["forecast_view"].__setitem__("full_snapshot_sha256", "wrong"),
            "slice reference mismatch",
        ),
        ("solver_records", lambda r: r.pop("forecast_view"), "slice reference mismatch"),
        (
            "dispatch_plans",
            lambda r: r["solver_record"]["forecast_view"].__setitem__("slice_length", 1),
            "native plan forecast view mismatch",
        ),
        (
            "dispatch_plans",
            lambda r: r.__setitem__("absolute_gap", float("nan")),
            "nonfinite/invalid",
        ),
        (
            "execution_feedback",
            lambda r: r["interval"].__setitem__("load_kw", 9999),
            "execution interval differs from domain_result",
        ),
    ):
        path = directory / f"{name}.jsonl"
        before = path.read_bytes()
        lines = before.decode().splitlines()
        data = json.loads(lines[0])
        change(data)
        lines[0] = json.dumps(data)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            with pytest.raises(InputError, match=expected):
                audit_controller_chain(
                    directory, inputs, plans_path=directory / "dispatch_plans.jsonl"
                )
        finally:
            path.write_bytes(before)


def test_actual_reserve_failure_retains_executed_interval_and_bill(tmp_path, monkeypatch):
    import datetime as dt

    import microgrid.problem.rolling_engine as module
    from microgrid.problem.contracts import BatteryAction
    from microgrid.problem.dispatch_feedback import apply_feedback
    from microgrid.problem.q4_common import ACTION_START
    from microgrid.problem.q4_export import load_replay_evidence
    from microgrid.problem.q4_validation import validate_q4_run
    from microgrid.problem.result_io import interval_from_dict

    inputs = Q4Inputs((600.0,) * 52560, (0.0,) * 52560, (1.0,) * 52560, (), {"actual": "hash"}, {})
    monkeypatch.setattr(module, "load_q4_inputs", lambda *args: inputs)
    monkeypatch.setattr(module, "require_approved_decisions", lambda *args: None)
    monkeypatch.setattr(module, "RESERVE_START", ACTION_START)
    monkeypatch.setattr(
        module,
        "apply_feedback",
        lambda intent, state, grid, measurement, **kwargs: apply_feedback(
            BatteryAction(0, 90), state, grid, measurement, **kwargs
        ),
    )
    directory = tmp_path / "reserve_failure"
    context = CaseContext(tmp_path, "q4_2", "test", directory, {"end_time": "2025-02-02"}, True)
    with pytest.raises(Q4Error, match="terminal_reserve_infeasible"):
        run_q4(context)
    failure = json.loads((directory / "failure.json").read_text())
    assert failure["completed_steps"] == 1
    assert failure["time"] == "2025-02-01 00:10:00"
    assert failure["energy_kwh"] == 5900
    ledger, executions = load_replay_evidence(directory, "q4_2")
    rows = [
        interval_from_dict(json.loads(line)["interval"])
        for line in (directory / "execution_feedback.jsonl").read_text().splitlines()
    ]
    assert len(ledger.bills) == len(rows) == len(executions) == 1
    assert not (directory / "domain_result.json").exists()
    validation = validate_q4_run(
        rows,
        executions,
        ledger,
        end_time=ACTION_START + dt.timedelta(days=1),
        reserve_start=ACTION_START,
    )
    assert "2025-02-01 00:10:00:terminal_reserve_infeasible" in validation["violations"]
