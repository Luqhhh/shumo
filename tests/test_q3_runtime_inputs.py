from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest

from microgrid.problem.contracts import InfoItem, TimeGrid
from microgrid.problem.q2_inputs import ActualInterval, FixedPricePoint, Q2InputBundle
from microgrid.problem.q3_runtime_inputs import (
    Q3RuntimeInputs,
    inclusive_days,
    load_q3_runtime_inputs,
    release_times,
)
from microgrid.schemas import InputError


@dataclass(frozen=True)
class _Version:
    issue_time: dt.datetime


@dataclass(frozen=True)
class _Archive:
    versions: tuple[_Version, ...]
    source_sha256: str = "attachment3-sha"

    def raw_info_items(self):
        return tuple(
            InfoItem(
                kind="pv_forecast_kw",
                available_at=version.issue_time,
                valid_time=version.issue_time + dt.timedelta(hours=1),
                value=1.0,
                source_ref=f"attachment3:{version.issue_time.isoformat()}",
            )
            for version in self.versions
        )


def _q2(days: tuple[dt.date, ...]) -> Q2InputBundle:
    grid = TimeGrid()
    actuals = tuple(
        ActualInterval(
            day=day,
            slot=slot,
            start=grid.interval(day, slot).start,
            end=grid.interval(day, slot).end,
            load_kw=100.0,
            pv_kw=50.0,
            load_source_ref=f"load:{day}:{slot}",
            pv_source_ref=f"pv:{day}:{slot}",
        )
        for day in days
        for slot in range(144)
    )
    return Q2InputBundle(
        fixed_prices=tuple(
            FixedPricePoint(slot=slot, price_cny_per_kwh=1.0, source_ref=f"price:{slot}")
            for slot in range(144)
        ),
        actuals=actuals,
        input_hashes=(("attachment1", "a"), ("attachment2", "b")),
    )


def test_runtime_loader_combines_actual_and_forecast_archives(monkeypatch) -> None:
    days = inclusive_days(dt.date(2025, 1, 1), dt.date(2025, 1, 2))
    archive = _Archive(tuple(_Version(issue) for issue in release_times(days)))
    monkeypatch.setattr(
        "microgrid.problem.q3_runtime_inputs.load_q2_inputs",
        lambda **_kwargs: _q2(days),
    )
    monkeypatch.setattr(
        "microgrid.problem.q3_runtime_inputs.load_q3_forecast_archive",
        lambda _path: archive,
    )

    result = load_q3_runtime_inputs(
        attachment1_path="attachment1.xlsx",
        attachment2_path="attachment2.xlsx",
        attachment3_path="attachment3.xlsx",
        actual_days=days,
    )

    assert len(result.actuals) == 288
    assert len(result.forecast_archive.versions) == 8
    assert len(result.raw_info_items) == 288 * 2 + 8
    assert dict(result.input_hashes) == {
        "attachment1": "a",
        "attachment2": "b",
        "attachment3": "attachment3-sha",
    }
    assert len(result.actuals_for((days[1],))) == 144


def test_runtime_bundle_rejects_a_missing_forecast_release() -> None:
    days = (dt.date(2025, 1, 1),)
    q2 = _q2(days)
    archive = _Archive(tuple(_Version(issue) for issue in release_times(days)[:-1]))
    with pytest.raises(ValueError, match="four releases"):
        Q3RuntimeInputs(
            fixed_prices=q2.fixed_prices,
            actuals=q2.actuals,
            forecast_archive=archive,
            raw_info_items=archive.raw_info_items(),
            actual_days=days,
            input_hashes=q2.input_hashes,
        )


def test_inclusive_days_rejects_reversed_range() -> None:
    with pytest.raises(InputError, match="cannot precede"):
        inclusive_days(dt.date(2025, 2, 2), dt.date(2025, 2, 1))
