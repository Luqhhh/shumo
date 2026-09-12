"""Immutable Q4 artifact inventories and forecast-to-execution audits."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

from ..approvals import decision_issues, load_decisions, normalize_decision_id
from ..dataio import sha256_file
from ..schemas import InputError
from .contracts import ENERGY_ABS_TOL_KWH as TOL
from .contracts import BatteryAction, BatteryState, InfoSet
from .dispatch_milp import (
    DispatchPlan,
    contract_hash,
    reuse_tail,
    solve_dispatch,
    validate_dispatch,
)
from .purchase_ledger import PurchaseLedger
from .q4_common import (
    ACTION_START,
    BLEND_DECISIONS,
    BLEND_MODEL_VERSION,
    STEP,
    YEAR_END,
    Q4Error,
    atomic_json,
    jsonable,
    reserve_start_from_config,
)
from .q4_forecasts import ForecastSnapshot

LOG_NAMES = (
    "forecasts",
    "contracts",
    "execution_feedback",
    "cost_ledger",
    "solver_records",
    "dispatch_plans",
)
ARTIFACT_NAMES = tuple(name + ".jsonl" for name in LOG_NAMES[:-1]) + (
    "domain_result.json",
    "input_snapshot.json",
    "effective_config.json",
    "validation.json",
    "summary.json",
)


def full_snapshot_sha256(snapshot: ForecastSnapshot) -> str:
    """Cover the entire immutable snapshot, including all provenance fields."""
    return hashlib.sha256(json.dumps(jsonable(snapshot), sort_keys=True).encode()).hexdigest()


def forecast_view(snapshot: ForecastSnapshot, slot: dt.datetime, digest: str) -> dict:
    start = int((slot - snapshot.issue_time) / STEP)
    if start < 0 or start >= len(snapshot.slots) or snapshot.slots[start] != slot:
        raise InputError("forecast view is outside its full snapshot")
    return {
        "view_schema_version": 1,
        "full_snapshot_sha256": digest,
        "slice_start": start,
        "slice_length": len(snapshot.slots) - start,
    }


def valid_forecast_view(saved, expected):
    return (
        isinstance(saved, dict)
        and saved.keys() == expected.keys()
        and all(
            type(saved[key]) is int
            for key in ("view_schema_version", "slice_start", "slice_length")
        )
        and isinstance(saved["full_snapshot_sha256"], str)
        and saved == expected
    )


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected object")
        return data
    except (OSError, ValueError) as exc:
        raise InputError(f"{path.name}: invalid or missing evidence: {exc}") from exc


def prefix_sha256(path: Path, size: int) -> str:
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise InputError("invalid checkpoint prefix size")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        remaining = size
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise InputError("checkpoint log shorter than committed prefix")
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def checkpoint_log_hashes(run_dir: Path, cache: dict) -> tuple[dict, dict]:
    sizes, hashes = {}, {}
    for name in LOG_NAMES:
        path = run_dir / f"{name}.jsonl"
        offset, digest = cache.setdefault(name, (0, hashlib.sha256()))
        with path.open("rb") as stream:
            stream.seek(offset)
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                offset += len(chunk)
        cache[name] = (offset, digest)
        sizes[name], hashes[name] = offset, digest.hexdigest()
    return sizes, hashes


def check_model_binding(repo: Path, run_dir: Path, export: dict | None = None) -> list[str]:
    issues = []
    try:
        manifest = read_json(run_dir / "manifest.json")
        config = read_json(run_dir / "effective_config.json")
        if type(config.get("evidence_schema_version", 1)) is not int or config.get(
            "evidence_schema_version", 1
        ) not in (1, 2):
            issues.append("unknown Q4 forecast evidence schema")
        reserve = reserve_start_from_config(config)
        case = manifest["case_id"]
        if case not in ("q4_2", "q4_3") or config.get("case_id") != case:
            issues.append("effective_config case mismatch")
        if config.get("is_synthetic") is not manifest.get("is_synthetic"):
            issues.append("effective_config synthetic flag mismatch")
        if config.get("timely_control") is not True or config.get("price_method") not in (
            "main",
            "lag1",
        ):
            issues.append("effective_config feedback/price method mismatch")
        for field in ("model_version", "end_time", "price_method"):
            if field in manifest and manifest[field] != config.get(field):
                issues.append(f"manifest/effective_config {field} mismatch")
        if export is not None and export.get("model_version") != config["model_version"]:
            issues.append("export/effective_config model_version mismatch")
        end = dt.datetime.fromisoformat(config["end_time"])
        if not ACTION_START < end <= YEAR_END or end.time() != dt.time():
            issues.append("effective_config execution period invalid")
        if manifest.get("full_annual") is True and end != YEAR_END:
            issues.append("effective_config annual coverage mismatch")
        if manifest.get("status") == "success" and (
            end != YEAR_END or config.get("price_method") != "main"
        ):
            issues.append("formal Q4 result requires full annual main configuration")
        current = load_decisions(repo)
        runtime = manifest.get("config_snapshot", {}).get("decisions.toml", {}).get("decisions", {})
        from ..cases import required_decisions

        ids = list(required_decisions(case))
        if reserve is None:
            ids = [decision_id for decision_id in ids if decision_id != "D_TERMINAL_RESERVE_Q4"]
        if config.get("price_method") == "lag1":
            ids.append("D_EVAL_Q4")
        if config["model_version"] == BLEND_MODEL_VERSION:
            ids.extend(BLEND_DECISIONS)
            issues.extend(
                f"{issue.decision_id}: {issue.reason}"
                for issue in decision_issues(repo, BLEND_DECISIONS)
            )
        expected_solver = {
            "presolve": True,
            "mip_rel_gap": 1e-4,
            "time_limits_seconds": [10, 60],
            "mip_feasibility_tolerance": 1e-9,
            "primal_feasibility_tolerance": 1e-8,
        }
        if config.get("solver") != expected_solver:
            issues.append("effective_config solver settings differ from approved Q4 implementation")
        for decision_id in ids:
            expected = current.get(normalize_decision_id(decision_id), {})
            if (
                expected.get("status") != "approved"
                or not expected.get("confirmed_by")
                or not expected.get("confirmed_at")
            ):
                issues.append(f"{decision_id}: current decision not explicitly approved")
            if "scope_cases" in expected and case not in expected["scope_cases"]:
                issues.append(f"{decision_id}: current decision scope mismatch")
            snapshot = runtime.get(decision_id)
            if not isinstance(snapshot, dict) or snapshot != expected:
                issues.append(f"{decision_id}: runtime decision snapshot missing or conflicting")
            if decision_id in ("D_TERMINAL_RESERVE_Q4", *BLEND_DECISIONS) and export is not None:
                saved = export.get("decision_snapshot", {}).get(decision_id)
                if not isinstance(saved, dict) or saved != expected or saved != snapshot:
                    issues.append(f"{decision_id}: export decision snapshot missing or conflicting")
        if reserve is None and config.get("terminal_reserve") is not None:
            issues.append("historical v2 cannot claim a terminal reserve configuration")
    except (InputError, Q4Error, KeyError, TypeError, ValueError, AttributeError) as exc:
        issues.append(f"Q4 model evidence invalid: {exc}")
    return issues


def supplementary_dir(repo: Path, case: str, run_id: str) -> Path:
    return repo / "outputs" / "evidence" / "q4_review" / case / run_id


def write_inventory(run_dir: Path, target: Path, audit: dict, *, plans_path: Path) -> Path:
    if target.exists():
        raise InputError("evidence inventory already exists; do not overwrite an audit")
    artifacts = {}
    for name in ARTIFACT_NAMES:
        path = run_dir / name
        artifacts[name] = {"sha256": sha256_file(path), "size_bytes": path.stat().st_size}
    manifest = read_json(run_dir / "manifest.json")
    data = {
        "schema_version": 1,
        "case_id": manifest["case_id"],
        "run_id": manifest["run_id"],
        "runtime_manifest_sha256": sha256_file(run_dir / "manifest.json")
        if target.parent != run_dir
        else None,
        "runtime_decisions_sha256": hashlib.sha256(
            json.dumps(manifest.get("config_snapshot"), sort_keys=True).encode()
        ).hexdigest(),
        "artifacts": artifacts,
        "dispatch_plans": {
            "name": plans_path.name,
            "sha256": sha256_file(plans_path),
            "size_bytes": plans_path.stat().st_size,
        },
        "verification_validation_sha256": sha256_file(target.parent / "validation.json"),
        "controller_audit": audit,
        "audit_kind": "runtime" if target.parent == run_dir else "post_run_verification",
    }
    if target.parent != run_dir:
        data["review_validation_sha256"] = sha256_file(target.parent / "review_validation.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(target, data)
    return target


def check_inventory(repo: Path, run_dir: Path, *, export: dict | None = None) -> list[str]:
    issues = []
    try:
        manifest = read_json(run_dir / "manifest.json")
        target = run_dir / "evidence_manifest.json"
        supplemental = not target.is_file()
        if supplemental:
            target = (
                supplementary_dir(repo, manifest["case_id"], manifest["run_id"])
                / "evidence_manifest.json"
            )
        inventory = read_json(target)
        if (inventory.get("case_id"), inventory.get("run_id")) != (
            manifest.get("case_id"),
            manifest.get("run_id"),
        ):
            issues.append("Q4 artifact inventory identity mismatch")
        if supplemental:
            if inventory.get("runtime_manifest_sha256") != sha256_file(run_dir / "manifest.json"):
                issues.append("Q4 supplemental inventory runtime manifest hash mismatch")
            review_path = target.parent / "review_validation.json"
            review = read_json(review_path)
            if (
                inventory.get("review_validation_sha256") != sha256_file(review_path)
                or review.get("ok") is not True
                or review.get("original_artifacts_unchanged") is not True
                or review.get("verifier_source_unchanged") is not True
            ):
                issues.append(
                    "Q4 supplemental review missing, changed or not successfully finalized"
                )
        elif manifest.get("evidence_manifest_sha256") != sha256_file(target):
            issues.append("Q4 evidence_manifest_sha256 mismatch")
        expected_decisions = hashlib.sha256(
            json.dumps(manifest.get("config_snapshot"), sort_keys=True).encode()
        ).hexdigest()
        if inventory.get("runtime_decisions_sha256") != expected_decisions:
            issues.append("Q4 artifact inventory runtime decision snapshot mismatch")
        for name in ARTIFACT_NAMES:
            description = inventory.get("artifacts", {}).get(name, {})
            path = run_dir / name
            if not path.is_file() or not description:
                issues.append(f"Q4 artifact missing: {name}")
            elif (
                description.get("sha256") != sha256_file(path)
                or description.get("size_bytes") != path.stat().st_size
            ):
                issues.append(f"Q4 artifact hash/size mismatch: {name}")
        plans = inventory.get("dispatch_plans", {})
        plans_path = target.parent / str(plans.get("name", ""))
        if (
            plans_path.name != "dispatch_plans.jsonl"
            or plans_path.parent != target.parent
            or not plans_path.is_file()
        ):
            issues.append("Q4 dispatch plans missing or unowned")
        elif (
            plans.get("sha256") != sha256_file(plans_path)
            or plans.get("size_bytes") != plans_path.stat().st_size
        ):
            issues.append("Q4 dispatch plans hash/size mismatch")
        audit = inventory.get("controller_audit", {})
        config = read_json(run_dir / "effective_config.json")
        expected_count = int((dt.datetime.fromisoformat(config["end_time"]) - ACTION_START) / STEP)
        if (
            audit.get("ok") is not True
            or audit.get("checked_intervals") != expected_count
            or audit.get("causal_forecasts_ok") is not True
            or audit.get("intent_plan_binding_ok") is not True
        ):
            issues.append("Q4 controller audit incomplete or failed")
        validation_path = target.parent / "validation.json"
        validation = read_json(validation_path)
        if (
            inventory.get("verification_validation_sha256") != sha256_file(validation_path)
            or validation.get("ok") is not True
            or validation.get("feedback_rule_verified") is not True
            or validation.get("checked_intervals") != expected_count
        ):
            issues.append("Q4 strict feedback validation missing, changed or incomplete")
        if (
            export is not None
            and not supplemental
            and export.get("evidence_manifest_sha256") != sha256_file(target)
        ):
            issues.append("Q4 export evidence inventory hash mismatch")
    except (InputError, KeyError, TypeError, ValueError, OSError, AttributeError) as exc:
        issues.append(f"Q4 artifact evidence invalid: {exc}")
    return issues


def snapshot_from_dict(row):
    row = dict(row)
    row["issue_time"] = dt.datetime.fromisoformat(row["issue_time"])
    row["slots"] = tuple(dt.datetime.fromisoformat(x) for x in row["slots"])
    for key in ("load_kwh", "pv_kwh", "prices"):
        row[key] = tuple(row[key])
    return ForecastSnapshot(**row)


def plan_from_dict(row):
    try:
        row = dict(row)
        row["flows"] = tuple(tuple(x) for x in row["flows"])
        row["energy"] = tuple(row["energy"])
        return DispatchPlan(**row)
    except (TypeError, ValueError, KeyError) as exc:
        raise InputError(f"invalid or missing saved dispatch plan: {exc}") from exc


def json_rows(path: Path):
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("expected object")
                yield row
            except ValueError as exc:
                raise InputError(f"{path.name}:{number}: invalid evidence row") from exc


def audit_controller_chain(
    run_dir: Path, inputs, *, plans_path: Path, reconstruct: bool = False, performance=None
) -> dict:
    """Replay forecasts and independently check saved or reconstructed MILP plans.

    Reconstruction writes a separate verification artifact, never original logs.
    It checks the existing intentions rather than executing a new trajectory.
    """
    from .q4_common import append_json
    from .rolling_engine import _initialize_forecaster

    config = read_json(run_dir / "effective_config.json")
    case = config["case_id"]
    end = dt.datetime.fromisoformat(config["end_time"])
    reserve_start = reserve_start_from_config(config)
    if reconstruct and plans_path.exists():
        raise InputError("reconstructed plan evidence already exists")
    service = _initialize_forecaster(
        inputs, case, config["price_method"], ACTION_START, model_version=config["model_version"]
    )
    official = defaultdict(list)
    for item in inputs.official:
        official[item.available_at].append(item)
    forecasts = iter(json_rows(run_dir / "forecasts.jsonl"))
    solvers = iter(json_rows(run_dir / "solver_records.jsonl"))
    executions = iter(json_rows(run_dir / "execution_feedback.jsonl"))
    contracts = iter(json_rows(run_dir / "contracts.jsonl"))
    plans = None if reconstruct else iter(json_rows(plans_path))
    if reconstruct:
        plans_path.parent.mkdir(parents=True, exist_ok=True)
        plans_path.touch()
    ledger, state, previous, snapshot = PurchaseLedger(case), BatteryState(6000), None, None
    refresh_count = solve_count = reuse_count = 0
    count = int((end - ACTION_START) / STEP)
    domain = read_json(run_dir / "domain_result.json")
    domain_intervals = domain.get("intervals")
    if not isinstance(domain_intervals, list) or len(domain_intervals) != count:
        raise InputError("domain_result/execution interval coverage mismatch")
    for k in range(count):
        slot = ACTION_START + k * STEP
        if k:
            service.ingest(
                InfoSet.from_raw(
                    slot, inputs.actual_info(inputs.index(slot) - 1) + tuple(official.get(slot, []))
                )
            )
        refresh = slot.hour in (0, 6, 12, 18) and slot.minute == 0
        if refresh:
            row = next(forecasts, None)
            if row is None:
                raise InputError(f"{slot}: missing forecast snapshot")
            expected = jsonable(service.refresh(InfoSet.from_raw(slot, ())))
            if row != expected:
                raise InputError(f"{slot}: forecast differs from causal history replay")
            snapshot = snapshot_from_dict(row)
            snapshot_digest = full_snapshot_sha256(snapshot)
            refresh_count += 1
        window = snapshot.sliced(slot)
        solver, execution = next(solvers, None), next(executions, None)
        if solver is None or execution is None:
            raise InputError(f"{slot}: missing solver/execution evidence")
        if execution.get("interval") != domain_intervals[k]:
            raise InputError(f"{slot}: execution interval differs from domain_result")
        if (
            execution.get("slot_start") != str(slot)
            or solver.get("window_start") != str(slot)
            or solver.get("window_end") != str(window.slots[-1] + STEP)
            or solver.get("forecast_id") != snapshot.snapshot_id
            or solver.get("status") != 0
        ):
            raise InputError(f"{slot}: solver/forecast/execution identity or status mismatch")
        if solver.get("contract_hash") != contract_hash(ledger, window):
            raise InputError(f"{slot}: solver contract hash mismatch")
        if solver.get("reused_tail") is True:
            if (
                previous is None
                or refresh
                or solver.get("parent_solve_id") != previous.solve_id
                or solver.get("shift_count") != previous.shift_count + 1
            ):
                raise InputError(f"{slot}: invalid reuse parent/shift")
            plan = reuse_tail(previous, state, window, ledger, reserve_start=reserve_start)
            if plan is None:
                raise InputError(f"{slot}: reuse certificate cannot be independently verified")
            reuse_count += 1
        else:
            plan = (
                solve_dispatch(
                    state, window, ledger, reserve_start=reserve_start, performance=performance
                )
                if reconstruct
                else plan_from_dict(next(plans, {}))
            )
            if plan.shift_count != 0:
                raise InputError(f"{slot}: fresh solve has a shifted plan")
            solve_count += 1
            if reconstruct:
                append_json(plans_path, plan)
        # reuse_tail already independently validates its reconstructed plan.
        problems = (
            ()
            if solver.get("reused_tail") is True
            else validate_dispatch(plan, state, window, ledger, reserve_start=reserve_start)
        )
        if (
            not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                for value in (
                    plan.objective,
                    plan.absolute_gap,
                    solver.get("objective"),
                    solver.get("dual_bound"),
                )
            )
            or plan.absolute_gap < 0
        ):
            raise InputError(f"{slot}: nonfinite/invalid objective or absolute bound")
        if not solver.get("reused_tail"):
            expected_solve_id = hashlib.sha256(
                (
                    snapshot.snapshot_id
                    + str(slot)
                    + str(state.energy_kwh)
                    + contract_hash(ledger, window)
                ).encode()
            ).hexdigest()
            if plan.solve_id != expected_solve_id:
                raise InputError(f"{slot}: solve identity differs from input state/context")
            raw = plan.solver_record.get("objective_raw_solver")
            bound = plan.solver_record.get("dual_bound")
            if (
                not all(
                    isinstance(value, (int, float)) and math.isfinite(value)
                    for value in (raw, bound)
                )
                or plan.solver_record.get("status") != 0
            ):
                raise InputError(f"{slot}: saved native plan lacks a finite accepted solver bound")
            expected_absolute_gap = max(0.0, raw - bound) + abs(plan.objective - raw)
            if (
                abs(plan.absolute_gap - expected_absolute_gap) > 1e-5
                or abs(solver["dual_bound"] - bound) > 1e-5
            ):
                raise InputError(f"{slot}: native absolute bound mismatch")
        if solver.get("reused_tail"):
            if abs(solver.get("absolute_gap_bound", float("inf")) - plan.absolute_gap) > 1e-5:
                raise InputError(f"{slot}: reused absolute bound mismatch")
        if solver.get("dual_bound") > solver.get("objective") + 1e-5:
            raise InputError(f"{slot}: solver bound exceeds primal objective")
        if (
            problems
            or solver.get("solve_id") != plan.solve_id
            or abs(solver.get("objective", float("inf")) - plan.objective) > 1e-5
        ):
            raise InputError(f"{slot}: dispatch plan/solver mismatch: {problems}")
        intent = BatteryAction(**execution["execution"]["intent"])
        if (
            max(
                abs(intent.charge_kwh - plan.intention().charge_kwh),
                abs(intent.discharge_kwh - plan.intention().discharge_kwh),
            )
            > TOL
        ):
            raise InputError(f"{slot}: execution intent differs from verified dispatch plan")
        if BatteryState(**execution["execution"]["state_start"]) != state:
            raise InputError(f"{slot}: execution state discontinuity")
        if "intent" in solver and solver["intent"] != execution["execution"]["intent"]:
            raise InputError(f"{slot}: solver intent binding mismatch")
        expected_view = forecast_view(snapshot, slot, snapshot_digest)
        if config.get("evidence_schema_version", 1) == 2 or "forecast_view" in solver:
            if not valid_forecast_view(solver.get("forecast_view"), expected_view):
                raise InputError(f"{slot}: full snapshot/slice reference mismatch")
            if not solver.get("reused_tail") and not valid_forecast_view(
                plan.solver_record.get("forecast_view"), expected_view
            ):
                raise InputError(f"{slot}: saved native plan forecast view mismatch")
        if (
            "forecast_sha256" in solver
            and solver["forecast_sha256"]
            != hashlib.sha256(json.dumps(jsonable(window), sort_keys=True).encode()).hexdigest()
        ):
            raise InputError(f"{slot}: sliced forecast hash mismatch")
        version = ledger.submit(slot, window.slots, tuple(flow[0] for flow in plan.flows))
        if version:
            saved = next(contracts, None)
            if saved is None or saved != jsonable(version):
                raise InputError(f"{slot}: contract differs from verified dispatch plan")
        state = BatteryState(**execution["execution"]["state_end"])
        previous = plan
        if reconstruct and (k + 1) % 1440 == 0:
            print(
                f"{case}: controller audit {k + 1}/{count}, fresh={solve_count}, reused={reuse_count}",
                flush=True,
            )
    for name, iterator in (
        ("forecasts", forecasts),
        ("solver_records", solvers),
        ("execution_feedback", executions),
        ("contracts", contracts),
        ("dispatch_plans", plans),
    ):
        if iterator is not None and next(iterator, None) is not None:
            raise InputError(f"{name}: extra records after verified coverage")
    summary = read_json(run_dir / "summary.json")
    if (
        summary.get("interval_count"),
        summary.get("solve_count"),
        summary.get("reused_tail_count"),
    ) != (count, solve_count, reuse_count):
        raise InputError("summary controller counts differ from verified evidence")
    return {
        "schema_version": 1,
        "ok": True,
        "checked_intervals": count,
        "checked_forecasts": refresh_count,
        "checked_fresh_plans": solve_count,
        "checked_reused_tails": reuse_count,
        "causal_forecasts_ok": True,
        "intent_plan_binding_ok": True,
        "plans_origin": "post_run_solver_reconstruction" if reconstruct else "runtime_saved_plans",
    }
