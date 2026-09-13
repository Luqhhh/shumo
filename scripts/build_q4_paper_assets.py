"""Render the explicitly selected Q4 scenario evidence without running a model."""

from __future__ import annotations

import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from build_paper_figures import (
    BLUE,
    GRAY,
    ORANGE,
    RED,
    ROOT,
    panel,
    q4_display_day,
    read_json,
    save,
    sha256,
)
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

COMPARISON_ROOT = ROOT / "outputs/evidence/q4_scenario_procurement/revision_02"
COST_KEYS = ("planned_cost_cny", "adjustment_cost_cny", "emergency_cost_cny")


def checked_run(directory: Path, case: str, run_id: str, source_hashes: dict) -> dict:
    """Bind plot sources to the archived runtime's hashes and independent audit."""
    manifest = read_json(directory / "manifest.json")
    if any(
        manifest.get(k) != v
        for k, v in {
            "case_id": case,
            "run_id": run_id,
            "status": "success",
            "is_synthetic": False,
        }.items()
    ):
        raise ValueError(f"Q4 run identity/status mismatch: {directory}")
    if sha256(directory / "evidence_manifest.json") != manifest["evidence_manifest_sha256"]:
        raise ValueError("Q4 evidence manifest hash mismatch")
    evidence = read_json(directory / "evidence_manifest.json")
    # Stream all bound artifacts, including forecasts and complete native plans.
    for name, binding in evidence["artifacts"].items():
        actual = sha256(directory / name)
        if actual != binding["sha256"]:
            raise ValueError(f"Q4 artifact changed: {directory}/{name}")
        source_hashes[str((directory / name).relative_to(ROOT))] = actual
    plans = evidence["dispatch_plans"]
    if sha256(directory / plans["name"]) != plans["sha256"]:
        raise ValueError("Q4 full dispatch plan hash mismatch")
    source_hashes[str((directory / plans["name"]).relative_to(ROOT))] = plans["sha256"]
    summary = read_json(directory / "summary.json")
    validation = read_json(directory / "validation.json")
    audit = evidence["controller_audit"]
    if not (
        summary["full_annual"]
        and summary["interval_count"] == 48096
        and validation["ok"]
        and validation["checked_intervals"] == 48096
        and audit["ok"]
        and audit["checked_intervals"] == 48096
        and audit["causal_forecasts_ok"]
        and audit["intent_plan_binding_ok"]
        and abs(summary["terminal_energy_kwh"] - 6000) <= 1e-6
    ):
        raise ValueError("Q4 incomplete annual or failed physical/control-chain audit")
    if not math.isclose(
        sum(summary["costs"].values()), summary["total_cost_cny"], abs_tol=1e-6, rel_tol=0
    ):
        raise ValueError("Q4 cost components disagree")
    return summary


def monthly_cost(directory: Path) -> dict:
    import json

    amounts = {f"2025-{month:02d}": 0.0 for month in range(2, 13)}
    counts = {month: 0 for month in amounts}
    with (directory / "cost_ledger.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            month = row["slot_start"][:7]
            amounts[month] += sum(row[key] for key in COST_KEYS)
            counts[month] += 1
    if sum(counts.values()) != 48096 or not all(counts.values()):
        raise ValueError("Q4 monthly ledger coverage mismatch")
    return amounts


def comparison_figure(records: dict, output: Path, figures: list) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 3.5), layout="constrained")
    positions = [0, 1, 2.6, 3.6]
    keys = [(case, method) for case in ("q4_2", "q4_3") for method in ("baseline", "candidate")]
    bottom = np.zeros(4)
    for field, color, label in zip(
        COST_KEYS, (BLUE, ORANGE, RED), ("初始合同费", "调整费", "紧急费"), strict=True
    ):
        heights = [records[key]["summary"]["costs"][field] / 1e4 for key in keys]
        axes[0].bar(
            positions, heights, bottom=bottom, width=0.72, color=color, alpha=0.85, label=label
        )
        bottom += heights
    for x, total in zip(positions, bottom, strict=True):
        axes[0].text(x, total + 25, f"{total:.2f}", ha="center", fontsize=9)
    axes[0].set_xticks(positions, ["点预测", "情景采购", "点预测", "情景采购"], fontsize=9)
    axes[0].set_xlabel("问题四（2）                 问题四（3）")
    axes[0].set_ylabel("回放期费用 / 万元")
    axes[0].set_ylim(0, 2100)
    axes[0].legend(loc="upper left", ncol=3, fontsize=8)
    panel(axes[0], "(a) 实际费用分解")
    for case, color, marker, label in (
        ("q4_2", BLUE, "o", "问题四（2）"),
        ("q4_3", RED, "s", "问题四（3）"),
    ):
        before = records[(case, "baseline")]["monthly"]
        after = records[(case, "candidate")]["monthly"]
        differences = [(after[month] - before[month]) / 1e4 for month in before]
        axes[1].plot(
            range(2, 13), differences, color=color, marker=marker, markersize=3.5, label=label
        )
    axes[1].axhline(0, color=GRAY, ls="--", lw=0.8)
    axes[1].set_xticks([2, 4, 6, 8, 10, 12])
    axes[1].set_xlabel("月份（2025年）")
    axes[1].set_ylabel("月费用差 / 万元")
    axes[1].legend(loc="lower left", fontsize=9)
    panel(axes[1], "(b) 情景采购减点预测基准")
    for axis in axes:
        axis.grid(axis="y", color="#dedede", linestyle=":")
        axis.set_axisbelow(True)
    save(
        fig,
        output,
        "q4_procurement_comparison",
        figures,
        run_ids=[r["run_id"] for r in records.values()],
        kind="actual_cost_components_and_monthly_delta",
    )


def workflow_figure(run_ids: list, output: Path, figures: list) -> None:
    fig, axis = plt.subplots(figsize=(7.0, 2.8))
    axis.set_xlim(0, 10)
    axis.set_ylim(0, 4)
    axis.axis("off")
    texts = [
        "可见历史与已发布预报\n生成并冻结原点预测",
        "已实现联合误差\n28日窗口、原时距分组",
        "构造低、名义、高情景\n权重 1/4、1/2、1/4",
        "求解情景混合整数模型\n共同合同及充放电意图",
        "执行共同首区间动作\n实际供需保护、费用结算",
        "传递实际储电量\n更新已实现误差样本",
    ]
    positions = [(1.55, 3), (5, 3), (8.45, 3), (8.45, 1), (5, 1), (1.55, 1)]
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
        axis.text(cx, cy, text, ha="center", va="center", fontsize=9, linespacing=1.5)
    for start, end in (
        ((3.02, 3), (3.53, 3)),
        ((6.47, 3), (6.98, 3)),
        ((8.45, 2.48), (8.45, 1.52)),
        ((6.98, 1), (6.47, 1)),
        ((3.53, 1), (3.02, 1)),
    ):
        axis.add_patch(
            FancyArrowPatch(
                start, end, arrowstyle="-|>", mutation_scale=11, linewidth=0.9, color=GRAY
            )
        )
    axis.add_patch(
        FancyArrowPatch(
            (1.55, 1.53),
            (1.55, 2.47),
            arrowstyle="-|>",
            mutation_scale=11,
            linewidth=0.9,
            color=BLUE,
            linestyle="--",
        )
    )
    save(
        fig,
        output,
        "q4_scenario_workflow",
        figures,
        run_ids=run_ids,
        kind="scenario_model_workflow",
    )


def paper_tables(selection_path: Path, records: dict, comparisons: dict) -> dict:
    lines = [
        "% Explicitly bound scenario-procurement results; no model run or approval mutation.",
        f"% selection SHA-256: {sha256(selection_path)}",
        *[f"% {case}: {records[(case, 'candidate')]['run_id']}" for case in comparisons],
        r"\subsection{回放结果与分析}",
        "点预测基准与情景采购均从6\\,000 kWh初态独立连续运行，覆盖2025年2月至12月的334日、"
        "48\\,096个区间，实际末态均为6\\,000 kWh。表\\ref{tab:q4-annual}给出同条件实际费用。",
        r"\begin{table}[H]\centering\small",
        r"\caption{问题四点预测基准与情景采购的实际费用（单位：万元）}\label{tab:q4-annual}",
        r"\begin{tabular}{llrrrr}\toprule",
        r"合同条件 & 采购方案 & 初始合同费 & 调整费 & 紧急费 & 总费 \\ \midrule",
    ]
    for case in comparisons:
        for method, label in (("baseline", "点预测基准"), ("candidate", "情景采购")):
            summary = records[(case, method)]["summary"]
            values = [summary["costs"][key] / 1e4 for key in COST_KEYS] + [
                summary["total_cost_cny"] / 1e4
            ]
            contract = "每日冻结" if case == "q4_2" else "允许调整"
            lines.append(
                contract + " & " + label + " & " + " & ".join(f"{v:.4f}" for v in values) + r" \\"
            )
        if case == "q4_2":
            lines.append(r"\midrule")
    lines += [r"\bottomrule\end{tabular}\end{table}", ""]
    for case, contract in (("q4_2", "每日冻结"), ("q4_3", "允许调整")):
        comp = comparisons[case]
        delta = comp["delta_candidate_minus_baseline"]
        saved = -delta["total_cost_cny"]
        percent = saved / comp["results"]["baseline"]["total_cost_cny"] * 100
        text = f"{contract}条件下，情景采购比点预测基准节省{saved:,.2f}元（{percent:.4f}\\%）。初始合同费增加{delta['planned_cost_cny'] / 1e4:.4f}万元，实际紧急费减少{-delta['emergency_cost_cny'] / 1e4:.4f}万元"
        if case == "q4_3":
            text += f"，调整费另减少{-delta['adjustment_cost_cny'] / 1e4:.4f}万元"
        lines.append((text + "。").replace(",", r"\,"))
    freeze_month = (
        records[("q4_2", "candidate")]["monthly"]["2025-02"]
        - records[("q4_2", "baseline")]["monthly"]["2025-02"]
    )
    adjust_month = (
        records[("q4_3", "candidate")]["monthly"]["2025-09"]
        - records[("q4_3", "baseline")]["monthly"]["2025-09"]
    )
    lines += [
        "",
        "图\\ref{fig:q4-procurement}将费用分项和各月费用差并列。初始合同费增加而紧急费减少，"
        "净节省由三项账单共同决定。"
        f"2月每日冻结方案费用比基准高{freeze_month / 1e4:.4f}万元，9月允许调整方案也高{adjust_month / 1e4:.4f}万元，"
        "其余月份费用均降低。因此，回放期节费并非逐月一致。",
        r"\begin{figure}[H]\centering",
        r"\PaperGraphic[width=0.98\linewidth]{figures/q4_procurement_comparison.pdf}",
        r"\caption{情景采购与点预测基准的实际费用分解及月费用差}\label{fig:q4-procurement}",
        r"\end{figure}",
        "",
        r"\subsubsection{供电缺口与余电代价}",
        "费用降低伴随更高的合同余电与弃光，表\\ref{tab:q4-operation}给出实际运行分项。",
        r"\begin{table}[H]\centering\small",
        r"\caption{情景采购的实际运行变化（电量单位：万kWh）}\label{tab:q4-operation}",
        r"\begin{tabular}{lrrrr}\toprule",
        r" & \multicolumn{2}{c}{每日冻结} & \multicolumn{2}{c}{允许调整} \\",
        r"\cmidrule(lr){2-3}\cmidrule(lr){4-5}",
        r"指标 & 点预测 & 情景采购 & 点预测 & 情景采购 \\ \midrule",
    ]
    for label, key in (
        ("紧急购电量", "emergency_kwh"),
        ("合同余电", "unused_contract_kwh"),
        ("弃光电量", "pv_curtailed_kwh"),
        ("电池吞吐量", "battery_throughput_kwh"),
        ("动作保护次数（次）", "protection_count"),
    ):
        values = [
            records[(case, method)]["summary"][key]
            for case in comparisons
            for method in ("baseline", "candidate")
        ]
        formatted = [f"{v:.0f}" if key == "protection_count" else f"{v / 1e4:.4f}" for v in values]
        lines.append(label + " & " + " & ".join(formatted) + r" \\")
    lines += [r"\bottomrule\end{tabular}\end{table}", ""]
    for case, label in (("q4_2", "每日冻结"), ("q4_3", "允许调整")):
        d = comparisons[case]["delta_candidate_minus_baseline"]
        lines.append(
            f"{label}条件下，紧急购电量减少{-d['emergency_kwh'] / 1e4:.4f}万kWh，但合同余电增加{d['unused_contract_kwh'] / 1e4:.4f}万kWh，弃光增加{d['pv_curtailed_kwh'] / 1e4:.4f}万kWh。"
        )
    lines += [
        "动作保护次数也有所减少，但电池吞吐量增加。当前目标未计寿命损耗，不能据总电费降低推断电池全寿命费用改善。",
        "",
        r"\subsubsection{点预测误差与候选方案比较}",
        "表\\ref{tab:q4-prediction}列出两采购方案共同的点预测误差。情景采购改变了规划中的风险处理，"
        "保留原始点预测及其来源，因而费用改善来自采购安排的变化。",
        r"\begin{table}[H]\centering\small",
        r"\caption{问题四两采购方案共同的点预测误差}\label{tab:q4-prediction}",
        r"\begin{tabular}{lrrrr}\toprule",
        r" & \multicolumn{2}{c}{6小时} & \multicolumn{2}{c}{24小时} \\",
        r"\cmidrule(lr){2-3}\cmidrule(lr){4-5}",
        r"变量 & MAE & RMSE & MAE & RMSE \\ \midrule",
    ]
    for label, case, variable, digits in (
        ("负载（kW）", "q4_2", "load_kw", 2),
        ("光伏（kW，每日冻结）", "q4_2", "pv_kw", 2),
        ("光伏（kW，允许调整）", "q4_3", "pv_kw", 2),
        ("电价（元/kWh）", "q4_2", "price_cny_per_kwh", 4),
    ):
        metrics = records[(case, "candidate")]["summary"]["prediction_metrics"]
        values = [
            metrics[f"{variable}:{window}"][metric]
            for window in ("6h", "24h")
            for metric in ("mae", "rmse")
        ]
        lines.append(label + " & " + " & ".join(f"{v:.{digits}f}" for v in values) + r" \\")
    lines += [
        r"\bottomrule\end{tabular}\end{table}",
        "6小时误差取每次刷新后首36格，共48\\,096个非重叠样本；24小时误差按发布--目标对计算，"
        "共192\\,168个样本，窗口重叠并截至观测末端。允许调整条件下，光伏6小时MAE较小，"
        "24小时MAE反而较大，需分别评价短窗和长窗。两类条件同时改变光伏信息与合同权限，"
        "不能把两类费用差单独解释为官方预报带来的收益。",
        "",
        "对其他模型机制也作了独立回放。表\\ref{tab:q4-candidates}只列完整可行候选，"
        "每行仅改变所列机制，未将其联合叠加。",
        r"\begin{table}[H]\centering\small",
        r"\caption{不同独立模型候选的回放期实际总费（单位：万元）}\label{tab:q4-candidates}",
        r"\begin{tabular}{lrr}\toprule",
        r"独立机制 & 每日冻结 & 允许调整 \\ \midrule",
    ]
    # The candidate overview is sealed in the optimization conclusion. Validate
    # source summaries before printing any of its real annual numbers.
    candidates = [
        ("点预测基准", "baseline", "baseline"),
        (
            "长窗光伏与历史基线融合",
            None,
            ROOT
            / "outputs/evidence/q4_pv_blend/revision_02/blend_runtime/outputs/runs/q4_3/q4-3-v4-pv-blend-long-annual-main-002",
        ),
        (
            "长窗光伏偏差校正",
            None,
            ROOT
            / "outputs/evidence/q4_pv_bias/revision_01/bias_runtime/outputs/runs/q4_3/q4-3-v4-pv-bias-long-annual-main-001",
        ),
        (
            "普通窗口75\\%分位数残值",
            None,
            ROOT
            / "outputs/evidence/q4_terminal_quartile/revision_01/runtime_preparation/outputs/runs/q4_3/q4-3-v4-terminal-quartile-annual-main-001",
        ),
        (
            "显式80\\%分位数采购余量",
            "q4-2-v4-safety-procurement-annual-main-002",
            "q4-3-v4-safety-procurement-annual-main-002",
        ),
        ("联合误差情景采购", "candidate", "candidate"),
    ]
    candidate_bindings = {}
    for label, *runs in candidates:
        values = []
        for case, run in zip(comparisons, runs, strict=True):
            if run is None:
                values.append("---")
                continue
            if run in ("baseline", "candidate"):
                summary = records[(case, run)]["summary"]
            else:
                directory = run if isinstance(run, Path) else ROOT / "outputs/runs" / case / run
                summary = read_json(directory / "summary.json")
                manifest = read_json(directory / "manifest.json")
                validation = read_json(directory / "validation.json")
                evidence = read_json(directory / "evidence_manifest.json")
                if not (
                    summary["full_annual"]
                    and summary["interval_count"] == 48096
                    and manifest["is_synthetic"] is False
                    and manifest["status"] == "success"
                    and validation["ok"]
                    and evidence["artifacts"]["summary.json"]["sha256"]
                    == sha256(directory / "summary.json")
                ):
                    raise ValueError("Candidate overview requires bound full annual evidence")
                candidate_bindings[str((directory / "summary.json").relative_to(ROOT))] = sha256(
                    directory / "summary.json"
                )
            values.append(f"{summary['total_cost_cny'] / 1e4:.4f}")
        lines.append(label + " & " + " & ".join(values) + r" \\")
    lines += [
        r"\bottomrule\end{tabular}\end{table}",
        "表中横线表示未开展该条件的年度试验。长窗融合与残值调整在允许调整条件下略有节费，"
        "偏差校正和显式余量则增加费用；显式余量在每日冻结条件下有所改善。"
        "本轮两类条件均采用实际总费较低的联合误差情景采购。该比较基于已查看的开发年份，"
        "尚不足以证明其他年份仍有相同收益。",
        "",
        r"\subsubsection{典型日的连续运行}",
        "图\\ref{fig:q4-freeze}与图\\ref{fig:q4-adjust}展示最新情景采购在12月21日的实际轨迹。"
        "各有144个动作区间和145个储能状态，日初沿用此前实际储电量。每日冻结条件下初始与最终合同重合；"
        "允许调整条件下可按发布预报更新尚未开始区间的合同。紧急购电及充放电均取实际执行量。",
    ]
    for case, label, reference in (
        ("q4_2", "每日冻结", "fig:q4-freeze"),
        ("q4_3", "允许调整", "fig:q4-adjust"),
    ):
        day = records[(case, "candidate")]["display_day"]
        if case == "q4_2":
            detail = f"每日冻结条件的当日合同共{day['initial_contract_kwh']:,.3f} kWh，实际紧急购电{day['emergency_kwh']:.3f} kWh。"
        else:
            detail = f"允许调整条件有{day['changed_slots']}个区间的最终合同与初始合同不同，合同总量由{day['initial_contract_kwh']:,.3f}增加至{day['final_contract_kwh']:,.3f} kWh，实际紧急购电{day['emergency_kwh']:.3f} kWh。"
        lines.append(
            (
                detail
                + f"储电量从{day['initial_energy_kwh']:,.3f}变为{day['terminal_energy_kwh']:,.3f} kWh。"
            ).replace(",", r"\,")
        )
        lines += [
            r"\begin{figure}[!htbp]\centering",
            rf"\PaperGraphic[width=0.94\linewidth]{{figures/{case}_2025-12-21.pdf}}",
            rf"\caption{{情景采购在{label}条件下12月21日的实际运行结果}}\label{{{reference}}}",
            r"\end{figure}",
        ]
    target = ROOT / "paper/tables/q4_results_paper.tex"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return candidate_bindings


def build_q4_assets(selection_path: Path, output: Path, figures: list) -> dict:
    selection = read_json(selection_path)
    completion = read_json(COMPARISON_ROOT / "validation_completion.json")
    if not completion["ok"] or set(selection["cases"]) != {"q4_2", "q4_3"}:
        raise ValueError("Q4 explicit selection or completion invalid")
    records, comparisons, source_hashes = {}, {}, {}
    for case in ("q4_2", "q4_3"):
        comp_path = COMPARISON_ROOT / f"{case}_annual_comparison.json"
        comp = read_json(comp_path)
        run_id = selection["cases"][case]["run_id"]
        if not (
            comp["ok"]
            and comp["full_annual"]
            and comp["candidate_run_id"] == run_id
            and completion["cases"][case]["run_id"] == run_id
            and comp["forecast_checks"]["raw_point_forecasts_and_provenance_exact"]
        ):
            raise ValueError("Q4 comparison/selection/forecast binding mismatch")
        # Both summary paths are explicitly bound by the comparison, so never
        # discover a baseline or candidate by modification time.
        bound_summaries = {
            Path(p).parent.name: Path(p)
            for p in comp["source_proofs"]
            if Path(p).name == "summary.json"
        }
        for method, selected_id in (("baseline", comp["baseline_run_id"]), ("candidate", run_id)):
            archived = bound_summaries[selected_id]
            if sha256(archived) != comp["source_proofs"][str(archived)]:
                raise ValueError("Q4 comparison source summary changed")
            directory = (
                ROOT / "outputs/runs" / case / selected_id
                if method == "candidate"
                else archived.parent
            )
            summary = checked_run(directory, case, selected_id, source_hashes)
            if summary["prediction_metrics"] != read_json(archived)["prediction_metrics"]:
                raise ValueError("Archived Q4 prediction metrics changed")
            if abs(summary["total_cost_cny"] - comp["results"][method]["total_cost_cny"]) > 1e-6:
                raise ValueError("Q4 comparison actual cost mismatch")
            monthly = monthly_cost(directory)
            if abs(sum(monthly.values()) - summary["total_cost_cny"]) > 1e-5:
                raise ValueError("Q4 monthly/annual ledger mismatch")
            records[(case, method)] = {
                "run_id": selected_id,
                "summary": summary,
                "monthly": monthly,
            }
            if method == "candidate":
                export = read_json(directory / "export_manifest.json")
                if not export["readback_ok"] or export["source_result_sha256"] != sha256(
                    directory / "domain_result.json"
                ):
                    raise ValueError("Q4 exported workbook source mismatch")
                for name, expected in read_json(directory / "manifest.json")[
                    "result_sha256"
                ].items():
                    if sha256(directory / "results" / name) != expected:
                        raise ValueError("Q4 exported workbook changed")
                result = read_json(directory / "domain_result.json")
                rows = [row for row in result["intervals"] if row["day"] == "2025-12-21"]
                records[(case, method)]["display_day"] = {
                    "initial_contract_kwh": sum(row["planned_purchase_kwh"] for row in rows),
                    "final_contract_kwh": sum(row["adjusted_purchase_kwh"] for row in rows),
                    "emergency_kwh": sum(row["emergency_purchase_kwh"] for row in rows),
                    "initial_energy_kwh": rows[0]["state_start"]["energy_kwh"],
                    "terminal_energy_kwh": rows[-1]["state_end"]["energy_kwh"],
                    "changed_slots": sum(
                        abs(row["planned_purchase_kwh"] - row["adjusted_purchase_kwh"]) > 1e-6
                        for row in rows
                    ),
                }
                q4_display_day(case, directory, result, output, figures)
        if (
            records[(case, "baseline")]["summary"]["prediction_metrics"]
            != records[(case, "candidate")]["summary"]["prediction_metrics"]
        ):
            raise ValueError("Scenario procurement unexpectedly changed point prediction errors")
        source_hashes[str(comp_path.relative_to(ROOT))] = sha256(comp_path)
        comparisons[case] = comp
    comparison_figure(records, output, figures)
    run_ids = [selection["cases"][case]["run_id"] for case in comparisons]
    workflow_figure(run_ids, output, figures)
    candidate_bindings = paper_tables(selection_path, records, comparisons)
    return {
        "source_script_sha256": sha256(Path(__file__)),
        "selection_sha256": sha256(selection_path),
        "run_ids": run_ids,
        "baseline_run_ids": [comparisons[case]["baseline_run_id"] for case in comparisons],
        "source_hashes": {**source_hashes, **candidate_bindings},
        "monthly_costs": {
            f"{case}:{method}": r["monthly"] for (case, method), r in records.items()
        },
        "display_days": {case: records[(case, "candidate")]["display_day"] for case in comparisons},
        "table_sha256": sha256(ROOT / "paper/tables/q4_results_paper.tex"),
    }
