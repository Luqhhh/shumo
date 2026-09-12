"""Structured Q3 audit sidecars frozen by the team on 2026-09-12.

``CaseResult`` keeps its global schema. Q3's one-to-many forecast, plan-version
and settlement history is written to three JSONL sidecars. This module only
serializes existing domain records; it chooses no forecasting or replay method.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..dataio import ensure_dir, sha256_file
from ..schemas import InputError
from .contracts import ENERGY_ABS_TOL_KWH, STEP_MINUTES, STEPS_PER_DAY, TimeGrid
from .q3_load_forecast import LoadForecastPoint, load_forecast_id
from .q3_plan_ledger import Q3PlanLedger
from .q3_pv_snapshot import PVForecastSnapshot, TailForecast

Q3_SIDECAR_SCHEMA_VERSION = 2
FORECAST_PROVENANCE_FILENAME = "forecast_provenance.jsonl"
PLAN_VERSIONS_FILENAME = "plan_versions.jsonl"
SETTLEMENT_LEDGER_FILENAME = "settlement_ledger.jsonl"


def _iso(value: dt.date | dt.datetime) -> str:
    return value.isoformat()


def _version_id(ledger: Q3PlanLedger, version: int) -> str:
    return f"{ledger.day.isoformat()}:v{version}"


@dataclass(frozen=True)
class PlanSlotForecastLink:
    """Forecast IDs used by one slot of one immutable plan version."""

    day: dt.date
    version: int
    target_slot: int
    forecast_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.version < 0:
            raise ValueError("plan forecast-link version must be non-negative")
        if not 0 <= self.target_slot < STEPS_PER_DAY:
            raise ValueError("plan forecast-link target_slot is outside the daily grid")
        if not self.forecast_ids or any(not value for value in self.forecast_ids):
            raise ValueError("plan forecast-link IDs must be non-empty")
        if len(self.forecast_ids) != len(set(self.forecast_ids)):
            raise ValueError("plan forecast-link IDs must be unique within a slot")


@dataclass(frozen=True)
class Q3SidecarArtifact:
    """Path and digest metadata suitable for ``CaseResult.metadata``."""

    name: str
    path: Path
    schema_version: int
    sha256: str
    row_count: int

    def metadata(self, *, relative_to: Path) -> dict[str, Any]:
        try:
            relative_path = self.path.relative_to(relative_to)
        except ValueError as exc:
            raise ValueError("Q3 sidecar is outside its run directory") from exc
        return {
            "path": relative_path.as_posix(),
            "schema_version": self.schema_version,
            "sha256": self.sha256,
            "row_count": self.row_count,
        }


@dataclass(frozen=True)
class Q3SidecarSet:
    forecast_provenance: Q3SidecarArtifact
    plan_versions: Q3SidecarArtifact
    settlement_ledger: Q3SidecarArtifact

    @property
    def paths(self) -> tuple[Path, Path, Path]:
        return (
            self.forecast_provenance.path,
            self.plan_versions.path,
            self.settlement_ledger.path,
        )

    def metadata(self, *, relative_to: Path) -> dict[str, dict[str, Any]]:
        return {
            artifact.name: artifact.metadata(relative_to=relative_to)
            for artifact in (
                self.forecast_provenance,
                self.plan_versions,
                self.settlement_ledger,
            )
        }


def _snapshot_source_details(
    snapshot: PVForecastSnapshot,
    valid_time: dt.datetime,
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    """Return auditable source knots for one resampled attachment-3 value."""

    lead_hours = (valid_time - snapshot.issue_time).total_seconds() / 3600.0
    if not 0 < lead_hours <= 24:
        raise ValueError("snapshot provenance valid_time is outside its 24-hour horizon")

    if snapshot.resampling_method == "linear":
        knot_hours = tuple(sorted({math.floor(lead_hours), math.ceil(lead_hours)}))
    else:
        # PCHIP slopes use neighbouring knots. Retain every input knot rather
        # than asserting a misleading pair of linear weights.
        knot_hours = tuple(range(25))

    source_refs: list[str] = []
    components: list[dict[str, Any]] = []
    for knot_hour in knot_hours:
        if knot_hour == 0:
            source_refs.append(snapshot.boundary_proxy.source_ref)
            components.append(
                {
                    "component_kind": "actual_boundary_proxy",
                    "valid_time": _iso(snapshot.boundary_proxy.interval_end),
                    "value_kw": snapshot.boundary_proxy.mean_power_kw,
                    "source_ref": snapshot.boundary_proxy.source_ref,
                }
            )
            continue

        knot_time = snapshot.issue_time + dt.timedelta(hours=knot_hour)
        contributions = tuple(
            item
            for item in snapshot.combined_forecast.contributions
            if item.valid_time == knot_time
        )
        if not contributions:
            raise ValueError("snapshot hourly knot has no retained vintage contribution")
        for item in contributions:
            source_refs.append(item.source_ref)
            components.append(
                {
                    "component_kind": "attachment3_vintage",
                    "issue_time": _iso(item.issue_time),
                    "valid_time": _iso(item.valid_time),
                    "lead_hours": item.lead_hours,
                    "forecast_value_kw": item.forecast_power_kw,
                    "historical_mae": item.historical_mae_kw,
                    "history_count": item.history_count,
                    "normalized_weight": item.normalized_weight,
                    "source_ref": item.source_ref,
                }
            )
    return tuple(dict.fromkeys(source_refs)), tuple(components)


def forecast_provenance_rows(
    *,
    load_versions: Iterable[tuple[LoadForecastPoint, ...]],
    pv_snapshots: Iterable[PVForecastSnapshot],
    tail_forecasts: Iterable[TailForecast] = (),
) -> tuple[dict[str, Any], ...]:
    """Return one row per ten-minute forecast value retained by Q3."""

    rows: list[dict[str, Any]] = []
    for version in load_versions:
        for point in version:
            rows.append(
                {
                    "schema_version": Q3_SIDECAR_SCHEMA_VERSION,
                    "record_type": "forecast_value",
                    "forecast_id": load_forecast_id(point),
                    "kind": "load",
                    "decision_time": _iso(point.decision_time),
                    "valid_time": _iso(point.valid_time),
                    "value_kw": point.power_kw,
                    "model_version": point.model_version,
                    "training_cutoff": _iso(point.training_cutoff),
                    "source_refs": list(point.source_refs),
                    "available_at": _iso(point.available_at),
                    "value_kwh": point.energy_kwh,
                    "data_version": point.data_version,
                    "raw_value_kw": point.raw_power_kw,
                    "lag_values_kw": list(point.lag_values_kw),
                    "lag_weights": list(point.lag_weights),
                    "ar1_phi": point.ar1_phi,
                    "latest_visible_residual_kw": point.latest_visible_residual_kw,
                    "was_clipped": point.was_clipped,
                }
            )

    for snapshot in pv_snapshots:
        for point in snapshot.points:
            source_refs, components = _snapshot_source_details(snapshot, point.valid_time)
            attachment_components = tuple(
                item for item in components if item["component_kind"] == "attachment3_vintage"
            )
            rows.append(
                {
                    "schema_version": Q3_SIDECAR_SCHEMA_VERSION,
                    "record_type": "forecast_value",
                    "forecast_id": point.forecast_id,
                    "kind": "pv_attachment3",
                    "decision_time": _iso(snapshot.issue_time),
                    "valid_time": _iso(point.valid_time),
                    "value_kw": point.power_kw,
                    "model_version": (
                        f"{snapshot.combined_forecast.model_version}+"
                        f"RESAMPLE-{snapshot.resampling_method.upper()}/v1"
                    ),
                    "training_cutoff": _iso(snapshot.issue_time),
                    "source_refs": list(source_refs),
                    "value_kwh": point.energy_kwh,
                    "issue_time": _iso(snapshot.issue_time),
                    "lead_hours": (point.valid_time - snapshot.issue_time).total_seconds() / 3600.0,
                    "historical_mae": [item["historical_mae"] for item in attachment_components],
                    "normalized_weight": [
                        item["normalized_weight"] for item in attachment_components
                    ],
                    "resampling_method": snapshot.resampling_method,
                    "attachment3_components": list(components),
                }
            )

    for forecast in tail_forecasts:
        for point in forecast.points:
            rows.append(
                {
                    "schema_version": Q3_SIDECAR_SCHEMA_VERSION,
                    "record_type": "forecast_value",
                    "forecast_id": point.forecast_id,
                    "kind": "pv_tail",
                    "decision_time": _iso(point.decision_time),
                    "valid_time": _iso(point.valid_time),
                    "value_kw": point.power_kw,
                    "model_version": point.model_version,
                    "training_cutoff": _iso(point.training_cutoff),
                    "source_refs": list(point.source_refs),
                    "available_at": _iso(point.available_at),
                    "value_kwh": point.energy_kwh,
                    "fallback_reason": point.fallback_reason,
                }
            )

    forecast_ids = tuple(str(row["forecast_id"]) for row in rows)
    if len(forecast_ids) != len(set(forecast_ids)):
        raise InputError("Q3 forecast provenance contains duplicate forecast_id values")
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                str(row["decision_time"]),
                str(row["kind"]),
                str(row["valid_time"]),
            ),
        )
    )


def plan_version_rows(
    ledgers: Iterable[Q3PlanLedger],
    *,
    forecast_links: Iterable[PlanSlotForecastLink],
    known_forecast_ids: frozenset[str],
) -> tuple[dict[str, Any], ...]:
    """Return the complete version chain with explicit forecast references."""

    link_by_key: dict[tuple[dt.date, int, int], PlanSlotForecastLink] = {}
    for link in forecast_links:
        key = (link.day, link.version, link.target_slot)
        if key in link_by_key:
            raise InputError(f"duplicate Q3 plan forecast-link for {key}")
        unknown = set(link.forecast_ids) - known_forecast_ids
        if unknown:
            raise InputError(f"Q3 plan references unknown forecast_id: {sorted(unknown)[0]}")
        link_by_key[key] = link

    rows: list[dict[str, Any]] = []
    used_keys: set[tuple[dt.date, int, int]] = set()
    grid = TimeGrid()
    for ledger in sorted(ledgers, key=lambda item: item.day):
        for version in ledger.versions:
            issue_slot = (version.issue_time.hour * 60 + version.issue_time.minute) // STEP_MINUTES
            previous = None if version.version == 0 else ledger.versions[version.version - 1]
            for slot, quantity in enumerate(version.committed_kwh):
                key = (ledger.day, version.version, slot)
                link = link_by_key.get(key)
                if link is None:
                    raise InputError(f"missing Q3 plan forecast-link for {key}")
                used_keys.add(key)
                interval = grid.interval(ledger.day, slot)
                rows.append(
                    {
                        "schema_version": Q3_SIDECAR_SCHEMA_VERSION,
                        "record_type": "plan_version",
                        "version_id": _version_id(ledger, version.version),
                        "issue_time": _iso(version.issue_time),
                        "target_slot_start": _iso(interval.start),
                        "target_slot_end": _iso(interval.end),
                        "previous_committed_kwh": (
                            None if previous is None else previous.committed_kwh[slot]
                        ),
                        "committed_kwh": quantity,
                        "purchase_is_fixed": slot < issue_slot,
                        "forecast_ids": list(link.forecast_ids),
                    }
                )
    unused_keys = set(link_by_key) - used_keys
    if unused_keys:
        raise InputError(f"Q3 plan forecast-link has no matching version: {sorted(unused_keys)[0]}")
    return tuple(rows)


def settlement_rows(ledgers: Iterable[Q3PlanLedger]) -> tuple[dict[str, Any], ...]:
    """Return base-plan and non-zero adjustment billing events.

    Emergency events belong to the actual replay layer. Until that layer
    supplies them, this writer must neither infer nor fabricate them.
    """

    rows: list[dict[str, Any]] = []
    grid = TimeGrid()
    for ledger in sorted(ledgers, key=lambda item: item.day):
        initial = ledger.versions[0]
        for slot, (quantity, price) in enumerate(
            zip(initial.committed_kwh, ledger.base_prices_cny_per_kwh, strict=True)
        ):
            interval = grid.interval(ledger.day, slot)
            rows.append(
                {
                    "schema_version": Q3_SIDECAR_SCHEMA_VERSION,
                    "record_type": "base_plan",
                    "issue_time": _iso(initial.issue_time),
                    "target_slot_start": _iso(interval.start),
                    "target_slot_end": _iso(interval.end),
                    "previous_committed_kwh": None,
                    "new_committed_kwh": quantity,
                    "delta_plus_kwh": 0.0,
                    "delta_minus_kwh": 0.0,
                    "energy_kwh": quantity,
                    "price_cny_per_kwh": price,
                    "cost_cny": quantity * price,
                }
            )
        for entry in ledger.adjustment_entries:
            if (
                entry.delta_plus_kwh <= ENERGY_ABS_TOL_KWH
                and entry.delta_minus_kwh <= ENERGY_ABS_TOL_KWH
            ):
                continue
            interval = grid.interval(ledger.day, entry.target_slot)
            rows.append(
                {
                    "schema_version": Q3_SIDECAR_SCHEMA_VERSION,
                    "record_type": "adjustment",
                    "issue_time": _iso(entry.issue_time),
                    "target_slot_start": _iso(interval.start),
                    "target_slot_end": _iso(interval.end),
                    "previous_committed_kwh": entry.previous_committed_kwh,
                    "new_committed_kwh": entry.new_committed_kwh,
                    "delta_plus_kwh": entry.delta_plus_kwh,
                    "delta_minus_kwh": entry.delta_minus_kwh,
                    "energy_kwh": entry.delta_plus_kwh + entry.delta_minus_kwh,
                    "price_cny_per_kwh": entry.transaction_price_cny_per_kwh,
                    "cost_cny": entry.adjustment_cost_cny,
                }
            )
    return tuple(rows)


def _write_jsonl(
    path: Path,
    rows: tuple[dict[str, Any], ...],
    *,
    overwrite: bool,
) -> Q3SidecarArtifact:
    if not rows:
        raise InputError(f"Q3 sidecar cannot be empty: {path.name}")
    if path.exists() and not overwrite:
        raise InputError(f"Q3 sidecar already exists: {path}")
    ensure_dir(path.parent)
    payload = "".join(
        json.dumps(row, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n" for row in rows
    )
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)
    return Q3SidecarArtifact(
        name=path.stem,
        path=path,
        schema_version=Q3_SIDECAR_SCHEMA_VERSION,
        sha256=sha256_file(path),
        row_count=len(rows),
    )


def write_q3_sidecars(
    run_dir: str | Path,
    *,
    load_versions: Iterable[tuple[LoadForecastPoint, ...]],
    pv_snapshots: Iterable[PVForecastSnapshot],
    plan_ledgers: Iterable[Q3PlanLedger],
    forecast_links: Iterable[PlanSlotForecastLink],
    tail_forecasts: Iterable[TailForecast] = (),
    overwrite: bool = False,
) -> Q3SidecarSet:
    """Validate and write the three accepted Q3 sidecar files."""

    directory = Path(run_dir)
    load_versions_tuple = tuple(load_versions)
    pv_snapshots_tuple = tuple(pv_snapshots)
    tail_forecasts_tuple = tuple(tail_forecasts)
    ledgers_tuple = tuple(plan_ledgers)
    links_tuple = tuple(forecast_links)
    if not load_versions_tuple or not pv_snapshots_tuple or not ledgers_tuple:
        raise InputError("Q3 formal sidecars require load, PV snapshot and plan records")
    ledger_days = tuple(ledger.day for ledger in ledgers_tuple)
    if len(ledger_days) != len(set(ledger_days)):
        raise InputError("Q3 plan sidecars contain duplicate daily ledgers")

    forecasts = forecast_provenance_rows(
        load_versions=load_versions_tuple,
        pv_snapshots=pv_snapshots_tuple,
        tail_forecasts=tail_forecasts_tuple,
    )
    plans = plan_version_rows(
        ledgers_tuple,
        forecast_links=links_tuple,
        known_forecast_ids=frozenset(str(row["forecast_id"]) for row in forecasts),
    )
    settlements = settlement_rows(ledgers_tuple)

    targets = (
        directory / FORECAST_PROVENANCE_FILENAME,
        directory / PLAN_VERSIONS_FILENAME,
        directory / SETTLEMENT_LEDGER_FILENAME,
    )
    if not overwrite:
        existing = tuple(path for path in targets if path.exists())
        if existing:
            raise InputError(f"Q3 sidecar already exists: {existing[0]}")
    return Q3SidecarSet(
        forecast_provenance=_write_jsonl(targets[0], forecasts, overwrite=overwrite),
        plan_versions=_write_jsonl(targets[1], plans, overwrite=overwrite),
        settlement_ledger=_write_jsonl(targets[2], settlements, overwrite=overwrite),
    )


def load_q3_sidecar(path: str | Path) -> tuple[dict[str, Any], ...]:
    """Read and minimally validate a Q3 JSONL sidecar for audit/review."""

    target = Path(path)
    if not target.is_file():
        raise InputError(f"Q3 sidecar not found: {target}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InputError(f"invalid Q3 sidecar JSON at {target}:{line_number}") from exc
        if not isinstance(row, dict):
            raise InputError(f"Q3 sidecar row must be an object at {target}:{line_number}")
        if row.get("schema_version") != Q3_SIDECAR_SCHEMA_VERSION:
            raise InputError(f"unsupported Q3 sidecar schema at {target}:{line_number}")
        if not row.get("record_type"):
            raise InputError(f"Q3 sidecar record_type is missing at {target}:{line_number}")
        rows.append(row)
    if not rows:
        raise InputError(f"Q3 sidecar is empty: {target}")
    return tuple(rows)
