"""Reject trajectory drift and broken evidence while allowing timing changes."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

from microgrid.schemas import InputError

compare_records = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts/compare_q4_annual_replay.py")
)["compare_records"]


def write_rows(directory, name, rows):
    directory.mkdir(exist_ok=True)
    (directory / (name + ".jsonl")).write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_annual_compare_rejects_changed_contract_even_with_unchanged_fee(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    old = [{"quantities": [100.0, 90.0], "trade_price": None}]
    new = [{"quantities": [90.0, 100.0], "trade_price": None}]
    write_rows(before, "contracts", old)
    write_rows(after, "contracts", new)
    with pytest.raises(InputError, match="contracts differs"):
        compare_records(before, after, "contracts", 1)


def test_annual_compare_rejects_forecast_provenance_change(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    write_rows(before, "forecasts", [{"pv_kwh": [20.0], "traces": {"source": "visible"}}])
    write_rows(after, "forecasts", [{"pv_kwh": [20.0], "traces": {"source": "future"}}])
    with pytest.raises(InputError, match="forecasts differs"):
        compare_records(before, after, "forecasts", 1)


@pytest.mark.parametrize("extra", [False, True])
def test_annual_compare_rejects_missing_or_additional_records(tmp_path, extra):
    before, after = tmp_path / "before", tmp_path / "after"
    write_rows(before, "cost_ledger", [{"total": 100.0}])
    write_rows(after, "cost_ledger", [{"total": 100.0}] * (2 if extra else 0))
    with pytest.raises(InputError, match="coverage differs"):
        compare_records(before, after, "cost_ledger", 1)


def test_annual_compare_solver_ignores_timing_after_checking_new_intent(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    intent = {"charge_kwh": 100.0, "discharge_kwh": 0.0}
    old = {"objective": 25.0, "elapsed_seconds": 1.0, "attempts": [{"elapsed_seconds": 0.9}]}
    new = {
        "objective": 25.0,
        "elapsed_seconds": 0.5,
        "attempts": [{"elapsed_seconds": 0.4}],
        "intent": intent,
        "forecast_view": {"slice_start": 0},
    }
    write_rows(before, "solver_records", [old])
    write_rows(after, "solver_records", [new])
    write_rows(after, "execution_feedback", [{"execution": {"intent": intent}}])
    assert compare_records(before, after, "solver_records", 1) == 1
    write_rows(
        after, "solver_records", [{**new, "intent": {"charge_kwh": 0.0, "discharge_kwh": 0.0}}]
    )
    with pytest.raises(InputError, match="solver intent differs"):
        compare_records(before, after, "solver_records", 1)


@pytest.mark.parametrize(
    "field,value",
    [("model_version", "q4-v2"), ("end_time", "2025-02-02 00:00:00"), ("timely_control", False)],
)
def test_annual_compare_requires_v3_full_year_and_approved_response(
    tmp_path, monkeypatch, field, value
):
    function = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts/compare_q4_annual_replay.py")
    )["checked_run"]
    namespace = function.__globals__
    # Isolate scope validation from inventory/source checks already tested elsewhere.
    monkeypatch.setitem(namespace, "check_inventory", lambda *args: [])
    monkeypatch.setitem(namespace, "check_model_binding", lambda *args: [])
    monkeypatch.setitem(namespace, "source_tree_hash", lambda *args: "source")
    monkeypatch.setitem(namespace, "ANNUAL_STEPS", 1)
    identity = {"case_id": "q4_2", "run_id": "test", "status": "success", "is_synthetic": False}
    config = {
        "case_id": "q4_2",
        "is_synthetic": False,
        "price_method": "main",
        "model_version": "q4-v3-reserve",
        "end_time": "2026-01-01 00:00:00",
        "timely_control": True,
    }
    documents = {
        "manifest.json": {**identity, "source_hash": "source"},
        "effective_config.json": config,
        "summary.json": {
            **identity,
            "price_method": "main",
            "full_annual": True,
            "interval_count": 1,
            "validation_ok": True,
            "terminal_energy_kwh": 6000,
            "source_hashes": {"actual": "input"},
        },
        "domain_result.json": {**identity, "intervals": [{}]},
        "input_snapshot.json": {"source_hashes": {"actual": "input"}},
    }
    monkeypatch.setitem(namespace, "read_json", lambda path: documents[path.name])
    assert function(tmp_path, "q4_2", "main", "test")[2] == config
    config[field] = value
    with pytest.raises(InputError, match="identity/status/coverage mismatch"):
        function(tmp_path, "q4_2", "main", "test")
