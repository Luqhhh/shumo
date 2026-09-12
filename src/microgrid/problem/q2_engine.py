"""Pure continuous rolling Q2 engineering engine."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from .contracts import ENERGY_ABS_TOL_KWH, BatteryState, CostBreakdown, IntervalResult
from .q2_forecast import ForecastConfig, ForecastPoint, build_q2_forecast
from .q2_inputs import Q2InputBundle
from .q2_model import Q2ModelConfig, Q2WindowInput, solve_q2_window
from .q2_replay import Q2ReplayRow, replay_q2_actions, rows_to_interval_results, summarize_q2_cost
from .validation import validate_complete_run


@dataclass(frozen=True)
class Q2EngineConfig:
    action_start_day: dt.date
    action_end_day: dt.date
    forecast_config: ForecastConfig
    model_config: Q2ModelConfig
    annual_terminal_soc_kwh: float | None = None
    is_synthetic: bool = False
    terminal_value_cny_per_kwh: float = 0.0

    def __post_init__(self) -> None:
        if self.action_end_day < self.action_start_day:
            raise ValueError("action_end_day must not precede action_start_day")
        if self.terminal_value_cny_per_kwh < 0:
            raise ValueError("terminal_value_cny_per_kwh must be non-negative")


@dataclass(frozen=True)
class Q2EngineeringResult:
    intervals: tuple[IntervalResult, ...]
    costs: CostBreakdown
    forecast_records: tuple[ForecastPoint, ...]
    solver_records: tuple[dict[str, Any], ...]
    metadata: dict[str, Any]
    is_synthetic: bool


class Q2EngineError(ValueError):
    """The pure Q2 rolling engine could not produce a valid result."""


def _fixed_price_by_slot(bundle: Q2InputBundle) -> dict[int, float]:
    return {point.slot: point.price_cny_per_kwh for point in bundle.fixed_prices}


def _slot_for_valid_time(valid_time: dt.datetime) -> int:
    start = valid_time - dt.timedelta(minutes=10)
    return (start.hour * 60 + start.minute) // 10


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

    state = BatteryState(6000.0)
    replay_rows: list[Q2ReplayRow] = []
    forecast_records: list[ForecastPoint] = []
    solver_records: list[dict[str, Any]] = []
    for _position, actual in enumerate(active_actuals):
        decision_time = actual.start
        visible_actuals = tuple(item for item in ordered_actuals if item.end <= decision_time)
        forecast = build_q2_forecast(
            visible_actuals,
            decision_time=decision_time,
            horizon_start=decision_time,
            config=config.forecast_config,
            horizon_steps=config.model_config.horizon_steps,
        )
        forecast_records.extend(forecast)
        window = Q2WindowInput(
            valid_times=tuple(point.valid_time for point in forecast),
            price_cny_per_kwh=tuple(
                price_by_slot[_slot_for_valid_time(point.valid_time)] for point in forecast
            ),
            load_forecast_kwh=tuple(point.load_kw / 6.0 for point in forecast),
            pv_forecast_kwh=tuple(point.pv_kw / 6.0 for point in forecast),
            initial_soc_kwh=state.energy_kwh,
            terminal_value_cny_per_kwh=config.terminal_value_cny_per_kwh,
            annual_terminal_soc_kwh=config.annual_terminal_soc_kwh,
            annual_terminal_step=(
                144 - actual.slot
                if (
                    config.annual_terminal_soc_kwh is not None
                    and actual.day == config.action_end_day
                )
                else None
            ),
        )
        plan = solve_q2_window(window, config.model_config)
        solver_records.append(
            {
                "decision_time": decision_time.isoformat(sep=" "),
                "horizon_steps": len(window.valid_times),
                "status": plan.solver_status,
                "message": plan.solver_message,
                "metadata": plan.solver_metadata,
            }
        )
        key = (actual.day, actual.slot)
        row = replay_q2_actions(
            (actual,),
            prices={key: price_by_slot[actual.slot]},
            planned={
                key: (
                    plan.planned_purchase_kwh[0],
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
    )
