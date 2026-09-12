from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from microgrid.problem.contracts import STEPS_PER_DAY, TimeGrid
from microgrid.problem.q3_load_forecast import LOAD_FORECAST_MODEL_VERSION, LoadForecastPoint
from microgrid.problem.q3_plan_ledger import Q3PlanLedger
from microgrid.problem.q3_pv_forecast import (
    PV_COMBINATION_MODEL_VERSION,
    CombinedPVForecast,
    PVVersionContribution,
)
from microgrid.problem.q3_pv_snapshot import (
    PVForecastSnapshot,
    create_pv_forecast_snapshot,
)
from microgrid.problem.q3_resampling import CombinedHourlyPVPoint, PVBoundaryProxy
from microgrid.problem.q3_sidecars import (
    PlanSlotForecastLink,
    load_forecast_id,
    load_q3_sidecar,
    write_q3_sidecars,
)
from microgrid.schemas import InputError


def _load_version(decision: dt.datetime) -> tuple[LoadForecastPoint, ...]:
    return tuple(
        LoadForecastPoint(
            decision_time=decision,
            valid_time=decision + dt.timedelta(minutes=10 * step),
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
        )
        for step in range(1, STEPS_PER_DAY + 1)
    )


def _pv_snapshot(decision: dt.datetime) -> PVForecastSnapshot:
    points = tuple(
        CombinedHourlyPVPoint(
            valid_time=decision + dt.timedelta(hours=lead),
            power_kw=float(lead),
            source_ref=f"combined:{decision.isoformat()}:{lead}",
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
            source_ref=f"attachment3:{decision.isoformat()}:lead={lead}",
        )
        for lead, point in enumerate(points, start=1)
    )
    combined = CombinedPVForecast(
        decision_time=decision,
        epsilon_kw=1.0,
        points=points,
        contributions=contributions,
        model_version=PV_COMBINATION_MODEL_VERSION,
    )
    return create_pv_forecast_snapshot(
        combined,
        boundary_proxy=PVBoundaryProxy(
            interval_end=decision,
            mean_power_kw=0.0,
            source_ref=f"actual-pv-boundary:{decision.isoformat()}",
        ),
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


def _links(
    day: dt.date,
    load_zero: tuple[LoadForecastPoint, ...],
    load_six: tuple[LoadForecastPoint, ...],
    pv_zero: PVForecastSnapshot,
    pv_six: PVForecastSnapshot,
) -> tuple[PlanSlotForecastLink, ...]:
    links: list[PlanSlotForecastLink] = []
    for version in range(2):
        for slot in range(STEPS_PER_DAY):
            if version == 0 or slot < 36:
                load_point = load_zero[slot]
                pv_point = pv_zero.points[slot]
            else:
                offset = slot - 36
                load_point = load_six[offset]
                pv_point = pv_six.points[offset]
            links.append(
                PlanSlotForecastLink(
                    day=day,
                    version=version,
                    target_slot=slot,
                    forecast_ids=(load_forecast_id(load_point), pv_point.forecast_id),
                )
            )
    return tuple(links)


def _sidecar_kwargs(day: dt.date) -> dict[str, object]:
    midnight = dt.datetime.combine(day, dt.time())
    six = dt.datetime.combine(day, dt.time(6))
    load_zero = _load_version(midnight)
    load_six = _load_version(six)
    pv_zero = _pv_snapshot(midnight)
    pv_six = _pv_snapshot(six)
    return {
        "load_versions": (load_zero, load_six),
        "pv_snapshots": (pv_zero, pv_six),
        "plan_ledgers": (_ledger(day),),
        "forecast_links": _links(day, load_zero, load_six, pv_zero, pv_six),
    }


def test_q3_sidecars_match_frozen_schema_and_preserve_full_history(tmp_path: Path) -> None:
    day = dt.date(2025, 2, 1)
    sidecars = write_q3_sidecars(tmp_path, **_sidecar_kwargs(day))

    forecast_rows = load_q3_sidecar(sidecars.forecast_provenance.path)
    plan_rows = load_q3_sidecar(sidecars.plan_versions.path)
    settlement_rows = load_q3_sidecar(sidecars.settlement_ledger.path)

    assert len(forecast_rows) == 4 * STEPS_PER_DAY
    assert {row["kind"] for row in forecast_rows} == {"load", "pv_attachment3"}
    pv_first = next(row for row in forecast_rows if row["kind"] == "pv_attachment3")
    assert pv_first["forecast_id"].startswith("pv_attachment3|")
    assert pv_first["source_refs"]
    assert pv_first["attachment3_components"]
    assert pv_first["historical_mae"]
    assert pv_first["normalized_weight"]

    assert len(plan_rows) == 2 * STEPS_PER_DAY
    revised_frozen = plan_rows[STEPS_PER_DAY]
    assert revised_frozen["version_id"] == "2025-02-01:v1"
    assert revised_frozen["purchase_is_fixed"] is True
    assert revised_frozen["previous_committed_kwh"] == pytest.approx(100.0)
    assert len(revised_frozen["forecast_ids"]) == 2
    revised_open = plan_rows[STEPS_PER_DAY + 36]
    assert revised_open["purchase_is_fixed"] is False
    assert revised_open["target_slot_start"] == "2025-02-01T06:00:00"
    assert revised_open["target_slot_end"] == "2025-02-01T06:10:00"

    assert len(settlement_rows) == STEPS_PER_DAY + 1
    assert settlement_rows[0]["record_type"] == "base_plan"
    target_adjustment = next(row for row in settlement_rows if row["record_type"] == "adjustment")
    assert target_adjustment["target_slot_start"] == "2025-02-01T20:00:00"
    assert target_adjustment["target_slot_end"] == "2025-02-01T20:10:00"
    assert target_adjustment["delta_plus_kwh"] == pytest.approx(10.0)
    assert target_adjustment["energy_kwh"] == pytest.approx(10.0)
    assert target_adjustment["cost_cny"] == pytest.approx(30.0)

    metadata = sidecars.metadata(relative_to=tmp_path)
    assert metadata["forecast_provenance"]["path"] == "forecast_provenance.jsonl"
    assert all(len(item["sha256"]) == 64 for item in metadata.values())
    assert set(sidecars.paths) == {
        tmp_path / "forecast_provenance.jsonl",
        tmp_path / "plan_versions.jsonl",
        tmp_path / "settlement_ledger.jsonl",
    }


def test_q3_sidecars_reject_unknown_forecast_reference(tmp_path: Path) -> None:
    kwargs = _sidecar_kwargs(dt.date(2025, 2, 1))
    links = list(kwargs["forecast_links"])
    links[0] = PlanSlotForecastLink(
        day=links[0].day,
        version=links[0].version,
        target_slot=links[0].target_slot,
        forecast_ids=("not-in-provenance",),
    )
    kwargs["forecast_links"] = tuple(links)

    with pytest.raises(InputError, match="unknown forecast_id"):
        write_q3_sidecars(tmp_path, **kwargs)


def test_q3_sidecars_refuse_silent_overwrite(tmp_path: Path) -> None:
    kwargs = _sidecar_kwargs(dt.date(2025, 2, 1))
    write_q3_sidecars(tmp_path, **kwargs)

    with pytest.raises(InputError, match="already exists"):
        write_q3_sidecars(tmp_path, **kwargs)


def test_plan_version_slot_end_uses_right_endpoint_contract(tmp_path: Path) -> None:
    rows = load_q3_sidecar(
        write_q3_sidecars(
            tmp_path,
            **_sidecar_kwargs(dt.date(2025, 2, 1)),
        ).plan_versions.path
    )
    grid = TimeGrid()
    assert rows[143]["target_slot_end"] == grid.boundary(dt.date(2025, 2, 1), 144).isoformat()
