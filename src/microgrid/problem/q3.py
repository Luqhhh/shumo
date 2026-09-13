"""Q3 formal runner entry.

The model card and decisions were approved on 2026-09-12, but the solver is
not implemented. The dispatcher checks D_LOAD_FORECAST and D_MODEL_Q3 along
with shared semantics, resampling and settlement before reaching this stub.
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
