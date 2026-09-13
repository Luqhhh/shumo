"""Production Q3 window factory over immutable causal forecast snapshots."""

from __future__ import annotations

import datetime as dt

from ..schemas import InputError
from .contracts import STEP_MINUTES, BatteryState, InfoItem, InfoSet
from .q2_inputs import FixedPricePoint
from .q3_load_snapshot import (
    LoadForecastSnapshot,
    build_q3_load_window,
)
from .q3_plan_ledger import Q3PlanLedger
from .q3_pv_snapshot import (
    PVForecastSnapshot,
    PVTailBaseline,
    TailForecast,
    build_q3_pv_window,
)
from .q3_pv_tail import Exp2PVTailBaseline
from .q3_window import Q3WindowInput, build_q3_window_input

_RELEASE_HOURS = (0, 6, 12, 18)


def _latest_release(decision_time: dt.datetime) -> dt.datetime:
    if decision_time.second or decision_time.microsecond or decision_time.minute % STEP_MINUTES:
        raise InputError("Q3 decision time must be on a ten-minute boundary")
    minute = decision_time.hour * 60 + decision_time.minute
    release_minute = max(hour * 60 for hour in _RELEASE_HOURS if hour * 60 <= minute)
    return dt.datetime.combine(decision_time.date(), dt.time()) + dt.timedelta(
        minutes=release_minute
    )


class _TailCapture:
    def __init__(self, baseline: PVTailBaseline) -> None:
        self._baseline = baseline
        self.result: TailForecast | None = None

    def predict(
        self,
        *,
        decision_time: dt.datetime,
        target_slot_ends: tuple[dt.datetime, ...],
        info_set: InfoSet,
    ) -> TailForecast:
        self.result = self._baseline.predict(
            decision_time=decision_time,
            target_slot_ends=target_slot_ends,
            info_set=info_set,
        )
        return self.result


class Q3SnapshotWindowFactory:
    """Select only the latest published immutable versions for each MPC step.

    Snapshot creation remains at the four approved publication times. This
    factory never recomputes them between releases. Raw information is exposed
    to the tail model only through a freshly filtered ``InfoSet``.
    """

    def __init__(
        self,
        *,
        load_snapshots: tuple[LoadForecastSnapshot, ...],
        pv_snapshots: tuple[PVForecastSnapshot, ...],
        fixed_prices: tuple[FixedPricePoint, ...],
        raw_info_items: tuple[InfoItem, ...],
        tail_baseline: PVTailBaseline | None = None,
        evaluation_end: dt.datetime | None = None,
    ) -> None:
        self._load_by_issue = self._unique_load_snapshots(load_snapshots)
        self._pv_by_issue = self._unique_pv_snapshots(pv_snapshots)
        if set(self._load_by_issue) != set(self._pv_by_issue):
            raise InputError("Q3 load and PV snapshots must have identical issue times")
        self._fixed_prices = fixed_prices
        self._raw_info_items = raw_info_items
        self._tail_baseline = tail_baseline or Exp2PVTailBaseline()
        self._evaluation_end = evaluation_end
        self._used_issues: set[dt.datetime] = set()
        self._tail_by_decision: dict[dt.datetime, TailForecast] = {}

    @staticmethod
    def _unique_load_snapshots(
        snapshots: tuple[LoadForecastSnapshot, ...],
    ) -> dict[dt.datetime, LoadForecastSnapshot]:
        result = {snapshot.issue_time: snapshot for snapshot in snapshots}
        if len(result) != len(snapshots):
            raise InputError("Q3 load snapshots contain a duplicate issue time")
        return result

    @staticmethod
    def _unique_pv_snapshots(
        snapshots: tuple[PVForecastSnapshot, ...],
    ) -> dict[dt.datetime, PVForecastSnapshot]:
        result = {snapshot.issue_time: snapshot for snapshot in snapshots}
        if len(result) != len(snapshots):
            raise InputError("Q3 PV snapshots contain a duplicate issue time")
        return result

    @property
    def used_load_snapshots(self) -> tuple[LoadForecastSnapshot, ...]:
        return tuple(self._load_by_issue[issue] for issue in sorted(self._used_issues))

    @property
    def used_pv_snapshots(self) -> tuple[PVForecastSnapshot, ...]:
        return tuple(self._pv_by_issue[issue] for issue in sorted(self._used_issues))

    @property
    def used_tail_forecasts(self) -> tuple[TailForecast, ...]:
        return tuple(self._tail_by_decision[key] for key in sorted(self._tail_by_decision))

    def __call__(
        self,
        *,
        decision_time: dt.datetime,
        battery_state: BatteryState,
        current_ledger: Q3PlanLedger | None,
    ) -> Q3WindowInput:
        issue_time = _latest_release(decision_time)
        load_snapshot = self._load_by_issue.get(issue_time)
        pv_snapshot = self._pv_by_issue.get(issue_time)
        if load_snapshot is None or pv_snapshot is None:
            raise InputError(f"Q3 has no immutable forecast snapshot for {issue_time}")
        info_set = InfoSet.from_raw(decision_time, self._raw_info_items)
        horizon_end = self._evaluation_end
        load_window = build_q3_load_window(
            decision_time=decision_time,
            snapshot=load_snapshot,
            horizon_end=horizon_end,
        )
        capture = _TailCapture(self._tail_baseline)
        pv_window = build_q3_pv_window(
            decision_time=decision_time,
            snapshot=pv_snapshot,
            info_set=info_set,
            tail_baseline=capture,
            horizon_end=horizon_end,
        )
        window = build_q3_window_input(
            decision_time=decision_time,
            battery_state=battery_state,
            load_window=load_window,
            pv_window=pv_window,
            fixed_prices=self._fixed_prices,
            current_ledger=current_ledger,
            evaluation_end=self._evaluation_end,
        )
        self._used_issues.add(issue_time)
        if capture.result is not None:
            existing = self._tail_by_decision.get(decision_time)
            if existing is not None and existing != capture.result:
                raise InputError("Q3 tail forecast changed for a repeated decision time")
            self._tail_by_decision[decision_time] = capture.result
        return window
