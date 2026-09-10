"""Contracts shared by the Stage 1 problem runners.

These contracts are deliberately small and contain no model assumptions.
A runner may only be implemented after its required decisions are approved in
``configs/decisions.toml``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class CaseContext:
    """Immutable inputs available to one formal case runner."""

    repo_root: Path
    case_id: str
    run_id: str | None = None
    output_dir: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def resolved_output_dir(self) -> Path:
        if self.output_dir is not None:
            return self.output_dir
        run_part = self.run_id or "unassigned"
        return self.repo_root / "outputs" / "runs" / self.case_id / run_part


@dataclass(frozen=True)
class CaseResult:
    """Formal case result metadata.

    The actual result files are kept as paths so the release guard can verify
    the selected run manifest and files independently of this object.
    """

    case_id: str
    run_id: str
    status: str
    is_synthetic: bool = False
    result_files: tuple[Path, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class CaseRunner(Protocol):
    """Callable signature for a problem runner entry point."""

    def __call__(self, context: CaseContext) -> CaseResult: ...
