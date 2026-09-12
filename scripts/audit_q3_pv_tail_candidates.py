"""Audit strictly causal PV-tail baseline candidates on attachment 2.

This evidence-only script evaluates exactly the suffix requested by HORIZON-B
between two attachment-3 releases. It neither implements nor approves
``D_PV_TAIL_BASELINE`` and is never imported by a formal case runner.
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
from typing import Literal

import numpy as np

from microgrid.dataio import read_wide_attachment, sha256_file

STEP_MINUTES = 10
STEPS_PER_DAY = 24 * 60 // STEP_MINUTES
ISSUE_HOURS = (0, 6, 12, 18)
NON_RELEASE_STEPS = 6 * 60 // STEP_MINUTES - 1
REFERENCE_METHOD = "seasonal_naive_1d"
SIMPLE_MEAN_METHOD = "mean_7d"
EXP2_METHOD = "exp_half_life_2d"


@dataclass(frozen=True)
class CandidateSpec:
    name: str
    lag_days: tuple[int, ...]
    aggregation: Literal["weighted_mean", "median"]
    weights: tuple[float, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if not self.lag_days or any(day <= 0 for day in self.lag_days):
            raise ValueError(f"{self.name}: lag days must be positive")
        if len(self.lag_days) != len(set(self.lag_days)):
            raise ValueError(f"{self.name}: lag days must be unique")
        if self.aggregation == "weighted_mean":
            if len(self.weights) != len(self.lag_days):
                raise ValueError(f"{self.name}: weights must match lag days")
            weights = np.asarray(self.weights, dtype=float)
            if not np.all(np.isfinite(weights)) or np.any(weights < 0):
                raise ValueError(f"{self.name}: weights must be finite and non-negative")
            if not math.isclose(float(weights.sum()), 1.0, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(f"{self.name}: weights must sum to one")
        elif self.weights:
            raise ValueError(f"{self.name}: median candidate cannot define weights")


@dataclass(frozen=True)
class PVSeries:
    start: dt.datetime
    values_kw: np.ndarray

    @property
    def end(self) -> dt.datetime:
        return self.start + dt.timedelta(minutes=STEP_MINUTES * (len(self.values_kw) - 1))

    def index_at(self, timestamp: dt.datetime) -> int:
        delta = timestamp - self.start
        seconds = delta.total_seconds()
        step_seconds = STEP_MINUTES * 60
        if seconds % step_seconds:
            raise KeyError(f"timestamp is not on the ten-minute grid: {timestamp}")
        index = int(seconds // step_seconds)
        if not 0 <= index < len(self.values_kw):
            raise KeyError(f"timestamp is outside the PV series: {timestamp}")
        return index


@dataclass(frozen=True)
class EvaluationRow:
    snapshot_issue_time: dt.datetime
    decision_time: dt.datetime
    target_time: dt.datetime
    tail_step: int
    actual_kw: float
    predictions_kw: Mapping[str, float]


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
        help="Exclusive snapshot issue-date boundary",
    )
    parser.add_argument(
        "--holdout-start",
        type=dt.date.fromisoformat,
        default=dt.date(2025, 9, 1),
    )
    parser.add_argument("--bootstrap-replicates", type=int, default=2000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260913)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/evidence/q3_pv_tail_candidates.json"),
    )
    return parser.parse_args()


def _normalized(values: np.ndarray) -> tuple[float, ...]:
    return tuple(float(value) for value in values / values.sum())


def candidate_specs() -> tuple[CandidateSpec, ...]:
    one_to_seven = tuple(range(1, 8))
    equal_three = (1 / 3, 1 / 3, 1 / 3)
    equal_seven = tuple(1 / 7 for _ in one_to_seven)
    exp_weights = _normalized(np.power(2.0, -np.arange(7, dtype=float) / 2.0))
    return (
        CandidateSpec(
            name=REFERENCE_METHOD,
            lag_days=(1,),
            aggregation="weighted_mean",
            weights=(1.0,),
            description="previous-day same ten-minute slot",
        ),
        CandidateSpec(
            name="mean_3d",
            lag_days=(1, 2, 3),
            aggregation="weighted_mean",
            weights=equal_three,
            description="equal mean of the previous 1/2/3 days at the same slot",
        ),
        CandidateSpec(
            name="mean_7d",
            lag_days=one_to_seven,
            aggregation="weighted_mean",
            weights=equal_seven,
            description="equal mean of the previous seven days at the same slot",
        ),
        CandidateSpec(
            name="median_7d",
            lag_days=one_to_seven,
            aggregation="median",
            description="median of the previous seven days at the same slot",
        ),
        CandidateSpec(
            name="exp_half_life_2d",
            lag_days=one_to_seven,
            aggregation="weighted_mean",
            weights=exp_weights,
            description="previous seven days with a pre-specified two-day recency half-life",
        ),
        CandidateSpec(
            name="weekly_7_14_21_28d",
            lag_days=(7, 14, 21, 28),
            aggregation="weighted_mean",
            weights=(8 / 15, 4 / 15, 2 / 15, 1 / 15),
            description="7/14/21/28-day same-slot lags with one-week half-life weights",
        ),
    )


def load_actual(path: Path) -> dict[dt.datetime, float]:
    rows = read_wide_attachment(
        path,
        sheet_name="光伏发电实际功率",
        kind="pv_actual_kw",
        unit="kW",
    )
    actual: dict[dt.datetime, float] = {}
    for row in rows:
        raw_timestamp = row["parsed_timestamp"]
        if raw_timestamp is None:
            raise ValueError(f"PV record has no parsed timestamp: {row['cell_ref']}")
        timestamp = dt.datetime.fromisoformat(str(raw_timestamp))
        if timestamp in actual:
            raise ValueError(f"duplicate PV timestamp: {timestamp}")
        raw_value = row["value"]
        if isinstance(raw_value, bool):
            raise ValueError(f"PV must be numeric, not boolean, at {timestamp}")
        value = float(raw_value)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"PV must be finite and non-negative at {timestamp}")
        actual[timestamp] = value
    return actual


def build_pv_series(actual: Mapping[dt.datetime, float]) -> PVSeries:
    if not actual:
        raise ValueError("attachment 2 contains no usable PV observations")
    timestamps = sorted(actual)
    if any(
        timestamp.second or timestamp.microsecond or timestamp.minute % STEP_MINUTES
        for timestamp in timestamps
    ):
        raise ValueError("PV timestamps must be aligned to ten-minute endpoints")
    step = dt.timedelta(minutes=STEP_MINUTES)
    for previous, current in zip(timestamps[:-1], timestamps[1:], strict=True):
        if current - previous != step:
            raise ValueError(
                "PV observations must form one continuous ten-minute grid; "
                f"gap between {previous} and {current}"
            )
    values = np.asarray([actual[timestamp] for timestamp in timestamps], dtype=float)
    if not np.all(np.isfinite(values)) or np.any(values < 0):
        raise ValueError("PV values must be finite and non-negative")
    return PVSeries(start=timestamps[0], values_kw=values)


def predict_point(
    *,
    series: PVSeries,
    candidate: CandidateSpec,
    decision_time: dt.datetime,
    target_time: dt.datetime,
) -> float:
    decision_index = series.index_at(decision_time)
    target_index = series.index_at(target_time)
    if target_index <= decision_index:
        raise ValueError("tail target must be after decision_time")
    lag_indices = target_index - np.asarray(candidate.lag_days) * STEPS_PER_DAY
    if np.any(lag_indices < 0):
        raise ValueError("not enough PV lag history for tail target")
    if np.any(lag_indices > decision_index):
        raise ValueError("PV tail candidate would read actual data after decision_time")
    lag_values = series.values_kw[lag_indices]
    if candidate.aggregation == "median":
        return float(np.median(lag_values))
    return float(np.dot(np.asarray(candidate.weights), lag_values))


def _snapshot_issue_times(start: dt.date, end: dt.date) -> tuple[dt.datetime, ...]:
    times: list[dt.datetime] = []
    day = start
    while day < end:
        times.extend(dt.datetime.combine(day, dt.time(hour)) for hour in ISSUE_HOURS)
        day += dt.timedelta(days=1)
    return tuple(times)


def build_evaluation_rows(
    *,
    series: PVSeries,
    candidates: tuple[CandidateSpec, ...],
    evaluation_start: dt.date,
    evaluation_end: dt.date,
) -> tuple[tuple[EvaluationRow, ...], dict[str, object]]:
    rows: list[EvaluationRow] = []
    scheduled_calls = 0
    scheduled_targets = 0
    missing_targets = 0
    missing_by_issue = {str(hour): 0 for hour in ISSUE_HOURS}
    for issue_time in _snapshot_issue_times(evaluation_start, evaluation_end):
        coverage_end = issue_time + dt.timedelta(hours=24)
        for age_step in range(1, NON_RELEASE_STEPS + 1):
            decision_time = issue_time + dt.timedelta(minutes=STEP_MINUTES * age_step)
            try:
                series.index_at(decision_time)
            except KeyError:
                continue
            scheduled_calls += 1
            for tail_step in range(1, age_step + 1):
                scheduled_targets += 1
                target_time = coverage_end + dt.timedelta(minutes=STEP_MINUTES * tail_step)
                try:
                    target_index = series.index_at(target_time)
                except KeyError:
                    missing_targets += 1
                    missing_by_issue[str(issue_time.hour)] += 1
                    continue
                predictions = {
                    candidate.name: predict_point(
                        series=series,
                        candidate=candidate,
                        decision_time=decision_time,
                        target_time=target_time,
                    )
                    for candidate in candidates
                }
                rows.append(
                    EvaluationRow(
                        snapshot_issue_time=issue_time,
                        decision_time=decision_time,
                        target_time=target_time,
                        tail_step=tail_step,
                        actual_kw=float(series.values_kw[target_index]),
                        predictions_kw=predictions,
                    )
                )
    if not rows:
        raise ValueError("no common PV tail evaluation rows are available")
    unique_targets = {(row.snapshot_issue_time, row.target_time) for row in rows}
    return tuple(rows), {
        "scheduled_snapshot_issues": len(_snapshot_issue_times(evaluation_start, evaluation_end)),
        "evaluated_non_release_mpc_calls": scheduled_calls,
        "scheduled_call_weighted_targets": scheduled_targets,
        "common_call_weighted_targets_per_method": len(rows),
        "common_unique_snapshot_targets_per_method": len(unique_targets),
        "missing_targets": missing_targets,
        "missing_targets_by_snapshot_issue_hour": missing_by_issue,
    }


def error_metrics(rows: tuple[EvaluationRow, ...], method: str) -> dict[str, float | int]:
    predictions = np.asarray([row.predictions_kw[method] for row in rows], dtype=float)
    actual = np.asarray([row.actual_kw for row in rows], dtype=float)
    errors = predictions - actual
    return {
        "n": len(rows),
        "mae_kw": float(np.mean(np.abs(errors))),
        "rmse_kw": float(np.sqrt(np.mean(np.square(errors)))),
        "bias_kw": float(np.mean(errors)),
    }


def unique_target_rows(rows: tuple[EvaluationRow, ...]) -> tuple[EvaluationRow, ...]:
    first_by_target: dict[tuple[dt.datetime, dt.datetime], EvaluationRow] = {}
    for row in rows:
        first_by_target.setdefault((row.snapshot_issue_time, row.target_time), row)
    return tuple(first_by_target[key] for key in sorted(first_by_target))


def paired_daily_bootstrap(
    rows: tuple[EvaluationRow, ...],
    *,
    candidate_method: str,
    baseline_method: str,
    replicates: int,
    seed: int,
) -> dict[str, float | int]:
    by_day: dict[dt.date, list[float]] = defaultdict(list)
    for row in rows:
        difference = abs(row.predictions_kw[candidate_method] - row.actual_kw) - abs(
            row.predictions_kw[baseline_method] - row.actual_kw
        )
        by_day[row.decision_time.date()].append(difference)
    days = sorted(by_day)
    sums = np.asarray([sum(by_day[day]) for day in days], dtype=float)
    counts = np.asarray([len(by_day[day]) for day in days], dtype=float)
    rng = np.random.default_rng(seed)
    sampled = rng.integers(0, len(days), size=(replicates, len(days)))
    differences = sums[sampled].sum(axis=1) / counts[sampled].sum(axis=1)
    lower, upper = np.quantile(differences, (0.025, 0.975))
    return {
        "day_blocks": len(days),
        "replicates": replicates,
        "mae_difference_kw": float(sums.sum() / counts.sum()),
        "ci95_low_kw": float(lower),
        "ci95_high_kw": float(upper),
    }


def evaluate(args: argparse.Namespace) -> dict[str, object]:
    if args.evaluation_start >= args.evaluation_end:
        raise ValueError("--evaluation-start must be before --evaluation-end")
    if not args.evaluation_start < args.holdout_start < args.evaluation_end:
        raise ValueError("--holdout-start must lie inside the evaluation period")
    if args.bootstrap_replicates <= 0:
        raise ValueError("--bootstrap-replicates must be positive")

    candidates = candidate_specs()
    series = build_pv_series(load_actual(args.actual))
    rows, sample = build_evaluation_rows(
        series=series,
        candidates=candidates,
        evaluation_start=args.evaluation_start,
        evaluation_end=args.evaluation_end,
    )
    unique_rows = unique_target_rows(rows)
    development = tuple(row for row in rows if row.decision_time.date() < args.holdout_start)
    holdout = tuple(row for row in rows if row.decision_time.date() >= args.holdout_start)
    positive = tuple(row for row in rows if row.actual_kw > 0)
    if not development or not holdout or not positive:
        raise ValueError("development, holdout and positive-PV samples must be non-empty")

    methods = {candidate.name: error_metrics(rows, candidate.name) for candidate in candidates}
    return {
        "schema_version": 1,
        "status": "candidate_evidence_only",
        "does_not_approve": ["D_PV_TAIL_BASELINE", "D_MODEL_Q3"],
        "formal_runner_input": False,
        "audit_script": {
            "path": "scripts/audit_q3_pv_tail_candidates.py",
            "sha256": sha256_file(Path(__file__)),
        },
        "source": {
            "actual_file": args.actual.name,
            "actual_sha256": sha256_file(args.actual),
            "sheet_name": "光伏发电实际功率",
            "unit": "kW",
        },
        "data_checks": {
            "pv_record_count": len(series.values_kw),
            "first_interval_end": series.start.isoformat(),
            "last_interval_end": series.end.isoformat(),
            "grid_minutes": STEP_MINUTES,
            "continuous_grid": True,
            "duplicate_timestamps": 0,
            "non_finite_values": 0,
            "negative_values": 0,
        },
        "evaluation": {
            "evaluation_start": args.evaluation_start.isoformat(),
            "evaluation_end_exclusive": args.evaluation_end.isoformat(),
            "holdout_start": args.holdout_start.isoformat(),
            "snapshot_issue_hours": list(ISSUE_HOURS),
            "tail_steps_per_cycle": f"1..{NON_RELEASE_STEPS}",
            "maximum_tail_length_minutes": NON_RELEASE_STEPS * STEP_MINUTES,
            "target_horizon_from_decision": "18h20m through 24h",
            "visibility_rule": "every lagged actual interval endpoint <= decision_time",
            "grain": "(snapshot_issue_time, decision_time, target_time)",
            "call_weighted_interpretation": (
                "matches every value requested by each intermediate ten-minute MPC call"
            ),
            "unique_target_interpretation": (
                "one row per snapshot and missing tail target; avoids repeated-call weighting"
            ),
            "holdout_interpretation": (
                "date-split diagnostic only; no claim of untouched model-selection holdout"
            ),
            **sample,
        },
        "candidate_contracts": {
            candidate.name: {
                "lag_days": list(candidate.lag_days),
                "aggregation": candidate.aggregation,
                "weights": list(candidate.weights),
                "description": candidate.description,
            }
            for candidate in candidates
        },
        "reference_method": REFERENCE_METHOD,
        "call_weighted_metrics": methods,
        "unique_snapshot_target_metrics": {
            candidate.name: error_metrics(unique_rows, candidate.name) for candidate in candidates
        },
        "actual_positive_metrics": {
            candidate.name: error_metrics(positive, candidate.name) for candidate in candidates
        },
        "development_holdout_metrics": {
            candidate.name: {
                "development": error_metrics(development, candidate.name),
                "holdout": error_metrics(holdout, candidate.name),
            }
            for candidate in candidates
        },
        "by_snapshot_issue_hour": {
            candidate.name: {
                str(hour): error_metrics(
                    tuple(row for row in rows if row.snapshot_issue_time.hour == hour),
                    candidate.name,
                )
                for hour in ISSUE_HOURS
            }
            for candidate in candidates
        },
        "paired_comparisons_vs_1d": {
            candidate.name: paired_daily_bootstrap(
                rows,
                candidate_method=candidate.name,
                baseline_method=REFERENCE_METHOD,
                replicates=args.bootstrap_replicates,
                seed=args.bootstrap_seed,
            )
            for candidate in candidates
            if candidate.name != REFERENCE_METHOD
        },
        "leading_candidate_vs_mean_7d": paired_daily_bootstrap(
            rows,
            candidate_method=EXP2_METHOD,
            baseline_method=SIMPLE_MEAN_METHOD,
            replicates=args.bootstrap_replicates,
            seed=args.bootstrap_seed,
        ),
        "bootstrap": {
            "block": "decision calendar day",
            "replicates": args.bootstrap_replicates,
            "seed": args.bootstrap_seed,
            "estimand": "candidate MAE minus 1-day seasonal-naive MAE; negative is better",
        },
        "decision_guard": (
            "metrics are evidence for human review only and do not select or approve a baseline"
        ),
    }


def render_json(result: Mapping[str, object]) -> str:
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
