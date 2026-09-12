"""Single-factor Q4-3 planning evaluation, retaining complete runtime evidence."""

from __future__ import annotations

import argparse
import datetime as dt
from itertools import zip_longest
from pathlib import Path

import numpy as np
from compare_q4_pv_blend import period_metrics
from compare_q4_solver_runs import checked

from microgrid.dataio import sha256_file
from microgrid.problem.q4_common import (
    ACTION_START,
    SAFETY_MODEL_VERSION,
    STEP,
    TERMINAL_MODEL_VERSION,
    YEAR_END,
    atomic_json,
)
from microgrid.problem.q4_evidence import json_rows, read_json
from microgrid.schemas import InputError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-repo", type=Path, required=True)
    parser.add_argument("--candidate-repo", type=Path, required=True)
    parser.add_argument("--candidate-run", required=True)
    parser.add_argument("--end-time", default=str(YEAR_END))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", choices=("q4_2", "q4_3"), default="q4_3")
    parser.add_argument("--mechanism", choices=("terminal", "safety"), default="terminal")
    args = parser.parse_args()
    version = SAFETY_MODEL_VERSION if args.mechanism == "safety" else TERMINAL_MODEL_VERSION
    trace_key = "safety_procurement" if args.mechanism == "safety" else "terminal_value_rule"
    end = dt.datetime.fromisoformat(args.end_time)
    if args.output.exists() or not ACTION_START < end <= YEAR_END or end.time() != dt.time():
        raise InputError("preserve prior evidence; end requires exclusive midnight")
    baseline = args.case.replace("_", "-") + "-v3-engineering-annual-main-001"
    before, bcfg, bs, bd = checked(args.baseline_repo, args.case, baseline, end)
    after, acfg, cs, cd = checked(args.candidate_repo, args.case, args.candidate_run, end, version)
    ignored = ("model_version", "optimization", "end_time")
    if {k: v for k, v in bcfg.items() if k not in ignored} != {
        k: v for k, v in acfg.items() if k not in ignored
    } or bs["source_hashes"] != cs["source_hashes"]:
        raise InputError("planning comparison changed another config/input")

    def rows(run):
        for row in json_rows(run / "forecasts.jsonl"):
            if dt.datetime.fromisoformat(row["issue_time"]) >= end:
                break
            yield row

    sentinel = object()
    forecasts = changed = 0
    for a, b in zip_longest(rows(before), rows(after), fillvalue=sentinel):
        if a is sentinel or b is sentinel:
            raise InputError("planning forecast coverage differs")
        for key in [
            "issue_time",
            "case_id",
            "slots",
            "load_kwh",
            "pv_kwh",
            "prices",
            "price_method",
            "source_hashes",
        ]:
            if a[key] != b[key]:
                raise InputError("planning comparison changed a raw forecast")
        if a["traces"] != {k: v for k, v in b["traces"].items() if k != trace_key}:
            raise InputError("planning comparison changed another provenance")
        if args.mechanism == "safety":
            if a["terminal_value"] != b["terminal_value"]:
                raise InputError("safety procurement changed ordinary terminal value")
            changed += any(point["margin_kwh"] > 0 for point in b["traces"][trace_key]["points"])
            forecasts += 1
            continue
        expected = (
            0.9 * float(np.quantile(a["prices"], 0.75, method="linear"))
            if len(a["slots"]) == 144
            and dt.datetime.fromisoformat(a["slots"][-1]) + STEP < YEAR_END
            else 0.0
        )
        if (
            b["terminal_value"] != expected
            or b["traces"]["terminal_value_rule"]["value"] != expected
        ):
            raise InputError("terminal rule not independently reconstructed")
        changed += a["terminal_value"] != b["terminal_value"]
        forecasts += 1
    if forecasts != int((end - ACTION_START) / dt.timedelta(hours=6)):
        raise InputError("forecast grid incomplete")
    a, b = period_metrics(before, bd, end), period_metrics(after, cd, end)
    atomic_json(
        args.output,
        {
            "ok": True,
            "mechanism": "SAFETY-PROCUREMENT"
            if args.mechanism == "safety"
            else "TERMINAL-UPPER-QUARTILE",
            "case_id": args.case,
            "full_annual": end == YEAR_END,
            "end_time": str(end),
            "scope": "one mechanism on inspected development data; causal historical replay, not untouched holdout",
            "baseline_run_id": baseline,
            "candidate_run_id": args.candidate_run,
            "forecast_checks": {
                "forecasts": forecasts,
                "raw_point_forecasts_and_provenance_exact": True,
                "changed_policy_snapshots": changed,
            },
            "results": {"baseline": a, "candidate": b},
            "delta_candidate_minus_baseline": {k: b[k] - a[k] for k in a},
            "candidate_performance": read_json(after / "performance_summary.json"),
            "source_proofs": {
                str(run / name): sha256_file(run / name)
                for run in (before, after)
                for name in [
                    "manifest.json",
                    "effective_config.json",
                    "evidence_manifest.json",
                    "summary.json",
                ]
            },
            "tool_sha256": sha256_file(Path(__file__)),
        },
    )
    print("total cost delta", b["total_cost_cny"] - a["total_cost_cny"])


if __name__ == "__main__":
    main()
