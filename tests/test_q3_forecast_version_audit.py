from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "audit_q3_forecast_versions.py"
_SPEC = importlib.util.spec_from_file_location("q3_forecast_version_audit_script", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - importlib platform guard
    raise RuntimeError(f"cannot load audit script: {_SCRIPT_PATH}")
_AUDIT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _AUDIT
_SPEC.loader.exec_module(_AUDIT)

historical_mae = _AUDIT.historical_mae
lead_bucket = _AUDIT.lead_bucket
method_name = _AUDIT.method_name
validate_forecasts = _AUDIT.validate_forecasts
weighted_prediction = _AUDIT.weighted_prediction


def test_historical_mae_uses_only_errors_realized_by_decision_time() -> None:
    times = np.array(
        [
            np.datetime64("2025-01-01T00:00"),
            np.datetime64("2025-01-01T01:00"),
            np.datetime64("2025-01-01T02:00"),
        ]
    )
    errors = np.array([1.0, 3.0, 1000.0])

    mae, count = historical_mae(
        times,
        errors,
        dt.datetime(2025, 1, 1, 1),
        "expanding",
    )

    assert count == 2
    assert mae == pytest.approx(2.0)


def test_rolling_56_day_history_has_explicit_open_left_boundary() -> None:
    decision_time = dt.datetime(2025, 3, 1)
    times = np.array(
        [
            np.datetime64(decision_time - dt.timedelta(days=56)),
            np.datetime64(decision_time - dt.timedelta(days=1)),
            np.datetime64(decision_time),
        ]
    )
    errors = np.array([1000.0, 1.0, 3.0])

    mae, count = historical_mae(
        times,
        errors,
        decision_time,
        "rolling_56_days",
    )

    assert count == 2
    assert mae == pytest.approx(2.0)


def test_weighted_prediction_uses_normalized_inverse_square_mae() -> None:
    decision_time = dt.datetime(2025, 2, 1)
    history_time = np.array([np.datetime64("2025-01-31T00:00")])
    candidates = [
        SimpleNamespace(lead_hours=1, issue_time=decision_time, pv_forecast_kw=100.0),
        SimpleNamespace(lead_hours=2, issue_time=decision_time, pv_forecast_kw=200.0),
    ]
    histories = {
        "lead_hours": {
            1: (history_time, np.array([10.0])),
            2: (history_time, np.array([20.0])),
        }
    }

    prediction, count = weighted_prediction(
        candidates,
        histories,
        decision_time=decision_time,
        grouping="lead_hours",
        window="expanding",
        epsilon_kw=1.0,
        power=2,
    )

    expected = np.average([100.0, 200.0], weights=[1 / 11**2, 1 / 21**2])
    assert count == 1
    assert prediction == pytest.approx(expected)


def test_method_names_preserve_distinct_float_epsilon_values() -> None:
    first = method_name("lead_hours", "expanding", 1.0000001, 2)
    second = method_name("lead_hours", "expanding", 1.0000002, 2)

    assert first != second


def test_lead_buckets_follow_q3_six_hour_updates() -> None:
    assert lead_bucket(1) == "lead_1_6_hours"
    assert lead_bucket(6) == "lead_1_6_hours"
    assert lead_bucket(7) == "lead_7_12_hours"
    assert lead_bucket(24) == "lead_19_24_hours"
    with pytest.raises(ValueError, match="1..24"):
        lead_bucket(25)


def test_forecast_validation_rejects_an_incomplete_issue() -> None:
    issue_time = dt.datetime(2025, 1, 1)
    records = [
        SimpleNamespace(
            issue_time=issue_time,
            lead_hours=lead,
            valid_time=issue_time + dt.timedelta(hours=lead),
            pv_forecast_kw=100.0,
        )
        for lead in range(1, 24)
    ]

    with pytest.raises(ValueError, match="missing=.*24"):
        validate_forecasts(records)
