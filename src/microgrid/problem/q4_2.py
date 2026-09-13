"""Approved Q4-2 entry: continuous storage with a frozen daily contract."""

from __future__ import annotations

from .contracts import CaseContext, CaseResult
from .rolling_engine import run_q4

CASE_ID = "q4_2"
MODEL_DECISION_ID = "D_MODEL_Q4_2"


def run(context: CaseContext) -> CaseResult:
    """Run the approved Q4-2 model with a frozen daily contract."""
    return run_q4(context)
