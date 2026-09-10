#!/usr/bin/env python3
"""Build the paper draft / AI-details draft with explicit LaTeX error handling."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from microgrid.checks import collect_blockers  # noqa: E402
from microgrid.paper_assets import generate_draft_assets  # noqa: E402


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build CUMCM 2026 C paper assets")
    parser.add_argument("--mode", choices=["draft", "final"], default="draft")
    parser.add_argument(
        "--target",
        choices=["main", "ai-details"],
        default="main",
        help="main paper or independent AI details PDF",
    )
    parser.add_argument("--repo", default=str(REPO_ROOT))
    return parser.parse_args(argv)


def _log_has_errors(log: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    markers_errors = [
        "! LaTeX Error",
        "! Undefined control sequence",
        "! Emergency stop",
        "Fatal error occurred",
        "There were undefined references",
    ]
    markers_warnings = [
        "Missing character",
        "LaTeX Warning: Reference",
        "LaTeX Warning: Citation",
        "Package biblatex Warning",
    ]
    for line in log.splitlines():
        if any(marker in line for marker in markers_errors):
            errors.append(line.strip())
        elif any(marker in line for marker in markers_warnings):
            warnings.append(line.strip())
    return errors, warnings


def _clean_build_artifacts(build_dir: Path, target_base: str) -> None:
    """Remove stale aux files so a fallback/biblatex switch cannot poison a build."""

    for path in build_dir.glob(f"{target_base}.*"):
        if path.is_file():
            path.unlink()


def _emit_ci_error_annotations(errors: list[str], tex_log: str, latexmk_log: Path) -> None:
    """Surface LaTeX failure context through GitHub Actions annotations."""

    import os

    if not os.environ.get("GITHUB_ACTIONS"):
        return
    messages = list(errors[:12])
    if tex_log:
        tail = [line.strip() for line in tex_log.splitlines() if line.strip()][-12:]
        messages.extend(tail)
    for message in messages:
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error file={latexmk_log.as_posix()}::{escaped}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    repo = Path(args.repo).resolve()
    paper_dir = repo / "paper"
    build_dir = paper_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    target_tex = "main.tex" if args.target == "main" else "ai_details.tex"
    target_base = "main" if args.target == "main" else "ai_details"

    if args.mode == "final":
        blockers = collect_blockers(repo, mode="final")
        print("FINAL BUILD BLOCKED:")
        for blocker in blockers:
            print(f"  - {blocker}")
        return 5

    generate_draft_assets(repo)
    _clean_build_artifacts(build_dir, target_base)

    for tool in ("xelatex", "latexmk"):
        if shutil.which(tool) is None:
            print(f"ENVIRONMENT GAP: required LaTeX tool not found: {tool}")
            print("latex_verified=false")
            return 2

    command = [
        "latexmk",
        "-xelatex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-outdir=build",
        target_tex,
    ]
    proc = subprocess.run(command, cwd=paper_dir, capture_output=True, text=True)
    log = proc.stdout + "\n" + proc.stderr
    latexmk_log = build_dir / f"{target_base}_latexmk.log"
    latexmk_log.write_text(log, encoding="utf-8", errors="replace")

    tex_log_path = build_dir / f"{target_base}.log"
    tex_log = (
        tex_log_path.read_text(encoding="utf-8", errors="replace") if tex_log_path.exists() else ""
    )
    errors, warnings = _log_has_errors(tex_log)
    combined_errors = errors + (["latexmk exited non-zero"] if proc.returncode else [])

    pdf_path = build_dir / f"{target_base}.pdf"
    if combined_errors:
        print(f"LaTeX build failed (exit={proc.returncode})")
        for error in combined_errors[:40]:
            print(f"  error: {error}")
        print(f"log: {latexmk_log}")
        _emit_ci_error_annotations(combined_errors, tex_log, latexmk_log)
        return 1

    if warnings:
        print(f"LaTeX build completed with {len(warnings)} warning(s):")
        for warning in warnings[:20]:
            print(f"  warning: {warning}")
        print(
            "draft mode: warnings listed above; final mode would fail on missing characters/undefined refs"
        )
    if not pdf_path.exists():
        print(f"LaTeX reported success but PDF is missing: {pdf_path}")
        return 1
    print(f"paper_pdf: {pdf_path}")
    if args.target == "ai-details":
        official = build_dir / "AI 工具使用详情.pdf"
        shutil.copyfile(pdf_path, official)
        print(f"ai_details_pdf: {official}")
    print("latex_verified=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
