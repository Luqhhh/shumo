"""Types and serialization shared by the approved Q4 implementation."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from contextlib import ExitStack, nullcontext
from dataclasses import fields, is_dataclass
from functools import lru_cache
from pathlib import Path

from ..schemas import MicrogridError

STEP = dt.timedelta(minutes=10)
YEAR_START = dt.datetime(2025, 1, 1)
ACTION_START = dt.datetime(2025, 2, 1)
YEAR_END = dt.datetime(2026, 1, 1)
RESERVE_START = YEAR_END - dt.timedelta(days=1)
MODEL_VERSION = "q4-v3-reserve"
BLEND_MODEL_VERSION = "q4-v4-pv-blend-long"
BIAS_MODEL_VERSION = "q4-v4-pv-bias-long"
TERMINAL_MODEL_VERSION = "q4-v4-terminal-upper-quartile"
SAFETY_MODEL_VERSION = "q4-v4-safety-procurement"
BLEND_DECISIONS = ("D_PV_BLEND_LONG_Q4", "D_EVAL_PV_BLEND_LONG_Q4")


def pv_blend_parameters() -> dict:
    """Return the explicitly approved parameters, without mutable shared state."""
    return {
        "schema_version": 1,
        "mechanism": "PV-BLEND-LONG",
        "minimum_lead_hours_exclusive": 12,
        "lead_buckets_hours": [[12, 18], [18, 24]],
        "history_days": 7,
        "error_window_days": 28,
        "cold_start_days": 7,
        "start": str(ACTION_START),
        "activation": str(ACTION_START + dt.timedelta(days=7)),
        "history_proxy_threshold_kw": 1.0,
        "mae_smoothing_kw": 1.0,
        "weight_power": -2,
        "sample_weighting": "equal_original_issue_target_pairs",
    }


def pv_bias_parameters() -> dict:
    """Agent's pre-recorded single-factor choice under D_OPTIMIZATION_Q4."""
    parameters = pv_blend_parameters()
    for key in ("mae_smoothing_kw", "weight_power"):
        parameters.pop(key)
    parameters.update(
        mechanism="PV-BIAS-LONG",
        correction="max(0, original_F + mean(realized_actual - saved_original_F))",
    )
    return parameters


def terminal_quartile_parameters() -> dict:
    return {
        "schema_version": 1,
        "mechanism": "TERMINAL-UPPER-QUARTILE",
        "quantile": 0.75,
        "quantile_method": "linear",
        "efficiency_factor": 0.9,
        "application": "complete 144-point snapshots ending strictly before YEAR_END; otherwise zero",
        "annual_reserve": "unchanged",
    }


def safety_procurement_parameters() -> dict:
    return {
        "schema_version": 1,
        "mechanism": "SAFETY-PROCUREMENT",
        "error_window_days": 28,
        "cold_start_days": 7,
        "start": str(ACTION_START),
        "activation": str(ACTION_START + dt.timedelta(days=7)),
        "lead_buckets_hours": [[0, 6], [6, 12], [12, 18], [18, 24]],
        "quantile": 0.8,
        "quantile_method": "linear",
        "sample_weighting": "equal_original_issue_target_pairs",
        "margin": "max(0, q80 of realized original net-demand error in kWh)",
        "planning_constraint": "grid + discharge >= max(0, raw_load - raw_pv + margin) only for procurement permissions and margin > 0",
        "grid_bound": "unchanged original Q4",
        "point_forecasts": "unchanged",
    }


def energy_lower_bound(
    time_at: dt.datetime, reserve_start: dt.datetime | None = RESERVE_START
) -> float:
    return 6000.0 if reserve_start is not None and time_at >= reserve_start else 1200.0


def reserve_start_from_config(config: dict) -> dt.datetime | None:
    """Bind validation to the run's model, preserving historical v2 evidence."""
    if config["model_version"] == "q4-v2":
        return None
    expected = {"start": str(RESERVE_START), "minimum_energy_kwh": 6000.0}
    if (
        config["model_version"]
        not in (
            MODEL_VERSION,
            BLEND_MODEL_VERSION,
            BIAS_MODEL_VERSION,
            TERMINAL_MODEL_VERSION,
            SAFETY_MODEL_VERSION,
        )
        or config.get("terminal_reserve") != expected
    ):
        raise Q4Error("config_mismatch", "unknown Q4 model or terminal reserve configuration")
    if config["model_version"] == BLEND_MODEL_VERSION:
        if (
            config.get("case_id") != "q4_3"
            or config.get("price_method") != "main"
            or config.get("optimization") is not None
            or json.dumps(config.get("pv_blend"), sort_keys=True)
            != json.dumps(pv_blend_parameters(), sort_keys=True)
        ):
            raise Q4Error("config_mismatch", "PV-BLEND-LONG configuration differs from approval")
    elif config["model_version"] in (
        BIAS_MODEL_VERSION,
        TERMINAL_MODEL_VERSION,
        SAFETY_MODEL_VERSION,
    ):
        if (
            config.get("case_id")
            not in (
                ("q4_2", "q4_3") if config["model_version"] == SAFETY_MODEL_VERSION else ("q4_3",)
            )
            or config.get("price_method") != "main"
            or config.get("pv_blend") is not None
            or json.dumps(config.get("optimization"), sort_keys=True)
            != json.dumps(
                pv_bias_parameters()
                if config["model_version"] == BIAS_MODEL_VERSION
                else safety_procurement_parameters()
                if config["model_version"] == SAFETY_MODEL_VERSION
                else terminal_quartile_parameters(),
                sort_keys=True,
            )
        ):
            raise Q4Error("config_mismatch", "PV-BIAS-LONG configuration differs from model card")
    elif config.get("pv_blend") is not None or config.get("optimization") is not None:
        raise Q4Error("config_mismatch", "v3 cannot claim a PV blend mechanism")
    return RESERVE_START


class Q4Error(MicrogridError):
    def __init__(self, status: str, message: str):
        self.status = status
        super().__init__(f"{status}: {message}")


def nonnegative(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return float(value)


@lru_cache(maxsize=32)
def _field_names(cls):
    return tuple(field.name for field in fields(cls))


def jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return {name: jsonable(getattr(value, name)) for name in _field_names(type(value))}
    if isinstance(value, (dt.datetime, dt.date, Path)):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [jsonable(item) for item in value]
    return value


def append_json(path: Path, value) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(jsonable(value), ensure_ascii=False, allow_nan=False) + "\n")


class JsonlWriter:
    """Own log handles; commit their durable prefixes before checkpoint writes."""

    def __init__(self, run_dir: Path, names, *, performance=None):
        self.performance = performance
        self.closed = False
        self.stack = ExitStack()
        try:
            self.streams = {
                name: self.stack.enter_context(
                    (run_dir / f"{name}.jsonl").open("a", encoding="utf-8")
                )
                for name in names
            }
        except BaseException:
            self.stack.close()
            raise

    def _measure(self):
        return (
            self.performance.measure("log_serialization_and_write")
            if self.performance
            else nullcontext()
        )

    def write(self, name, value):
        with self._measure():
            self.streams[name].write(
                json.dumps(jsonable(value), ensure_ascii=False, allow_nan=False) + "\n"
            )

    def flush(self):
        with self._measure():
            for stream in self.streams.values():
                stream.flush()
                os.fsync(stream.fileno())

    def close(self):
        if not self.closed:
            try:
                self.flush()
            finally:
                self.stack.close()
                self.closed = True


def atomic_json(path: Path, value, *, indent=2) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(jsonable(value), ensure_ascii=False, allow_nan=False, indent=indent) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
