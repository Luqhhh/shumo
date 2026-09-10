"""Release guards and lightweight content checks.

These checks are intentionally conservative.  Passing them is not evidence
that a model, a settlement formula or a conclusion is correct.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from .schemas import ReleaseBlockedError

REQUIRED_CASES = ("q1", "q2", "q3", "q4_2", "q4_3")
REQUIRED_RESULT_FILES = (
    "result1.xlsx",
    "result2.xlsx",
    "result3.xlsx",
    "result4-2.xlsx",
    "result4-3.xlsx",
)
OFFICIAL_AI_DETAILS_NAME = "AI 工具使用详情.pdf"
AI_DETAILS_ALIASES = ("AI 工具使用详情.pdf", "AI工具使用详情.pdf")
PAPER_PLACEHOLDER_MARKERS = (
    "\\PH{",
    "TODO",
    "TBD",
    "待计算",
    "待确认",
    "占位",
    "placeholder",
)


def load_decisions(repo_root: str | Path) -> dict[str, dict[str, Any]]:
    path = Path(repo_root) / "configs" / "decisions.toml"
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {key.replace("_", "-"): value for key, value in data.get("decisions", {}).items()}


def pending_decision_ids(repo_root: str | Path) -> list[str]:
    return [
        key
        for key, value in load_decisions(repo_root).items()
        if str(value.get("status", "pending")) != "approved"
    ]


def paper_placeholder_hits(repo_root: str | Path) -> list[str]:
    paper = Path(repo_root) / "paper"
    hits: list[str] = []
    for path in sorted(paper.rglob("*.tex")):
        if "generated" in path.parts or "build" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in PAPER_PLACEHOLDER_MARKERS:
            if marker in text:
                hits.append(f"{path.relative_to(repo_root)}: {marker}")
    return hits


def collect_blockers(repo_root: str | Path, *, mode: str = "final") -> list[str]:
    repo = Path(repo_root)
    blockers: list[str] = []

    decisions = load_decisions(repo)
    if not decisions:
        blockers.append("decisions.toml is missing or empty")
    for key, value in decisions.items():
        if str(value.get("status", "pending")) != "approved":
            blockers.append(f"decision {key} is {value.get('status', 'pending')}")
        if str(value.get("status", "pending")) == "approved":
            if not str(value.get("confirmed_by", "")).strip():
                blockers.append(f"decision {key} approved without confirmed_by")
            if not str(value.get("confirmed_at", "")).strip():
                blockers.append(f"decision {key} approved without confirmed_at")

    blockers.append("model_implementation=not_implemented (Stage 0 hard stop)")
    blockers.append("formal_experiments=not_started")

    results_dir = repo / "dist"
    for filename in REQUIRED_RESULT_FILES:
        matches = list(results_dir.rglob(filename))
        if not matches:
            blockers.append(f"missing formal result file: {filename}")

    if mode == "final":
        hits = paper_placeholder_hits(repo)
        blockers.extend(f"paper still contains placeholder: {hit}" for hit in hits)
        ai_files = [
            p for p in results_dir.rglob("*") if p.is_file() and p.name in AI_DETAILS_ALIASES
        ]
        if not ai_files:
            blockers.append(f"missing AI details PDF: {OFFICIAL_AI_DETAILS_NAME}")

    if not (repo / "paper" / "main.tex").exists():
        blockers.append("paper/main.tex is missing")

    # Synthetic artifacts must never be selected as formal results.
    for path in results_dir.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if data.get("is_synthetic") is True:
            blockers.append(f"synthetic artifact found under dist/: {path.name}")

    return blockers


def assert_release_ready(repo_root: str | Path, *, mode: str = "final") -> None:
    blockers = collect_blockers(repo_root, mode=mode)
    if blockers:
        raise ReleaseBlockedError(blockers)
