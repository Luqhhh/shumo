from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from microgrid.dataio import sha256_file
from microgrid.problem.contracts import CaseContext, CaseResult
from microgrid.problem.q3 import REQUIRED_DECISIONS, run
from microgrid.problem.q3_artifacts import Q3DayArtifacts
from microgrid.problem.q3_sidecars import Q3SidecarArtifact, Q3SidecarSet


def _approve_q3(repo: Path) -> None:
    config = repo / "configs" / "decisions.toml"
    config.parent.mkdir(parents=True)
    lines: list[str] = []
    for decision_id in REQUIRED_DECISIONS:
        lines.extend(
            (
                f"[decisions.{decision_id}]",
                'status = "approved"',
                'choice = "test"',
                'rationale = "test"',
                'confirmed_by = "tester"',
                'confirmed_at = "2026-09-13"',
                'source = "test"',
                "",
            )
        )
    config.write_text("\n".join(lines), encoding="utf-8")
    manifest = repo / "records" / "inputs_manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"items": []}\n', encoding="utf-8")


def _fake_artifacts(run_dir: Path, run_id: str) -> Q3DayArtifacts:
    domain = run_dir / "domain_result.json"
    domain.write_text('{"synthetic": true}\n', encoding="utf-8")
    sidecar_artifacts: list[Q3SidecarArtifact] = []
    for name in ("forecast_provenance", "plan_versions", "settlement_ledger"):
        path = run_dir / f"{name}.jsonl"
        path.write_text('{"synthetic": true}\n', encoding="utf-8")
        sidecar_artifacts.append(
            Q3SidecarArtifact(
                name=name,
                path=path,
                schema_version=2,
                sha256=sha256_file(path),
                row_count=1,
            )
        )
    result = CaseResult(
        case_id="q3",
        run_id=run_id,
        status="success",
        is_synthetic=True,
        metadata={
            "state_start_kwh": 6_000.0,
            "state_end_kwh": 6_000.0,
            "planned_cost_cny": 1.0,
            "adjustment_cost_cny": 2.0,
            "emergency_cost_cny": 3.0,
            "total_cost_cny": 6.0,
        },
    )
    return Q3DayArtifacts(
        result=result,
        domain_result_path=domain,
        sidecars=Q3SidecarSet(
            forecast_provenance=sidecar_artifacts[0],
            plan_versions=sidecar_artifacts[1],
            settlement_ledger=sidecar_artifacts[2],
        ),
    )


def test_q3_runner_success_registers_all_internal_artifacts(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _approve_q3(repo)
    runtime = SimpleNamespace(
        raw_info_items=(),
        input_hashes=(("attachment2", "attachment2-sha"),),
        fixed_prices=(),
        actuals_for=lambda _days: (),
    )

    class FakeFactory:
        def __init__(self, **_kwargs) -> None:
            self.used_load_snapshots = ()
            self.used_pv_snapshots = ()
            self.used_tail_forecasts = ()

    fake_period = SimpleNamespace(state_end=SimpleNamespace(energy_kwh=6_000.0))
    monkeypatch.setattr("microgrid.problem.q3.verify_imported_inputs", lambda _repo: [])
    monkeypatch.setattr("microgrid.problem.q3.verify_required_inputs", lambda _repo, _paths: [])
    monkeypatch.setattr("microgrid.problem.q3.load_q3_runtime_inputs", lambda **_kwargs: runtime)
    monkeypatch.setattr("microgrid.problem.q3.build_q3_snapshot_catalog", lambda **_kwargs: ())
    monkeypatch.setattr("microgrid.problem.q3.Q3SnapshotWindowFactory", FakeFactory)
    monkeypatch.setattr("microgrid.problem.q3.run_q3_period", lambda **_kwargs: fake_period)

    def fake_write(run_dir, **kwargs):
        return _fake_artifacts(Path(run_dir), kwargs["run_id"])

    monkeypatch.setattr("microgrid.problem.q3.write_q3_period_artifacts", fake_write)
    result = run(
        CaseContext(
            repo_root=repo,
            case_id="q3",
            run_id="synthetic-runner-test",
            is_synthetic=True,
        )
    )

    assert result.status == "success"
    run_dir = repo / "outputs" / "runs" / "q3" / "synthetic-runner-test"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "success"
    assert manifest["model_status"] == "implemented"
    assert manifest["is_synthetic"] is True
    assert set(manifest["result_files"]) == {
        "domain_result.json",
        "forecast_provenance.jsonl",
        "plan_versions.jsonl",
        "settlement_ledger.jsonl",
    }
    assert set(manifest["result_sha256"]) == set(manifest["result_files"])
    assert all(len(digest) == 64 for digest in manifest["result_sha256"].values())
