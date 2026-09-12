from __future__ import annotations

import datetime as dt
import math

import pytest

from microgrid.problem.contracts import InfoItem, InfoSet
from microgrid.problem.q3_pv_forecast import PV_ACTUAL_KIND
from microgrid.problem.q3_pv_snapshot import TAIL_FALLBACK_REASON
from microgrid.problem.q3_pv_tail import (
    TAIL_EXP2_MODEL_VERSION,
    TAIL_EXP2_WEIGHTS,
    TAIL_HALF_LIFE_DAYS,
    TAIL_LAG_DAYS,
    Exp2PVTailBaseline,
)
from microgrid.schemas import InputError


def _info_for_targets(
    decision_time: dt.datetime,
    targets: tuple[dt.datetime, ...],
    *,
    omit: tuple[dt.datetime, int] | None = None,
    future_noise: bool = False,
) -> InfoSet:
    items: list[InfoItem] = []
    for target_index, target in enumerate(targets, start=1):
        for lag_days in TAIL_LAG_DAYS:
            valid_time = target - dt.timedelta(days=lag_days)
            if omit == (target, lag_days):
                continue
            items.append(
                InfoItem(
                    kind=PV_ACTUAL_KIND,
                    available_at=valid_time,
                    valid_time=valid_time,
                    value=100.0 * target_index + lag_days,
                    source_ref=f"pv:{valid_time.isoformat()}",
                )
            )
    if future_noise:
        future_time = decision_time + dt.timedelta(minutes=10)
        items.append(
            InfoItem(
                kind=PV_ACTUAL_KIND,
                available_at=future_time,
                valid_time=future_time,
                value=999_999.0,
                source_ref="future-must-be-filtered",
            )
        )
    return InfoSet.from_raw(decision_time, tuple(items))


def test_exp2_formula_and_provenance_are_exact() -> None:
    decision = dt.datetime(2025, 3, 10, 11, 50)
    targets = (decision + dt.timedelta(hours=23, minutes=50), decision + dt.timedelta(hours=24))
    result = Exp2PVTailBaseline().predict(
        decision_time=decision,
        target_slot_ends=targets,
        info_set=_info_for_targets(decision, targets),
    )

    raw = tuple(2 ** (-(day - 1) / 2) for day in range(1, 8))
    expected_weights = tuple(value / math.fsum(raw) for value in raw)
    assert TAIL_HALF_LIFE_DAYS == 2.0
    assert TAIL_EXP2_WEIGHTS == pytest.approx(expected_weights)
    assert sum(TAIL_EXP2_WEIGHTS) == pytest.approx(1.0)
    assert tuple(point.valid_time for point in result.points) == targets
    for target_index, point in enumerate(result.points, start=1):
        expected_kw = sum(
            weight * (100.0 * target_index + lag_days)
            for weight, lag_days in zip(TAIL_EXP2_WEIGHTS, TAIL_LAG_DAYS, strict=True)
        )
        assert point.power_kw == pytest.approx(expected_kw)
        assert point.energy_kwh == pytest.approx(expected_kw / 6)
        assert point.available_at == point.training_cutoff == decision
        assert point.model_version == TAIL_EXP2_MODEL_VERSION
        assert point.fallback_reason == TAIL_FALLBACK_REASON
        assert len(point.source_refs) == 7


def test_future_actual_is_filtered_before_tail_prediction() -> None:
    decision = dt.datetime(2025, 3, 10, 11, 50)
    target = decision + dt.timedelta(hours=24)

    clean = Exp2PVTailBaseline().predict(
        decision_time=decision,
        target_slot_ends=(target,),
        info_set=_info_for_targets(decision, (target,)),
    )
    noisy = Exp2PVTailBaseline().predict(
        decision_time=decision,
        target_slot_ends=(target,),
        info_set=_info_for_targets(decision, (target,), future_noise=True),
    )
    assert noisy == clean


def test_missing_any_of_seven_lags_fails_without_silent_fallback() -> None:
    decision = dt.datetime(2025, 3, 10, 11, 50)
    target = decision + dt.timedelta(hours=24)
    info = _info_for_targets(decision, (target,), omit=(target, 4))

    with pytest.raises(InputError, match="missing 4-day actual PV lag"):
        Exp2PVTailBaseline().predict(
            decision_time=decision,
            target_slot_ends=(target,),
            info_set=info,
        )


@pytest.mark.parametrize(
    "targets, message",
    [
        ((dt.datetime(2025, 3, 11, 11, 40), dt.datetime(2025, 3, 11, 11, 20)), "increasing"),
        ((dt.datetime(2025, 3, 11, 11, 20), dt.datetime(2025, 3, 11, 11, 40)), "continuous"),
        ((dt.datetime(2025, 3, 11, 12, 0),), "through decision\\+24h"),
    ],
)
def test_tail_targets_must_be_an_ordered_24h_suffix(
    targets: tuple[dt.datetime, ...], message: str
) -> None:
    decision = dt.datetime(2025, 3, 10, 11, 50)
    info = _info_for_targets(decision, targets)

    with pytest.raises(InputError, match=message):
        Exp2PVTailBaseline().predict(
            decision_time=decision,
            target_slot_ends=targets,
            info_set=info,
        )
