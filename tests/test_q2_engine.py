from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.contracts import ENERGY_ABS_TOL_KWH
from microgrid.problem.q2_engine import (
    Q2EngineConfig,
    Q2EngineeringResult,
    Q2EngineError,
    run_q2_engineering,
)
from microgrid.problem.q2_forecast import ForecastConfig, ForecastPoint
from microgrid.problem.q2_inputs import ActualInterval, FixedPricePoint, Q2InputBundle
from microgrid.problem.q2_model import (
    Q2ModelConfig,
    Q2Plan,
    Q2ValidationReport,
    Q2WindowInput,
    solve_q2_window,
)


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


_forecast_calls: list[tuple[dt.datetime, tuple[ActualInterval, ...]]] = []


def _flat_forecast(
    decision_time: dt.datetime,
    horizon_start: dt.datetime,
    model_version: str,
    horizon_steps: int = 144,
) -> tuple[ForecastPoint, ...]:
    return tuple(
        ForecastPoint(
            valid_time=horizon_start + dt.timedelta(minutes=10 * (index + 1)),
            available_at=decision_time,
            load_kw=600.0,
            pv_kw=0.0,
            training_cutoff=decision_time,
            model_version=model_version,
            data_version="synthetic-visible-history",
            fallback_reason="",
        )
        for index in range(horizon_steps)
    )


class _FakeForecastBuilder:
    """Deterministic stand-in for Q2ForecastBuilder that records visible history."""

    def __init__(self, config: ForecastConfig) -> None:
        self._config = config
        self._visible: list[ActualInterval] = []

    def add(self, item: ActualInterval) -> None:
        self._visible.append(item)

    def build(
        self,
        decision_time: dt.datetime,
        horizon_start: dt.datetime,
        horizon_steps: int = 144,
    ) -> tuple[ForecastPoint, ...]:
        _forecast_calls.append((decision_time, tuple(self._visible)))
        return _flat_forecast(
            decision_time, horizon_start, self._config.model_version, horizon_steps
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

    forecast_calls = _forecast_calls
    forecast_calls.clear()
    solve_calls = []

    def fake_solve(window, config):
        solve_calls.append(window)
        return _zero_action_plan(window, config)

    monkeypatch.setattr(engine, "Q2ForecastBuilder", _FakeForecastBuilder)
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

    monkeypatch.setattr(engine, "Q2ForecastBuilder", _FakeForecastBuilder)
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

    monkeypatch.setattr(engine, "Q2ForecastBuilder", _FakeForecastBuilder)
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


def test_engine_freezes_midnight_purchase_contract_for_the_whole_day(monkeypatch) -> None:
    import microgrid.problem.q2_engine as engine

    solve_calls = []

    def fake_solve(window, config):
        solve_calls.append(window)
        call_number = len(solve_calls)
        purchases = tuple(
            fixed if fixed is not None else 100.0 * call_number + index
            for index, fixed in enumerate(window.fixed_purchase_kwh)
        )
        return Q2Plan(
            planned_purchase_kwh=purchases,
            charge_kwh=(0.0,) * config.horizon_steps,
            discharge_kwh=(0.0,) * config.horizon_steps,
            pv_used_kwh=(0.0,) * config.horizon_steps,
            soc_kwh=(window.initial_soc_kwh,) * (config.horizon_steps + 1),
            objective_cny=sum(purchases),
            solver_status=0,
            solver_message="synthetic solver",
            solver_metadata={"status": 0},
        )

    monkeypatch.setattr(engine, "Q2ForecastBuilder", _FakeForecastBuilder)
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

    assert tuple(row.planned_purchase_kwh for row in result.intervals) == pytest.approx(
        tuple(100.0 + slot for slot in range(144))
    )
    assert all(value is None for value in solve_calls[0].fixed_purchase_kwh)
    assert solve_calls[1].fixed_purchase_kwh[0] == pytest.approx(101.0)
    assert solve_calls[1].fixed_purchase_kwh[-1] is None
    assert solve_calls[-1].fixed_purchase_kwh[0] == pytest.approx(243.0)
    assert tuple(record["fixed_purchase_count"] for record in result.solver_records) == (
        0,
        *range(143, 0, -1),
    )


def test_fixed_purchase_values_are_enforced_by_the_model() -> None:
    valid_times = tuple(
        dt.datetime(2025, 2, 1, 0, 10) + dt.timedelta(minutes=10 * i) for i in range(2)
    )
    window = Q2WindowInput(
        valid_times=valid_times,
        price_cny_per_kwh=(1.0, 1.0),
        load_forecast_kwh=(10.0, 10.0),
        pv_forecast_kwh=(0.0, 0.0),
        initial_soc_kwh=6000.0,
        terminal_value_cny_per_kwh=0.0,
        fixed_purchase_kwh=(12.0, None),
    )
    plan = solve_q2_window(window, Q2ModelConfig(horizon_steps=2))
    assert plan.planned_purchase_kwh[0] == pytest.approx(12.0)


def test_engine_rejects_invalid_solver_plan_before_replay(monkeypatch) -> None:
    import microgrid.problem.q2_engine as engine

    monkeypatch.setattr(engine, "Q2ForecastBuilder", _FakeForecastBuilder)
    monkeypatch.setattr(engine, "solve_q2_window", _zero_action_plan)
    monkeypatch.setattr(
        engine,
        "validate_q2_plan",
        lambda window, plan: Q2ValidationReport(
            False, ("synthetic invalid plan",), 1.0, 0.0, 0.0, ("synthetic invalid plan",)
        ),
    )
    with pytest.raises(Q2EngineError, match="synthetic invalid plan"):
        run_q2_engineering(
            _bundle(),
            Q2EngineConfig(
                action_start_day=dt.date(2025, 2, 1),
                action_end_day=dt.date(2025, 2, 1),
                forecast_config=ForecastConfig(weights=(0.25, 0.25, 0.25, 0.25)),
                model_config=Q2ModelConfig(horizon_steps=144),
                is_synthetic=True,
            ),
        )


def test_engine_accounting_separates_planned_from_realized_emergency(monkeypatch) -> None:
    """The MILP recourse and the settled emergency are distinct and billed once."""

    import microgrid.problem.q2_engine as engine

    def short_purchase_plan(window, config) -> Q2Plan:
        horizon = config.horizon_steps
        prices = window.price_cny_per_kwh
        # The synthetic forecast load is 600 kW = 100 kWh per interval; only
        # 60 kWh is committed, and the MILP covers the forecast gap with a
        # 45 kWh planning recourse.
        return Q2Plan(
            planned_purchase_kwh=(60.0,) * horizon,
            charge_kwh=(0.0,) * horizon,
            discharge_kwh=(0.0,) * horizon,
            pv_used_kwh=(0.0,) * horizon,
            soc_kwh=(window.initial_soc_kwh,) * (horizon + 1),
            objective_cny=sum(p * 60.0 for p in prices) + sum(5.0 * p * 45.0 for p in prices),
            solver_status=0,
            solver_message="synthetic solver",
            solver_metadata={"status": 0},
            e_plan_kwh=(45.0,) * horizon,
        )

    monkeypatch.setattr(engine, "Q2ForecastBuilder", _FakeForecastBuilder)
    monkeypatch.setattr(engine, "solve_q2_window", short_purchase_plan)

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

    accounting = result.accounting
    # 100 kWh of actual load less the 60 kWh committed leaves 40 kWh realized.
    assert accounting.e_realized_kwh == pytest.approx(40.0 * 144)
    assert accounting.e_plan_kwh == pytest.approx(45.0 * 144)
    assert accounting.e_plan_kwh != accounting.e_realized_kwh
    # Settlement bills realized emergency only, and exactly once.
    assert accounting.emergency_cost_cny == pytest.approx(result.costs.emergency_cost_cny)
    assert result.costs.emergency_cost_cny == pytest.approx(
        sum(interval.emergency_purchase_kwh * 5.0 for interval in result.intervals)
    )
    assert accounting.pv_accounting_policy == "grid_and_discharge_first_residual_pv"
    assert accounting.max_abs_ledger_residual_kwh <= ENERGY_ABS_TOL_KWH
    assert accounting.max_pv_partition_residual_kwh <= ENERGY_ABS_TOL_KWH


def test_annual_constraint_only_appears_when_explicit_endpoint_enters_window(monkeypatch) -> None:
    import microgrid.problem.q2_engine as engine

    calls = []
    monkeypatch.setattr(engine, "Q2ForecastBuilder", _FakeForecastBuilder)
    monkeypatch.setattr(
        engine,
        "solve_q2_window",
        lambda window, config: calls.append(window) or _zero_action_plan(window, config),
    )
    run_q2_engineering(
        _bundle(),
        Q2EngineConfig(
            action_start_day=dt.date(2025, 2, 1),
            action_end_day=dt.date(2025, 2, 1),
            forecast_config=ForecastConfig(weights=(0.25, 0.25, 0.25, 0.25)),
            model_config=Q2ModelConfig(horizon_steps=144),
            annual_terminal_soc_kwh=6000.0,
            annual_endpoint_day=dt.date(2025, 12, 31),
            is_synthetic=True,
        ),
    )
    assert all(window.annual_terminal_step is None for window in calls)
