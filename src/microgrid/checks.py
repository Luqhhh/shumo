"""Release guards driven by explicit formal-run artifacts.

Stage 1 replaces the old permanent "model not implemented" blockers with real
state checks:

selection -> manifest -> status -> synthetic flag -> result file ownership.

Pending decisions and paper placeholders remain blockers where appropriate.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

from .approvals import (
    FINAL_REQUIRED_DECISION_IDS,
    DecisionIssue,
    decision_issues,
    load_decisions,
    normalize_decision_id,
    template_export_decision_id,
)
from .schemas import ReleaseBlockedError

REQUIRED_CASES = ("q1", "q2", "q3", "q4_2", "q4_3")

RESULT_FILE_BY_CASE = {
    "q1": "result1.xlsx",
    "q2": "result2.xlsx",
    "q3": "result3.xlsx",
    "q4_2": "result4-2.xlsx",
    "q4_3": "result4-3.xlsx",
}

REQUIRED_RESULT_FILES = tuple(RESULT_FILE_BY_CASE.values())

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


def pending_decision_ids(repo_root: str | Path) -> list[str]:
    return [issue.decision_id for issue in decision_issues(repo_root, FINAL_REQUIRED_DECISION_IDS)]


def _format_decision_issue(issue: DecisionIssue) -> str:
    if issue.status is not None and issue.status != "approved":
        return f"decision {issue.decision_id} is {issue.status}"
    return f"decision {issue.decision_id}: {issue.reason}"


def load_selected_runs(repo_root: str | Path) -> tuple[dict[str, str], str]:
    path = Path(repo_root) / "configs" / "selected_runs.toml"
    if not path.exists():
        return {}, "missing"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    runs = data.get("runs", {})
    selection = {str(case): str(run_id) for case, run_id in runs.items() if str(run_id).strip()}
    return selection, str(data.get("selection_status", "pending"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _within_run_dir(path: Path, run_dir: Path) -> bool:
    try:
        path.resolve().relative_to(run_dir.resolve())
    except ValueError:
        return False
    return True


def _candidate_paths(manifest: dict[str, Any], run_dir: Path, filename: str) -> list[Path]:
    candidates: list[Path] = []
    raw = manifest.get("result_files")
    if isinstance(raw, dict):
        iterable = raw.values()
    elif isinstance(raw, list):
        iterable = raw
    else:
        iterable = []
    for item in iterable:
        if isinstance(item, str):
            candidates.append(Path(item))
        elif isinstance(item, dict):
            value = item.get("path") or item.get("name")
            if isinstance(value, str):
                candidates.append(Path(value))
    candidates.extend([Path("results") / filename, Path(filename)])
    result: list[Path] = []
    for candidate in candidates:
        path = candidate if candidate.is_absolute() else run_dir / candidate
        if path.name == filename:
            result.append(path)
    return result


def _selected_result_path(manifest: dict[str, Any], run_dir: Path, filename: str) -> Path | None:
    for candidate in _candidate_paths(manifest, run_dir, filename):
        if _within_run_dir(candidate, run_dir) and candidate.is_file():
            return candidate
    return None


def _check_run_evidence(
    repo: Path,
    run_dir: Path,
    case_id: str,
    run_id: str,
    manifest: dict[str, Any],
    filename: str,
    result_path: Path | None,
) -> list[str]:
    blockers: list[str] = []
    if case_id in ("q4_2", "q4_3"):
        from .problem.q4_evidence import check_inventory, check_model_binding

        blockers.extend(f"case {case_id}: {issue}" for issue in check_model_binding(repo, run_dir))

    validation_path = run_dir / "validation.json"
    if not validation_path.is_file():
        blockers.append(f"case {case_id}: selected run has no validation.json")
    else:
        try:
            validation = json.loads(validation_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            blockers.append(f"case {case_id}: validation.json is invalid: {exc}")
        else:
            if validation.get("ok") is not True:
                blockers.append(f"case {case_id}: validation.json does not report ok=true")
            violations = validation.get("violations")
            if isinstance(violations, list) and violations:
                blockers.append(f"case {case_id}: validation.json still contains violations")

    domain_path = run_dir / "domain_result.json"
    domain: dict[str, Any] | None = None
    if not domain_path.is_file():
        blockers.append(f"case {case_id}: selected run has no domain_result.json")
    else:
        try:
            domain = json.loads(domain_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            blockers.append(f"case {case_id}: domain_result.json is invalid: {exc}")
        else:
            if domain.get("case_id") != case_id or domain.get("run_id") != run_id:
                blockers.append(f"case {case_id}: domain_result identity mismatch")
            if domain.get("status") != "success" or domain.get("is_synthetic") is not False:
                blockers.append(
                    f"case {case_id}: domain_result is not a successful non-synthetic result"
                )

    export_path = run_dir / "export_manifest.json"
    if not export_path.is_file():
        blockers.append(f"case {case_id}: selected run has no export_manifest.json")
    else:
        try:
            export_manifest = json.loads(export_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            blockers.append(f"case {case_id}: export_manifest.json is invalid: {exc}")
        else:
            if case_id in ("q4_2", "q4_3"):
                blockers.extend(
                    f"case {case_id}: {issue}"
                    for issue in check_model_binding(repo, run_dir, export_manifest)
                    + check_inventory(repo, run_dir, export=export_manifest)
                )
            if export_manifest.get("case_id") != case_id or export_manifest.get("run_id") != run_id:
                blockers.append(f"case {case_id}: export_manifest identity mismatch")
            if domain_path.is_file() and export_manifest.get("source_result_sha256") != _sha256(
                domain_path
            ):
                blockers.append(f"case {case_id}: export_manifest source_result_sha256 mismatch")
            if result_path is not None and export_manifest.get("output_sha256") != _sha256(
                result_path
            ):
                blockers.append(f"case {case_id}: export_manifest output_sha256 mismatch")
            template_file = export_manifest.get("template_file")
            if not isinstance(template_file, str) or not template_file:
                blockers.append(f"case {case_id}: export_manifest has no template_file")
            else:
                template_path = repo / template_file
                if not template_path.is_file():
                    blockers.append(
                        f"case {case_id}: exported template file is missing: {template_file}"
                    )
                elif export_manifest.get("template_sha256") != _sha256(template_path):
                    blockers.append(f"case {case_id}: export_manifest template_sha256 mismatch")
            decision_id = template_export_decision_id(case_id)
            current_decision = load_decisions(repo).get(normalize_decision_id(decision_id), {})
            snapshot = export_manifest.get("decision_snapshot", {}).get(decision_id)
            if not isinstance(snapshot, dict):
                blockers.append(f"case {case_id}: export_manifest has no export decision snapshot")
            else:
                fields = ("status", "choice", "confirmed_by", "confirmed_at")
                if case_id in ("q4_2", "q4_3"):
                    fields += ("scope_cases",)
                for field in fields:
                    if str(snapshot.get(field)) != str(current_decision.get(field)):
                        blockers.append(
                            f"case {case_id}: export decision snapshot differs from current "
                            f"{decision_id}.{field}"
                        )
    return blockers


def check_selected_run(repo_root: str | Path, case_id: str, run_id: str) -> list[str]:
    repo = Path(repo_root)
    run_dir = repo / "outputs" / "runs" / case_id / run_id
    manifest_path = run_dir / "manifest.json"
    blockers: list[str] = []
    if not manifest_path.is_file():
        return [
            f"case {case_id}: selected run manifest not found: {manifest_path.relative_to(repo)}"
        ]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"case {case_id}: selected run manifest is not valid JSON: {exc}"]

    if str(manifest.get("run_id", "")) != run_id:
        blockers.append(f"case {case_id}: manifest run_id does not match selection ({run_id})")
    if str(manifest.get("case_id", "")) != case_id:
        blockers.append(f"case {case_id}: manifest case_id mismatch ({manifest.get('case_id')!r})")
    if str(manifest.get("status", "")) != "success":
        blockers.append(f"case {case_id}: selected run status is not success")
    if manifest.get("is_synthetic") is not False:
        blockers.append(f"case {case_id}: selected run is not explicitly non-synthetic")

    filename = RESULT_FILE_BY_CASE[case_id]
    result_path = _selected_result_path(manifest, run_dir, filename)
    if result_path is None:
        blockers.append(f"case {case_id}: selected run has no owned result file {filename}")
    else:
        result_hashes = manifest.get("result_sha256")
        expected = None
        if isinstance(result_hashes, dict):
            expected = result_hashes.get(filename)
            if expected is None:
                for key, value in result_hashes.items():
                    if Path(str(key)).name == filename:
                        expected = value
                        break
        if not expected:
            blockers.append(f"case {case_id}: manifest has no result_sha256 for {filename}")
        elif str(expected) != _sha256(result_path):
            blockers.append(f"case {case_id}: result file hash mismatch for {filename}")

    blockers.extend(
        _check_run_evidence(repo, run_dir, case_id, run_id, manifest, filename, result_path)
    )
    return blockers


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

    blockers.extend(
        _format_decision_issue(issue)
        for issue in decision_issues(repo, FINAL_REQUIRED_DECISION_IDS)
    )

    if mode == "final":
        selection, selection_status = load_selected_runs(repo)
        if selection_status != "approved":
            blockers.append(
                f"selected_runs.toml selection_status is not approved (got {selection_status!r})"
            )
        for case_id in REQUIRED_CASES:
            run_id = selection.get(case_id)
            if not run_id:
                blockers.append(f"case {case_id}: no selected run_id")
            else:
                blockers.extend(check_selected_run(repo, case_id, run_id))

        hits = paper_placeholder_hits(repo)
        blockers.extend(f"paper still contains placeholder: {hit}" for hit in hits)

        ai_files = [
            path
            for path in (repo / "dist").rglob("*")
            if path.is_file() and path.name in AI_DETAILS_ALIASES
        ]
        if not ai_files:
            blockers.append(f"missing AI details PDF: {OFFICIAL_AI_DETAILS_NAME}")

    if not (repo / "paper" / "main.tex").exists():
        blockers.append("paper/main.tex is missing")

    return blockers


def assert_release_ready(repo_root: str | Path, *, mode: str = "final") -> None:
    blockers = collect_blockers(repo_root, mode=mode)
    if blockers:
        raise ReleaseBlockedError(blockers)
