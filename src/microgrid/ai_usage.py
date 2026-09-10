"""Append-friendly AI usage records and aggregation.

Parallel contributors must not edit one shared JSONL file.  Each contributor
or session appends to its own segment under ``records/ai_usage/entries/``;
this module concatenates the legacy root JSONL and all segment files into one
deterministic aggregate for the final AI details PDF.

Human review is intentionally never inferred here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .dataio import utc_now

LEGACY_FILE = Path("records/ai_usage.jsonl")
SEGMENT_GLOB = "records/ai_usage/entries/**/*.jsonl"
AGGREGATE_SCHEMA_VERSION = 1

REQUIRED_ENTRY_FIELDS = (
    "entry_id",
    "timestamp_utc",
    "tool_name",
    "version_model",
    "purpose",
    "affected_files",
    "prompt_summary",
    "adoption",
    "human_modification",
    "human_review_status",
)


class AIUsageError(ValueError):
    """Invalid AI usage record or duplicate aggregation key."""


def _iter_segment_paths(repo_root: Path) -> list[Path]:
    paths: list[Path] = []
    legacy = repo_root / LEGACY_FILE
    if legacy.is_file():
        paths.append(legacy)
    paths.extend(sorted(repo_root.glob(SEGMENT_GLOB)))
    return paths


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AIUsageError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(entry, dict):
            raise AIUsageError(f"{path}:{line_number}: entry must be a JSON object")
        entries.append(entry)
    return entries


def validate_entry(entry: dict[str, Any], *, source: str = "") -> list[str]:
    """Return validation issues; never repairs or rewrites a record."""

    issues: list[str] = []
    prefix = f"{source}: " if source else ""
    for field in REQUIRED_ENTRY_FIELDS:
        if field not in entry:
            issues.append(f"{prefix}missing field {field}")
    status = str(entry.get("human_review_status", ""))
    if status not in {"pending", "confirmed"}:
        issues.append(f"{prefix}invalid human_review_status {status!r}")
    if status == "confirmed":
        if not str(entry.get("reviewed_by", "")).strip():
            issues.append(f"{prefix}confirmed record missing reviewed_by")
        if not str(entry.get("reviewed_at", "")).strip():
            issues.append(f"{prefix}confirmed record missing reviewed_at")
    entry_id = str(entry.get("entry_id", "")).strip()
    if not entry_id:
        issues.append(f"{prefix}entry_id must not be empty")
    text = json.dumps(entry, ensure_ascii=False)
    if "/home/" in text or "c:\\users\\" in text.lower():
        issues.append(f"{prefix}entry appears to contain an absolute user path")
    return issues


def aggregate_usage(repo_root: str | Path) -> dict[str, Any]:
    """Validate and aggregate legacy + segment records without losing history."""

    repo = Path(repo_root)
    entries: list[dict[str, Any]] = []
    sources: list[str] = []
    issues: list[str] = []
    seen_ids: set[str] = set()
    for path in _iter_segment_paths(repo):
        rel = path.relative_to(repo).as_posix()
        sources.append(rel)
        for entry in _read_jsonl(path):
            issues.extend(validate_entry(entry, source=rel))
            entry_id = str(entry.get("entry_id", ""))
            if entry_id in seen_ids:
                issues.append(f"{rel}: duplicate entry_id {entry_id!r}")
            seen_ids.add(entry_id)
            entries.append(entry)
    if issues:
        raise AIUsageError("; ".join(issues))
    pending = sum(1 for entry in entries if entry.get("human_review_status") == "pending")
    return {
        "schema_version": AGGREGATE_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "source_files": sources,
        "entry_count": len(entries),
        "pending_review_count": pending,
        "entries": entries,
        "aggregate_hash": hashlib.sha256(
            json.dumps(entries, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def write_aggregate(repo_root: str | Path, output: str | Path | None = None) -> Path:
    repo = Path(repo_root)
    target = (
        Path(output)
        if output is not None
        else repo / "paper" / "generated" / "ai_usage_aggregate.json"
    )
    if not target.is_absolute():
        target = repo / target
    target.parent.mkdir(parents=True, exist_ok=True)
    aggregate = aggregate_usage(repo)
    target.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target
