"""Small, documented data records and exception types.

No domain framework: plain dataclasses keep the Stage 0 interfaces inspectable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


class MicrogridError(Exception):
    """Base class for expected, user-facing command failures."""

    exit_code = 1


class InputError(MicrogridError):
    """Invalid or missing input."""

    exit_code = 2


class TimeLabelError(InputError):
    """A time/date label could not be parsed losslessly."""


class VersionConflictError(InputError):
    """Same logical input path was observed with a different hash."""


class PendingDecisionError(MicrogridError):
    """A model/run/export command depends on an unapproved decision."""

    exit_code = 4

    def __init__(self, decision_ids: list[str], message: str | None = None) -> None:
        self.decision_ids = decision_ids
        super().__init__(message or f"pending decisions: {', '.join(decision_ids)}")


class ModelNotImplementedError(MicrogridError):
    """The formal model entry point has intentionally not been implemented."""

    exit_code = 3

    def __init__(self, case_id: str, message: str | None = None) -> None:
        self.case_id = case_id
        super().__init__(message or f"case {case_id}: model is not implemented in Stage 0")


class ReleaseBlockedError(MicrogridError):
    """Formal release was blocked by Stage 0 guards."""

    exit_code = 5

    def __init__(self, blockers: list[str], message: str | None = None) -> None:
        self.blockers = blockers
        super().__init__(message or "release blocked:\n- " + "\n- ".join(blockers))


@dataclass(frozen=True)
class ObservationRecord:
    """A lossless single-cell observation.  Never infers an interval meaning."""

    source_file: str
    sheet_name: str
    cell_ref: str
    source_hash: str
    raw_time_label: str
    value: float | int | str | None
    unit: str | None = None
    kind: str | None = None
    source_date: str | None = None
    parsed_day_offset: int | None = None
    parsed_minute_of_day: int | None = None
    parsed_timestamp: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if isinstance(self.parsed_timestamp, datetime):
            data["parsed_timestamp"] = self.parsed_timestamp.isoformat()
        return data


@dataclass(frozen=True)
class ForecastRecord:
    """One raw forecast cell: issue_time + lead_hours -> valid_time."""

    issue_time: datetime
    valid_time: datetime
    lead_hours: int
    pv_forecast_kw: float
    source_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_time": self.issue_time.isoformat(sep=" "),
            "valid_time": self.valid_time.isoformat(sep=" "),
            "lead_hours": self.lead_hours,
            "pv_forecast_kw": self.pv_forecast_kw,
            "source_ref": self.source_ref,
        }


@dataclass
class RunManifest:
    """Deterministic run metadata for smoke / future formal runs."""

    schema_version: int = 1
    run_id: str = ""
    case_id: str = ""
    command: list[str] = field(default_factory=list)
    input_hashes: dict[str, str] = field(default_factory=dict)
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    code_commit: str | None = None
    code_dirty: bool | None = None
    source_hash: str = ""
    environment: dict[str, Any] = field(default_factory=dict)
    random_seed: int | None = None
    created_at: str = ""
    status: str = "not_run"
    is_synthetic: bool = False
    model_status: str = "unknown"
    result_files: dict[str, str] = field(default_factory=dict)
    result_sha256: dict[str, str] = field(default_factory=dict)
    input_verification_issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IntervalLabel:
    """Literal parsing of a result-template interval label.

    `suspicious` is evidence of a literal inconsistency.  Callers must not
    silently repair the label.
    """

    raw_label: str
    start_day_offset: int
    start_minute_of_day: int
    end_day_offset: int
    end_minute_of_day: int
    span_minutes: int
    suspicious: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
