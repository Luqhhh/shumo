from __future__ import annotations

from pathlib import Path

import pytest

from microgrid.approvals import (
    FINAL_REQUIRED_DECISION_IDS,
    decision_issues,
    require_approved_decisions,
    unapproved_decision_ids,
)
from microgrid.schemas import PendingDecisionError


def _write_decisions(repo: Path, lines: list[str]) -> None:
    (repo / "configs").mkdir(parents=True, exist_ok=True)
    (repo / "configs" / "decisions.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _decision_block(
    decision_id: str,
    *,
    status: str = "pending",
    confirmed_by: str | None = "",
    confirmed_at: str | None = "",
) -> list[str]:
    lines = [
        f"[decisions.{decision_id}]",
        f'status = "{status}"',
        'choice = "x"',
        'rationale = "x"',
    ]
    if confirmed_by is not None:
        lines.append(f'confirmed_by = "{confirmed_by}"')
    if confirmed_at is not None:
        lines.append(f'confirmed_at = "{confirmed_at}"')
    lines.extend(['source = "test"', ""])
    return lines


@pytest.mark.parametrize("status", ["pending", "proposed", "rejected", "unknown"])
def test_non_approved_status_blocks(tmp_path: Path, status: str) -> None:
    repo = tmp_path / "repo"
    lines = _decision_block("D_TIME_INTERNAL", status=status, confirmed_by="t", confirmed_at="2026")
    _write_decisions(repo, lines)
    issues = decision_issues(repo, ("D_TIME_INTERNAL",))
    assert len(issues) == 1
    assert issues[0].decision_id == "D-TIME-INTERNAL"
    assert status in issues[0].reason


def test_missing_required_decision_blocks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_decisions(repo, [])
    issues = decision_issues(repo, ("D_TIME_INTERNAL",))
    assert issues and issues[0].reason == "required decision is missing"


@pytest.mark.parametrize(
    ("status", "confirmed_by", "confirmed_at", "expected"),
    [
        ("approved", "", "2026", "confirmed_by"),
        ("approved", "   ", "2026", "confirmed_by"),
        ("approved", None, "2026", "confirmed_by"),
        ("approved", "tester", "", "confirmed_at"),
        ("approved", "tester", None, "confirmed_at"),
    ],
)
def test_approved_requires_non_empty_confirmation_fields(
    tmp_path: Path,
    status: str,
    confirmed_by: str | None,
    confirmed_at: str | None,
    expected: str,
) -> None:
    repo = tmp_path / "repo"
    lines = _decision_block(
        "D_TIME_INTERNAL",
        status=status,
        confirmed_by=confirmed_by,
        confirmed_at=confirmed_at,
    )
    _write_decisions(repo, lines)
    issues = decision_issues(repo, ("D_TIME_INTERNAL",))
    assert len(issues) == 1
    assert expected in issues[0].reason


def test_complete_approval_passes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    lines = _decision_block(
        "D_TIME_INTERNAL", status="approved", confirmed_by="tester", confirmed_at="2026-09-10"
    )
    _write_decisions(repo, lines)
    assert decision_issues(repo, ("D_TIME_INTERNAL",)) == []
    assert unapproved_decision_ids(repo, ("D_TIME_INTERNAL",)) == []


def test_require_approved_decisions_raises_with_reasons(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    lines = _decision_block("D_TIME_INTERNAL", status="proposed")
    _write_decisions(repo, lines)
    with pytest.raises(PendingDecisionError) as excinfo:
        require_approved_decisions(repo, ("D_TIME_INTERNAL", "D_MODEL_Q1"))
    assert "D-TIME-INTERNAL" in excinfo.value.decision_ids
    assert "D-MODEL-Q1" in excinfo.value.decision_ids
    assert "missing" in str(excinfo.value)


def test_final_required_set_is_explicit() -> None:
    assert "D_MODEL_Q1" in FINAL_REQUIRED_DECISION_IDS
    assert "D_TIME_TEMPLATE_EXPORT" in FINAL_REQUIRED_DECISION_IDS
    assert "D_TIME_INTERNAL" in FINAL_REQUIRED_DECISION_IDS
    assert "D_RESAMPLE" in FINAL_REQUIRED_DECISION_IDS
    assert "D_LOAD_FORECAST" in FINAL_REQUIRED_DECISION_IDS
    assert "D_PV_TAIL_BASELINE" in FINAL_REQUIRED_DECISION_IDS


@pytest.mark.parametrize(
    "broken", ["missing", "pending", "proposed", "confirmed_by", "confirmed_at"]
)
def test_q3_load_approval_blocks_dispatch_and_final_release(tmp_path: Path, monkeypatch, broken):
    from microgrid.cases import CASE_RUNNERS, required_decisions, run_case
    from microgrid.checks import collect_blockers

    lines = []
    for decision_id in FINAL_REQUIRED_DECISION_IDS:
        if decision_id == "D_LOAD_FORECAST" and broken == "missing":
            continue
        status, by, at = "approved", "tester", "2026-09-12"
        if decision_id == "D_LOAD_FORECAST":
            if broken in ("pending", "proposed"):
                status = broken
            elif broken == "confirmed_by":
                by = " "
            elif broken == "confirmed_at":
                at = " "
        lines.extend(_decision_block(decision_id, status=status, confirmed_by=by, confirmed_at=at))
    _write_decisions(tmp_path, lines)

    def forbidden_runner(_context):
        pytest.fail("Q3 runner must not be invoked without load approval")

    monkeypatch.setitem(CASE_RUNNERS, "q3", forbidden_runner)
    assert "D_LOAD_FORECAST" in required_decisions("q3")
    with pytest.raises(PendingDecisionError) as excinfo:
        run_case("q3", tmp_path)
    assert excinfo.value.decision_ids == ["D-LOAD-FORECAST"]
    assert any("D-LOAD-FORECAST" in blocker for blocker in collect_blockers(tmp_path, mode="final"))


def test_q3_complete_approval_reaches_unimplemented_runner(tmp_path: Path):
    from microgrid.cases import required_decisions, run_case
    from microgrid.schemas import ModelNotImplementedError

    lines = []
    for decision_id in required_decisions("q3"):
        lines.extend(
            _decision_block(
                decision_id, status="approved", confirmed_by="tester", confirmed_at="2026-09-12"
            )
        )
    _write_decisions(tmp_path, lines)
    with pytest.raises(ModelNotImplementedError):
        run_case("q3", tmp_path)
    assert not (tmp_path / "outputs").exists()


def test_q3_tail_baseline_pending_blocks_before_runner(tmp_path: Path, monkeypatch) -> None:
    from microgrid.cases import CASE_RUNNERS, required_decisions, run_case

    lines = []
    for decision_id in required_decisions("q3"):
        status = "pending" if decision_id == "D_PV_TAIL_BASELINE" else "approved"
        lines.extend(
            _decision_block(
                decision_id,
                status=status,
                confirmed_by="tester" if status == "approved" else "",
                confirmed_at="2026-09-12" if status == "approved" else "",
            )
        )
    _write_decisions(tmp_path, lines)

    def forbidden_runner(_context):
        pytest.fail("Q3 runner must not run while the tail baseline is pending")

    monkeypatch.setitem(CASE_RUNNERS, "q3", forbidden_runner)
    with pytest.raises(PendingDecisionError) as excinfo:
        run_case("q3", tmp_path)
    assert excinfo.value.decision_ids == ["D-PV-TAIL-BASELINE"]
