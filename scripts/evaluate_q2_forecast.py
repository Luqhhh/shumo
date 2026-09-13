"""Evaluate the causal Q2 forecast on an explicitly supplied workbook."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path
from typing import Any

from microgrid.dataio import sha256_file
from microgrid.problem.q2_forecast import ForecastConfig, ForecastPoint, build_q2_forecast
from microgrid.problem.q2_inputs import ActualInterval, _wide_values
from microgrid.schemas import InputError

PROTECTED_OUTPUT_PARTS = {"raw", "templates", "resources", "dist"}


def _parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise InputError(f"invalid ISO date: {value}") from exc


def _read_actuals(path: Path, load_sheet: str, pv_sheet: str) -> tuple[ActualInterval, ...]:
    loads = _wide_values(
        logical_name="attachment2 load",
        path=path,
        sheet_name=load_sheet,
        kind="load_kw",
        unit="kW",
    )
    pvs = _wide_values(
        logical_name="attachment2 pv",
        path=path,
        sheet_name=pv_sheet,
        kind="pv_actual_kw",
        unit="kW",
    )
    if set(loads) != set(pvs):
        raise InputError("attachment2 load/PV grids do not match")
    return tuple(
        ActualInterval(
            day=loads[key].day,
            slot=loads[key].slot,
            start=loads[key].start,
            end=loads[key].end,
            load_kw=loads[key].value,
            pv_kw=pvs[key].value,
            load_source_ref=loads[key].source_ref,
            pv_source_ref=pvs[key].source_ref,
        )
        for key in sorted(loads)
    )


def _days(start: dt.date, end: dt.date) -> tuple[dt.date, ...]:
    if end < start:
        raise InputError("end-date must not precede start-date")
    return tuple(start + dt.timedelta(days=offset) for offset in range((end - start).days + 1))


def _metric(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "mae": 0.0, "rmse": 0.0}
    absolute = [abs(value) for value in values]
    return {
        "count": len(values),
        "mae": sum(absolute) / len(values),
        "rmse": math.sqrt(sum(value * value for value in values) / len(values)),
    }


def _protected_output(path: Path) -> bool:
    return any(part.lower() in PROTECTED_OUTPUT_PARTS for part in path.resolve().parts)


def evaluate(
    *,
    attachment2: Path,
    load_sheet: str,
    pv_sheet: str,
    start_date: dt.date,
    end_date: dt.date,
    output: Path,
) -> dict[str, Any]:
    if not attachment2.is_file():
        raise InputError(f"attachment2 path is not a file: {attachment2}")
    if _protected_output(output):
        raise InputError(f"output path is protected: {output}")
    actuals = _read_actuals(attachment2, load_sheet, pv_sheet)
    by_key = {(item.day, item.slot): item for item in actuals}
    config = ForecastConfig(
        weights=(0.25, 0.25, 0.25, 0.25),
        ar1_phi=0.0,
        model_version="q2-weekly-ar1-v1",
        allow_short_history=False,
    )
    load_errors: list[float] = []
    pv_errors: list[float] = []
    lag_counts = {"7d": 0, "14d": 0, "21d": 0, "28d": 0}
    forecast_points: list[ForecastPoint] = []
    visible_counts: list[int] = []
    decisions = _days(start_date, end_date)
    for day in decisions:
        decision_time = dt.datetime.combine(day, dt.time())
        visible = tuple(item for item in actuals if item.end <= decision_time)
        visible_counts.append(len(visible))
        forecast = build_q2_forecast(
            visible,
            decision_time=decision_time,
            horizon_start=decision_time,
            config=config,
            horizon_steps=144,
        )
        forecast_points.extend(forecast)
        for point in forecast:
            target_start = point.valid_time - dt.timedelta(minutes=10)
            key = (target_start.date(), (target_start.hour * 60 + target_start.minute) // 10)
            if start_date <= target_start.date() <= end_date:
                actual = by_key.get(key)
                if actual is not None:
                    load_errors.append(point.load_kw - actual.load_kw)
                    pv_errors.append(point.pv_kw - actual.pv_kw)
            for days in (7, 14, 21, 28):
                lag_key = (target_start.date() - dt.timedelta(days=days), key[1])
                if lag_key in {(item.day, item.slot) for item in visible}:
                    lag_counts[f"{days}d"] += 1

    training_cutoff = max(point.training_cutoff for point in forecast_points)
    payload = {
        "source_hash": sha256_file(attachment2),
        "sample_range": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        "training_cutoff": training_cutoff.isoformat(sep=" "),
        "model_version": config.model_version,
        "weights": list(config.weights),
        "ar1_phi": config.ar1_phi,
        "parameter_status": "candidate engineering parameters; not an approval",
        "metrics": {
            "load": _metric(load_errors),
            "pv": _metric(pv_errors),
            "definitions": {
                "mae": "mean(abs(forecast_kw - actual_kw))",
                "rmse": "sqrt(mean((forecast_kw - actual_kw)^2))",
            },
        },
        "causal_visibility": {
            "decision_count": len(decisions),
            "visible_actual_counts": visible_counts,
            "future_visible_records": 0,
            "lag_counts": lag_counts,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attachment2", type=Path, required=True)
    parser.add_argument("--load-sheet", required=True)
    parser.add_argument("--pv-sheet", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        evaluate(
            attachment2=args.attachment2,
            load_sheet=args.load_sheet,
            pv_sheet=args.pv_sheet,
            start_date=_parse_date(args.start_date),
            end_date=_parse_date(args.end_date),
            output=args.output,
        )
    except (InputError, OSError, ValueError) as exc:
        print(f"evaluate_q2_forecast: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
