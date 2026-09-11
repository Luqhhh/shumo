from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from microgrid.problem.q2_model import (
    Q2ModelConfig,
    Q2Plan,
    Q2SolveError,
    Q2WindowInput,
    solve_q2_window,
    validate_q2_plan,
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


def _valid_plan(window: Q2WindowInput) -> Q2Plan:
    steps = len(window.valid_times)
    planned = window.load_forecast_kwh
    return Q2Plan(
        planned_purchase_kwh=planned,
        charge_kwh=(0.0,) * steps,
        discharge_kwh=(0.0,) * steps,
        pv_used_kwh=(0.0,) * steps,
        soc_kwh=(window.initial_soc_kwh,) * (steps + 1),
        objective_cny=sum(
            price * quantity
            for price, quantity in zip(window.price_cny_per_kwh, planned, strict=True)
        ),
        solver_status=0,
        solver_message="ok",
        solver_metadata={},
    )


def test_validate_q2_plan_accepts_a_valid_plan() -> None:
    window = _window(load=(10.0, 10.0), pv=(0.0, 0.0), prices=(1.0, 2.0))
    report = validate_q2_plan(window, _valid_plan(window))

    assert report.ok is True
    assert report.issues == ()
    assert report.violations == ()
    assert report.max_balance_residual_kwh == pytest.approx(0.0)
    assert report.max_dynamics_residual_kwh == pytest.approx(0.0)
    assert report.cost_gap_cny == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("label", "tamper"),
    [
        ("supply", lambda plan: replace(plan, planned_purchase_kwh=(0.0, 10.0))),
        ("dynamics", lambda plan: replace(plan, soc_kwh=(6000.0, 6000.0, 5999.0))),
        ("pv", lambda plan: replace(plan, pv_used_kwh=(1.0, 0.0))),
        (
            "simultaneous",
            lambda plan: replace(plan, charge_kwh=(1.0, 0.0), discharge_kwh=(1.0, 0.0)),
        ),
        ("objective", lambda plan: replace(plan, objective_cny=999.0)),
        ("nonfinite", lambda plan: replace(plan, planned_purchase_kwh=(float("nan"), 10.0))),
    ],
)
def test_validate_q2_plan_reports_tampering(label: str, tamper) -> None:
    window = _window(load=(10.0, 10.0), pv=(0.0, 0.0), prices=(1.0, 2.0))
    report = validate_q2_plan(window, tamper(_valid_plan(window)))

    assert report.ok is False
    assert report.issues
    assert any(label in violation for violation in report.violations)
