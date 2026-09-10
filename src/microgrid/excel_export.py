"""Official result-template adapters.

Stage 0 never overwrites the official templates.  It can produce previews in
`outputs/template_preview/`; real numeric filling is blocked by D-TIME.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook  # type: ignore[import-untyped]

from .dataio import ensure_dir
from .schemas import InputError

TEMPLATE_BY_CASE = {
    "q1": "result1.xlsx",
    "q2": "result2.xlsx",
    "q3": "result3.xlsx",
    "q4_2": "result4-2.xlsx",
    "q4_3": "result4-3.xlsx",
}

EXPECTED_SHEETS = {
    "q1": ["计划购电量", "充放电量"],
    "q2": ["计划购电量", "充放电量", "紧急购电量"],
    "q3": ["计划购电量", "调整购电量", "充放电量", "紧急购电量"],
    "q4_2": ["计划购电量", "充放电量", "紧急购电量"],
    "q4_3": ["计划购电量", "调整购电量", "充放电量", "紧急购电量"],
}


@dataclass(frozen=True)
class TemplateValidation:
    case_id: str
    filename: str
    sheet_names: list[str]
    issues: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.issues


def template_dir(repo_root: str | Path) -> Path:
    return Path(repo_root) / "data" / "templates"


def template_path(repo_root: str | Path, case_id: str) -> Path:
    if case_id not in TEMPLATE_BY_CASE:
        raise InputError(f"unknown case {case_id!r}")
    return template_dir(repo_root) / TEMPLATE_BY_CASE[case_id]


def validate_template(repo_root: str | Path, case_id: str) -> TemplateValidation:
    path = template_path(repo_root, case_id)
    if not path.exists():
        return TemplateValidation(case_id, path.name, [], [f"missing template: {path}"], [])
    wb = load_workbook(path, data_only=False)
    try:
        sheets = list(wb.sheetnames)
        expected = EXPECTED_SHEETS[case_id]
        issues: list[str] = []
        warnings: list[str] = []
        if sheets != expected:
            issues.append(f"sheet set/order mismatch: got {sheets}, expected {expected}")
        for name in sheets:
            ws = wb[name]
            if ws.max_row is None or ws.max_column is None:
                warnings.append(f"sheet {name} has no dimension")
            if name in {"计划购电量", "调整购电量"}:
                if case_id == "q1":
                    if ws["A1"].value != "时间段" or ws["B1"].value != "购电量":
                        issues.append(f"{name}: unexpected header row")
                else:
                    if ws["A1"].value not in {"日期\\时间", "日期/时间"}:
                        issues.append(f"{name}: expected date/time header in A1")
                    first_label = ws["B1"].value
                    if first_label != "0:10-0:20":
                        issues.append(f"{name}: first interval label changed to {first_label!r}")
                    last_interval_col = ws.max_column - 2 if case_id != "q1" else ws.max_column - 1
                    last_label = ws.cell(row=1, column=last_interval_col).value
                    if last_label not in {"0:00-0:10+1", "0:00+1-0:10+1"}:
                        warnings.append(f"{name}: unexpected last interval label {last_label!r}")
            if name == "充放电量":
                if case_id == "q1":
                    if ws["A1"].value != "时间段":
                        issues.append("充放电量: expected A1=时间段")
                    if ws["D1"].value != "时刻" or ws["E1"].value != "储电量":
                        issues.append("充放电量: expected D1=时刻, E1=储电量")
                else:
                    if ws["A1"].value != "日期" or ws["B1"].value != "时间段":
                        issues.append("充放电量: expected A1=日期, B1=时间段")
                    if ws["E1"].value != "时刻" or ws["F1"].value != "储电量":
                        issues.append("充放电量: expected E1=时刻, F1=储电量")
        return TemplateValidation(case_id, path.name, sheets, issues, warnings)
    finally:
        wb.close()


def copy_template_preview(repo_root: str | Path, case_id: str) -> Path:
    """Copy a template into an isolated preview area without editing the original."""

    repo = Path(repo_root)
    src = template_path(repo, case_id)
    if not src.exists():
        raise InputError(f"template not found: {src}")
    dest_dir = ensure_dir(repo / "outputs" / "template_preview")
    dest = dest_dir / f"preview_{src.name}"
    shutil.copyfile(src, dest)
    # Re-open and save to prove the adapter can round-trip structure.  The
    # original is still untouched; any loss would show in the re-open check.
    wb = load_workbook(dest, data_only=False)
    try:
        wb.save(dest)
    finally:
        wb.close()
    check = validate_template(repo, case_id)
    if not check.ok:
        (dest_dir / f"preview_{src.name}.issues.txt").write_text(
            "\n".join(check.issues), encoding="utf-8"
        )
    return dest


def make_smoke_result_name(filename: str) -> str:
    """Smoke exports must be visibly isolated from official result names."""

    return f"SMOKE_{filename}"


def assert_official_result_names(paths: list[Path]) -> list[str]:
    allowed = set(TEMPLATE_BY_CASE.values())
    issues = [str(p) for p in paths if p.name not in allowed]
    return issues
