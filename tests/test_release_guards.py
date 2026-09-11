from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from microgrid.cases import CASE_IDS, run_case
from microgrid.checks import assert_release_ready, collect_blockers, load_selected_runs
from microgrid.cli import main
from microgrid.schemas import (
    InputError,
    ModelNotImplementedError,
    PendingDecisionError,
    ReleaseBlockedError,
)

DECISION_IDS = (
    "D_TIME_INTERNAL",
    "D_TIME_TEMPLATE_EXPORT",
    "D_EFF",
    "D_STATE",
    "D_INFO",
    "D_RESAMPLE",
    "D_SETTLE",
    "D_MODEL_Q1",
    "D_MODEL_Q2",
    "D_LOAD_FORECAST",
    "D_PRICE_FORECAST",
    "D_YEAR_BOUNDARY",
    "D_TERMINAL",
    "D_MPC",
    "D_MODEL_Q3",
    "D_MODEL_Q4_2",
    "D_MODEL_Q4_3",
    "D_EVAL",
)

RESULT_BY_CASE = {
    "q1": "result1.xlsx",
    "q2": "result2.xlsx",
    "q3": "result3.xlsx",
    "q4_2": "result4-2.xlsx",
    "q4_3": "result4-3.xlsx",
}


def _write_decisions(repo: Path, *, approved: bool) -> None:
    (repo / "configs").mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for decision_id in DECISION_IDS:
        lines.extend(
            [
                f"[decisions.{decision_id}]",
                f'status = "{"approved" if approved else "pending"}"',
                f'choice = "{"test choice" if approved else ""}"',
                f'rationale = "{"test rationale" if approved else ""}"',
                f'confirmed_by = "{"tester" if approved else ""}"',
                f'confirmed_at = "{"2026-09-10" if approved else ""}"',
                'source = "test fixture"',
                "",
            ]
        )
    (repo / "configs" / "decisions.toml").write_text("\n".join(lines), encoding="utf-8")


def _sha256_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_run(
    repo: Path,
    case_id: str,
    run_id: str,
    *,
    is_synthetic: bool = False,
    status: str = "success",
    create_result: bool = True,
) -> None:
    filename = RESULT_BY_CASE[case_id]
    run_dir = repo / "outputs" / "runs" / case_id / run_id
    results_dir = run_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    result_path = results_dir / filename
    if create_result:
        result_path.write_bytes(f"{case_id}:{run_id}\n".encode())
    else:
        result_path.unlink(missing_ok=True)
    manifest = {
        "run_id": run_id,
        "case_id": case_id,
        "status": status,
        "is_synthetic": is_synthetic,
        "result_files": {filename: f"results/{filename}"},
        "result_sha256": {filename: _sha256_bytes(result_path)} if create_result else {},
    }
    (run_dir / "manifest.json").write_text(
        __import__("json").dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_selected_runs(repo: Path) -> None:
    (repo / "configs").mkdir(parents=True, exist_ok=True)
    lines = [
        "schema_version = 1",
        'selection_status = "approved"',
        'note = "test selection"',
        "",
        "[runs]",
    ]
    for case_id in CASE_IDS:
        lines.append(f'{case_id} = "{case_id}-run-test"')
    (repo / "configs" / "selected_runs.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_paper_and_ai_details(repo: Path) -> None:
    (repo / "paper").mkdir(parents=True, exist_ok=True)
    (repo / "paper" / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}x\\end{document}\n", encoding="utf-8"
    )
    (repo / "dist").mkdir(parents=True, exist_ok=True)
    (repo / "dist" / "AI 工具使用详情.pdf").write_bytes(b"%PDF-1.4 fake\n")


def _ready_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _write_decisions(repo, approved=True)
    _write_selected_runs(repo)
    _write_paper_and_ai_details(repo)
    for case_id in CASE_IDS:
        _make_run(repo, case_id, f"{case_id}-run-test")
    return repo


def test_pending_decision_blocks_case(tmp_path):
    repo = tmp_path / "repo"
    _write_decisions(repo, approved=False)
    with pytest.raises(PendingDecisionError) as excinfo:
        run_case("q1", repo)
    assert "D-TIME-INTERNAL" in excinfo.value.decision_ids


def test_case_dependency_graph_splits_model_gates_and_q4_2_has_no_resample():
    from microgrid.cases import CASE_DECISIONS

    assert "D_TIME_INTERNAL" in CASE_DECISIONS["q1"]
    assert "D_MODEL_Q1" in CASE_DECISIONS["q1"]
    assert "D_MODEL_Q2" in CASE_DECISIONS["q2"]
    assert "D_MODEL_Q3" in CASE_DECISIONS["q3"]
    assert "D_MODEL_Q4_2" in CASE_DECISIONS["q4_2"]
    assert "D_MODEL_Q4_3" in CASE_DECISIONS["q4_3"]
    assert "D_MODEL" not in CASE_DECISIONS["q1"]
    assert "D_TIME_TEMPLATE_EXPORT" not in CASE_DECISIONS["q1"]
    assert "D_RESAMPLE" not in CASE_DECISIONS["q4_2"]


def test_proposed_decision_still_blocks_dispatch(tmp_path):
    repo = tmp_path / "repo"
    _write_decisions(repo, approved=False)
    path = repo / "configs" / "decisions.toml"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        '[decisions.D_TIME_INTERNAL]\nstatus = "pending"',
        '[decisions.D_TIME_INTERNAL]\nstatus = "proposed"',
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(PendingDecisionError) as excinfo:
        run_case("q1", repo)
    assert "D-TIME-INTERNAL" in excinfo.value.decision_ids
    blockers = collect_blockers(repo, mode="final")
    assert "decision D-TIME-INTERNAL is proposed" in blockers


def _write_approved_subset(repo: Path, decision_ids: tuple[str, ...]) -> None:
    (repo / "configs").mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for decision_id in decision_ids:
        lines.extend(
            [
                f"[decisions.{decision_id}]",
                'status = "approved"',
                'choice = "test"',
                'rationale = "test"',
                'confirmed_by = "tester"',
                'confirmed_at = "2026-09-10"',
                'source = "test"',
                "",
            ]
        )
    (repo / "configs" / "decisions.toml").write_text("\n".join(lines), encoding="utf-8")


def test_q1_approved_is_not_blocked_by_q3_or_export_pending(tmp_path):
    from microgrid.cases import pending_decisions, required_decisions

    repo = tmp_path / "repo"
    _write_approved_subset(repo, required_decisions("q1"))
    assert pending_decisions(repo, "q1") == []
    # Dispatch reaches the Q1 runner; missing local attachment is later than decision gate.
    with pytest.raises(InputError):
        run_case("q1", repo)


def test_approved_decisions_reach_explicit_q2_input_error(tmp_path):
    repo = tmp_path / "repo"
    _write_decisions(repo, approved=True)
    with pytest.raises(InputError, match="attachment1"):
        run_case("q2", repo)
    # Dispatcher must not invent a fake solution or success.
    assert not (repo / "outputs").exists()

def test_final_guard_has_no_permanent_stage0_blocker(tmp_path):
    repo = tmp_path / "repo"
    _write_decisions(repo, approved=False)
    _write_paper_and_ai_details(repo)
    blockers = collect_blockers(repo, mode="final")
    text = "\n".join(blockers)
    assert "model_implementation=not_implemented" not in text
    assert "formal_experiments=not_started" not in text
    assert "no selected run_id" in text
    with pytest.raises(ReleaseBlockedError):
        assert_release_ready(repo, mode="final")


def test_release_guard_can_be_satisfied_by_explicit_run_artifacts(tmp_path):
    repo = _ready_repo(tmp_path)
    selection, status = load_selected_runs(repo)
    assert status == "approved"
    assert set(selection) == set(CASE_IDS)
    assert collect_blockers(repo, mode="final") == []
    assert_release_ready(repo, mode="final")


def test_synthetic_selected_run_blocks_release(tmp_path):
    repo = _ready_repo(tmp_path)
    _make_run(repo, "q1", "q1-run-test", is_synthetic=True)
    blockers = collect_blockers(repo, mode="final")
    assert any("not explicitly non-synthetic" in blocker for blocker in blockers)


def test_missing_owned_result_file_blocks_release(tmp_path):
    repo = _ready_repo(tmp_path)
    _make_run(repo, "q2", "q2-run-test", create_result=False)
    blockers = collect_blockers(repo, mode="final")
    assert any("no owned result file result2.xlsx" in blocker for blocker in blockers)


def test_cli_run_case_returns_pending_code(tmp_path, capsys):
    repo = tmp_path / "repo"
    _write_decisions(repo, approved=False)
    code = main(["--repo", str(repo), "run", "--case", "q1"])
    assert code == 4
    captured = capsys.readouterr()
    assert "pending" in (captured.out + captured.err)
