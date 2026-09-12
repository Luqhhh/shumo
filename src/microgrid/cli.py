"""Command-line interface for the Stage 0 microgrid scaffold."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .artifacts import environment_snapshot, git_info
from .cases import CASE_IDS, run_case
from .dataio import ingest_source, inventory, load_inputs_manifest, write_inventory
from .schemas import MicrogridError, PendingDecisionError
from .smoke import run_smoke


def find_repo_root(start: Path | None = None) -> Path:
    env = os.environ.get("MICROGRID_REPO_ROOT")
    if env:
        return Path(env).resolve()
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "pyproject.toml").exists() and (candidate / "configs").exists():
            return candidate
    # Editable/installed package fallback: src/microgrid/cli.py -> repo root
    package_root = Path(__file__).resolve().parents[2]
    if (package_root / "pyproject.toml").exists():
        return package_root
    return current


def cmd_doctor(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    print(f"repo_root: {repo}")
    print(f"package_version: {__version__}")
    env = environment_snapshot()
    print(f"python: {env['python'].splitlines()[0]}")
    print(f"platform: {env['platform']}")
    print("dependencies:")
    for name, version in env["packages"].items():
        print(f"  {name}: {version or 'NOT INSTALLED'}")
    tools = {}
    for name in ("uv", "git", "xelatex", "latexmk", "biber", "pdftotext"):
        path = shutil.which(name)
        tools[name] = path
        print(f"tool {name}: {path or 'NOT FOUND'}")
    git = git_info(repo)
    print(f"git_commit: {git.get('commit') or 'null'}")
    print(f"git_dirty: {git.get('dirty')}")
    print(f"git_remote: {git.get('remote') or 'null'}")

    decisions_path = repo / "configs" / "decisions.toml"
    print(
        f"decisions_file: {decisions_path} ({'present' if decisions_path.exists() else 'MISSING'})"
    )
    manifest = load_inputs_manifest(repo)
    print(f"inputs_manifest: {'present' if manifest.get('items_available') else 'MISSING'}")
    print(f"input_items: {len(manifest.get('items', []))}")
    raw_files = sorted((repo / "data" / "raw").glob("*.xlsx"))
    templates = sorted((repo / "data" / "templates").glob("*.xlsx"))
    print(f"raw_xlsx: {len(raw_files)}")
    print(f"templates_xlsx: {len(templates)}")
    print(f"latex_ready: {bool(tools['xelatex'] and tools['latexmk'])}")
    if not tools["biber"]:
        print("warning: biber not found; main paper draft will use the fallback bibliography")
    if not raw_files:
        print("warning: raw data not imported; run ingest for local integration")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    manifest = ingest_source(args.source, repo)
    print(f"imported_items: {len(manifest.get('items', []))}")
    print(f"conflicts: {len(manifest.get('conflicts', []))}")
    print(f"manifest: {repo / 'records' / 'inputs_manifest.json'}")
    return 0


def cmd_inspect_data(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    data = inventory(repo)
    path = write_inventory(repo, data)
    print(f"inventory_json: {path}")
    print(f"raw_files: {len(data.get('raw_files', []))}")
    print(f"template_files: {len(data.get('template_files', []))}")
    print(f"literal_time_warnings: {len(data.get('warnings', []))}")
    for warning in data.get("warnings", []):
        print(
            f"  warning {warning['file']}!{warning['sheet']}: "
            f"{warning['label']} ({warning['reason']})"
        )
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir = run_smoke(repo)
    print(f"smoke_dir: {run_dir}")
    print("status: pass")
    print("is_synthetic: true")
    print("model_status: not_implemented")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    try:
        result = run_case(
            args.case,
            repo,
            run_id=args.run_id,
            end_time=args.end_time,
            price_method=args.price_method,
            pv_method=args.pv_method,
            resume=args.resume,
        )
    except PendingDecisionError as exc:
        print(f"case {args.case}: blocked by pending decisions: {', '.join(exc.decision_ids)}")
        print(str(exc))
        return exc.exit_code
    except MicrogridError as exc:
        print(f"case {args.case}: {exc}")
        return exc.exit_code
    print(f"case {result.case_id}: status={result.status} run_id={result.run_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="microgrid", description="CUMCM 2026 C Stage 0 scaffold")
    parser.add_argument("--version", action="version", version=f"microgrid {__version__}")
    parser.add_argument(
        "--repo",
        default=str(find_repo_root()),
        help="repository root (default: auto-detected)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="environment and project checks")
    p_doctor.set_defaults(func=cmd_doctor)

    p_ingest = sub.add_parser("ingest", help="import C-problem files from zip or directory")
    p_ingest.add_argument("--source", required=True, help="zip file or extracted directory")
    p_ingest.set_defaults(func=cmd_ingest)

    p_inspect = sub.add_parser("inspect-data", help="read-only workbook inventory")
    p_inspect.set_defaults(func=cmd_inspect_data)

    p_smoke = sub.add_parser("smoke", help="deterministic synthetic I/O smoke")
    p_smoke.set_defaults(func=cmd_smoke)

    p_run = sub.add_parser(
        "run", help="formal case entry (dispatcher; runners may be unimplemented)"
    )
    p_run.add_argument("--case", choices=CASE_IDS, required=True, help=", ".join(CASE_IDS))
    p_run.add_argument("--run-id", default=None, help="explicit run id for this execution")
    p_run.add_argument(
        "--end-time",
        default=None,
        help="Q4 diagnostic exclusive midnight end; partial runs cannot be selected as formal results",
    )
    p_run.add_argument(
        "--price-method",
        choices=("main", "lag1"),
        default="main",
        help="Q4 causal price prediction method",
    )
    p_run.add_argument(
        "--pv-method",
        choices=("v3", "blend-long"),
        default="v3",
        help="Q4-3/main approved long-lead PV blend; default retains v3",
    )
    p_run.add_argument(
        "--resume",
        action="store_true",
        help="Q4 resume an explicit run at its source-bound checkpoint",
    )
    p_run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except MicrogridError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 1)
    except FileNotFoundError as exc:
        print(f"InputError: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
