"""Formal case dispatcher.

Stage 1 semantics:
* pending required decisions  -> PendingDecisionError
* all required decisions approved -> dispatch to problem/q*.py
* runner not yet implemented -> ModelNotImplementedError

The dispatcher must never synthesize a solution when a runner is missing.
"""

from __future__ import annotations

from pathlib import Path

from .approvals import decision_issues, unapproved_decision_ids
from .problem import CASE_RUNNERS
from .problem.contracts import CaseContext, CaseResult
from .schemas import InputError, PendingDecisionError

CASE_IDS = ("q1", "q2", "q3", "q4_2", "q4_3")

# Shared physical semantics stay in D_TIME_INTERNAL / D_EFF / D_STATE / D_INFO.
# D_TIME_TEMPLATE_EXPORT is intentionally not a model-input decision: it gates
# only formal Excel export.  Each case has its own model approval gate.  Q4-2
# is the fluctuating-price replay of Q2, so it deliberately does NOT depend on
# D_RESAMPLE.
CASE_DECISIONS: dict[str, tuple[str, ...]] = {
    "q1": ("D_TIME_INTERNAL", "D_EFF", "D_STATE", "D_MODEL_Q1"),
    "q2": ("D_TIME_INTERNAL", "D_EFF", "D_STATE", "D_INFO", "D_SETTLE", "D_MODEL_Q2"),
    "q3": (
        "D_TIME_INTERNAL",
        "D_EFF",
        "D_STATE",
        "D_INFO",
        "D_LOAD_FORECAST",
        "D_PV_TAIL_BASELINE",
        "D_RESAMPLE",
        "D_SETTLE",
        "D_MODEL_Q3",
    ),
    "q4_2": ("D_TIME_INTERNAL", "D_EFF", "D_STATE", "D_INFO", "D_SETTLE", "D_MODEL_Q4_2"),
    "q4_3": (
        "D_TIME_INTERNAL",
        "D_EFF",
        "D_STATE",
        "D_INFO",
        "D_RESAMPLE",
        "D_SETTLE",
        "D_MODEL_Q4_3",
    ),
}

CASE_DESCRIPTIONS = {
    "q1": "问题1：单日计划购电模型",
    "q2": "问题2：实际负载/光伏下的多日计划与紧急购电",
    "q3": "问题3：多时刻预报与调整购电",
    "q4_2": "问题4-2：波动电价下重算问题2",
    "q4_3": "问题4-3：波动电价下重算问题3",
}


def required_decisions(case_id: str) -> tuple[str, ...]:
    if case_id not in CASE_IDS:
        raise InputError(f"unknown case {case_id!r}; expected one of {', '.join(CASE_IDS)}")
    return CASE_DECISIONS[case_id]


def pending_decisions(repo_root: str | Path, case_id: str) -> list[str]:
    return unapproved_decision_ids(repo_root, required_decisions(case_id))


def run_case(
    case_id: str,
    repo_root: str | Path = ".",
    *,
    run_id: str | None = None,
) -> CaseResult:
    """Dispatch one formal case after decision gating."""

    if case_id not in CASE_IDS:
        raise InputError(f"unknown case {case_id!r}; expected one of {', '.join(CASE_IDS)}")

    issues = decision_issues(repo_root, required_decisions(case_id))
    if issues:
        raise PendingDecisionError(
            [issue.decision_id for issue in issues],
            f"{CASE_DESCRIPTIONS[case_id]} requires fully approved human decisions: "
            + "; ".join(f"{issue.decision_id}: {issue.reason}" for issue in issues),
        )

    runner = CASE_RUNNERS.get(case_id)
    if runner is None:
        from .schemas import ModelNotImplementedError

        raise ModelNotImplementedError(case_id, f"case {case_id}: no registered runner")

    context = CaseContext(repo_root=Path(repo_root), case_id=case_id, run_id=run_id)
    return runner(context)
