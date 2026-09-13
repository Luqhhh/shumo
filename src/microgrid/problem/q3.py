"""Formal Q3 causal rolling-MILP runner over official attachments 1/2/3."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..approvals import require_approved_decisions
from ..artifacts import (
    build_manifest,
    ensure_run_id_available,
    file_digest,
    new_run_id,
    verify_imported_inputs,
    verify_required_inputs,
    write_json,
    write_manifest,
)
from ..dataio import ensure_dir
from ..schemas import InputError
from .contracts import INITIAL_SOC_KWH, BatteryState, CaseContext, CaseResult
from .q3_artifacts import Q3DayArtifacts, write_q3_period_artifacts
from .q3_release_snapshots import build_q3_snapshot_catalog
from .q3_rolling import run_q3_period
from .q3_runtime_inputs import (
    Q3_EVALUATION_END,
    Q3_EVALUATION_START,
    Q3_STATE_END,
    inclusive_days,
    load_q3_runtime_inputs,
    release_times,
)
from .q3_window_factory import Q3SnapshotWindowFactory

CASE_ID = "q3"
MODEL_DECISION_ID = "D_MODEL_Q3"
REQUIRED_DECISIONS = (
    "D_TIME_INTERNAL",
    "D_EFF",
    "D_STATE",
    "D_INFO",
    "D_LOAD_FORECAST",
    "D_PV_TAIL_BASELINE",
    "D_RESAMPLE",
    "D_SETTLE",
    "D_MODEL_Q3",
)
REQUIRED_INPUT_PATHS = (
    "data/raw/附件1.xlsx",
    "data/raw/附件2.xlsx",
    "data/raw/附件3.xlsx",
)


def _command(context: CaseContext) -> list[str]:
    raw = context.metadata.get("command")
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    return ["python", "-m", "microgrid", "run", "--case", CASE_ID]


def _resolve_run_dir(context: CaseContext, run_id: str) -> Path:
    if context.output_dir is not None:
        run_dir = Path(context.output_dir)
        if run_dir.exists() and any(run_dir.iterdir()):
            raise InputError(f"Q3 output directory is not empty: {run_dir}")
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
    manifest["failure_stage"] = stage
    manifest["error_type"] = type(exc).__name__
    manifest["error_message"] = str(exc)
    write_manifest(run_dir, manifest, overwrite=True)


def _result_file_maps(
    repo_root: Path,
    artifacts: Q3DayArtifacts,
) -> tuple[dict[str, str], dict[str, str]]:
    files: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for path in artifacts.paths:
        try:
            relative = path.relative_to(repo_root).as_posix()
        except ValueError:
            relative = str(path)
        files[path.name] = relative
        hashes[path.name] = file_digest(path)
    return files, hashes


def _summary(
    result: CaseResult,
    *,
    snapshot_count: int,
    tail_forecast_count: int,
) -> dict[str, Any]:
    return {
        "case_id": result.case_id,
        "run_id": result.run_id,
        "status": result.status,
        "is_synthetic": result.is_synthetic,
        "model_status": "implemented",
        "evaluation_start": Q3_EVALUATION_START.isoformat(),
        "evaluation_end": Q3_EVALUATION_END.isoformat(),
        "interval_count": len(result.intervals),
        "release_snapshot_count": snapshot_count,
        "tail_forecast_call_count": tail_forecast_count,
        "state_start_kwh": result.metadata["state_start_kwh"],
        "state_end_kwh": result.metadata["state_end_kwh"],
        "planned_cost_cny": result.metadata["planned_cost_cny"],
        "adjustment_cost_cny": result.metadata["adjustment_cost_cny"],
        "emergency_cost_cny": result.metadata["emergency_cost_cny"],
        "total_cost_cny": result.metadata["total_cost_cny"],
    }


def run(context: CaseContext) -> CaseResult:
    """Run the complete approved February--December Q3 evaluation period."""

    require_approved_decisions(context.repo_root, REQUIRED_DECISIONS)
    run_id = context.run_id or new_run_id(CASE_ID, context.repo_root)
    run_dir: Path | None = None
    stage = "run_allocate"
    try:
        run_dir = _resolve_run_dir(context, run_id)
        ensure_dir(run_dir)
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

        stage = "provenance"
        provenance_issues = verify_imported_inputs(context.repo_root)
        provenance_issues.extend(verify_required_inputs(context.repo_root, REQUIRED_INPUT_PATHS))
        if provenance_issues:
            raise InputError("input provenance check failed: " + "; ".join(provenance_issues))

        stage = "input_load"
        runtime = load_q3_runtime_inputs(
            attachment1_path=context.repo_root / REQUIRED_INPUT_PATHS[0],
            attachment2_path=context.repo_root / REQUIRED_INPUT_PATHS[1],
            attachment3_path=context.repo_root / REQUIRED_INPUT_PATHS[2],
        )
        evaluation_days = inclusive_days(Q3_EVALUATION_START, Q3_EVALUATION_END)

        stage = "forecast_snapshots"
        catalog = build_q3_snapshot_catalog(
            issue_times=release_times(evaluation_days),
            raw_info_items=runtime.raw_info_items,
            load_data_version=dict(runtime.input_hashes)["attachment2"],
        )
        factory = Q3SnapshotWindowFactory(
            load_snapshots=tuple(item.load for item in catalog),
            pv_snapshots=tuple(item.pv for item in catalog),
            fixed_prices=runtime.fixed_prices,
            raw_info_items=runtime.raw_info_items,
            evaluation_end=Q3_STATE_END,
        )

        stage = "rolling_solve"
        period = run_q3_period(
            days=evaluation_days,
            state_start=BatteryState(INITIAL_SOC_KWH),
            actuals=runtime.actuals_for(evaluation_days),
            window_factory=factory,
        )
        if not math.isclose(
            period.state_end.energy_kwh,
            INITIAL_SOC_KWH,
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            raise InputError("Q3 annual result does not satisfy the approved 6000 kWh boundary")

        stage = "artifact_write"
        artifacts = write_q3_period_artifacts(
            run_dir,
            run_id=run_id,
            period_run=period,
            load_snapshots=factory.used_load_snapshots,
            pv_snapshots=factory.used_pv_snapshots,
            tail_forecasts=factory.used_tail_forecasts,
            is_synthetic=context.is_synthetic,
        )
        write_json(
            run_dir / "summary.json",
            _summary(
                artifacts.result,
                snapshot_count=len(factory.used_pv_snapshots),
                tail_forecast_count=len(factory.used_tail_forecasts),
            ),
        )
        result_files, result_hashes = _result_file_maps(context.repo_root, artifacts)
        manifest = build_manifest(
            context.repo_root,
            run_id=run_id,
            case_id=CASE_ID,
            command=_command(context),
            status="success",
            is_synthetic=context.is_synthetic,
            model_status="implemented",
            result_files=result_files,
            result_sha256=result_hashes,
            verify_inputs=True,
        )
        manifest["validation_ok"] = True
        manifest["evaluation_start"] = Q3_EVALUATION_START.isoformat()
        manifest["evaluation_end"] = Q3_EVALUATION_END.isoformat()
        write_manifest(run_dir, manifest, overwrite=True)
        return artifacts.result
    except Exception as exc:
        if run_dir is not None:
            _write_failure_evidence(context, run_id, run_dir, stage=stage, exc=exc)
        raise
