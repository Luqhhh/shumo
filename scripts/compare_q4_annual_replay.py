"""Compare explicit approved annual Q4 runs, retaining every action and bill."""

from __future__ import annotations

import argparse
from itertools import zip_longest
from pathlib import Path

from microgrid.artifacts import source_tree_hash
from microgrid.dataio import sha256_file
from microgrid.problem.q4_common import MODEL_VERSION, YEAR_END, atomic_json
from microgrid.problem.q4_evidence import check_inventory, check_model_binding, json_rows, read_json
from microgrid.schemas import InputError

ANNUAL_STEPS = 48096
SPECS = (
    ("q4_2", "main", "main2"),
    ("q4_2", "lag1", "lag2"),
    ("q4_3", "main", "main3"),
    ("q4_3", "lag1", "lag3"),
)


def without_timing(value):
    if isinstance(value, dict):
        return {
            key: without_timing(item) for key, item in value.items() if key != "elapsed_seconds"
        }
    if isinstance(value, list):
        return [without_timing(item) for item in value]
    return value


def compare_records(before: Path, after: Path, name: str, expected_count: int):
    """Ignore solver representation changes only after binding its new intent."""
    sentinel = object()
    count = 0
    intents = (
        iter(json_rows(after / "execution_feedback.jsonl")) if name == "solver_records" else None
    )
    for old, new in zip_longest(
        json_rows(before / f"{name}.jsonl"), json_rows(after / f"{name}.jsonl"), fillvalue=sentinel
    ):
        if old is sentinel or new is sentinel:
            raise InputError(f"annual replay {name} coverage differs at record {count}")
        if name == "solver_records":
            execution = next(intents, None)
            if execution is None or new.get("intent") != execution["execution"]["intent"]:
                raise InputError(f"annual replay solver intent differs at record {count}")
            old, new = dict(old), dict(new)
            for row in (old, new):
                for key in ("intent", "forecast_view", "forecast_sha256"):
                    row.pop(key, None)
            old, new = without_timing(old), without_timing(new)
        if old != new:
            raise InputError(f"annual replay {name} differs at record {count}")
        count += 1
    if count != expected_count or intents is not None and next(intents, sentinel) is not sentinel:
        raise InputError(f"annual replay {name} record count mismatch")
    return count


def checked_run(repo: Path, case: str, method: str, run_id: str):
    run = repo / "outputs/runs" / case / run_id
    issues = check_model_binding(repo, run) + check_inventory(repo, run)
    if issues:
        raise InputError(";".join(issues[:8]))
    manifest, config, summary, result = (
        read_json(run / name)
        for name in ("manifest.json", "effective_config.json", "summary.json", "domain_result.json")
    )
    expected_status = "success" if method == "main" else "diagnostic_success"
    if any(
        record.get("case_id") != case
        or record.get("run_id") != run_id
        or record.get("status") != expected_status
        for record in (manifest, summary, result)
    ) or (
        result["is_synthetic"]
        or manifest["is_synthetic"]
        or config["is_synthetic"]
        or config["model_version"] != MODEL_VERSION
        or config["end_time"] != str(YEAR_END)
        or config["timely_control"] is not True
        or config["case_id"] != case
        or config["price_method"] != method
        or summary["price_method"] != method
        or not summary["full_annual"]
        or summary["interval_count"] != ANNUAL_STEPS
        or len(result["intervals"]) != ANNUAL_STEPS
        or not summary["validation_ok"]
        or abs(summary["terminal_energy_kwh"] - 6000) > 1e-6
    ):
        raise InputError(f"annual replay identity/status/coverage mismatch: {run_id}")
    if source_tree_hash(run / "source_snapshot") != manifest["source_hash"]:
        raise InputError(f"annual replay source snapshot mismatch: {run_id}")
    if read_json(run / "input_snapshot.json")["source_hashes"] != summary["source_hashes"]:
        raise InputError(f"annual replay input snapshot mismatch: {run_id}")
    return run, manifest, config, summary, result


def compare_pair(before_repo, after_repo, case, method, before_id, after_id):
    old_run, old_manifest, old_config, old_summary, old_result = checked_run(
        before_repo, case, method, before_id
    )
    new_run, new_manifest, new_config, new_summary, new_result = checked_run(
        after_repo, case, method, after_id
    )
    for config in (old_config, new_config):
        config.pop("evidence_schema_version", None)
    if old_config != new_config:
        raise InputError(f"annual replay approved configuration differs: {case}/{method}")
    ignored = {"run_id", "elapsed_seconds", "elapsed_seconds_scope"}
    if {k: v for k, v in old_summary.items() if k not in ignored} != {
        k: v for k, v in new_summary.items() if k not in ignored
    }:
        raise InputError(f"annual replay costs, states or metrics differ: {case}/{method}")
    if old_result["intervals"] != new_result["intervals"]:
        raise InputError(f"annual replay domain actions differ: {case}/{method}")
    counts = {
        name: compare_records(old_run, new_run, name, count)
        for name, count in (
            ("forecasts", 1336),
            ("contracts", 334 if case == "q4_2" else 1336),
            ("execution_feedback", ANNUAL_STEPS),
            ("cost_ledger", ANNUAL_STEPS),
            ("solver_records", ANNUAL_STEPS),
        )
    }
    if (old_run / "daily_summary.csv").read_bytes() != (new_run / "daily_summary.csv").read_bytes():
        raise InputError(f"annual replay daily CSV differs: {case}/{method}")
    inventory = read_json(new_run / "evidence_manifest.json")
    audit = inventory["controller_audit"]
    if (
        inventory["audit_kind"] != "runtime"
        or audit["plans_origin"] != "runtime_saved_plans"
        or not audit["ok"]
        or (
            audit["checked_intervals"] != ANNUAL_STEPS
            or audit["checked_forecasts"] != 1336
            or audit["checked_fresh_plans"] != new_summary["solve_count"]
            or audit["checked_reused_tails"] != new_summary["reused_tail_count"]
        )
    ):
        raise InputError("annual replay lacks complete runtime controller audit")
    checkpoint = read_json(new_run / "checkpoint.json")
    if (
        checkpoint["schema_version"] != 3
        or "ledger" in checkpoint
        or checkpoint["ledger_state"]["bill_count"] != ANNUAL_STEPS
    ):
        raise InputError("annual replay checkpoint is not a complete compact checkpoint")
    return {
        "case_id": case,
        "price_method": method,
        "before_run_id": before_id,
        "after_run_id": after_id,
        "old_source_hash": old_manifest["source_hash"],
        "new_source_hash": new_manifest["source_hash"],
        "interval_count": ANNUAL_STEPS,
        "exact_trajectory_match": True,
        "daily_csv_bytes_match": True,
        "records_checked": counts,
        "total_cost_cny": new_summary["total_cost_cny"],
        "terminal_energy_kwh": new_summary["terminal_energy_kwh"],
        "controller_audit": audit,
        "performance": read_json(new_run / "performance_summary.json"),
        "checkpoint_size_bytes": (new_run / "checkpoint.json").stat().st_size,
        "source_artifact_sha256": {
            label: {
                name: sha256_file(run / name)
                for name in (
                    "manifest.json",
                    "domain_result.json",
                    "summary.json",
                    "daily_summary.csv",
                )
            }
            for label, run in (("before", old_run), ("after", new_run))
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-repo", type=Path, default=Path.cwd())
    parser.add_argument("--after-repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    for case, method, key in SPECS:
        parser.add_argument(
            "--before-" + key, default=case.replace("_", "-") + "-v3-annual-" + method + "-001"
        )
        parser.add_argument(
            "--after-" + key,
            default=case.replace("_", "-") + "-v3-engineering-annual-" + method + "-001",
        )
    args = parser.parse_args()
    if args.output.exists():
        raise InputError("annual replay comparison output exists; preserve old evidence")
    results = [
        compare_pair(
            args.before_repo.resolve(),
            args.after_repo.resolve(),
            case,
            method,
            getattr(args, "before_" + key),
            getattr(args, "after_" + key),
        )
        for case, method, key in SPECS
    ]
    atomic_json(
        args.output,
        {
            "schema_version": 1,
            "ok": True,
            "scope": "approved v3 engineering equivalence; not an economic strategy improvement or untouched holdout",
            "ignored_solver_fields": "elapsed_seconds; new intent/view representation bound to identical execution and runtime controller audit",
            "historical_timing_comparable": False,
            "comparison_tool_sha256": sha256_file(Path(__file__)),
            "runs": results,
        },
    )
    print(args.output)


if __name__ == "__main__":
    main()
