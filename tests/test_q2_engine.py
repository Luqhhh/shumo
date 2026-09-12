from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.q2_engine import (
    Q2EngineConfig,
    Q2EngineeringResult,
    Q2EngineError,
    run_q2_engineering,
)
from microgrid.problem.q2_forecast import ForecastConfig, ForecastPoint
from microgrid.problem.q2_inputs import ActualInterval, FixedPricePoint, Q2InputBundle
from microgrid.problem.q2_model import Q2ModelConfig, Q2Plan


def _bundle() -> Q2InputBundle:
    actuals: list[ActualInterval] = []
    for day_index in range(32):
        day = dt.date(2025, 1, 1) + dt.timedelta(days=day_index)
        for slot in range(144):
            start = dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=slot * 10)
            actuals.append(
                ActualInterval(
                    day=day,
                    slot=slot,
                    start=start,
                    end=start + dt.timedelta(minutes=10),
                    load_kw=600.0,
                    pv_kw=0.0,
                    load_source_ref=f"load-{day}-{slot}",
                    pv_source_ref=f"pv-{day}-{slot}",
                )
            )
    return Q2InputBundle(
        fixed_prices=tuple(
            FixedPricePoint(slot=slot, price_cny_per_kwh=1.0, source_ref=f"price-{slot}")
            for slot in range(144)
        ),
        actuals=tuple(actuals),
        input_hashes=(("attachment1", "a" * 64), ("attachment2", "b" * 64)),
    )


def _flat_forecast(actuals, decision_time, horizon_start, config, horizon_steps=144):
    return tuple(
        ForecastPoint(
            valid_time=horizon_start + dt.timedelta(minutes=10 * (index + 1)),
            available_at=decision_time,
            load_kw=600.0,
            pv_kw=0.0,
            training_cutoff=decision_time,
            model_version=config.model_version,
            data_version="synthetic-visible-history",
            fallback_reason="",
        )
        for index in range(horizon_steps)
    )


def _zero_action_plan(window, config) -> Q2Plan:
    return Q2Plan(
        planned_purchase_kwh=(100.0,) * config.horizon_steps,
        charge_kwh=(0.0,) * config.horizon_steps,
        discharge_kwh=(0.0,) * config.horizon_steps,
        pv_used_kwh=(0.0,) * config.horizon_steps,
        soc_kwh=(window.initial_soc_kwh,) * (config.horizon_steps + 1),
        objective_cny=100.0 * config.horizon_steps,
        solver_status=0,
        solver_message="synthetic solver",
        solver_metadata={"status": 0},
    )


def test_engine_commits_one_causal_action_per_ten_minute_and_preserves_soc(
    monkeypatch,
) -> None:
    import microgrid.problem.q2_engine as engine

    forecast_calls: list[tuple[dt.datetime, tuple[ActualInterval, ...]]] = []
    solve_calls = []

    def fake_forecast(actuals, decision_time, horizon_start, config, horizon_steps=144):
        forecast_calls.append((decision_time, actuals))
        return _flat_forecast(actuals, decision_time, horizon_start, config, horizon_steps)

    def fake_solve(window, config):
        solve_calls.append(window)
        return _zero_action_plan(window, config)

    monkeypatch.setattr(engine, "build_q2_forecast", fake_forecast)
    monkeypatch.setattr(engine, "solve_q2_window", fake_solve)
    result = run_q2_engineering(
        _bundle(),
        Q2EngineConfig(
            action_start_day=dt.date(2025, 2, 1),
            action_end_day=dt.date(2025, 2, 1),
            forecast_config=ForecastConfig(weights=(0.25, 0.25, 0.25, 0.25), ar1_phi=0.0),
            model_config=Q2ModelConfig(horizon_steps=144),
            annual_terminal_soc_kwh=6000.0,
            is_synthetic=True,
        ),
    )

    assert isinstance(result, Q2EngineeringResult)
    assert len(result.intervals) == 144
    assert result.intervals[0].day == dt.date(2025, 2, 1)
    assert result.intervals[0].slot == 0
    assert all(item.day >= dt.date(2025, 2, 1) for item in result.intervals)
    assert all(
        current.state_start == previous.state_end
        for previous, current in zip(result.intervals[:-1], result.intervals[1:], strict=True)
    )
    assert len(forecast_calls) == 144
    assert len(solve_calls) == 144
    assert all(len(window.valid_times) == 144 for window in solve_calls)
    assert all(
        all(actual.end <= decision_time for actual in visible)
        for decision_time, visible in forecast_calls
    )
    assert tuple(window.annual_terminal_step for window in solve_calls) == tuple(range(144, 0, -1))
    assert all(window.annual_terminal_soc_kwh == pytest.approx(6000.0) for window in solve_calls)
    assert result.is_synthetic is True
    assert result.metadata["warmup"]["action_start_day"] == "2025-02-01"
    assert result.metadata["decision_trace"]["count"] == 144
    assert result.metadata["input_hashes"] == {
        "attachment1": "a" * 64,
        "attachment2": "b" * 64,
    }


def test_engine_rejects_executed_final_soc_mismatch(monkeypatch) -> None:
    import microgrid.problem.q2_engine as engine

    monkeypatch.setattr(engine, "build_q2_forecast", _flat_forecast)
    monkeypatch.setattr(engine, "solve_q2_window", _zero_action_plan)

    with pytest.raises(Q2EngineError, match="final SOC"):
        run_q2_engineering(
            _bundle(),
            Q2EngineConfig(
                action_start_day=dt.date(2025, 2, 1),
                action_end_day=dt.date(2025, 2, 1),
                forecast_config=ForecastConfig(weights=(0.25, 0.25, 0.25, 0.25), ar1_phi=0.0),
                model_config=Q2ModelConfig(horizon_steps=144),
                annual_terminal_soc_kwh=6500.0,
                is_synthetic=True,
            ),
        )


def test_engine_runs_without_optional_annual_terminal_target(monkeypatch) -> None:
    import microgrid.problem.q2_engine as engine

    solve_calls = []

    def fake_solve(window, config):
        solve_calls.append(window)
        return _zero_action_plan(window, config)

    monkeypatch.setattr(engine, "build_q2_forecast", _flat_forecast)
    monkeypatch.setattr(engine, "solve_q2_window", fake_solve)

    result = run_q2_engineering(
        _bundle(),
        Q2EngineConfig(
            action_start_day=dt.date(2025, 2, 1),
            action_end_day=dt.date(2025, 2, 1),
            forecast_config=ForecastConfig(weights=(0.25, 0.25, 0.25, 0.25), ar1_phi=0.0),
            model_config=Q2ModelConfig(horizon_steps=144),
            is_synthetic=True,
        ),
    )

    assert result.intervals[-1].state_end.energy_kwh == pytest.approx(6000.0)
    assert all(window.annual_terminal_step is None for window in solve_calls)
