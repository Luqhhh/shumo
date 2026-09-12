from __future__ import annotations

import datetime as dt

import pytest
from openpyxl import Workbook

from microgrid.dataio import sha256_file
from microgrid.problem.q3_inputs import EXPECTED_FORECAST_LEADS, load_q3_forecast_archive
from microgrid.schemas import InputError


def _write_forecast_workbook(
    path,
    *,
    missing: tuple[int, int] | None = None,
    duplicate_issue_hour: int | None = None,
):
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["日期", "预报时刻"] + [f"预报{lead}小时" for lead in EXPECTED_FORECAST_LEADS])
    for issue_hour in (0, 6, 12, 18):
        values = []
        for lead in EXPECTED_FORECAST_LEADS:
            value = float(issue_hour * 100 + lead)
            if missing == (issue_hour, lead):
                value = None
            values.append(value)
        ws.append(["2025-02-01" if issue_hour == 0 else "", f"{issue_hour}:00", *values])
        if duplicate_issue_hour == issue_hour:
            ws.append(["", f"{issue_hour}:00", *values])
    wb.save(path)


def test_q3_forecast_archive_preserves_issue_and_valid_time_versions(tmp_path):
    path = tmp_path / "附件3.xlsx"
    _write_forecast_workbook(path)
    before = sha256_file(path)

    archive = load_q3_forecast_archive(path)

    assert archive.source_sha256 == before
    assert sha256_file(path) == before
    assert [version.issue_time for version in archive.versions] == [
        dt.datetime(2025, 2, 1, 0, 0),
        dt.datetime(2025, 2, 1, 6, 0),
        dt.datetime(2025, 2, 1, 12, 0),
        dt.datetime(2025, 2, 1, 18, 0),
    ]
    assert all(
        tuple(record.lead_hours for record in version.records) == EXPECTED_FORECAST_LEADS
        for version in archive.versions
    )
    assert archive.versions[1].records[0].valid_time == dt.datetime(2025, 2, 1, 7, 0)
    evening = archive.versions[-1]
    assert evening.records[5].valid_time == dt.datetime(2025, 2, 2, 0, 0)
    assert evening.records[-1].valid_time == dt.datetime(2025, 2, 2, 18, 0)


def test_q3_forecast_archive_at_0600_keeps_all_visible_versions_without_selecting(tmp_path):
    path = tmp_path / "附件3.xlsx"
    _write_forecast_workbook(path)
    archive = load_q3_forecast_archive(path)

    info = archive.info_set_at(dt.datetime(2025, 2, 1, 6, 0))

    assert len(info.visible_items) == 48
    assert {item.available_at.hour for item in info.visible_items} == {0, 6}
    target = dt.datetime(2025, 2, 1, 7, 0)
    same_target = [item for item in info.visible_items if item.valid_time == target]
    assert len(same_target) == 2
    assert {item.available_at.hour for item in same_target} == {0, 6}


def test_q3_forecast_archive_rejects_an_incomplete_publication(tmp_path):
    path = tmp_path / "附件3.xlsx"
    _write_forecast_workbook(path, missing=(6, 24))

    with pytest.raises(InputError, match="ordered leads 1..24"):
        load_q3_forecast_archive(path)


def test_q3_forecast_archive_rejects_duplicate_issue_and_lead(tmp_path):
    path = tmp_path / "附件3.xlsx"
    _write_forecast_workbook(path, duplicate_issue_hour=6)

    with pytest.raises(InputError, match="duplicate leads"):
        load_q3_forecast_archive(path)
