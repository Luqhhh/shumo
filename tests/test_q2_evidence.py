from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

from openpyxl import Workbook

from microgrid.dataio import sha256_file


def _main(args: list[str]) -> int:
    script = Path(__file__).parents[1] / "scripts" / "evaluate_q2_forecast.py"
    spec = importlib.util.spec_from_file_location("evaluate_q2_forecast", script)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError("scripts/evaluate_q2_forecast.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(args)


def _labels() -> list[str]:
    labels = [f"{(slot + 1) * 10 // 60}:{(slot + 1) * 10 % 60:02d}" for slot in range(143)]
    labels.append("0:00+1")
    return labels


def _write_actual_workbook(path: Path) -> Path:
    wb = Workbook()
    for index, name in enumerate(("负载", "光伏")):
        ws = wb.active if index == 0 else wb.create_sheet()
        ws.title = name
        ws.append(["日期", *_labels()])
        for day_index in range(35):
            day = dt.date(2025, 1, 1) + dt.timedelta(days=day_index)
            base = 600.0 + 10.0 * day_index if name == "负载" else 100.0 + day_index
            ws.append([day, *[base + slot for slot in range(144)]])
    wb.save(path)
    wb.close()
    return path


def _args(attachment: Path, output: Path) -> list[str]:
    return [
        "--attachment2",
        str(attachment),
        "--load-sheet",
        "负载",
        "--pv-sheet",
        "光伏",
        "--start-date",
        "2025-02-01",
        "--end-date",
        "2025-02-02",
        "--output",
        str(output),
    ]


def test_q2_evidence_is_explicit_causal_and_deterministic(tmp_path: Path) -> None:
    attachment = _write_actual_workbook(tmp_path / "附件2.xlsx")
    before = sha256_file(attachment)
    first_output = tmp_path / "evidence-1.json"
    second_output = tmp_path / "evidence-2.json"

    assert _main(_args(attachment, first_output)) == 0
    assert _main(_args(attachment, second_output)) == 0
    assert sha256_file(attachment) == before

    first = json.loads(first_output.read_text(encoding="utf-8"))
    second = json.loads(second_output.read_text(encoding="utf-8"))
    assert first == second
    assert first["source_hash"] == before
    assert first["sample_range"] == {"start_date": "2025-02-01", "end_date": "2025-02-02"}
    assert first["weights"] == [0.25, 0.25, 0.25, 0.25]
    assert first["model_version"]
    assert first["metrics"]["definitions"]["mae"]
    assert first["metrics"]["definitions"]["rmse"]
    assert first["causal_visibility"]["future_visible_records"] == 0
    assert first["causal_visibility"]["lag_counts"] == {
        "7d": 288,
        "14d": 288,
        "21d": 288,
        "28d": 288,
    }


def test_q2_evidence_rejects_protected_output_directory(tmp_path: Path) -> None:
    attachment = _write_actual_workbook(tmp_path / "附件2.xlsx")
    protected = tmp_path / "data" / "raw" / "evidence.json"

    assert _main(_args(attachment, protected)) != 0
    assert not protected.exists()
