from __future__ import annotations

import datetime as dt

import pytest

from microgrid.schemas import TimeLabelError
from microgrid.timekeys import (
    excel_serial_to_date,
    is_available,
    parse_interval,
    parse_time_label,
    valid_time,
)


def test_excel_fraction_and_time_objects():
    assert parse_time_label(6.9444444444444441e-3).minute_of_day == 10
    assert parse_time_label(0.5).minute_of_day == 12 * 60
    assert parse_time_label(dt.time(23, 50)).minute_of_day == 23 * 60 + 50
    assert parse_time_label(dt.datetime(2025, 1, 1, 0, 10)).base_date == dt.date(2025, 1, 1)


def test_24_and_plus_one_are_not_modulo_24():
    midnight = parse_time_label("24:00")
    plus = parse_time_label("0:00+1")
    assert midnight.day_offset == 1 and midnight.minute_of_day == 0
    assert plus.day_offset == 1 and plus.minute_of_day == 0
    assert parse_time_label("23:50").total_minutes == 23 * 60 + 50


def test_cross_day_and_cross_year():
    parsed = parse_time_label(dt.datetime(2025, 12, 31, 18, 0))
    nxt = valid_time(parsed.as_datetime(), 6)
    assert nxt == dt.datetime(2026, 1, 1, 0, 0)


def test_interval_labels_are_literal_and_suspicious_is_reported():
    normal = parse_interval("23:50-0:00+1")
    assert normal.span_minutes == 10
    assert normal.suspicious is False
    weird = parse_interval("0:00-0:10+1")
    assert weird.span_minutes == 24 * 60 + 10
    assert weird.suspicious is True
    next_day = parse_interval("0:00+1-0:10+1")
    assert next_day.span_minutes == 10
    assert next_day.suspicious is False


def test_excel_serial_date_systems():
    assert excel_serial_to_date(45658) == dt.date(2025, 1, 1)
    assert excel_serial_to_date(0, date_system="1904") == dt.date(1904, 1, 1)


def test_unknown_labels_raise():
    with pytest.raises(TimeLabelError):
        parse_time_label("not-a-time")
    with pytest.raises(TimeLabelError):
        parse_interval("0:10")


def test_forecast_visibility_rule_is_explicit():
    issue = dt.datetime(2025, 1, 1, 6, 0)
    assert is_available(issue, dt.datetime(2025, 1, 1, 6, 0)) is True
    assert is_available(issue, dt.datetime(2025, 1, 1, 5, 59)) is False
