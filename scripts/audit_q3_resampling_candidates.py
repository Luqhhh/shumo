"""Reproduce the evidence comparison for proposed Q3 resampling choices.

This is an audit-only script.  It does not approve ``D_RESAMPLE`` and is not
imported by a formal case runner.  Official workbooks are opened read-only via
the shared data readers; only aggregate evidence is written.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.interpolate import PchipInterpolator

from microgrid.dataio import read_forecasts, read_wide_attachment, sha256_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actual", type=Path, required=True, help="附件2 workbook path")
    parser.add_argument("--forecast", type=Path, required=True, help="附件3 workbook path")
    parser.add_argument(
        "--weight-epsilon-kw",
        type=float,
        required=True,
        help="Explicit numerical epsilon in 1 / (historical_MAE + epsilon)^2",
    )
    parser.add_argument(
        "--evaluation-start",
        type=dt.date.fromisoformat,
        default=dt.date(2025, 2, 1),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/evidence/q3_resampling_candidates.json"),
    )
    return parser.parse_args()


def load_actual(path: Path) -> dict[dt.datetime, float]:
    rows = read_wide_attachment(
        path,
        sheet_name="光伏发电实际功率",
        kind="pv_actual_kw",
        unit="kW",
    )
    return {dt.datetime.fromisoformat(row["parsed_timestamp"]): float(row["value"]) for row in rows}


def build_inputs(actual_path: Path, forecast_path: Path):
    actual = load_actual(actual_path)
    forecasts = read_forecasts(forecast_path)
    by_valid: dict[dt.datetime, list[Any]] = defaultdict(list)
    by_issue: dict[dt.datetime, list[Any]] = defaultdict(list)
    history: dict[int, list[tuple[dt.datetime, float]]] = defaultdict(list)

    for record in forecasts:
        by_valid[record.valid_time].append(record)
        by_issue[record.issue_time].append(record)
        if record.valid_time in actual:
            history[record.lead_hours].append(
                (
                    record.valid_time,
                    abs(record.pv_forecast_kw - actual[record.valid_time]),
                )
            )

    history_arrays: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for lead_hours, rows in history.items():
        rows.sort(key=lambda item: item[0])
        history_arrays[lead_hours] = (
            np.array([np.datetime64(item[0]) for item in rows]),
            np.array([item[1] for item in rows], dtype=float),
        )
    for rows in by_valid.values():
        rows.sort(key=lambda record: record.issue_time)
    return actual, by_valid, by_issue, history_arrays


def causal_mae(
    history_arrays: dict[int, tuple[np.ndarray, np.ndarray]],
    lead_hours: int,
    decision_time: dt.datetime,
) -> float:
    times, errors = history_arrays[lead_hours]
    # At a decision boundary, actuals whose interval ends at that boundary are visible.
    end = int(np.searchsorted(times, np.datetime64(decision_time), side="right"))
    if end == 0:
        raise ValueError(f"no causal error history for lead={lead_hours} at {decision_time}")
    return float(np.mean(errors[:end]))


def combined_hourly_knots(
    actual: dict[dt.datetime, float],
    by_valid: dict[dt.datetime, list[Any]],
    history_arrays: dict[int, tuple[np.ndarray, np.ndarray]],
    decision_time: dt.datetime,
    epsilon_kw: float,
) -> np.ndarray:
    """Return boundary proxy plus 24 combined hourly VERSION-B knots."""

    knots = [actual[decision_time]]
    for lead in range(1, 25):
        valid_time = decision_time + dt.timedelta(hours=lead)
        visible = [record for record in by_valid[valid_time] if record.issue_time <= decision_time]
        if not visible:
            raise ValueError(f"no visible forecast for {valid_time} at {decision_time}")
        historical_mae = np.array(
            [causal_mae(history_arrays, record.lead_hours, decision_time) for record in visible],
            dtype=float,
        )
        raw_weights = 1.0 / np.square(historical_mae + epsilon_kw)
        values = np.array([record.pv_forecast_kw for record in visible], dtype=float)
        knots.append(float(np.average(values, weights=raw_weights)))
    return np.array(knots, dtype=float)


def resample(knots: np.ndarray, method: str) -> np.ndarray:
    hourly_x = np.arange(25, dtype=float)
    right_endpoint_x = np.arange(1, 145, dtype=float) / 6.0
    if method == "next_hour_hold":
        values = knots[np.ceil(right_endpoint_x - 1e-12).astype(int)]
    elif method == "previous_knot_hold":
        values = knots[np.floor(right_endpoint_x + 1e-12).astype(int)]
    elif method == "linear":
        values = np.interp(right_endpoint_x, hourly_x, knots)
    elif method == "pchip":
        values = PchipInterpolator(hourly_x, knots)(right_endpoint_x)
    else:
        raise ValueError(f"unknown method: {method}")
    return np.maximum(values, 0.0)


def summarize_errors(rows: pd.DataFrame) -> dict[str, dict[str, float | int]]:
    summary: dict[str, dict[str, float | int]] = {}
    for method, frame in rows.groupby("method"):
        errors = frame["error_kw"].to_numpy(dtype=float)
        summary[str(method)] = {
            "n": int(len(frame)),
            "mae_kw": float(np.mean(np.abs(errors))),
            "rmse_kw": float(np.sqrt(np.mean(np.square(errors)))),
            "bias_kw": float(np.mean(errors)),
        }
    return summary


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    if not math.isfinite(args.weight_epsilon_kw) or args.weight_epsilon_kw <= 0:
        raise ValueError("--weight-epsilon-kw must be finite and positive")

    actual, by_valid, by_issue, history_arrays = build_inputs(args.actual, args.forecast)
    evaluation_start = dt.datetime.combine(args.evaluation_start, dt.time())
    decision_times = [
        issue_time
        for issue_time in sorted(by_issue)
        if issue_time >= evaluation_start and issue_time in actual
    ]
    methods = ["next_hour_hold", "previous_knot_hold", "linear", "pchip"]
    point_rows: list[dict[str, Any]] = []
    complete_horizons = 0

    for decision_time in decision_times:
        knots = combined_hourly_knots(
            actual,
            by_valid,
            history_arrays,
            decision_time,
            args.weight_epsilon_kw,
        )
        slot_ends = [decision_time + dt.timedelta(minutes=10 * step) for step in range(1, 145)]
        actual_values = np.array(
            [actual.get(slot_end, np.nan) for slot_end in slot_ends], dtype=float
        )
        available = np.isfinite(actual_values)
        if available.all():
            complete_horizons += 1
        previous_actual = np.array(
            [actual.get(slot_end - dt.timedelta(minutes=10), np.nan) for slot_end in slot_ends],
            dtype=float,
        )
        actual_change = np.abs(actual_values - previous_actual)

        for method in methods:
            predictions = resample(knots, method)
            for index in np.flatnonzero(available):
                point_rows.append(
                    {
                        "method": method,
                        "decision_hour": decision_time.hour,
                        "horizon_minutes": (index + 1) * 10,
                        "active_pv": bool(actual_values[index] > 0),
                        "ramp_over_250kw": bool(actual_change[index] > 250),
                        "error_kw": float(predictions[index] - actual_values[index]),
                    }
                )

    points = pd.DataFrame(point_rows)
    return {
        "schema_version": 1,
        "status": "candidate_evidence_only",
        "does_not_approve": "D_RESAMPLE",
        "source": {
            "actual_file": args.actual.name,
            "actual_sha256": sha256_file(args.actual),
            "forecast_file": args.forecast.name,
            "forecast_sha256": sha256_file(args.forecast),
        },
        "method_contract": {
            "visible_rule": "issue_time <= decision_time",
            "version_combination": (
                "all visible vintages; historical MAE grouped by lead_hours; "
                "expanding history with actual valid_time <= decision_time"
            ),
            "weight_formula": "raw_weight = 1 / (historical_MAE + epsilon_kw)^2",
            "weight_epsilon_kw": args.weight_epsilon_kw,
            "left_boundary_proxy": "last observed 10-minute interval mean",
            "target_grid": "10-minute slot_end values from t+10min through t+24h",
            "metric_weighting": "each available 10-minute right-endpoint equally weighted",
        },
        "sample": {
            "evaluation_start": evaluation_start.isoformat(),
            "decision_count": len(decision_times),
            "point_count_per_method": int((points["method"] == "linear").sum()),
            "complete_24h_horizons_per_method": complete_horizons,
        },
        "all_24h_points": summarize_errors(points),
        "next_6h_points": summarize_errors(points[points["horizon_minutes"] <= 360]),
        "active_pv_points": summarize_errors(points[points["active_pv"]]),
        "ramp_over_250kw_points": summarize_errors(points[points["ramp_over_250kw"]]),
    }


def main() -> None:
    args = parse_args()
    result = evaluate(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
