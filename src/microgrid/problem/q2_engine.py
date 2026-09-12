"""Pure continuous rolling Q2 engineering engine."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

from .contracts import ENERGY_ABS_TOL_KWH, BatteryState, CostBreakdown, IntervalResult
from .q2_forecast import ForecastConfig, ForecastPoint, Q2ForecastBuilder
from .q2_inputs import Q2InputBundle
from .q2_model import Q2ModelConfig, Q2WindowInput, solve_q2_window, validate_q2_plan
from .q2_replay import (
    PV_ACCOUNTING_POLICY,
    Q2ReplayRow,
    replay_q2_actions,
    rows_to_interval_results,
    summarize_q2_cost,
)
from .validation import validate_complete_run


@dataclass(frozen=True)
class Q2EngineConfig:
    action_start_day: dt.date
    action_end_day: dt.date
    forecast_config: ForecastConfig
    model_config: Q2ModelConfig
    annual_terminal_soc_kwh: float | None = None
    annual_endpoint_day: dt.date | None = None
    is_synthetic: bool = False
    terminal_value_cny_per_kwh: float = 0.0
    purchase_margin: float = 0.0

    def __post_init__(self) -> None:
        if self.action_end_day < self.action_start_day:
            raise ValueError("action_end_day must not precede action_start_day")
        if self.terminal_value_cny_per_kwh < 0:
            raise ValueError("terminal_value_cny_per_kwh must be non-negative")
        if (
            isinstance(self.purchase_margin, bool)
            or not isinstance(self.purchase_margin, (int, float))
            or not math.isfinite(float(self.purchase_margin))
            or float(self.purchase_margin) < 0
        ):
            raise ValueError("purchase_margin must be a finite non-negative number")


@dataclass(frozen=True)
class Q2Accounting:
    """Settlement-visible split of the Q2 emergency and PV quantities.

    ``e_plan_kwh`` sums the MILP's forecast-scenario recourse over the committed
    first actions. It is a planning figure only: it is never billed and never
    reaches the shared ``IntervalResult`` contract.

    ``e_realized_kwh`` is what the replay actually had to buy against realised
    load/PV. It is billed exactly once (at five times the interval price) and is
    the sole source of ``emergency_cost_cny`` and of
    ``IntervalResult.emergency_purchase_kwh``.
    """

    e_plan_kwh: float = 0.0
    e_realized_kwh: float = 0.0
    emergency_cost_cny: float = 0.0
    pv_available_kwh: float = 0.0
    pv_used_kwh: float = 0.0
    pv_curtail_kwh: float = 0.0
    max_pv_partition_residual_kwh: float = 0.0
    max_abs_ledger_residual_kwh: float = 0.0
    pv_accounting_policy: str = PV_ACCOUNTING_POLICY


@dataclass(frozen=True)
class Q2EngineeringResult:
    intervals: tuple[IntervalResult, ...]
    costs: CostBreakdown
    forecast_records: tuple[ForecastPoint, ...]
    solver_records: tuple[dict[str, Any], ...]
    metadata: dict[str, Any]
    is_synthetic: bool
    accounting: Q2Accounting = field(default_factory=Q2Accounting)


class Q2EngineError(ValueError):
    """The pure Q2 rolling engine could not produce a valid result."""


def _fixed_price_by_slot(bundle: Q2InputBundle) -> dict[int, float]:
    return {point.slot: point.price_cny_per_kwh for point in bundle.fixed_prices}


def _slot_for_valid_time(valid_time: dt.datetime) -> int:
    start = valid_time - dt.timedelta(minutes=10)
    return (start.hour * 60 + start.minute) // 10

def _interval_key_for_valid_time(valid_time: dt.datetime) -> tuple[dt.date, int]:
    start = valid_time - dt.timedelta(minutes=10)
    return (start.date(), (start.hour * 60 + start.minute) // 10)



def _active_actuals(
    actuals: tuple[Any, ...],
    start_day: dt.date,
    end_day: dt.date,
) -> tuple[Any, ...]:
    return tuple(item for item in actuals if start_day <= item.day <= end_day)


def run_q2_engineering(
    bundle: Q2InputBundle,
    config: Q2EngineConfig,
) -> Q2EngineeringResult:
    """Run continuous ten-minute rolling control without writing artifacts."""

    ordered_actuals = tuple(sorted(bundle.actuals, key=lambda item: (item.day, item.slot)))
    active_actuals = _active_actuals(
        ordered_actuals,
        config.action_start_day,
        config.action_end_day,
    )
    if not active_actuals:
        raise Q2EngineError("action range has no actual intervals")
    price_by_slot = _fixed_price_by_slot(bundle)
    if set(price_by_slot) != set(range(144)):
        raise Q2EngineError("fixed prices must contain all 144 slots")

    # The incremental forecast state below assumes the visible history is a
    # prefix of `ordered_actuals`, which holds because ActualInterval pins `end`
    # to an increasing function of (day, slot). Check it once rather than
    # silently mis-handle a bundle that violates it.
    for previous_item, current_item in zip(ordered_actuals, ordered_actuals[1:], strict=False):
        if current_item.end < previous_item.end:
            raise Q2EngineError("actual history is not ordered by interval end")

    state = BatteryState(6000.0)
    replay_rows: list[Q2ReplayRow] = []
    forecast_records: list[ForecastPoint] = []
    solver_records: list[dict[str, Any]] = []
    daily_contracts: dict[tuple[dt.date, int], float] = {}
    forecast_builder = Q2ForecastBuilder(config.forecast_config)
    history_cursor = 0
    e_plan_total_kwh = 0.0
    for _position, actual in enumerate(active_actuals):
        decision_time = actual.start
        while (
            history_cursor < len(ordered_actuals)
            and ordered_actuals[history_cursor].end <= decision_time
        ):
            forecast_builder.add(ordered_actuals[history_cursor])
            history_cursor += 1
        forecast = forecast_builder.build(
            decision_time=decision_time,
            horizon_start=decision_time,
            horizon_steps=config.model_config.horizon_steps,
        )
        forecast_records.extend(forecast)
        window = Q2WindowInput(
            valid_times=tuple(point.valid_time for point in forecast),
            price_cny_per_kwh=tuple(
                price_by_slot[_slot_for_valid_time(point.valid_time)] for point in forecast
            ),
            load_forecast_kwh=tuple(
                point.load_kw / 6.0 * (1.0 + config.purchase_margin) for point in forecast
            ),
            pv_forecast_kwh=tuple(point.pv_kw / 6.0 for point in forecast),
            initial_soc_kwh=state.energy_kwh,
            terminal_value_cny_per_kwh=config.terminal_value_cny_per_kwh,
            fixed_purchase_kwh=tuple(
                daily_contracts.get(_interval_key_for_valid_time(point.valid_time))
                for point in forecast
            ),
            annual_terminal_soc_kwh=config.annual_terminal_soc_kwh,
            annual_terminal_step=(
                (config.annual_endpoint_day - actual.day).days * 144 + 144 - actual.slot
                if (
                    config.annual_terminal_soc_kwh is not None
                    and config.annual_endpoint_day is not None
                    and 1 <= (config.annual_endpoint_day - actual.day).days * 144 + 144 - actual.slot
                    <= config.model_config.horizon_steps
                )
                else (
                    144 - actual.slot
                    if config.annual_terminal_soc_kwh is not None
                    and config.annual_endpoint_day is None
                    and actual.day == config.action_end_day
                    else None
                )
            ),
        )
        plan = solve_q2_window(window, config.model_config)
        # Only the first action is committed, so only e_plan[0] -- the planning
        # recourse for the step actually executed -- is booked to this window.
        e_plan_total_kwh += (plan.e_plan_kwh or (0.0,) * len(window.valid_times))[0]
        plan_validation = validate_q2_plan(window, plan)
        if not plan_validation.ok:
            prefix = (
                "final SOC "
                if "annual terminal SOC mismatch" in plan_validation.issues
                else ""
            )
            raise Q2EngineError(
                prefix + "invalid solver plan: " + "; ".join(plan_validation.issues)
            )
        if actual.slot == 0:
            for point, quantity in zip(forecast, plan.planned_purchase_kwh, strict=True):
                contract_key = _interval_key_for_valid_time(point.valid_time)
                if contract_key[0] == actual.day:
                    daily_contracts[contract_key] = quantity
        key = (actual.day, actual.slot)
        if key not in daily_contracts:
            raise Q2EngineError(f"missing frozen purchase contract for {key}")
        committed_purchase_kwh = daily_contracts[key]
        solver_records.append(
            {
                "decision_time": decision_time.isoformat(sep=" "),
                "horizon_steps": len(window.valid_times),
                "status": plan.solver_status,
                "fixed_purchase_count": sum(value is not None for value in window.fixed_purchase_kwh),
                "message": plan.solver_message,
                "metadata": plan.solver_metadata,
            }
        )
        row = replay_q2_actions(
            (actual,),
            prices={key: price_by_slot[actual.slot]},
            planned={
                key: (
                    committed_purchase_kwh,
                    plan.charge_kwh[0],
                    plan.discharge_kwh[0],
                )
            },
            initial_state=state,
        )[0]
        replay_rows.append(row)
        state = row.state_end

    if (
        config.annual_terminal_soc_kwh is not None
        and (
            config.annual_endpoint_day is None
            or config.action_end_day == config.annual_endpoint_day
        )
        and abs(state.energy_kwh - config.annual_terminal_soc_kwh) > ENERGY_ABS_TOL_KWH
    ):
        raise Q2EngineError(
            f"final SOC mismatch: {state.energy_kwh} != {config.annual_terminal_soc_kwh} kWh"
        )

    intervals = rows_to_interval_results(tuple(replay_rows))
    expected_days = tuple(
        config.action_start_day + dt.timedelta(days=offset)
        for offset in range((config.action_end_day - config.action_start_day).days + 1)
    )
    validation = validate_complete_run(intervals, expected_days)
    if not validation.ok:
        raise Q2EngineError("invalid rolling result: " + "; ".join(validation.issues))
    costs = summarize_q2_cost(tuple(replay_rows))
    accounting = Q2Accounting(
        e_plan_kwh=e_plan_total_kwh,
        e_realized_kwh=sum(row.purchase_plan.emergency_kwh for row in replay_rows),
        emergency_cost_cny=costs.emergency_cost_cny,
        pv_available_kwh=sum(row.actual.pv_kwh for row in replay_rows),
        pv_used_kwh=sum(row.pv_used_kwh for row in replay_rows),
        pv_curtail_kwh=sum(row.pv_curtail_kwh for row in replay_rows),
        max_pv_partition_residual_kwh=max(
            (abs(row.pv_partition_residual_kwh) for row in replay_rows), default=0.0
        ),
        max_abs_ledger_residual_kwh=max(
            (abs(row.ledger_residual_kwh) for row in replay_rows), default=0.0
        ),
    )
    metadata: dict[str, Any] = {
        "model_version": config.forecast_config.model_version,
        "forecast_version": config.forecast_config.model_version,
        "training_cutoff": (
            forecast_records[-1].training_cutoff.isoformat(sep=" ") if forecast_records else None
        ),
        "warmup": {
            "initial_soc_kwh": 6000.0,
            "action_start_day": config.action_start_day.isoformat(),
            "history_before_action": True,
        },
        "decision_trace": {"count": len(solver_records)},
        "solver_status": [record["status"] for record in solver_records],
        "input_hashes": dict(bundle.input_hashes),
        "validation": {
            "ok": validation.ok,
            "checked_intervals": validation.checked_intervals,
        },
    }
    return Q2EngineeringResult(
        intervals=intervals,
        costs=costs,
        forecast_records=tuple(forecast_records),
        solver_records=tuple(solver_records),
        metadata=metadata,
        is_synthetic=config.is_synthetic,
        accounting=accounting,
    )
