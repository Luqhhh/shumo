"""Approved long-lead PV blend, learning only saved, realized forecast pairs."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from collections import defaultdict, deque

from .q4_common import ACTION_START, STEP, Q4Error, nonnegative, pv_blend_parameters


class LongPVBlend:
    def __init__(self):
        self.pending = defaultdict(list)
        self.realized = deque()
        self.pending_count = 0
        self.last_issue = None
        self.last_input = self.last_result = None
        self.issue_count = 0
        self.chain = hashlib.sha256(b"PV-BLEND-LONG empty at 2025-02-01").digest()

    def _bind(self, event):
        self.chain = hashlib.sha256(
            self.chain + json.dumps(event, separators=(",", ":"), allow_nan=False).encode()
        ).digest()

    def observe(self, target: dt.datetime, actual_kw: float):
        """Called only after InfoSet and the continuous history validate arrival."""
        pairs = self.pending.pop(target, [])
        if pairs:
            actual_kw = nonnegative("realized PV", actual_kw)
            for issue, bucket, group, original, historical in pairs:
                if not issue < target:
                    raise Q4Error("invalid_history", "PV pair is not a prior forecast")
                self.realized.append(
                    (target, bucket, group, abs(actual_kw - original), abs(actual_kw - historical))
                )
            self.pending_count -= len(pairs)
            self._bind(["realized", str(target), actual_kw, len(pairs)])

    def witness(self):
        # A replayable incremental history digest avoids serializing every old pair
        # in every checkpoint. Forecast logs remain the source for reconstruction.
        return {
            "schema_version": 1,
            "history_sha256": self.chain.hex(),
            "pending_pair_count": self.pending_count,
            "realized_pair_count": len(self.realized),
            "issue_count": self.issue_count,
            "last_issue": str(self.last_issue) if self.last_issue is not None else None,
        }

    def apply(self, issue: dt.datetime, original, historical):
        original, historical = tuple(original), tuple(historical)
        if (
            issue < ACTION_START
            or not 0 < len(original) <= 144
            or len(original) != len(historical)
            or issue.time() not in tuple(dt.time(h) for h in (0, 6, 12, 18))
        ):
            raise Q4Error("invalid_forecast", "invalid PV blend initialization or dimensions")
        for x in (*original, *historical):
            nonnegative("PV prediction", x)
        if issue == self.last_issue:
            if self.last_input != (original, historical):
                raise Q4Error("invalid_forecast", "frozen PV issue cannot be rewritten")
            return self.last_result
        if self.last_issue is not None and issue < self.last_issue:
            raise Q4Error("invalid_forecast", "PV issues must advance chronologically")
        lower = issue - dt.timedelta(days=28)
        pruned = 0
        while self.realized and self.realized[0][0] <= lower:
            self.realized.popleft()
            pruned += 1
        if pruned:
            self._bind(["prune", str(issue), pruned])
        samples = defaultdict(list)
        for target, bucket, group, f_error, h_error in self.realized:
            if not lower < target <= issue:
                raise Q4Error("invalid_history", "PV training target is not visible in window")
            samples[(bucket, group)].append((f_error, h_error))
        statistics = {
            key: (len(values), math.fsum(x[0] for x in values), math.fsum(x[1] for x in values))
            for key, values in samples.items()
        }
        cold = issue < ACTION_START + dt.timedelta(days=7)
        before = self.witness()
        values, points, pairs = [], [], []
        for index, (f_kw, h_kw) in enumerate(zip(original, historical, strict=True), 1):
            target = issue + index * STEP
            bucket = "12-18h" if 72 < index <= 108 else "18-24h" if index > 108 else None
            group = "history_gt_1kw" if h_kw > 1.0 else "history_le_1kw"
            n, f_sum, h_sum = statistics.get((bucket, group), (0, 0.0, 0.0))
            m_f, m_h = (f_sum / n, h_sum / n) if n else (None, None)
            alpha = 1.0
            if bucket is not None and not cold and n:
                a_f, a_h = 1 / (m_f + 1.0) ** 2, 1 / (m_h + 1.0) ** 2
                alpha = a_f / (a_f + a_h)
            value = f_kw if alpha == 1.0 else alpha * f_kw + (1 - alpha) * h_kw
            values.append(value)
            points.append(
                {
                    "valid_time": str(target),
                    "horizon_steps_from_original_issue": index,
                    "bucket": bucket,
                    "history_proxy_group": group,
                    "original_v3_kw": f_kw,
                    "history_kw": h_kw,
                    "sample_count": n,
                    "original_abs_error_sum_kw": f_sum,
                    "history_abs_error_sum_kw": h_sum,
                    "original_mae_kw": m_f,
                    "history_mae_kw": m_h,
                    "alpha": alpha,
                    "prediction_kw": value,
                }
            )
            if bucket is not None:
                self.pending[target].append((issue, bucket, group, f_kw, h_kw))
                self.pending_count += 1
                pairs.append([str(target), bucket, group, f_kw, h_kw])
        self._bind(["issue", str(issue), pairs])
        self.issue_count += 1
        self.last_issue = issue
        trace = {
            "schema_version": 1,
            "parameters": pv_blend_parameters(),
            "original_issue_time": str(issue),
            "training_cutoff": str(issue),
            "target_lower_exclusive": str(lower),
            "cold_start": cold,
            "history_before_issue": before,
            "points": points,
        }
        self.last_input, self.last_result = (original, historical), (tuple(values), trace)
        return self.last_result
