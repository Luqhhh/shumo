from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.q2_model import (
    Q2ModelConfig,
    Q2SolveError,
    Q2WindowInput,
    solve_q2_window,
)


def _window(
    *,
    load: tuple[float, ...] = (100.0, 60.0, 80.0, 20.0),
    pv: tuple[float, ...] = (0.0, 90.0, 0.0, 0.0),
    prices: tuple[float, ...] = (1.0, 2.0, 1.0, 2.0),
    initial_soc: float = 6000.0,
    terminal_value: float = 0.0,
    annual_terminal_soc: float | None = None,
    is_annual_endpoint: bool = False,
) -> Q2WindowInput:
    steps = len(load)
    assert len(pv) == len(prices) == steps
    start = dt.datetime(2025, 2, 1)
    return Q2WindowInput(
        valid_times=tuple(start + dt.timedelta(minutes=10 * index) for index in range(steps)),
        price_cny_per_kwh=prices,
        load_forecast_kwh=load,
        pv_forecast_kwh=pv,
        initial_soc_kwh=initial_soc,
        terminal_value_cny_per_kwh=terminal_value,
        annual_terminal_soc_kwh=annual_terminal_soc,
        is_annual_endpoint=is_annual_endpoint,
    )


def _config(steps: int) -> Q2ModelConfig:
    return Q2ModelConfig(horizon_steps=steps, time_limit_s=10.0)


def test_small_milp_respects_supply_pv_and_battery_constraints() -> None:
    window = _window()
    plan = solve_q2_window(window, _config(4))

    assert len(plan.planned_purchase_kwh) == 4
    assert all(quantity >= 0 for quantity in plan.planned_purchase_kwh)
    assert all(
        pv_used <= pv_forecast + 1e-7
        for pv_used, pv_forecast in zip(plan.pv_used_kwh, window.pv_forecast_kwh, strict=True)
    )
    assert plan.pv_used_kwh[1] == pytest.approx(90.0)
    assert plan.planned_purchase_kwh[1] == pytest.approx(0.0)
    assert plan.objective_cny == pytest.approx(
        sum(
            quantity * price
            for quantity, price in zip(
                plan.planned_purchase_kwh, window.price_cny_per_kwh, strict=True
            )
        )
    )
    for quantity, charge, discharge, pv_used, load, pv_forecast in zip(
        plan.planned_purchase_kwh,
        plan.charge_kwh,
        plan.discharge_kwh,
        plan.pv_used_kwh,
        window.load_forecast_kwh,
        window.pv_forecast_kwh,
        strict=True,
    ):
        assert quantity + discharge + pv_used + 1e-7 >= load + charge
        assert charge == 0.0 or discharge == 0.0
        assert pv_used <= pv_forecast + 1e-7

    assert plan.soc_kwh[0] == pytest.approx(window.initial_soc_kwh)
    assert all(1200.0 - 1e-7 <= soc <= 10800.0 + 1e-7 for soc in plan.soc_kwh)
    for previous, charge, discharge, current in zip(
        plan.soc_kwh[:-1],
        plan.charge_kwh,
        plan.discharge_kwh,
        plan.soc_kwh[1:],
        strict=True,
    ):
        assert current == pytest.approx(previous + 0.9 * charge - discharge / 0.9)


def test_annual_terminal_state_is_enforced_only_at_enabled_endpoint() -> None:
    endpoint = solve_q2_window(
        _window(
            load=(0.0, 0.0),
            pv=(0.0, 0.0),
            prices=(1.0, 1.0),
            annual_terminal_soc=7000.0,
            is_annual_endpoint=True,
        ),
        _config(2),
    )
    assert endpoint.soc_kwh[-1] == pytest.approx(7000.0)

    ordinary = solve_q2_window(
        _window(
            load=(0.0, 0.0),
            pv=(0.0, 0.0),
            prices=(1.0, 1.0),
            annual_terminal_soc=7000.0,
            is_annual_endpoint=False,
        ),
        _config(2),
    )
    assert ordinary.soc_kwh[-1] != pytest.approx(7000.0)


def test_non_successful_solver_result_raises_q2_solve_error(monkeypatch) -> None:
    import microgrid.problem.q2_model as q2_model

    class FailedResult:
        status = 1
        message = "synthetic failure"
        x = None

    monkeypatch.setattr(q2_model, "milp", lambda *args, **kwargs: FailedResult())

    with pytest.raises(Q2SolveError, match="synthetic failure"):
        solve_q2_window(
            _window(load=(1.0, 1.0), pv=(0.0, 0.0), prices=(1.0, 2.0)),
            _config(2),
        )
