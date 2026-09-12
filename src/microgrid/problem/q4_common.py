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


def energy_lower_bound(
    time_at: dt.datetime, reserve_start: dt.datetime | None = RESERVE_START
) -> float:
    return 6000.0 if reserve_start is not None and time_at >= reserve_start else 1200.0


def reserve_start_from_config(config: dict) -> dt.datetime | None:
    """Bind validation to the run's model, preserving historical v2 evidence."""
    if config["model_version"] == "q4-v2":
        return None
    expected = {"start": str(RESERVE_START), "minimum_energy_kwh": 6000.0}
    if config["model_version"] != MODEL_VERSION or config.get("terminal_reserve") != expected:
        raise Q4Error("config_mismatch", "unknown Q4 model or terminal reserve configuration")
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
