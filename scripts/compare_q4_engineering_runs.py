"""Compare frozen engineering runs, retaining all actions and independent evidence."""

from __future__ import annotations

import argparse
from pathlib import Path

from microgrid.artifacts import source_tree_hash
from microgrid.dataio import sha256_file
from microgrid.problem.q4_common import atomic_json
from microgrid.problem.q4_evidence import check_inventory, json_rows, read_json
from microgrid.schemas import InputError


def deterministic(value):
    if isinstance(value, dict):
        return {
            key: deterministic(item)
            for key, item in value.items()
            if key not in ("elapsed_seconds", "forecast_sha256", "forecast_view")
        }
    if isinstance(value, list):
        return [deterministic(item) for item in value]
    return value


def compare(before_repo, after_repo, case):
    suffix = "-week-001"
    before = (
        before_repo / "outputs/runs" / case / (case.replace("_", "-") + "-v3-perf-before" + suffix)
    )
    after = (
        after_repo / "outputs/runs" / case / (case.replace("_", "-") + "-v3-perf-after" + suffix)
    )
    for repo, run in ((before_repo, before), (after_repo, after)):
        if check_inventory(repo, run):
            raise InputError("engineering comparison evidence inventory mismatch")
        if source_tree_hash(repo) != read_json(run / "manifest.json")["source_hash"]:
            raise InputError("frozen benchmark source changed")
    configs = [read_json(run / "effective_config.json") for run in (before, after)]
    for config in configs:
        config.pop("evidence_schema_version", None)
    if configs[0] != configs[1] or read_json(before / "input_snapshot.json") != read_json(
        after / "input_snapshot.json"
    ):
        raise InputError("engineering comparison model or input mismatch")
    checks = {}
    for name in (
        "forecasts",
        "contracts",
        "execution_feedback",
        "cost_ledger",
        "solver_records",
        "dispatch_plans",
    ):
        sentinel = object()
        from itertools import zip_longest

        rows = zip_longest(
            json_rows(before / f"{name}.jsonl"),
            json_rows(after / f"{name}.jsonl"),
            fillvalue=sentinel,
        )
        count = 0
        for a, b in rows:
            if (
                a is sentinel
                or b is sentinel
                or (
                    deterministic(a) != deterministic(b)
                    if name in ("solver_records", "dispatch_plans")
                    else a != b
                )
            ):
                raise InputError(f"{case}: {name} differs at record {count}")
            count += 1
        checks[name] = count
    if (
        before.joinpath("daily_summary.csv").read_bytes()
        != after.joinpath("daily_summary.csv").read_bytes()
    ):
        raise InputError("daily summary bytes differ")
    a, b = (read_json(run / "summary.json") for run in (before, after))
    if a["costs"] != b["costs"] or a["prediction_metrics"] != b["prediction_metrics"]:
        raise InputError("costs or prediction metrics differ")
    timings = [read_json(run / "performance_summary.json") for run in (before, after)]
    if timings[0]["environment"] != timings[1]["environment"]:
        raise InputError("benchmark machine, dependencies or thread environment differ")
    return {
        "case_id": case,
        "interval_count": a["interval_count"],
        "total_cost_cny": a["total_cost_cny"],
        "exact_trajectory_match": True,
        "daily_summary_bytes_match": True,
        "deterministic_records_checked": checks,
        "before": timings[0],
        "after": timings[1],
        "end_to_end_reduction_fraction": 1
        - timings[1]["end_to_end_seconds"] / timings[0]["end_to_end_seconds"],
        "timing_artifact_sha256": {
            str(run / "performance_summary.json"): sha256_file(run / "performance_summary.json")
            for run in (before, after)
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-repo", type=Path, required=True)
    parser.add_argument("--after-repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise InputError("comparison output already exists")
    results = [
        compare(args.before_repo.resolve(), args.after_repo.resolve(), case)
        for case in ("q4_2", "q4_3")
    ]
    atomic_json(
        args.output,
        {
            "schema_version": 1,
            "ok": True,
            "scope": "one paired real-data week benchmark per case; excludes Excel; no annual speedup claim",
            "runs": results,
        },
    )
    for result in results:
        print(
            result["case_id"],
            "exact trajectory match",
            "before/after seconds",
            result["before"]["end_to_end_seconds"],
            result["after"]["end_to_end_seconds"],
        )


if __name__ == "__main__":
    main()
