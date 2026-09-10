#!/usr/bin/env python3
"""Prepare formal submission staging.

Stage 0 must fail before any final artifact is produced.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from microgrid.checks import collect_blockers  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare final submission staging")
    parser.add_argument("--mode", choices=["draft", "final"], default="final")
    parser.add_argument("--repo", default=str(REPO_ROOT))
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if args.mode == "final":
        blockers = collect_blockers(repo, mode="final")
        print("FORMAL SUBMISSION BLOCKED:")
        for blocker in blockers:
            print(f"  - {blocker}")
        print("No staging was produced.  Resolve the blockers above and rerun.")
        return 5

    blockers = collect_blockers(repo, mode="draft")
    print("draft staging is not implemented; use build_paper.py --mode draft")
    print(f"draft blockers: {len(blockers)}")
    return 6


if __name__ == "__main__":
    raise SystemExit(main())
