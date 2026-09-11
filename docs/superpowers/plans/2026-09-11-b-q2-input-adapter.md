# B Task Q2/Q4-2 Input Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a typed, causal, provenance-preserving adapter for Attachment 1, Attachment 2, and Attachment 4 inputs, plus synthetic result-I/O interoperability and B decision documentation, without implementing a formal Q2/Q4-2 model.

**Architecture:** Add one leaf module, `microgrid.problem.q2_inputs`, that composes the existing lossless readers with `TimeGrid`, validates complete right-endpoint grids, and returns immutable tuples. Keep optimization and settlement absent; causality is exposed only through `InfoItem` and `InfoSet.from_raw`, while existing `CaseResult` serialization is exercised only by synthetic tests.

**Tech Stack:** Python 3.11, dataclasses, pathlib, openpyxl, pytest, Ruff, uv.

**Spec:** `docs/superpowers/specs/2026-09-11-b-q2-input-adapter-design.md`

## Global Constraints

- Treat `CUMCM2026Problems/`, `data/raw/`, `data/templates/`, and `resources/` as read-only; every workbook used by tests lives under pytest `tmp_path`.
- Leave every status, `confirmed_by`, and `confirmed_at` field in `configs/decisions.toml` unchanged.
- Leave `src/microgrid/problem/q2.py` and `src/microgrid/problem/q4_2.py` unchanged and raising `ModelNotImplementedError`.
- Do not run a formal case, create a formal run under `outputs/runs/`, export `result2.xlsx` or `result4-2.xlsx`, or prepare a final submission.
- Mark the result-I/O fixture `is_synthetic=True`; never write synthetic artifacts into `dist/` or cite them as paper results.
- Align Attachment 1/2/4 labels as right endpoints: `0:10 -> slot 0` and `0:00+1 -> slot 143`; never use unresolved result-template labels to redefine the internal grid.
- Preserve source load/PV as kW and derive ten-minute energy exactly once with `TimeGrid.power_to_energy_kwh(..., minutes=10)`.
- Record, but do not implement, the future-model invariants `grid_supplied_kwh + discharge_kwh + pv_kwh >= load_kwh + charge_kwh` and `planned_cost_cny = sum(planned_purchase_kwh[k] * tariff_cny_per_kwh[k])`.
- Use `uv run --locked` from a WSL login shell for every Python, pytest, and Ruff command.
- Each task ends in a local commit. Do not push any commit until the user explicitly authorizes that specific push.

## File Map

- Create `src/microgrid/problem/q2_inputs.py`: immutable input records, explicit-path workbook loading, right-endpoint validation, Q2/Q4-2 grid checks, and causal information views.
- Create `tests/test_q2_inputs.py`: synthetic workbook builders and all adapter, causality, source-integrity, validation, and result-I/O tests.
- Create `docs/b_decision_evidence.md`: evidence and unresolved human decisions for B.
- Create `docs/b_model_card.md`: non-approved/non-implemented model boundary and future acceptance criteria.
- Do not modify `src/microgrid/dataio.py`, `src/microgrid/problem/contracts.py`, `src/microgrid/problem/result_io.py`, `src/microgrid/problem/q2.py`, `src/microgrid/problem/q4_2.py`, or `configs/decisions.toml`.

---

### Task 1: Immutable B Input Records and Synthetic Result Interoperability

**Files:**
- Create: `src/microgrid/problem/q2_inputs.py`
- Create: `tests/test_q2_inputs.py`
- Read: `src/microgrid/problem/contracts.py:70-93,123-186,287-389`
- Read: `src/microgrid/problem/result_io.py:37-155`

**Interfaces:**
- Consumes: `TimeGrid.power_to_energy_kwh(power_kw: float, minutes: int = 10) -> float`, `BatteryAction`, `BatteryState`, `IntervalResult`, `CaseResult`, `save_case_result`, and `load_case_result`.
- Produces: `FixedPricePoint`, `ActualInterval`, `VariablePricePoint`, `Q2InputBundle`, and `VariablePriceBundle` with the exact fields in the approved spec.

- [ ] **Step 1: Write the failing carrier and round-trip tests**

Create `tests/test_q2_inputs.py` with these imports and tests:

```python
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from openpyxl import Workbook

from microgrid.dataio import sha256_file
from microgrid.problem.contracts import (
    INITIAL_SOC_KWH,
    BatteryAction,
    BatteryState,
    CaseResult,
    IntervalResult,
)
from microgrid.problem.q2_inputs import (
    ActualInterval,
    FixedPricePoint,
    Q2InputBundle,
    VariablePriceBundle,
    VariablePricePoint,
)
from microgrid.problem.result_io import case_result_to_dict, load_case_result, save_case_result
from microgrid.schemas import InputError


def test_actual_interval_converts_ten_minute_power_to_energy() -> None:
    actual = ActualInterval(
        day=dt.date(2025, 2, 1),
        slot=0,
        start=dt.datetime(2025, 2, 1, 0, 0),
        end=dt.datetime(2025, 2, 1, 0, 10),
        load_kw=600.0,
        pv_kw=300.0,
        load_source_ref="附件2.xlsx!负荷!B2 sha256=aaaaaaaaaaaa",
        pv_source_ref="附件2.xlsx!光伏!B2 sha256=aaaaaaaaaaaa",
    )

    assert actual.load_kw == 600.0
    assert actual.pv_kw == 300.0
    assert actual.load_kwh == pytest.approx(100.0)
    assert actual.pv_kwh == pytest.approx(50.0)


@pytest.mark.parametrize("bad_value", [-1.0, float("nan"), float("inf")])
def test_value_records_reject_negative_or_non_finite_values(bad_value: float) -> None:
    with pytest.raises(InputError, match="price_cny_per_kwh"):
        FixedPricePoint(slot=0, price_cny_per_kwh=bad_value, source_ref="cell")


def test_q2_case_result_roundtrip_preserves_adapter_provenance(tmp_path: Path) -> None:
    actual = ActualInterval(
        day=dt.date(2025, 2, 1),
        slot=0,
        start=dt.datetime(2025, 2, 1, 0, 0),
        end=dt.datetime(2025, 2, 1, 0, 10),
        load_kw=600.0,
        pv_kw=300.0,
        load_source_ref="附件2.xlsx!负荷!B2 sha256=aaaaaaaaaaaa",
        pv_source_ref="附件2.xlsx!光伏!B2 sha256=aaaaaaaaaaaa",
    )
    state = BatteryState(INITIAL_SOC_KWH)
    interval = IntervalResult(
        day=actual.day,
        slot=actual.slot,
        load_kw=actual.load_kw,
        pv_kw=actual.pv_kw,
        planned_purchase_kwh=0.0,
        adjusted_purchase_kwh=0.0,
        emergency_purchase_kwh=0.0,
        action=BatteryAction(),
        state_start=state,
        state_end=state,
        source_ref=f"{actual.load_source_ref}; {actual.pv_source_ref}",
    )
    result = CaseResult(
        case_id="q2",
        run_id="synthetic-adapter-roundtrip",
        status="interface-test",
        is_synthetic=True,
        intervals=(interval,),
        metadata={"input_hashes": [["attachment2", "a" * 64]]},
    )

    path = tmp_path / "q2-result.json"
    save_case_result(path, result)
    loaded = load_case_result(path)

    assert case_result_to_dict(loaded) == case_result_to_dict(result)
    assert loaded.is_synthetic is True
    assert loaded.intervals[0].source_ref == interval.source_ref
```

- [ ] **Step 2: Run the tests and verify the expected RED state**

Run:

```bash
uv run --locked pytest -q   tests/test_q2_inputs.py::test_actual_interval_converts_ten_minute_power_to_energy   tests/test_q2_inputs.py::test_q2_case_result_roundtrip_preserves_adapter_provenance
```

Expected: collection fails with `ModuleNotFoundError: No module named 'microgrid.problem.q2_inputs'`. This proves the new API is absent rather than exposing a fixture error.

- [ ] **Step 3: Add the minimal immutable records**

Create `src/microgrid/problem/q2_inputs.py` with the following foundation:

```python
"""Typed, causal input adaptation for Q2 and Q4-2.

This module interprets already-read source records on the shared internal
time grid.  It does not choose a forecast, optimization, or settlement model.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from pathlib import Path

from ..schemas import InputError
from .contracts import InfoItem, InfoSet, TimeGrid


def _require_nonnegative_finite(name: str, value: float, *, source_ref: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{name} must be numeric at {source_ref}")
    if not math.isfinite(float(value)) or float(value) < 0:
        raise InputError(f"{name} must be finite and non-negative at {source_ref}")


@dataclass(frozen=True)
class FixedPricePoint:
    slot: int
    price_cny_per_kwh: float
    source_ref: str

    def __post_init__(self) -> None:
        if not 0 <= self.slot < TimeGrid.steps_per_day:
            raise InputError(f"fixed-price slot out of range: {self.slot}")
        _require_nonnegative_finite(
            "price_cny_per_kwh", self.price_cny_per_kwh, source_ref=self.source_ref
        )


@dataclass(frozen=True)
class ActualInterval:
    day: dt.date
    slot: int
    start: dt.datetime
    end: dt.datetime
    load_kw: float
    pv_kw: float
    load_source_ref: str
    pv_source_ref: str

    def __post_init__(self) -> None:
        interval = TimeGrid().interval(self.day, self.slot)
        if (self.start, self.end) != (interval.start, interval.end):
            raise InputError(f"actual interval is not aligned: {(self.day, self.slot)}")
        _require_nonnegative_finite("load_kw", self.load_kw, source_ref=self.load_source_ref)
        _require_nonnegative_finite("pv_kw", self.pv_kw, source_ref=self.pv_source_ref)

    @property
    def load_kwh(self) -> float:
        return TimeGrid.power_to_energy_kwh(self.load_kw, minutes=10)

    @property
    def pv_kwh(self) -> float:
        return TimeGrid.power_to_energy_kwh(self.pv_kw, minutes=10)


@dataclass(frozen=True)
class VariablePricePoint:
    day: dt.date
    slot: int
    start: dt.datetime
    end: dt.datetime
    price_cny_per_kwh: float
    source_ref: str

    def __post_init__(self) -> None:
        interval = TimeGrid().interval(self.day, self.slot)
        if (self.start, self.end) != (interval.start, interval.end):
            raise InputError(f"variable-price interval is not aligned: {(self.day, self.slot)}")
        _require_nonnegative_finite(
            "price_cny_per_kwh", self.price_cny_per_kwh, source_ref=self.source_ref
        )


@dataclass(frozen=True)
class Q2InputBundle:
    fixed_prices: tuple[FixedPricePoint, ...]
    actuals: tuple[ActualInterval, ...]
    input_hashes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class VariablePriceBundle:
    prices: tuple[VariablePricePoint, ...]
    input_hashes: tuple[tuple[str, str], ...]
```

Do not add loading functions or model logic in this task.

- [ ] **Step 4: Run the Task 1 tests and verify GREEN**

Run:

```bash
uv run --locked pytest -q   tests/test_q2_inputs.py::test_actual_interval_converts_ten_minute_power_to_energy   tests/test_q2_inputs.py::test_value_records_reject_negative_or_non_finite_values   tests/test_q2_inputs.py::test_q2_case_result_roundtrip_preserves_adapter_provenance
```

Expected: all parameterized cases and both named tests pass; the JSON is written only below `tmp_path`.

- [ ] **Step 5: Run focused lint and commit locally**

Run:

```bash
uv run --locked ruff check src/microgrid/problem/q2_inputs.py tests/test_q2_inputs.py
uv run --locked ruff format --check src/microgrid/problem/q2_inputs.py tests/test_q2_inputs.py
git diff --check
git add src/microgrid/problem/q2_inputs.py tests/test_q2_inputs.py
git commit -m "feat: add B input records"
```

Expected: checks pass and the commit contains only the new module and test file. Do not push.

---

### Task 2: Explicit Q2 Workbook Loading, Right-Endpoint Alignment, and Source Integrity

**Files:**
- Modify: `src/microgrid/problem/q2_inputs.py`
- Modify: `tests/test_q2_inputs.py`
- Read: `src/microgrid/dataio.py:47-55,492-617`

**Interfaces:**
- Consumes: `read_attachment1(path) -> list[ObservationRecord]`, `read_wide_attachment(path, *, sheet_name, kind, unit) -> list[dict[str, Any]]`, `sha256_file(path) -> str`, and `TimeGrid.interval_from_right_endpoint(day, raw_label) -> TimeInterval`.
- Produces: `load_q2_inputs(*, attachment1_path, attachment2_path, load_sheet_name, pv_sheet_name) -> Q2InputBundle`.

- [ ] **Step 1: Add deterministic synthetic workbook builders**

Append these helpers to `tests/test_q2_inputs.py`:

```python
def _right_endpoint_labels() -> list[str]:
    labels: list[str] = []
    for slot in range(143):
        minutes = (slot + 1) * 10
        labels.append(f"{minutes // 60}:{minutes % 60:02d}")
    labels.append("0:00+1")
    return labels


def _write_attachment1(path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["时间", "电价", "小区负载", "光伏发电预测功率"])
    for slot, label in enumerate(_right_endpoint_labels()):
        ws.append([label, 0.4 + slot / 1000.0, 1000.0 + slot, 200.0 + slot])
    wb.save(path)
    wb.close()
    return path


def _write_wide_workbook(
    path: Path,
    *,
    sheets: tuple[str, ...],
    days: tuple[dt.date, ...],
    omitted: dict[str, set[tuple[dt.date, int]]] | None = None,
    duplicate_header_slot: int | None = None,
    bad_values: dict[tuple[str, dt.date, int], object] | None = None,
) -> Path:
    omitted = omitted or {}
    bad_values = bad_values or {}
    labels = _right_endpoint_labels()
    if duplicate_header_slot is not None:
        labels[duplicate_header_slot] = labels[duplicate_header_slot - 1]

    wb = Workbook()
    for sheet_index, sheet_name in enumerate(sheets):
        ws = wb.active if sheet_index == 0 else wb.create_sheet()
        ws.title = sheet_name
        ws.append(["日期", *labels])
        for day_index, day in enumerate(days):
            row: list[object | None] = [day]
            for slot in range(144):
                key = (day, slot)
                value: object | None = 1000.0 + 10.0 * day_index + slot
                if key in omitted.get(sheet_name, set()):
                    value = None
                value = bad_values.get((sheet_name, day, slot), value)
                row.append(value)
            ws.append(row)
    wb.save(path)
    wb.close()
    return path
```

- [ ] **Step 2: Write the failing happy-path and source-integrity tests**

Add imports for `load_q2_inputs`, then add:

```python
def test_load_q2_inputs_aligns_fixed_prices_and_actuals(tmp_path: Path) -> None:
    attachment1 = _write_attachment1(tmp_path / "附件1.xlsx")
    days = (dt.date(2025, 1, 1), dt.date(2025, 1, 2))
    attachment2 = _write_wide_workbook(
        tmp_path / "附件2.xlsx",
        sheets=("实际负荷", "实际光伏"),
        days=days,
    )

    bundle = load_q2_inputs(
        attachment1_path=attachment1,
        attachment2_path=attachment2,
        load_sheet_name="实际负荷",
        pv_sheet_name="实际光伏",
    )

    assert len(bundle.fixed_prices) == 144
    assert [point.slot for point in bundle.fixed_prices] == list(range(144))
    assert len(bundle.actuals) == 288
    assert bundle.actuals[0].start == dt.datetime(2025, 1, 1, 0, 0)
    assert bundle.actuals[0].end == dt.datetime(2025, 1, 1, 0, 10)
    assert bundle.actuals[-1].slot == 143
    assert bundle.actuals[-1].end == dt.datetime(2025, 1, 3, 0, 0)
    assert bundle.fixed_prices[0].source_ref.startswith("附件1.xlsx!Sheet1!B2 ")
    assert bundle.actuals[0].load_source_ref.startswith("附件2.xlsx!实际负荷!B2 ")
    assert bundle.input_hashes == tuple(
        sorted(
            (
                ("attachment1", sha256_file(attachment1)),
                ("attachment2", sha256_file(attachment2)),
            )
        )
    )


def test_load_q2_inputs_does_not_modify_sources(tmp_path: Path) -> None:
    attachment1 = _write_attachment1(tmp_path / "附件1.xlsx")
    attachment2 = _write_wide_workbook(
        tmp_path / "附件2.xlsx",
        sheets=("实际负荷", "实际光伏"),
        days=(dt.date(2025, 1, 1),),
    )
    before = (sha256_file(attachment1), sha256_file(attachment2))

    load_q2_inputs(
        attachment1_path=attachment1,
        attachment2_path=attachment2,
        load_sheet_name="实际负荷",
        pv_sheet_name="实际光伏",
    )

    assert (sha256_file(attachment1), sha256_file(attachment2)) == before
```

Add these validation tests in the same RED batch before implementing the loader:

```python
def test_load_q2_inputs_rejects_mismatched_actual_grids(tmp_path: Path) -> None:
    day = dt.date(2025, 1, 1)
    attachment1 = _write_attachment1(tmp_path / "附件1.xlsx")
    attachment2 = _write_wide_workbook(
        tmp_path / "附件2.xlsx",
        sheets=("实际负荷", "实际光伏"),
        days=(day,),
        omitted={"实际光伏": {(day, 7)}},
    )

    with pytest.raises(InputError, match="missing_slots=.*7"):
        load_q2_inputs(
            attachment1_path=attachment1,
            attachment2_path=attachment2,
            load_sheet_name="实际负荷",
            pv_sheet_name="实际光伏",
        )


def test_load_q2_inputs_rejects_duplicate_right_endpoints(tmp_path: Path) -> None:
    attachment1 = _write_attachment1(tmp_path / "附件1.xlsx")
    attachment2 = _write_wide_workbook(
        tmp_path / "附件2.xlsx",
        sheets=("实际负荷", "实际光伏"),
        days=(dt.date(2025, 1, 1),),
        duplicate_header_slot=1,
    )

    with pytest.raises(InputError, match="duplicate key"):
        load_q2_inputs(
            attachment1_path=attachment1,
            attachment2_path=attachment2,
            load_sheet_name="实际负荷",
            pv_sheet_name="实际光伏",
        )


def test_load_q2_inputs_rejects_negative_cell_with_source_context(tmp_path: Path) -> None:
    day = dt.date(2025, 1, 1)
    attachment1 = _write_attachment1(tmp_path / "附件1.xlsx")
    attachment2 = _write_wide_workbook(
        tmp_path / "附件2.xlsx",
        sheets=("实际负荷", "实际光伏"),
        days=(day,),
        bad_values={("实际负荷", day, 4): -1.0},
    )

    with pytest.raises(InputError) as excinfo:
        load_q2_inputs(
            attachment1_path=attachment1,
            attachment2_path=attachment2,
            load_sheet_name="实际负荷",
            pv_sheet_name="实际光伏",
        )
    message = str(excinfo.value)
    assert "attachment2 load" in message
    assert "附件2.xlsx!实际负荷!F2" in message


def test_load_q2_inputs_wraps_missing_path_and_sheet(tmp_path: Path) -> None:
    attachment1 = _write_attachment1(tmp_path / "附件1.xlsx")
    attachment2 = _write_wide_workbook(
        tmp_path / "附件2.xlsx",
        sheets=("实际负荷", "实际光伏"),
        days=(dt.date(2025, 1, 1),),
    )

    with pytest.raises(InputError, match="attachment1 path is not a file"):
        load_q2_inputs(
            attachment1_path=tmp_path / "missing.xlsx",
            attachment2_path=attachment2,
            load_sheet_name="实际负荷",
            pv_sheet_name="实际光伏",
        )
    with pytest.raises(InputError, match="attachment2 load.*missing-sheet"):
        load_q2_inputs(
            attachment1_path=attachment1,
            attachment2_path=attachment2,
            load_sheet_name="missing-sheet",
            pv_sheet_name="实际光伏",
        )
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
uv run --locked pytest -q tests/test_q2_inputs.py
```

Expected: collection fails because `load_q2_inputs` is not exported by `q2_inputs.py`.

- [ ] **Step 4: Implement explicit-path loading and validation helpers**

Add these imports and private structures to `q2_inputs.py`:

```python
import zipfile
from typing import Any, Callable, TypeVar

from openpyxl.utils.exceptions import InvalidFileException

from ..dataio import read_attachment1, read_wide_attachment, sha256_file

_FIXED_PRICE_SENTINEL_DAY = dt.date(2000, 1, 1)
_T = TypeVar("_T")


@dataclass(frozen=True)
class _WideValue:
    day: dt.date
    slot: int
    start: dt.datetime
    end: dt.datetime
    value: float
    source_ref: str


def _require_file(logical_name: str, path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_file():
        raise InputError(f"{logical_name} path is not a file: {candidate}")
    return candidate


def _source_ref(record: dict[str, Any] | object) -> str:
    if isinstance(record, dict):
        source_file = str(record["source_file"])
        sheet = str(record["sheet_name"])
        cell = str(record["cell_ref"])
        digest = str(record["source_hash"])
    else:
        source_file = str(getattr(record, "source_file"))
        sheet = str(getattr(record, "sheet_name"))
        cell = str(getattr(record, "cell_ref"))
        digest = str(getattr(record, "source_hash"))
    return f"{Path(source_file).name}!{sheet}!{cell} sha256={digest[:12]}"


def _call_reader(logical_name: str, path: Path, reader: Callable[[], _T]) -> _T:
    try:
        return reader()
    except InputError as exc:
        raise InputError(f"{logical_name} path={path}: {exc}") from exc
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, InvalidFileException) as exc:
        raise InputError(f"{logical_name} path={path}: {exc}") from exc


def _parse_source_day(logical_name: str, record: dict[str, Any]) -> dt.date:
    raw = record.get("source_date")
    try:
        return dt.date.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise InputError(
            f"{logical_name} has invalid source_date={raw!r} at {_source_ref(record)}"
        ) from exc


def _wide_values(
    *,
    logical_name: str,
    path: Path,
    sheet_name: str,
    kind: str,
    unit: str,
) -> dict[tuple[dt.date, int], _WideValue]:
    records = _call_reader(
        logical_name,
        path,
        lambda: read_wide_attachment(path, sheet_name=sheet_name, kind=kind, unit=unit),
    )
    if not records:
        raise InputError(f"{logical_name} path={path} sheet={sheet_name}: no records")

    grid = TimeGrid()
    values: dict[tuple[dt.date, int], _WideValue] = {}
    for record in records:
        ref = _source_ref(record)
        day = _parse_source_day(logical_name, record)
        raw_label = str(record.get("raw_time_label", ""))
        try:
            interval = grid.interval_from_right_endpoint(day, raw_label)
        except (InputError, ValueError) as exc:
            raise InputError(f"{logical_name} has bad time label at {ref}: {exc}") from exc
        if interval.day != day:
            raise InputError(
                f"{logical_name} right endpoint crosses before its source day at {ref}"
            )
        parsed_raw = record.get("parsed_timestamp")
        if parsed_raw is None:
            raise InputError(f"{logical_name} missing parsed_timestamp at {ref}")
        try:
            parsed_end = dt.datetime.fromisoformat(str(parsed_raw))
        except ValueError as exc:
            raise InputError(f"{logical_name} has invalid parsed_timestamp at {ref}") from exc
        if parsed_end != interval.end:
            raise InputError(
                f"{logical_name} parsed_timestamp disagrees with right endpoint at {ref}"
            )

        raw_value = record.get("value")
        _require_nonnegative_finite(logical_name, raw_value, source_ref=ref)
        key = (interval.day, interval.index)
        if key in values:
            raise InputError(f"{logical_name} duplicate key={key} at {ref}")
        values[key] = _WideValue(
            day=interval.day,
            slot=interval.index,
            start=interval.start,
            end=interval.end,
            value=float(raw_value),
            source_ref=ref,
        )

    _require_complete_days(logical_name, path, sheet_name, values)
    return values


def _require_complete_days(
    logical_name: str,
    path: Path,
    sheet_name: str,
    values: dict[tuple[dt.date, int], _WideValue],
) -> None:
    expected = set(range(TimeGrid.steps_per_day))
    days = sorted({day for day, _slot in values})
    for day in days:
        observed = {slot for value_day, slot in values if value_day == day}
        if observed != expected:
            missing = sorted(expected - observed)
            extra = sorted(observed - expected)
            raise InputError(
                f"{logical_name} path={path} sheet={sheet_name} day={day} "
                f"missing_slots={missing} extra_slots={extra}"
            )
```

Then implement the public Q2 loader:

```python
def _fixed_prices(path: Path) -> tuple[FixedPricePoint, ...]:
    records = _call_reader("attachment1", path, lambda: read_attachment1(path))
    price_records = [record for record in records if record.kind == "price_cny_per_kwh"]
    points: dict[int, FixedPricePoint] = {}
    grid = TimeGrid()
    for record in price_records:
        ref = _source_ref(record)
        try:
            interval = grid.interval_from_right_endpoint(
                _FIXED_PRICE_SENTINEL_DAY, record.raw_time_label
            )
        except (InputError, ValueError) as exc:
            raise InputError(f"attachment1 has bad time label at {ref}: {exc}") from exc
        if interval.day != _FIXED_PRICE_SENTINEL_DAY:
            raise InputError(f"attachment1 right endpoint crosses before the sentinel day at {ref}")
        point = FixedPricePoint(
            slot=interval.index,
            price_cny_per_kwh=record.value,
            source_ref=ref,
        )
        if point.slot in points:
            raise InputError(f"attachment1 duplicate slot={point.slot} at {ref}")
        points[point.slot] = point

    expected = set(range(TimeGrid.steps_per_day))
    if set(points) != expected:
        raise InputError(
            f"attachment1 path={path} missing_slots={sorted(expected - set(points))} "
            f"extra_slots={sorted(set(points) - expected)}"
        )
    return tuple(points[slot] for slot in range(TimeGrid.steps_per_day))


def load_q2_inputs(
    *,
    attachment1_path: str | Path,
    attachment2_path: str | Path,
    load_sheet_name: str,
    pv_sheet_name: str,
) -> Q2InputBundle:
    attachment1 = _require_file("attachment1", attachment1_path)
    attachment2 = _require_file("attachment2", attachment2_path)
    fixed_prices = _fixed_prices(attachment1)
    loads = _wide_values(
        logical_name="attachment2 load",
        path=attachment2,
        sheet_name=load_sheet_name,
        kind="load_kw",
        unit="kW",
    )
    pvs = _wide_values(
        logical_name="attachment2 pv",
        path=attachment2,
        sheet_name=pv_sheet_name,
        kind="pv_actual_kw",
        unit="kW",
    )
    if set(loads) != set(pvs):
        raise InputError(
            f"attachment2 load/pv grid mismatch path={attachment2} "
            f"load_only={sorted(set(loads) - set(pvs))[:10]} "
            f"pv_only={sorted(set(pvs) - set(loads))[:10]}"
        )

    actuals = tuple(
        ActualInterval(
            day=loads[key].day,
            slot=loads[key].slot,
            start=loads[key].start,
            end=loads[key].end,
            load_kw=loads[key].value,
            pv_kw=pvs[key].value,
            load_source_ref=loads[key].source_ref,
            pv_source_ref=pvs[key].source_ref,
        )
        for key in sorted(loads)
    )
    return Q2InputBundle(
        fixed_prices=fixed_prices,
        actuals=actuals,
        input_hashes=tuple(
            sorted(
                (
                    ("attachment1", sha256_file(attachment1)),
                    ("attachment2", sha256_file(attachment2)),
                )
            )
        ),
    )
```

If Ruff wraps lines, accept its deterministic formatting; do not change behavior or infer sheet names.

- [ ] **Step 5: Run the Task 2 tests and full file tests**

Run:

```bash
uv run --locked pytest -q   tests/test_q2_inputs.py::test_load_q2_inputs_aligns_fixed_prices_and_actuals   tests/test_q2_inputs.py::test_load_q2_inputs_does_not_modify_sources
uv run --locked pytest -q tests/test_q2_inputs.py
```

Expected: both named tests pass; all Task 1 tests remain green.

- [ ] **Step 6: Format, inspect the source boundary, and commit locally**

Run:

```bash
uv run --locked ruff format src/microgrid/problem/q2_inputs.py tests/test_q2_inputs.py
uv run --locked ruff check src/microgrid/problem/q2_inputs.py tests/test_q2_inputs.py
git diff --check
git diff --name-only
git add src/microgrid/problem/q2_inputs.py tests/test_q2_inputs.py
git commit -m "feat: load Q2 source inputs"
```

Expected: the name list contains only the adapter and its test. Do not push.

---

### Task 3: Attachment 4 Loading and Explicit Q4-2 Grid Matching

**Files:**
- Modify: `src/microgrid/problem/q2_inputs.py`
- Modify: `tests/test_q2_inputs.py`

**Interfaces:**
- Consumes: Task 2 `_wide_values`, `VariablePricePoint`, `VariablePriceBundle`, and `Q2InputBundle`.
- Produces: `load_q4_2_prices(*, attachment4_path, price_sheet_name) -> VariablePriceBundle` and `require_matching_q4_2_grid(q2_inputs, variable_prices) -> None`.

- [ ] **Step 1: Write the failing Attachment 4 and grid tests**

Add imports for the two public functions and add:

```python
def test_load_q4_2_prices_and_require_matching_grid(tmp_path: Path) -> None:
    day = dt.date(2025, 1, 1)
    attachment1 = _write_attachment1(tmp_path / "附件1.xlsx")
    attachment2 = _write_wide_workbook(
        tmp_path / "附件2.xlsx",
        sheets=("实际负荷", "实际光伏"),
        days=(day,),
    )
    attachment4 = _write_wide_workbook(
        tmp_path / "附件4.xlsx",
        sheets=("波动电价",),
        days=(day,),
    )
    q2_inputs = load_q2_inputs(
        attachment1_path=attachment1,
        attachment2_path=attachment2,
        load_sheet_name="实际负荷",
        pv_sheet_name="实际光伏",
    )

    prices = load_q4_2_prices(
        attachment4_path=attachment4,
        price_sheet_name="波动电价",
    )

    assert len(prices.prices) == 144
    assert prices.prices[0].start == dt.datetime(2025, 1, 1, 0, 0)
    assert prices.prices[-1].end == dt.datetime(2025, 1, 2, 0, 0)
    assert prices.prices[0].source_ref.startswith("附件4.xlsx!波动电价!B2 ")
    assert prices.input_hashes == (("attachment4", sha256_file(attachment4)),)
    require_matching_q4_2_grid(q2_inputs, prices)


def test_require_matching_q4_2_grid_rejects_different_days(tmp_path: Path) -> None:
    attachment1 = _write_attachment1(tmp_path / "附件1.xlsx")
    attachment2 = _write_wide_workbook(
        tmp_path / "附件2.xlsx",
        sheets=("实际负荷", "实际光伏"),
        days=(dt.date(2025, 1, 1),),
    )
    attachment4 = _write_wide_workbook(
        tmp_path / "附件4.xlsx",
        sheets=("波动电价",),
        days=(dt.date(2025, 1, 2),),
    )
    q2_inputs = load_q2_inputs(
        attachment1_path=attachment1,
        attachment2_path=attachment2,
        load_sheet_name="实际负荷",
        pv_sheet_name="实际光伏",
    )
    prices = load_q4_2_prices(
        attachment4_path=attachment4,
        price_sheet_name="波动电价",
    )

    with pytest.raises(InputError, match="Q2/Attachment 4 grid mismatch"):
        require_matching_q4_2_grid(q2_inputs, prices)
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run --locked pytest -q   tests/test_q2_inputs.py::test_load_q4_2_prices_and_require_matching_grid   tests/test_q2_inputs.py::test_require_matching_q4_2_grid_rejects_different_days
```

Expected: collection fails because `load_q4_2_prices` and `require_matching_q4_2_grid` do not exist.

- [ ] **Step 3: Implement the two narrow public functions**

Append to `q2_inputs.py`:

```python
def load_q4_2_prices(
    *,
    attachment4_path: str | Path,
    price_sheet_name: str,
) -> VariablePriceBundle:
    attachment4 = _require_file("attachment4", attachment4_path)
    values = _wide_values(
        logical_name="attachment4 price",
        path=attachment4,
        sheet_name=price_sheet_name,
        kind="price_actual_cny_per_kwh",
        unit="元/kWh",
    )
    prices = tuple(
        VariablePricePoint(
            day=value.day,
            slot=value.slot,
            start=value.start,
            end=value.end,
            price_cny_per_kwh=value.value,
            source_ref=value.source_ref,
        )
        for _key, value in sorted(values.items())
    )
    return VariablePriceBundle(
        prices=prices,
        input_hashes=(("attachment4", sha256_file(attachment4)),),
    )


def require_matching_q4_2_grid(
    q2_inputs: Q2InputBundle,
    variable_prices: VariablePriceBundle,
) -> None:
    actual_keys = {(item.day, item.slot) for item in q2_inputs.actuals}
    price_keys = {(item.day, item.slot) for item in variable_prices.prices}
    if actual_keys != price_keys:
        raise InputError(
            "Q2/Attachment 4 grid mismatch: "
            f"actual_only={sorted(actual_keys - price_keys)[:10]} "
            f"price_only={sorted(price_keys - actual_keys)[:10]}"
        )
```

Do not merge prices into Q2 inputs and do not calculate any cost.

- [ ] **Step 4: Run tests, lint, and format**

Run:

```bash
uv run --locked pytest -q tests/test_q2_inputs.py
uv run --locked ruff format src/microgrid/problem/q2_inputs.py tests/test_q2_inputs.py
uv run --locked ruff check src/microgrid/problem/q2_inputs.py tests/test_q2_inputs.py
```

Expected: all tests pass; values remain sorted by `(day, slot)`.

- [ ] **Step 5: Commit locally**

Run:

```bash
git diff --check
git add src/microgrid/problem/q2_inputs.py tests/test_q2_inputs.py
git commit -m "feat: load Q4-2 price history"
```

Do not push.

---

### Task 4: Causal Historical Information Views

**Files:**
- Modify: `src/microgrid/problem/q2_inputs.py`
- Modify: `tests/test_q2_inputs.py`
- Read: `src/microgrid/problem/contracts.py:352-389`

**Interfaces:**
- Consumes: `Q2InputBundle`, optional `VariablePriceBundle`, `InfoItem`, and `InfoSet.from_raw`.
- Produces: `historical_info_items(q2_inputs, *, variable_prices=None) -> tuple[InfoItem, ...]` and `info_set_at(decision_time, items) -> InfoSet`.

- [ ] **Step 1: Write the failing causality test**

Add imports and:

```python
def test_historical_info_items_are_causal_at_interval_end(tmp_path: Path) -> None:
    day = dt.date(2025, 1, 1)
    attachment1 = _write_attachment1(tmp_path / "附件1.xlsx")
    attachment2 = _write_wide_workbook(
        tmp_path / "附件2.xlsx",
        sheets=("实际负荷", "实际光伏"),
        days=(day,),
    )
    attachment4 = _write_wide_workbook(
        tmp_path / "附件4.xlsx",
        sheets=("波动电价",),
        days=(day,),
    )
    q2_inputs = load_q2_inputs(
        attachment1_path=attachment1,
        attachment2_path=attachment2,
        load_sheet_name="实际负荷",
        pv_sheet_name="实际光伏",
    )
    prices = load_q4_2_prices(
        attachment4_path=attachment4,
        price_sheet_name="波动电价",
    )
    items = historical_info_items(q2_inputs, variable_prices=prices)

    before_end = info_set_at(dt.datetime(2025, 1, 1, 0, 9, 59), items)
    at_end = info_set_at(dt.datetime(2025, 1, 1, 0, 10), items)

    assert before_end.visible_items == ()
    assert [item.kind for item in at_end.visible_items] == [
        "load_actual_kw",
        "price_actual_cny_per_kwh",
        "pv_actual_kw",
    ]
    assert all(item.available_at == dt.datetime(2025, 1, 1, 0, 10) for item in at_end.visible_items)
    assert all(item.valid_time == item.available_at for item in at_end.visible_items)
    assert not any(
        item.valid_time == dt.datetime(2025, 1, 1, 0, 20) for item in at_end.visible_items
    )
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run --locked pytest -q   tests/test_q2_inputs.py::test_historical_info_items_are_causal_at_interval_end
```

Expected: collection fails because `historical_info_items` and `info_set_at` are absent.

- [ ] **Step 3: Implement deterministic item creation and the narrow InfoSet wrapper**

Append:

```python
def historical_info_items(
    q2_inputs: Q2InputBundle,
    *,
    variable_prices: VariablePriceBundle | None = None,
) -> tuple[InfoItem, ...]:
    items: list[InfoItem] = []
    for actual in q2_inputs.actuals:
        items.extend(
            (
                InfoItem(
                    kind="load_actual_kw",
                    available_at=actual.end,
                    valid_time=actual.end,
                    value=actual.load_kw,
                    source_ref=actual.load_source_ref,
                ),
                InfoItem(
                    kind="pv_actual_kw",
                    available_at=actual.end,
                    valid_time=actual.end,
                    value=actual.pv_kw,
                    source_ref=actual.pv_source_ref,
                ),
            )
        )
    if variable_prices is not None:
        for price in variable_prices.prices:
            items.append(
                InfoItem(
                    kind="price_actual_cny_per_kwh",
                    available_at=price.end,
                    valid_time=price.end,
                    value=price.price_cny_per_kwh,
                    source_ref=price.source_ref,
                )
            )
    return tuple(sorted(items, key=lambda item: (item.valid_time, item.kind, item.source_ref)))


def info_set_at(
    decision_time: dt.datetime,
    items: tuple[InfoItem, ...],
) -> InfoSet:
    return InfoSet.from_raw(decision_time, items)
```

Do not include fixed prices in historical actual items.

- [ ] **Step 4: Run focused and complete adapter tests**

Run:

```bash
uv run --locked pytest -q   tests/test_q2_inputs.py::test_historical_info_items_are_causal_at_interval_end
uv run --locked pytest -q tests/test_q2_inputs.py
uv run --locked ruff format src/microgrid/problem/q2_inputs.py tests/test_q2_inputs.py
uv run --locked ruff check src/microgrid/problem/q2_inputs.py tests/test_q2_inputs.py
```

Expected: all tests pass and item order is `(valid_time, kind, source_ref)`.

- [ ] **Step 5: Commit locally**

Run:

```bash
git diff --check
git add src/microgrid/problem/q2_inputs.py tests/test_q2_inputs.py
git commit -m "feat: expose causal B history"
```

Do not push.

---

### Task 5: B Decision Evidence and Non-Implemented Model Card

**Files:**
- Create: `docs/b_decision_evidence.md`
- Create: `docs/b_model_card.md`
- Read: `configs/decisions.toml:1-96`
- Read: `docs/superpowers/specs/2026-09-11-b-q2-input-adapter-design.md`

**Interfaces:**
- Consumes: current decision statuses and the four user-reviewed model invariants.
- Produces: engineering evidence only; no executable model, approval, confirmation identity, or result claim.

- [ ] **Step 1: Create the decision evidence document**

Create `docs/b_decision_evidence.md` with this structure and concrete entries:

```markdown
# B 任务决策证据

本文只整理证据和人工审批问题，不改变 `configs/decisions.toml`，也不代表任何模型已获批准。

| Decision | 当前状态 | 已有证据 | 仍需参赛队确认 | 可执行核验 |
|---|---|---|---|---|
| `D_STATE` | proposed | 2025-01 初始 6000 kWh；shared-core 保存连续状态语义 | Q2/Q4-2 连续期末状态如何进入最终比较 | 检查跨日状态连续且没有每日重置 |
| `D_INFO` | proposed | actual 只在十分钟区间结束时可见；固定价格与历史 actual 分开 | 计划时刻可使用的历史窗口与预测器输入 | 对每个 decision_time 验证 `available_at <= decision_time` |
| `D_SETTLE` | pending | 题面给出紧急购电 5 倍、调整差异 50%/1.5 倍 | 完整结算分量、调整比较基准和余电处理 | 按分量对账，禁止把计划量替换成实际使用量 |
| `D_MODEL_Q2` | pending | 已有内部时间、电池和结果契约；本分支只提供输入适配 | 预测方法、变量、目标函数、约束和求解器 | 模型批准前 runner 必须继续抛出未实现错误 |
| `D_MODEL_Q4_2` | pending | 附件4可作为历史/回放价格；Q4-2 不依赖 `D_RESAMPLE` | 未来价格信息条件与 Q2 模型扩展 | 检查价格因果边界并保持 Q2/Q4-2 独立批准 |
| `D_TIME_TEMPLATE_EXPORT` | pending | 附件右端点网格与 result 模板字面标签存在异常 | 正式 Excel 的列到内部 slot 映射 | 批准前不得导出正式 result 工作簿 |

## 已确认需要保留的模型不变量

1. 附件1/2/4按右端点对齐；附件3“预报1小时”为发布后一小时，日期只在同日四行发布块内继承，数值禁止 ffill。
2. 负载和光伏是 kW；十分钟电量只换算一次：`energy_kwh = power_kw * (1 / 6 h)`。
3. 未来供需约束方向为 `grid_supplied_kwh + discharge_kwh + pv_kwh >= load_kwh + charge_kwh`。
4. 计划购电费按 `planned_purchase_kwh` 结算，不按实际使用量、净负荷或事后剩余量结算。

## 当前工程结论

输入适配、因果过滤和合成结果载体可以测试；Q2/Q4-2 的预测、优化、结算和正式导出仍未获批准、未实现。
```

- [ ] **Step 2: Create the model card**

Create `docs/b_model_card.md`:

```markdown
# B 任务 Q2/Q4-2 模型卡

- 状态：未批准、未实现
- 当前可执行范围：输入适配、来源追踪、内部时间对齐、因果信息过滤、合成结果接口测试
- 当前禁止范围：正式预测、优化、结算、case 运行、结果 Excel 导出和论文数值引用

## 输入契约

- 附件1固定电价：144 个唯一右端点 slot，有限且非负。
- 附件2实际负载/PV：每个日期各 144 个唯一 slot，两个 sheet 键集合完全一致，保留 kW。
- 附件4实际波动电价：每个日期 144 个唯一 slot，有限且非负；只表示历史或回放事实。
- 每个来源保留文件名、sheet、cell 和 SHA-256 前缀；bundle 保留完整文件 SHA-256。

## 时间与信息边界

- `0:10` 对应 `[00:00, 00:10)`；`0:00+1` 对应 `[23:50, 次日 00:00)`。
- 完整区间 actual 的 `valid_time` 与 `available_at` 都是区间结束时刻。
- 固定电价不混入历史 actual 信息项；未来波动电价不得由历史实测价格冒充。
- `D_TIME_TEMPLATE_EXPORT` 获批前，内部 slot 不映射到正式 result 模板。

## 单位与未来模型验收条件

- `load_kw`、`pv_kw` 保留功率；模型量使用 `load_kwh = load_kw / 6`、`pv_kwh = pv_kw / 6`。
- 供需约束必须使用 `>=`，允许未利用供给或弃光，不为凑等式伪造量。
- 计划购电费必须由计划购电量乘对应电价得到。
- 调整、违约、紧急购电费用保持独立，等待 `D_SETTLE` 和相应模型 decision 获批。

## 输出契约

批准后的模型必须用 shared `IntervalResult` / `CaseResult` 承载计划、调整、紧急购电、电池动作和状态轨迹。当前测试结果必须设置 `is_synthetic=True`，不得进入 `dist/`。

## 批准前置

参赛队需要分别审批 `D_STATE`、`D_INFO`、`D_SETTLE`、`D_MODEL_Q2` 和 `D_MODEL_Q4_2`；正式 Excel 还需要 `D_TIME_TEMPLATE_EXPORT`。审批必须在配置中由人工填写状态、确认人和确认时间，本分支不代填。

## 回归核验

- 目标与全量 pytest 通过。
- Ruff lint 和 format check 通过。
- `q2.py`、`q4_2.py` 仍抛出 `ModelNotImplementedError`。
- decisions 配置无差异。
- Git diff 不含原始题包、raw、templates、resources、outputs 或 dist 文件。
```

- [ ] **Step 3: Verify document content and decision immutability**

Run:

```bash
grep -n -e D_STATE -e D_INFO -e D_SETTLE -e D_MODEL_Q2 -e D_MODEL_Q4_2 -e D_TIME_TEMPLATE_EXPORT   docs/b_decision_evidence.md docs/b_model_card.md
grep -n -e "kW" -e "kWh" -e ">=" -e "planned_purchase_kwh"   docs/b_decision_evidence.md docs/b_model_card.md
git diff --exit-code main -- configs/decisions.toml   src/microgrid/problem/q2.py src/microgrid/problem/q4_2.py
git diff --check
```

Expected: every required decision and invariant is found; protected files have no branch diff.

- [ ] **Step 4: Commit locally**

Run:

```bash
git add docs/b_decision_evidence.md docs/b_model_card.md
git commit -m "docs: record B model decision evidence"
```

Do not push.

---

### Task 6: Full Verification and Branch Audit

**Files:**
- Verify: `src/microgrid/problem/q2_inputs.py`
- Verify: `tests/test_q2_inputs.py`
- Verify: `docs/b_decision_evidence.md`
- Verify: `docs/b_model_card.md`
- Verify unchanged: `configs/decisions.toml`, `src/microgrid/problem/q2.py`, `src/microgrid/problem/q4_2.py`

**Interfaces:**
- Consumes: all prior task commits.
- Produces: fresh evidence that the adapter is testable, guards remain intact, and no protected data entered the branch.

- [ ] **Step 1: Run the target tests**

Run:

```bash
uv run --locked pytest -q tests/test_q2_inputs.py
```

Expected: every B adapter test passes.

- [ ] **Step 2: Run the full project gate**

Run:

```bash
uv lock --check
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
```

Expected: every command exits 0. Record the fresh pytest count; do not reuse the 73-test baseline number.

- [ ] **Step 3: Re-run existing model and release guard tests**

Run:

```bash
uv run --locked pytest -q   tests/test_release_guards.py::test_case_dependency_graph_splits_model_gates_and_q4_2_has_no_resample   tests/test_release_guards.py::test_approved_decisions_dispatch_to_unimplemented_runner   tests/test_release_guards.py::test_final_guard_has_no_permanent_stage0_blocker
```

Expected: all three pass, showing the adapter did not bypass approval or unimplemented-model guards. Do not invoke a formal case command.

- [ ] **Step 4: Audit the branch and protected paths**

Run:

```bash
git diff --check main...HEAD
git diff --name-status main...HEAD
git diff --exit-code main -- configs/decisions.toml   src/microgrid/problem/q2.py src/microgrid/problem/q4_2.py
git status --short --branch
```

Expected:

- Tracked branch changes are limited to the approved spec, implementation plan, `q2_inputs.py`, `test_q2_inputs.py`, and the two B documents.
- `CUMCM2026Problems/` and the three local planning files may remain untracked but are never staged.
- No path under `data/raw/`, `data/templates/`, `resources/`, `outputs/`, or `dist/` appears in the branch diff.
- No protected runner or decision file differs from `main`.

- [ ] **Step 5: Report commits and request push authorization**

Report the final local commit hashes, `git diff --stat main...HEAD`, target/full test results, and protected-path audit. Explicitly state that nothing has been pushed after the reviewed spec revision. Ask for a fresh yes/no authorization before running `git push`.
