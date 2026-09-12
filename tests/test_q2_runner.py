from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from microgrid.cases import required_decisions
from microgrid.problem.contracts import (
    BatteryAction,
    BatteryState,
    CaseContext,
    CaseResult,
    CostBreakdown,
    IntervalResult,
)
from microgrid.problem.q2 import run
from microgrid.problem.q2_engine import Q2EngineeringResult
from microgrid.problem.q2_inputs import FixedPricePoint, Q2InputBundle
from microgrid.problem.result_io import load_case_result
from microgrid.schemas import InputError, PendingDecisionError


def _write_decisions(repo: Path, *, blocked_id: str | None = None) -> None:
    config_dir = repo / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    for decision_id in required_decisions("q2"):
        status = "proposed" if decision_id == blocked_id else "approved"
        confirmed_by = "" if status == "proposed" else "tester"
        confirmed_at = "" if status == "proposed" else "2026-09-11T00:00:00+08:00"
        blocks.extend(
            [
                f"[decisions.{decision_id}]",
                f'status = "{status}"',
                'choice = "test fixture"',
                'rationale = "test fixture"',
                f'confirmed_by = "{confirmed_by}"',
                f'confirmed_at = "{confirmed_at}"',
                'source = "test fixture"',
                "",
            ]
        )
    (config_dir / "decisions.toml").write_text("\n".join(blocks), encoding="utf-8")


def _fake_engineering_result() -> Q2EngineeringResult:
    day = dt.date(2025, 2, 1)
    state = BatteryState(6000.0)
    intervals = tuple(
        IntervalResult(
            day=day,
            slot=slot,
            load_kw=0.0,
            pv_kw=0.0,
            planned_purchase_kwh=0.0,
            adjusted_purchase_kwh=0.0,
            emergency_purchase_kwh=0.0,
            action=BatteryAction(),
            state_start=state,
            state_end=state,
            source_ref="synthetic",
        )
        for slot in range(144)
    )
    return Q2EngineeringResult(
        intervals=intervals,
        costs=CostBreakdown(),
        forecast_records=(),
        solver_records=(),
        metadata={"validation": {"ok": True}},
        is_synthetic=True,
    )


def test_q2_runner_writes_internal_synthetic_artifact_chain(tmp_path: Path, monkeypatch) -> None:
    import microgrid.problem.q2 as q2

    repo = tmp_path / "repo"
    _write_decisions(repo)
    attachment1 = tmp_path / "attachment1.xlsx"
    attachment2 = tmp_path / "attachment2.xlsx"
    attachment1.write_bytes(b"synthetic attachment 1")
    attachment2.write_bytes(b"synthetic attachment 2")
    monkeypatch.setattr(q2, "load_q2_inputs", lambda **kwargs: Q2InputBundle((), (), ()))
    monkeypatch.setattr(q2, "run_q2_engineering", lambda bundle, config: _fake_engineering_result())

    output_dir = tmp_path / "run"
    result = run(
        CaseContext(
            repo_root=repo,
            case_id="q2",
            run_id="synthetic-q2",
            output_dir=output_dir,
            is_synthetic=True,
            metadata={
                "attachment1_path": str(attachment1),
                "attachment2_path": str(attachment2),
                "load_sheet_name": "负载",
                "pv_sheet_name": "光伏",
                "action_start_day": "2025-02-01",
                "action_end_day": "2025-02-01",
            },
        )
    )

    assert isinstance(result, CaseResult)
    assert result.is_synthetic is True
    for name in (
        "manifest.json",
        "input_snapshot.json",
        "domain_result.json",
        "validation.json",
        "summary.json",
        "solver.log",
    ):
        assert (output_dir / name).is_file()
    assert not list(output_dir.glob("*.xlsx"))
    assert load_case_result(output_dir / "domain_result.json").intervals == result.intervals


def test_q2_runner_stops_at_proposed_decision_before_allocating_output(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_decisions(repo, blocked_id="D_MPC")
    with pytest.raises(PendingDecisionError):
        run(CaseContext(repo_root=repo, case_id="q2", run_id="blocked"))
    assert not (repo / "outputs").exists()


def test_q2_runner_records_synthetic_missing_input_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_decisions(repo)
    output_dir = tmp_path / "failed-run"
    with pytest.raises(InputError, match="attachment1"):
        run(
            CaseContext(
                repo_root=repo,
                case_id="q2",
                run_id="missing-input",
                output_dir=output_dir,
                is_synthetic=True,
                metadata={
                    "attachment1_path": str(tmp_path / "missing-1.xlsx"),
                    "attachment2_path": str(tmp_path / "missing-2.xlsx"),
                },
            )
        )
    assert (output_dir / "manifest.json").is_file()
    assert (output_dir / "failure.json").is_file()


def test_q2_defaults_cover_official_full_year_and_write_complete_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    import json
    import microgrid.problem.q2 as q2
    repo = tmp_path / "repo"
    _write_decisions(repo)
    attachment1 = repo / "data" / "raw" / "附件1.xlsx"
    attachment2 = repo / "data" / "raw" / "附件2.xlsx"
    attachment1.parent.mkdir(parents=True)
    attachment1.write_bytes(b"one")
    attachment2.write_bytes(b"two")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        q2,
        "load_q2_inputs",
        lambda **kwargs: captured.update(kwargs)
        or Q2InputBundle(
            tuple(FixedPricePoint(slot=i, price_cny_per_kwh=1.0, source_ref="test") for i in range(144)),
            (),
            (),
        ),
    )
    monkeypatch.setattr(q2, "run_q2_engineering", lambda bundle, config: captured.update(config=config) or _fake_engineering_result())
    monkeypatch.setattr(q2, "validate_complete_run", lambda intervals, days: type("Report", (), {"ok": True, "issues": (), "checked_intervals": len(intervals), "max_abs_violation_kwh": 0.0})())
    output_dir = tmp_path / "run"
    run(CaseContext(repo_root=repo, case_id="q2", output_dir=output_dir, is_synthetic=True))
    assert captured["load_sheet_name"] == "小区负载"
    assert captured["pv_sheet_name"] == "光伏发电实际功率"
    assert captured["expected_days"][0] == dt.date(2025, 1, 1)
    assert captured["expected_days"][-1] == dt.date(2025, 12, 31)
    config = captured["config"]
    assert config.action_end_day == dt.date(2025, 12, 31)
    assert config.annual_endpoint_day == dt.date(2025, 12, 31)
    assert config.annual_terminal_soc_kwh == pytest.approx(6000.0)
    assert config.forecast_config.ar1_phi is None
    assert config.terminal_value_cny_per_kwh == pytest.approx(0.9)
    for name in ("forecast_records.json", "effective_config.json", "solver_records.json"):
        assert (output_dir / name).is_file()
    recorded = json.loads((output_dir / "effective_config.json").read_text(encoding="utf-8"))
    assert recorded["settlement"]["terminal_value_cny_per_kwh"] == pytest.approx(0.9)
    # The artifact has to record the forecast settings that actually ran, not a
    # subset of them: a run whose model cannot be read back off its own artifact
    # is not reproducible, and the recorded values must not drift from the
    # config handed to the engine.
    assert recorded["forecast"]["lags"] == list(config.forecast_config.lags)
    assert recorded["forecast"]["pv_lags"] == list(config.forecast_config.pv_lags or [])
    assert recorded["forecast"]["pv_weights"] == list(config.forecast_config.pv_weights or [])
    assert recorded["forecast"]["load_bias_window"] == config.forecast_config.load_bias_window
    assert recorded["forecast"]["load_bias_scale"] == config.forecast_config.load_bias_scale
    assert recorded["forecast"]["model_version"] == config.forecast_config.model_version
    assert recorded["settlement"]["purchase_margin"] == config.purchase_margin
    assert recorded["is_synthetic"] is True


def test_q2_provenance_verifies_the_paths_actually_loaded(tmp_path: Path, monkeypatch) -> None:
    import microgrid.problem.q2 as q2
    repo = tmp_path / "repo"
    _write_decisions(repo)
    custom1, custom2 = repo / "inputs" / "a1.xlsx", repo / "inputs" / "a2.xlsx"
    custom1.parent.mkdir(parents=True)
    custom1.write_bytes(b"one")
    custom2.write_bytes(b"two")
    seen: list[Path] = []
    monkeypatch.setattr(q2, "verify_imported_inputs", lambda root: [])
    monkeypatch.setattr(q2, "verify_loaded_input_paths", lambda root, paths: seen.extend(paths) or [])
    monkeypatch.setattr(q2, "load_q2_inputs", lambda **kwargs: Q2InputBundle((), (), ()))
    monkeypatch.setattr(q2, "run_q2_engineering", lambda bundle, config: _fake_engineering_result())
    monkeypatch.setattr(q2, "validate_complete_run", lambda intervals, days: type("Report", (), {"ok": True, "issues": (), "checked_intervals": len(intervals), "max_abs_violation_kwh": 0.0})())
    run(CaseContext(repo_root=repo, case_id="q2", output_dir=tmp_path / "run", metadata={"attachment1_path": custom1, "attachment2_path": custom2}))
    assert seen == [custom1, custom2]


def test_q2_record_writer_is_byte_identical_to_write_json(tmp_path: Path) -> None:
    """The streaming record writer must not change a single byte.

    ``forecast_records.json`` holds 6.9M records, so it is written one element
    at a time instead of via ``write_json``, which materialises the whole array
    and then encodes it -- that cost several GB and OOM-killed an annual run.
    Every artifact's sha256 is recorded in the manifest, so the streaming form
    has to reproduce ``json.dumps`` exactly, including the empty array, unicode,
    escapes and the JSON float edge cases.
    """
    import microgrid.problem.q2 as q2
    from microgrid.artifacts import write_json

    forecast_records = [
        {
            "valid_time": "2025-02-01 00:00:00",
            "available_at": "2025-01-31 00:00:00",
            "load_kw": 123.456,
            "pv_kw": 0.0,
            "training_cutoff": "2025-01-31 00:00:00",
            "model_version": "q2-weekly-ar1-v1",
            "data_version": "v1",
            "fallback_reason": None,
        },
        {
            "valid_time": "2025-02-01 00:10:00",
            "available_at": "2025-01-31 00:00:00",
            "load_kw": -0.0,
            "pv_kw": 5e-324,
            "training_cutoff": "2025-01-31 00:00:00",
            "model_version": "q2-weekly-ar1-v1",
            "data_version": "v1",
            "fallback_reason": "cold start",
        },
        {"unicode": "小区负载", "quote": 'a"b', "tab": "x\ty", "nested": {"k": [1, None, True]}},
    ]
    expected = write_json(tmp_path / "expected.json", forecast_records).read_bytes()
    assert q2._write_json_records(tmp_path / "actual.json", forecast_records).read_bytes() == expected
    # forecast_records is handed over as a generator, so iterators must work too.
    assert q2._write_json_records(tmp_path / "iter.json", iter(forecast_records)).read_bytes() == expected
    assert (
        q2._write_json_records(tmp_path / "empty.json", []).read_bytes()
        == write_json(tmp_path / "empty_expected.json", []).read_bytes()
        == b"[]\n"
    )
