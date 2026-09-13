"""Draw manuscript figures from the selected Q1 run and bound Q4 evidence.

This script reads saved results, checks their provenance, and does not solve
models or approve result selections. Original evidence figures are preserved.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import tomllib
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

ROOT = Path(__file__).resolve().parents[1]
BLUE = "#356485"
ORANGE = "#bb8440"
GREEN = "#518272"
RED = "#ab5656"
GRAY = "#737373"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def configure_style() -> None:
    # Use a Song TrueType font for portable, searchable PDF chart text.
    candidates = (
        Path("/mnt/c/Windows/Fonts/simsun.ttc"),
        Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
        Path("/usr/share/fonts/truetype/arphic-gbsn00lp/gbsn00lp.ttf"),
    )
    font = next((path for path in candidates if path.is_file()), candidates[-1])
    if font.is_file():
        font_manager.fontManager.addfont(str(font))
        family = font_manager.FontProperties(fname=str(font)).get_name()
    else:
        family = "AR PL SungtiL GB"
    plt.rcParams.update(
        {
            "font.family": [family, "DejaVu Serif"],
            "font.size": 11,
            "axes.labelsize": 11,
            "axes.titlesize": 11,
            "axes.linewidth": 0.65,
            "axes.edgecolor": "#555555",
            "axes.unicode_minus": False,
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            "xtick.direction": "in",
            "ytick.direction": "in",
            "legend.fontsize": 10,
            "legend.frameon": False,
            "lines.linewidth": 1.4,
            "grid.linewidth": 0.45,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "savefig.facecolor": "white",
        }
    )


def time_axis(axis, xlabel: bool = False) -> None:
    axis.set_xlim(0, 24)
    ticks = np.arange(0, 25, 4)
    axis.set_xticks(ticks, [f"{int(t):02d}:00" for t in ticks])
    axis.grid(axis="y", color="#dedede", linestyle=":")
    if xlabel:
        axis.set_xlabel("日内时刻")


def panel(axis, text: str) -> None:
    axis.set_title(text, loc="left", pad=7)


def stairs(axis, values, edges, **kwargs) -> None:
    # Unfilled lines must not invent jumps to zero at the day's endpoints.
    baseline = 0 if kwargs.get("fill", False) else None
    axis.stairs(values, edges, baseline=baseline, **kwargs)


def save(fig, output: Path, stem: str, figures: list, **source) -> None:
    files = {}
    for ext in ("pdf", "png"):
        path = output / f"{stem}.{ext}"
        fig.savefig(
            path,
            dpi=300,
            bbox_inches="tight",
            pad_inches=0.06,
            metadata={"Creator": "Matplotlib", "CreationDate": None} if ext == "pdf" else {},
        )
        files[path.relative_to(ROOT).as_posix()] = sha256(path)
    plt.close(fig)
    figures.append({"name": stem, **source, "files": files})


def q1_figures(run_id: str, output: Path, figures: list) -> dict:
    directory = ROOT / "outputs" / "runs" / "q1" / run_id
    source = read_json(directory / "input_snapshot.json")
    result = read_json(directory / "domain_result.json")
    summary = read_json(directory / "summary.json")
    validation = read_json(directory / "validation.json")
    if (
        result["case_id"] != "q1"
        or result["run_id"] != run_id
        or result["status"] != "success"
        or result["is_synthetic"] is not False
        or summary["run_id"] != run_id
        or summary["is_synthetic"] is not False
        or not summary["validation_ok"]
        or not validation["ok"]
        or source["source_sha256"] != summary["source_sha256"]
    ):
        raise ValueError("Q1 figures require one validated, non-synthetic Q1 run")
    rows, inputs = result["intervals"], source["intervals"]
    if len(rows) != 144 or len(inputs) != 144:
        raise ValueError("Q1 requires 144 input and result intervals")
    for k, (row, item) in enumerate(zip(rows, inputs, strict=True)):
        if row["slot"] != k or item["slot"] != k:
            raise ValueError("Q1 interval order mismatch")
        for actual, expected in (
            (row["load_kw"], item["load_kw"]),
            (row["pv_kw"], item["pv_forecast_kw"]),
        ):
            if not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-9):
                raise ValueError("Q1 input/result mismatch")
    x = np.arange(145) / 6
    load = np.array([r["load_kw"] for r in rows])
    pv = np.array([r["pv_kw"] for r in rows])
    prices = np.array([r["price_cny_per_kwh"] for r in inputs])
    purchase = np.array([r["planned_purchase_kwh"] for r in rows])
    charge = np.array([r["action"]["charge_kwh"] for r in rows])
    discharge = np.array([r["action"]["discharge_kwh"] for r in rows])
    used = np.array([r["pv_used_kwh"] for r in rows])
    energy = np.array(
        [rows[0]["state_start"]["energy_kwh"]] + [r["state_end"]["energy_kwh"] for r in rows]
    )
    balances = purchase + discharge + used - load / 6 - charge
    dynamics = np.diff(energy) - 0.9 * charge + discharge / 0.9
    if max(abs(balances)) > 1e-6 or max(abs(dynamics)) > 1e-6:
        raise ValueError("Q1 independent energy checks failed")
    if not math.isclose(
        float(prices @ purchase), summary["total_planned_cost_cny"], rel_tol=0, abs_tol=1e-6
    ):
        raise ValueError("Q1 cost mismatch")
    if np.any((charge > 1e-6) & (discharge > 1e-6)):
        raise ValueError("Q1 simultaneous charging and discharging")
    if np.any(energy < 1200 - 1e-6) or np.any(energy > 10800 + 1e-6):
        raise ValueError("Q1 energy boundary violation")
    binding = {"case_id": "q1", "run_id": run_id, "interval_count": 144, "state_count": 145}

    fig, axes = plt.subplots(2, 1, figsize=(7.0, 4.2), sharex=True, layout="constrained")
    stairs(axes[0], load, x, label="负载功率", color=BLUE)
    stairs(axes[0], pv, x, label="光伏预测功率", color=ORANGE, linestyle="--")
    axes[0].set_ylabel("功率 / kW")
    axes[0].legend(loc="upper left", ncol=2)
    panel(axes[0], "(a) 负载与光伏预测")
    stairs(axes[1], prices, x, color=GRAY)
    axes[1].set_ylabel("电价 / (元/kWh)")
    axes[1].set_ylim(0, max(prices) * 1.12)
    panel(axes[1], "(b) 十分钟电价")
    for axis in axes:
        time_axis(axis)
    axes[-1].set_xlabel("日内时刻")
    save(fig, output, "q1_input", figures, **binding)

    fig, axes = plt.subplots(2, 1, figsize=(7.0, 4.2), sharex=True, layout="constrained")
    stairs(axes[0], purchase, x, color=BLUE, fill=True, alpha=0.22)
    stairs(axes[0], purchase, x, color=BLUE)
    axes[0].set_ylabel("购电量 / kWh")
    panel(axes[0], "(a) 十分钟计划购电量")
    stairs(axes[1], charge, x, color=GREEN, fill=True, alpha=0.24)
    stairs(axes[1], -discharge, x, color=RED, fill=True, alpha=0.24)
    stairs(axes[1], charge, x, color=GREEN, label="充电量（正值）")
    stairs(axes[1], -discharge, x, color=RED, label="放电量（负值）")
    axes[1].axhline(0, color=GRAY, lw=0.7)
    axes[1].set_ylabel("充放电量 / kWh")
    axes[1].set_ylim(-1100, 1350)
    axes[1].legend(loc="upper right", ncol=2)
    panel(axes[1], "(b) 十分钟充放电计划")
    for axis in axes:
        time_axis(axis)
    axes[-1].set_xlabel("日内时刻")
    save(fig, output, "q1_dispatch", figures, **binding)

    fig, axis = plt.subplots(figsize=(7.0, 3.05), layout="constrained")
    axis.axhspan(1200, 10800, facecolor=BLUE, alpha=0.035)
    axis.plot(x, energy, color=BLUE, label="储电量")
    for value, label in ((1200, "运行下界 1200"), (10800, "运行上界 10800")):
        axis.axhline(value, color=GRAY, ls="--", lw=0.9)
        axis.text(23.6, value + 200, label, ha="right", va="bottom", fontsize=10)
    axis.axhline(6000, color=GRAY, ls=":", lw=0.8)
    axis.scatter([0, 24], [energy[0], energy[-1]], color=BLUE, s=28, zorder=4, clip_on=False)
    axis.annotate(
        "日初、日末均为 6000 kWh",
        xy=(24, 6000),
        xytext=(12.5, 7100),
        arrowprops={"arrowstyle": "->", "color": GRAY, "lw": 0.8},
        fontsize=10,
    )
    axis.set_ylim(0, 12400)
    axis.set_yticks([1200, 3600, 6000, 8400, 10800])
    axis.set_ylabel("储电量 / kWh")
    time_axis(axis, xlabel=True)
    save(fig, output, "q1_soc", figures, **binding)

    fig, axis = plt.subplots(figsize=(7.0, 2.8))
    axis.set_xlim(0, 10)
    axis.set_ylim(0, 4)
    axis.axis("off")
    positions = [(1.55, 3), (5, 3), (8.45, 3), (8.45, 1), (5, 1), (1.55, 1)]
    texts = [
        "输入电价、负载、光伏\n统一144个区间",
        "功率换算为区间电量\n区分母线与电池状态",
        "建立目标函数与约束\n供需与储能运行限制",
        "HiGHS 联合求解\n144格计划、145个状态",
        "独立复算\n物理约束与购电费用",
        "汇总指定时段结果\n输出购电与储能计划",
    ]
    for i, ((cx, cy), text) in enumerate(zip(positions, texts, strict=True)):
        axis.add_patch(
            FancyBboxPatch(
                (cx - 1.42, cy - 0.48),
                2.84,
                0.96,
                boxstyle="round,pad=0.04,rounding_size=0.06",
                edgecolor=BLUE if i in (2, 3) else GRAY,
                facecolor="#edf2f5" if i in (2, 3) else "#f8f8f8",
                linewidth=0.9,
            )
        )
        axis.text(cx, cy, text, ha="center", va="center", fontsize=10, linespacing=1.5)
    connections = [
        ((3.02, 3), (3.53, 3)),
        ((6.47, 3), (6.98, 3)),
        ((8.45, 2.48), (8.45, 1.52)),
        ((6.98, 1), (6.47, 1)),
        ((3.53, 1), (3.02, 1)),
    ]
    for start, end in connections:
        axis.add_patch(
            FancyArrowPatch(
                start, end, arrowstyle="-|>", mutation_scale=11, linewidth=0.9, color=GRAY
            )
        )
    save(fig, output, "q1_workflow", figures, **binding, kind="model_workflow")
    return {
        name: sha256(directory / name)
        for name in ("input_snapshot.json", "domain_result.json", "summary.json", "validation.json")
    }


def q4_display_day(case: str, directory: Path, result: dict, output: Path, figures: list) -> None:
    date = "2025-12-21"
    rows = [row for row in result["intervals"] if row["day"] == date]
    if len(rows) != 144 or [row["slot"] for row in rows] != list(range(144)):
        raise ValueError("Q4 display day coverage mismatch")
    prices = []
    with (directory / "cost_ledger.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            bill = json.loads(line)
            if bill["slot_start"].startswith(date):
                prices.append(bill["execution_price"])
    if len(prices) != 144:
        raise ValueError("Q4 display day price coverage mismatch")
    x = np.arange(145) / 6
    fig, axes = plt.subplots(5, 1, figsize=(7.0, 8.2), sharex=True, layout="constrained")
    stairs(axes[0], [r["load_kw"] for r in rows], x, color=BLUE, label="实际负载")
    stairs(axes[0], [r["pv_kw"] for r in rows], x, color=ORANGE, linestyle="--", label="实际光伏")
    axes[0].set_ylabel("功率 / kW")
    axes[0].legend(loc="upper left", ncol=2)
    panel(axes[0], "(a) 实际负载与光伏")
    stairs(axes[1], prices, x, color=GRAY)
    axes[1].set_ylabel("电价 / (元/kWh)")
    axes[1].set_ylim(0, max(prices) * 1.12)
    panel(axes[1], "(b) 实际电价")
    stairs(
        axes[2],
        [r["planned_purchase_kwh"] for r in rows],
        x,
        color=BLUE,
        linestyle="--",
        label="初始购电合同",
    )
    stairs(
        axes[2],
        [r["adjusted_purchase_kwh"] for r in rows],
        x,
        color=GREEN,
        linewidth=1.1,
        label="最终确认合同",
    )
    stairs(
        axes[2],
        [r["emergency_purchase_kwh"] for r in rows],
        x,
        color=RED,
        fill=True,
        alpha=0.35,
        label="紧急购电",
    )
    axes[2].set_ylabel("购电量 / kWh")
    axes[2].legend(loc="upper left", ncol=3, fontsize=9)
    panel(axes[2], "(c) 十分钟合同与紧急购电")
    stairs(
        axes[3],
        [r["action"]["charge_kwh"] for r in rows],
        x,
        color=GREEN,
        label="充电量（正值）",
    )
    stairs(
        axes[3],
        [-r["action"]["discharge_kwh"] for r in rows],
        x,
        color=RED,
        label="放电量（负值）",
    )
    axes[3].axhline(0, color=GRAY, lw=0.65)
    axes[3].set_ylabel("充放电量 / kWh")
    axes[3].set_ylim(-1100, 1550)
    axes[3].legend(loc="upper right", ncol=2, fontsize=9)
    panel(axes[3], "(d) 实际充放电")
    energy = [rows[0]["state_start"]["energy_kwh"]] + [r["state_end"]["energy_kwh"] for r in rows]
    axes[4].plot(x, energy, color=BLUE)
    for bound in (1200, 10800):
        axes[4].axhline(bound, color=GRAY, ls="--", lw=0.8)
    axes[4].set_ylim(0, 12300)
    axes[4].set_yticks([1200, 6000, 10800])
    axes[4].set_ylabel("储电量 / kWh")
    panel(axes[4], "(e) 连续运行的储电量")
    for axis in axes:
        time_axis(axis)
    axes[-1].set_xlabel("日内时刻")
    save(
        fig,
        output,
        f"{case}_{date}",
        figures,
        case_id=case,
        run_id=result["run_id"],
        date=date,
        interval_count=144,
        state_count=145,
    )


def q4_figures(evidence_path: Path, output: Path, figures: list) -> dict:
    evidence = read_json(evidence_path)
    if "cases" in evidence and "criterion" in evidence:
        from build_q4_paper_assets import build_q4_assets

        return build_q4_assets(evidence_path, output, figures)
    if evidence["is_synthetic"] is not False or evidence["continuous_days"] != 334:
        raise ValueError("Q4 requires non-synthetic, complete replay evidence")
    records = {(r["case_id"], r["price_method"]): r for r in evidence["runs"]}
    for (case, method), record in records.items():
        summary = record["summary"]
        directory = ROOT / "outputs" / "runs" / case / record["run_id"]
        if (
            not summary["full_annual"]
            or summary["interval_count"] != 48096
            or not record["independent_validation"]["ok"]
        ):
            raise ValueError("Q4 incomplete or invalid replay")
        if sha256(directory / "summary.json") != record["artifact_hashes"]["summary.json"]:
            raise ValueError("Q4 summary provenance mismatch")
        if method != "main":
            continue
        for name in ("domain_result.json", "cost_ledger.jsonl"):
            if sha256(directory / name) != record["artifact_hashes"][name]:
                raise ValueError(f"Q4 source hash mismatch: {case}/{name}")
        result = read_json(directory / "domain_result.json")
        if (
            result["is_synthetic"] is not False
            or result["run_id"] != record["run_id"]
            or result["case_id"] != case
            or result["status"] != "success"
        ):
            raise ValueError("Q4 result identity mismatch")
        q4_display_day(case, directory, result, output, figures)

    fig, axes = plt.subplots(1, 2, figsize=(7.0, 3.0), layout="constrained")
    main = records[("q4_2", "main")]["summary"]["prediction_metrics"]
    lag = records[("q4_2", "lag1")]["summary"]["prediction_metrics"]
    categories = np.arange(2)
    for values, offset, color, label in (
        (main, -0.17, BLUE, "主价格预测"),
        (lag, 0.17, ORANGE, "前一日同刻价格"),
    ):
        heights = [values[f"price_cny_per_kwh:{window}"]["mae"] for window in ("6h", "24h")]
        axes[0].bar(categories + offset, heights, width=0.31, color=color, alpha=0.85, label=label)
        for cx, height in zip(categories + offset, heights, strict=True):
            axes[0].text(cx, height + 0.002, f"{height:.4f}", ha="center", fontsize=9)
    axes[0].set_xticks(categories, ["6小时", "24小时"])
    axes[0].set_ylim(0, 0.118)
    axes[0].set_ylabel("电价 MAE / (元/kWh)")
    axes[0].legend(loc="upper left", fontsize=9)
    panel(axes[0], "(a) 电价预测误差")
    for case, color, marker, label in (
        ("q4_2", BLUE, "o", "问题四（2）"),
        ("q4_3", RED, "s", "问题四（3）"),
    ):
        amounts = {}
        for row in evidence["monthly_costs"]:
            if row["case_id"] == case:
                amounts[(row["month"], row["price_method"])] = row["total_cost_cny"]
        months = [f"2025-{month:02d}" for month in range(2, 13)]
        differences = [
            (amounts[(month, "main")] - amounts[(month, "lag1")]) / 1e4 for month in months
        ]
        axes[1].plot(
            range(2, 13), differences, color=color, marker=marker, markersize=3.5, label=label
        )
    axes[1].axhline(0, color=GRAY, ls="--", lw=0.8)
    axes[1].set_xticks([2, 4, 6, 8, 10, 12])
    axes[1].set_xlabel("月份（2025年）")
    axes[1].set_ylabel("月费用差 / 万元")
    axes[1].legend(loc="lower left", fontsize=9)
    panel(axes[1], "(b) 月购电费用差")
    for axis in axes:
        axis.grid(axis="y", color="#dedede", linestyle=":")
    save(
        fig,
        output,
        "q4_price_cost_comparison",
        figures,
        run_ids=[record["run_id"] for record in records.values()],
        kind="price_and_monthly_cost_comparison",
    )
    q4_paper_tables(evidence_path, evidence, records)
    return {
        "comparison_sha256": sha256(evidence_path),
        "run_ids": [r["run_id"] for r in records.values()],
    }


def q4_paper_tables(evidence_path: Path, evidence: dict, records: dict) -> None:
    lines = [
        "% Presentation derived from saved Q4 evidence; result selection is unchanged.",
        f"% comparison.json SHA-256: {sha256(evidence_path)}",
        r"\subsection{回放结果与分析}",
        "四条轨迹均从6\\,000 kWh独立连续运行，覆盖2025年2月至12月的334日、"
        "48\\,096个区间。实际末态均为6\\,000 kWh，连续状态、合同权限及实际费用复核通过。"
        "表\\ref{tab:q4-annual}给出费用分项，表\\ref{tab:q4-prediction}给出预测误差。",
        r"\begin{table}[H]\centering\small",
        r"\caption{问题四回放期购电费用及价格对照（单位：万元）}\label{tab:q4-annual}",
        r"\begin{tabular}{llrrrr}\toprule",
        r"合同条件 & 价格预测 & 计划费 & 调整费 & 紧急费 & 总费 \\ \midrule",
    ]
    for (case, method), record in records.items():
        summary = record["summary"]
        costs = summary["costs"]
        label = "合同冻结" if case == "q4_2" else "允许调整"
        method_label = "主方案" if method == "main" else "前一日同刻"
        values = [
            costs["planned_cost_cny"],
            costs["adjustment_cost_cny"],
            costs["emergency_cost_cny"],
            summary["total_cost_cny"],
        ]
        lines.append(
            label
            + " & "
            + method_label
            + " & "
            + " & ".join(f"{value / 1e4:.4f}" for value in values)
            + r" \\"
        )
    lines.extend(
        [
            r"\bottomrule\end{tabular}\end{table}",
            "",
            r"\begin{table}[H]\centering\small",
            r"\caption{问题四的6小时与24小时预测误差}\label{tab:q4-prediction}",
            r"\begin{tabular}{lrrrr}\toprule",
            r"指标与条件 & 6小时MAE & 6小时RMSE & 24小时MAE & 24小时RMSE \\ \midrule",
        ]
    )
    specifications = [
        ("负载（kW，两类条件）", "q4_2", "main", "load_kw", 2),
        ("光伏（kW，合同冻结）", "q4_2", "main", "pv_kw", 2),
        ("光伏（kW，允许调整）", "q4_3", "main", "pv_kw", 2),
        ("电价（元/kWh，主方案）", "q4_2", "main", "price_cny_per_kwh", 4),
        ("电价（元/kWh，价格对照）", "q4_2", "lag1", "price_cny_per_kwh", 4),
    ]
    for label, case, method, variable, digits in specifications:
        metrics = records[(case, method)]["summary"]["prediction_metrics"]
        values = [
            metrics[f"{variable}:{window}"][metric]
            for window in ("6h", "24h")
            for metric in ("mae", "rmse")
        ]
        lines.append(label + " & " + " & ".join(f"{value:.{digits}f}" for value in values) + r" \\")
    lines.extend(
        [
            r"\bottomrule\end{tabular}\end{table}",
            "",
            "6小时误差取每次刷新后首36格，共48\\,096个非重叠样本；24小时误差按发布时间与目标区间对计数，"
            "共192\\,168个样本，窗口重叠并截至观测末端。价格预测和负载预测在两类合同条件间相同。",
        ]
    )
    lines.append("")
    for comparison in evidence["comparisons"]:
        if not comparison["annual_comparison_valid"]:
            raise ValueError("Q4 paper tables require complete annual comparisons")
        case = comparison["case_id"]
        label = "合同冻结" if case == "q4_2" else "允许调整"
        difference = -comparison["main_minus_lag1_cost_cny"]
        percentage = -comparison["main_minus_lag1_cost_percent"]
        emergency = (
            records[(case, "main")]["summary"]["emergency_kwh"]
            - records[(case, "lag1")]["summary"]["emergency_kwh"]
        )
        lines.append(
            f"{label}条件下，主方案相对价格对照的总费用减少{difference:,.2f}元（{percentage:.3f}\\%），"
            f"但紧急购电量增加{emergency:,.2f} kWh。".replace(",", r"\,")
        )
    lines.extend(
        [
            "",
            "图\\ref{fig:q4-price-cost}将价格误差与月费用差并列。主方案的价格MAE较低，但各月费用差仍有波动。"
            "这说明价格预测改善会改变合同与储能安排，其收益需按实际账单评价，不能仅由误差大小推定。",
            r"\begin{figure}[H]\centering",
            r"\PaperGraphic[width=0.98\linewidth]{figures/q4_price_cost_comparison.pdf}",
            r"\caption{问题四的价格预测误差与月费用差（费用差为主方案减价格对照）}\label{fig:q4-price-cost}",
            r"\end{figure}",
            "",
            "允许调整条件下的光伏MAE在6小时窗口为83.15 kW，低于合同冻结条件的153.40 kW；"
            "在24小时窗口则为182.64 kW，高于后者的153.50 kW。因此，短窗与长窗需分别讨论。"
            "两类合同条件同时改变光伏信息和调整权限，其费用差不能单独归因于官方预报。",
            "",
            "图\\ref{fig:q4-freeze}与图\\ref{fig:q4-adjust}展示12月21日从完整轨迹提取的结果。"
            "合同冻结条件下初始与最终合同重合；允许调整条件下，最终确认量随预报更新而改变。"
            "图中储电量沿用此前实际状态，未为展示日重新设定初态。",
        ]
    )
    for case, label, name in (
        ("q4_2", "冻结合同", "fig:q4-freeze"),
        ("q4_3", "允许调整合同", "fig:q4-adjust"),
    ):
        lines.extend(
            [
                r"\begin{figure}[!htbp]\centering",
                rf"\PaperGraphic[width=0.94\linewidth]{{figures/{case}_2025-12-21.pdf}}",
                rf"\caption{{问题四{label}条件下12月21日的实际运行结果}}\label{{{name}}}",
                r"\end{figure}",
            ]
        )
    lines.append(
        "本节为已有完整回放的候选结果，最终运行选择及人工核验仍待参赛队确认；"
        "四条运行编号及未舍入账单见支撑材料的回放证据文件。"
    )
    target = ROOT / "paper/tables/q4_results_paper.tex"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--q1-run", default=None)
    parser.add_argument(
        "--q4-evidence",
        type=Path,
        default=Path("outputs/evidence/q4_all_optimization_completion/selection.json"),
    )
    args = parser.parse_args()
    selection = tomllib.loads((ROOT / "configs/selected_runs.toml").read_text())
    run_id = args.q1_run or selection["runs"]["q1"]
    configure_style()
    output = ROOT / "paper/figures"
    output.mkdir(parents=True, exist_ok=True)
    figures = []
    q1 = q1_figures(run_id, output, figures)
    evidence_path = args.q4_evidence if args.q4_evidence.is_absolute() else ROOT / args.q4_evidence
    q4 = q4_figures(evidence_path, output, figures) if evidence_path.is_file() else None
    manifest = {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "is_synthetic": False,
        "result_selection_changed": False,
        "source_script_sha256": sha256(Path(__file__)),
        "q1": {"run_id": run_id, "source_hashes": q1},
        "q4": q4,
        "figures": figures,
    }
    (output / "figure_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Generated {len(figures)} figures (PDF + 300 dpi PNG): {output}")


if __name__ == "__main__":
    main()
