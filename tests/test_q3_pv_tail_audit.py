from __future__ import annotations

import datetime as dt
import importlib.util
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "audit_q3_pv_tail_candidates.py"
_SPEC = importlib.util.spec_from_file_location("q3_pv_tail_audit_script", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - importlib platform guard
    raise RuntimeError(f"cannot load audit script: {_SCRIPT_PATH}")
_AUDIT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _AUDIT
_SPEC.loader.exec_module(_AUDIT)


def _synthetic_actual(*, days: int = 45) -> dict[dt.datetime, float]:
    start = dt.datetime(2025, 1, 1, 0, 10)
    return {
        start + dt.timedelta(minutes=10 * index): max(
            0.0,
            800.0
            * math.sin(
                math.pi * ((index + 1) % _AUDIT.STEPS_PER_DAY - 36) / (_AUDIT.STEPS_PER_DAY - 72)
            )
            + 10.0 * math.sin(index / 17.0),
        )
        for index in range(days * _AUDIT.STEPS_PER_DAY)
    }


def test_candidate_definitions_are_fixed_and_normalized() -> None:
    candidates = {candidate.name: candidate for candidate in _AUDIT.candidate_specs()}

    assert set(candidates) == {
        "seasonal_naive_1d",
        "mean_3d",
        "mean_7d",
        "median_7d",
        "exp_half_life_2d",
        "weekly_7_14_21_28d",
    }
    assert sum(candidates["exp_half_life_2d"].weights) == pytest.approx(1.0)
    assert candidates["weekly_7_14_21_28d"].weights == pytest.approx(
        (8 / 15, 4 / 15, 2 / 15, 1 / 15)
    )


def test_prediction_uses_only_actuals_available_by_decision_time() -> None:
    actual = _synthetic_actual()
    decision = dt.datetime(2025, 2, 1, 11, 50)
    target = decision + dt.timedelta(hours=24)
    candidate = next(item for item in _AUDIT.candidate_specs() if item.name == "mean_7d")
    original = _AUDIT.build_pv_series(actual)

    changed_actual = dict(actual)
    for timestamp in changed_actual:
        if timestamp > decision:
            changed_actual[timestamp] += 100_000.0
    changed = _AUDIT.build_pv_series(changed_actual)

    original_prediction = _AUDIT.predict_point(
        series=original,
        candidate=candidate,
        decision_time=decision,
        target_time=target,
    )
    changed_prediction = _AUDIT.predict_point(
        series=changed,
        candidate=candidate,
        decision_time=decision,
        target_time=target,
    )
    assert changed_prediction == pytest.approx(original_prediction)


def test_one_day_evaluation_matches_horizon_b_tail_geometry() -> None:
    rows, sample = _AUDIT.build_evaluation_rows(
        series=_AUDIT.build_pv_series(_synthetic_actual()),
        candidates=_AUDIT.candidate_specs(),
        evaluation_start=dt.date(2025, 2, 1),
        evaluation_end=dt.date(2025, 2, 2),
    )

    assert len(rows) == 4 * sum(range(1, 36))
    assert sample["evaluated_non_release_mpc_calls"] == 4 * 35
    assert sample["common_unique_snapshot_targets_per_method"] == 4 * 35
    assert (
        min((row.target_time - row.decision_time).total_seconds() for row in rows)
        == (18 * 60 + 20) * 60
    )
    assert (
        max((row.target_time - row.decision_time).total_seconds() for row in rows) == (24 * 60) * 60
    )


def test_series_rejects_missing_ten_minute_interval() -> None:
    actual = _synthetic_actual(days=1)
    del actual[dt.datetime(2025, 1, 1, 0, 20)]

    with pytest.raises(ValueError, match="continuous ten-minute grid"):
        _AUDIT.build_pv_series(actual)


def test_evidence_is_deterministic_and_keeps_decision_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    actual = _synthetic_actual()
    monkeypatch.setattr(_AUDIT, "load_actual", lambda _path: actual)
    monkeypatch.setattr(_AUDIT, "sha256_file", lambda _path: "a" * 64)
    args = SimpleNamespace(
        actual=tmp_path / "附件2.xlsx",
        evaluation_start=dt.date(2025, 2, 1),
        evaluation_end=dt.date(2025, 2, 3),
        holdout_start=dt.date(2025, 2, 2),
        bootstrap_replicates=50,
        bootstrap_seed=20260913,
    )

    first = _AUDIT.evaluate(args)
    second = _AUDIT.evaluate(args)

    assert _AUDIT.render_json(first) == _AUDIT.render_json(second)
    assert first["status"] == "candidate_evidence_only"
    assert first["does_not_approve"] == ["D_PV_TAIL_BASELINE", "D_MODEL_Q3"]
    assert first["formal_runner_input"] is False
    assert first["evaluation"]["common_call_weighted_targets_per_method"] == 2 * 4 * 630
    assert first["bootstrap"]["block"] == "decision calendar day"
