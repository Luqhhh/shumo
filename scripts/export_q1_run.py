#!/usr/bin/env python3
"""Export one internal Q1 run to result1.xlsx and generate same-source tables."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from microgrid.approvals import load_decisions  # noqa: E402
from microgrid.artifacts import run_dir_for, write_manifest  # noqa: E402
from microgrid.dataio import sha256_file, utc_now  # noqa: E402
from microgrid.excel_export import (  # noqa: E402
    TEMPLATE_EXPORT_DECISION_ID,
    export_case_result,
    template_path,
)
from microgrid.paper_assets import generate_q1_result_tables  # noqa: E402
from microgrid.problem.result_io import load_case_result  # noqa: E402
from microgrid.problem.validation import validate_complete_run  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a formal Q1 run to result1.xlsx")
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", default=None, help="default: run_dir/results/result1.xlsx")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    run_dir = run_dir_for(repo, "q1", args.run_id)
    manifest_path = run_dir / "manifest.json"
    domain_path = run_dir / "domain_result.json"
    if not manifest_path.is_file() or not domain_path.is_file():
        print(f"InputError: incomplete run directory: {run_dir}", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("run_id") != args.run_id:
        print("InputError: manifest.run_id does not match requested run_id", file=sys.stderr)
        return 2
    if manifest.get("case_id") != "q1" or manifest.get("status") != "success":
        print("InputError: only successful Q1 runs can be exported", file=sys.stderr)
        return 2
    if manifest.get("is_synthetic") is not False:
        print("InputError: synthetic run cannot be exported", file=sys.stderr)
        return 2

    result = load_case_result(domain_path)
    if result.case_id != "q1" or result.run_id != args.run_id:
        print("InputError: domain_result identity does not match requested Q1 run", file=sys.stderr)
        return 2
    if result.status != "success" or result.is_synthetic is not False:
        print("InputError: domain_result is not a successful non-synthetic Q1 run", file=sys.stderr)
        return 2
    report = validate_complete_run(
        result.intervals,
        (result.intervals[0].day,) if result.intervals else (),
        require_daily_equal_ends=True,
    )
    if not report.ok:
        print(
            "InputError: domain_result failed complete-run validation: " + "; ".join(report.issues),
            file=sys.stderr,
        )
        return 2
    output = Path(args.output).resolve() if args.output else run_dir / "results" / "result1.xlsx"
    try:
        exported = export_case_result(repo, result, output_path=output)
    except Exception as exc:
        print(f"ExportError: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    template = template_path(repo, "q1")
    decision = load_decisions(repo).get(TEMPLATE_EXPORT_DECISION_ID.replace("_", "-"), {})
    export_manifest = {
        "schema_version": 1,
        "run_id": args.run_id,
        "case_id": "q1",
        "source_result_file": "domain_result.json",
        "source_result_sha256": sha256_file(domain_path),
        "template_file": str(template.relative_to(repo)),
        "template_sha256": sha256_file(template),
        "output_file": str(exported.relative_to(repo)),
        "output_sha256": sha256_file(exported),
        "decision_snapshot": {
            "D_TIME_TEMPLATE_EXPORT": {
                "status": decision.get("status"),
                "choice": decision.get("choice"),
                "confirmed_by": decision.get("confirmed_by"),
                "confirmed_at": decision.get("confirmed_at"),
            }
        },
        "exported_at": utc_now(),
        "note": "Q1 row-order mapping preserves official template labels; see D_TIME_TEMPLATE_EXPORT.",
    }
    (run_dir / "export_manifest.json").write_text(
        json.dumps(export_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    manifest["result_files"] = {"result1.xlsx": str(exported.relative_to(run_dir))}
    manifest["result_sha256"] = {"result1.xlsx": export_manifest["output_sha256"]}
    manifest["export_manifest"] = "export_manifest.json"
    write_manifest(run_dir, manifest, overwrite=True)

    tables = generate_q1_result_tables(repo, args.run_id, run_dir=run_dir)
    print(f"exported_result1: {exported}")
    print(f"export_manifest: {run_dir / 'export_manifest.json'}")
    print(f"paper_tables: {tables}")
    print(f"output_sha256: {export_manifest['output_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
