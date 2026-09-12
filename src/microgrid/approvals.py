"""Single shared approval gate for decisions.

Every dispatcher, export and release path must use these helpers instead of
re-implementing status/confirm-field checks.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .schemas import PendingDecisionError

APPROVED_STATUS = "approved"

SHARED_DECISION_IDS = (
    "D_TIME_INTERNAL",
    "D_EFF",
    "D_STATE",
    "D_INFO",
)
MODEL_DECISION_IDS = (
    "D_MODEL_Q1",
    "D_MODEL_Q2",
    "D_MODEL_Q3",
    "D_MODEL_Q4_2",
    "D_MODEL_Q4_3",
)
DECISION_CASE_SCOPES = {
    "D_MODEL_Q4_2": ("q4_2",),
    "D_MODEL_Q4_3": ("q4_3",),
    "D_TIME_TEMPLATE_EXPORT_Q4": ("q4_2", "q4_3"),
    "D_EVAL_Q4": ("q4_2", "q4_3"),
    "D_TERMINAL_RESERVE_Q4": ("q4_2", "q4_3"),
}
FINAL_REQUIRED_DECISION_IDS = (
    *SHARED_DECISION_IDS,
    "D_TIME_TEMPLATE_EXPORT",
    "D_TIME_TEMPLATE_EXPORT_Q4",
    "D_RESAMPLE",
    "D_LOAD_FORECAST",
    "D_SETTLE",
    *MODEL_DECISION_IDS,
    "D_EVAL",
    "D_EVAL_Q4",
    "D_TERMINAL_RESERVE_Q4",
)


def template_export_decision_id(case_id: str) -> str:
    return "D_TIME_TEMPLATE_EXPORT_Q4" if case_id in ("q4_2", "q4_3") else "D_TIME_TEMPLATE_EXPORT"


@dataclass(frozen=True)
class DecisionIssue:
    decision_id: str
    reason: str
    status: str | None = None


def normalize_decision_id(decision_id: str) -> str:
    return decision_id.strip().replace("_", "-")


def load_decisions(repo_root: str | Path) -> dict[str, dict[str, object]]:
    path = Path(repo_root) / "configs" / "decisions.toml"
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {
        normalize_decision_id(key): value
        for key, value in data.get("decisions", {}).items()
        if isinstance(value, dict)
    }


def decision_issues(
    repo_root: str | Path, required_decision_ids: tuple[str, ...] | list[str]
) -> list[DecisionIssue]:
    """Return one issue per required decision that is not fully approved.

    Missing entries block as hard as pending ones.  `confirmed_by` and
    `confirmed_at` must be non-empty strings; ``None`` and whitespace do not
    count as human confirmation. Q4 decisions must also declare their exact
    case scope; a Q3 prediction or Q1 mapping approval cannot stand in for it.
    """

    decisions = load_decisions(repo_root)
    issues: list[DecisionIssue] = []
    for required in required_decision_ids:
        normalized = normalize_decision_id(required)
        record = decisions.get(normalized)
        if record is None:
            issues.append(DecisionIssue(normalized, "required decision is missing"))
            continue
        status = str(record.get("status", "pending"))
        if status != APPROVED_STATUS:
            issues.append(
                DecisionIssue(normalized, f"required decision status is {status!r}", status=status)
            )
            continue
        for field in ("confirmed_by", "confirmed_at"):
            value = record.get(field)
            if value is None or not str(value).strip():
                issues.append(
                    DecisionIssue(
                        normalized,
                        f"approved decision has empty {field}",
                        status=status,
                    )
                )
        expected_scope = DECISION_CASE_SCOPES.get(normalized.replace("-", "_"))
        if expected_scope is not None:
            scope = record.get("scope_cases")
            if (
                not isinstance(scope, list)
                or not all(isinstance(case, str) for case in scope)
                or len(scope) != len(expected_scope)
                or set(scope) != set(expected_scope)
            ):
                issues.append(
                    DecisionIssue(
                        normalized,
                        f"approved decision scope_cases must be {list(expected_scope)!r}",
                        status=status,
                    )
                )
    return issues


def unapproved_decision_ids(
    repo_root: str | Path, required_decision_ids: tuple[str, ...] | list[str]
) -> list[str]:
    return [issue.decision_id for issue in decision_issues(repo_root, required_decision_ids)]


def require_approved_decisions(
    repo_root: str | Path, required_decision_ids: tuple[str, ...] | list[str]
) -> None:
    issues = decision_issues(repo_root, required_decision_ids)
    if issues:
        message = "; ".join(f"{issue.decision_id}: {issue.reason}" for issue in issues)
        raise PendingDecisionError([issue.decision_id for issue in issues], message)
