from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from microgrid.artifacts import (
    build_manifest,
    config_snapshot,
    ensure_run_id_available,
    environment_snapshot,
    source_tree_hash,
    verify_imported_inputs,
    verify_required_inputs,
    write_manifest,
)
from microgrid.schemas import InputError
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
    assert manifest["model_status"] == "unknown"
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


def test_source_hash_covers_recursive_problem_and_lock_files(tmp_path):
    repo = _minimal_repo(tmp_path)
    original = source_tree_hash(repo)
    problem = repo / "src" / "microgrid" / "problem"
    problem.mkdir(parents=True)
    (problem / "q1.py").write_text("def run():\n    return None\n", encoding="utf-8")
    assert source_tree_hash(repo) != original

    with_problem = source_tree_hash(repo)
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    assert source_tree_hash(repo) != with_problem

    with_lock = source_tree_hash(repo)
    (repo / "outputs").mkdir()
    (repo / "outputs" / "run.json").write_text("{}", encoding="utf-8")
    assert source_tree_hash(repo) == with_lock


def test_verify_imported_inputs_reports_missing_or_changed(tmp_path):
    repo = _minimal_repo(tmp_path)
    assert verify_imported_inputs(repo) == ["inputs_manifest.json is missing"]

    payload = b"original"
    data_file = repo / "data" / "raw" / "x.bin"
    data_file.parent.mkdir(parents=True)
    data_file.write_bytes(payload)
    (repo / "records").mkdir()
    (repo / "records" / "inputs_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "items": [
                    {
                        "repository_path": "data/raw/x.bin",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert verify_imported_inputs(repo) == []
    data_file.write_bytes(b"changed")
    issues = verify_imported_inputs(repo)
    assert issues and "hash changed" in issues[0]


def test_verify_required_inputs_requires_exactly_one_hash_bound_entry(tmp_path):
    repo = _minimal_repo(tmp_path)
    required = "data/raw/附件1.xlsx"
    data_file = repo / required
    data_file.parent.mkdir(parents=True)
    data_file.write_bytes(b"official-like bytes")
    manifest = repo / "records" / "inputs_manifest.json"
    manifest.parent.mkdir()
    digest = hashlib.sha256(data_file.read_bytes()).hexdigest()

    manifest.write_text(json.dumps({"items": []}), encoding="utf-8")
    assert verify_required_inputs(repo, (required,))

    manifest.write_text(json.dumps({"items": [{"repository_path": required}]}), encoding="utf-8")
    assert verify_required_inputs(repo, (required,))

    duplicate = {
        "items": [
            {"repository_path": required, "sha256": digest},
            {"repository_path": required, "sha256": digest},
        ]
    }
    manifest.write_text(json.dumps(duplicate), encoding="utf-8")
    assert verify_required_inputs(repo, (required,))

    valid = {"items": [{"repository_path": required, "sha256": digest}]}
    manifest.write_text(json.dumps(valid), encoding="utf-8")
    assert verify_required_inputs(repo, (required,)) == []

    data_file.write_bytes(b"changed")
    assert verify_required_inputs(repo, (required,))


def test_run_id_allocation_and_manifest_overwrite_guard(tmp_path):
    repo = _minimal_repo(tmp_path)
    run_dir = ensure_run_id_available(repo, "q1", "run-1")
    assert not run_dir.exists()
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(InputError):
        ensure_run_id_available(repo, "q1", "run-1")
    manifest = build_manifest(
        repo,
        run_id="run-1",
        case_id="q1",
        command=["python", "-m", "microgrid", "run", "--case", "q1"],
        status="success",
        is_synthetic=False,
    )
    with pytest.raises(InputError):
        write_manifest(run_dir, manifest)
    assert write_manifest(run_dir, manifest, overwrite=True).exists()


def test_manifest_can_record_input_verification_issues(tmp_path):
    repo = _minimal_repo(tmp_path)
    manifest = build_manifest(
        repo,
        run_id="run-verify",
        case_id="q1",
        command=["x"],
        status="success",
        is_synthetic=False,
        verify_inputs=True,
    )
    assert manifest["input_verification_issues"] == ["inputs_manifest.json is missing"]
