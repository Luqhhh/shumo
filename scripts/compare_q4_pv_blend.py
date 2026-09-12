"""Single-factor evaluation of explicitly approved Q4-3/main PV-BLEND-LONG."""

from __future__ import annotations

import argparse
import datetime as dt
import math
from itertools import zip_longest
from pathlib import Path

from microgrid.artifacts import source_tree_hash
from microgrid.dataio import sha256_file
from microgrid.problem.q4_common import (
    ACTION_START,
    BLEND_MODEL_VERSION,
    MODEL_VERSION,
    STEP,
    YEAR_END,
    atomic_json,
)
from microgrid.problem.q4_diagnostics import Errors
from microgrid.problem.q4_evidence import check_inventory, check_model_binding, json_rows, read_json
from microgrid.problem.q4_inputs import load_q4_inputs
from microgrid.schemas import InputError


def checked_run(repo, run_id, version, end):
    run = repo / "outputs/runs/q4_3" / run_id
    issues = check_model_binding(repo, run) + check_inventory(repo, run)
    if issues:
        raise InputError(";".join(issues[:8]))
    manifest, config, summary, domain = (
        read_json(run / name)
        for name in ("manifest.json", "effective_config.json", "summary.json", "domain_result.json")
    )
    actual_end = dt.datetime.fromisoformat(config["end_time"])
    expected_status = "success" if actual_end == YEAR_END else "diagnostic_success"
    count = int((actual_end - ACTION_START) / STEP)
    if (
        config["model_version"] != version
        or config["price_method"] != "main"
        or actual_end < end
        or any(
            row.get("case_id") != "q4_3"
            or row.get("run_id") != run_id
            or row.get("status") != expected_status
            for row in (manifest, summary, domain)
        )
        or manifest["is_synthetic"]
        or domain["is_synthetic"]
        or len(domain["intervals"]) != count
        or summary["interval_count"] != count
        or summary["validation_ok"] is not True
        or (end == YEAR_END and summary["full_annual"] is not True)
        or source_tree_hash(run / "source_snapshot") != manifest["source_hash"]
    ):
        raise InputError("PV comparison requires complete feasible nonsynthetic main runs")
    inventory = read_json(run / "evidence_manifest.json")
    audit = inventory["controller_audit"]
    if (
        inventory["audit_kind"] != "runtime"
        or audit["plans_origin"] != "runtime_saved_plans"
        or audit["checked_intervals"] != count
        or audit["checked_fresh_plans"] + audit["checked_reused_tails"] != count
    ):
        raise InputError("PV comparison requires full runtime controller evidence")
    return run, config, summary, domain


def compare_forecasts(before, after, end, inputs):
    def rows(run):
        for row in json_rows(run / "forecasts.jsonl"):
            if dt.datetime.fromisoformat(row["issue_time"]) >= end:
                break
            yield row

    statistics = {label: {} for label in ("v3", "blend")}
    counts = {"forecasts": 0, "changed_pv_points": 0, "cold_pv_points": 0, "short_pv_points": 0}
    sentinel = object()
    for base, candidate in zip_longest(rows(before), rows(after), fillvalue=sentinel):
        if base is sentinel or candidate is sentinel:
            raise InputError("PV comparison forecast coverage differs")
        issue = dt.datetime.fromisoformat(base["issue_time"])
        if any(
            base[k] != candidate[k]
            for k in (
                "issue_time",
                "case_id",
                "slots",
                "load_kwh",
                "prices",
                "terminal_value",
                "price_method",
                "source_hashes",
            )
        ) or base["traces"] != {k: v for k, v in candidate["traces"].items() if k != "pv_blend"}:
            raise InputError("single-factor PV comparison changed another forecast or provenance")
        trace = candidate["traces"]["pv_blend"]
        if len(trace["points"]) != len(base["slots"]) or len(candidate["pv_kwh"]) != len(
            base["slots"]
        ):
            raise InputError("PV comparison point coverage differs")
        cold = issue < ACTION_START + dt.timedelta(days=7)
        for i, (original, new, point) in enumerate(
            zip(base["pv_kwh"], candidate["pv_kwh"], trace["points"], strict=True)
        ):
            if point["original_v3_kw"] / 6 != original or point["prediction_kw"] / 6 != new:
                raise InputError("PV blend original/output is not bound to its forecast point")
            if i < 72 or cold:
                if original != new:
                    raise InputError("PV blend modified an approved frozen short/cold point")
                counts["short_pv_points" if i < 72 else "cold_pv_points"] += 1
            counts["changed_pv_points"] += original != new
            slot = dt.datetime.fromisoformat(base["slots"][i])
            if slot >= end:
                continue
            actual = inputs.pv_kw[inputs.index(slot)]
            groups = [
                "24h",
                "6h" if i < 36 else None,
                "0-6h" if i < 36 else "6-12h" if i < 72 else "12-18h" if i < 108 else "18-24h",
            ]
            for label, value in (("v3", original), ("blend", new)):
                for group in groups:
                    if group is not None:
                        statistics[label].setdefault(group, Errors()).add(actual, value * 6)
        counts["forecasts"] += 1
    if counts["forecasts"] != int((end - ACTION_START) / dt.timedelta(hours=6)):
        raise InputError("PV comparison lacks the approved refresh grid")
    return {
        label: {k: v.summary() for k, v in groups.items()} for label, groups in statistics.items()
    }, counts


def period_metrics(run, domain, end):
    count = int((end - ACTION_START) / STEP)
    intervals = domain["intervals"][:count]
    executions = []
    for row in json_rows(run / "execution_feedback.jsonl"):
        if dt.datetime.fromisoformat(row["slot_start"]) >= end:
            break
        executions.append(row["execution"])
    costs = {name: [] for name in ("planned_cost_cny", "adjustment_cost_cny", "emergency_cost_cny")}
    for row in json_rows(run / "cost_ledger.jsonl"):
        if dt.datetime.fromisoformat(row["slot_start"]) >= end:
            break
        for name in costs:
            costs[name].append(row[name])
    if len(executions) != count or any(len(v) != count for v in costs.values()):
        raise InputError("PV comparison settlement/execution period is incomplete")
    result = {name: math.fsum(values) for name, values in costs.items()}
    result["total_cost_cny"] = math.fsum(result.values())
    result.update(
        interval_count=count,
        initial_energy_kwh=executions[0]["state_start"]["energy_kwh"],
        terminal_energy_kwh=executions[-1]["state_end"]["energy_kwh"],
        unused_contract_kwh=math.fsum(x["unused_grid_kwh"] for x in executions),
        pv_curtailed_kwh=math.fsum(x["curtailed_pv_kwh"] for x in executions),
        emergency_kwh=math.fsum(x["emergency_kwh"] for x in executions),
        battery_throughput_kwh=math.fsum(
            x["action"]["charge_kwh"] + x["action"]["discharge_kwh"] for x in executions
        ),
        actual_charge_kwh=math.fsum(x["action"]["charge_kwh"] for x in executions),
        actual_discharge_kwh=math.fsum(x["action"]["discharge_kwh"] for x in executions),
        protection_count=sum(bool(x["reasons"]) for x in executions),
    )
    if len(intervals) != count or result["initial_energy_kwh"] != 6000:
        raise InputError("PV comparison does not start at the approved initial state")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-repo", type=Path, required=True)
    parser.add_argument("--candidate-repo", type=Path, required=True)
    parser.add_argument("--baseline-run", default="q4-3-v3-engineering-annual-main-001")
    parser.add_argument("--candidate-run", required=True)
    parser.add_argument("--end-time", default=str(YEAR_END))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    end = dt.datetime.fromisoformat(args.end_time)
    if not ACTION_START < end <= YEAR_END or end.time() != dt.time():
        raise InputError("PV comparison end must be an approved exclusive midnight")
    if args.output.exists():
        raise InputError("PV comparison output exists; preserve earlier evidence")
    before, bcfg, bsummary, bdomain = checked_run(
        args.baseline_repo.resolve(), args.baseline_run, MODEL_VERSION, end
    )
    after, acfg, asummary, adomain = checked_run(
        args.candidate_repo.resolve(), args.candidate_run, BLEND_MODEL_VERSION, end
    )
    configs = [
        {k: v for k, v in c.items() if k not in ("model_version", "pv_blend", "end_time")}
        for c in (bcfg, acfg)
    ]
    if configs[0] != configs[1] or bsummary["source_hashes"] != asummary["source_hashes"]:
        raise InputError("PV comparison changed a non-PV configuration or actual input")
    inputs = load_q4_inputs(args.candidate_repo.resolve(), "q4_3")
    if inputs.source_hashes != asummary["source_hashes"]:
        raise InputError("PV comparison actual input differs from its saved snapshot")
    forecast_metrics, counts = compare_forecasts(before, after, end, inputs)
    results = {
        label: period_metrics(run, domain, end)
        for label, run, domain in (("v3", before, bdomain), ("blend", after, adomain))
    }
    deltas = {k: results["blend"][k] - results["v3"][k] for k in results["v3"]}
    if (
        abs(
            deltas["total_cost_cny"]
            - math.fsum(
                deltas[k] for k in ("planned_cost_cny", "adjustment_cost_cny", "emergency_cost_cny")
            )
        )
        > 0.01
    ):
        raise InputError("PV comparison cost delta does not reconcile")
    proofs = {
        label: {
            name: sha256_file(run / name)
            for name in (
                "manifest.json",
                "effective_config.json",
                "summary.json",
                "evidence_manifest.json",
                "forecasts.jsonl",
                "execution_feedback.jsonl",
                "cost_ledger.jsonl",
            )
        }
        for label, run in (("v3", before), ("blend", after))
    }
    report = {
        "ok": True,
        "end_time": str(end),
        "full_annual": end == YEAR_END,
        "scope": "one approved PV mechanism on inspected development data; causal replay, not untouched holdout",
        "baseline_run_id": args.baseline_run,
        "candidate_run_id": args.candidate_run,
        "mechanism": "PV-BLEND-LONG",
        "forecast_checks": counts,
        "pv_metrics_kw": forecast_metrics,
        "results": results,
        "delta_blend_minus_v3": deltas,
        "runtime_counts": {
            label: {k: s[k] for k in ("solve_count", "reused_tail_count")}
            for label, s in (("v3", bsummary), ("blend", asummary))
        },
        "performance": {
            label: read_json(run / "performance_summary.json")
            for label, run in (("v3", before), ("blend", after))
        },
        "cost_reduction_percent": -100 * deltas["total_cost_cny"] / results["v3"]["total_cost_cny"],
        "source_artifact_sha256": proofs,
        "comparison_tool_sha256": sha256_file(Path(__file__)),
        "run_selection_changed": False,
        "human_ai_review_changed": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(args.output, report)
    print(args.output)
    print(
        "delta total CNY", deltas["total_cost_cny"], "reduction %", report["cost_reduction_percent"]
    )


if __name__ == "__main__":
    main()
