#!/usr/bin/env python3
"""Revalidate an existing Q4 run without changing its original artifacts."""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from microgrid.artifacts import source_tree_hash  # noqa: E402
from microgrid.dataio import sha256_file, utc_now  # noqa: E402
from microgrid.problem.q4_common import atomic_json, reserve_start_from_config  # noqa: E402
from microgrid.problem.q4_evidence import (  # noqa: E402
    audit_controller_chain,
    check_model_binding,
    read_json,
    supplementary_dir,
    write_inventory,
)
from microgrid.problem.q4_export import load_replay_evidence  # noqa: E402
from microgrid.problem.q4_inputs import load_q4_inputs  # noqa: E402
from microgrid.problem.q4_validation import validate_q4_run  # noqa: E402
from microgrid.problem.result_io import load_case_result  # noqa: E402
from microgrid.schemas import InputError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--case", choices=("q4_2", "q4_3"), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--verified-plans",
        type=Path,
        help="recheck separately reconstructed plans instead of solving again",
    )
    args = parser.parse_args()
    repo = args.repo.resolve()
    run = repo / "outputs" / "runs" / args.case / args.run_id
    target = supplementary_dir(repo, args.case, args.run_id)
    if target.exists():
        raise InputError("audit output already exists; preserve previous evidence")
    target.mkdir(parents=True)
    original_hashes = {
        str(path.relative_to(run)): sha256_file(path)
        for path in run.rglob("*")
        if path.is_file() and "source_snapshot" not in path.parts
    }
    report = {
        "schema_version": 1,
        "case_id": args.case,
        "run_id": args.run_id,
        "audit_started_at": utc_now(),
        "verifier_source_hash": source_tree_hash(repo),
        "original_artifact_hashes": original_hashes,
        "ok": False,
    }
    try:
        config = read_json(run / "effective_config.json")
        export = (
            read_json(run / "export_manifest.json")
            if (run / "export_manifest.json").is_file()
            else None
        )
        issues = check_model_binding(repo, run, export)
        if issues:
            raise InputError(";".join(issues))
        inputs = load_q4_inputs(repo, args.case)
        if inputs.source_hashes != read_json(run / "input_snapshot.json")["source_hashes"]:
            raise InputError("current inputs differ from original input hashes")
        result = load_case_result(run / "domain_result.json")
        ledger, executions = load_replay_evidence(run, args.case)
        validation = validate_q4_run(
            result.intervals,
            executions,
            ledger,
            end_time=dt.datetime.fromisoformat(config["end_time"]),
            actual_inputs=inputs,
            reserve_start=reserve_start_from_config(config),
            timely_control=config["timely_control"],
        )
        report["independent_validation"] = validation
        atomic_json(target / "validation.json", validation)
        print(
            f"{args.case}/{args.run_id}: strict input/feedback/physical/cost validation ok={validation['ok']}",
            flush=True,
        )
        if not validation["ok"]:
            raise InputError(";".join(validation["violations"][:8]))
        plans_path = target / "dispatch_plans.jsonl"
        if args.verified_plans is not None:
            shutil.copyfile(args.verified_plans, plans_path)
        audit = audit_controller_chain(
            run, inputs, plans_path=plans_path, reconstruct=args.verified_plans is None
        )
        if args.verified_plans is not None:
            audit["plans_origin"] = "post_run_solver_reconstruction_rechecked"
            report["reconstructed_plans_source_sha256"] = sha256_file(args.verified_plans)
        report["controller_audit"] = audit
        report["ok"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(report["error"], flush=True)
    finally:
        current_hashes = {
            str(path.relative_to(run)): sha256_file(path)
            for path in run.rglob("*")
            if path.is_file() and "source_snapshot" not in path.parts
        }
        report["original_artifacts_unchanged"] = current_hashes == original_hashes
        report["verifier_source_unchanged"] = (
            source_tree_hash(repo) == report["verifier_source_hash"]
        )
        report["ok"] = (
            report["ok"]
            and report["original_artifacts_unchanged"]
            and report["verifier_source_unchanged"]
        )
        atomic_json(target / "review_validation.json", report)
    if report["ok"]:
        write_inventory(
            run,
            target / "evidence_manifest.json",
            report["controller_audit"],
            plans_path=target / "dispatch_plans.jsonl",
        )
    print(
        f"{args.case}/{args.run_id}: review ok={report['ok']}; original artifacts unchanged={report['original_artifacts_unchanged']}",
        flush=True,
    )
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
