"""Formal case entries.

Stage 0 intentionally does not implement any model.  Every formal case must
fail loudly, either because of a pending human decision or because the model
entry point is not implemented.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .schemas import ModelNotImplementedError, PendingDecisionError

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


def pending_decisions(repo_root: str | Path, case_id: str) -> list[str]:
    required = CASE_DECISIONS.get(case_id, ("D_MODEL",))
    statuses = decision_statuses(repo_root)
    pending = []
    for key in required:
        normalized = key.replace("_", "-")
        if statuses.get(normalized, "pending") != "approved":
            pending.append(normalized)
    return pending


def run_case(case_id: str, repo_root: str | Path = ".") -> None:
    """Always fail in Stage 0; never returns a fake solution."""

    if case_id not in CASE_IDS:
        from .schemas import InputError

        raise InputError(f"unknown case {case_id!r}; expected one of {', '.join(CASE_IDS)}")
    pending = pending_decisions(repo_root, case_id)
    if pending:
        raise PendingDecisionError(
            pending,
            f"{CASE_DESCRIPTIONS[case_id]} requires human decisions before implementation",
        )
    raise ModelNotImplementedError(case_id)
