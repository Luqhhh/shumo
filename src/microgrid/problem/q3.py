"""Q3 formal runner entry.

Stage 1 dispatcher target.  The model is intentionally not implemented here:
its objective, variables, constraints, settlement rules and solver require
human-approved decisions (this case gate: D_MODEL_Q3, plus shared D_TIME_INTERNAL/D_EFF/D_STATE/D_INFO as applicable).
"""

from __future__ import annotations

from ..schemas import ModelNotImplementedError
from .contracts import CaseContext, CaseResult

CASE_ID = "q3"
MODEL_DECISION_ID = "D_MODEL_Q3"


def run(context: CaseContext) -> CaseResult:
    """Fail loudly until the approved model implementation is supplied."""

    raise ModelNotImplementedError(
        context.case_id,
        f"case {context.case_id}: runner exists but its approved model is not implemented yet",
    )
