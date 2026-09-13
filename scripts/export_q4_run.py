"""Export a validated full-year Q4 main run; never select or approve it."""

from __future__ import annotations

import argparse
from pathlib import Path

from microgrid.excel_export import export_case_result
from microgrid.problem.result_io import load_case_result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--case", choices=("q4_2", "q4_3"), required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    path = args.repo / "outputs" / "runs" / args.case / args.run_id / "domain_result.json"
    result = load_case_result(path)
    print(export_case_result(args.repo, result))


if __name__ == "__main__":
    main()
