"""Read-only annual input preflight; actual arrays belong to replay only."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from ..artifacts import verify_required_inputs
from ..dataio import read_wide_attachment, sha256_file
from ..schemas import InputError
from .contracts import InfoItem
from .q3_inputs import load_q3_forecast_archive
from .q4_common import STEP, YEAR_END, YEAR_START, nonnegative


def annual_time_keys() -> tuple[dt.datetime, ...]:
    return tuple(YEAR_START + (i + 1) * STEP for i in range(int((YEAR_END - YEAR_START) / STEP)))


@dataclass(frozen=True)
class Q4Inputs:
    load_kw: tuple[float, ...]
    pv_kw: tuple[float, ...]
    prices: tuple[float, ...]
    official: tuple[InfoItem, ...]
    source_hashes: dict[str, str]
    snapshot: dict
    time_keys: tuple[dt.datetime, ...] = field(default_factory=annual_time_keys)

    def __post_init__(self):
        if self.time_keys != annual_time_keys() or any(
            len(values) != len(self.time_keys) for values in (self.load_kw, self.pv_kw, self.prices)
        ):
            raise InputError("Q4 actual series need a common complete 52560-slot annual grid")

    def index(self, time: dt.datetime) -> int:
        seconds = (time - YEAR_START).total_seconds()
        if seconds % STEP.total_seconds():
            raise InputError("unaligned input index")
        index = int(seconds // STEP.total_seconds())
        if not 0 <= index < len(self.prices):
            raise InputError(f"actual outside annual input: {time}")
        return index

    def actual_info(self, index: int) -> tuple[InfoItem, ...]:
        end = YEAR_START + (index + 1) * STEP
        return tuple(
            InfoItem(kind, end, end, values[index], path)
            for kind, values, path in (
                ("load_actual_kw", self.load_kw, "data/raw/附件2.xlsx"),
                ("pv_actual_kw", self.pv_kw, "data/raw/附件2.xlsx"),
                ("price_actual", self.prices, "data/raw/附件4.xlsx"),
            )
        )


def load_q4_inputs(repo: Path, case_id: str) -> Q4Inputs:
    required = ["data/raw/附件2.xlsx", "data/raw/附件4.xlsx"]
    if case_id == "q4_3":
        required.append("data/raw/附件3.xlsx")
    issues = verify_required_inputs(repo, required)
    if issues:
        raise InputError("; ".join(issues))
    expected_times = annual_time_keys()
    specifications = (
        ("data/raw/附件2.xlsx", "小区负载", "load_actual_kw", "kW"),
        ("data/raw/附件2.xlsx", "光伏发电实际功率", "pv_actual_kw", "kW"),
        ("data/raw/附件4.xlsx", "Sheet1", "price_actual", "元/kWh"),
    )
    arrays = []
    tables = []
    for path, sheet, kind, unit in specifications:
        rows = read_wide_attachment(repo / path, sheet_name=sheet, kind=kind, unit=unit)
        pairs = []
        for number, row in enumerate(rows, 1):
            try:
                timestamp = row.get("parsed_timestamp")
                if not isinstance(timestamp, str) or not timestamp.strip():
                    raise ValueError("missing timestamp")
                pairs.append(
                    (dt.datetime.fromisoformat(timestamp), nonnegative(kind, row["value"]))
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise InputError(
                    f"{path}:{sheet}:record {number}:invalid timestamp/value: {exc}"
                ) from exc
        times = tuple(time for time, _ in pairs)
        if times != expected_times:
            raise InputError(f"{path}:{sheet} needs unique complete 52560-slot annual grid")
        values = tuple(value for _, value in pairs)
        arrays.append(values)
        tables.append(
            {
                "path": path,
                "sheet": sheet,
                "kind": kind,
                "unit": unit,
                "count": len(values),
                "first_right_endpoint": expected_times[0],
                "last_right_endpoint": YEAR_END,
            }
        )
    official = ()
    if case_id == "q4_3":
        archive = load_q3_forecast_archive(repo / "data/raw/附件3.xlsx")
        official = archive.raw_info_items()
        tables.append(
            {
                "path": required[-1],
                "versions": len(archive.versions),
                "records": len(official),
                "unit": "kW",
            }
        )
    hashes = {path: sha256_file(repo / path) for path in required}
    snapshot = {
        "schema_version": 1,
        "case_id": case_id,
        "timezone": "Asia/Shanghai",
        "timestamp_convention": "naive local; right endpoint; preceding ten-minute average",
        "power_to_kwh": 1 / 6,
        "price_conversion": 1,
        "source_hashes": hashes,
        "tables": tables,
    }
    return Q4Inputs(*arrays, official, hashes, snapshot, expected_times)
