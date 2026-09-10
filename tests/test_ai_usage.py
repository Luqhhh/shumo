from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_ai_usage_records_are_valid_and_pending():
    path = REPO_ROOT / "records" / "ai_usage.jsonl"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines
    for line in lines:
        record = json.loads(line)
        assert record["tool_name"]
        assert record["version_model"]
        assert record["purpose"]
        assert record["human_review_status"] == "pending"
        text = line.lower()
        assert "/home/" not in text
        assert "c:\\users\\" not in text


def test_inputs_manifest_has_no_absolute_source_path():
    path = REPO_ROOT / "records" / "inputs_manifest.json"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert "/home/" not in text
    assert data["schema_version"] == 1
