from __future__ import annotations

import datetime as dt

from microgrid.dataio import read_forecasts


def test_forecast_issue_and_valid_time(tiny_forecast):
    records = read_forecasts(tiny_forecast)
    by_lead = {(rec.issue_time, rec.lead_hours): rec for rec in records}
    assert by_lead[(dt.datetime(2025, 1, 1, 0, 0), 1)].valid_time == dt.datetime(2025, 1, 1, 1, 0)
    assert by_lead[(dt.datetime(2025, 1, 1, 0, 0), 7)].valid_time == dt.datetime(2025, 1, 1, 7, 0)
    # Empty date strings in later block rows must inherit only the block date.
    assert by_lead[(dt.datetime(2025, 1, 1, 6, 0), 1)].valid_time == dt.datetime(2025, 1, 1, 7, 0)


def test_same_valid_time_from_different_issue_times_is_preserved(tiny_forecast):
    records = read_forecasts(tiny_forecast)
    target = dt.datetime(2025, 1, 1, 7, 0)
    matching = [rec for rec in records if rec.valid_time == target]
    assert len(matching) == 2
    assert {rec.issue_time for rec in matching} == {
        dt.datetime(2025, 1, 1, 0, 0),
        dt.datetime(2025, 1, 1, 6, 0),
    }
