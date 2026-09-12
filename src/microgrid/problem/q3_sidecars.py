"""Structured Q3 sidecars accepted by the team on 2026-09-12.

The global ``CaseResult`` schema remains unchanged.  These writers only create
the three one-to-many audit artifacts; a formal runner must later register the
returned paths and hashes in its ordinary result/manifest structures.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..dataio import ensure_dir, sha256_file
from ..schemas import InputError
from .q3_load_forecast import LoadForecastPoint
from .q3_plan_ledger import Q3PlanLedger
from .q3_pv_forecast import CombinedPVForecast

Q3_SIDECAR_SCHEMA_VERSION = 1
FORECAST_PROVENANCE_FILENAME = "forecast_provenance.jsonl"
PLAN_VERSIONS_FILENAME = "plan_versions.jsonl"
SETTLEMENT_LEDGER_FILENAME = "settlement_ledger.jsonl"


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


def _iso(value: Any) -> str:
    return value.isoformat()


def forecast_provenance_rows(
    *,
    load_versions: Iterable[tuple[LoadForecastPoint, ...]],
    pv_versions: Iterable[CombinedPVForecast],
) -> tuple[dict[str, Any], ...]:
    """Normalize load and PV forecast provenance without dropping vintages."""

    rows: list[dict[str, Any]] = []
    for version in load_versions:
        for point in version:
            rows.append(
                {
                    "schema_version": Q3_SIDECAR_SCHEMA_VERSION,
                    "record_type": "load_forecast",
                    "decision_time": _iso(point.decision_time),
                    "valid_time": _iso(point.valid_time),
                    "available_at": _iso(point.available_at),
                    "training_cutoff": _iso(point.training_cutoff),
                    "prediction_power_kw": point.power_kw,
                    "prediction_energy_kwh": point.energy_kwh,
                    "raw_power_kw": point.raw_power_kw,
                    "lag_values_kw": list(point.lag_values_kw),
                    "lag_weights": list(point.lag_weights),
                    "ar1_phi": point.ar1_phi,
                    "latest_visible_residual_kw": point.latest_visible_residual_kw,
                    "was_clipped": point.was_clipped,
                    "model_version": point.model_version,
                    "data_version": point.data_version,
                    "source_refs": list(point.source_refs),
                }
            )
    for version in pv_versions:
        for point in version.points:
            contributions = tuple(
                row for row in version.contributions if row.valid_time == point.valid_time
            )
            rows.append(
                {
                    "schema_version": Q3_SIDECAR_SCHEMA_VERSION,
                    "record_type": "pv_combined_forecast",
                    "decision_time": _iso(version.decision_time),
                    "valid_time": _iso(point.valid_time),
                    "prediction_power_kw": point.power_kw,
                    "epsilon_kw": version.epsilon_kw,
                    "model_version": version.model_version,
                    "source_ref": point.source_ref,
                    "contributions": [
                        {
                            "issue_time": _iso(row.issue_time),
                            "valid_time": _iso(row.valid_time),
                            "lead_hours": row.lead_hours,
                            "forecast_power_kw": row.forecast_power_kw,
                            "historical_mae_kw": row.historical_mae_kw,
                            "history_count": row.history_count,
                            "raw_weight": row.raw_weight,
                            "normalized_weight": row.normalized_weight,
                            "source_ref": row.source_ref,
                        }
                        for row in contributions
                    ],
                }
            )
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                str(row["decision_time"]),
                str(row["record_type"]),
                str(row["valid_time"]),
            ),
        )
    )


def plan_version_rows(ledgers: Iterable[Q3PlanLedger]) -> tuple[dict[str, Any], ...]:
    """Return one row per immutable plan version and target slot."""

    rows: list[dict[str, Any]] = []
    for ledger in sorted(ledgers, key=lambda item: item.day):
        for version in ledger.versions:
            for slot, quantity in enumerate(version.committed_kwh):
                rows.append(
                    {
                        "schema_version": Q3_SIDECAR_SCHEMA_VERSION,
                        "record_type": "plan_version_slot",
                        "day": version.day.isoformat(),
                        "version": version.version,
                        "issue_time": _iso(version.issue_time),
                        "target_slot": slot,
                        "committed_kwh": quantity,
                    }
                )
    return tuple(rows)


def settlement_rows(ledgers: Iterable[Q3PlanLedger]) -> tuple[dict[str, Any], ...]:
    """Return initial-plan and non-netted adjustment events.

    Emergency execution events will be added by the replay layer after its
    still-pending physical recourse wording is transcribed into the machine
    decision.  This function must not invent those actions.
    """

    rows: list[dict[str, Any]] = []
    for ledger in sorted(ledgers, key=lambda item: item.day):
        initial = ledger.versions[0]
        for slot, (quantity, price) in enumerate(
            zip(initial.committed_kwh, ledger.base_prices_cny_per_kwh, strict=True)
        ):
            rows.append(
                {
                    "schema_version": Q3_SIDECAR_SCHEMA_VERSION,
                    "record_type": "planned_purchase",
                    "day": ledger.day.isoformat(),
                    "issue_time": _iso(initial.issue_time),
                    "target_slot": slot,
                    "quantity_kwh": quantity,
                    "price_cny_per_kwh": price,
                    "cost_cny": quantity * price,
                }
            )
        for entry in ledger.adjustment_entries:
            rows.append(
                {
                    "schema_version": Q3_SIDECAR_SCHEMA_VERSION,
                    "record_type": "purchase_adjustment",
                    "day": ledger.day.isoformat(),
                    "issue_time": _iso(entry.issue_time),
                    "target_slot": entry.target_slot,
                    "previous_committed_kwh": entry.previous_committed_kwh,
                    "new_committed_kwh": entry.new_committed_kwh,
                    "delta_plus_kwh": entry.delta_plus_kwh,
                    "delta_minus_kwh": entry.delta_minus_kwh,
                    "price_cny_per_kwh": entry.transaction_price_cny_per_kwh,
                    "cost_cny": entry.adjustment_cost_cny,
                    "is_frozen": entry.is_frozen,
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
    pv_versions: Iterable[CombinedPVForecast],
    plan_ledgers: Iterable[Q3PlanLedger],
    overwrite: bool = False,
) -> Q3SidecarSet:
    """Write each of the three accepted Q3 sidecars atomically."""

    directory = Path(run_dir)
    load_versions_tuple = tuple(load_versions)
    pv_versions_tuple = tuple(pv_versions)
    ledgers_tuple = tuple(plan_ledgers)
    if not load_versions_tuple or not pv_versions_tuple or not ledgers_tuple:
        raise InputError("Q3 formal sidecars require load, PV and plan records")
    ledger_days = tuple(ledger.day for ledger in ledgers_tuple)
    if len(ledger_days) != len(set(ledger_days)):
        raise InputError("Q3 plan sidecars contain duplicate daily ledgers")
    targets = (
        directory / FORECAST_PROVENANCE_FILENAME,
        directory / PLAN_VERSIONS_FILENAME,
        directory / SETTLEMENT_LEDGER_FILENAME,
    )
    if not overwrite:
        existing = tuple(path for path in targets if path.exists())
        if existing:
            raise InputError(f"Q3 sidecar already exists: {existing[0]}")
    forecasts = forecast_provenance_rows(
        load_versions=load_versions_tuple,
        pv_versions=pv_versions_tuple,
    )
    plans = plan_version_rows(ledgers_tuple)
    settlements = settlement_rows(ledgers_tuple)
    return Q3SidecarSet(
        forecast_provenance=_write_jsonl(
            targets[0],
            forecasts,
            overwrite=overwrite,
        ),
        plan_versions=_write_jsonl(
            targets[1],
            plans,
            overwrite=overwrite,
        ),
        settlement_ledger=_write_jsonl(
            targets[2],
            settlements,
            overwrite=overwrite,
        ),
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
