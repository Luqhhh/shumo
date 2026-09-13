"""Q4-2 formal runner: Q2 rolling control under causal fluctuating prices.

Q4-2 is Q2 with the fixed price environment replaced by Attachment 4 prices.
The physical model, the load/PV forecast, the continuous SOC, the 00:00 frozen
day-ahead contract and the annual terminal SOC are all inherited from Q2; what
changes is where the optimizer's prices come from.

Information boundary (approved D_PRICE_FORECAST):

* The planner sees only a strictly causal forecast.  A price enters the training
  history only once its interval has ended, so no future Attachment 4 price can
  reach the optimizer.
* Settlement is different by design: replay bills the planned purchase at the
  realized Attachment 4 price for that interval, and the emergency purchase at
  five times that same realized price.
* Q4-2 has no adjustment purchase; it does not inherit Q3's resampling.

Every run therefore carries a price-forecast audit trail, and the causal
backtest over the real Attachment 4 series is written next to the result rather
than being kept in a notebook.  If the AR(1) correction fails to beat the bare
same-slot baseline, that evidence is recorded as-is: the model is not retuned
against future prices to make the numbers look better.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from ..approvals import require_approved_decisions
from ..artifacts import (
    build_manifest,
    ensure_run_id_available,
    new_run_id,
    sha256_file,
    verify_imported_inputs,
    verify_loaded_input_paths,
    write_json,
    write_manifest,
)
from ..schemas import InputError
from .contracts import CaseContext, CaseResult
from .q2 import (
    _accounting_dict,
    _build_engine_config,
    _bundle_to_dict,
    _command,
    _day,
    _days_between,
    _input_path,
    _write_json_records,
)
from .q2_engine import run_q2_engineering
from .q2_inputs import load_q2_inputs, load_q4_2_prices, require_matching_q4_2_grid
from .q4_price_forecast import evaluate_causal_price_forecast
from .result_io import save_case_result
from .validation import validate_complete_run

CASE_ID = "q4_2"
MODEL_DECISION_ID = "D_MODEL_Q4_2"

# Mirrors ``microgrid.cases.CASE_DECISIONS["q4_2"]``.  It is repeated here
# rather than imported because ``cases`` imports this package, so a module-level
# import back into ``cases`` would be circular.
Q4_2_DECISION_IDS = (
    "D_TIME_INTERNAL",
    "D_EFF",
    "D_STATE",
    "D_INFO",
    "D_SETTLE",
    "D_PRICE_FORECAST",
    "D_MODEL_Q4_2",
)

# Attachment 4 uses raw right-endpoint wide columns, so the sheet is not one of
# the named Attachment 2 sheets.
DEFAULT_PRICE_SHEET = "Sheet1"


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


def run(context: CaseContext) -> CaseResult:
    """Execute Q4-2 only after all decisions are approved and inputs are explicit."""

    require_approved_decisions(context.repo_root, Q4_2_DECISION_IDS)
    raw = context.repo_root / "data" / "raw"
    attachment1 = _input_path(context, "attachment1_path", raw / "附件1.xlsx")
    attachment2 = _input_path(context, "attachment2_path", raw / "附件2.xlsx")
    attachment4 = _input_path(context, "attachment4_path", raw / "附件4.xlsx")
    paths = (
        ("attachment1", attachment1),
        ("attachment2", attachment2),
        ("attachment4", attachment4),
    )
    if not context.is_synthetic:
        for logical_name, path in paths:
            if not path.is_file():
                raise InputError(f"{logical_name} path is not a file: {path}")

    action_start = _day(context, "action_start_day", dt.date(2025, 2, 1))
    action_end = _day(context, "action_end_day", dt.date(2025, 12, 31))
    run_id = context.run_id or new_run_id(CASE_ID, context.repo_root)
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
                verify_loaded_input_paths(
                    context.repo_root, [attachment1, attachment2, attachment4]
                )
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
        variable_prices = load_q4_2_prices(
            attachment4_path=attachment4,
            price_sheet_name=str(context.metadata.get("price_sheet_name", DEFAULT_PRICE_SHEET)),
        )

        stage = "grid_check"
        # The two series have to describe the same ten-minute grid, otherwise a
        # settlement price could be matched to the wrong interval.
        require_matching_q4_2_grid(bundle, variable_prices)

        stage = "price_backtest"
        backtest = evaluate_causal_price_forecast(
            variable_prices, start_day=action_start, end_day=action_end
        )

        stage = "solve"
        engine_config = _build_engine_config(context, action_start, action_end, bundle)
        engineering = run_q2_engineering(bundle, engine_config, variable_prices=variable_prices)

        stage = "validation"
        complete = validate_complete_run(
            engineering.intervals,
            _days_between(action_start, action_end),
        )
        if not complete.ok:
            raise InputError("Q4-2 complete-run validation failed: " + "; ".join(complete.issues))
        result = CaseResult(
            case_id=CASE_ID,
            run_id=run_id,
            status="success",
            is_synthetic=context.is_synthetic,
            intervals=engineering.intervals,
            metadata={
                **engineering.metadata,
                "model": "q4_2_variable_price_rolling_milp",
                "model_status": "implemented",
                "model_decision": MODEL_DECISION_ID,
                "price_decision": "D_PRICE_FORECAST",
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
        input_snapshot = write_json(
            run_dir / "input_snapshot.json",
            {
                **_bundle_to_dict(bundle),
                "variable_prices": [
                    {
                        "day": point.day.isoformat(),
                        "slot": point.slot,
                        "start": point.start.isoformat(sep=" "),
                        "end": point.end.isoformat(sep=" "),
                        "price_cny_per_kwh": point.price_cny_per_kwh,
                        "source_ref": point.source_ref,
                    }
                    for point in variable_prices.prices
                ],
            },
        )
        effective_config = write_json(
            run_dir / "effective_config.json",
            {
                "action_start_day": action_start.isoformat(),
                "action_end_day": action_end.isoformat(),
                "is_synthetic": engine_config.is_synthetic,
                "price": {
                    "decision": "D_PRICE_FORECAST",
                    "model_version": backtest["model_version"],
                    "price_sheet_name": str(
                        context.metadata.get("price_sheet_name", DEFAULT_PRICE_SHEET)
                    ),
                    "planning_prices": "strictly causal forecast",
                    "settlement_prices": "realized Attachment 4 interval price",
                    "emergency_multiplier": 5.0,
                    "adjustment_purchase": "not applicable to Q4-2",
                    "terminal_value": (
                        "eta_d * mean(predicted price over the 144 steps after the "
                        "current window); suppressed when the annual terminal SOC "
                        "constraint is active in the window"
                    ),
                },
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
        price_forecast_path = _write_json_records(
            run_dir / "price_forecast_records.json",
            (
                {
                    "valid_time": point.valid_time.isoformat(sep=" "),
                    "available_at": point.available_at.isoformat(sep=" "),
                    "training_cutoff": point.training_cutoff.isoformat(sep=" "),
                    "model_version": point.model_version,
                    "data_version": point.data_version,
                    "ar1_phi": point.ar1_phi,
                    "baseline_cny_per_kwh": point.baseline_cny_per_kwh,
                    "residual_adjustment_cny_per_kwh": (point.residual_adjustment_cny_per_kwh),
                    "predicted_cny_per_kwh": point.predicted_cny_per_kwh,
                }
                for point in engineering.price_forecast_records
            ),
        )
        backtest_path = write_json(run_dir / "price_forecast_backtest.json", backtest)
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
                "final_soc_kwh": result.intervals[-1].state_end.energy_kwh,
                "emergency_purchase_kwh": engineering.accounting.e_realized_kwh,
                "price_forecast_model_version": backtest["model_version"],
                "price_forecast_metrics_by_lead": backtest["metrics_by_lead"],
                "solver_records": len(engineering.solver_records),
                "validation_ok": complete.ok,
            },
        )
        solver_log = run_dir / "solver.log"
        solver_log.write_text(
            "\n".join(
                [
                    "solver=scipy.optimize.milp (HiGHS)",
                    "prices=strictly causal Attachment 4 forecast; replay billed at realized price",
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
                input_snapshot,
                effective_config,
                forecast_path,
                price_forecast_path,
                backtest_path,
                solver_records_path,
                domain_result,
                validation_path,
                summary,
                solver_log,
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
