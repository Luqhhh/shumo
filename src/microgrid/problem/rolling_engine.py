"""One continuous causal replay for either approved Q4 case."""

from __future__ import annotations

import csv
import datetime as dt
import json
import shutil
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

import numpy as np

from ..approvals import require_approved_decisions
from ..artifacts import (
    _source_paths,
    build_manifest,
    ensure_run_id_available,
    new_run_id,
    source_tree_hash,
    write_manifest,
)
from ..dataio import sha256_file, utc_now
from ..schemas import InputError
from .contracts import BatteryAction, BatteryState, CaseContext, CaseResult, InfoSet, IntervalResult
from .dispatch_feedback import ExecutionRecord, Measurement, apply_feedback
from .dispatch_milp import DispatchPlan, reuse_tail, solve_dispatch
from .purchase_ledger import PurchaseLedger
from .q4_checkpoint import CHECKPOINT_SCHEMA, ledger_witness, restore_ledger
from .q4_common import (
    ACTION_START,
    BIAS_MODEL_VERSION,
    BLEND_DECISIONS,
    BLEND_MODEL_VERSION,
    MODEL_VERSION,
    RESERVE_START,
    STEP,
    YEAR_END,
    JsonlWriter,
    Q4Error,
    append_json,
    atomic_json,
    jsonable,
    pv_bias_parameters,
    pv_blend_parameters,
)
from .q4_evidence import (
    LOG_NAMES,
    audit_controller_chain,
    checkpoint_log_hashes,
    forecast_view,
    full_snapshot_sha256,
    prefix_sha256,
    write_inventory,
)
from .q4_forecasts import ForecastSnapshot, Q4Forecaster
from .q4_inputs import Q4Inputs, load_q4_inputs
from .q4_performance import Performance
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


def _restore_blend_forecaster(inputs, time_at, run_dir, committed_size, saved_forecast):
    """Replay only the committed forecast prefix and ended history at recovery."""
    service = _initialize_forecaster(
        inputs, "q4_3", "main", ACTION_START, model_version=saved_forecast["model_version"]
    )
    official = defaultdict(list)
    for item in inputs.official:
        official[item.available_at].append(item)
    with (run_dir / "forecasts.jsonl").open("rb") as stream:

        def next_committed():
            if stream.tell() == committed_size:
                return None
            line = stream.readline()
            if not line or stream.tell() > committed_size or not line.endswith(b"\n"):
                raise InputError("checkpoint forecast prefix boundary invalid")
            return json.loads(line)

        slot, latest = ACTION_START, None
        while slot <= time_at:
            if slot != ACTION_START:
                service.ingest(
                    InfoSet.from_raw(
                        slot,
                        inputs.actual_info((inputs.index(slot) if slot < YEAR_END else 52560) - 1)
                        + tuple(official.get(slot, [])),
                    )
                )
            if slot < time_at and slot.time() in tuple(dt.time(h) for h in (0, 6, 12, 18)):
                expected = jsonable(service.refresh(InfoSet.from_raw(slot, ())))
                if next_committed() != expected:
                    raise InputError("checkpoint PV forecast differs from causal prefix replay")
                latest = expected
            slot += STEP
        if next_committed() is not None:
            raise InputError("checkpoint has extra committed forecast snapshots")
        if saved_forecast != latest:
            raise InputError("checkpoint frozen forecast differs from committed causal snapshot")
    return service


def _execution_from_dict(data):
    data = dict(data)
    for key in ("intent", "action"):
        data[key] = BatteryAction(**data[key])
    for key in ("state_start", "state_end"):
        data[key] = BatteryState(**data[key])
    data["reasons"] = tuple(data["reasons"])
    return ExecutionRecord(**data)


def _seal_source_snapshot(repo, run_dir, source_hash, manifest):
    target = run_dir / "source_snapshot"
    target.mkdir()
    files = {}
    for path in _source_paths(repo):
        name = path.relative_to(repo)
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        files[str(name)] = sha256_file(destination)
    if source_tree_hash(target) != source_hash:
        raise Q4Error("source_changed", "source changed while sealing new model snapshot")
    atomic_json(
        run_dir / "source_snapshot_manifest.json",
        {
            "source_hash": source_hash,
            "captured_at_utc": utc_now(),
            "code_commit": manifest["code_commit"],
            "code_dirty": manifest["code_dirty"],
            "files": files,
            "method": "runner copy of code/config only before simulation",
        },
    )


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
    performance = Performance()
    initial_resume = bool(context.metadata.get("resume", False))
    writer = None

    def write_log(path, value):
        if writer is not None and not writer.closed:
            writer.write(path.stem, value)
        else:
            with performance.measure("log_serialization_and_write"):
                append_json(path, value)

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
    pv_method = context.metadata.get("pv_method", "v3")
    if pv_method not in ("v3", "blend-long", "bias-long"):
        raise InputError("Q4 pv_method must be v3, blend-long or bias-long")
    model_version = MODEL_VERSION
    if pv_method == "blend-long":
        if context.case_id != "q4_3" or method != "main":
            raise InputError("PV-BLEND-LONG is approved only for Q4-3/main")
        require_approved_decisions(context.repo_root, BLEND_DECISIONS)
        model_version = BLEND_MODEL_VERSION
    elif pv_method == "bias-long":
        if context.case_id != "q4_3" or method != "main":
            raise InputError("PV-BIAS-LONG trial is scoped to Q4-3/main")
        require_approved_decisions(context.repo_root, ("D_OPTIMIZATION_Q4",))
        model_version = BIAS_MODEL_VERSION
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
        "evidence_schema_version": 2,
        "model_version": model_version,
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
    if model_version == BLEND_MODEL_VERSION:
        config["pv_blend"] = pv_blend_parameters()
    elif model_version == BIAS_MODEL_VERSION:
        config["optimization"] = pv_bias_parameters()
    started = time.perf_counter()
    stage = "input_preflight"
    step_count = 0
    prior_step_count = 0
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
        with performance.measure("input_preflight"):
            inputs = load_q4_inputs(context.repo_root, context.case_id)
        if not resume:
            if model_version in (BLEND_MODEL_VERSION, BIAS_MODEL_VERSION):
                _seal_source_snapshot(context.repo_root, run_dir, code_hash, manifest)
            write_manifest(run_dir, manifest)
            atomic_json(run_dir / "input_snapshot.json", inputs.snapshot)
            atomic_json(run_dir / "effective_config.json", config)
            for name in LOG_NAMES:
                (run_dir / f"{name}.jsonl").touch()
            with performance.measure("forecast_ingest"):
                service = _initialize_forecaster(
                    inputs, context.case_id, method, ACTION_START, model_version=model_version
                )
        else:
            checkpoint = json.loads((run_dir / "checkpoint.json").read_text())
            if (
                checkpoint.get("schema_version") != CHECKPOINT_SCHEMA
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
            with performance.measure("forecast_ingest"):
                service = (
                    _restore_blend_forecaster(
                        inputs,
                        time_at,
                        run_dir,
                        checkpoint["log_sizes"]["forecasts"],
                        checkpoint["forecast"],
                    )
                    if model_version in (BLEND_MODEL_VERSION, BIAS_MODEL_VERSION)
                    else _initialize_forecaster(inputs, context.case_id, method, time_at)
                )
            if jsonable(service.training_state()) != checkpoint["training_state"]:
                raise InputError("checkpoint training state mismatch")
            for name, size in checkpoint["log_sizes"].items():
                with (run_dir / f"{name}.jsonl").open("r+b") as stream:
                    stream.truncate(size)
            with performance.measure("checkpoint_restore"):
                ledger = restore_ledger(
                    run_dir, context.case_id, time_at, checkpoint["ledger_state"]
                )
            state = BatteryState(checkpoint["energy_kwh"])
            forecast = _snapshot_from_dict(checkpoint["forecast"])
            with performance.measure("forecast_evidence_hash"):
                forecast_digest = full_snapshot_sha256(forecast)
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
            if time_at != ACTION_START + step_count * STEP or len(ledger.bills) != step_count:
                raise InputError("checkpoint execution coverage mismatch")
            prior_step_count = step_count
            solve_count, reused_count, protection_count = checkpoint["counts"]
            if solve_count + reused_count != step_count or protection_count != sum(
                bool(record.reasons) for record in executions
            ):
                raise InputError("checkpoint controller counts mismatch")
            write_manifest(run_dir, manifest, overwrite=True)
        writer = JsonlWriter(run_dir, LOG_NAMES, performance=performance)
        official_by_issue = defaultdict(list)
        for item in inputs.official:
            official_by_issue[item.available_at].append(item)
        while time_at < end:
            stage = "forecast"
            if time_at != ACTION_START and not (
                resume and time_at == dt.datetime.fromisoformat(checkpoint["time"])
            ):
                index = inputs.index(time_at) - 1
                with performance.measure("forecast_ingest"):
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
                with performance.measure("forecast_refresh"):
                    forecast = service.refresh(InfoSet.from_raw(time_at, ()))
                write_log(run_dir / "forecasts.jsonl", forecast)
                with performance.measure("forecast_evidence_hash"):
                    forecast_digest = full_snapshot_sha256(forecast)
            with performance.measure("window_slice"):
                current = forecast.sliced(time_at)
            stage = "dispatch"
            plan = None
            if previous_plan is not None and not refresh:
                with performance.measure("tail_reuse_validation"):
                    plan = reuse_tail(
                        previous_plan, state, current, ledger, diagnostics=performance.counters
                    )
            else:
                performance.counters["tail_reuse_skipped:refresh_or_no_parent"] += 1
            if plan is None:
                plan = solve_dispatch(state, current, ledger, performance=performance)
                performance.counters["fresh_solves"] += 1
                solve_count += 1
            else:
                reused_count += 1
                performance.counters["reused_tails"] += 1
            plan.solver_record.update(
                {
                    "intent": jsonable(plan.intention()),
                    "forecast_view": forecast_view(forecast, time_at, forecast_digest),
                }
            )
            if not plan.solver_record["reused_tail"]:
                write_log(run_dir / "dispatch_plans.jsonl", plan)
            write_log(run_dir / "solver_records.jsonl", plan.solver_record)
            stage = "contract_event"
            with performance.measure("feedback_and_settlement"):
                version = ledger.submit(
                    time_at, current.slots, tuple(flow[0] for flow in plan.flows)
                )
            if version:
                write_log(run_dir / "contracts.jsonl", version)
            stage = "execution"
            index = inputs.index(time_at)
            with performance.measure("feedback_and_settlement"):
                execution = apply_feedback(
                    plan.intention(),
                    state,
                    ledger.committed(time_at),
                    Measurement(inputs.load_kw[index] / 6, inputs.pv_kw[index] / 6),
                )
            if execution.status != "success":
                write_log(
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
            write_log(
                run_dir / "execution_feedback.jsonl",
                {
                    "slot_start": time_at,
                    "execution": execution,
                    "interval": interval_to_dict(interval),
                },
            )
            stage = "settlement"
            with performance.measure("feedback_and_settlement"):
                bill = ledger.settle(
                    time_at,
                    available_at=time_at + STEP,
                    actual_price=inputs.prices[index],
                    emergency_kwh=execution.emergency_kwh,
                )
            write_log(run_dir / "cost_ledger.jsonl", bill)
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
                with performance.measure("checkpoint"):
                    # Advance the causal history before checkpointing at this boundary.
                    with performance.measure("forecast_ingest"):
                        service.ingest(
                            InfoSet.from_raw(
                                time_at,
                                inputs.actual_info(inputs.index(time_at) - 1)
                                + tuple(official_by_issue.get(time_at, [])),
                            )
                            if time_at < YEAR_END
                            else InfoSet.from_raw(time_at, inputs.actual_info(52559))
                        )
                    writer.flush()
                    log_sizes, log_sha256 = checkpoint_log_hashes(run_dir, log_hash_cache)
                    atomic_json(
                        run_dir / "checkpoint.json",
                        {
                            "schema_version": CHECKPOINT_SCHEMA,
                            "source_hash": code_hash,
                            "source_hashes": inputs.source_hashes,
                            "config": config,
                            "time": time_at,
                            "energy_kwh": state.energy_kwh,
                            "ledger_state": ledger_witness(ledger),
                            "training_state": service.training_state(),
                            "forecast": forecast,
                            "previous_plan": previous_plan,
                            "counts": [solve_count, reused_count, protection_count],
                            "log_sizes": log_sizes,
                            "log_sha256": log_sha256,
                        },
                        indent=None,
                    )
                performance.counters["checkpoint_writes"] += 1
                performance.counters["checkpoint_max_size_bytes"] = max(
                    performance.counters["checkpoint_max_size_bytes"],
                    (run_dir / "checkpoint.json").stat().st_size,
                )
                # The next loop must not ingest this boundary a second time.
                resume = True
                checkpoint = {"time": str(time_at)}
            if step_count % 144 == 0:
                print(
                    f"{context.case_id}/{method}: {time_at.date()} steps={step_count} solves={solve_count} reused={reused_count} E={state.energy_kwh:.6f}",
                    flush=True,
                )
        writer.close()
        performance.switch_phase("validation")
        stage = "independent_validation"
        with performance.measure("physical_validation"):
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
        performance.switch_phase("report")
        report_started = time.perf_counter()
        save_case_result(run_dir / "domain_result.json", result, overwrite=True)
        daily = []
        for offset in range(0, len(intervals), 144):
            rows = intervals[offset : offset + 144]
            day = rows[0].day
            day_start = dt.datetime.combine(day, dt.time())
            bills = [ledger.bills[day_start + k * STEP] for k in range(len(rows))]
            record = {
                "day": str(day),
                "planned_kwh": sum(r.planned_purchase_kwh for r in rows),
                "final_contract_kwh": sum(ledger.versions[day][-1].quantities),
                "emergency_kwh": sum(r.emergency_purchase_kwh for r in rows),
                "charge_kwh": sum(r.action.charge_kwh for r in rows),
                "discharge_kwh": sum(r.action.discharge_kwh for r in rows),
                "initial_energy_kwh": 6000
                if offset == 0 and rows[0].state_start.energy_kwh == 6000
                else rows[0].state_start.energy_kwh,
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
            csv_writer = csv.DictWriter(stream, fieldnames=list(daily[0]))
            csv_writer.writeheader()
            csv_writer.writerows(daily)
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
            "elapsed_seconds_scope": "legacy: before prediction metrics and controller audit; use performance_summary.json for full run timing",
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
        performance.record("metrics_and_report", time.perf_counter() - report_started)
        performance.switch_phase("validation")
        stage = "controller_evidence_validation"
        with performance.measure("controller_chain_audit"):
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
        manifest["model_version"] = model_version
        manifest["evidence_manifest_sha256"] = sha256_file(inventory)
        write_manifest(run_dir, manifest, overwrite=True)
        timing = performance.summary()
        timing.update(
            {
                "case_id": context.case_id,
                "run_id": run_id,
                "status": status,
                "resumed_attempt": initial_resume,
                "completed_steps": step_count,
                "initial_completed_steps": prior_step_count,
                "source_hash": code_hash,
            }
        )
        timing["log_sizes_bytes"] = {
            name: (run_dir / f"{name}.jsonl").stat().st_size for name in LOG_NAMES
        }
        timing["checkpoint_size_bytes"] = (run_dir / "checkpoint.json").stat().st_size
        timing["export_included"] = False
        atomic_json(run_dir / "performance_summary.json", timing)
        return result
    except Exception as exc:
        if getattr(exc, "solver_record", None):
            write_log(run_dir / "solver_records.jsonl", exc.solver_record)
        if writer is not None:
            try:
                writer.close()
            except OSError as persistence_error:
                exc = persistence_error
                stage = "log_persistence"
        status = exc.status if isinstance(exc, Q4Error) else "failed"
        atomic_json(
            run_dir / "failure.json",
            {
                "schema_version": 1,
                "status": status,
                "stage": stage,
                "time": time_at,
                "completed_steps": step_count,
                "initial_completed_steps": prior_step_count,
                "energy_kwh": state.energy_kwh,
                "exception_type": type(exc).__name__,
                "message": str(exc),
            },
        )
        manifest.update({"status": status, "validation_ok": False, "completed_steps": step_count})
        write_manifest(run_dir, manifest, overwrite=True)
        timing = performance.summary()
        timing.update(
            {
                "case_id": context.case_id,
                "run_id": run_id,
                "status": status,
                "resumed_attempt": initial_resume,
                "completed_steps": step_count,
                "initial_completed_steps": prior_step_count,
                "source_hash": code_hash,
                "export_included": False,
            }
        )
        atomic_json(run_dir / "performance_summary.json", timing)
        raise
    finally:
        if writer is not None:
            writer.close()
