from __future__ import annotations

import datetime as dt
import json
from copy import deepcopy
from dataclasses import replace

import pytest
from test_q4_pv_blend import toy_inputs

from microgrid.problem.contracts import CaseContext, InfoSet
from microgrid.problem.q4_common import ACTION_START, BIAS_MODEL_VERSION, STEP, Q4Error
from microgrid.problem.q4_evidence import audit_controller_chain, prefix_sha256
from microgrid.problem.q4_pv_bias import LongPVBias
from microgrid.problem.result_io import case_result_to_dict
from microgrid.problem.rolling_engine import _initialize_forecaster, run_q4
from microgrid.schemas import InputError, PendingDecisionError


def test_bias_signed_correction_and_short_cold_empty_groups():
    bias = LongPVBias()
    cold, trace = bias.apply(ACTION_START, [100.0] * 144, [20.0] * 144)
    assert cold == (100.0,) * 144 and trace["cold_start"]
    assert bias.apply(ACTION_START, [100.0] * 144, [20.0] * 144) == (cold, trace)
    assert bias.pending_count == 72
    bias.observe(ACTION_START + 73 * STEP, 10.0)
    warm, trace = bias.apply(ACTION_START + dt.timedelta(days=7), [100.0] * 144, [20.0] * 144)
    p = trace["points"][72]
    assert p["sample_count"] == 1 and p["original_bias_kw"] == -90.0
    assert p["bias_correction_kw"] == -90.0 and warm[72] == 10.0
    assert warm[:72] == (100.0,) * 72
    assert warm[108:] == (100.0,) * 36
    # Proxy comes from saved H; observing actual zero does not move that pair.
    assert p["history_proxy_group"] == "history_gt_1kw"


def test_bias_positive_and_nonnegative_clipping_use_saved_original_errors():
    bias = LongPVBias()
    bias.apply(ACTION_START, [20.0] * 144, [0.0] * 144)
    bias.observe(ACTION_START + 109 * STEP, 120.0)
    warm, trace = bias.apply(ACTION_START + dt.timedelta(days=7), [10.0] * 144, [0.0] * 144)
    assert warm[108] == 110.0 and trace["points"][108]["original_bias_kw"] == 100.0
    bias.observe(ACTION_START + dt.timedelta(days=7) + 109 * STEP, 0.0)
    values, trace = bias.apply(ACTION_START + dt.timedelta(days=14), [1.0] * 144, [0.0] * 144)
    assert trace["points"][108]["sample_count"] == 2
    assert values[108] == 46.0  # original F=10, not prior corrected output 110
    other = LongPVBias()
    other.apply(ACTION_START, [100.0] * 144, [20.0] * 144)
    other.observe(ACTION_START + 73 * STEP, 0.0)
    values, _ = other.apply(ACTION_START + dt.timedelta(days=7), [1.0] * 144, [20.0] * 144)
    assert values[72] == 0.0


def test_bias_exact_lower_window_endpoint_is_excluded():
    bias = LongPVBias()
    bias.apply(ACTION_START, [100.0] * 144, [20.0] * 144)
    target = ACTION_START + dt.timedelta(hours=18)
    bias.observe(target, 10.0)
    bias.observe(target + STEP, 20.0)
    _, trace = bias.apply(target + dt.timedelta(days=28), [100.0] * 144, [20.0] * 144)
    assert len(bias.realized) == 1
    assert trace["points"][72]["sample_count"] == 0
    assert trace["points"][108]["original_bias_kw"] == -80.0


def test_bias_service_causality_and_unchanged_raw_layers():
    inputs = toy_inputs()
    base = _initialize_forecaster(inputs, "q4_3", "main", ACTION_START)
    bias = _initialize_forecaster(
        inputs, "q4_3", "main", ACTION_START, model_version=BIAS_MODEL_VERSION
    )
    for k in range(1009):
        slot = ACTION_START + k * STEP
        if k:
            info = InfoSet.from_raw(
                slot,
                inputs.actual_info(inputs.index(slot) - 1)
                + tuple(x for x in inputs.official if x.available_at == slot),
            )
            base.ingest(info)
            bias.ingest(info)
        if k % 36 == 0:
            a, b = (s.refresh(InfoSet.from_raw(slot, ())) for s in (base, bias))
            assert (
                a.load_kwh == b.load_kwh
                and a.prices == b.prices
                and a.terminal_value == b.terminal_value
            )
            assert a.traces == {key: value for key, value in b.traces.items() if key != "pv_bias"}
            assert a.pv_kwh[:72] == b.pv_kwh[:72]
            assert k >= 1008 or a.pv_kwh == b.pv_kwh
    assert a.pv_kwh[72:] != b.pv_kwh[72:]
    assert deepcopy(bias).refresh(InfoSet.from_raw(slot, ())) == b


def test_bias_missing_delegation_blocks_before_input(tmp_path, monkeypatch):
    import microgrid.problem.rolling_engine as module

    def gates(repo, ids):
        if "D_OPTIMIZATION_Q4" in ids:
            raise PendingDecisionError(ids)

    monkeypatch.setattr(module, "require_approved_decisions", gates)
    with pytest.raises(PendingDecisionError):
        run_q4(CaseContext(tmp_path, "q4_3", metadata={"pv_method": "bias-long"}))
    assert not (tmp_path / "outputs/runs").exists()


def test_bias_resume_rebuilds_committed_pairs_and_rejects_coherent_tampering(tmp_path, monkeypatch):
    import microgrid.problem.rolling_engine as module

    inputs = toy_inputs()
    monkeypatch.setattr(module, "load_q4_inputs", lambda *args: inputs)
    monkeypatch.setattr(module, "require_approved_decisions", lambda *args: None)
    metadata = {"end_time": "2025-02-09", "pv_method": "bias-long"}
    context = CaseContext(tmp_path, "q4_3", "test", tmp_path / "continuous", metadata, True)
    continuous = run_q4(context)
    interrupted = replace(context, output_dir=tmp_path / "interrupted")
    atomic = module.atomic_json

    def stop(path, data, **kwargs):
        atomic(path, data, **kwargs)
        if path.name == "checkpoint.json" and str(data["time"]) == "2025-02-08 12:00:00":
            raise Q4Error("test_interruption", "checkpoint committed after activation")

    monkeypatch.setattr(module, "atomic_json", stop)
    with pytest.raises(Q4Error, match="test_interruption"):
        run_q4(interrupted)
    monkeypatch.setattr(module, "atomic_json", atomic)
    recovered_context = replace(interrupted, metadata={**metadata, "resume": True})
    cp_path = interrupted.output_dir / "checkpoint.json"
    cp_bytes = cp_path.read_bytes()
    cp = json.loads(cp_bytes)
    cp["training_state"]["pv_bias"]["history_sha256"] = "0" * 64
    cp_path.write_text(json.dumps(cp))
    with pytest.raises(InputError, match="training state mismatch"):
        run_q4(recovered_context)
    cp = json.loads(cp_bytes)
    cp["forecast"]["pv_kwh"][80] += 1
    cp_path.write_text(json.dumps(cp))
    with pytest.raises(InputError, match="frozen forecast differs"):
        run_q4(recovered_context)
    cp = json.loads(cp_bytes)
    forecast_path = interrupted.output_dir / "forecasts.jsonl"
    old = forecast_path.read_bytes()
    changed = old.replace(b'"bias_correction_kw": 0.0', b'"bias_correction_kw": 0.9', 1)
    assert changed != old and len(changed) == len(old)
    forecast_path.write_bytes(changed)
    cp["log_sha256"]["forecasts"] = prefix_sha256(forecast_path, cp["log_sizes"]["forecasts"])
    cp_path.write_text(json.dumps(cp))
    with pytest.raises(InputError, match="causal prefix replay"):
        run_q4(recovered_context)
    forecast_path.write_bytes(old)
    cp_path.write_bytes(cp_bytes)
    for name in module.LOG_NAMES:
        with (interrupted.output_dir / (name + ".jsonl")).open("ab") as stream:
            stream.write(b'{"uncommitted":true}\n')
    recovered = run_q4(recovered_context)
    assert case_result_to_dict(recovered) == case_result_to_dict(continuous)
    assert (interrupted.output_dir / "daily_summary.csv").read_bytes() == (
        context.output_dir / "daily_summary.csv"
    ).read_bytes()
    for name in ("forecasts", "contracts", "execution_feedback", "cost_ledger"):
        assert (interrupted.output_dir / (name + ".jsonl")).read_bytes() == (
            context.output_dir / (name + ".jsonl")
        ).read_bytes()
    # Even when a log hash is not the gate, causal replay itself rejects changed bias.
    path = context.output_dir / "forecasts.jsonl"
    path.write_bytes(
        path.read_bytes().replace(b'"bias_correction_kw": 0.0', b'"bias_correction_kw": 0.9', 1)
    )
    with pytest.raises(InputError, match="forecast differs from causal history"):
        audit_controller_chain(
            context.output_dir, inputs, plans_path=context.output_dir / "dispatch_plans.jsonl"
        )


@pytest.mark.parametrize("broken", ["runtime", "export", "parameters", "downgrade"])
def test_bias_binding_rejects_delegation_or_config_conflict(tmp_path, broken):
    import shutil
    import tomllib
    from pathlib import Path

    from microgrid.problem.q4_common import pv_bias_parameters
    from microgrid.problem.q4_evidence import check_model_binding

    (tmp_path / "configs").mkdir()
    shutil.copyfile(
        Path(__file__).parents[1] / "configs/decisions.toml", tmp_path / "configs/decisions.toml"
    )
    decisions = tomllib.loads((tmp_path / "configs/decisions.toml").read_text())["decisions"]
    manifest = {
        "case_id": "q4_3",
        "is_synthetic": False,
        "status": "running",
        "config_snapshot": {"decisions.toml": {"decisions": deepcopy(decisions)}},
    }
    config = {
        "case_id": "q4_3",
        "is_synthetic": False,
        "model_version": BIAS_MODEL_VERSION,
        "end_time": "2026-01-01 00:00:00",
        "price_method": "main",
        "timely_control": True,
        "terminal_reserve": {"start": "2025-12-31 00:00:00", "minimum_energy_kwh": 6000.0},
        "optimization": pv_bias_parameters(),
        "solver": {
            "presolve": True,
            "mip_rel_gap": 1e-4,
            "time_limits_seconds": [10, 60],
            "mip_feasibility_tolerance": 1e-9,
            "primal_feasibility_tolerance": 1e-8,
        },
    }
    export = {"model_version": BIAS_MODEL_VERSION, "decision_snapshot": deepcopy(decisions)}

    def save():
        (tmp_path / "manifest.json").write_text(json.dumps(manifest))
        (tmp_path / "effective_config.json").write_text(json.dumps(config))

    save()
    assert check_model_binding(tmp_path, tmp_path, export) == []
    if broken == "runtime":
        manifest["config_snapshot"]["decisions.toml"]["decisions"].pop("D_OPTIMIZATION_Q4")
    elif broken == "export":
        export["decision_snapshot"].pop("D_OPTIMIZATION_Q4")
    elif broken == "parameters":
        config["optimization"]["error_window_days"] = 29
    else:
        config["model_version"] = export["model_version"] = "q4-v3-reserve"
    save()
    assert check_model_binding(tmp_path, tmp_path, export)
