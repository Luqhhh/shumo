#!/usr/bin/env python3
"""Aggregate legacy and parallel AI usage segments."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from microgrid.ai_usage import AIUsageError, write_aggregate  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate AI usage records")
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument(
        "--output", default=None, help="default: paper/generated/ai_usage_aggregate.json"
    )
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        target = write_aggregate(repo, args.output)
        print(f"ai_usage_aggregate: {target}")
        return 0
    except AIUsageError as exc:
        print(f"AIUsageError: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
