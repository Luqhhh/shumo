"""Realized original joint forecast errors, isolated from future measurements."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import defaultdict, deque

import numpy as np

from .q4_common import (
    ACTION_START,
    STEP,
    Q4Error,
    nonnegative,
    safety_procurement_parameters,
    scenario_procurement_parameters,
)


class JointRiskHistory:
    def __init__(self, method="safety"):
        if method not in ("safety", "scenario"):
            raise ValueError("unknown joint-risk mechanism")
        self.method = method
        self.pending = defaultdict(list)
        self.realized = deque()
        self.pending_count = 0
        self.last_issue = None
        self.last_input = self.last_result = None
        self.issue_count = 0
        self.chain = hashlib.sha256(
            (
                "SCENARIO-PROCUREMENT empty at 2025-02-01"
                if method == "scenario"
                else "SAFETY-PROCUREMENT empty at 2025-02-01"
            ).encode()
        ).digest()

    def _bind(self, event):
        self.chain = hashlib.sha256(
            self.chain + json.dumps(event, separators=(",", ":"), allow_nan=False).encode()
        ).digest()

    def witness(self):
        return {
            "schema_version": 1,
            "history_sha256": self.chain.hex(),
            "pending_pair_count": self.pending_count,
            "realized_pair_count": len(self.realized),
            "issue_count": self.issue_count,
            "last_issue": str(self.last_issue) if self.last_issue is not None else None,
        }

    def observe(self, target, load_kw, pv_kw, price):
        pairs = self.pending.pop(target, [])
        if not pairs:
            return
        for name, value in [
            ("actual load", load_kw),
            ("actual PV", pv_kw),
            ("actual price", price),
        ]:
            nonnegative(name, value)
        for issue, bucket, load, pv, p in pairs:
            if not issue < target:
                raise Q4Error("invalid_history", "joint pair is not a prior forecast")
            self.realized.append((target, issue, bucket, load_kw - load, pv_kw - pv, price - p))
        self.pending_count -= len(pairs)
        self._bind(["realized", str(target), load_kw, pv_kw, price, len(pairs)])

    def apply(self, issue, load_kw, pv_kw, prices):
        load_kw, pv_kw, prices = tuple(load_kw), tuple(pv_kw), tuple(prices)
        if (
            issue < ACTION_START
            or not 0 < len(load_kw) <= 144
            or not len(load_kw) == len(pv_kw) == len(prices)
            or issue.time() not in tuple(dt.time(h) for h in (0, 6, 12, 18))
        ):
            raise Q4Error("invalid_forecast", "invalid joint risk issue/dimensions")
        inputs = (load_kw, pv_kw, prices)
        for series in inputs:
            for value in series:
                nonnegative("joint prediction", value)
        if issue == self.last_issue:
            if inputs != self.last_input:
                raise Q4Error("invalid_forecast", "frozen risk issue cannot be rewritten")
            return self.last_result
        if self.last_issue is not None and issue < self.last_issue:
            raise Q4Error("invalid_forecast", "joint risk issues must advance")
        lower = issue - dt.timedelta(days=28)
        pruned = 0
        while self.realized and self.realized[0][0] <= lower:
            self.realized.popleft()
            pruned += 1
        if pruned:
            self._bind(["prune", str(issue), pruned])
        samples = defaultdict(list)
        for target, original_issue, bucket, l_error, pv_error, p_error in self.realized:
            if not lower < target <= issue:
                raise Q4Error("invalid_history", "risk sample target not visible")
            samples[bucket].append((target, original_issue, l_error, pv_error, p_error))
        q80 = {
            bucket: float(np.quantile([(x[2] - x[3]) / 6 for x in values], 0.8, method="linear"))
            for bucket, values in samples.items()
        }
        cold = issue < ACTION_START + dt.timedelta(days=7)
        representatives = {}
        if self.method == "scenario" and not cold:
            for bucket, values in samples.items():
                representatives[bucket] = []
                net_errors = [(x[2] - x[3]) / 6 for x in values]
                for quantile in (0.2, 0.8):
                    threshold = float(np.quantile(net_errors, quantile, method="linear"))
                    row = min(
                        values, key=lambda x: (abs((x[2] - x[3]) / 6 - threshold), x[0], x[1])
                    )
                    representatives[bucket].append(
                        {
                            "load_error_kw": row[2],
                            "pv_error_kw": row[3],
                            "price_error": row[4],
                            "source_target": str(row[0]),
                            "source_issue": str(row[1]),
                            "net_error_quantile_kwh": threshold,
                            "quantile": quantile,
                        }
                    )
        before = self.witness()
        points = []
        pairs = []
        for index, (load, pv, p) in enumerate(zip(*inputs, strict=True), 1):
            target = issue + index * STEP
            bucket = min((index - 1) // 36, 3)
            count = len(samples[bucket])
            quantile = q80.get(bucket)
            margin = 0.0 if cold or not count else max(0.0, quantile)
            points.append(
                {
                    "valid_time": str(target),
                    "horizon_steps_from_original_issue": index,
                    "lead_bucket": bucket,
                    "original_load_kw": load,
                    "original_pv_kw": pv,
                    "original_price": p,
                    "sample_count": count,
                    "q80_net_error_kwh": quantile,
                    "margin_kwh": margin,
                }
            )
            if self.method == "scenario":
                point = points[-1]
                point.pop("margin_kwh")
                point.pop("q80_net_error_kwh")
                zero = {
                    "load_error_kw": 0.0,
                    "pv_error_kw": 0.0,
                    "price_error": 0.0,
                    "source_target": None,
                    "source_issue": None,
                    "net_error_quantile_kwh": None,
                    "quantile": None,
                }
                rows = representatives.get(bucket)
                point["joint_error_scenarios"] = [
                    rows[0] if rows else dict(zero),
                    dict(zero),
                    rows[1] if rows else dict(zero),
                ]
            self.pending[target].append((issue, bucket, load, pv, p))
            self.pending_count += 1
            pairs.append([str(target), bucket, load, pv, p])
        self._bind(["issue", str(issue), pairs])
        self.issue_count += 1
        self.last_issue = issue
        self.last_input = inputs
        self.last_result = {
            "schema_version": 1,
            "parameters": scenario_procurement_parameters()
            if self.method == "scenario"
            else safety_procurement_parameters(),
            "original_issue_time": str(issue),
            "training_cutoff": str(issue),
            "target_lower_exclusive": str(lower),
            "cold_start": cold,
            "history_before_issue": before,
            "points": points,
        }
        if self.method == "scenario":
            self.last_result["active"] = bool(representatives)
        return self.last_result


def procurement_margins(forecast):
    trace = forecast.traces.get("safety_procurement")
    if trace is None:
        return (0.0,) * len(forecast.slots)
    points = {point["valid_time"]: point for point in trace["points"]}
    result = []
    for slot, load, pv, price in zip(
        forecast.slots, forecast.load_kwh, forecast.pv_kwh, forecast.prices, strict=True
    ):
        point = points[str(slot + STEP)]
        if (
            point["original_load_kw"] / 6 != load
            or point["original_pv_kw"] / 6 != pv
            or point["original_price"] != price
        ):
            raise Q4Error(
                "invalid_forecast", "risk margin is not bound to original point forecasts"
            )
        result.append(nonnegative("procurement margin", point["margin_kwh"]))
    return tuple(result)


def procurement_floor_activation(forecast, permissions):
    margins = procurement_margins(forecast)
    return tuple(
        permission != "fixed" and margin > 0 and max(0.0, load - pv + margin) > 0
        for permission, margin, load, pv in zip(
            permissions, margins, forecast.load_kwh, forecast.pv_kwh, strict=True
        )
    )
