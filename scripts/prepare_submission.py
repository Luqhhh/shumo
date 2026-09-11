#!/usr/bin/env python3
"""Prepare formal submission staging.

Blockers are evaluated first.  Only when no blockers remain does the script
create staging, write manifests, and invoke the final paper build.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from microgrid.artifacts import run_dir_for  # noqa: E402
from microgrid.checks import collect_blockers, load_selected_runs  # noqa: E402
from microgrid.dataio import sha256_file, utc_now  # noqa: E402

RESULT_BY_CASE = {
    "q1": "result1.xlsx",
    "q2": "result2.xlsx",
    "q3": "result3.xlsx",
    "q4_2": "result4-2.xlsx",
    "q4_3": "result4-3.xlsx",
}


def create_staging(repo: Path) -> Path:
    selection, status = load_selected_runs(repo)
    if status != "approved":
        raise RuntimeError(f"selection_status is not approved: {status!r}")
    staging = repo / "dist" / "support_staging"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "results").mkdir(parents=True)
    for case_id, filename in RESULT_BY_CASE.items():
        run_id = selection.get(case_id)
        if not run_id:
            raise RuntimeError(f"missing selected run for {case_id}")
        source = run_dir_for(repo, case_id, run_id) / "results" / filename
        if not source.is_file():
            raise RuntimeError(f"missing selected result: {source}")
        shutil.copyfile(source, staging / "results" / filename)

    for name in ("src", "configs"):
        source = repo / name
        if source.is_dir():
            shutil.copytree(source, staging / name)
    for name in ("README.md", "pyproject.toml", "uv.lock"):
        source = repo / name
        if source.is_file():
            shutil.copyfile(source, staging / name)

    support_manifest = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "files": sorted(
            str(path.relative_to(staging))
            for path in staging.rglob("*")
            if path.is_file() and path.name != "support_manifest.json"
        ),
    }
    (staging / "support_manifest.json").write_text(
        json.dumps(support_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    selected_metadata = {
        "schema_version": 1,
        "selection": selection,
        "run_manifest_sha256": {
            case_id: sha256_file(run_dir_for(repo, case_id, run_id) / "manifest.json")
            for case_id, run_id in selection.items()
            if (run_dir_for(repo, case_id, run_id) / "manifest.json").is_file()
        },
    }
    (staging / "selected_run_metadata.json").write_text(
        json.dumps(selected_metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return staging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare final submission staging")
    parser.add_argument("--mode", choices=["draft", "final"], default="final")
    parser.add_argument("--repo", default=str(REPO_ROOT))
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if args.mode == "final":
        blockers = collect_blockers(repo, mode="final")
        if blockers:
            print("FORMAL SUBMISSION BLOCKED:")
            for blocker in blockers:
                print(f"  - {blocker}")
            print("No staging was produced.  Resolve the blockers above and rerun.")
            return 5
        try:
            staging = create_staging(repo)
            build = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "build_paper.py"),
                    "--mode",
                    "final",
                    "--repo",
                    str(repo),
                ],
                capture_output=True,
                text=True,
            )
            if build.returncode != 0:
                print(build.stdout)
                print(build.stderr, file=sys.stderr)
                return build.returncode
            print(f"support_staging: {staging}")
            print("formal submission staging prepared; manual review is still required")
            return 0
        except Exception as exc:
            print(f"FORMAL SUBMISSION BLOCKED: {type(exc).__name__}: {exc}")
            return 5

    blockers = collect_blockers(repo, mode="draft")
    print(f"draft blockers: {len(blockers)}; use build_paper.py --mode draft")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
