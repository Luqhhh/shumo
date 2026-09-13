"""Pair original and experimental Q4 solves on a preserved frozen-window input."""

from __future__ import annotations

import argparse
import datetime as dt
import time
from pathlib import Path

from microgrid.dataio import sha256_file
from microgrid.problem.contracts import BatteryState
from microgrid.problem.dispatch_milp import solve_dispatch
from microgrid.problem.purchase_ledger import ContractVersion, PurchaseLedger
from microgrid.problem.q4_common import atomic_json
from microgrid.problem.q4_evidence import read_json, snapshot_from_dict
from microgrid.problem.q4_solver_methods import METHODS, solver_method_parameters
from microgrid.schemas import InputError


def restore_ledger(row):
    ledger = PurchaseLedger(row["case_id"])
    for day, versions in row["versions"].items():
        converted = []
        for version in versions:
            version = dict(version)
            version["day"] = dt.date.fromisoformat(version["day"])
            version["event_time"] = dt.datetime.fromisoformat(version["event_time"])
            for field in ("quantities", "plus", "minus"):
                version[field] = tuple(version[field])
            converted.append(ContractVersion(**version))
        ledger.versions[dt.date.fromisoformat(day)] = converted
    return ledger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--method", choices=[value for value in METHODS if value != "scipy"], required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise InputError("benchmark output exists; retain prior evidence")
    source_hash = sha256_file(args.input)
    source = read_json(args.input)
    if (
        source.get("source_artifacts_unchanged") is not True
        or not source.get("source_sha256")
        or not source.get("cases")
    ):
        raise InputError("frozen real-window source binding is missing")
    totals = {
        "baseline_seconds": 0.0,
        "candidate_seconds": 0.0,
        "windows": 0,
        "intention_changes": 0,
        "max_objective_difference_cny": 0.0,
        "failures": [],
        "method_records": [],
    }
    for index, row in enumerate(source["cases"]):
        forecast = snapshot_from_dict(row["forecast"])
        state = BatteryState(**row["state"])
        ledger = restore_ledger(row)
        plans = {}
        pair = [("baseline", "scipy"), ("candidate", args.method)]
        for label, method in pair if index % 2 == 0 else reversed(pair):
            started = time.perf_counter()
            try:
                plans[label] = solve_dispatch(state, forecast, ledger, solver_method=method)
            except Exception as exc:
                totals["failures"].append(
                    {
                        "slot": row["slot"],
                        "case": row["case_id"],
                        "label": label,
                        "exception": repr(exc),
                    }
                )
            finally:
                totals[label + "_seconds"] += time.perf_counter() - started
        totals["windows"] += 1
        if len(plans) == 2:
            a, b = plans["baseline"], plans["candidate"]
            delta = abs(a.objective - b.objective)
            totals["max_objective_difference_cny"] = max(
                totals["max_objective_difference_cny"], delta
            )
            totals["intention_changes"] += a.intention() != b.intention()
            totals["method_records"].append(b.solver_record["solver_method_record"])
            if delta > max(a.absolute_gap, b.absolute_gap) + 1e-4:
                totals["failures"].append(
                    {
                        "slot": row["slot"],
                        "exception": "objective outside original accepted gap envelope",
                        "difference": delta,
                    }
                )
        if index % 100 == 0:
            print(args.method, index, flush=True)
    if sha256_file(args.input) != source_hash:
        raise InputError("frozen input changed during benchmark")
    totals["speedup_ratio"] = totals["baseline_seconds"] / totals["candidate_seconds"]
    atomic_json(
        args.output,
        {
            "ok": not totals["failures"],
            "scope": "paired frozen native windows; shared-machine timing, not annual closed-loop fee identity",
            "method": args.method,
            "parameters": solver_method_parameters(args.method),
            "input_sha256": source_hash,
            "source_sha256": source["source_sha256"],
            "tool_sha256": sha256_file(Path(__file__)),
            "result": totals,
        },
    )
    print(
        "windows",
        totals["windows"],
        "failures",
        len(totals["failures"]),
        "ratio",
        totals["speedup_ratio"],
    )
    if totals["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
