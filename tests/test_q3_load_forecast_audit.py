from __future__ import annotations

import datetime as dt
import importlib.util
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "audit_q3_load_forecast_candidates.py"
_SPEC = importlib.util.spec_from_file_location("q3_load_forecast_audit_script", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - importlib platform guard
    raise RuntimeError(f"cannot load audit script: {_SCRIPT_PATH}")
_AUDIT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _AUDIT
_SPEC.loader.exec_module(_AUDIT)


def _synthetic_actual(*, days: int = 35) -> dict[dt.datetime, float]:
    start = dt.datetime(2025, 1, 1)
    points = days * _AUDIT.SLOTS_PER_DAY + 1
    return {
        start + dt.timedelta(minutes=_AUDIT.STEP_MINUTES * index): (
            2_000.0
            + 150.0 * math.sin(2.0 * math.pi * (index % _AUDIT.SLOTS_PER_DAY) / 144)
            + 0.2 * (index // _AUDIT.SLOTS_PER_DAY)
            + 8.0 * math.sin(index / 13.0)
        )
        for index in range(points)
    }


def _candidate_and_state(actual: dict[dt.datetime, float]):
    series = _AUDIT.build_load_series(actual)
    candidate = next(
        item
        for item in _AUDIT.candidate_specs((0.5, 1.0, 2.0))
        if item.name == _AUDIT.PREFERRED_METHOD
    )
    return series, candidate, _AUDIT.fit_candidate_state(series, candidate)


def test_one_week_half_life_has_expected_four_week_weights() -> None:
    weights = _AUDIT.exponential_lag_weights(1.0)

    assert weights == pytest.approx((8 / 15, 4 / 15, 2 / 15, 1 / 15))
    assert sum(weights) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="include 1.0"):
        _AUDIT.candidate_specs((0.5, 2.0))


def test_prediction_is_unchanged_when_future_actuals_are_modified() -> None:
    actual = _synthetic_actual()
    decision_time = dt.datetime(2025, 1, 30, 6)
    target_time = decision_time + dt.timedelta(hours=6)
    original_series, candidate, original_state = _candidate_and_state(actual)

    changed = dict(actual)
    for timestamp in tuple(changed):
        if timestamp > decision_time:
            changed[timestamp] += 100_000.0
    changed_series, changed_candidate, changed_state = _candidate_and_state(changed)

    original = _AUDIT.predict_point(
        series=original_series,
        candidate=candidate,
        state=original_state,
        decision_time=decision_time,
        target_time=target_time,
    )
    modified = _AUDIT.predict_point(
        series=changed_series,
        candidate=changed_candidate,
        state=changed_state,
        decision_time=decision_time,
        target_time=target_time,
    )

    assert modified.power_kw == pytest.approx(original.power_kw)
    assert modified.phi == pytest.approx(original.phi)
    assert modified.latest_visible_residual_kw == pytest.approx(original.latest_visible_residual_kw)


def test_prediction_uses_exact_7_14_21_28_day_target_lags() -> None:
    series = _AUDIT.build_load_series(_synthetic_actual())
    candidate = _AUDIT.CandidateSpec(
        name="hand_checked",
        lag_weights=(0.4, 0.3, 0.2, 0.1),
        description="test candidate",
    )
    zeros = np.zeros(len(series.values_kw), dtype=float)
    state = _AUDIT.CandidateState(zeros, zeros, zeros)
    decision_time = dt.datetime(2025, 1, 30)
    target_time = decision_time + dt.timedelta(hours=6)
    target_index = series.index_at(target_time)
    expected = sum(
        weight * series.values_kw[target_index - lag]
        for weight, lag in zip(candidate.lag_weights, _AUDIT.LAG_SLOTS, strict=True)
    )

    prediction = _AUDIT.predict_point(
        series=series,
        candidate=candidate,
        state=state,
        decision_time=decision_time,
        target_time=target_time,
    )

    assert prediction.power_kw == pytest.approx(expected)


def test_each_candidate_starts_residual_history_at_its_longest_active_lag() -> None:
    series = _AUDIT.build_load_series(_synthetic_actual())
    candidates = {
        candidate.name: candidate for candidate in _AUDIT.candidate_specs((0.5, 1.0, 2.0))
    }
    lag7_state = _AUDIT.fit_candidate_state(series, candidates[_AUDIT.REFERENCE_METHOD])
    load_a_state = _AUDIT.fit_candidate_state(series, candidates[_AUDIT.PREFERRED_METHOD])

    assert np.isnan(lag7_state.residual_kw[_AUDIT.LAG_SLOTS[0] - 1])
    assert np.isfinite(lag7_state.residual_kw[_AUDIT.LAG_SLOTS[0]])
    assert np.isnan(load_a_state.residual_kw[_AUDIT.LAG_SLOTS[-1] - 1])
    assert np.isfinite(load_a_state.residual_kw[_AUDIT.LAG_SLOTS[-1]])
    assert lag7_state.ar_denominator[_AUDIT.LAG_SLOTS[-1]] > 0
    assert load_a_state.ar_denominator[_AUDIT.LAG_SLOTS[-1]] == 0


def test_load_series_rejects_a_missing_ten_minute_interval() -> None:
    actual = _synthetic_actual(days=1)
    del actual[dt.datetime(2025, 1, 1, 0, 20)]

    with pytest.raises(ValueError, match="continuous ten-minute grid"):
        _AUDIT.build_load_series(actual)


def test_q3_rows_use_36_steps_per_issue_and_midnight_lf_b_origin() -> None:
    series = _AUDIT.build_load_series(_synthetic_actual())
    candidates = _AUDIT.candidate_specs((0.5, 1.0, 2.0))
    states = {
        candidate.name: _AUDIT.fit_candidate_state(series, candidate) for candidate in candidates
    }

    rows, sample = _AUDIT.build_evaluation_rows(
        series=series,
        candidates=candidates,
        states=states,
        evaluation_start=dt.date(2025, 1, 30),
        evaluation_end=dt.date(2025, 1, 31),
    )

    assert len(rows) == 4 * 36
    assert sample["common_target_count_per_method"] == 144
    assert sample["missing_targets"] == 0
    assert {row.issue_hour for row in rows} == {0, 6, 12, 18}
    assert all(1 <= row.horizon_steps <= 36 for row in rows)

    row = next(
        item
        for item in rows
        if item.decision_time == dt.datetime(2025, 1, 30, 6)
        and item.target_time == dt.datetime(2025, 1, 30, 6, 10)
    )
    direct_midnight = _AUDIT.predict_point(
        series=series,
        candidate=candidates[0],
        state=states[candidates[0].name],
        decision_time=dt.datetime(2025, 1, 30),
        target_time=row.target_time,
    )
    assert row.midnight_predictions[candidates[0].name] == direct_midnight


def test_paired_bootstrap_uses_decision_day_and_candidate_minus_baseline_sign() -> None:
    def prediction(value: float) -> object:
        return _AUDIT.Prediction(value, value, 0.0, 0.0, False)

    rows = tuple(
        _AUDIT.EvaluationRow(
            decision_time=dt.datetime(2025, 2, day),
            target_time=dt.datetime(2025, 2, day, 0, 10),
            issue_hour=0,
            horizon_steps=1,
            actual_kw=100.0,
            updated_predictions={"candidate": prediction(90.0), "baseline": prediction(80.0)},
            midnight_predictions={"candidate": prediction(90.0), "baseline": prediction(80.0)},
        )
        for day in (1, 2)
    )

    result = _AUDIT.paired_daily_bootstrap(
        rows,
        candidate_method="candidate",
        baseline_method="baseline",
        candidate_midnight=False,
        baseline_midnight=False,
        replicates=50,
        seed=7,
    )

    assert result["day_blocks"] == 2
    assert result["mae_difference_kw"] == pytest.approx(-10.0)
    assert result["ci95_low_kw"] == pytest.approx(-10.0)
    assert result["ci95_high_kw"] == pytest.approx(-10.0)


def test_evidence_json_is_deterministic_and_marks_pending_decisions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    actual = _synthetic_actual()
    monkeypatch.setattr(_AUDIT, "load_actual", lambda _path: actual)
    monkeypatch.setattr(_AUDIT, "sha256_file", lambda _path: "a" * 64)
    args = SimpleNamespace(
        actual=tmp_path / "附件2.xlsx",
        evaluation_start=dt.date(2025, 1, 30),
        evaluation_end=dt.date(2025, 2, 2),
        holdout_start=dt.date(2025, 2, 1),
        half_life_weeks=(0.5, 1.0, 2.0),
        bootstrap_replicates=50,
        bootstrap_seed=20260912,
    )

    first = _AUDIT.evaluate(args)
    second = _AUDIT.evaluate(args)

    assert _AUDIT.render_json(first) == _AUDIT.render_json(second)
    assert first["status"] == "candidate_evidence_only"
    assert first["does_not_approve"] == ["D_LOAD_FORECAST", "D_MODEL_Q3"]
    assert first["formal_runner_input"] is False
    assert first["evaluation"]["common_target_count_per_method"] == 3 * 4 * 36
    assert all(metrics["n"] == 3 * 4 * 36 for metrics in first["q3_next_6h_metrics"].values())
    assert first["bootstrap"]["block"] == "decision calendar day"
