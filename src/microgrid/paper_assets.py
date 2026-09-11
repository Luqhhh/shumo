"""Paper assets must come from one explicit result selection.

Until formal runs are selected, this module only emits clearly marked draft
placeholders.  It never treats a synthetic run as a result.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .dataio import ensure_dir
from .problem.contracts import STEPS_PER_DAY
from .problem.result_io import load_case_result
from .schemas import PendingDecisionError

SOURCE_FILES = [
    "tests/test_approvals.py",
    "tests/test_ai_usage.py",
    "tests/test_result_io.py",
    "tests/test_q1_inputs.py",
    "tests/test_q1_model.py",
    "tests/test_shared_contracts.py",
    "src/microgrid/approvals.py",
    "src/microgrid/__init__.py",
    "src/microgrid/__main__.py",
    "src/microgrid/artifacts.py",
    "src/microgrid/cases.py",
    "src/microgrid/checks.py",
    "src/microgrid/cli.py",
    "src/microgrid/dataio.py",
    "src/microgrid/excel_export.py",
    "src/microgrid/paper_assets.py",
    "src/microgrid/schemas.py",
    "src/microgrid/smoke.py",
    "src/microgrid/timekeys.py",
    "src/microgrid/ai_usage.py",
    "scripts/aggregate_ai_usage.py",
    "scripts/export_q1_run.py",
    "src/microgrid/problem/__init__.py",
    "src/microgrid/problem/result_io.py",
    "src/microgrid/problem/validation.py",
    "src/microgrid/problem/contracts.py",
    "src/microgrid/problem/common.py",
    "src/microgrid/problem/q1_inputs.py",
    "src/microgrid/problem/q1.py",
    "src/microgrid/problem/q2.py",
    "src/microgrid/problem/q3.py",
    "src/microgrid/problem/q4_2.py",
    "src/microgrid/problem/q4_3.py",
    "scripts/build_paper.py",
    "scripts/check_submission.py",
    "scripts/prepare_submission.py",
]

TABLE_ONE = r"""
\begin{table}[htbp]
\centering
\caption{微网在指定时间段的购电量及全天购电量和购电费（草稿占位，数值待计算）}
\begin{tabular}{lrrlrrlrr}
\toprule
时间段 & 购电量 & 时间段 & 购电量 & 时间段 & 购电量 \\
\midrule
10:00--10:10 & \PH{待计算} & 12:00--12:10 & \PH{待计算} & 14:00--14:10 & \PH{待计算} \\
16:00--16:10 & \PH{待计算} & 18:00--18:10 & \PH{待计算} & 20:00--20:10 & \PH{待计算} \\
全天购电量 & \PH{待计算} & 全天购电费 & \PH{待计算} & & \\
\bottomrule
\end{tabular}
\end{table}
"""

TABLE_TWO = r"""
\begin{table}[htbp]
\centering
\caption{储能设备指定时间段充放电量与 0:00/24:00 储电量（草稿占位，数值待计算）}
\begin{tabular}{lrrlrr}
\toprule
时间段 & 充电量 & 放电量 & 时间段 & 充电量 & 放电量 \\
\midrule
0:00--4:00 & \PH{待计算} & \PH{待计算} & 4:00--8:00 & \PH{待计算} & \PH{待计算} \\
8:00--12:00 & \PH{待计算} & \PH{待计算} & 12:00--16:00 & \PH{待计算} & \PH{待计算} \\
16:00--20:00 & \PH{待计算} & \PH{待计算} & 20:00--24:00 & \PH{待计算} & \PH{待计算} \\
0:00 储电量 & \PH{待计算} & & 24:00 储电量 & \PH{待计算} & \\
\bottomrule
\end{tabular}
\end{table}
"""

TABLE_THREE = r"""
\begin{table}[htbp]
\centering
\caption{指定日期紧急购电量（草稿占位，数值待计算）}
\begin{tabular}{lrlrlrlr}
\toprule
2025.3.20 时间段 & 购电量 & 2025.6.21 时间段 & 购电量 & 2025.9.23 时间段 & 购电量 & 2025.12.21 时间段 & 购电量 \\
\midrule
\PH{待计算} & \PH{待计算} & \PH{待计算} & \PH{待计算} & \PH{待计算} & \PH{待计算} & \PH{待计算} & \PH{待计算} \\
\bottomrule
\end{tabular}
\end{table}
"""


def generated_dir(repo_root: str | Path) -> Path:
    return ensure_dir(Path(repo_root) / "paper" / "generated")


def generate_draft_assets(repo_root: str | Path) -> dict[str, Path]:
    repo = Path(repo_root)
    out = generated_dir(repo)
    files: dict[str, Path] = {}

    tables = out / "draft_tables.tex"
    tables.write_text(TABLE_ONE + "\n" + TABLE_TWO + "\n" + TABLE_THREE + "\n", encoding="utf-8")
    files["tables"] = tables

    macros = out / "draft_macros.tex"
    macros.write_text(
        "% Generated draft macros.  Do not edit by hand.\n"
        "\\providecommand{\\PaperStatus}{草稿 / 结果未完成}\n"
        "\\providecommand{\\PaperModeName}{draft}\n"
        "\\providecommand{\\SyntheticFlag}{合成数据仅用于工程 smoke，不是正式结果}\n",
        encoding="utf-8",
    )
    files["macros"] = macros

    source_lines = [
        "% Generated source-code appendix.  Do not edit by hand.",
        "% In draft mode this list shows the Stage 0 source snapshot.",
        "",
    ]
    for rel in SOURCE_FILES:
        source_lines.append(f"\\VerbatimInput[breaklines=true,breakanywhere=true]{{../{rel}}}")
        source_lines.append("")
    source_file = out / "source_code.tex"
    source_file.write_text("\n".join(source_lines) + "\n", encoding="utf-8")
    files["source_code"] = source_file

    file_list_lines = [
        "% Generated support-file list.  Do not edit by hand.",
        "\\begin{itemize}",
        "  \\item \\texttt{src/}：可运行源码。",
        "  \\item \\texttt{configs/}：项目配置和待确认决策。",
        r"  \item \texttt{records/inputs\_manifest.json}：官方附件哈希清单。",
        r"  \item \texttt{records/ai\_usage.jsonl}：AI 使用记录（人工核验 pending）。",
        "  \\item \\texttt{data/templates/}、\\texttt{data/raw/}、\\texttt{resources/}：原始与官方材料，不随代码提交。",
        "\\end{itemize}",
    ]
    file_list = out / "file_list.tex"
    file_list.write_text("\n".join(file_list_lines) + "\n", encoding="utf-8")
    files["file_list"] = file_list

    ai_macros = out / "ai_usage_macros.tex"
    ai_macros.write_text(
        "% Generated from records/ai_usage.jsonl; model version may be unknown.\n"
        "\\newcommand{\\AIUsedFlag}{使用了 AI 工具}\n"
        "\\newcommand{\\AIReviewStatus}{人工核验：pending}\n",
        encoding="utf-8",
    )
    files["ai_macros"] = ai_macros
    return files


def generate_final_assets(
    repo_root: str | Path, selection: dict[str, Any] | None = None
) -> dict[str, Path]:
    if not selection or not selection.get("runs"):
        raise PendingDecisionError(
            ["D_MODEL_Q1", "D_MODEL_Q2", "D_MODEL_Q3", "D_MODEL_Q4_2", "D_MODEL_Q4_3", "D_EVAL"],
            "final paper assets require an explicit selected-run catalogue and approved decisions",
        )
    raise PendingDecisionError(
        ["D_MODEL_Q1", "D_MODEL_Q2", "D_MODEL_Q3", "D_MODEL_Q4_2", "D_MODEL_Q4_3", "D_EVAL"],
        "Stage 0 intentionally does not generate final assets",
    )


def _fmt_number(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


def generate_q1_result_tables(
    repo_root: str | Path,
    run_id: str,
    *,
    output_dir: str | Path | None = None,
    run_dir: str | Path | None = None,
) -> Path:
    """Build paper tables 1 and 2 from one explicit Q1 run snapshot."""

    repo = Path(repo_root)
    source_run = Path(run_dir) if run_dir is not None else repo / "outputs" / "runs" / "q1" / run_id
    result = load_case_result(source_run / "domain_result.json")
    if result.case_id != "q1":
        raise PendingDecisionError(["D_MODEL_Q1"], "paper Q1 tables require a Q1 run")
    if len(result.intervals) != STEPS_PER_DAY:
        raise PendingDecisionError(
            ["D_MODEL_Q1"], "paper Q1 tables require exactly 144 interval results"
        )
    summary_path = source_run / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}

    table1_rows: list[tuple[str, str]] = []
    for label, slot in (
        ("10:00--10:10", 60),
        ("12:00--12:10", 72),
        ("14:00--14:10", 84),
        ("16:00--16:10", 96),
        ("18:00--18:10", 108),
        ("20:00--20:10", 120),
    ):
        table1_rows.append((label, _fmt_number(result.intervals[slot].planned_purchase_kwh)))
    total_purchase = sum(interval.planned_purchase_kwh for interval in result.intervals)
    total_cost = float(summary.get("total_planned_cost_cny", 0.0))

    table2_rows: list[tuple[str, str, str]] = []
    for block, label in enumerate(
        ("0:00--4:00", "4:00--8:00", "8:00--12:00", "12:00--16:00", "16:00--20:00", "20:00--24:00")
    ):
        block_intervals = result.intervals[block * 24 : (block + 1) * 24]
        charge = sum(interval.action.charge_kwh for interval in block_intervals)
        discharge = sum(interval.action.discharge_kwh for interval in block_intervals)
        table2_rows.append((label, _fmt_number(charge), _fmt_number(discharge)))
    initial_energy = result.intervals[0].state_start.energy_kwh
    terminal_energy = result.intervals[-1].state_end.energy_kwh

    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        r"\caption{Q1 指定区间购电量、全天购电量和购电费（来自 run " + run_id + r"）}",
        r"\begin{tabular}{lrlrlr}",
        r"\toprule",
        r"时间段 & 购电量 & 时间段 & 购电量 & 时间段 & 购电量 \\",
        r"\midrule",
    ]
    for index in range(0, len(table1_rows), 3):
        group = list(table1_rows[index : index + 3])
        while len(group) < 3:
            group.append(("", ""))
        lines.append(
            rf"{group[0][0]} & {group[0][1]} & {group[1][0]} & {group[1][1]} & {group[2][0]} & {group[2][1]} \\"
        )
    lines.append(
        rf"全天购电量 & {_fmt_number(total_purchase)} & & & 全天购电费 & {_fmt_number(total_cost)} \\"
    )
    lines.extend([r"\bottomrule", r"\end{tabular}", r"\end{table}", ""])
    lines.extend(
        [
            r"\begin{table}[htbp]",
            r"\centering",
            r"\caption{Q1 四小时充放电量与 0:00/24:00 储电量}",
            r"\begin{tabular}{lrr}",
            r"\toprule",
            r"时间段 & 充电量 & 放电量 \\",
            r"\midrule",
        ]
    )
    for label, charge, discharge in table2_rows:
        lines.append(rf"{label} & {charge} & {discharge} \\")
    lines.append(rf"0:00 储电量 & {_fmt_number(initial_energy)} & \\")
    lines.append(rf"24:00 储电量 & {_fmt_number(terminal_energy)} & \\")
    lines.extend([r"\bottomrule", r"\end{tabular}", r"\end{table}", ""])
    lines.append(r"\newcommand{\QOneRunId}{" + run_id + "}")
    lines.append(r"\newcommand{\QOneTotalPurchaseKwh}{" + _fmt_number(total_purchase) + "}")
    lines.append(r"\newcommand{\QOneTotalCostCny}{" + _fmt_number(total_cost) + "}")

    destination = Path(output_dir) if output_dir is not None else repo / "paper" / "generated"
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "q1_result_tables.tex"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target
