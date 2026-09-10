"""Formal case dispatcher.

Stage 1 semantics:
* pending required decisions  -> PendingDecisionError
* all required decisions approved -> dispatch to problem/q*.py
* runner not yet implemented -> ModelNotImplementedError

The dispatcher must never synthesize a solution when a runner is missing.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .problem import CASE_RUNNERS
from .problem.contracts import CaseContext, CaseResult
from .schemas import InputError, PendingDecisionError

CASE_IDS = ("q1", "q2", "q3", "q4_2", "q4_3")

CASE_DECISIONS: dict[str, tuple[str, ...]] = {
    "q1": ("D_TIME", "D_EFF", "D_STATE", "D_MODEL"),
    "q2": ("D_TIME", "D_EFF", "D_STATE", "D_INFO", "D_SETTLE", "D_MODEL"),
    "q3": ("D_TIME", "D_EFF", "D_STATE", "D_INFO", "D_RESAMPLE", "D_SETTLE", "D_MODEL"),
    "q4_2": ("D_TIME", "D_EFF", "D_STATE", "D_INFO", "D_RESAMPLE", "D_SETTLE", "D_MODEL"),
    "q4_3": ("D_TIME", "D_EFF", "D_STATE", "D_INFO", "D_RESAMPLE", "D_SETTLE", "D_MODEL"),
}

CASE_DESCRIPTIONS = {
    "q1": "问题1：单日计划购电模型",
    "q2": "问题2：实际负载/光伏下的多日计划与紧急购电",
    "q3": "问题3：多时刻预报与调整购电",
    "q4_2": "问题4-2：波动电价下重算问题2",
    "q4_3": "问题4-3：波动电价下重算问题3",
}


def decision_statuses(repo_root: str | Path) -> dict[str, str]:
    path = Path(repo_root) / "configs" / "decisions.toml"
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    statuses: dict[str, str] = {}
    for key, value in data.get("decisions", {}).items():
        statuses[key.replace("_", "-")] = str(value.get("status", "pending"))
    return statuses


def required_decisions(case_id: str) -> tuple[str, ...]:
    if case_id not in CASE_IDS:
        raise InputError(f"unknown case {case_id!r}; expected one of {', '.join(CASE_IDS)}")
    return CASE_DECISIONS[case_id]


def pending_decisions(repo_root: str | Path, case_id: str) -> list[str]:
    required = required_decisions(case_id)
    statuses = decision_statuses(repo_root)
    pending = []
    for key in required:
        normalized = key.replace("_", "-")
        if statuses.get(normalized, "pending") != "approved":
            pending.append(normalized)
    return pending


def run_case(
    case_id: str,
    repo_root: str | Path = ".",
    *,
    run_id: str | None = None,
) -> CaseResult:
    """Dispatch one formal case after decision gating."""

    if case_id not in CASE_IDS:
        raise InputError(f"unknown case {case_id!r}; expected one of {', '.join(CASE_IDS)}")

    pending = pending_decisions(repo_root, case_id)
    if pending:
        raise PendingDecisionError(
            pending,
            f"{CASE_DESCRIPTIONS[case_id]} requires human decisions before implementation",
        )

    runner = CASE_RUNNERS.get(case_id)
    if runner is None:
        from .schemas import ModelNotImplementedError

        raise ModelNotImplementedError(case_id, f"case {case_id}: no registered runner")

    context = CaseContext(repo_root=Path(repo_root), case_id=case_id, run_id=run_id)
    return runner(context)
