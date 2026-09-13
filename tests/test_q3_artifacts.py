from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from microgrid.dataio import sha256_file
from microgrid.problem.contracts import BatteryAction, BatteryState, IntervalResult
from microgrid.problem.q3_artifacts import write_q3_day_artifacts, write_q3_period_artifacts
from microgrid.problem.q3_plan_ledger import Q3PlanLedger
from microgrid.problem.q3_rolling import Q3DayRun, Q3PeriodRun
from microgrid.problem.q3_sidecars import (
    PlanSlotForecastLink,
    Q3SidecarArtifact,
    Q3SidecarSet,
)
from microgrid.problem.result_io import load_case_result
from microgrid.schemas import InputError


def _day_run(day: dt.date) -> Q3DayRun:
    state = BatteryState(6_000.0)
    intervals = tuple(
        IntervalResult(
            day=day,
            slot=slot,
            load_kw=0.0,
            pv_kw=0.0,
            planned_purchase_kwh=0.0,
            adjusted_purchase_kwh=0.0,
            emergency_purchase_kwh=0.0,
            action=BatteryAction(),
            state_start=state,
            state_end=state,
            source_ref=f"synthetic:{slot}",
            pv_used_kwh=0.0,
        )
        for slot in range(144)
    )
    ledger = Q3PlanLedger.start(
        day,
        initial_commitments_kwh=(0.0,) * 144,
        base_prices_cny_per_kwh=(1.0,) * 144,
    )
    for hour in (6, 12, 18):
        ledger = ledger.revise(
            dt.datetime.combine(day, dt.time(hour)),
            new_commitments_kwh=(0.0,) * 144,
            transaction_price_cny_per_kwh=1.0,
        )
    links = tuple(
        PlanSlotForecastLink(
            day=day,
            version=version,
            target_slot=slot,
            forecast_ids=(f"load:{version}:{slot}", f"pv:{version}:{slot}"),
        )
        for version in range(4)
        for slot in range(144)
    )
    return Q3DayRun(
        day=day,
        state_start=state,
        state_end=state,
        intervals=intervals,
        ledger=ledger,
        forecast_links=links,
        emergency_entries=(),
    )


def _fake_sidecars(directory: Path) -> Q3SidecarSet:
    artifacts: list[Q3SidecarArtifact] = []
    for name in ("forecast_provenance", "plan_versions", "settlement_ledger"):
        path = directory / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"synthetic": true}\n', encoding="utf-8")
        artifacts.append(
            Q3SidecarArtifact(
                name=name,
                path=path,
                schema_version=2,
                sha256=sha256_file(path),
                row_count=1,
            )
        )
    return Q3SidecarSet(
        forecast_provenance=artifacts[0],
        plan_versions=artifacts[1],
        settlement_ledger=artifacts[2],
    )


def test_q3_day_artifacts_register_sidecars_without_schema_upgrade(
    tmp_path: Path,
    monkeypatch,
) -> None:
    day = dt.date(2025, 2, 1)

    def fake_writer(run_dir, **kwargs):
        assert len(kwargs["forecast_links"]) == 576
        assert kwargs["overwrite"] is False
        return _fake_sidecars(Path(run_dir))

    monkeypatch.setattr("microgrid.problem.q3_artifacts.write_q3_sidecars", fake_writer)
    artifacts = write_q3_day_artifacts(
        tmp_path,
        run_id="synthetic-q3-day",
        day_run=_day_run(day),
        load_snapshots=(),
        pv_snapshots=(),
        is_synthetic=True,
    )

    assert artifacts.domain_result_path == tmp_path / "domain_result.json"
    assert len(artifacts.paths) == 4
    assert artifacts.result.result_files == (
        Path("domain_result.json"),
        Path("forecast_provenance.jsonl"),
        Path("plan_versions.jsonl"),
        Path("settlement_ledger.jsonl"),
    )
    assert set(artifacts.result.metadata["sidecars"]) == {
        "forecast_provenance",
        "plan_versions",
        "settlement_ledger",
    }
    assert all(len(item["sha256"]) == 64 for item in artifacts.result.metadata["sidecars"].values())
    loaded = load_case_result(artifacts.domain_result_path, expected_days=(day,))
    assert loaded == artifacts.result
    assert loaded.is_synthetic is True


def test_q3_day_artifacts_refuse_overwriting_domain_result(tmp_path: Path) -> None:
    (tmp_path / "domain_result.json").write_text("existing", encoding="utf-8")
    with pytest.raises(InputError, match="already exists"):
        write_q3_day_artifacts(
            tmp_path,
            run_id="synthetic-q3-day",
            day_run=_day_run(dt.date(2025, 2, 1)),
            load_snapshots=(),
            pv_snapshots=(),
            is_synthetic=True,
        )


def test_q3_period_artifacts_preserve_both_days_and_continuous_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = dt.date(2025, 2, 1)
    second = first + dt.timedelta(days=1)
    period = Q3PeriodRun(days=(_day_run(first), _day_run(second)))

    def fake_writer(run_dir, **kwargs):
        assert len(kwargs["plan_ledgers"]) == 2
        assert len(kwargs["forecast_links"]) == 1_152
        return _fake_sidecars(Path(run_dir))

    monkeypatch.setattr("microgrid.problem.q3_artifacts.write_q3_sidecars", fake_writer)
    artifacts = write_q3_period_artifacts(
        tmp_path,
        run_id="synthetic-q3-period",
        period_run=period,
        load_snapshots=(),
        pv_snapshots=(),
        is_synthetic=True,
    )

    assert len(artifacts.result.intervals) == 288
    assert artifacts.result.metadata["start_day"] == "2025-02-01"
    assert artifacts.result.metadata["end_day"] == "2025-02-02"
    assert artifacts.result.metadata["day_count"] == 2
    loaded = load_case_result(
        artifacts.domain_result_path,
        expected_days=(first, second),
    )
    assert loaded == artifacts.result


@pytest.mark.parametrize("period", [False, True])
def test_artifact_writer_rechecks_realized_reserve_before_writing_sidecars(
    tmp_path, period
) -> None:
    day_run = _day_run(dt.date(2025, 12, 31))
    low = BatteryState(5999.0)
    invalid = replace(
        day_run,
        state_start=low,
        state_end=low,
        intervals=tuple(replace(i, state_start=low, state_end=low) for i in day_run.intervals),
    )
    kwargs = {"period_run": Q3PeriodRun((invalid,))} if period else {"day_run": invalid}
    writer = write_q3_period_artifacts if period else write_q3_day_artifacts
    with pytest.raises(InputError, match="actual terminal reserve validation failed"):
        writer(
            tmp_path,
            run_id="synthetic-invalid-reserve",
            load_snapshots=(),
            pv_snapshots=(),
            is_synthetic=True,
            **kwargs,
        )
    assert not (tmp_path / "domain_result.json").exists()
    assert not (tmp_path / "forecast_provenance.jsonl").exists()
