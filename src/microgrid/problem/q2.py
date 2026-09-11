"""Approval-gated Q2 formal runner and internal artifact lifecycle."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from ..approvals import require_approved_decisions
from ..artifacts import (
    build_manifest,
    ensure_run_id_available,
    new_run_id,
    sha256_file,
    verify_imported_inputs,
    verify_required_inputs,
    write_json,
    write_manifest,
)
from ..schemas import InputError
from .contracts import CaseContext, CaseResult
from .q2_engine import Q2EngineConfig, run_q2_engineering
from .q2_forecast import ForecastConfig
from .q2_inputs import Q2InputBundle, load_q2_inputs
from .q2_model import Q2ModelConfig
from .result_io import save_case_result
from .validation import validate_complete_run

CASE_ID = "q2"
MODEL_DECISION_ID = "D_MODEL_Q2"
Q2_DECISION_IDS = (
    "D_TIME_INTERNAL",
    "D_EFF",
    "D_STATE",
    "D_INFO",
    "D_SETTLE",
    "D_MODEL_Q2",
    "D_MPC",
    "D_TERMINAL",
    "D_YEAR_BOUNDARY",
)


def _input_path(context: CaseContext, key: str, default: Path) -> Path:
    raw = context.metadata.get(key)
    return Path(raw) if raw is not None else default


def _day(context: CaseContext, key: str, default: dt.date) -> dt.date:
    raw = context.metadata.get(key)
    if raw is None:
        return default
    if isinstance(raw, dt.date):
        return raw
    try:
        return dt.date.fromisoformat(str(raw))
    except ValueError as exc:
        raise InputError(f"{key} must be an ISO date") from exc


def _days_between(start: dt.date, end: dt.date) -> tuple[dt.date, ...]:
    return tuple(start + dt.timedelta(days=offset) for offset in range((end - start).days + 1))


def _command(context: CaseContext) -> list[str]:
    raw = context.metadata.get("command")
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    return ["python", "-m", "microgrid", "run", "--case", "q2"]


def _resolve_run_dir(context: CaseContext, run_id: str) -> Path:
    if context.output_dir is not None:
        run_dir = Path(context.output_dir)
        if run_dir.exists() and any(run_dir.iterdir()):
            raise InputError(f"synthetic output directory is not empty: {run_dir}")
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir
    return ensure_run_id_available(context.repo_root, CASE_ID, run_id)


def _write_failure_evidence(
    context: CaseContext,
    run_id: str,
    run_dir: Path,
    *,
    stage: str,
    exc: BaseException,
) -> None:
    write_json(
        run_dir / "failure.json",
        {
            "run_id": run_id,
            "case_id": CASE_ID,
            "failure_stage": stage,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "is_synthetic": context.is_synthetic,
            "model_status": "implemented",
        },
    )
    manifest = build_manifest(
        context.repo_root,
        run_id=run_id,
        case_id=CASE_ID,
        command=_command(context),
        status="failed",
        is_synthetic=context.is_synthetic,
        model_status="implemented",
        result_files={},
        result_sha256={},
        verify_inputs=False,
    )
    manifest.update(
        {
            "failure_stage": stage,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }
    )
    write_manifest(run_dir, manifest, overwrite=True)


def _bundle_to_dict(bundle: Q2InputBundle) -> dict[str, Any]:
    return {
        "input_hashes": dict(bundle.input_hashes),
        "fixed_prices": [
            {
                "slot": point.slot,
                "price_cny_per_kwh": point.price_cny_per_kwh,
                "source_ref": point.source_ref,
            }
            for point in bundle.fixed_prices
        ],
        "actuals": [
            {
                "day": item.day.isoformat(),
                "slot": item.slot,
                "start": item.start.isoformat(sep=" "),
                "end": item.end.isoformat(sep=" "),
                "load_kw": item.load_kw,
                "pv_kw": item.pv_kw,
                "load_source_ref": item.load_source_ref,
                "pv_source_ref": item.pv_source_ref,
            }
            for item in bundle.actuals
        ],
    }


def _build_engine_config(context: CaseContext, start: dt.date, end: dt.date) -> Q2EngineConfig:
    raw_weights = context.metadata.get("forecast_weights", (0.25, 0.25, 0.25, 0.25))
    weights = tuple(float(value) for value in raw_weights)
    return Q2EngineConfig(
        action_start_day=start,
        action_end_day=end,
        forecast_config=ForecastConfig(
            weights=weights,
            ar1_phi=context.metadata.get("ar1_phi", 0.0),
            model_version=str(context.metadata.get("forecast_model_version", "q2-weekly-ar1-v1")),
            allow_short_history=False,
        ),
        model_config=Q2ModelConfig(
            horizon_steps=144,
            time_limit_s=context.metadata.get("solver_time_limit_s"),
        ),
        annual_terminal_soc_kwh=context.metadata.get("annual_terminal_soc_kwh"),
        is_synthetic=context.is_synthetic,
        terminal_value_cny_per_kwh=float(context.metadata.get("terminal_value_cny_per_kwh", 0.0)),
    )


def run(context: CaseContext) -> CaseResult:
    """Execute Q2 only after all decisions are approved and inputs are explicit."""

    require_approved_decisions(context.repo_root, Q2_DECISION_IDS)
    attachment1 = _input_path(
        context, "attachment1_path", context.repo_root / "data" / "raw" / "附件1.xlsx"
    )
    attachment2 = _input_path(
        context, "attachment2_path", context.repo_root / "data" / "raw" / "附件2.xlsx"
    )
    paths = (("attachment1", attachment1), ("attachment2", attachment2))
    if not context.is_synthetic:
        for logical_name, path in paths:
            if not path.is_file():
                raise InputError(f"{logical_name} path is not a file: {path}")

    action_start = _day(context, "action_start_day", dt.date(2025, 2, 1))
    action_end = _day(context, "action_end_day", action_start)
    run_id = context.run_id or new_run_id("q2", context.repo_root)
    run_dir: Path | None = None
    stage = "run_allocate"
    try:
        run_dir = _resolve_run_dir(context, run_id)
        write_manifest(
            run_dir,
            build_manifest(
                context.repo_root,
                run_id=run_id,
                case_id=CASE_ID,
                command=_command(context),
                status="running",
                is_synthetic=context.is_synthetic,
                model_status="implemented",
                result_files={},
                result_sha256={},
                verify_inputs=False,
            ),
            overwrite=True,
        )

        stage = "input_preflight"
        for logical_name, path in paths:
            if not path.is_file():
                raise InputError(f"{logical_name} path is not a file: {path}")
        if not context.is_synthetic:
            provenance_issues = verify_imported_inputs(context.repo_root)
            provenance_issues.extend(
                verify_required_inputs(
                    context.repo_root, ("data/raw/附件1.xlsx", "data/raw/附件2.xlsx")
                )
            )
            if provenance_issues:
                raise InputError("input provenance check failed: " + "; ".join(provenance_issues))

        stage = "input_load"
        expected_days = context.metadata.get("expected_days")
        if expected_days is not None:
            expected = tuple(dt.date.fromisoformat(str(value)) for value in expected_days)
        else:
            input_start = _day(context, "input_start_day", action_start - dt.timedelta(days=28))
            expected = _days_between(input_start, action_end)
        bundle = load_q2_inputs(
            attachment1_path=attachment1,
            attachment2_path=attachment2,
            load_sheet_name=str(context.metadata.get("load_sheet_name", "实际负荷")),
            pv_sheet_name=str(context.metadata.get("pv_sheet_name", "实际光伏")),
            expected_days=expected,
        )

        stage = "solve"
        engineering = run_q2_engineering(
            bundle, _build_engine_config(context, action_start, action_end)
        )

        stage = "validation"
        complete = validate_complete_run(
            engineering.intervals,
            _days_between(action_start, action_end),
        )
        if not complete.ok:
            raise InputError("Q2 complete-run validation failed: " + "; ".join(complete.issues))
        result = CaseResult(
            case_id=CASE_ID,
            run_id=run_id,
            status="success",
            is_synthetic=context.is_synthetic,
            intervals=engineering.intervals,
            metadata={
                **engineering.metadata,
                "model": "q2_rolling_milp",
                "model_status": "implemented",
                "model_decision": MODEL_DECISION_ID,
                "is_synthetic": context.is_synthetic,
                "costs": {
                    "planned_cost_cny": engineering.costs.planned_cost_cny,
                    "adjustment_cost_cny": engineering.costs.adjustment_cost_cny,
                    "emergency_cost_cny": engineering.costs.emergency_cost_cny,
                    "total_cost_cny": engineering.costs.total_cost_cny,
                },
            },
        )

        stage = "artifact_write"
        input_snapshot = write_json(run_dir / "input_snapshot.json", _bundle_to_dict(bundle))
        domain_result = save_case_result(run_dir / "domain_result.json", result)
        validation_path = write_json(
            run_dir / "validation.json",
            {
                "ok": complete.ok,
                "issues": list(complete.issues),
                "checked_intervals": complete.checked_intervals,
                "max_abs_violation_kwh": complete.max_abs_violation_kwh,
            },
        )
        summary = write_json(
            run_dir / "summary.json",
            {
                "case_id": CASE_ID,
                "run_id": run_id,
                "is_synthetic": context.is_synthetic,
                "interval_count": len(result.intervals),
                "costs": result.metadata["costs"],
                "solver_records": len(engineering.solver_records),
                "validation_ok": complete.ok,
            },
        )
        solver_log = run_dir / "solver.log"
        solver_log.write_text(
            "\n".join(
                [
                    "solver=scipy.optimize.milp (HiGHS)",
                    *(
                        f"decision={record['decision_time']} status={record['status']}"
                        for record in engineering.solver_records
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        result_paths = {
            path.name: str(path.relative_to(run_dir))
            for path in (input_snapshot, domain_result, validation_path, summary, solver_log)
        }
        result_hashes = {
            name: sha256_file(run_dir / relative_path)
            for name, relative_path in result_paths.items()
        }
        manifest = build_manifest(
            context.repo_root,
            run_id=run_id,
            case_id=CASE_ID,
            command=_command(context),
            status="success",
            is_synthetic=context.is_synthetic,
            model_status="implemented",
            result_files=result_paths,
            result_sha256=result_hashes,
            verify_inputs=not context.is_synthetic,
        )
        manifest["validation_ok"] = complete.ok
        manifest["action_start_day"] = action_start.isoformat()
        manifest["action_end_day"] = action_end.isoformat()
        write_manifest(run_dir, manifest, overwrite=True)
        return result
    except Exception as exc:
        if run_dir is not None:
            _write_failure_evidence(context, run_id, run_dir, stage=stage, exc=exc)
        raise
