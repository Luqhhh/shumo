from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from microgrid.problem.contracts import (
    INITIAL_SOC_KWH,
    BatteryAction,
    BatteryState,
    CaseResult,
    IntervalResult,
)
from microgrid.problem.q2_inputs import ActualInterval, FixedPricePoint
from microgrid.problem.result_io import case_result_to_dict, load_case_result, save_case_result
from microgrid.schemas import InputError


def test_actual_interval_converts_ten_minute_power_to_energy() -> None:
    actual = ActualInterval(
        day=dt.date(2025, 2, 1),
        slot=0,
        start=dt.datetime(2025, 2, 1, 0, 0),
        end=dt.datetime(2025, 2, 1, 0, 10),
        load_kw=600.0,
        pv_kw=300.0,
        load_source_ref="附件2.xlsx!负荷!B2 sha256=aaaaaaaaaaaa",
        pv_source_ref="附件2.xlsx!光伏!B2 sha256=aaaaaaaaaaaa",
    )

    assert actual.load_kw == 600.0
    assert actual.pv_kw == 300.0
    assert actual.load_kwh == pytest.approx(100.0)
    assert actual.pv_kwh == pytest.approx(50.0)


@pytest.mark.parametrize("bad_value", [-1.0, float("nan"), float("inf")])
def test_value_records_reject_negative_or_non_finite_values(bad_value: float) -> None:
    with pytest.raises(InputError, match="price_cny_per_kwh"):
        FixedPricePoint(slot=0, price_cny_per_kwh=bad_value, source_ref="cell")


def test_q2_case_result_roundtrip_preserves_adapter_provenance(tmp_path: Path) -> None:
    actual = ActualInterval(
        day=dt.date(2025, 2, 1),
        slot=0,
        start=dt.datetime(2025, 2, 1, 0, 0),
        end=dt.datetime(2025, 2, 1, 0, 10),
        load_kw=600.0,
        pv_kw=300.0,
        load_source_ref="附件2.xlsx!负荷!B2 sha256=aaaaaaaaaaaa",
        pv_source_ref="附件2.xlsx!光伏!B2 sha256=aaaaaaaaaaaa",
    )
    state = BatteryState(INITIAL_SOC_KWH)
    interval = IntervalResult(
        day=actual.day,
        slot=actual.slot,
        load_kw=actual.load_kw,
        pv_kw=actual.pv_kw,
        planned_purchase_kwh=0.0,
        adjusted_purchase_kwh=0.0,
        emergency_purchase_kwh=0.0,
        action=BatteryAction(),
        state_start=state,
        state_end=state,
        source_ref=f"{actual.load_source_ref}; {actual.pv_source_ref}",
    )
    result = CaseResult(
        case_id="q2",
        run_id="synthetic-adapter-roundtrip",
        status="interface-test",
        is_synthetic=True,
        intervals=(interval,),
        metadata={"input_hashes": [["attachment2", "a" * 64]]},
    )

    path = tmp_path / "q2-result.json"
    save_case_result(path, result)
    loaded = load_case_result(path)

    assert case_result_to_dict(loaded) == case_result_to_dict(result)
    assert loaded.is_synthetic is True
    assert loaded.intervals[0].source_ref == interval.source_ref
