"""Create the explicitly approved Q3 delivery without rewriting diagnostic sources."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from microgrid.artifacts import (  # noqa: E402
    build_manifest,
    file_digest,
    write_json,
    write_manifest,
)
from microgrid.problem.q3_export import create_q3_delivery, publish_q3_paper_assets  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--audit-report", required=True)
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    safe_id = bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.run_id))
    target = repo / "outputs/runs/q3" / args.run_id
    existed = target.exists()
    try:
        print(f"Q3 approved source preflight: {args.source_run_id}", flush=True)
        create_q3_delivery(
            repo,
            source_run_id=args.source_run_id,
            run_id=args.run_id,
            audit_report=Path(args.audit_report).resolve(),
        )
        tables = publish_q3_paper_assets(repo, args.run_id)
        output = target / "results/result3.xlsx"
        print(
            f"result3: {output}\nSHA-256: {file_digest(output)}\npaper_tables: {tables}", flush=True
        )
        print("formal delivery complete; audited trajectory reused, no MPC rerun", flush=True)
        return 0
    except Exception as exc:
        print(f"Q3 delivery failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        if safe_id and not existed and args.source_run_id != args.run_id:
            target.mkdir(parents=True, exist_ok=True)
            write_json(
                target / "failure.json",
                {
                    "case_id": "q3",
                    "run_id": args.run_id,
                    "failure_stage": "formal_delivery",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            manifest = build_manifest(
                repo,
                run_id=args.run_id,
                case_id="q3",
                command=["python", "scripts/export_q3_run.py", *(argv or sys.argv[1:])],
                status="failed",
                is_synthetic=False,
                model_status="formal_delivery_from_audited_trajectory",
            )
            manifest.update(validation_ok=False, failure_stage="formal_delivery")
            write_manifest(target, manifest, overwrite=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
