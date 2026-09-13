"""Audit evidence-only Q3 load-forecast candidates on attachment 2.

The script performs causal rolling-origin comparisons at 00:00, 06:00,
12:00 and 18:00.  It does not approve ``D_LOAD_FORECAST`` or
``D_MODEL_Q3`` and is not imported by a formal case runner.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from microgrid.dataio import read_wide_attachment, sha256_file

STEP_MINUTES = 10
SLOTS_PER_DAY = 24 * 60 // STEP_MINUTES
LAG_DAYS = (7, 14, 21, 28)
LAG_SLOTS = tuple(day * SLOTS_PER_DAY for day in LAG_DAYS)
ISSUE_HOURS = (0, 6, 12, 18)
COMMIT_HORIZON_STEPS = 6 * 60 // STEP_MINUTES
REFERENCE_METHOD = "lag7_plus_ar1"
PREFERRED_METHOD = "exp_half_life_1w_plus_ar1"


@dataclass(frozen=True)
class CandidateSpec:
    name: str
    lag_weights: tuple[float, ...]
    description: str

    def __post_init__(self) -> None:
        if len(self.lag_weights) != len(LAG_DAYS):
            raise ValueError(f"{self.name}: expected {len(LAG_DAYS)} lag weights")
        weights = np.asarray(self.lag_weights, dtype=float)
        if not np.all(np.isfinite(weights)) or np.any(weights < 0):
            raise ValueError(f"{self.name}: lag weights must be finite and non-negative")
        if not math.isclose(float(weights.sum()), 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError(f"{self.name}: lag weights must sum to one")


@dataclass(frozen=True)
class LoadSeries:
    start: dt.datetime
    values_kw: np.ndarray

    @property
    def end(self) -> dt.datetime:
        return self.start + dt.timedelta(minutes=STEP_MINUTES * (len(self.values_kw) - 1))

    def index_at(self, timestamp: dt.datetime) -> int:
        delta = timestamp - self.start
        total_seconds = delta.total_seconds()
        step_seconds = STEP_MINUTES * 60
        if total_seconds % step_seconds != 0:
            raise KeyError(f"timestamp is not on the ten-minute grid: {timestamp}")
        index = int(total_seconds // step_seconds)
        if not 0 <= index < len(self.values_kw):
            raise KeyError(f"timestamp is outside the load series: {timestamp}")
        return index


@dataclass(frozen=True)
class CandidateState:
    residual_kw: np.ndarray
    ar_numerator: np.ndarray
    ar_denominator: np.ndarray


@dataclass(frozen=True)
class Prediction:
    power_kw: float
    raw_power_kw: float
    phi: float
    latest_visible_residual_kw: float
    was_clipped: bool


@dataclass(frozen=True)
class EvaluationRow:
    decision_time: dt.datetime
    target_time: dt.datetime
    issue_hour: int
    horizon_steps: int
    actual_kw: float
    updated_predictions: Mapping[str, Prediction]
    midnight_predictions: Mapping[str, Prediction]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actual", type=Path, required=True, help="附件2 workbook path")
    parser.add_argument(
        "--evaluation-start",
        type=dt.date.fromisoformat,
        default=dt.date(2025, 2, 1),
    )
    parser.add_argument(
        "--evaluation-end",
        type=dt.date.fromisoformat,
        default=dt.date(2026, 1, 1),
        help="Exclusive decision-date boundary",
    )
    parser.add_argument(
        "--holdout-start",
        type=dt.date.fromisoformat,
        default=dt.date(2025, 9, 1),
    )
    parser.add_argument(
        "--half-life-weeks",
        type=float,
        nargs="+",
        default=[0.5, 1.0, 2.0],
        help="Recency half-lives for the 7/14/21/28-day lag sensitivity",
    )
    parser.add_argument("--bootstrap-replicates", type=int, default=2000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260912)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/evidence/q3_load_forecast_candidates.json"),
    )
    return parser.parse_args()


def exponential_lag_weights(half_life_weeks: float) -> tuple[float, ...]:
    """Return normalized weights for 7/14/21/28-day same-slot lags."""

    if not math.isfinite(half_life_weeks) or half_life_weeks <= 0:
        raise ValueError("half-life must be finite and positive")
    raw = np.power(2.0, -np.arange(len(LAG_DAYS), dtype=float) / half_life_weeks)
    normalized = raw / raw.sum()
    return tuple(float(value) for value in normalized)


def _half_life_label(value: float) -> str:
    return str(int(value)) if value.is_integer() else repr(value)


def candidate_specs(half_lives: tuple[float, ...]) -> tuple[CandidateSpec, ...]:
    clean_half_lives = tuple(sorted(set(float(value) for value in half_lives)))
    if 1.0 not in clean_half_lives:
        raise ValueError("--half-life-weeks must include 1.0 to evaluate LOAD-A")
    candidates = [
        CandidateSpec(
            name=REFERENCE_METHOD,
            lag_weights=(1.0, 0.0, 0.0, 0.0),
            description="7-day same-slot lag with expanding AR(1) residual correction",
        ),
        CandidateSpec(
            name="equal_7_14_21_28d_plus_ar1",
            lag_weights=(0.25, 0.25, 0.25, 0.25),
            description="equal 7/14/21/28-day same-slot mean with AR(1) correction",
        ),
    ]
    for half_life in clean_half_lives:
        label = _half_life_label(half_life)
        candidates.append(
            CandidateSpec(
                name=f"exp_half_life_{label}w_plus_ar1",
                lag_weights=exponential_lag_weights(half_life),
                description=(
                    "exponentially weighted 7/14/21/28-day same-slot lags; "
                    f"half-life={label} week(s); expanding AR(1) residual correction"
                ),
            )
        )
    return tuple(candidates)


def load_actual(path: Path) -> dict[dt.datetime, float]:
    rows = read_wide_attachment(
        path,
        sheet_name="小区负载",
        kind="load_actual_kw",
        unit="kW",
    )
    actual: dict[dt.datetime, float] = {}
    for row in rows:
        raw_timestamp = row["parsed_timestamp"]
        if raw_timestamp is None:
            raise ValueError(f"load record has no parsed timestamp: {row['cell_ref']}")
        timestamp = dt.datetime.fromisoformat(str(raw_timestamp))
        if timestamp in actual:
            raise ValueError(f"duplicate load timestamp: {timestamp}")
        raw_value = row["value"]
        if isinstance(raw_value, bool):
            raise ValueError(f"load must be numeric, not boolean, at {timestamp}")
        value = float(raw_value)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"load must be finite and non-negative at {timestamp}")
        actual[timestamp] = value
    return actual


def build_load_series(actual: Mapping[dt.datetime, float]) -> LoadSeries:
    if not actual:
        raise ValueError("attachment 2 contains no usable load observations")
    timestamps = sorted(actual)
    if any(
        timestamp.second != 0 or timestamp.microsecond != 0 or timestamp.minute % STEP_MINUTES != 0
        for timestamp in timestamps
    ):
        raise ValueError("load timestamps must be aligned to ten-minute endpoints")
    for previous, current in zip(timestamps[:-1], timestamps[1:], strict=True):
        if current - previous != dt.timedelta(minutes=STEP_MINUTES):
            raise ValueError(
                "load observations must form one continuous ten-minute grid; "
                f"gap between {previous} and {current}"
            )
    values = np.asarray([actual[timestamp] for timestamp in timestamps], dtype=float)
    if not np.all(np.isfinite(values)) or np.any(values < 0):
        raise ValueError("load values must be finite and non-negative")
    return LoadSeries(start=timestamps[0], values_kw=values)


def fit_candidate_state(series: LoadSeries, candidate: CandidateSpec) -> CandidateState:
    values = series.values_kw
    residuals = np.full(len(values), np.nan, dtype=float)
    weights = np.asarray(candidate.lag_weights, dtype=float)
    active_lag_slots = tuple(
        lag_slot for lag_slot, weight in zip(LAG_SLOTS, weights, strict=True) if weight > 0
    )
    if not active_lag_slots:  # guarded by the sum-to-one check, kept explicit here
        raise ValueError(f"{candidate.name}: at least one lag weight must be positive")
    first_residual_index = max(active_lag_slots)
    for index in range(first_residual_index, len(values)):
        lag_values = values[index - np.asarray(LAG_SLOTS)]
        residuals[index] = values[index] - float(np.dot(weights, lag_values))

    ar_numerator = np.zeros(len(values), dtype=float)
    ar_denominator = np.zeros(len(values), dtype=float)
    running_numerator = 0.0
    running_denominator = 0.0
    for index in range(1, len(values)):
        previous = residuals[index - 1]
        current = residuals[index]
        if math.isfinite(previous) and math.isfinite(current):
            running_numerator += float(previous * current)
            running_denominator += float(previous * previous)
        ar_numerator[index] = running_numerator
        ar_denominator[index] = running_denominator
    return CandidateState(residuals, ar_numerator, ar_denominator)


def predict_point(
    *,
    series: LoadSeries,
    candidate: CandidateSpec,
    state: CandidateState,
    decision_time: dt.datetime,
    target_time: dt.datetime,
) -> Prediction:
    decision_index = series.index_at(decision_time)
    target_index = series.index_at(target_time)
    horizon_steps = target_index - decision_index
    if horizon_steps <= 0:
        raise ValueError("target_time must be after decision_time")

    lag_indices = target_index - np.asarray(LAG_SLOTS)
    if np.any(lag_indices < 0):
        raise ValueError("not enough lag history for target")
    if np.any(lag_indices > decision_index):
        raise ValueError("lag predictor would read an observation after decision_time")
    latest_residual = float(state.residual_kw[decision_index])
    if not math.isfinite(latest_residual):
        raise ValueError("not enough causal residual history at decision_time")

    denominator = float(state.ar_denominator[decision_index])
    phi = 0.0
    if denominator > 0:
        phi = float(np.clip(state.ar_numerator[decision_index] / denominator, 0.0, 0.999))
    base_kw = float(np.dot(np.asarray(candidate.lag_weights), series.values_kw[lag_indices]))
    raw_power_kw = base_kw + phi**horizon_steps * latest_residual
    return Prediction(
        power_kw=max(0.0, raw_power_kw),
        raw_power_kw=raw_power_kw,
        phi=phi,
        latest_visible_residual_kw=latest_residual,
        was_clipped=raw_power_kw < 0,
    )


def _decision_times(start: dt.date, end: dt.date) -> tuple[dt.datetime, ...]:
    times: list[dt.datetime] = []
    day = start
    while day < end:
        for hour in ISSUE_HOURS:
            times.append(dt.datetime.combine(day, dt.time(hour=hour)))
        day += dt.timedelta(days=1)
    return tuple(times)


def build_evaluation_rows(
    *,
    series: LoadSeries,
    candidates: tuple[CandidateSpec, ...],
    states: Mapping[str, CandidateState],
    evaluation_start: dt.date,
    evaluation_end: dt.date,
) -> tuple[tuple[EvaluationRow, ...], dict[str, Any]]:
    rows: list[EvaluationRow] = []
    missing_decisions: list[str] = []
    missing_targets = 0
    missing_targets_by_issue = {str(hour): 0 for hour in ISSUE_HOURS}
    scheduled_decisions = 0
    evaluated_decisions = 0

    for decision_time in _decision_times(evaluation_start, evaluation_end):
        scheduled_decisions += 1
        try:
            series.index_at(decision_time)
        except KeyError:
            missing_decisions.append(decision_time.isoformat())
            continue
        midnight = dt.datetime.combine(decision_time.date(), dt.time())
        try:
            series.index_at(midnight)
        except KeyError as exc:
            raise ValueError(f"midnight LF-B origin is unavailable for {decision_time}") from exc
        evaluated_decisions += 1

        for horizon_steps in range(1, COMMIT_HORIZON_STEPS + 1):
            target_time = decision_time + dt.timedelta(minutes=STEP_MINUTES * horizon_steps)
            try:
                target_index = series.index_at(target_time)
            except KeyError:
                missing_targets += 1
                missing_targets_by_issue[str(decision_time.hour)] += 1
                continue
            updated = {
                candidate.name: predict_point(
                    series=series,
                    candidate=candidate,
                    state=states[candidate.name],
                    decision_time=decision_time,
                    target_time=target_time,
                )
                for candidate in candidates
            }
            midnight_predictions = {
                candidate.name: predict_point(
                    series=series,
                    candidate=candidate,
                    state=states[candidate.name],
                    decision_time=midnight,
                    target_time=target_time,
                )
                for candidate in candidates
            }
            rows.append(
                EvaluationRow(
                    decision_time=decision_time,
                    target_time=target_time,
                    issue_hour=decision_time.hour,
                    horizon_steps=horizon_steps,
                    actual_kw=float(series.values_kw[target_index]),
                    updated_predictions=updated,
                    midnight_predictions=midnight_predictions,
                )
            )

    if not rows:
        raise ValueError("no common Q3 evaluation targets are available")
    return tuple(rows), {
        "scheduled_decision_times": scheduled_decisions,
        "evaluated_decision_times": evaluated_decisions,
        "missing_decision_times": missing_decisions,
        "scheduled_targets": evaluated_decisions * COMMIT_HORIZON_STEPS,
        "common_target_count_per_method": len(rows),
        "missing_targets": missing_targets,
        "missing_targets_by_issue_hour": missing_targets_by_issue,
    }


def error_metrics(
    rows: tuple[EvaluationRow, ...],
    method: str,
    *,
    midnight_plan: bool = False,
) -> dict[str, float | int]:
    predictions = np.asarray(
        [
            (row.midnight_predictions if midnight_plan else row.updated_predictions)[
                method
            ].power_kw
            for row in rows
        ]
    )
    actual = np.asarray([row.actual_kw for row in rows])
    errors = predictions - actual
    clipped = sum(
        (row.midnight_predictions if midnight_plan else row.updated_predictions)[method].was_clipped
        for row in rows
    )
    return {
        "n": len(rows),
        "mae_kw": float(np.mean(np.abs(errors))),
        "rmse_kw": float(np.sqrt(np.mean(np.square(errors)))),
        "bias_kw": float(np.mean(errors)),
        "clipped_prediction_count": int(clipped),
    }


def paired_daily_bootstrap(
    rows: tuple[EvaluationRow, ...],
    *,
    candidate_method: str,
    baseline_method: str,
    candidate_midnight: bool,
    baseline_midnight: bool,
    replicates: int,
    seed: int,
) -> dict[str, float | int]:
    by_day: dict[dt.date, list[float]] = defaultdict(list)
    for row in rows:
        candidate = (row.midnight_predictions if candidate_midnight else row.updated_predictions)[
            candidate_method
        ].power_kw
        baseline = (row.midnight_predictions if baseline_midnight else row.updated_predictions)[
            baseline_method
        ].power_kw
        difference = abs(candidate - row.actual_kw) - abs(baseline - row.actual_kw)
        by_day[row.decision_time.date()].append(difference)

    days = sorted(by_day)
    daily_sums = np.asarray([sum(by_day[day]) for day in days], dtype=float)
    daily_counts = np.asarray([len(by_day[day]) for day in days], dtype=float)
    rng = np.random.default_rng(seed)
    sampled = rng.integers(0, len(days), size=(replicates, len(days)))
    sampled_differences = daily_sums[sampled].sum(axis=1) / daily_counts[sampled].sum(axis=1)
    lower, upper = np.quantile(sampled_differences, [0.025, 0.975])
    return {
        "day_blocks": len(days),
        "replicates": replicates,
        "mae_difference_kw": float(daily_sums.sum() / daily_counts.sum()),
        "ci95_low_kw": float(lower),
        "ci95_high_kw": float(upper),
    }


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    if args.evaluation_start >= args.evaluation_end:
        raise ValueError("--evaluation-start must be before --evaluation-end")
    if not args.evaluation_start < args.holdout_start < args.evaluation_end:
        raise ValueError("--holdout-start must be inside the evaluation period")
    if args.bootstrap_replicates <= 0:
        raise ValueError("--bootstrap-replicates must be positive")

    candidates = candidate_specs(tuple(args.half_life_weeks))
    actual = load_actual(args.actual)
    series = build_load_series(actual)
    states = {candidate.name: fit_candidate_state(series, candidate) for candidate in candidates}
    rows, sample = build_evaluation_rows(
        series=series,
        candidates=candidates,
        states=states,
        evaluation_start=args.evaluation_start,
        evaluation_end=args.evaluation_end,
    )
    development = tuple(row for row in rows if row.decision_time.date() < args.holdout_start)
    holdout = tuple(row for row in rows if row.decision_time.date() >= args.holdout_start)
    if not development or not holdout:
        raise ValueError("development and holdout samples must both be non-empty")

    metrics = {candidate.name: error_metrics(rows, candidate.name) for candidate in candidates}
    by_issue_hour = {
        candidate.name: {
            str(hour): error_metrics(
                tuple(row for row in rows if row.issue_hour == hour), candidate.name
            )
            for hour in ISSUE_HOURS
        }
        for candidate in candidates
    }
    split_metrics = {
        candidate.name: {
            "development": error_metrics(development, candidate.name),
            "holdout": error_metrics(holdout, candidate.name),
        }
        for candidate in candidates
    }
    lf_a_vs_lf_b = {}
    for candidate in candidates:
        lf_a = error_metrics(rows, candidate.name)
        lf_b = error_metrics(rows, candidate.name, midnight_plan=True)
        lf_a_vs_lf_b[candidate.name] = {
            "common_target_count": len(rows),
            "lf_a_updated_mae_kw": lf_a["mae_kw"],
            "lf_b_midnight_plan_mae_kw": lf_b["mae_kw"],
            "paired_day_block_bootstrap": paired_daily_bootstrap(
                rows,
                candidate_method=candidate.name,
                baseline_method=candidate.name,
                candidate_midnight=False,
                baseline_midnight=True,
                replicates=args.bootstrap_replicates,
                seed=args.bootstrap_seed,
            ),
        }
    method_comparisons = {
        candidate.name: paired_daily_bootstrap(
            rows,
            candidate_method=candidate.name,
            baseline_method=REFERENCE_METHOD,
            candidate_midnight=False,
            baseline_midnight=False,
            replicates=args.bootstrap_replicates,
            seed=args.bootstrap_seed,
        )
        for candidate in candidates
        if candidate.name != REFERENCE_METHOD
    }

    return {
        "schema_version": 1,
        "status": "candidate_evidence_only",
        "does_not_approve": ["D_LOAD_FORECAST", "D_MODEL_Q3"],
        "formal_runner_input": False,
        "audit_script": {
            "path": "scripts/audit_q3_load_forecast_candidates.py",
            "sha256": sha256_file(Path(__file__)),
        },
        "source": {
            "actual_file": args.actual.name,
            "actual_sha256": sha256_file(args.actual),
            "sheet_name": "小区负载",
            "unit": "kW",
        },
        "data_checks": {
            "load_record_count": len(series.values_kw),
            "first_interval_end": series.start.isoformat(),
            "last_interval_end": series.end.isoformat(),
            "grid_minutes": STEP_MINUTES,
            "continuous_grid": True,
            "duplicate_timestamps": 0,
            "gaps_within_observed_range": 0,
            "edge_cells_skipped_by_source_reader": "not inferable from normalized records",
            "non_finite_values": 0,
            "negative_values": 0,
        },
        "evaluation": {
            "evaluation_start": args.evaluation_start.isoformat(),
            "evaluation_end_exclusive": args.evaluation_end.isoformat(),
            "holdout_start": args.holdout_start.isoformat(),
            "issue_hours": list(ISSUE_HOURS),
            "commit_horizon_steps": COMMIT_HORIZON_STEPS,
            "commit_horizon_hours": 6,
            "grain": "(decision_time, target_time)",
            "timezone": "Asia/Shanghai local time stored without offset",
            "visibility_rule": "interval actual with endpoint <= decision_time is visible",
            "common_sample_rule": "all candidates use the same available target rows",
            "holdout_interpretation": (
                "date split diagnostic only; this audit does not claim that candidates or "
                "hyperparameters were selected without observing the holdout"
            ),
            **sample,
        },
        "method_contract": {
            "lag_days": list(LAG_DAYS),
            "base_formula": "sum(lag_weight_j * load[target_time - lag_days_j])",
            "residual_formula": "residual[t] = load[t] - base[t]",
            "ar1_fit": (
                "expanding OLS without intercept through decision_time; "
                "phi=clip(sum(e[t-1]*e[t])/sum(e[t-1]^2), 0, 0.999)"
            ),
            "forecast_formula": "max(0, base[target] + phi^horizon_steps * residual[decision])",
            "lf_a": "recompute causally at 00:00/06:00/12:00/18:00 for the next 6 hours",
            "lf_b": "continue the same day's 00:00 forecast for matched target times",
            "candidate_methods": {
                candidate.name: {
                    "description": candidate.description,
                    "lag_weights": list(candidate.lag_weights),
                }
                for candidate in candidates
            },
            "preferred_candidate_under_review": PREFERRED_METHOD,
            "reference_method": REFERENCE_METHOD,
            "empirical_choice_guard": (
                "rankings are evidence for team review only; they do not select or approve a model"
            ),
        },
        "q3_next_6h_metrics": metrics,
        "q3_next_6h_by_issue_hour": by_issue_hour,
        "development_holdout_metrics": split_metrics,
        "lf_a_vs_lf_b": lf_a_vs_lf_b,
        "paired_method_comparisons_vs_lag7_plus_ar1": method_comparisons,
        "bootstrap": {
            "block": "decision calendar day",
            "replicates": args.bootstrap_replicates,
            "seed": args.bootstrap_seed,
            "estimand": "candidate MAE minus baseline MAE; negative is better",
        },
    }


def render_json(result: Mapping[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"


def main() -> None:
    args = parse_args()
    result = evaluate(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_json(result)
    args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
