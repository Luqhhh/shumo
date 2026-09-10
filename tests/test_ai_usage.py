from __future__ import annotations

import json
from pathlib import Path

from microgrid.ai_usage import aggregate_usage, validate_entry

REPO_ROOT = Path(__file__).resolve().parents[1]


def _iter_usage_paths(repo: Path) -> list[Path]:
    paths = []
    legacy = repo / "records" / "ai_usage.jsonl"
    if legacy.exists():
        paths.append(legacy)
    paths.extend(sorted((repo / "records" / "ai_usage" / "entries").rglob("*.jsonl")))
    return paths


def test_ai_usage_records_are_valid_and_pending():
    paths = _iter_usage_paths(REPO_ROOT)
    assert paths
    seen_ids: set[str] = set()
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            assert not validate_entry(record, source=path.name)
            assert record["human_review_status"] == "pending"
            assert record["entry_id"] not in seen_ids
            seen_ids.add(record["entry_id"])


def test_aggregate_usage_includes_legacy_history():
    aggregate = aggregate_usage(REPO_ROOT)
    assert aggregate["entry_count"] >= 1
    assert aggregate["pending_review_count"] >= 1
    assert "records/ai_usage.jsonl" in aggregate["source_files"]
    assert aggregate["aggregate_hash"]


def test_confirmed_record_requires_human_review_fields():
    record = {
        "entry_id": "test-confirmed",
        "timestamp_utc": "2026-09-10T00:00:00Z",
        "tool_name": "test tool",
        "version_model": "test-model",
        "purpose": "test",
        "affected_files": ["x"],
        "prompt_summary": "test",
        "adoption": "test",
        "human_modification": "test",
        "human_review_status": "confirmed",
    }
    issues = validate_entry(record)
    assert any("missing reviewed_by" in issue for issue in issues)
    assert any("missing reviewed_at" in issue for issue in issues)


def test_inputs_manifest_has_no_absolute_source_path():
    path = REPO_ROOT / "records" / "inputs_manifest.json"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert "/home/" not in text
    assert data["schema_version"] == 1
