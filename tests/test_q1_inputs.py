from __future__ import annotations

import datetime as dt

import pytest
from openpyxl import Workbook

from microgrid.dataio import sha256_file
from microgrid.problem.q1_inputs import Q1_KINDS, load_q1_inputs
from microgrid.schemas import InputError


def _right_endpoint_label(slot: int) -> str:
    end_minutes = (slot + 1) * 10
    if end_minutes == 24 * 60:
        return "0:00+1"
    return f"{end_minutes // 60}:{end_minutes % 60:02d}"


def _write_q1_workbook(path, *, omit_slot: int | None = None, duplicate_slot: int | None = None):
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["时间", "电价", "小区负载", "光伏发电预测功率"])
    for slot in range(144):
        if omit_slot == slot:
            continue
        label = _right_endpoint_label(slot)
        load_kw = 600.0
        if slot < 20 and label != "0:00+1":
            time_value = ((slot + 1) * 10) / (24 * 60)
        else:
            time_value = label
        ws.append([time_value, 0.5, load_kw, 0.0])
        if duplicate_slot == slot:
            ws.append([_right_endpoint_label(slot), 0.5, 600.0, 0.0])
    wb.save(path)


def test_load_q1_inputs_maps_mixed_excel_times_to_144_slots(tmp_path):
    path = tmp_path / "附件1.xlsx"
    _write_q1_workbook(path)
    before = sha256_file(path)
    snapshot = load_q1_inputs(path, reference_day=dt.date(2025, 1, 1))
    assert len(snapshot.intervals) == 144
    assert snapshot.intervals[0].slot == 0
    assert snapshot.intervals[0].start == dt.datetime(2025, 1, 1, 0, 0)
    assert snapshot.intervals[0].end == dt.datetime(2025, 1, 1, 0, 10)
    assert snapshot.intervals[-1].slot == 143
    assert snapshot.intervals[-1].start == dt.datetime(2025, 1, 1, 23, 50)
    assert snapshot.intervals[-1].end == dt.datetime(2025, 1, 2, 0, 0)
    assert snapshot.intervals[0].load_kwh == pytest.approx(100.0)
    assert {point.price_cny_per_kwh for point in snapshot.intervals} == {0.5}
    assert snapshot.source_hash == before
    assert sha256_file(path) == before


def test_load_q1_inputs_rejects_missing_slot(tmp_path):
    path = tmp_path / "附件1.xlsx"
    _write_q1_workbook(path, omit_slot=5)
    with pytest.raises(InputError):
        load_q1_inputs(path, reference_day=dt.date(2025, 1, 1))


def test_load_q1_inputs_rejects_duplicate_slot(tmp_path):
    path = tmp_path / "附件1.xlsx"
    _write_q1_workbook(path, duplicate_slot=3)
    with pytest.raises(InputError):
        load_q1_inputs(path, reference_day=dt.date(2025, 1, 1))


def test_load_q1_inputs_does_not_clip_negative_price(tmp_path):
    path = tmp_path / "附件1.xlsx"
    _write_q1_workbook(path)
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["时间", "电价", "小区负载", "光伏发电预测功率"])
    for slot in range(144):
        label = _right_endpoint_label(slot)
        time_value = ((slot + 1) * 10) / (24 * 60) if slot < 20 and label != "0:00+1" else label
        ws.append([time_value, -0.1, 600.0, 0.0])
    wb.save(path)
    snapshot = load_q1_inputs(path, reference_day=dt.date(2025, 1, 1))
    assert all(point.price_cny_per_kwh == -0.1 for point in snapshot.intervals)


def test_expected_kind_contract_is_explicit():
    assert Q1_KINDS == ("price_cny_per_kwh", "load_kw", "pv_forecast_kw")
