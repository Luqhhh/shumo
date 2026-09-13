"""Approved Q4-3 entry: official PV forecasts and scheduled contract adjustments."""

from __future__ import annotations

from .contracts import CaseContext, CaseResult
from .rolling_engine import run_q4

CASE_ID = "q4_3"
MODEL_DECISION_ID = "D_MODEL_Q4_3"


def run(context: CaseContext) -> CaseResult:
    """Run the approved Q4-3 model with official PV and legal adjustments."""
    return run_q4(context)
