from __future__ import annotations

import datetime as dt

import pytest

from microgrid.problem.q3_resampling import (
    CombinedHourlyPVPoint,
    PVBoundaryProxy,
    resample_combined_hourly_pv,
)


def _hourly_points(
    decision_time: dt.datetime,
    values: tuple[float, ...],
) -> tuple[CombinedHourlyPVPoint, ...]:
    return tuple(
        CombinedHourlyPVPoint(
            valid_time=decision_time + dt.timedelta(hours=lead),
            power_kw=value,
            source_ref=f"combined:lead={lead}",
        )
        for lead, value in enumerate(values, start=1)
    )


def _boundary_proxy(decision_time: dt.datetime, power_kw: float) -> PVBoundaryProxy:
    return PVBoundaryProxy(
        interval_end=decision_time,
        mean_power_kw=power_kw,
        source_ref="actual-pv:last-completed-interval",
    )


def test_linear_resampling_uses_ten_minute_right_endpoints_and_kwh_conversion():
    decision_time = dt.datetime(2025, 2, 1, 6, 0)
    hourly = _hourly_points(decision_time, tuple(float(60 * lead) for lead in range(1, 25)))

    result = resample_combined_hourly_pv(
        decision_time=decision_time,
        boundary_proxy=_boundary_proxy(decision_time, 0.0),
        hourly_points=hourly,
    )

    assert len(result) == 144
    assert result[0].slot_end == dt.datetime(2025, 2, 1, 6, 10)
    assert result[0].power_kw == pytest.approx(10.0)
    assert result[0].energy_kwh == pytest.approx(10.0 / 6.0)
    assert result[5].slot_end == dt.datetime(2025, 2, 1, 7, 0)
    assert result[5].power_kw == pytest.approx(60.0)
    assert result[-1].slot_end == dt.datetime(2025, 2, 2, 6, 0)
    assert result[-1].power_kw == pytest.approx(1_440.0)


def test_linear_resampling_crosses_midnight_by_real_valid_time():
    decision_time = dt.datetime(2025, 2, 1, 18, 0)
    hourly = _hourly_points(decision_time, (120.0,) * 24)

    result = resample_combined_hourly_pv(
        decision_time=decision_time,
        boundary_proxy=_boundary_proxy(decision_time, 120.0),
        hourly_points=hourly,
    )

    assert result[35].slot_end == dt.datetime(2025, 2, 2, 0, 0)
    assert result[35].power_kw == pytest.approx(120.0)
    assert result[-1].slot_end == dt.datetime(2025, 2, 2, 18, 0)


def test_linear_resampling_hits_all_hourly_knots() -> None:
    decision_time = dt.datetime(2025, 2, 1, 0, 0)
    values = tuple(float(lead * lead) for lead in range(1, 25))
    hourly = _hourly_points(decision_time, values)

    result = resample_combined_hourly_pv(
        decision_time=decision_time,
        boundary_proxy=_boundary_proxy(decision_time, 0.0),
        hourly_points=hourly,
        method="linear",
    )

    for lead, expected_kw in enumerate(values, start=1):
        point = result[lead * 6 - 1]
        assert point.slot_end == decision_time + dt.timedelta(hours=lead)
        assert point.power_kw == pytest.approx(expected_kw)
        assert point.energy_kwh == pytest.approx(expected_kw / 6.0)


@pytest.mark.parametrize("method", ["linear", "pchip"])
def test_resampling_is_deterministic_and_does_not_mutate_inputs(method: str) -> None:
    decision_time = dt.datetime(2025, 2, 1, 18, 0)
    hourly = _hourly_points(
        decision_time,
        tuple(float((lead % 7) * 25) for lead in range(1, 25)),
    )
    proxy = _boundary_proxy(decision_time, 12.5)
    original_hourly = tuple(hourly)

    first = resample_combined_hourly_pv(
        decision_time=decision_time,
        boundary_proxy=proxy,
        hourly_points=hourly,
        method=method,  # type: ignore[arg-type]
    )
    second = resample_combined_hourly_pv(
        decision_time=decision_time,
        boundary_proxy=proxy,
        hourly_points=hourly,
        method=method,  # type: ignore[arg-type]
    )

    assert first == second
    assert hourly == original_hourly


def test_pchip_sensitivity_uses_the_same_knots_and_hits_every_hour():
    decision_time = dt.datetime(2025, 2, 1, 12, 0)
    values = tuple(float((lead % 5) * 100) for lead in range(1, 25))
    hourly = _hourly_points(decision_time, values)

    result = resample_combined_hourly_pv(
        decision_time=decision_time,
        boundary_proxy=_boundary_proxy(decision_time, 50.0),
        hourly_points=hourly,
        method="pchip",
    )

    for lead, expected_kw in enumerate(values, start=1):
        point = result[lead * 6 - 1]
        assert point.slot_end == decision_time + dt.timedelta(hours=lead)
        assert point.power_kw == pytest.approx(expected_kw)
        assert point.energy_kwh == pytest.approx(expected_kw / 6.0)


def test_resampler_rejects_a_shifted_or_incomplete_hourly_series():
    decision_time = dt.datetime(2025, 2, 1, 6, 0)
    incomplete = _hourly_points(decision_time, (100.0,) * 23)
    with pytest.raises(ValueError, match="exactly 24"):
        resample_combined_hourly_pv(
            decision_time=decision_time,
            boundary_proxy=_boundary_proxy(decision_time, 100.0),
            hourly_points=incomplete,
        )

    shifted = list(_hourly_points(decision_time, (100.0,) * 24))
    shifted[0] = CombinedHourlyPVPoint(
        valid_time=decision_time + dt.timedelta(hours=2),
        power_kw=100.0,
    )
    with pytest.raises(ValueError, match=r"decision_time \+ 1h through \+24h"):
        resample_combined_hourly_pv(
            decision_time=decision_time,
            boundary_proxy=_boundary_proxy(decision_time, 100.0),
            hourly_points=tuple(shifted),
        )


@pytest.mark.parametrize(
    ("decision_time", "proxy_time", "boundary_proxy_kw", "method", "message"),
    [
        (
            dt.datetime(2025, 2, 1, 6, 5),
            dt.datetime(2025, 2, 1, 6, 0),
            100.0,
            "linear",
            "ten-minute boundary",
        ),
        (
            dt.datetime(2025, 2, 1, 6, 0),
            dt.datetime(2025, 2, 1, 5, 50),
            100.0,
            "linear",
            "interval_end must equal",
        ),
        (
            dt.datetime(2025, 2, 1, 6, 0),
            dt.datetime(2025, 2, 1, 6, 0),
            -1.0,
            "linear",
            "non-negative",
        ),
        (
            dt.datetime(2025, 2, 1, 6, 0),
            dt.datetime(2025, 2, 1, 6, 0),
            100.0,
            "cubic",
            "unsupported",
        ),
    ],
)
def test_resampler_rejects_invalid_boundary_or_method(
    decision_time: dt.datetime,
    proxy_time: dt.datetime,
    boundary_proxy_kw: float,
    method: str,
    message: str,
):
    hourly = _hourly_points(dt.datetime(2025, 2, 1, 6, 0), (100.0,) * 24)
    with pytest.raises(ValueError, match=message):
        boundary_proxy = PVBoundaryProxy(
            interval_end=proxy_time,
            mean_power_kw=boundary_proxy_kw,
        )
        resample_combined_hourly_pv(
            decision_time=decision_time,
            boundary_proxy=boundary_proxy,
            hourly_points=hourly,
            method=method,  # type: ignore[arg-type]
        )
