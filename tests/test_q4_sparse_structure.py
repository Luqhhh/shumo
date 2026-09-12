"""Full numeric and sparse-order equivalence against a frozen old builder."""

from __future__ import annotations

import datetime as dt
import runpy
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from microgrid.problem.contracts import BatteryState
from microgrid.problem.dispatch_milp import build_dispatch_problem, constraint_structure
from microgrid.problem.purchase_ledger import PurchaseLedger
from microgrid.problem.q4_common import ACTION_START, RESERVE_START, STEP, YEAR_END
from microgrid.problem.q4_forecasts import ForecastSnapshot

legacy_problem = runpy.run_path(str(Path(__file__).parent / "fixtures/q4_milp_reference.py"))[
    "legacy_problem"
]


def instance(case, time, count, seed, model="q4-v3-reserve"):
    rng = np.random.default_rng(seed)
    ledger = PurchaseLedger(case)
    midnight = dt.datetime.combine(time.date(), dt.time())
    if time != midnight:
        slots = tuple(midnight + k * STEP for k in range(144))
        quantities = list(rng.uniform(0, 1200, 144))
        quantities[int((time - midnight) / STEP)] = 0.0
        ledger.submit(midnight, slots, tuple(quantities))
    snapshot = ForecastSnapshot(
        "matrix-test",
        case,
        time,
        tuple(time + k * STEP for k in range(count)),
        tuple(rng.uniform(0, 1600, count)),
        tuple(rng.uniform(0, 1500, count)),
        tuple(rng.uniform(0.01, 2, count)),
        0.21,
        "main",
        {},
        {},
        model,
    )
    # Include exact zero-load/PV/grid values, retaining original explicit zeros.
    snapshot = replace(
        snapshot, load_kwh=(0.0, *snapshot.load_kwh[1:]), pv_kwh=(0.0, *snapshot.pv_kwh[1:])
    )
    return ledger, snapshot


@pytest.mark.parametrize("case", ["q4_2", "q4_3"])
@pytest.mark.parametrize("hour,count", [(0, 144), (6, 144), (12, 109), (18, 144), (19, 30)])
def test_cached_instance_matches_all_original_arrays(case, hour, count):
    constraint_structure.cache_clear()
    for seed in (11, 29):
        ledger, snapshot = instance(case, ACTION_START + dt.timedelta(hours=hour), count, seed)
        expected = legacy_problem(BatteryState(6000), snapshot, ledger)
        actual = build_dispatch_problem(BatteryState(6000), snapshot, ledger)
        values = (
            actual.objective,
            actual.integrality,
            actual.lower,
            actual.upper,
            actual.matrix,
            actual.rlo,
            actual.rhi,
        )
        for old, new in zip(expected, values, strict=True):
            if hasattr(old, "indptr"):
                assert old.shape == new.shape
                for field in ("indptr", "indices", "data"):
                    np.testing.assert_array_equal(getattr(old, field), getattr(new, field))
                    assert getattr(old, field).tobytes() == getattr(new, field).tobytes()
            else:
                np.testing.assert_array_equal(old, new)
    assert constraint_structure.cache_info().hits >= 1


@pytest.mark.parametrize("model,reserve", [("q4-v3-reserve", RESERVE_START), ("q4-v2", None)])
@pytest.mark.parametrize("offset,count", [(150, 144), (36, 36), (1, 1)])
def test_cached_year_end_bounds_and_model_are_instance_specific(model, reserve, offset, count):
    ledger, snapshot = instance("q4_2", YEAR_END - offset * STEP, count, 81, model)
    expected = legacy_problem(BatteryState(6000), snapshot, ledger, reserve_start=reserve)
    actual = build_dispatch_problem(BatteryState(6000), snapshot, ledger, reserve_start=reserve)
    for old, new in zip(
        expected,
        (
            actual.objective,
            actual.integrality,
            actual.lower,
            actual.upper,
            actual.matrix,
            actual.rlo,
            actual.rhi,
        ),
        strict=True,
    ):
        if hasattr(old, "toarray"):
            np.testing.assert_array_equal(old.toarray(), new.toarray())
        else:
            np.testing.assert_array_equal(old, new)


def test_instance_updates_cannot_mutate_cached_structure_or_previous_problem():
    ledger, snapshot = instance("q4_3", ACTION_START + dt.timedelta(hours=6), 144, 92)
    first = build_dispatch_problem(BatteryState(6000), snapshot, ledger)
    original = first.matrix.data.copy()
    structure = constraint_structure(144, ledger.permissions(snapshot.slots[0], snapshot.slots))
    assert not structure.indices.flags.writeable and not structure.data.flags.writeable
    second = build_dispatch_problem(
        BatteryState(6800), replace(snapshot, pv_kwh=(900.0,) * 144), ledger
    )
    assert not np.shares_memory(first.matrix.data, second.matrix.data)
    np.testing.assert_array_equal(first.matrix.data, original)
    assert np.any(second.matrix.data != original)
    with pytest.raises(ValueError, match="unknown constraint layout"):
        constraint_structure(144, ("fixed",) * 144, 2)
