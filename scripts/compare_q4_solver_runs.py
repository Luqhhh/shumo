"""Compare complete feasible Q4 solver diagnostics without assuming tied actions agree."""

from __future__ import annotations

import argparse
import datetime as dt
from itertools import zip_longest
from pathlib import Path

from compare_q4_pv_blend import period_metrics

from microgrid.artifacts import source_tree_hash
from microgrid.dataio import sha256_file
from microgrid.problem.q4_common import ACTION_START, MODEL_VERSION, STEP, YEAR_END, atomic_json
from microgrid.problem.q4_evidence import check_inventory, check_model_binding, json_rows, read_json
from microgrid.schemas import InputError


def checked(repo, case, run_id, end, version=MODEL_VERSION):
    run = repo / "outputs/runs" / case / run_id
    issues = check_inventory(repo, run) + check_model_binding(repo, run)
    if issues:
        raise InputError(";".join(issues[:8]))
    config, summary, domain, manifest = (
        read_json(run / name)
        for name in ["effective_config.json", "summary.json", "domain_result.json", "manifest.json"]
    )
    actual_end = dt.datetime.fromisoformat(config["end_time"])
    count = int((actual_end - ACTION_START) / STEP)
    status = "success" if actual_end == YEAR_END else "diagnostic_success"
    audit = read_json(run / "evidence_manifest.json")["controller_audit"]
    if (
        config["model_version"] != version
        or config["price_method"] != "main"
        or actual_end < end
        or summary["status"] != status
        or domain["status"] != status
        or manifest["status"] != status
        or manifest["is_synthetic"]
        or domain["is_synthetic"]
        or summary["validation_ok"] is not True
        or len(domain["intervals"]) != count
        or audit["checked_intervals"] != count
        or audit["plans_origin"] != "runtime_saved_plans"
        or source_tree_hash(run / "source_snapshot") != manifest["source_hash"]
    ):
        raise InputError(
            "solver comparison requires full feasible nonsynthetic evidence and source binding"
        )
    return run, config, summary, domain


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-repo", type=Path, required=True)
    parser.add_argument("--candidate-repo", type=Path, required=True)
    parser.add_argument("--case", choices=["q4_2", "q4_3"], required=True)
    parser.add_argument("--candidate-run", required=True)
    parser.add_argument("--method", required=True)
    parser.add_argument("--end-time", default="2025-02-15")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    end = dt.datetime.fromisoformat(args.end_time)
    if args.output.exists() or not ACTION_START < end <= YEAR_END or end.time() != dt.time():
        raise InputError("preserve prior evidence; comparison requires exclusive midnight")
    baseline_id = args.case.replace("_", "-") + "-v3-engineering-annual-main-001"
    before, bcfg, bs, bd = checked(args.baseline_repo, args.case, baseline_id, end)
    after, acfg, cs, cd = checked(args.candidate_repo, args.case, args.candidate_run, end)
    if bcfg.get("solver_method", "scipy") != "scipy" or acfg.get("solver_method") != args.method:
        raise InputError("solver method identity differs")
    ignored = ["end_time", "solver_method", "solver_method_parameters"]
    if {k: v for k, v in bcfg.items() if k not in ignored} != {
        k: v for k, v in acfg.items() if k not in ignored
    } or bs["source_hashes"] != cs["source_hashes"]:
        raise InputError("single solver factor changed other model configuration/input")

    def forecast_rows(run):
        for row in json_rows(run / "forecasts.jsonl"):
            if dt.datetime.fromisoformat(row["issue_time"]) >= end:
                break
            yield row

    sentinel = object()
    forecasts = 0
    for a, b in zip_longest(forecast_rows(before), forecast_rows(after), fillvalue=sentinel):
        if a is sentinel or b is sentinel or a != b:
            raise InputError("solver experiment changed a forecast or provenance")
        forecasts += 1
    if forecasts != int((end - ACTION_START) / dt.timedelta(hours=6)):
        raise InputError("forecast coverage differs")
    a, b = period_metrics(before, bd, end), period_metrics(after, cd, end)
    count = a["interval_count"]
    changes = sum(
        x != y for x, y in zip(bd["intervals"][:count], cd["intervals"][:count], strict=True)
    )
    atomic_json(
        args.output,
        {
            "ok": True,
            "case_id": args.case,
            "method": args.method,
            "full_annual": end == YEAR_END,
            "end_time": str(end),
            "scope": "complete causal closed-loop diagnostic on inspected development data; prefixes are not annual results",
            "baseline_run_id": baseline_id,
            "candidate_run_id": args.candidate_run,
            "forecasts_exact": True,
            "forecasts_checked": forecasts,
            "changed_domain_intervals": changes,
            "results": {"baseline": a, "candidate": b},
            "delta_candidate_minus_baseline": {k: b[k] - a[k] for k in a},
            "candidate_performance": read_json(after / "performance_summary.json"),
            "source_proofs": {
                str(run / name): sha256_file(run / name)
                for run in (before, after)
                for name in [
                    "manifest.json",
                    "effective_config.json",
                    "summary.json",
                    "evidence_manifest.json",
                ]
            },
            "tool_sha256": sha256_file(Path(__file__)),
        },
    )
    print("domain changes", changes, "cost delta", b["total_cost_cny"] - a["total_cost_cny"])


if __name__ == "__main__":
    main()
