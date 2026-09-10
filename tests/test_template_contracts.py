from __future__ import annotations

from pathlib import Path

from microgrid.dataio import sha256_file
from microgrid.excel_export import (
    assert_official_result_names,
    copy_template_preview,
    make_smoke_result_name,
    validate_template,
)


def test_all_five_synthetic_templates_validate(synthetic_template_repo):
    for case in ("q1", "q2", "q3", "q4_2", "q4_3"):
        check = validate_template(synthetic_template_repo, case)
        assert check.ok, check.issues


def test_preview_is_isolated_and_original_hash_unchanged(synthetic_template_repo):
    original = synthetic_template_repo / "data" / "templates" / "result1.xlsx"
    before = sha256_file(original)
    preview = copy_template_preview(synthetic_template_repo, "q1")
    assert preview.exists()
    assert preview.name == "preview_result1.xlsx"
    assert sha256_file(original) == before
    assert "template_preview" in str(preview)


def test_official_result_name_guard_and_smoke_prefix():
    assert assert_official_result_names([Path("result1.xlsx"), Path("result4-3.xlsx")]) == []
    assert assert_official_result_names([Path("result1_filled.xlsx")])
    assert make_smoke_result_name("result2.xlsx") == "SMOKE_result2.xlsx"
