"""One continuous causal replay for either approved Q4 case."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

import numpy as np

from ..approvals import require_approved_decisions
from ..artifacts import (
    build_manifest,
    ensure_run_id_available,
    new_run_id,
    source_tree_hash,
    write_manifest,
)
from ..dataio import sha256_file
from ..schemas import InputError
from .contracts import BatteryAction, BatteryState, CaseContext, CaseResult, InfoSet, IntervalResult
from .dispatch_feedback import ExecutionRecord, Measurement, apply_feedback
from .dispatch_milp import DispatchPlan, reuse_tail, solve_dispatch
from .purchase_ledger import Bill, ContractVersion, PurchaseLedger
from .q4_common import (
    ACTION_START,
    MODEL_VERSION,
    RESERVE_START,
    STEP,
    YEAR_END,
    Q4Error,
    append_json,
    atomic_json,
    jsonable,
)
from .q4_evidence import (
    LOG_NAMES,
    audit_controller_chain,
    checkpoint_log_hashes,
    prefix_sha256,
    write_inventory,
)
from .q4_forecasts import ForecastSnapshot, Q4Forecaster
from .q4_inputs import Q4Inputs, load_q4_inputs
from .q4_validation import validate_q4_run
from .result_io import interval_from_dict, interval_to_dict, save_case_result


def _initialize_forecaster(
    inputs: Q4Inputs,
    case_id: str,
    price_method: str,
    time_at: dt.datetime,
    *,
    model_version: str = MODEL_VERSION,
):
    service = Q4Forecaster(case_id, inputs.source_hashes, price_method, model_version=model_version)
    records = (
        [item for index in range(inputs.index(time_at)) for item in inputs.actual_info(index)]
        if time_at < YEAR_END
        else [item for index in range(52560) for item in inputs.actual_info(index)]
    )
    records.extend(inputs.official)
    service.ingest(InfoSet.from_raw(time_at, tuple(records)))
    return service


def _snapshot_from_dict(data):
    data = dict(data)
    data["issue_time"] = dt.datetime.fromisoformat(data["issue_time"])
    data["slots"] = tuple(dt.datetime.fromisoformat(value) for value in data["slots"])
    for key in ("load_kwh", "pv_kwh", "prices"):
        data[key] = tuple(data[key])
    return ForecastSnapshot(**data)


def _execution_from_dict(data):
    data = dict(data)
    for key in ("intent", "action"):
        data[key] = BatteryAction(**data[key])
    for key in ("state_start", "state_end"):
        data[key] = BatteryState(**data[key])
    data["reasons"] = tuple(data["reasons"])
    return ExecutionRecord(**data)


def _ledger_from_dict(data):
    ledger = PurchaseLedger(data["case_id"])
    for day, versions in data["versions"].items():
        restored = []
        for item in versions:
            item = dict(item)
            item["day"] = dt.date.fromisoformat(item["day"])
            item["event_time"] = dt.datetime.fromisoformat(item["event_time"])
            for key in ("quantities", "plus", "minus"):
                item[key] = tuple(item[key])
            restored.append(ContractVersion(**item))
        ledger.versions[dt.date.fromisoformat(day)] = restored
    for slot, bill in data["bills"].items():
        bill = dict(bill)
        bill["slot_start"] = dt.datetime.fromisoformat(bill["slot_start"])
        ledger.bills[dt.datetime.fromisoformat(slot)] = Bill(**bill)
    return ledger


def _metrics(run_dir: Path, inputs: Q4Inputs, end: dt.datetime):
    errors = defaultdict(list)
    last = inputs.index(end) if end < YEAR_END else 52560
    with (run_dir / "forecasts.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            forecast = _snapshot_from_dict(json.loads(line))
            start = inputs.index(forecast.issue_time)
            for h, (load, pv, price) in enumerate(
                zip(forecast.load_kwh, forecast.pv_kwh, forecast.prices, strict=True)
            ):
                index = start + h
                if index >= last:
                    break
                for variable, estimate, actual in (
                    ("load_kw", load * 6, inputs.load_kw[index]),
                    ("pv_kw", pv * 6, inputs.pv_kw[index]),
                    ("price_cny_per_kwh", price, inputs.prices[index]),
                ):
                    if h < 36:
                        errors[(variable, "6h")].append(estimate - actual)
                    errors[(variable, "24h")].append(estimate - actual)
                    errors[(variable, "month:" + forecast.slots[h].strftime("%Y-%m"))].append(
                        estimate - actual
                    )
    return {
        f"{variable}:{window}": {
            "count": len(values),
            "mae": float(np.mean(np.abs(values))),
            "rmse": float(np.sqrt(np.mean(np.square(values)))),
        }
        for (variable, window), values in errors.items()
    }


def run_q4(context: CaseContext) -> CaseResult:
    # This direct entry also gates approval; callers cannot bypass the dispatcher.
    from ..cases import required_decisions

    require_approved_decisions(context.repo_root, required_decisions(context.case_id))
    end = dt.datetime.fromisoformat(str(context.metadata.get("end_time", YEAR_END.isoformat())))
    if not ACTION_START < end <= YEAR_END or end.time() != dt.time():
        raise InputError("Q4 end_time must be midnight in 2025-02-02..2026-01-01")
    method = str(context.metadata.get("price_method", "main"))
    if method not in ("main", "lag1"):
        raise InputError("Q4 price_method must be main or lag1")
    if method == "lag1":
        require_approved_decisions(context.repo_root, ("D_EVAL_Q4",))
    run_id = context.run_id or new_run_id(context.case_id, context.repo_root)
    resume = bool(context.metadata.get("resume", False))
    run_dir = (
        context.resolved_output_dir()
        if context.output_dir
        else context.repo_root / "outputs" / "runs" / context.case_id / run_id
    )
    if not resume:
        if context.output_dir:
            if run_dir.exists() and any(run_dir.iterdir()):
                raise InputError("Q4 output directory is not empty")
        else:
            ensure_run_id_available(context.repo_root, context.case_id, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
    elif not context.run_id or not (run_dir / "checkpoint.json").is_file():
        raise InputError("resume requires explicit existing run_id and checkpoint")
    config = {
        "schema_version": 1,
        "model_version": MODEL_VERSION,
        "terminal_reserve": {"start": str(RESERVE_START), "minimum_energy_kwh": 6000.0},
        "case_id": context.case_id,
        "end_time": str(end),
        "price_method": method,
        "timely_control": True,
        "is_synthetic": context.is_synthetic,
        "solver": {
            "presolve": True,
            "mip_rel_gap": 1e-4,
            "time_limits_seconds": [10, 60],
            "mip_feasibility_tolerance": 1e-9,
            "primal_feasibility_tolerance": 1e-8,
        },
    }
    started = time.perf_counter()
    stage = "input_preflight"
    step_count = 0
    inputs = None
    intervals, executions = [], []
    ledger = PurchaseLedger(context.case_id)
    state = BatteryState(6000)
    forecast = previous_plan = None
    time_at = ACTION_START
    solve_count = reused_count = protection_count = 0
    log_hash_cache = {}
    manifest = build_manifest(
        context.repo_root,
        run_id=run_id,
        case_id=context.case_id,
        command=["python", "-m", "microgrid", "run", "--case", context.case_id],
        status="running",
        is_synthetic=context.is_synthetic,
        model_status="implemented",
    )
    code_hash = source_tree_hash(context.repo_root)
    try:
        inputs = load_q4_inputs(context.repo_root, context.case_id)
        if not resume:
            write_manifest(run_dir, manifest)
            atomic_json(run_dir / "input_snapshot.json", inputs.snapshot)
            atomic_json(run_dir / "effective_config.json", config)
            for name in LOG_NAMES:
                (run_dir / f"{name}.jsonl").touch()
            service = _initialize_forecaster(inputs, context.case_id, method, ACTION_START)
        else:
            checkpoint = json.loads((run_dir / "checkpoint.json").read_text())
            if (
                checkpoint.get("schema_version") != 2
                or checkpoint["source_hash"] != code_hash
                or checkpoint["source_hashes"] != inputs.source_hashes
                or checkpoint["config"] != config
            ):
                raise InputError("checkpoint source/config/schema mismatch")
            if set(checkpoint.get("log_sizes", {})) != set(LOG_NAMES) or set(
                checkpoint.get("log_sha256", {})
            ) != set(LOG_NAMES):
                raise InputError("checkpoint log inventory incomplete")
            for name, size in checkpoint["log_sizes"].items():
                path = run_dir / f"{name}.jsonl"
                if (
                    not path.is_file()
                    or prefix_sha256(path, size) != checkpoint["log_sha256"][name]
                ):
                    raise InputError(f"checkpoint committed log prefix hash mismatch: {name}")
            time_at = dt.datetime.fromisoformat(checkpoint["time"])
            service = _initialize_forecaster(inputs, context.case_id, method, time_at)
            if jsonable(service.training_state()) != checkpoint["training_state"]:
                raise InputError("checkpoint training state mismatch")
            for name, size in checkpoint["log_sizes"].items():
                with (run_dir / f"{name}.jsonl").open("r+b") as stream:
                    stream.truncate(size)
            ledger = _ledger_from_dict(checkpoint["ledger"])
            state = BatteryState(checkpoint["energy_kwh"])
            forecast = _snapshot_from_dict(checkpoint["forecast"])
            data = checkpoint["previous_plan"]
            previous_plan = DispatchPlan(
                **{
                    **data,
                    "flows": tuple(tuple(x) for x in data["flows"]),
                    "energy": tuple(data["energy"]),
                }
            )
            with (run_dir / "execution_feedback.jsonl").open() as stream:
                for line in stream:
                    row = json.loads(line)
                    intervals.append(interval_from_dict(row["interval"]))
                    executions.append(_execution_from_dict(row["execution"]))
            step_count = len(intervals)
            solve_count, reused_count, protection_count = checkpoint["counts"]
            write_manifest(run_dir, manifest, overwrite=True)
        official_by_issue = defaultdict(list)
        for item in inputs.official:
            official_by_issue[item.available_at].append(item)
        while time_at < end:
            stage = "forecast"
            if time_at != ACTION_START and not (
                resume and time_at == dt.datetime.fromisoformat(checkpoint["time"])
            ):
                index = inputs.index(time_at) - 1
                service.ingest(
                    InfoSet.from_raw(
                        time_at,
                        inputs.actual_info(index) + tuple(official_by_issue.get(time_at, [])),
                    )
                )
            elif resume:
                resume = False
            refresh = time_at.time() in tuple(dt.time(hour) for hour in (0, 6, 12, 18))
            if refresh:
                forecast = service.refresh(InfoSet.from_raw(time_at, ()))
                append_json(run_dir / "forecasts.jsonl", forecast)
            current = forecast.sliced(time_at)
            stage = "dispatch"
            plan = (
                reuse_tail(previous_plan, state, current, ledger)
                if previous_plan is not None and not refresh
                else None
            )
            if plan is None:
                plan = solve_dispatch(state, current, ledger)
                solve_count += 1
            else:
                reused_count += 1
            plan.solver_record.update(
                {
                    "intent": jsonable(plan.intention()),
                    "forecast_sha256": hashlib.sha256(
                        json.dumps(jsonable(current), sort_keys=True).encode()
                    ).hexdigest(),
                }
            )
            if not plan.solver_record["reused_tail"]:
                append_json(run_dir / "dispatch_plans.jsonl", plan)
            append_json(run_dir / "solver_records.jsonl", plan.solver_record)
            stage = "contract_event"
            version = ledger.submit(time_at, current.slots, tuple(flow[0] for flow in plan.flows))
            if version:
                append_json(run_dir / "contracts.jsonl", version)
            stage = "execution"
            index = inputs.index(time_at)
            execution = apply_feedback(
                plan.intention(),
                state,
                ledger.committed(time_at),
                Measurement(inputs.load_kw[index] / 6, inputs.pv_kw[index] / 6),
            )
            if execution.status != "success":
                append_json(
                    run_dir / "execution_feedback.jsonl",
                    {"slot_start": time_at, "execution": execution},
                )
                raise Q4Error("execution_infeasible", ";".join(execution.reasons))
            interval = IntervalResult(
                time_at.date(),
                (time_at.hour * 60 + time_at.minute) // 10,
                inputs.load_kw[index],
                inputs.pv_kw[index],
                ledger.committed(time_at, initial=True),
                ledger.committed(time_at),
                execution.emergency_kwh,
                execution.action,
                state,
                execution.state_end,
                "data/raw/附件2.xlsx",
                execution.pv_used_kwh,
            )
            append_json(
                run_dir / "execution_feedback.jsonl",
                {
                    "slot_start": time_at,
                    "execution": execution,
                    "interval": interval_to_dict(interval),
                },
            )
            stage = "settlement"
            bill = ledger.settle(
                time_at,
                available_at=time_at + STEP,
                actual_price=inputs.prices[index],
                emergency_kwh=execution.emergency_kwh,
            )
            append_json(run_dir / "cost_ledger.jsonl", bill)
            intervals.append(interval)
            executions.append(execution)
            state = execution.state_end
            protection_count += bool(execution.reasons)
            previous_plan = plan
            time_at += STEP
            step_count += 1
            stage = "actual_terminal_reserve"
            if time_at >= RESERVE_START and state.energy_kwh < 6000 - 1e-6:
                raise Q4Error(
                    "terminal_reserve_infeasible",
                    f"actual E({time_at})={state.energy_kwh} below 6000 kWh",
                )
            if step_count % 36 == 0 or time_at == end:
                # Advance the causal history before checkpointing at this boundary.
                service.ingest(
                    InfoSet.from_raw(
                        time_at,
                        inputs.actual_info(inputs.index(time_at) - 1)
                        + tuple(official_by_issue.get(time_at, [])),
                    )
                    if time_at < YEAR_END
                    else InfoSet.from_raw(time_at, inputs.actual_info(52559))
                )
                log_sizes, log_sha256 = checkpoint_log_hashes(run_dir, log_hash_cache)
                atomic_json(
                    run_dir / "checkpoint.json",
                    {
                        "schema_version": 2,
                        "source_hash": code_hash,
                        "source_hashes": inputs.source_hashes,
                        "config": config,
                        "time": time_at,
                        "energy_kwh": state.energy_kwh,
                        "ledger": {
                            "case_id": ledger.case_id,
                            "versions": ledger.versions,
                            "bills": ledger.bills,
                        },
                        "training_state": service.training_state(),
                        "forecast": forecast,
                        "previous_plan": previous_plan,
                        "counts": [solve_count, reused_count, protection_count],
                        "log_sizes": log_sizes,
                        "log_sha256": log_sha256,
                    },
                    indent=None,
                )
                # The next loop must not ingest this boundary a second time.
                resume = True
                checkpoint = {"time": str(time_at)}
            if step_count % 144 == 0:
                print(
                    f"{context.case_id}/{method}: {time_at.date()} steps={step_count} solves={solve_count} reused={reused_count} E={state.energy_kwh:.6f}",
                    flush=True,
                )
        stage = "independent_validation"
        validation = validate_q4_run(
            tuple(intervals),
            tuple(executions),
            ledger,
            end_time=end,
            actual_inputs=inputs,
            reserve_start=RESERVE_START,
        )
        if not context.is_synthetic and any(
            sha256_file(context.repo_root / path) != digest
            for path, digest in inputs.source_hashes.items()
        ):
            raise Q4Error("source_changed", "actual input changed during replay")
        atomic_json(run_dir / "validation.json", validation)
        if not validation["ok"]:
            status = (
                "terminal_infeasible"
                if "terminal_infeasible" in validation["violations"]
                else "validation_failed"
            )
            raise Q4Error(status, "; ".join(validation["violations"][:8]))
        status = "success" if end == YEAR_END and method == "main" else "diagnostic_success"
        result = CaseResult(
            context.case_id, run_id, status, context.is_synthetic, intervals=tuple(intervals)
        )
        save_case_result(run_dir / "domain_result.json", result, overwrite=True)
        daily = []
        for offset in range(0, len(intervals), 144):
            rows = intervals[offset : offset + 144]
            day = rows[0].day
            bills = [bill for slot, bill in ledger.bills.items() if slot.date() == day]
            record = {
                "day": str(day),
                "planned_kwh": sum(r.planned_purchase_kwh for r in rows),
                "final_contract_kwh": sum(ledger.versions[day][-1].quantities),
                "emergency_kwh": sum(r.emergency_purchase_kwh for r in rows),
                "charge_kwh": sum(r.action.charge_kwh for r in rows),
                "discharge_kwh": sum(r.action.discharge_kwh for r in rows),
                "initial_energy_kwh": rows[0].state_start.energy_kwh,
                "terminal_energy_kwh": rows[-1].state_end.energy_kwh,
                "planned_cost_cny": sum(b.planned_cost_cny for b in bills),
                "adjustment_cost_cny": sum(b.adjustment_cost_cny for b in bills),
                "emergency_cost_cny": sum(b.emergency_cost_cny for b in bills),
            }
            record["total_cost_cny"] = (
                record["planned_cost_cny"]
                + record["adjustment_cost_cny"]
                + record["emergency_cost_cny"]
            )
            daily.append(record)
        with (run_dir / "daily_summary.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(daily[0]))
            writer.writeheader()
            writer.writerows(daily)
        summary = {
            "schema_version": 1,
            "case_id": context.case_id,
            "run_id": run_id,
            "status": status,
            "full_annual": end == YEAR_END,
            "price_method": method,
            "interval_count": len(intervals),
            "initial_energy_kwh": 6000,
            "terminal_energy_kwh": state.energy_kwh,
            "costs": asdict(ledger.costs()),
            "total_cost_cny": ledger.costs().total_cost_cny,
            "emergency_kwh": sum(r.emergency_kwh for r in executions),
            "emergency_intervals": sum(r.emergency_kwh > 1e-6 for r in executions),
            "emergency_days": sum(row["emergency_kwh"] > 1e-6 for row in daily),
            "pv_curtailed_kwh": sum(r.curtailed_pv_kwh for r in executions),
            "unused_contract_kwh": sum(r.unused_grid_kwh for r in executions),
            "actual_external_kwh": sum(r.grid_used_kwh + r.emergency_kwh for r in executions),
            "planned_purchase_kwh": sum(i.planned_purchase_kwh for i in intervals),
            "final_contract_kwh": sum(sum(v[-1].quantities) for v in ledger.versions.values()),
            "battery_throughput_kwh": sum(
                r.action.charge_kwh + r.action.discharge_kwh for r in executions
            ),
            "protection_count": protection_count,
            "solve_count": solve_count,
            "reused_tail_count": reused_count,
            "elapsed_seconds": time.perf_counter() - started,
            "prediction_metrics": _metrics(run_dir, inputs, end),
            "prediction_sampling": "one error per issue/target; 24h windows overlap, clipped to realized diagnostic/annual end",
            "source_hashes": inputs.source_hashes,
            "validation_ok": True,
        }
        atomic_json(run_dir / "summary.json", summary)
        manifest.update(
            {
                "status": status,
                "validation_ok": True,
                "full_annual": end == YEAR_END,
                "actual_input_hashes": inputs.source_hashes,
                "end_time": str(end),
                "price_method": method,
            }
        )
        write_manifest(run_dir, manifest, overwrite=True)
        stage = "controller_evidence_validation"
        controller_audit = audit_controller_chain(
            run_dir, inputs, plans_path=run_dir / "dispatch_plans.jsonl"
        )
        if source_tree_hash(context.repo_root) != code_hash:
            raise Q4Error("source_changed", "Q4 source/config changed during replay or validation")
        inventory = write_inventory(
            run_dir,
            run_dir / "evidence_manifest.json",
            controller_audit,
            plans_path=run_dir / "dispatch_plans.jsonl",
        )
        manifest["model_version"] = MODEL_VERSION
        manifest["evidence_manifest_sha256"] = sha256_file(inventory)
        write_manifest(run_dir, manifest, overwrite=True)
        return result
    except Exception as exc:
        if getattr(exc, "solver_record", None):
            append_json(run_dir / "solver_records.jsonl", exc.solver_record)
        status = exc.status if isinstance(exc, Q4Error) else "failed"
        atomic_json(
            run_dir / "failure.json",
            {
                "schema_version": 1,
                "status": status,
                "stage": stage,
                "time": time_at,
                "completed_steps": step_count,
                "energy_kwh": state.energy_kwh,
                "exception_type": type(exc).__name__,
                "message": str(exc),
            },
        )
        manifest.update({"status": status, "validation_ok": False, "completed_steps": step_count})
        write_manifest(run_dir, manifest, overwrite=True)
        raise
