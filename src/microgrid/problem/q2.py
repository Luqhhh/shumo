"""Approval-gated Q2 formal runner and internal artifact lifecycle."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..approvals import require_approved_decisions
from ..artifacts import (
    build_manifest,
    ensure_run_id_available,
    new_run_id,
    sha256_file,
    verify_imported_inputs,
    verify_loaded_input_paths,
    verify_required_inputs,
    write_json,
    write_manifest,
)
from ..dataio import ensure_dir
from ..schemas import InputError
from .contracts import CaseContext, CaseResult
from .q2_engine import Q2EngineConfig, run_q2_engineering
from .q2_forecast import ForecastConfig
from .q2_inputs import Q2InputBundle, load_q2_inputs
from .q2_model import Q2ModelConfig
from .result_io import save_case_result

Q2_RUN_CONFIG_RELATIVE_PATH = Path("configs") / "q2_run.toml"

# Calibrated forecast configuration for the annual Q2 run: weekly load lags with
# a residual bias correction, and short daily PV lags.  These are only the
# defaults -- `configs/q2_run.toml` overrides them and `context.metadata`
# overrides both, so `effective_config.json` records what actually ran.
_Q2_SETTING_DEFAULTS: dict[str, Any] = {
    "forecast_weights": (0.25, 0.25, 0.25, 0.25),
    "forecast_lags": (7, 14, 21, 28),
    "pv_lags": (1, 2, 3, 4, 5),
    "pv_weights": (0.35, 0.25, 0.2, 0.12, 0.08),
    "load_bias_window": 4,
    "load_bias_scale": 0.8,
    "purchase_margin": 0.0,
}


def _load_q2_run_settings(repo_root: Path) -> dict[str, Any]:
    """Read the optional per-run Q2 settings file.

    The file is absent by default, in which case the calibrated in-code
    defaults above apply.  A run that needs a different purchase margin writes
    it there rather than editing this module.
    """
    path = Path(repo_root) / Q2_RUN_CONFIG_RELATIVE_PATH
    if not path.is_file():
        return {}
    import tomllib

    with path.open("rb") as handle:
        data = tomllib.load(handle)
    section = data.get("q2", data)
    return dict(section) if isinstance(section, dict) else {}
from .validation import validate_complete_run

CASE_ID = "q2"


def _accounting_dict(engineering: Any) -> dict[str, Any]:
    """Serialise the Q2 emergency/PV accounting block.

    ``e_plan_kwh`` and ``e_realized_kwh`` are reported side by side so a reader
    of the artifact can never mistake the MILP's planning recourse for the
    quantity that was actually billed. ``pv_accounting_policy`` records the
    attribution rule that produced ``pv_used_kwh`` / ``pv_curtail_kwh``: under
    ``grid_and_discharge_first_residual_pv`` those are rule artifacts and must
    not be quoted as PV utilization or as true curtailment.
    """

    accounting = engineering.accounting
    return {
        "e_plan_kwh": accounting.e_plan_kwh,
        "e_realized_kwh": accounting.e_realized_kwh,
        "emergency_cost_cny": accounting.emergency_cost_cny,
        "pv_available_kwh": accounting.pv_available_kwh,
        "pv_used_kwh": accounting.pv_used_kwh,
        "pv_curtail_kwh": accounting.pv_curtail_kwh,
        "max_pv_partition_residual_kwh": accounting.max_pv_partition_residual_kwh,
        "max_abs_ledger_residual_kwh": accounting.max_abs_ledger_residual_kwh,
        "pv_accounting_policy": accounting.pv_accounting_policy,
    }
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


def _write_json_records(path: Path, records: Iterable[dict[str, Any]]) -> Path:
    """Write a JSON array of uniform records one element at a time.

    ``write_json`` materialises the whole list and then encodes it to a single
    string. That is fine for the small summaries, but ``forecast_records.json``
    holds 6.9M records, so the dict list and the encoded string are both several
    GB on top of the forecast points the engine is already holding -- the first
    annual run was OOM-killed there after its solve had completed.

    Encoding one record at a time to an open handle keeps a single record alive
    instead of two full copies of the array. The bytes are identical to
    ``json.dumps(records, ensure_ascii=False, indent=2) + "\\n"`` because the
    encoder is deterministic and the nesting depth only shifts the indent
    prefix, which is reapplied here.
    """

    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        empty = True
        for record in records:
            handle.write("[\n" if empty else ",\n")
            empty = False
            text = json.dumps(record, ensure_ascii=False, indent=2)
            handle.write("\n".join("  " + line if line else line for line in text.split("\n")))
        handle.write("[]\n" if empty else "\n]\n")
    return path


def _build_engine_config(
    context: CaseContext, start: dt.date, end: dt.date, bundle: Q2InputBundle
) -> Q2EngineConfig:
    # metadata > configs/q2_run.toml > calibrated defaults
    settings: dict[str, Any] = dict(_Q2_SETTING_DEFAULTS)
    settings.update(_load_q2_run_settings(context.repo_root))
    settings.update(context.metadata)

    raw_weights = settings["forecast_weights"]
    weights = tuple(float(value) for value in raw_weights)
    lags = tuple(int(value) for value in settings["forecast_lags"])
    pv_lags_value = settings.get("pv_lags")
    pv_lags = tuple(int(value) for value in pv_lags_value) if pv_lags_value else None
    pv_weights_value = settings.get("pv_weights")
    pv_weights = (
        tuple(float(value) for value in pv_weights_value) if pv_weights_value else None
    )
    prices = tuple(point.price_cny_per_kwh for point in bundle.fixed_prices)
    default_terminal_value = 0.9 * sum(prices) / len(prices) if prices else 0.0
    annual_endpoint = _day(context, "annual_endpoint_day", dt.date(2025, 12, 31))
    annual_target = settings.get(
        "annual_terminal_soc_kwh", 6000.0 if end == annual_endpoint else None
    )
    return Q2EngineConfig(
        action_start_day=start,
        action_end_day=end,
        forecast_config=ForecastConfig(
            weights=weights,
            ar1_phi=settings.get("ar1_phi"),
            model_version=str(settings.get("forecast_model_version", "q2-weekly-ar1-v2")),
            allow_short_history=False,
            lags=lags,
            pv_lags=pv_lags,
            pv_weights=pv_weights,
            load_bias_window=int(settings.get("load_bias_window", 0)),
            load_bias_scale=float(settings.get("load_bias_scale", 0.0)),
        ),
        model_config=Q2ModelConfig(
            horizon_steps=144,
            time_limit_s=settings.get("solver_time_limit_s"),
        ),
        annual_terminal_soc_kwh=annual_target,
        annual_endpoint_day=annual_endpoint,
        is_synthetic=context.is_synthetic,
        terminal_value_cny_per_kwh=float(
            settings.get("terminal_value_cny_per_kwh", default_terminal_value)
        ),
        purchase_margin=float(settings.get("purchase_margin", 0.0)),
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
    action_end = _day(context, "action_end_day", dt.date(2025, 12, 31))
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
                verify_loaded_input_paths(context.repo_root, [attachment1, attachment2])
            )
            if provenance_issues:
                raise InputError("input provenance check failed: " + "; ".join(provenance_issues))

        stage = "input_load"
        expected_days = context.metadata.get("expected_days")
        if expected_days is not None:
            expected = tuple(dt.date.fromisoformat(str(value)) for value in expected_days)
        else:
            input_start = _day(context, "input_start_day", dt.date(2025, 1, 1))
            input_end = _day(context, "input_end_day", dt.date(2025, 12, 31))
            expected = _days_between(input_start, input_end)
        bundle = load_q2_inputs(
            attachment1_path=attachment1,
            attachment2_path=attachment2,
            load_sheet_name=str(context.metadata.get("load_sheet_name", "小区负载")),
            pv_sheet_name=str(context.metadata.get("pv_sheet_name", "光伏发电实际功率")),
            expected_days=expected,
        )

        stage = "solve"
        # Build the config once and reuse the same object for the engine and the
        # recorded artifact, so effective_config.json cannot drift from what
        # actually ran.
        engine_config = _build_engine_config(context, action_start, action_end, bundle)
        engineering = run_q2_engineering(bundle, engine_config)

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
                "accounting": _accounting_dict(engineering),
            },
        )

        stage = "artifact_write"
        input_snapshot = write_json(run_dir / "input_snapshot.json", _bundle_to_dict(bundle))
        effective_config = write_json(
            run_dir / "effective_config.json",
            {
                "action_start_day": action_start.isoformat(),
                "action_end_day": action_end.isoformat(),
                "is_synthetic": engine_config.is_synthetic,
                "forecast": {
                    "model_version": engine_config.forecast_config.model_version,
                    "lags": list(engine_config.forecast_config.lags),
                    "weights": list(engine_config.forecast_config.weights),
                    "pv_lags": (
                        None
                        if engine_config.forecast_config.pv_lags is None
                        else list(engine_config.forecast_config.pv_lags)
                    ),
                    "pv_weights": (
                        None
                        if engine_config.forecast_config.pv_weights is None
                        else list(engine_config.forecast_config.pv_weights)
                    ),
                    "ar1_phi": engine_config.forecast_config.ar1_phi,
                    "load_bias_window": engine_config.forecast_config.load_bias_window,
                    "load_bias_scale": engine_config.forecast_config.load_bias_scale,
                },
                "settlement": {
                    "purchase_margin": engine_config.purchase_margin,
                    "terminal_value_cny_per_kwh": engine_config.terminal_value_cny_per_kwh,
                },
                "model": {
                    "horizon_steps": engine_config.model_config.horizon_steps,
                    "solver_time_limit_s": engine_config.model_config.time_limit_s,
                },
                "annual": {
                    "annual_terminal_soc_kwh": engine_config.annual_terminal_soc_kwh,
                    "annual_endpoint_day": engine_config.annual_endpoint_day.isoformat(),
                },
            },
        )
        forecast_path = _write_json_records(
            run_dir / "forecast_records.json",
            (
                {
                    "valid_time": point.valid_time.isoformat(sep=" "),
                    "available_at": point.available_at.isoformat(sep=" "),
                    "load_kw": point.load_kw,
                    "pv_kw": point.pv_kw,
                    "training_cutoff": point.training_cutoff.isoformat(sep=" "),
                    "model_version": point.model_version,
                    "data_version": point.data_version,
                    "fallback_reason": point.fallback_reason,
                }
                for point in engineering.forecast_records
            ),
        )
        solver_records_path = _write_json_records(
            run_dir / "solver_records.json", engineering.solver_records
        )
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
                "accounting": result.metadata["accounting"],
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
            for path in (
                input_snapshot, effective_config, forecast_path, solver_records_path,
                domain_result, validation_path, summary, solver_log,
            )
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
