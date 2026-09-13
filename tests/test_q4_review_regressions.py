"""Regression probes for the four issues reported in the Q4 v3 review."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace

import pytest
from test_release_guards import _ready_repo

from microgrid.checks import check_selected_run
from microgrid.problem.contracts import BatteryAction, BatteryState
from microgrid.problem.dispatch_feedback import Measurement, apply_feedback, validate_execution
from microgrid.problem.q4_common import STEP, YEAR_START
from microgrid.problem.q4_inputs import Q4Inputs, load_q4_inputs
from microgrid.schemas import InputError


def test_missing_timestamp_with_value_is_rejected(tmp_path, monkeypatch):
    import microgrid.problem.q4_inputs as module

    rows = [
        {"parsed_timestamp": (YEAR_START + (i + 1) * STEP).isoformat(), "value": 1.0}
        for i in range(52560)
    ]
    rows.insert(144, {"parsed_timestamp": None, "value": 999999.0})
    monkeypatch.setattr(module, "verify_required_inputs", lambda *args: [])
    monkeypatch.setattr(module, "read_wide_attachment", lambda *args, **kwargs: rows)
    with pytest.raises(InputError, match="record 145:invalid timestamp/value"):
        load_q4_inputs(tmp_path, "q4_2")


def test_arbitrary_cancellation_of_charge_is_rejected():
    good = apply_feedback(BatteryAction(100), BatteryState(6000), 200, Measurement(50, 0))
    bad = replace(
        good,
        action=BatteryAction(),
        state_end=BatteryState(6000),
        grid_used_kwh=50,
        unused_grid_kwh=150,
        charge_reduction_kwh=100,
    )
    assert "feedback_charge" in validate_execution(bad)
    assert validate_execution(good) == ()


def test_arbitrary_cancellation_of_discharge_is_rejected():
    good = apply_feedback(
        BatteryAction(discharge_kwh=90), BatteryState(6000), 0, Measurement(100, 0)
    )
    bad = replace(
        good,
        action=BatteryAction(),
        state_end=BatteryState(6000),
        emergency_kwh=100,
        discharge_reduction_kwh=90,
    )
    assert "feedback_discharge" in validate_execution(bad)
    assert validate_execution(good) == ()


@pytest.mark.parametrize("broken", ["missing", "conflicting"])
def test_v3_export_reserve_snapshot_is_required(tmp_path, broken):
    repo = _ready_repo(tmp_path)
    directory = repo / "outputs/runs/q4_2/q4_2-run-test"
    assert check_selected_run(repo, "q4_2", "q4_2-run-test") == []
    path = directory / "export_manifest.json"
    manifest = json.loads(path.read_text())
    if broken == "missing":
        del manifest["decision_snapshot"]["D_TERMINAL_RESERVE_Q4"]
    else:
        manifest["decision_snapshot"]["D_TERMINAL_RESERVE_Q4"]["choice"] = "E>=1200"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert any(
        "D_TERMINAL_RESERVE_Q4" in issue
        for issue in check_selected_run(repo, "q4_2", "q4_2-run-test")
    )


def test_forecast_and_solver_logs_cannot_be_missing(tmp_path):
    repo = _ready_repo(tmp_path)
    directory = repo / "outputs/runs/q4_2/q4_2-run-test"
    (directory / "forecasts.jsonl").unlink()
    (directory / "solver_records.jsonl").unlink()
    issues = check_selected_run(repo, "q4_2", "q4_2-run-test")
    assert any("forecasts.jsonl" in issue for issue in issues)
    assert any("solver_records.jsonl" in issue for issue in issues)


@pytest.mark.parametrize("broken", ["length", "time_keys"])
def test_actual_arrays_require_common_complete_time_keys(broken):
    inputs = Q4Inputs((1.0,) * 52560, (1.0,) * 52560, (1.0,) * 52560, (), {}, {})
    with pytest.raises(InputError, match="common complete"):
        if broken == "length":
            replace(inputs, prices=(1.0,) * 52561)
        else:
            replace(inputs, time_keys=(YEAR_START + dt.timedelta(days=1),) + inputs.time_keys[1:])


def test_failed_execution_status_and_wrong_reduction_are_rejected():
    good = apply_feedback(BatteryAction(100), BatteryState(6000), 200, Measurement(50, 0))
    assert "execution_status" in validate_execution(replace(good, status="execution_infeasible"))
    assert "charge_reduction" in validate_execution(replace(good, charge_reduction_kwh=10))


@pytest.mark.parametrize("log", ["forecasts", "solver_records"])
def test_same_size_log_corruption_is_rejected(tmp_path, log):
    repo = _ready_repo(tmp_path)
    directory = repo / "outputs/runs/q4_2/q4_2-run-test"
    path = directory / f"{log}.jsonl"
    before = path.read_bytes()
    after = before.replace(b'"status":0', b'"status":9')
    assert len(after) == len(before) and before != after
    path.write_bytes(after)
    assert any(
        f"{log}.jsonl" in issue for issue in check_selected_run(repo, "q4_2", "q4_2-run-test")
    )


@pytest.mark.parametrize(
    "broken", ["model_version", "terminal_reserve", "runtime_snapshot", "runtime_scope"]
)
def test_v3_configuration_and_runtime_decision_are_bound(tmp_path, broken):
    repo = _ready_repo(tmp_path)
    directory = repo / "outputs/runs/q4_2/q4_2-run-test"
    if broken.startswith("runtime"):
        path = directory / "manifest.json"
        data = json.loads(path.read_text())
        snapshot = data["config_snapshot"]["decisions.toml"]["decisions"]["D_TERMINAL_RESERVE_Q4"]
        snapshot["choice" if broken == "runtime_snapshot" else "scope_cases"] = (
            "changed" if broken == "runtime_snapshot" else ["q3"]
        )
    else:
        path = directory / "effective_config.json"
        data = json.loads(path.read_text())
        if broken == "model_version":
            data["model_version"] = "q4-unknown"
        else:
            data["terminal_reserve"]["minimum_energy_kwh"] = 1200
    path.write_text(json.dumps(data), encoding="utf-8")
    assert check_selected_run(repo, "q4_2", "q4_2-run-test")
