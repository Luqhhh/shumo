"""Shared engineering helpers for problem runners.

No objective function, storage equation, settlement rule or solver choice is
encoded here.  Those belong to approved decision contracts.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .contracts import CaseContext


def load_toml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("rb") as fh:
        return tomllib.load(fh)


def load_project_config(repo_root: str | Path) -> dict[str, Any]:
    return load_toml(Path(repo_root) / "configs" / "project.toml")


def load_constants(repo_root: str | Path) -> dict[str, Any]:
    return load_toml(Path(repo_root) / "configs" / "constants.toml")


def build_context(
    repo_root: str | Path,
    case_id: str,
    *,
    run_id: str | None = None,
) -> CaseContext:
    return CaseContext(repo_root=Path(repo_root), case_id=case_id, run_id=run_id)


def ensure_case_output_dir(context: CaseContext) -> Path:
    path = context.resolved_output_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path
