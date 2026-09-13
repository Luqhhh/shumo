"""Assemble one validated Q3 day into sidecars and shared CaseResult v1."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..schemas import InputError
from .contracts import CaseResult
from .q3_load_snapshot import LoadForecastSnapshot
from .q3_pv_snapshot import PVForecastSnapshot, TailForecast
from .q3_rolling import Q3DayRun
from .q3_sidecars import Q3SidecarSet, write_q3_sidecars
from .result_io import load_case_result, save_case_result
from .validation import validate_complete_run

DOMAIN_RESULT_FILENAME = "domain_result.json"


@dataclass(frozen=True)
class Q3DayArtifacts:
    result: CaseResult
    domain_result_path: Path
    sidecars: Q3SidecarSet

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.domain_result_path, *self.sidecars.paths)


def write_q3_day_artifacts(
    run_dir: str | Path,
    *,
    run_id: str,
    day_run: Q3DayRun,
    load_snapshots: tuple[LoadForecastSnapshot, ...],
    pv_snapshots: tuple[PVForecastSnapshot, ...],
    tail_forecasts: tuple[TailForecast, ...] = (),
    is_synthetic: bool,
    overwrite: bool = False,
) -> Q3DayArtifacts:
    """Write all Q3 audit records without changing global result schema v1."""

    if not run_id.strip():
        raise InputError("Q3 artifact run_id must be non-empty")
    directory = Path(run_dir)
    domain_path = directory / DOMAIN_RESULT_FILENAME
    if domain_path.exists() and not overwrite:
        raise InputError(f"result file already exists: {domain_path}")

    sidecars = write_q3_sidecars(
        directory,
        load_versions=tuple(snapshot.points for snapshot in load_snapshots),
        pv_snapshots=pv_snapshots,
        plan_ledgers=(day_run.ledger,),
        forecast_links=day_run.forecast_links,
        tail_forecasts=tail_forecasts,
        emergency_entries=day_run.emergency_entries,
        overwrite=overwrite,
    )
    sidecar_metadata = sidecars.metadata(relative_to=directory)
    relative_sidecars = tuple(path.relative_to(directory) for path in sidecars.paths)
    costs = day_run.cost_breakdown
    result = CaseResult(
        case_id="q3",
        run_id=run_id,
        status="success",
        is_synthetic=is_synthetic,
        result_files=(Path(DOMAIN_RESULT_FILENAME), *relative_sidecars),
        intervals=day_run.intervals,
        metadata={
            "model": "q3_rolling_milp",
            "model_status": "implemented_one_day",
            "day": day_run.day.isoformat(),
            "state_start_kwh": day_run.state_start.energy_kwh,
            "state_end_kwh": day_run.state_end.energy_kwh,
            "planned_cost_cny": costs.planned_cost_cny,
            "adjustment_cost_cny": costs.adjustment_cost_cny,
            "emergency_cost_cny": costs.emergency_cost_cny,
            "total_cost_cny": costs.total_cost_cny,
            "sidecars": sidecar_metadata,
        },
    )
    validation = validate_complete_run(result.intervals, (day_run.day,))
    if not validation.ok:
        raise InputError("Q3 day result validation failed: " + "; ".join(validation.issues))
    save_case_result(domain_path, result, overwrite=overwrite)
    loaded = load_case_result(domain_path, expected_days=(day_run.day,))
    if loaded != result:
        raise InputError("Q3 saved CaseResult did not round-trip exactly")
    return Q3DayArtifacts(
        result=result,
        domain_result_path=domain_path,
        sidecars=sidecars,
    )
