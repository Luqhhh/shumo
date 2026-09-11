from __future__ import annotations

from pathlib import Path

import pytest

from microgrid.cases import required_decisions, run_case
from microgrid.schemas import InputError, PendingDecisionError


def _write_q2_decisions(repo: Path, *, blocked_id: str | None = None) -> None:
    decisions_dir = repo / "configs"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for decision_id in required_decisions("q2"):
        blocked = decision_id == blocked_id
        confirmed_by = "" if blocked else "tester"
        confirmed_at = "" if blocked else "2026-09-11"
        lines.extend(
            [
                f"[decisions.{decision_id}]",
                f'status = "{"proposed" if blocked else "approved"}"',
                'choice = "test"',
                'rationale = "test"',
                f'confirmed_by = "{confirmed_by}"',
                f'confirmed_at = "{confirmed_at}"',
                'source = "test fixture"',
                "",
            ]
        )
    (decisions_dir / "decisions.toml").write_text("\n".join(lines), encoding="utf-8")


def test_q2_new_dependency_still_blocks_dispatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    blocked_id = "D_MPC"
    _write_q2_decisions(repo, blocked_id=blocked_id)

    with pytest.raises(PendingDecisionError) as excinfo:
        run_case("q2", repo)

    assert blocked_id.replace("_", "-") in excinfo.value.decision_ids


def test_q2_approved_dependencies_reach_explicit_input_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_q2_decisions(repo)

    with pytest.raises(InputError) as excinfo:
        run_case("q2", repo)

    assert "attachment1" in str(excinfo.value)
    assert not (repo / "outputs").exists()
