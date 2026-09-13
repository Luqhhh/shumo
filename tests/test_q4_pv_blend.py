from __future__ import annotations

import datetime as dt
import json
import shutil
import tomllib
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from microgrid.problem.contracts import CaseContext, InfoItem, InfoSet
from microgrid.problem.q4_common import (
    ACTION_START,
    BLEND_MODEL_VERSION,
    STEP,
    YEAR_START,
    Q4Error,
    pv_blend_parameters,
    reserve_start_from_config,
)
from microgrid.problem.q4_evidence import audit_controller_chain, check_model_binding, prefix_sha256
from microgrid.problem.q4_inputs import Q4Inputs
from microgrid.problem.q4_pv_blend import LongPVBlend
from microgrid.problem.result_io import case_result_to_dict
from microgrid.problem.rolling_engine import _initialize_forecaster, run_q4
from microgrid.schemas import InputError, PendingDecisionError


def toy_inputs():
    official, issue = [], YEAR_START
    while issue <= ACTION_START + dt.timedelta(days=15):
        for h in range(1, 25):
            official.append(InfoItem("pv_forecast_kw", issue, issue + dt.timedelta(hours=h), 200))
        issue += dt.timedelta(hours=6)
    return Q4Inputs(
        (600.0,) * 52560,
        (20.0,) * 52560,
        (1.0,) * 52560,
        tuple(official),
        {"actual": "hash"},
        {"source_hashes": {"actual": "hash"}},
    )


def test_blend_cold_start_boundaries_and_frozen_proxy_groups():
    blend = LongPVBlend()
    first, trace = blend.apply(ACTION_START, [100.0] * 144, [20.0] * 144)
    assert first == (100.0,) * 144 and trace["cold_start"]
    assert trace["points"][71]["bucket"] is None
    assert trace["points"][72]["bucket"] == trace["points"][107]["bucket"] == "12-18h"
    assert trace["points"][108]["bucket"] == "18-24h"
    assert blend.pending_count == 72
    assert blend.apply(ACTION_START, [100.0] * 144, [20.0] * 144) == (first, trace)
    assert blend.pending_count == 72  # same issue cannot double-count training pairs
    # Future group is fixed from H=20, although the realized PV becomes zero.
    blend.observe(ACTION_START + 73 * STEP, 0.0)
    warm, trace = blend.apply(ACTION_START + dt.timedelta(days=7), [100.0] * 144, [20.0] * 144)
    p = trace["points"][72]
    assert not trace["cold_start"] and p["sample_count"] == 1
    assert (p["original_mae_kw"], p["history_mae_kw"]) == (100.0, 20.0)
    assert p["alpha"] == pytest.approx(21**2 / (101**2 + 21**2))
    assert warm[:72] == (100.0,) * 72
    assert warm[72] == pytest.approx(p["alpha"] * 100 + (1 - p["alpha"]) * 20)
    assert p["history_proxy_group"] == "history_gt_1kw"
    assert warm[108:] == (100.0,) * 36  # other lead bucket has no realized samples


def test_blend_counts_original_issue_pairs_and_excludes_window_lower_endpoint():
    blend = LongPVBlend()
    blend.apply(ACTION_START, [100.0] * 144, [1.0] * 144)
    second = ACTION_START + dt.timedelta(hours=6)
    blend.apply(second, [200.0] * 144, [1.0] * 144)
    target = ACTION_START + 109 * STEP
    blend.observe(target, 10.0)  # two old vintages, different original lead buckets
    values, trace = blend.apply(ACTION_START + dt.timedelta(days=7), [100.0] * 144, [1.0] * 144)
    assert trace["points"][72]["original_mae_kw"] == 190
    assert trace["points"][108]["original_mae_kw"] == 90
    assert trace["points"][72]["history_proxy_group"] == "history_le_1kw"
    assert values[72] < 100
    # Exact target == r-28 days is excluded, not retained in MAE.
    expiry = target + dt.timedelta(days=28)
    blend.apply(expiry.replace(hour=0, minute=0) + dt.timedelta(days=1), [100.0] * 144, [1.0] * 144)
    assert len(blend.realized) == 0


def test_blend_excludes_exact_28_day_lower_endpoint_but_retains_later_target():
    blend = LongPVBlend()
    blend.apply(ACTION_START, [100.0] * 144, [20.0] * 144)
    target = ACTION_START + dt.timedelta(hours=18)
    blend.observe(target, 10.0)
    blend.observe(target + STEP, 10.0)
    _, trace = blend.apply(target + dt.timedelta(days=28), [100.0] * 144, [20.0] * 144)
    assert len(blend.realized) == 1
    assert trace["points"][72]["sample_count"] == 0
    assert trace["points"][108]["sample_count"] == 1


@pytest.mark.parametrize(
    "broken",
    ["runtime_model", "runtime_eval", "export_model", "export_eval", "parameters", "downgrade"],
)
def test_blend_binding_rejects_missing_or_conflicting_supplement(tmp_path, broken):
    (tmp_path / "configs").mkdir()
    shutil.copyfile(
        Path(__file__).parents[1] / "configs/decisions.toml", tmp_path / "configs/decisions.toml"
    )
    decisions = tomllib.loads((tmp_path / "configs/decisions.toml").read_text())["decisions"]
    run = tmp_path / "run"
    run.mkdir()
    manifest = {
        "case_id": "q4_3",
        "is_synthetic": False,
        "status": "running",
        "config_snapshot": {"decisions.toml": {"decisions": deepcopy(decisions)}},
    }
    config = {
        "case_id": "q4_3",
        "is_synthetic": False,
        "model_version": BLEND_MODEL_VERSION,
        "end_time": "2026-01-01 00:00:00",
        "price_method": "main",
        "timely_control": True,
        "terminal_reserve": {"start": "2025-12-31 00:00:00", "minimum_energy_kwh": 6000.0},
        "pv_blend": pv_blend_parameters(),
        "solver": {
            "presolve": True,
            "mip_rel_gap": 1e-4,
            "time_limits_seconds": [10, 60],
            "mip_feasibility_tolerance": 1e-9,
            "primal_feasibility_tolerance": 1e-8,
        },
    }
    export = {"model_version": BLEND_MODEL_VERSION, "decision_snapshot": deepcopy(decisions)}

    def save():
        (run / "manifest.json").write_text(json.dumps(manifest))
        (run / "effective_config.json").write_text(json.dumps(config))

    save()
    assert check_model_binding(tmp_path, run, export) == []
    if broken.startswith("runtime"):
        key = "D_PV_BLEND_LONG_Q4" if broken.endswith("model") else "D_EVAL_PV_BLEND_LONG_Q4"
        manifest["config_snapshot"]["decisions.toml"]["decisions"].pop(key)
    elif broken.startswith("export"):
        key = "D_PV_BLEND_LONG_Q4" if broken.endswith("model") else "D_EVAL_PV_BLEND_LONG_Q4"
        export["decision_snapshot"].pop(key)
    elif broken == "parameters":
        config["pv_blend"]["mae_smoothing_kw"] = 2.0
    else:
        config["model_version"] = "q4-v3-reserve"
        export["model_version"] = "q4-v3-reserve"
    save()
    assert check_model_binding(tmp_path, run, export)
    # New approval does not retroactively require a blend snapshot in old v3.
    config["model_version"] = export["model_version"] = "q4-v3-reserve"
    config.pop("pv_blend")
    save()
    assert check_model_binding(tmp_path, run, export) == []


@pytest.mark.parametrize("case,price", [("q4_2", "main"), ("q4_3", "lag1")])
def test_blend_rejects_unapproved_case_or_price_scope(tmp_path, monkeypatch, case, price):
    import microgrid.problem.rolling_engine as module

    monkeypatch.setattr(module, "require_approved_decisions", lambda *args: None)
    context = CaseContext(
        tmp_path, case, metadata={"pv_method": "blend-long", "price_method": price}
    )
    with pytest.raises(InputError, match="only for Q4-3/main"):
        run_q4(context)
    assert not (tmp_path / "outputs/runs").exists()


def test_blend_missing_supplement_approval_blocks_before_input(tmp_path, monkeypatch):
    import microgrid.problem.rolling_engine as module

    def gates(repo, ids):
        if "D_PV_BLEND_LONG_Q4" in ids:
            raise PendingDecisionError(ids)

    monkeypatch.setattr(module, "require_approved_decisions", gates)
    with pytest.raises(PendingDecisionError):
        run_q4(CaseContext(tmp_path, "q4_3", metadata={"pv_method": "blend-long"}))
    assert not (tmp_path / "outputs/runs").exists()


@pytest.mark.parametrize("field", ["error_window_days", "history_days", "cold_start_days"])
def test_blend_config_cannot_claim_changed_parameters(field):
    config = {
        "model_version": BLEND_MODEL_VERSION,
        "case_id": "q4_3",
        "price_method": "main",
        "terminal_reserve": {"start": "2025-12-31 00:00:00", "minimum_energy_kwh": 6000.0},
        "pv_blend": pv_blend_parameters(),
    }
    assert reserve_start_from_config(config) == dt.datetime(2025, 12, 31)
    config["pv_blend"][field] += 1
    with pytest.raises(Q4Error, match="differs from approval"):
        reserve_start_from_config(config)


def test_blend_preserves_official_layer_short_window_and_causal_visibility():
    inputs = toy_inputs()
    base = _initialize_forecaster(inputs, "q4_3", "main", ACTION_START)
    blend = _initialize_forecaster(
        inputs, "q4_3", "main", ACTION_START, model_version=BLEND_MODEL_VERSION
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
            blend.ingest(info)
        if k % 36 == 0:
            a, b = (s.refresh(InfoSet.from_raw(slot, ())) for s in (base, blend))
            assert a.load_kwh == b.load_kwh and a.prices == b.prices
            assert a.traces["pv"] == b.traces["pv"]
            assert a.pv_kwh[:72] == b.pv_kwh[:72]
            if k < 1008:
                assert a.pv_kwh == b.pv_kwh
            else:
                assert a.pv_kwh[72:] != b.pv_kwh[72:]
    frozen = b.sliced(slot + 35 * STEP)
    assert frozen.traces["pv_blend"] == b.traces["pv_blend"]
    altered = deepcopy(blend)
    altered.ingest(
        InfoSet.from_raw(slot, (InfoItem("pv_actual_kw", slot + STEP, slot + STEP, 999999),))
    )
    assert altered.refresh(InfoSet.from_raw(slot, ())) == b


def test_blend_resume_rebuilds_committed_pairs_and_rejects_coherent_tampering(
    tmp_path, monkeypatch
):
    import microgrid.problem.rolling_engine as module

    inputs = toy_inputs()
    monkeypatch.setattr(module, "load_q4_inputs", lambda *args: inputs)
    monkeypatch.setattr(module, "require_approved_decisions", lambda *args: None)
    metadata = {"end_time": "2025-02-09", "pv_method": "blend-long"}
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
    cp["training_state"]["pv_blend"]["history_sha256"] = "0" * 64
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
    changed = old.replace(b'"alpha": 1.0', b'"alpha": 0.9', 1)
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
    # Even when a log hash is not the gate, causal replay itself rejects changed alpha.
    path = context.output_dir / "forecasts.jsonl"
    path.write_bytes(path.read_bytes().replace(b'"alpha": 1.0', b'"alpha": 0.9', 1))
    with pytest.raises(InputError, match="forecast differs from causal history"):
        audit_controller_chain(
            context.output_dir, inputs, plans_path=context.output_dir / "dispatch_plans.jsonl"
        )
