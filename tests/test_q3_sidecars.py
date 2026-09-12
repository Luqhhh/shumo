from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from microgrid.problem.contracts import STEPS_PER_DAY
from microgrid.problem.q3_load_forecast import LOAD_FORECAST_MODEL_VERSION, LoadForecastPoint
from microgrid.problem.q3_plan_ledger import Q3PlanLedger
from microgrid.problem.q3_pv_forecast import (
    PV_COMBINATION_MODEL_VERSION,
    CombinedPVForecast,
    PVVersionContribution,
)
from microgrid.problem.q3_resampling import CombinedHourlyPVPoint
from microgrid.problem.q3_sidecars import load_q3_sidecar, write_q3_sidecars
from microgrid.schemas import InputError


def _load_version(decision: dt.datetime) -> tuple[LoadForecastPoint, ...]:
    valid_time = decision + dt.timedelta(minutes=10)
    return (
        LoadForecastPoint(
            decision_time=decision,
            valid_time=valid_time,
            available_at=decision,
            training_cutoff=decision,
            power_kw=600.0,
            energy_kwh=100.0,
            raw_power_kw=600.0,
            lag_values_kw=(600.0, 600.0, 600.0, 600.0),
            lag_weights=(8 / 15, 4 / 15, 2 / 15, 1 / 15),
            ar1_phi=0.5,
            latest_visible_residual_kw=0.0,
            was_clipped=False,
            model_version=LOAD_FORECAST_MODEL_VERSION,
            data_version="attachment2-sha256",
            source_refs=("lag7", "lag14", "lag21", "lag28"),
        ),
    )


def _pv_version(decision: dt.datetime) -> CombinedPVForecast:
    points = tuple(
        CombinedHourlyPVPoint(
            valid_time=decision + dt.timedelta(hours=lead),
            power_kw=float(lead),
            source_ref=f"combined:{lead}",
        )
        for lead in range(1, 25)
    )
    contributions = tuple(
        PVVersionContribution(
            issue_time=decision,
            valid_time=point.valid_time,
            lead_hours=lead,
            forecast_power_kw=point.power_kw,
            historical_mae_kw=float(lead),
            history_count=10,
            raw_weight=1.0 / (lead + 1) ** 2,
            normalized_weight=1.0,
            source_ref=f"attachment3:lead={lead}",
        )
        for lead, point in enumerate(points, start=1)
    )
    return CombinedPVForecast(
        decision_time=decision,
        epsilon_kw=1.0,
        points=points,
        contributions=contributions,
        model_version=PV_COMBINATION_MODEL_VERSION,
    )


def _ledger(day: dt.date) -> Q3PlanLedger:
    initial = (100.0,) * STEPS_PER_DAY
    ledger = Q3PlanLedger.start(
        day,
        initial_commitments_kwh=initial,
        base_prices_cny_per_kwh=(1.0,) * STEPS_PER_DAY,
    )
    changed = list(initial)
    changed[120] = 110.0
    return ledger.revise(
        dt.datetime.combine(day, dt.time(6)),
        new_commitments_kwh=tuple(changed),
        transaction_price_cny_per_kwh=2.0,
    )


def test_q3_sidecars_preserve_versions_transactions_and_forecast_provenance(
    tmp_path: Path,
) -> None:
    decision = dt.datetime(2025, 2, 1, 6)
    sidecars = write_q3_sidecars(
        tmp_path,
        load_versions=(_load_version(decision),),
        pv_versions=(_pv_version(decision),),
        plan_ledgers=(_ledger(decision.date()),),
    )

    forecast_rows = load_q3_sidecar(sidecars.forecast_provenance.path)
    plan_rows = load_q3_sidecar(sidecars.plan_versions.path)
    settlement_rows = load_q3_sidecar(sidecars.settlement_ledger.path)

    assert len(forecast_rows) == 25
    assert {row["record_type"] for row in forecast_rows} == {
        "load_forecast",
        "pv_combined_forecast",
    }
    pv_first = next(row for row in forecast_rows if row["record_type"] == "pv_combined_forecast")
    assert pv_first["contributions"][0]["source_ref"].startswith("attachment3")
    assert len(plan_rows) == 2 * STEPS_PER_DAY
    assert len(settlement_rows) == 2 * STEPS_PER_DAY
    target_adjustment = next(
        row
        for row in settlement_rows
        if row["record_type"] == "purchase_adjustment" and row["target_slot"] == 120
    )
    assert target_adjustment["delta_plus_kwh"] == pytest.approx(10.0)
    assert target_adjustment["cost_cny"] == pytest.approx(30.0)

    metadata = sidecars.metadata(relative_to=tmp_path)
    assert metadata["forecast_provenance"]["path"] == "forecast_provenance.jsonl"
    assert all(len(item["sha256"]) == 64 for item in metadata.values())
    assert set(sidecars.paths) == {
        tmp_path / "forecast_provenance.jsonl",
        tmp_path / "plan_versions.jsonl",
        tmp_path / "settlement_ledger.jsonl",
    }


def test_q3_sidecars_refuse_silent_overwrite(tmp_path: Path) -> None:
    decision = dt.datetime(2025, 2, 1, 6)
    kwargs = {
        "load_versions": (_load_version(decision),),
        "pv_versions": (_pv_version(decision),),
        "plan_ledgers": (_ledger(decision.date()),),
    }
    write_q3_sidecars(tmp_path, **kwargs)

    with pytest.raises(InputError, match="already exists"):
        write_q3_sidecars(tmp_path, **kwargs)
