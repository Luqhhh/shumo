"""Q2 formal runner with an approval-gated engineering entry point.

The optimization engine is added in later tasks. This entry point first
enforces every Q2-specific decision dependency and rejects missing explicit
inputs instead of silently falling back to synthetic data.
"""

from __future__ import annotations

from pathlib import Path

from ..approvals import require_approved_decisions
from ..schemas import InputError, ModelNotImplementedError
from .contracts import CaseContext, CaseResult

CASE_ID = "q2"
MODEL_DECISION_ID = "D_MODEL_Q2"
Q2_DECISION_IDS = (
    "D_TIME_INTERNAL",
    "D_EFF",
    "D_STATE",
    "D_INFO",
    "D_SETTLE",
    "D_MODEL_Q2",
    "D_MPC",
    "D_TERMINAL",
    "D_YEAR_BOUNDARY",
)


def _input_path(context: CaseContext, key: str, default: Path) -> Path:
    raw = context.metadata.get(key)
    return Path(raw) if raw is not None else default


def run(context: CaseContext) -> CaseResult:
    """Enforce Q2 gates and explicit input paths before model execution."""

    require_approved_decisions(context.repo_root, Q2_DECISION_IDS)
    attachment1 = _input_path(
        context,
        "attachment1_path",
        context.repo_root / "data" / "raw" / "附件1.xlsx",
    )
    attachment2 = _input_path(
        context,
        "attachment2_path",
        context.repo_root / "data" / "raw" / "附件2.xlsx",
    )
    for logical_name, path in (("attachment1", attachment1), ("attachment2", attachment2)):
        if not path.is_file():
            raise InputError(f"{logical_name} path is not a file: {path}")
    raise ModelNotImplementedError(
        context.case_id,
        f"case {context.case_id}: optimization engine is not wired yet",
    )
