from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from microgrid.problem.contracts import (
    ENERGY_ABS_TOL_KWH,
    MAX_BUS_ENERGY_KWH,
    BatteryAction,
    BatteryState,
    apply_battery_action,
)
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


def test_annual_terminal_state_is_enforced_at_selected_window_step() -> None:
    window = replace(
        _window(
            load=(0.0, 0.0),
            pv=(0.0, 0.0),
            prices=(1.0, 1.0),
            annual_terminal_soc=6500.0,
        ),
        annual_terminal_step=1,
    )

    plan = solve_q2_window(window, _config(2))

    assert plan.soc_kwh[1] == pytest.approx(6500.0)


def test_annual_hard_constraint_disables_terminal_salvage_value() -> None:
    window = replace(
        _window(
            load=(0.0, 0.0),
            pv=(0.0, 0.0),
            prices=(1.0, 1.0),
            terminal_value=10.0,
            annual_terminal_soc=6000.0,
        ),
        annual_terminal_step=1,
    )

    plan = solve_q2_window(window, _config(2))
    planned_cost = sum(
        price * quantity
        for price, quantity in zip(window.price_cny_per_kwh, plan.planned_purchase_kwh, strict=True)
    )

    assert plan.objective_cny == pytest.approx(planned_cost)
    assert validate_q2_plan(window, plan).ok


def test_annual_terminal_step_must_be_inside_window() -> None:
    with pytest.raises(ValueError, match="annual_terminal_step"):
        replace(
            _window(
                load=(0.0, 0.0),
                pv=(0.0, 0.0),
                prices=(1.0, 1.0),
                annual_terminal_soc=6000.0,
            ),
            annual_terminal_step=0,
        )


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


def _successful_result_with_first_discharge(
    discharge_kwh: float, *, initial_soc_kwh: float = 6000.0
):
    values = [0.0] * 13
    values[4] = discharge_kwh
    values[8] = initial_soc_kwh
    values[9] = initial_soc_kwh - discharge_kwh / 0.9
    values[10] = values[9]

    class SuccessfulResult:
        status = 0
        success = True
        message = "synthetic success"
        mip_gap = 0.0
        mip_node_count = 0
        mip_dual_bound = 0.0
        x = values

    return SuccessfulResult()


def _successful_result_with_first_charge(charge_kwh: float, *, initial_soc_kwh: float = 6000.0):
    values = [0.0] * 13
    values[2] = charge_kwh
    values[8] = initial_soc_kwh
    values[9] = initial_soc_kwh + 0.9 * charge_kwh
    values[10] = values[9]

    class SuccessfulResult:
        status = 0
        success = True
        message = "synthetic success"
        mip_gap = 0.0
        mip_node_count = 0
        mip_dual_bound = 0.0
        x = values

    return SuccessfulResult()


def test_solver_output_normalizes_tiny_soc_equality_residual(monkeypatch) -> None:
    import microgrid.problem.q2_model as q2_model

    result = _successful_result_with_first_charge(1.0)
    result.x[0] = 1.0
    result.x[10] += 1.1e-6
    monkeypatch.setattr(q2_model, "milp", lambda *args, **kwargs: result)
    window = _window(load=(0.0, 0.0), pv=(0.0, 0.0), prices=(1.0, 1.0))
    plan = solve_q2_window(window, _config(2))
    assert validate_q2_plan(window, plan).ok


def test_fixed_contract_shortfall_uses_penalized_emergency_purchase() -> None:
    window = replace(
        _window(
            load=(1000.0, 1000.0),
            pv=(0.0, 0.0),
            prices=(1.0, 1.0),
            initial_soc=1200.0,
        ),
        fixed_purchase_kwh=(0.0, 0.0),
    )
    plan = solve_q2_window(window, _config(2))
    assert plan.e_plan_kwh == pytest.approx((1000.0, 1000.0))
    assert plan.objective_cny == pytest.approx(10_000.0)
    assert validate_q2_plan(window, plan).ok


def test_solver_output_normalizes_tiny_opposing_battery_action(monkeypatch) -> None:
    import microgrid.problem.q2_model as q2_model

    result = _successful_result_with_first_charge(1.0)
    result.x[0] = 1.0
    result.x[4] = 1.7e-5
    result.x[9] = 6000.0 + 0.9 - 1.7e-5 / 0.9
    result.x[10] = result.x[9]
    monkeypatch.setattr(q2_model, "milp", lambda *args, **kwargs: result)
    window = _window(load=(0.0, 0.0), pv=(0.0, 0.0), prices=(1.0, 1.0))
    plan = solve_q2_window(window, _config(2))
    assert plan.discharge_kwh[0] == 0.0
    assert validate_q2_plan(window, plan).ok


def _successful_result_with_first_purchase(purchase_kwh: float):
    values = [0.0] * 13
    values[0] = purchase_kwh
    values[8] = 6000.0
    values[9] = 6000.0
    values[10] = 6000.0

    class SuccessfulResult:
        status = 0
        success = True
        message = "synthetic success"
        mip_gap = 0.0
        mip_node_count = 0
        mip_dual_bound = 0.0
        x = values

    return SuccessfulResult()


def test_solver_normalizes_within_tolerance_upper_bound_excess(monkeypatch) -> None:
    import microgrid.problem.q2_model as q2_model

    excess = 1e-12
    monkeypatch.setattr(
        q2_model,
        "milp",
        lambda *args, **kwargs: _successful_result_with_first_discharge(
            MAX_BUS_ENERGY_KWH + excess
        ),
    )

    plan = solve_q2_window(
        _window(load=(0.0, 0.0), pv=(0.0, 0.0), prices=(1.0, 2.0)),
        _config(2),
    )

    assert plan.discharge_kwh[0] == MAX_BUS_ENERGY_KWH
    assert plan.solver_metadata["bound_normalization_count"] == 1
    assert plan.solver_metadata["max_bound_normalization_kwh"] == pytest.approx(
        1.0231815394945443e-12
    )


def test_solver_rejects_upper_bound_excess_beyond_tolerance(monkeypatch) -> None:
    import microgrid.problem.q2_model as q2_model

    monkeypatch.setattr(
        q2_model,
        "milp",
        lambda *args, **kwargs: _successful_result_with_first_discharge(
            MAX_BUS_ENERGY_KWH + 2.0 * ENERGY_ABS_TOL_KWH
        ),
    )

    with pytest.raises(Q2SolveError, match="discharge_kwh.*upper bound"):
        solve_q2_window(
            _window(load=(0.0, 0.0), pv=(0.0, 0.0), prices=(1.0, 2.0)),
            _config(2),
        )


def test_solver_normalizes_first_action_to_strict_soc_lower_bound(monkeypatch) -> None:
    import microgrid.problem.q2_model as q2_model

    initial_soc_kwh = 1457.0527638888889
    raw_discharge_kwh = 231.34748750000017
    monkeypatch.setattr(
        q2_model,
        "milp",
        lambda *args, **kwargs: _successful_result_with_first_discharge(
            raw_discharge_kwh, initial_soc_kwh=initial_soc_kwh
        ),
    )

    plan = solve_q2_window(
        _window(
            load=(0.0, 0.0),
            pv=(0.0, 0.0),
            prices=(1.0, 2.0),
            initial_soc=initial_soc_kwh,
        ),
        _config(2),
    )
    next_state = apply_battery_action(
        BatteryState(initial_soc_kwh),
        BatteryAction(
            charge_kwh=plan.charge_kwh[0],
            discharge_kwh=plan.discharge_kwh[0],
        ),
    )

    assert plan.discharge_kwh[0] == 231.3474875
    assert next_state.energy_kwh == 1200.0
    assert plan.solver_metadata["bound_normalization_count"] == 1
    assert plan.solver_metadata["max_bound_normalization_kwh"] == pytest.approx(
        1.7053025658242404e-13
    )


def test_solver_rejects_first_action_soc_violation_beyond_tolerance(monkeypatch) -> None:
    import microgrid.problem.q2_model as q2_model

    initial_soc_kwh = 1457.0527638888889
    discharge_kwh = (initial_soc_kwh - (1200.0 - 2.0 * ENERGY_ABS_TOL_KWH)) * 0.9
    monkeypatch.setattr(
        q2_model,
        "milp",
        lambda *args, **kwargs: _successful_result_with_first_discharge(
            discharge_kwh, initial_soc_kwh=initial_soc_kwh
        ),
    )

    with pytest.raises(Q2SolveError, match="first action.*SOC lower bound"):
        solve_q2_window(
            _window(
                load=(0.0, 0.0),
                pv=(0.0, 0.0),
                prices=(1.0, 2.0),
                initial_soc=initial_soc_kwh,
            ),
            _config(2),
        )


def test_solver_normalizes_first_action_to_strict_soc_upper_bound(monkeypatch) -> None:
    import microgrid.problem.q2_model as q2_model

    initial_soc_kwh = 10500.0
    raw_charge_kwh = 333.3333333333353
    monkeypatch.setattr(
        q2_model,
        "milp",
        lambda *args, **kwargs: _successful_result_with_first_charge(
            raw_charge_kwh, initial_soc_kwh=initial_soc_kwh
        ),
    )

    plan = solve_q2_window(
        _window(
            load=(0.0, 0.0),
            pv=(0.0, 0.0),
            prices=(1.0, 2.0),
            initial_soc=initial_soc_kwh,
        ),
        _config(2),
    )
    next_state = apply_battery_action(
        BatteryState(initial_soc_kwh),
        BatteryAction(
            charge_kwh=plan.charge_kwh[0],
            discharge_kwh=plan.discharge_kwh[0],
        ),
    )

    assert plan.charge_kwh[0] == 333.3333333333333
    assert next_state.energy_kwh == 10800.0
    assert plan.solver_metadata["bound_normalization_count"] == 1
    assert plan.solver_metadata["max_bound_normalization_kwh"] == pytest.approx(
        1.9895196601282805e-12
    )


def test_solver_rejects_first_action_soc_upper_violation_beyond_tolerance(
    monkeypatch,
) -> None:
    import microgrid.problem.q2_model as q2_model

    initial_soc_kwh = 10500.0
    charge_kwh = (10800.0 + 2.0 * ENERGY_ABS_TOL_KWH - initial_soc_kwh) / 0.9
    monkeypatch.setattr(
        q2_model,
        "milp",
        lambda *args, **kwargs: _successful_result_with_first_charge(
            charge_kwh, initial_soc_kwh=initial_soc_kwh
        ),
    )

    with pytest.raises(Q2SolveError, match="first action.*SOC upper bound"):
        solve_q2_window(
            _window(
                load=(0.0, 0.0),
                pv=(0.0, 0.0),
                prices=(1.0, 2.0),
                initial_soc=initial_soc_kwh,
            ),
            _config(2),
        )


def test_solver_normalizes_within_tolerance_negative_purchase(monkeypatch) -> None:
    import microgrid.problem.q2_model as q2_model

    negative = -1.1368683772161603e-13
    monkeypatch.setattr(
        q2_model,
        "milp",
        lambda *args, **kwargs: _successful_result_with_first_purchase(negative),
    )

    plan = solve_q2_window(
        _window(load=(0.0, 0.0), pv=(0.0, 0.0), prices=(1.0, 2.0)),
        _config(2),
    )

    assert plan.planned_purchase_kwh[0] == 0.0
    assert plan.solver_metadata["bound_normalization_count"] == 1
    assert plan.solver_metadata["max_bound_normalization_kwh"] == pytest.approx(
        1.1368683772161603e-13
    )


def test_solver_rejects_negative_purchase_beyond_tolerance(monkeypatch) -> None:
    import microgrid.problem.q2_model as q2_model

    monkeypatch.setattr(
        q2_model,
        "milp",
        lambda *args, **kwargs: _successful_result_with_first_purchase(-2.0 * ENERGY_ABS_TOL_KWH),
    )

    with pytest.raises(Q2SolveError, match="planned_purchase_kwh.*lower bound"):
        solve_q2_window(
            _window(load=(0.0, 0.0), pv=(0.0, 0.0), prices=(1.0, 2.0)),
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
        (
            "power_upper_bound",
            lambda plan: replace(
                plan, discharge_kwh=(MAX_BUS_ENERGY_KWH + 2.0 * ENERGY_ABS_TOL_KWH, 0.0)
            ),
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
