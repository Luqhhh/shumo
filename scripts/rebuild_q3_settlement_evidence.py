"""Rebuild diagnostic settlement artifacts from a complete persisted Q3 trajectory.

This does not execute MPC. Source evidence is hash-checked and never overwritten;
the new manifest separately identifies trajectory and serialization provenance.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import shutil
from dataclasses import replace
from pathlib import Path

from microgrid.approvals import require_approved_decisions
from microgrid.artifacts import (
    build_manifest,
    ensure_run_id_available,
    file_digest,
    input_hashes,
    verify_imported_inputs,
    write_json,
    write_manifest,
)
from microgrid.problem.contracts import ENERGY_ABS_TOL_KWH, CaseResult, TimeGrid
from microgrid.problem.q3 import REQUIRED_DECISIONS
from microgrid.problem.q3_plan_ledger import Q3EmergencySettlementEntry, Q3PlanLedger
from microgrid.problem.q3_runtime_inputs import (
    Q3_EVALUATION_END,
    Q3_EVALUATION_START,
    inclusive_days,
)
from microgrid.problem.q3_sidecars import load_q3_sidecar, settlement_rows
from microgrid.problem.q3_terminal_reserve import validate_q3_terminal_reserve
from microgrid.problem.result_io import load_case_result, save_case_result
from microgrid.schemas import InputError


def ledgers_from_plan_rows(
    rows: tuple[dict, ...], prices: tuple[float, ...]
) -> tuple[Q3PlanLedger, ...]:
    groups: dict[dt.datetime, dict[int, float]] = {}
    source_rows: dict[tuple[dt.datetime, int], dict] = {}
    for row in rows:
        issue = dt.datetime.fromisoformat(row["issue_time"])
        start = dt.datetime.fromisoformat(row["target_slot_start"])
        if issue.date() != start.date() or issue.time() not in (
            dt.time(),
            dt.time(6),
            dt.time(12),
            dt.time(18),
        ):
            raise InputError("source plan has an invalid publication or target day")
        slot = (start.hour * 60 + start.minute) // 10
        interval = TimeGrid().interval(start.date(), slot)
        if start != interval.start or row["target_slot_end"] != interval.end.isoformat():
            raise InputError("source plan target is not on the ten-minute grid")
        if row["purchase_is_fixed"] != (start < issue):
            raise InputError("source plan frozen flag is inconsistent")
        quantities = groups.setdefault(issue, {})
        if slot in quantities:
            raise InputError("source plan has duplicate issue/slot")
        quantities[slot] = row["committed_kwh"]
        source_rows[(issue, slot)] = row
    ledgers: list[Q3PlanLedger] = []
    for day in sorted({issue.date() for issue in groups}):
        ledger = None
        for hour in (0, 6, 12, 18):
            issue = dt.datetime.combine(day, dt.time(hour))
            quantities = groups.get(issue, {})
            if set(quantities) != set(range(144)):
                raise InputError("source plan does not contain all four complete daily versions")
            committed = tuple(quantities[slot] for slot in range(144))
            for slot in range(144):
                previous = source_rows[(issue, slot)]["previous_committed_kwh"]
                if ledger is None:
                    if previous is not None:
                        raise InputError("source version zero must not have a previous commitment")
                elif previous != ledger.current.committed_kwh[slot]:
                    raise InputError(
                        "source previous commitment does not match its complete version chain"
                    )
            if ledger is None:
                ledger = Q3PlanLedger.start(
                    day, initial_commitments_kwh=committed, base_prices_cny_per_kwh=prices
                )
            else:
                ledger = ledger.revise(
                    issue,
                    new_commitments_kwh=committed,
                    transaction_price_cny_per_kwh=prices[hour * 6],
                )
        assert ledger is not None
        ledgers.append(ledger)
    return tuple(ledgers)


def rebuild(repo: Path, *, source_run_id: str, run_id: str) -> CaseResult:
    for value in (source_run_id, run_id):
        if value in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise InputError("run IDs must be simple directory names")
    if source_run_id == run_id:
        raise InputError("source and target run IDs must differ")
    require_approved_decisions(repo, REQUIRED_DECISIONS)
    source = repo / "outputs/runs/q3" / source_run_id
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if (
        manifest.get("status") != "success"
        or manifest.get("diagnostic_only") is not True
        or manifest.get("validation_ok") is not True
    ):
        raise InputError("source must be a successful diagnostic with validation")
    if (
        manifest.get("is_synthetic") is not False
        or verify_imported_inputs(repo)
        or manifest.get("input_hashes") != input_hashes(repo)
    ):
        raise InputError("source inputs must be verified non-synthetic attachments")
    for name in (
        "domain_result.json",
        "forecast_provenance.jsonl",
        "plan_versions.jsonl",
        "settlement_ledger.jsonl",
    ):
        if file_digest(source / name) != manifest["result_sha256"].get(name):
            raise InputError(f"source artifact hash mismatch: {name}")
    expected = inclusive_days(Q3_EVALUATION_START, Q3_EVALUATION_END)
    result = load_case_result(source / "domain_result.json", expected_days=expected)
    if (
        len(result.intervals) != 48096
        or abs(result.intervals[-1].state_end.energy_kwh - 6000) > 1e-6
    ):
        raise InputError("source is not a complete approved annual trajectory")
    reserve = validate_q3_terminal_reserve(result.intervals)
    if not reserve["ok"] or reserve["checked_boundary_count"] != 145:
        raise InputError("source realized reserve audit failed")
    old_rows = load_q3_sidecar(source / "settlement_ledger.jsonl")
    prices = {
        ((start := dt.datetime.fromisoformat(row["target_slot_start"])).hour * 60 + start.minute)
        // 10: row["price_cny_per_kwh"]
        for row in old_rows
        if row["record_type"] == "base_plan"
    }
    ledgers = ledgers_from_plan_rows(
        load_q3_sidecar(source / "plan_versions.jsonl"), tuple(prices[k] for k in range(144))
    )
    emergency = tuple(
        Q3EmergencySettlementEntry(
            day=i.day,
            slot=i.slot,
            settled_at=TimeGrid().interval(i.day, i.slot).end,
            energy_kwh=i.emergency_purchase_kwh,
            price_cny_per_kwh=prices[i.slot],
            cost_cny=5 * prices[i.slot] * i.emergency_purchase_kwh,
        )
        for i in result.intervals
        if i.emergency_purchase_kwh > ENERGY_ABS_TOL_KWH
    )
    rows = settlement_rows(ledgers, emergency_entries=emergency)
    component_names = {
        "base_plan": "planned_cost_cny",
        "adjustment": "adjustment_cost_cny",
        "emergency": "emergency_cost_cny",
    }
    for kind, key in component_names.items():
        cost = math.fsum(row["cost_cny"] for row in rows if row["record_type"] == kind)
        if abs(cost - result.metadata[key]) > 1e-5:
            raise InputError(f"rebuilt {kind} does not reconcile to source summary")
    target = ensure_run_id_available(repo, "q3", run_id)
    target.mkdir(parents=True, exist_ok=True)
    for name in ("forecast_provenance.jsonl", "plan_versions.jsonl"):
        shutil.copy2(source / name, target / name)
    settlement_path = target / "settlement_ledger.jsonl"
    with settlement_path.open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    provenance = {
        "run_id": source_run_id,
        "code_commit": manifest["code_commit"],
        "source_hash": manifest["source_hash"],
        "manifest_sha256": file_digest(source / "manifest.json"),
        "summary_sha256": file_digest(source / "summary.json"),
        "result_sha256": manifest["result_sha256"],
        "operation": "settlement serialization only; no MPC rerun",
    }
    metadata = dict(result.metadata)
    metadata["sidecars"] = {name: dict(info) for name, info in metadata["sidecars"].items()}
    metadata["sidecars"]["settlement_ledger"].update(
        sha256=file_digest(settlement_path), row_count=len(rows)
    )
    metadata["trajectory_source"] = provenance
    rebuilt = replace(result, run_id=run_id, metadata=metadata)
    save_case_result(target / "domain_result.json", rebuilt)
    if load_case_result(target / "domain_result.json", expected_days=expected) != rebuilt:
        raise InputError("rebuilt domain result did not round-trip")
    summary = json.loads((source / "summary.json").read_text(encoding="utf-8"))
    summary.update(run_id=run_id, trajectory_source=provenance)
    write_json(target / "summary.json", summary)
    names = (
        "domain_result.json",
        "forecast_provenance.jsonl",
        "plan_versions.jsonl",
        "settlement_ledger.jsonl",
    )
    rebuilt_manifest = build_manifest(
        repo,
        run_id=run_id,
        case_id="q3",
        command=[
            "python",
            "scripts/rebuild_q3_settlement_evidence.py",
            "--source-run-id",
            source_run_id,
            "--run-id",
            run_id,
        ],
        status="success",
        is_synthetic=False,
        model_status="implemented_derived_artifact_rebuild",
        result_files={n: (target / n).relative_to(repo).as_posix() for n in names},
        result_sha256={n: file_digest(target / n) for n in names},
        verify_inputs=True,
    )
    rebuilt_manifest.update(
        validation_ok=True,
        diagnostic_only=True,
        artifact_regeneration_only=True,
        trajectory_source=provenance,
        evaluation_start=expected[0].isoformat(),
        evaluation_end=expected[-1].isoformat(),
    )
    write_manifest(target, rebuilt_manifest)
    return rebuilt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    result = rebuild(Path.cwd(), source_run_id=args.source_run_id, run_id=args.run_id)
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "interval_count": len(result.intervals),
                "artifact_regeneration_only": True,
                "trajectory_source": args.source_run_id,
            }
        )
    )


if __name__ == "__main__":
    main()
