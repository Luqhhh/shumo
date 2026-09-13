from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from microgrid.dataio import ingest_source, sha256_file
from microgrid.schemas import InputError, VersionConflictError


def _make_source_tree(root: Path) -> None:
    (root / "C题" / "附件" / "附件5").mkdir(parents=True)
    (root / "C题" / "C题.pdf").write_bytes(b"pdf")
    (root / "C题" / "附件" / "附件1.xlsx").write_bytes(b"xlsx-one")
    (root / "C题" / "附件" / "附件2.xlsx").write_bytes(b"xlsx-two")
    (root / "C题" / "附件" / "附件5" / "result1.xlsx").write_bytes(b"template-one")
    (root / "C题" / "附件" / "附件5" / "result2.xlsx").write_bytes(b"template-two")
    (root / "C题" / "附件" / "附件5" / "result3.xlsx").write_bytes(b"template-three")
    (root / "C题" / "附件" / "附件5" / "result4-2.xlsx").write_bytes(b"template-four-two")
    (root / "C题" / "附件" / "附件5" / "result4-3.xlsx").write_bytes(b"template-four-three")
    # unrelated file must be ignored
    (root / "A题").mkdir()
    (root / "A题" / "A题.pdf").write_bytes(b"not-c")


def test_ingest_is_idempotent_and_preserves_hash(tmp_path):
    source = tmp_path / "source"
    repo = tmp_path / "repo"
    _make_source_tree(source)
    before = sha256_file(source / "C题" / "附件" / "附件1.xlsx")
    manifest = ingest_source(source, repo)
    assert len(manifest["items"]) == 8
    target = repo / "data" / "raw" / "附件1.xlsx"
    assert sha256_file(target) == before
    assert not (repo / "data" / "raw" / "A题.pdf").exists()
    again = ingest_source(source, repo)
    assert all(item["status"] in {"unchanged", "imported"} for item in again["items"])


def test_same_name_different_hash_does_not_overwrite(tmp_path):
    source = tmp_path / "source"
    repo = tmp_path / "repo"
    _make_source_tree(source)
    ingest_source(source, repo)
    target = repo / "data" / "raw" / "附件1.xlsx"
    original_hash = sha256_file(target)
    (source / "C题" / "附件" / "附件1.xlsx").write_bytes(b"changed-content")
    with pytest.raises(VersionConflictError):
        ingest_source(source, repo)
    assert sha256_file(target) == original_hash


def test_zip_path_traversal_is_rejected(tmp_path):
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../evil.xlsx", b"x")
    with pytest.raises(InputError):
        ingest_source(zip_path, tmp_path / "repo")


def test_official_attachment1_reader_keeps_raw_labels(tiny_attachment1):
    from microgrid.dataio import read_attachment1

    records = read_attachment1(tiny_attachment1)
    assert len(records) == 9
    assert records[0].raw_time_label == "0.006944444444444444"
    assert records[0].parsed_minute_of_day == 10
    assert records[-1].raw_time_label == "0:00+1"
    assert records[-1].parsed_day_offset == 1
    assert hashlib.sha256(tiny_attachment1.read_bytes()).hexdigest() == records[0].source_hash


def _wide_attachment(tmp_path: Path, date_value: object) -> Path:
    from openpyxl import Workbook

    path = tmp_path / "附件2.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "小区负载"
    ws.append(["日期\\时间", "0:10", "0:20"])
    ws.append([date_value, 1.0, 2.0])
    wb.save(path)
    return path


def test_wide_reader_rejects_a_serial_past_the_calendar(tmp_path):
    """A mistyped date cell is a bad label, never a bare OverflowError."""

    from microgrid.dataio import read_wide_attachment
    from microgrid.schemas import TimeLabelError

    path = _wide_attachment(tmp_path, 3_000_000)
    with pytest.raises(TimeLabelError, match="outside the supported calendar range"):
        read_wide_attachment(path, sheet_name="小区负载", kind="load_kw", unit="kW")


def test_wide_reader_reports_a_row_date_past_the_calendar(tmp_path):
    """A row date at the very end of the calendar overflows the right endpoint."""

    import datetime as dt

    from microgrid.dataio import read_wide_attachment
    from microgrid.schemas import TimeLabelError

    path = tmp_path / "附件2.xlsx"
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "小区负载"
    ws.append(["日期\\时间", "0:00+1", "0:20"])
    ws.append([dt.date(9999, 12, 31), 1.0, 2.0])
    wb.save(path)

    with pytest.raises(TimeLabelError, match="past the supported calendar range"):
        read_wide_attachment(path, sheet_name="小区负载", kind="load_kw", unit="kW")
