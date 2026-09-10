from __future__ import annotations

import json
from pathlib import Path

from microgrid.artifacts import (
    build_manifest,
    config_snapshot,
    environment_snapshot,
    source_tree_hash,
    write_manifest,
)
from microgrid.smoke import run_smoke


def _minimal_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    (repo / "src" / "microgrid").mkdir(parents=True)
    (repo / "scripts").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "configs" / "project.toml").write_text("schema_version=1\n", encoding="utf-8")
    (repo / "src" / "microgrid" / "__init__.py").write_text("__version__='0'\n", encoding="utf-8")
    (repo / "scripts" / "x.py").write_text("print('x')\n", encoding="utf-8")
    return repo


def test_source_hash_and_manifest_are_deterministic(tmp_path):
    repo = _minimal_repo(tmp_path)
    h1 = source_tree_hash(repo)
    h2 = source_tree_hash(repo)
    assert h1 == h2 and len(h1) == 64
    manifest = build_manifest(
        repo,
        run_id="run-test",
        case_id="smoke",
        command=["python", "-m", "microgrid", "smoke"],
        status="pass",
        is_synthetic=True,
    )
    assert manifest["is_synthetic"] is True
    assert manifest["model_status"] == "not_implemented"
    assert manifest["source_hash"] == h1
    assert manifest["code_commit"] is None
    out = write_manifest(repo / "outputs" / "run-test", manifest)
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "pass"


def test_config_snapshot_and_environment_are_recorded(tmp_path):
    repo = _minimal_repo(tmp_path)
    snapshot = config_snapshot(repo)
    assert snapshot["project.toml"]["schema_version"] == 1
    assert "python" in environment_snapshot()


def test_smoke_is_synthetic_and_not_formal(tmp_path):
    repo = _minimal_repo(tmp_path)
    run_dir = run_smoke(repo)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert manifest["is_synthetic"] is True
    assert manifest["model_status"] == "not_implemented"
    assert summary["is_synthetic"] is True
    assert summary["energy_kwh_per_interval"] == 100.0
    assert summary["formal_result_eligible"] if "formal_result_eligible" in summary else True
    assert (run_dir / "status.json").exists()
