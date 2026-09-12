"""Incremental forecasts trained ONLY through causal InfoSet views."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from .contracts import InfoSet
from .q4_common import (
    BIAS_MODEL_VERSION,
    BLEND_MODEL_VERSION,
    MODEL_VERSION,
    SAFETY_MODEL_VERSION,
    STEP,
    TERMINAL_MODEL_VERSION,
    YEAR_END,
    Q4Error,
    jsonable,
    nonnegative,
    terminal_quartile_parameters,
)
from .q4_pv_bias import LongPVBias
from .q4_pv_blend import LongPVBlend
from .q4_risk_history import JointRiskHistory


class LagAR:
    def __init__(self, lags: tuple[int, ...], weights: tuple[float, ...]):
        self.lags, self.weights = lags, weights
        self.values: list[float] = []
        self.last_time: dt.datetime | None = None
        self.latest_residual: float | None = None
        self.numerator = self.denominator = 0.0
        self.pairs = 0

    def add(self, time: dt.datetime, value: float):
        if self.last_time is not None and time != self.last_time + STEP:
            raise Q4Error("missing_history", "AR histories must be continuous")
        nonnegative("history", value)
        self.values.append(value)
        self.last_time = time
        index = len(self.values) - 1
        if index >= max(self.lags):
            residual = value - sum(
                w * self.values[index - lag] for w, lag in zip(self.weights, self.lags, strict=True)
            )
            if self.latest_residual is not None:
                self.numerator += self.latest_residual * residual
                self.denominator += self.latest_residual**2
                self.pairs += 1
            self.latest_residual = residual

    def predict(self, count: int, *, correction: bool = True):
        if self.latest_residual is None and correction:
            raise Q4Error("missing_history", "latest complete-lag residual unavailable")
        phi = (
            float(np.clip(self.numerator / self.denominator, 0, 0.999)) if self.denominator else 0.0
        )
        values, traces = [], []
        for h in range(1, count + 1):
            indices = [len(self.values) - 1 + h - lag for lag in self.lags]
            if any(index < 0 or index >= len(self.values) for index in indices):
                raise Q4Error("missing_history", "forecast lag is not yet visible")
            lag_values = [self.values[i] for i in indices]
            base = sum(w * x for w, x in zip(self.weights, lag_values, strict=True))
            value = max(0.0, base + phi**h * self.latest_residual) if correction else base
            values.append(value)
            traces.append(
                {
                    "horizon_steps": h,
                    "lag_steps": self.lags,
                    "lag_values": lag_values,
                    "lag_weights": self.weights,
                    "phi": phi if correction else None,
                    "latest_residual": self.latest_residual if correction else None,
                    "residual_pair_count": self.pairs,
                    "historical_sample_count": len(self.values),
                    "prediction_value": value,
                }
            )
        return tuple(values), tuple(traces)


@dataclass(frozen=True)
class ForecastSnapshot:
    snapshot_id: str
    case_id: str
    issue_time: dt.datetime
    slots: tuple[dt.datetime, ...]
    load_kwh: tuple[float, ...]
    pv_kwh: tuple[float, ...]
    prices: tuple[float, ...]
    terminal_value: float
    price_method: str
    source_hashes: dict[str, str]
    traces: dict
    model_version: str = MODEL_VERSION
    schema_version: int = 1

    def sliced(self, time: dt.datetime):
        from dataclasses import replace

        index = int((time - self.issue_time) / STEP)
        if index < 0 or index >= len(self.slots) or self.slots[index] != time:
            raise Q4Error("missing_forecast", "saved snapshot does not cover current window")
        return replace(
            self,
            slots=self.slots[index:],
            load_kwh=self.load_kwh[index:],
            pv_kwh=self.pv_kwh[index:],
            prices=self.prices[index:],
        )


class Q4Forecaster:
    def __init__(
        self,
        case_id: str,
        source_hashes: dict[str, str],
        price_method: str = "main",
        *,
        model_version: str = MODEL_VERSION,
    ):
        if case_id not in ("q4_2", "q4_3") or price_method not in ("main", "lag1"):
            raise ValueError("invalid forecast case or price method")
        self.case_id, self.source_hashes, self.price_method = case_id, source_hashes, price_method
        if model_version not in (
            "q4-v2",
            MODEL_VERSION,
            BLEND_MODEL_VERSION,
            BIAS_MODEL_VERSION,
            TERMINAL_MODEL_VERSION,
            SAFETY_MODEL_VERSION,
        ):
            raise ValueError("unknown Q4 forecast model version")
        if model_version in (BLEND_MODEL_VERSION, BIAS_MODEL_VERSION, TERMINAL_MODEL_VERSION) and (
            case_id != "q4_3" or price_method != "main"
        ):
            raise ValueError("PV blend approval applies only to Q4-3/main")
        self.model_version = model_version
        self.blend = (
            LongPVBlend()
            if model_version == BLEND_MODEL_VERSION
            else LongPVBias()
            if model_version == BIAS_MODEL_VERSION
            else None
        )
        self.correction_key = "pv_bias" if model_version == BIAS_MODEL_VERSION else "pv_blend"
        if model_version == SAFETY_MODEL_VERSION and price_method != "main":
            raise ValueError("safety procurement trial is scoped to main")
        self.risk = JointRiskHistory() if model_version == SAFETY_MODEL_VERSION else None
        self.load = LagAR((1008, 2016, 3024, 4032), (8 / 15, 4 / 15, 2 / 15, 1 / 15))
        self.price = LagAR((144, 1008, 2016), (0.5, 0.3, 0.2))
        self.pv = LagAR(tuple(144 * i for i in range(1, 8)), (1 / 7,) * 7)
        self.official = defaultdict(list)
        self.errors = defaultdict(lambda: [0.0, 0])
        self.time: dt.datetime | None = None

    def ingest(self, info: InfoSet):
        # Consume each item once. The engine constructs these incremental views
        # using InfoSet.from_raw before passing them to this object.
        for item in sorted(info.visible_items, key=lambda x: (x.available_at, x.kind)):
            if item.kind == "pv_forecast_kw":
                if self.case_id != "q4_3":
                    continue
                lead = int((item.valid_time - item.available_at).total_seconds() / 3600)
                if lead not in range(1, 25):
                    raise Q4Error("invalid_forecast", "official lead outside 1..24")
                self.official[item.valid_time].append((item, lead))
            elif item.kind in ("load_actual_kw", "pv_actual_kw", "price_actual"):
                if item.valid_time != item.available_at or item.valid_time > info.decision_time:
                    raise Q4Error("invalid_history", "actual must arrive at its right endpoint")
                series = {
                    "load_actual_kw": self.load,
                    "pv_actual_kw": self.pv,
                    "price_actual": self.price,
                }[item.kind]
                series.add(item.valid_time, float(item.value))
                if item.kind == "pv_actual_kw":
                    if self.risk is not None:
                        if (
                            not self.load.last_time
                            == self.price.last_time
                            == self.pv.last_time
                            == item.valid_time
                        ):
                            raise Q4Error(
                                "invalid_history", "joint actual series are not synchronized"
                            )
                        self.risk.observe(
                            item.valid_time,
                            self.load.values[-1],
                            self.pv.values[-1],
                            self.price.values[-1],
                        )
                    if self.blend is not None:
                        self.blend.observe(item.valid_time, float(item.value))
                    for forecast, lead in self.official.pop(item.valid_time, []):
                        self.errors[lead][0] += abs(float(forecast.value) - float(item.value))
                        self.errors[lead][1] += 1
        self.time = info.decision_time

    def training_state(self):
        state = {
            "time": self.time,
            "load": {
                "pairs": self.load.pairs,
                "numerator": self.load.numerator,
                "denominator": self.load.denominator,
                "latest_residual": self.load.latest_residual,
                "count": len(self.load.values),
            },
            "price": {
                "pairs": self.price.pairs,
                "numerator": self.price.numerator,
                "denominator": self.price.denominator,
                "latest_residual": self.price.latest_residual,
                "count": len(self.price.values),
            },
            "lead_errors": dict(self.errors),
        }
        if self.risk is not None:
            state["safety_procurement"] = self.risk.witness()
        if self.blend is not None:
            state[self.correction_key] = self.blend.witness()
        return state

    def refresh(self, info: InfoSet) -> ForecastSnapshot:
        time = info.decision_time
        if time != self.time or any(
            series.last_time != time for series in (self.load, self.pv, self.price)
        ):
            raise Q4Error("missing_history", "latest ended actual required for all series")
        if time.time() not in tuple(dt.time(h) for h in (0, 6, 12, 18)):
            raise ValueError("forecast refresh outside approved events")
        count = min(144, int((YEAR_END - time) / STEP))
        if count <= 0:
            raise ValueError("no targets beyond annual boundary")
        load, load_trace = self.load.predict(count)
        if self.price_method == "main":
            price, price_trace = self.price.predict(count)
        else:
            price = tuple(
                self.price.values[len(self.price.values) - 1 + h - 144] for h in range(1, count + 1)
            )
            price_trace = tuple(
                {
                    "horizon_steps": h,
                    "lag_steps": [144],
                    "lag_weights": [1.0],
                    "lag_values": [value],
                    "prediction_value": value,
                    "historical_sample_count": len(self.price.values),
                }
                for h, value in enumerate(price, 1)
            )
        if self.case_id == "q4_2":
            pv, pv_trace = self.pv.predict(count, correction=False)
        else:
            hourly, pv_trace = [], []
            for lead in range(1, (count + 5) // 6 + 1):
                target = time + dt.timedelta(hours=lead)
                candidates = self.official.get(target, [])
                if not candidates:
                    raise Q4Error("missing_forecast", str(target))
                maes, counts = [], []
                for _, bucket in candidates:
                    total, samples = self.errors[bucket]
                    if not samples:
                        raise Q4Error("missing_forecast_history", f"lead {bucket} not initialized")
                    maes.append(total / samples)
                    counts.append(samples)
                weights = np.power(np.array(maes) + 1.0, -2)
                weights /= weights.sum()
                value = float(
                    sum(
                        w * float(item.value)
                        for w, (item, _) in zip(weights, candidates, strict=True)
                    )
                )
                hourly.append(value)
                pv_trace.append(
                    {
                        "valid_time": target,
                        "unit": "kW",
                        "versions": [
                            {
                                "issue_time": item.available_at,
                                "lead_hours": bucket,
                                "forecast_kw": item.value,
                                "source_ref": item.source_ref,
                                "historical_mae_kw": mae,
                                "historical_sample_count": samples,
                                "weight": float(weight),
                            }
                            for (item, bucket), mae, samples, weight in zip(
                                candidates, maes, counts, weights, strict=True
                            )
                        ],
                        "prediction_value": value,
                    }
                )
            pv = tuple(
                float(x)
                for x in np.interp(
                    np.arange(1, count + 1) / 6,
                    np.arange(len(hourly) + 1),
                    [self.pv.values[-1], *hourly],
                )
            )
        slots = tuple(time + i * STEP for i in range(count))
        traces = {
            "training_cutoff": time,
            "available_at": time,
            "load_unit": "kW",
            "pv_unit": "kW",
            "price_unit": "元/kWh",
            "load": load_trace,
            "pv": pv_trace,
            "price": price_trace,
            "pv_boundary_proxy_kw": self.pv.values[-1] if self.case_id == "q4_3" else None,
            "valid_times": [slot + STEP for slot in slots],
        }
        if self.blend is not None:
            historical, _ = self.pv.predict(count, correction=False)
            pv, traces[self.correction_key] = self.blend.apply(time, pv, historical)
        if self.risk is not None:
            traces["safety_procurement"] = self.risk.apply(time, load, pv, price)
        payload = {
            "case": self.case_id,
            "issue": str(time),
            "method": self.price_method,
            "load": load,
            "pv": pv,
            "price": price,
            "sources": self.source_hashes,
            "model": self.model_version,
        }
        snapshot_id = hashlib.sha256(
            json.dumps(jsonable(payload), sort_keys=True).encode()
        ).hexdigest()
        terminal_value = (
            0.9 * sum(price) / 144 if count == 144 and slots[-1] + STEP < YEAR_END else 0.0
        )
        if self.model_version == TERMINAL_MODEL_VERSION:
            terminal_value = (
                0.9 * float(np.quantile(price, 0.75, method="linear"))
                if count == 144 and slots[-1] + STEP < YEAR_END
                else 0.0
            )
            traces["terminal_value_rule"] = {
                "parameters": terminal_quartile_parameters(),
                "value": terminal_value,
                "original_issue_time": str(time),
            }
        return ForecastSnapshot(
            snapshot_id,
            self.case_id,
            time,
            slots,
            tuple(x / 6 for x in load),
            tuple(x / 6 for x in pv),
            price,
            terminal_value,
            self.price_method,
            self.source_hashes,
            traces,
            model_version=self.model_version,
        )
