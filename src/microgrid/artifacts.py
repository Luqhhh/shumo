"""Run directories, manifests and reproducibility fingerprints."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path
from typing import Any

from .dataio import ensure_dir, sha256_file, utc_now
from .schemas import RunManifest

CODE_GLOBS = [
    "pyproject.toml",
    "configs/*.toml",
    "src/microgrid/*.py",
    "scripts/*.py",
]


def git_info(repo_root: str | Path) -> dict[str, Any]:
    repo = Path(repo_root)
    out: dict[str, Any] = {"commit": None, "dirty": None, "status_short": None, "remote": None}
    try:
        top = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if Path(top).resolve() != repo.resolve():
            # A temporary or unrelated directory must not inherit a parent
            # repository's commit / dirty state.
            return out
        commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        remote = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        out.update(
            {
                "commit": commit or None,
                "dirty": bool(status.strip()),
                "status_short": status.splitlines(),
                "remote": remote or None,
            }
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    return out


def source_tree_hash(repo_root: str | Path) -> str:
    repo = Path(repo_root)
    digest = hashlib.sha256()
    files: list[Path] = []
    for pattern in CODE_GLOBS:
        files.extend(sorted(repo.glob(pattern)))
    for path in files:
        if not path.is_file():
            continue
        rel = path.relative_to(repo).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def config_snapshot(repo_root: str | Path) -> dict[str, Any]:
    repo = Path(repo_root)
    snapshot: dict[str, Any] = {}
    for path in sorted((repo / "configs").glob("*.toml")):
        snapshot[path.name] = tomllib.loads(path.read_text(encoding="utf-8"))
    return snapshot


def environment_snapshot() -> dict[str, Any]:
    packages = {}
    for name in (
        "numpy",
        "pandas",
        "openpyxl",
        "matplotlib",
        "pypdf",
        "pytest",
        "ruff",
        "microgrid",
    ):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "python": sys.version,
        "executable": Path(sys.executable).name,
        "platform": platform.platform(),
        "packages": packages,
    }


def input_hashes(repo_root: str | Path) -> dict[str, str]:
    manifest_path = Path(repo_root) / "records" / "inputs_manifest.json"
    if not manifest_path.exists():
        return {}
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    hashes: dict[str, str] = {}
    for item in data.get("items", []):
        path = item.get("repository_path")
        digest = item.get("sha256")
        if path and digest:
            hashes[path] = digest
    return hashes


def new_run_id(prefix: str, repo_root: str | Path) -> str:
    return f"{prefix}-{utc_now().replace(':', '').replace('+00:00', 'Z')}-{source_tree_hash(repo_root)[:8]}"


def build_manifest(
    repo_root: str | Path,
    *,
    run_id: str,
    case_id: str,
    command: Iterable[str],
    status: str,
    is_synthetic: bool,
    model_status: str = "not_implemented",
    random_seed: int | None = None,
    result_files: dict[str, str] | None = None,
    result_sha256: dict[str, str] | None = None,
) -> dict[str, Any]:
    repo = Path(repo_root)
    git = git_info(repo)
    manifest = RunManifest(
        run_id=run_id,
        case_id=case_id,
        command=list(command),
        input_hashes=input_hashes(repo),
        config_snapshot=config_snapshot(repo),
        code_commit=git.get("commit"),
        code_dirty=git.get("dirty"),
        source_hash=source_tree_hash(repo),
        environment=environment_snapshot(),
        random_seed=random_seed,
        created_at=utc_now(),
        status=status,
        is_synthetic=is_synthetic,
        model_status=model_status,
        result_files=dict(result_files or {}),
        result_sha256=dict(result_sha256 or {}),
    )
    return manifest.to_dict()


def write_manifest(run_dir: str | Path, manifest: dict[str, Any]) -> Path:
    directory = ensure_dir(run_dir)
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_json(path: str | Path, data: Any) -> Path:
    p = Path(path)
    ensure_dir(p.parent)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def file_digest(path: str | Path) -> str:
    return sha256_file(path)
