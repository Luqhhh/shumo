"""Audit Q3 forecast-vintage combination candidates on official data.

This evidence-only script compares visible-version combination formulas at
hourly valid times.  It does not resample, approve ``D_MODEL_Q3``, or provide
inputs to a formal case runner.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np

from microgrid.dataio import read_forecasts, read_wide_attachment, sha256_file

HistoryGrouping = Literal["lead_hours", "issue_clock_and_lead_hours"]
HistoryWindow = Literal["expanding", "rolling_56_days"]
LEAD_BUCKETS = ((1, 6), (7, 12), (13, 18), (19, 24))


@dataclass
class ErrorTotals:
    count: int = 0
    absolute: float = 0.0
    squared: float = 0.0
    signed: float = 0.0

    def add(self, error_kw: float) -> None:
        self.count += 1
        self.absolute += abs(error_kw)
        self.squared += error_kw**2
        self.signed += error_kw

    def metrics(self) -> dict[str, float | int]:
        if self.count == 0:
            raise ValueError("cannot summarize an empty method")
        return {
            "n": self.count,
            "mae_kw": self.absolute / self.count,
            "rmse_kw": float(np.sqrt(self.squared / self.count)),
            "bias_kw": self.signed / self.count,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actual", type=Path, required=True, help="附件2 workbook path")
    parser.add_argument("--forecast", type=Path, required=True, help="附件3 workbook path")
    parser.add_argument(
        "--epsilon-kw",
        type=float,
        nargs="+",
        required=True,
        help="One or more explicit epsilon values for inverse-error weights",
    )
    parser.add_argument(
        "--evaluation-start",
        type=dt.date.fromisoformat,
        default=dt.date(2025, 2, 1),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/evidence/q3_forecast_versions.json"),
    )
    parser.add_argument("--bootstrap-replicates", type=int, default=2000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260911)
    return parser.parse_args()


def load_actual(path: Path) -> dict[dt.datetime, float]:
    rows = read_wide_attachment(
        path,
        sheet_name="光伏发电实际功率",
        kind="pv_actual_kw",
        unit="kW",
    )
    actual: dict[dt.datetime, float] = {}
    for row in rows:
        timestamp = dt.datetime.fromisoformat(row["parsed_timestamp"])
        if timestamp in actual:
            raise ValueError(f"duplicate actual timestamp: {timestamp}")
        value = float(row["value"])
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"actual PV must be finite and non-negative at {timestamp}")
        actual[timestamp] = value
    return actual


def validate_forecasts(forecasts: list[Any]) -> None:
    expected_leads = set(range(1, 25))
    issue_leads: dict[dt.datetime, set[int]] = defaultdict(set)
    seen: set[tuple[dt.datetime, int]] = set()
    for record in forecasts:
        key = (record.issue_time, record.lead_hours)
        if key in seen:
            raise ValueError(f"duplicate forecast issue/lead key: {key}")
        seen.add(key)
        issue_leads[record.issue_time].add(record.lead_hours)
        if record.valid_time != record.issue_time + dt.timedelta(hours=record.lead_hours):
            raise ValueError(f"forecast lead/valid_time mismatch: {key}")
        value = float(record.pv_forecast_kw)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"forecast PV must be finite and non-negative at {key}")

    for issue_time, leads in issue_leads.items():
        if leads != expected_leads:
            raise ValueError(
                f"forecast issue {issue_time} has missing={sorted(expected_leads - leads)} "
                f"extra={sorted(leads - expected_leads)}"
            )
    issue_hours = {timestamp.hour for timestamp in issue_leads}
    if issue_hours != {0, 6, 12, 18}:
        raise ValueError(f"unexpected forecast issue hours: {sorted(issue_hours)}")


def history_key(record: Any, grouping: HistoryGrouping) -> int | tuple[int, int]:
    if grouping == "lead_hours":
        return record.lead_hours
    return record.issue_time.hour, record.lead_hours


def prepare_history(forecasts: list[Any], actual: dict[dt.datetime, float]):
    histories: dict[
        HistoryGrouping,
        dict[int | tuple[int, int], tuple[np.ndarray, np.ndarray]],
    ] = {}
    for grouping in ("lead_hours", "issue_clock_and_lead_hours"):
        rows_by_key: dict[int | tuple[int, int], list[tuple[dt.datetime, float]]] = defaultdict(
            list
        )
        for record in forecasts:
            actual_kw = actual.get(record.valid_time)
            if actual_kw is None:
                continue
            rows_by_key[history_key(record, grouping)].append(
                (record.valid_time, abs(record.pv_forecast_kw - actual_kw))
            )
        arrays = {}
        for key, rows in rows_by_key.items():
            rows.sort(key=lambda item: item[0])
            arrays[key] = (
                np.array([np.datetime64(item[0]) for item in rows]),
                np.array([item[1] for item in rows], dtype=float),
            )
        histories[grouping] = arrays
    return histories


def historical_mae(
    times: np.ndarray,
    errors: np.ndarray,
    decision_time: dt.datetime,
    window: HistoryWindow,
) -> tuple[float, int]:
    cutoff = np.datetime64(decision_time)
    end = int(np.searchsorted(times, cutoff, side="right"))
    start = 0
    if window == "rolling_56_days":
        lower = np.datetime64(decision_time - dt.timedelta(days=56))
        start = int(np.searchsorted(times, lower, side="right"))
    selected = errors[start:end]
    if len(selected) == 0:
        raise ValueError(f"no causal history at {decision_time} for {window}")
    return float(np.mean(selected)), len(selected)


def weighted_prediction(
    candidates: list[Any],
    histories: dict[HistoryGrouping, dict[Any, tuple[np.ndarray, np.ndarray]]],
    *,
    decision_time: dt.datetime,
    grouping: HistoryGrouping,
    window: HistoryWindow,
    epsilon_kw: float,
    power: int,
) -> tuple[float, int]:
    scores = []
    counts = []
    for candidate in candidates:
        times, errors = histories[grouping][history_key(candidate, grouping)]
        score, count = historical_mae(times, errors, decision_time, window)
        scores.append(score)
        counts.append(count)
    denominators = np.array(scores, dtype=float) + epsilon_kw
    if np.any(denominators <= 0):
        raise ValueError("historical MAE + epsilon must be positive")
    raw_weights = 1.0 / np.power(denominators, power)
    if not np.all(np.isfinite(raw_weights)) or float(np.sum(raw_weights)) <= 0:
        raise ValueError("inverse-error weights must be finite with a positive sum")
    values = np.array([candidate.pv_forecast_kw for candidate in candidates], dtype=float)
    return float(np.average(values, weights=raw_weights)), min(counts)


def method_name(
    grouping: HistoryGrouping,
    window: HistoryWindow,
    epsilon_kw: float,
    power: int,
) -> str:
    return f"inverse_power_{power}__{grouping}__{window}__epsilon_{epsilon_kw!r}_kw"


def lead_bucket(lead_hours: int) -> str:
    for lower, upper in LEAD_BUCKETS:
        if lower <= lead_hours <= upper:
            return f"lead_{lower}_{upper}_hours"
    raise ValueError(f"lead_hours must be in 1..24, got {lead_hours}")


def add_error(
    *,
    totals: dict[str, ErrorTotals],
    bucket_totals: dict[str, dict[str, ErrorTotals]],
    active_pv_totals: dict[str, ErrorTotals],
    daily_totals: dict[str, dict[dt.date, ErrorTotals]],
    method: str,
    error_kw: float,
    lead_hours: int,
    decision_day: dt.date,
    actual_kw: float,
) -> None:
    totals.setdefault(method, ErrorTotals()).add(error_kw)
    bucket_totals.setdefault(method, {}).setdefault(lead_bucket(lead_hours), ErrorTotals()).add(
        error_kw
    )
    daily_totals.setdefault(method, {}).setdefault(decision_day, ErrorTotals()).add(error_kw)
    if actual_kw > 0:
        active_pv_totals.setdefault(method, ErrorTotals()).add(error_kw)


def paired_daily_bootstrap(
    daily_totals: dict[str, dict[dt.date, ErrorTotals]],
    *,
    method: str,
    replicates: int,
    seed: int,
) -> dict[str, float]:
    baseline = daily_totals["latest_only"]
    candidate = daily_totals[method]
    days = sorted(baseline)
    if set(candidate) != set(days):
        raise AssertionError(f"daily comparison population differs for {method}")

    baseline_absolute = np.array([baseline[day].absolute for day in days])
    candidate_absolute = np.array([candidate[day].absolute for day in days])
    baseline_count = np.array([baseline[day].count for day in days], dtype=float)
    candidate_count = np.array([candidate[day].count for day in days], dtype=float)
    if not np.array_equal(baseline_count, candidate_count):
        raise AssertionError(f"daily comparison counts differ for {method}")

    rng = np.random.default_rng(seed)
    sampled = rng.integers(0, len(days), size=(replicates, len(days)))
    denominator = baseline_count[sampled].sum(axis=1)
    differences = (
        candidate_absolute[sampled].sum(axis=1) / denominator
        - baseline_absolute[sampled].sum(axis=1) / denominator
    )
    lower, upper = np.quantile(differences, [0.025, 0.975])
    return {
        "mae_difference_vs_latest_kw": (
            candidate_absolute.sum() / candidate_count.sum()
            - baseline_absolute.sum() / baseline_count.sum()
        ),
        "ci95_low_kw": float(lower),
        "ci95_high_kw": float(upper),
    }


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    if any(not math.isfinite(epsilon) or epsilon <= 0 for epsilon in args.epsilon_kw):
        raise ValueError(
            "--epsilon-kw values must be finite and positive; zero is singular when a "
            "historical MAE is zero"
        )
    if args.bootstrap_replicates <= 0:
        raise ValueError("--bootstrap-replicates must be positive")
    epsilon_values = tuple(dict.fromkeys(float(value) for value in args.epsilon_kw))
    actual = load_actual(args.actual)
    forecasts = read_forecasts(args.forecast)
    validate_forecasts(forecasts)
    by_valid: dict[dt.datetime, list[Any]] = defaultdict(list)
    by_issue: dict[dt.datetime, list[Any]] = defaultdict(list)
    for record in forecasts:
        by_valid[record.valid_time].append(record)
        by_issue[record.issue_time].append(record)
    for records in by_valid.values():
        records.sort(key=lambda record: record.issue_time)

    histories = prepare_history(forecasts, actual)
    totals: dict[str, ErrorTotals] = {}
    bucket_totals: dict[str, dict[str, ErrorTotals]] = {}
    active_pv_totals: dict[str, ErrorTotals] = {}
    daily_totals: dict[str, dict[dt.date, ErrorTotals]] = {}
    min_history: dict[str, int] = {}
    evaluation_start = dt.datetime.combine(args.evaluation_start, dt.time())
    evaluated_targets = 0
    scheduled_targets = 0
    missing_actual_targets = 0
    missing_actual_by_lead_bucket: dict[str, int] = defaultdict(int)
    visible_version_count: dict[int, int] = defaultdict(int)
    first_decision_time: dt.datetime | None = None
    last_decision_time: dt.datetime | None = None
    first_valid_time: dt.datetime | None = None
    last_valid_time: dt.datetime | None = None

    for decision_time in sorted(by_issue):
        if decision_time < evaluation_start:
            continue
        for current in sorted(by_issue[decision_time], key=lambda record: record.lead_hours):
            scheduled_targets += 1
            actual_kw = actual.get(current.valid_time)
            if actual_kw is None:
                missing_actual_targets += 1
                missing_actual_by_lead_bucket[lead_bucket(current.lead_hours)] += 1
                continue
            visible = [
                record
                for record in by_valid[current.valid_time]
                if record.issue_time <= decision_time
            ]
            if not visible:
                raise AssertionError("the current publication must be visible")
            visible_version_count[len(visible)] += 1
            evaluated_targets += 1
            first_decision_time = first_decision_time or decision_time
            last_decision_time = decision_time
            first_valid_time = first_valid_time or current.valid_time
            last_valid_time = max(last_valid_time or current.valid_time, current.valid_time)
            latest_kw = float(visible[-1].pv_forecast_kw)
            mean_kw = float(np.mean([record.pv_forecast_kw for record in visible]))
            add_error(
                totals=totals,
                bucket_totals=bucket_totals,
                active_pv_totals=active_pv_totals,
                daily_totals=daily_totals,
                method="latest_only",
                error_kw=latest_kw - actual_kw,
                lead_hours=current.lead_hours,
                decision_day=decision_time.date(),
                actual_kw=actual_kw,
            )
            add_error(
                totals=totals,
                bucket_totals=bucket_totals,
                active_pv_totals=active_pv_totals,
                daily_totals=daily_totals,
                method="simple_average",
                error_kw=mean_kw - actual_kw,
                lead_hours=current.lead_hours,
                decision_day=decision_time.date(),
                actual_kw=actual_kw,
            )

            for grouping in ("lead_hours", "issue_clock_and_lead_hours"):
                for window in ("expanding", "rolling_56_days"):
                    for epsilon_kw in epsilon_values:
                        for power in (1, 2):
                            name = method_name(grouping, window, epsilon_kw, power)
                            prediction_kw, history_count = weighted_prediction(
                                visible,
                                histories,
                                decision_time=decision_time,
                                grouping=grouping,
                                window=window,
                                epsilon_kw=epsilon_kw,
                                power=power,
                            )
                            add_error(
                                totals=totals,
                                bucket_totals=bucket_totals,
                                active_pv_totals=active_pv_totals,
                                daily_totals=daily_totals,
                                method=name,
                                error_kw=prediction_kw - actual_kw,
                                lead_hours=current.lead_hours,
                                decision_day=decision_time.date(),
                                actual_kw=actual_kw,
                            )
                            previous = min_history.get(name)
                            min_history[name] = (
                                history_count if previous is None else min(previous, history_count)
                            )

    if not evaluated_targets:
        raise ValueError("no targets were available in the requested evaluation period")
    assert first_decision_time is not None
    assert last_decision_time is not None
    assert first_valid_time is not None
    assert last_valid_time is not None

    leaderboard = []
    for name, method_totals in totals.items():
        row = {
            "method": name,
            **method_totals.metrics(),
            "actual_pv_positive": active_pv_totals[name].metrics(),
            "by_lead_bucket": {
                lead_bucket(lower): bucket_totals[name][lead_bucket(lower)].metrics()
                for lower, _upper in LEAD_BUCKETS
            },
            "paired_day_block_bootstrap": paired_daily_bootstrap(
                daily_totals,
                method=name,
                replicates=args.bootstrap_replicates,
                seed=args.bootstrap_seed,
            ),
        }
        if name in min_history:
            row["minimum_history_count"] = min_history[name]
        leaderboard.append(row)
    leaderboard.sort(key=lambda row: (row["mae_kw"], row["rmse_kw"], row["method"]))

    return {
        "schema_version": 2,
        "status": "candidate_evidence_only",
        "does_not_approve": "D_MODEL_Q3",
        "audit_script": {
            "path": "scripts/audit_q3_forecast_versions.py",
            "sha256": sha256_file(Path(__file__)),
        },
        "source": {
            "actual_file": args.actual.name,
            "actual_sha256": sha256_file(args.actual),
            "forecast_file": args.forecast.name,
            "forecast_sha256": sha256_file(args.forecast),
        },
        "data_checks": {
            "actual_records": len(actual),
            "forecast_records": len(forecasts),
            "forecast_issue_times": len(by_issue),
            "issue_hours": sorted({timestamp.hour for timestamp in by_issue}),
            "maximum_visible_versions": max(len(records) for records in by_valid.values()),
        },
        "evaluation": {
            "start": evaluation_start.isoformat(),
            "first_decision_time": first_decision_time.isoformat(),
            "last_decision_time": last_decision_time.isoformat(),
            "first_valid_time": first_valid_time.isoformat(),
            "last_valid_time": last_valid_time.isoformat(),
            "scheduled_hourly_targets": scheduled_targets,
            "hourly_target_count_per_method": evaluated_targets,
            "missing_actual_targets": missing_actual_targets,
            "missing_actual_by_lead_bucket": {
                lead_bucket(lower): missing_actual_by_lead_bucket.get(lead_bucket(lower), 0)
                for lower, _upper in LEAD_BUCKETS
            },
            "visible_version_count_distribution": dict(sorted(visible_version_count.items())),
            "timestamp_interpretation": "Asia/Shanghai local time stored without offset",
            "visibility": "issue_time <= decision_time",
            "history_cutoff": "forecast-error valid_time <= decision_time",
            "history_c_56_boundary": ("decision_time - 56 days < valid_time <= decision_time"),
            "weight_formula": ("normalize(1 / (historical_mae_kw + epsilon_kw) ** power)"),
            "error_definition": (
                "forecast point power minus actual ten-minute interval mean "
                "whose right endpoint is valid_time"
            ),
            "actual_pv_positive_filter": "actual_kw > 0; diagnostic only",
            "evaluation_grain": "(decision_time, valid_time)",
            "epsilon_kw_values": epsilon_values,
            "weight_powers": [1, 2],
            "history_groupings": ["lead_hours", "issue_clock_and_lead_hours"],
            "history_windows": ["expanding", "rolling_56_days"],
            "lead_buckets_hours": [list(bucket) for bucket in LEAD_BUCKETS],
            "paired_day_block_bootstrap": {
                "block": "decision calendar day",
                "replicates": args.bootstrap_replicates,
                "seed": args.bootstrap_seed,
                "estimand": "candidate MAE minus latest-only MAE; negative is better",
            },
            "interpretation_guard": (
                "Candidates are compared on one fixed 2025 evaluation population. "
                "Do not describe the smallest epsilon difference as independently "
                "held-out superiority without a nested or later-period evaluation."
            ),
        },
        "leaderboard": leaderboard,
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
