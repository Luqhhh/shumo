from __future__ import annotations

import datetime as dt
from copy import deepcopy

import numpy as np
import pytest
from test_q4_pv_blend import toy_inputs

from microgrid.problem.contracts import CaseContext, InfoSet
from microgrid.problem.q4_common import ACTION_START, TERMINAL_MODEL_VERSION, YEAR_END
from microgrid.problem.rolling_engine import _initialize_forecaster, run_q4
from microgrid.schemas import InputError, PendingDecisionError


@pytest.mark.parametrize(
    "issue", [ACTION_START, YEAR_END - dt.timedelta(days=1), YEAR_END - dt.timedelta(hours=6)]
)
def test_terminal_changes_only_frozen_ordinary_residual_value(issue):
    from dataclasses import replace

    inputs = replace(
        toy_inputs(), prices=tuple(0.8 + 0.3 * np.sin(i * 2 * np.pi / 144) for i in range(52560))
    )
    if issue > ACTION_START + dt.timedelta(days=15):
        # Reuse the real service mechanics with synthetic official releases through year-end.
        from dataclasses import replace

        from microgrid.problem.contracts import InfoItem

        official = []
        at = dt.datetime(2025, 1, 1)
        while at <= issue:
            official.extend(
                InfoItem("pv_forecast_kw", at, at + dt.timedelta(hours=h), 200.0)
                for h in range(1, 25)
            )
            at += dt.timedelta(hours=6)
        inputs = replace(inputs, official=tuple(official))
    a, b = (
        _initialize_forecaster(inputs, "q4_3", "main", issue, model_version=version).refresh(
            InfoSet.from_raw(issue, ())
        )
        for version in ["q4-v3-reserve", TERMINAL_MODEL_VERSION]
    )
    assert a.load_kwh == b.load_kwh and a.pv_kwh == b.pv_kwh and a.prices == b.prices
    assert a.traces == {
        key: value for key, value in b.traces.items() if key != "terminal_value_rule"
    }
    expected = (
        0.9 * float(np.quantile(a.prices, 0.75, method="linear"))
        if len(a.slots) == 144 and a.slots[-1] + dt.timedelta(minutes=10) < YEAR_END
        else 0.0
    )
    assert b.terminal_value == expected and b.traces["terminal_value_rule"]["value"] == expected
    assert a.snapshot_id != b.snapshot_id
    if issue == ACTION_START:
        assert a.terminal_value != b.terminal_value


def test_terminal_requires_unmixed_scope_and_delegation(tmp_path, monkeypatch):
    import microgrid.problem.rolling_engine as module

    monkeypatch.setattr(module, "require_approved_decisions", lambda *args: None)
    with pytest.raises(InputError, match="unmixed Q4-3/main"):
        run_q4(
            CaseContext(
                tmp_path,
                "q4_3",
                metadata={"planning_method": "terminal-quartile", "pv_method": "blend-long"},
            )
        )

    def gate(repo, ids):
        if "D_OPTIMIZATION_Q4" in ids:
            raise PendingDecisionError(ids)

    monkeypatch.setattr(module, "require_approved_decisions", gate)
    with pytest.raises(PendingDecisionError):
        run_q4(CaseContext(tmp_path, "q4_3", metadata={"planning_method": "terminal-quartile"}))
    assert not (tmp_path / "outputs/runs").exists()


def test_terminal_future_actual_remains_hidden():
    from microgrid.problem.contracts import InfoItem

    inputs = toy_inputs()
    service = _initialize_forecaster(
        inputs, "q4_3", "main", ACTION_START, model_version=TERMINAL_MODEL_VERSION
    )
    original = service.refresh(InfoSet.from_raw(ACTION_START, ()))
    other = deepcopy(service)
    future = ACTION_START + dt.timedelta(minutes=10)
    other.ingest(
        InfoSet.from_raw(ACTION_START, (InfoItem("price_actual", future, future, 999999),))
    )
    assert other.refresh(InfoSet.from_raw(ACTION_START, ())) == original
