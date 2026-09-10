#!/usr/bin/env python3
"""Preflight checks for candidate paper PDF / support archive.

This is an auxiliary check, not a substitute for human review by the team.
"""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path

MAX_BYTES = 20_000_000
DISALLOWED_SUPPORT_PARTS = {".git", ".venv", "__pycache__", "local", "data/raw", "resources"}
REQUIRED_RESULT_NAMES = {
    "result1.xlsx",
    "result2.xlsx",
    "result3.xlsx",
    "result4-2.xlsx",
    "result4-3.xlsx",
}
OFFICIAL_AI_DETAILS_NAME = "AI 工具使用详情.pdf"


def inspect_file(path: Path) -> dict[str, object]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_file": path.is_file(),
        "size_bytes": path.stat().st_size if path.exists() else None,
        "under_20_000_000": path.exists() and path.is_file() and path.stat().st_size < MAX_BYTES,
    }


def pdf_summary(path: Path) -> dict[str, object]:
    summary: dict[str, object] = {"page_count": None, "metadata": {}, "error": None}
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        summary["page_count"] = len(reader.pages)
        metadata = reader.metadata or {}
        # Never print every metadata field; keep only the anonymity-sensitive ones.
        summary["metadata"] = {
            "author": metadata.get("/Author"),
            "creator": metadata.get("/Creator"),
            "title": metadata.get("/Title"),
        }
    except Exception as exc:  # pragma: no cover - environment dependent
        summary["error"] = f"{type(exc).__name__}: {exc}"
    return summary


def support_summary(path: Path) -> dict[str, object]:
    summary: dict[str, object] = {
        "zip_member_count": None,
        "disallowed_members": [],
        "missing_required_result_files": sorted(REQUIRED_RESULT_NAMES),
        "error": None,
    }
    if path.suffix.lower() != ".zip":
        summary["error"] = "candidate support is not a ZIP; inspect with the team-approved tool"
        return summary
    try:
        with zipfile.ZipFile(path) as zf:
            names = [name.replace("\\", "/") for name in zf.namelist()]
        summary["zip_member_count"] = len(names)
        disallowed = []
        for name in names:
            parts = set(name.split("/"))
            if parts & DISALLOWED_SUPPORT_PARTS:
                disallowed.append(name)
            if re.search(r"(^|/)(\.env|id_rsa|.*token.*|.*secret.*)$", name, re.IGNORECASE):
                disallowed.append(name)
        summary["disallowed_members"] = disallowed
        present = {Path(name).name for name in names}
        required = set(REQUIRED_RESULT_NAMES) | {OFFICIAL_AI_DETAILS_NAME}
        summary["missing_required_result_files"] = sorted(required - present)
    except zipfile.BadZipFile as exc:
        summary["error"] = f"BadZipFile: {exc}"
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Candidate submission preflight")
    parser.add_argument("--paper", required=True)
    parser.add_argument("--support", required=True)
    parser.add_argument("--json", action="store_true", help="emit machine-readable report")
    args = parser.parse_args(argv)

    paper_info = inspect_file(Path(args.paper))
    support_info = inspect_file(Path(args.support))
    report = {
        "paper": paper_info,
        "support": support_info,
        "paper_pdf": pdf_summary(Path(args.paper)) if paper_info["exists"] else None,
        "support_archive": support_summary(Path(args.support)) if support_info["exists"] else None,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for result in (paper_info, support_info):
            print(
                f"{result['path']}: exists={result['exists']} size={result['size_bytes']} "
                f"under_20MB={result['under_20_000_000']}"
            )
        if report["paper_pdf"]:
            pdf = report["paper_pdf"]
            print(f"paper_pages: {pdf['page_count']} error={pdf['error']}")
            print(f"paper_metadata: {pdf['metadata']}")
        if report["support_archive"]:
            support = report["support_archive"]
            print(f"support_members: {support['zip_member_count']} error={support['error']}")
            print(f"support_disallowed: {support['disallowed_members'][:20]}")
            print(f"support_missing_results: {support['missing_required_result_files']}")

    hard_failure = not (paper_info["under_20_000_000"] and support_info["under_20_000_000"])
    if report["support_archive"]:
        support_archive = report["support_archive"]
        hard_failure = hard_failure or bool(support_archive["disallowed_members"])
        hard_failure = hard_failure or bool(support_archive["error"])
        hard_failure = hard_failure or bool(support_archive["missing_required_result_files"])
    if report["paper_pdf"] and report["paper_pdf"].get("metadata"):
        author = report["paper_pdf"]["metadata"].get("author")
        if author not in (None, "", "None"):
            hard_failure = True
            print(f"anonymity warning: PDF Author metadata is not empty: {author!r}")
    if hard_failure:
        print("check failed: candidate file/size/archive guard failed")
        return 1
    print("candidate check passed; this is not proof of final rule compliance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
