"""Build Q4 candidate evidence from four explicit, validated annual runs."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from microgrid.approvals import require_approved_decisions
from microgrid.artifacts import source_tree_hash
from microgrid.dataio import sha256_file
from microgrid.problem.contracts import CaseResult
from microgrid.problem.q4_common import (
    ACTION_START,
    STEP,
    YEAR_END,
    atomic_json,
    reserve_start_from_config,
)
from microgrid.problem.q4_export import load_replay_evidence
from microgrid.problem.q4_inputs import load_q4_inputs
from microgrid.problem.q4_validation import validate_q4_run
from microgrid.problem.result_io import interval_from_dict, load_case_result
from microgrid.problem.rolling_engine import _metrics

RUN_SPECS = (
    ("q4_2", "main", "main2"),
    ("q4_2", "lag1", "lag2"),
    ("q4_3", "main", "main3"),
    ("q4_3", "lag1", "lag3"),
)
DISPLAY_DATES = ("2025-03-20", "2025-06-21", "2025-09-23", "2025-12-21")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def checked_run(repo, case, method, run_id):
    directory = repo / "outputs" / "runs" / case / run_id
    manifest = read_json(directory / "manifest.json")
    if manifest["status"] not in ("success", "diagnostic_success"):
        return checked_failure(repo, case, method, run_id)
    summary = read_json(directory / "summary.json")
    config = read_json(directory / "effective_config.json")
    result = load_case_result(directory / "domain_result.json")
    expected_status = "success" if method == "main" else "diagnostic_success"
    if (
        result.is_synthetic
        or result.status != expected_status
        or manifest["status"] != expected_status
        or not summary["full_annual"]
        or summary["price_method"] != method
        or summary["interval_count"] != 48096
        or result.case_id != case
        or result.run_id != run_id
        or config["price_method"] != method
    ):
        raise ValueError(f"not a validated annual {case}/{method} run: {run_id}")
    if source_tree_hash(directory / "source_snapshot") != manifest["source_hash"]:
        raise ValueError(f"source snapshot mismatch: {run_id}")
    ledger, executions = load_replay_evidence(directory, case)
    inputs = load_q4_inputs(repo, case)
    validation = validate_q4_run(
        result.intervals,
        executions,
        ledger,
        end_time=YEAR_END,
        actual_inputs=inputs,
        reserve_start=reserve_start_from_config(config),
    )
    if not validation["ok"] or not validation["full_annual"]:
        raise ValueError(f"independent replay validation failed: {run_id}")
    if summary["source_hashes"] != inputs.source_hashes:
        raise ValueError(f"actual input mismatch: {run_id}")
    if abs(summary["total_cost_cny"] - validation["costs"]["total_cost_cny"]) > 0.01:
        raise ValueError(f"summary cost mismatch: {run_id}")
    summary["cost_scope"] = "full_annual"
    hashes = {
        name: sha256_file(directory / name)
        for name in (
            "domain_result.json",
            "summary.json",
            "validation.json",
            "effective_config.json",
            "execution_feedback.jsonl",
            "contracts.jsonl",
            "cost_ledger.jsonl",
            "forecasts.jsonl",
            "source_snapshot_manifest.json",
        )
    }
    return (
        {
            "case_id": case,
            "price_method": method,
            "run_id": run_id,
            "summary": summary,
            "independent_validation": validation,
            "source_hash": manifest["source_hash"],
            "artifact_hashes": hashes,
            "effective_config": config,
        },
        result,
        executions,
        ledger,
    )


def checked_failure(repo, case, method, run_id):
    directory = repo / "outputs" / "runs" / case / run_id
    manifest = read_json(directory / "manifest.json")
    failure = read_json(directory / "failure.json")
    config = read_json(directory / "effective_config.json")
    if manifest["is_synthetic"] or config["price_method"] != method:
        raise ValueError(f"failed run source/method mismatch: {run_id}")
    if source_tree_hash(directory / "source_snapshot") != manifest["source_hash"]:
        raise ValueError(f"failed run source snapshot mismatch: {run_id}")
    intervals = tuple(
        interval_from_dict(json.loads(line)["interval"])
        for line in (directory / "execution_feedback.jsonl").open(encoding="utf-8")
    )
    ledger, executions = load_replay_evidence(directory, case)
    if len(intervals) != failure["completed_steps"] or len(ledger.bills) != len(intervals):
        raise ValueError(f"failed run prefix/log count mismatch: {run_id}")
    end = ACTION_START + len(intervals) * STEP
    if str(end) != failure["time"]:
        raise ValueError(f"failed run chronology mismatch: {run_id}")
    inputs = load_q4_inputs(repo, case)
    validation = validate_q4_run(
        intervals,
        executions,
        ledger,
        end_time=YEAR_END,
        actual_inputs=inputs,
        reserve_start=reserve_start_from_config(config),
    )
    allowed = {
        "interval/execution coverage mismatch",
        "billing coverage mismatch",
        "terminal_infeasible",
    }
    unexpected = [
        issue
        for issue in validation["violations"]
        if issue not in allowed
        and not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}: (missing slots \[[\d, ]+\]|expected 144 intervals, got \d+)", issue
        )
    ]
    if validation["ok"] or unexpected:
        raise ValueError(f"failed run has unexpected validation issues: {run_id}: {unexpected}")
    costs = ledger.costs()
    with (directory / "solver_records.jsonl").open(encoding="utf-8") as stream:
        solver_records = [json.loads(line) for line in stream]
    successful_records = [record for record in solver_records if record["status"] == 0]
    summary = {
        "case_id": case,
        "run_id": run_id,
        "price_method": method,
        "status": manifest["status"],
        "full_annual": False,
        "interval_count": len(intervals),
        "observed_end_time": str(end),
        "initial_energy_kwh": 6000,
        "terminal_energy_kwh": failure["energy_kwh"],
        "cost_scope": "executed_and_settled_prefix_including_prior_adjustment_events",
        "costs": {
            "planned_cost_cny": costs.planned_cost_cny,
            "adjustment_cost_cny": costs.adjustment_cost_cny,
            "emergency_cost_cny": costs.emergency_cost_cny,
        },
        "total_cost_cny": costs.total_cost_cny,
        "emergency_kwh": math.fsum(r.emergency_kwh for r in executions),
        "pv_curtailed_kwh": math.fsum(r.curtailed_pv_kwh for r in executions),
        "unused_contract_kwh": math.fsum(r.unused_grid_kwh for r in executions),
        "actual_external_kwh": math.fsum(r.grid_used_kwh + r.emergency_kwh for r in executions),
        "planned_purchase_kwh": math.fsum(r.planned_purchase_kwh for r in intervals),
        "final_contract_kwh": math.fsum(r.adjusted_purchase_kwh for r in intervals),
        "battery_throughput_kwh": math.fsum(
            r.action.charge_kwh + r.action.discharge_kwh for r in executions
        ),
        "solve_count": sum(not record["reused_tail"] for record in successful_records),
        "reused_tail_count": sum(record["reused_tail"] for record in successful_records),
        "failed_solver_calls": sum(record["status"] != 0 for record in solver_records),
        "elapsed_seconds": None,
        "prediction_metrics": _metrics(directory, inputs, end),
        "source_hashes": inputs.source_hashes,
        "validation_ok": False,
    }
    entry = {
        "case_id": case,
        "price_method": method,
        "run_id": run_id,
        "summary": summary,
        "independent_validation": validation,
        "present_prefix_physics_source_and_billing_checks_ok": True,
        "validation_scope_note": "full_annual in the failing validator report denotes requested endpoint; ok=false and actual trajectory is incomplete",
        "failure": failure,
        "source_hash": manifest["source_hash"],
        "effective_config": config,
        "artifact_hashes": {
            name: sha256_file(directory / name)
            for name in (
                "manifest.json",
                "failure.json",
                "effective_config.json",
                "execution_feedback.jsonl",
                "contracts.jsonl",
                "cost_ledger.jsonl",
                "forecasts.jsonl",
                "solver_records.jsonl",
                "source_snapshot_manifest.json",
            )
        },
    }
    return (
        entry,
        CaseResult(case, run_id, manifest["status"], False, intervals=intervals),
        executions,
        ledger,
    )


def plot_day(repo, output, case, run_id, day, result, executions, ledger):
    date = dt.date.fromisoformat(day)
    offset = (date - ACTION_START.date()).days * 144
    rows = result.intervals[offset : offset + 144]
    bills = [ledger.bills[dt.datetime.combine(date, dt.time()) + k * STEP] for k in range(144)]
    x = np.arange(144) / 6
    fig, axes = plt.subplots(4, 1, figsize=(10, 10), sharex=True, layout="constrained")
    axes[0].plot(x, [r.load_kw for r in rows], label="Actual load", color="#255f85")
    axes[0].plot(x, [r.pv_kw for r in rows], label="Actual PV", color="#db9c2c")
    axes[0].set_ylabel("Power (kW)")
    price_axis = axes[0].twinx()
    price_axis.plot(
        x, [b.execution_price for b in bills], color="#9370a4", alpha=0.7, label="Actual price"
    )
    price_axis.set_ylabel("Price (CNY/kWh)")
    axes[0].legend(loc="upper left", ncol=2)
    price_axis.legend(loc="upper right")
    axes[1].step(x, [r.planned_purchase_kwh for r in rows], where="post", label="Initial G0")
    axes[1].step(
        x,
        [r.adjusted_purchase_kwh for r in rows],
        where="post",
        label="Executed contract",
        alpha=0.8,
    )
    axes[1].fill_between(
        x, [r.emergency_purchase_kwh for r in rows], step="post", alpha=0.6, label="Emergency U"
    )
    axes[1].set_ylabel("Energy/slot (kWh)")
    axes[1].legend(loc="upper left", ncol=3)
    axes[2].step(
        x, [r.action.charge_kwh for r in rows], where="post", label="Charge C", color="#3b9068"
    )
    axes[2].step(
        x,
        [-r.action.discharge_kwh for r in rows],
        where="post",
        label="Discharge -D",
        color="#b65050",
    )
    axes[2].set_ylabel("Energy/slot (kWh)")
    axes[2].legend(loc="upper left", ncol=2)
    boundaries = [rows[0].state_start.energy_kwh] + [r.state_end.energy_kwh for r in rows]
    axes[3].plot(np.arange(145) / 6, boundaries, color="#255f85")
    axes[3].axhline(1200, color="gray", ls="--", lw=0.8)
    axes[3].axhline(10800, color="gray", ls="--", lw=0.8)
    axes[3].set_ylabel("Stored energy (kWh)")
    axes[3].set_xlabel("Local hour (Asia/Shanghai)")
    axes[3].set_xlim(0, 24)
    axes[3].set_xticks(np.arange(0, 25, 4))
    for axis in axes:
        axis.grid(alpha=0.2)
    fig.suptitle(f"{case.replace('_', '-').upper()} | {day} | {run_id}", fontsize=12)
    stem = output / f"{case}_{day}"
    for extension in ("pdf", "png"):
        fig.savefig(stem.with_suffix("." + extension), dpi=170)
    plt.close(fig)
    return {
        "case_id": case,
        "run_id": run_id,
        "day": day,
        "first_interval_index": offset,
        "interval_count": len(rows),
        "interval_endpoints": [
            str(dt.datetime.combine(date, dt.time())),
            str(dt.datetime.combine(date + dt.timedelta(days=1), dt.time())),
        ],
        "files": {
            str(stem.with_suffix("." + ext).relative_to(repo)): sha256_file(
                stem.with_suffix("." + ext)
            )
            for ext in ("pdf", "png")
        },
    }


def build_report(args):
    repo = args.repo.resolve()
    require_approved_decisions(repo, ("D_EVAL_Q4",))
    output = repo / "outputs" / "evidence" / "q4_annual"
    output.mkdir(parents=True, exist_ok=True)
    records, replay = {}, {}
    for case, method, field in RUN_SPECS:
        entry, result, executions, ledger = checked_run(repo, case, method, getattr(args, field))
        records[(case, method)] = entry
        replay[(case, method)] = (result, executions, ledger)
    has_failed = any(not record["summary"]["full_annual"] for record in records.values())
    comparisons = []
    monthly = []
    for case in ("q4_2", "q4_3"):
        main, lag1 = records[(case, "main")], records[(case, "lag1")]
        main_config, lag_config = dict(main["effective_config"]), dict(lag1["effective_config"])
        main_config.pop("price_method")
        lag_config.pop("price_method")
        if (
            main_config != lag_config
            or main["source_hash"] != lag1["source_hash"]
            or main["summary"]["source_hashes"] != lag1["summary"]["source_hashes"]
        ):
            raise ValueError(f"incomparable configuration/source pair: {case}")
        a, b = main["summary"], lag1["summary"]
        complete = a["full_annual"] and b["full_annual"]
        difference = a["total_cost_cny"] - b["total_cost_cny"] if complete else None
        comparisons.append(
            {
                "case_id": case,
                "main_run_id": main["run_id"],
                "lag1_run_id": lag1["run_id"],
                "annual_comparison_valid": complete,
                "unavailable_reason": None
                if complete
                else "incomplete failed trajectory; prefix cost is not annual cost",
                "main_minus_lag1_cost_cny": difference,
                "main_minus_lag1_cost_percent": 100 * difference / b["total_cost_cny"]
                if complete
                else None,
                "main_minus_lag1_price_6h_mae": a["prediction_metrics"]["price_cny_per_kwh:6h"][
                    "mae"
                ]
                - b["prediction_metrics"]["price_cny_per_kwh:6h"]["mae"]
                if complete
                else None,
                "main_minus_lag1_price_24h_mae": a["prediction_metrics"]["price_cny_per_kwh:24h"][
                    "mae"
                ]
                - b["prediction_metrics"]["price_cny_per_kwh:24h"]["mae"]
                if complete
                else None,
                "cost_direction": "unavailable"
                if not complete
                else "main_higher"
                if difference > 0
                else "main_lower"
                if difference < 0
                else "equal",
            }
        )
        for method in ("main", "lag1"):
            sums = {}
            for bill in replay[(case, method)][2].bills.values():
                key = bill.slot_start.strftime("%Y-%m")
                sums.setdefault(key, []).append(
                    bill.planned_cost_cny + bill.adjustment_cost_cny + bill.emergency_cost_cny
                )
            monthly.extend(
                {
                    "case_id": case,
                    "price_method": method,
                    "month": month,
                    "total_cost_cny": math.fsum(values),
                }
                for month, values in sorted(sums.items())
            )
    figures = []
    for case in ("q4_2", "q4_3"):
        result, executions, ledger = replay[(case, "main")]
        for day in DISPLAY_DATES:
            figures.append(
                plot_day(
                    repo,
                    output,
                    case,
                    records[(case, "main")]["run_id"],
                    day,
                    result,
                    executions,
                    ledger,
                )
            )
    fig, axes = plt.subplots(2, 1, figsize=(9, 6), sharex=True, layout="constrained")
    for axis, case in zip(axes, ("q4_2", "q4_3"), strict=True):
        for method in ("main", "lag1"):
            points = [
                row for row in monthly if row["case_id"] == case and row["price_method"] == method
            ]
            if (
                abs(
                    math.fsum(row["total_cost_cny"] for row in points)
                    - records[(case, method)]["summary"]["total_cost_cny"]
                )
                > 0.01
            ):
                raise ValueError(f"monthly cost coverage mismatch: {case}/{method}")
            axis.plot(
                [int(row["month"][-2:]) for row in points],
                [row["total_cost_cny"] / 1e4 for row in points],
                marker="o",
                label=method
                if records[(case, method)]["summary"]["full_annual"]
                else method + " (failed prefix)",
            )
        axis.set_title(case.replace("_", "-").upper())
        axis.set_ylabel("Actual cost (10,000 CNY)")
        axis.grid(alpha=0.2)
        axis.legend()
    axes[-1].set_xticks(range(2, 13))
    axes[-1].set_xlabel("Month in 2025")
    monthly_files = {}
    for extension in ("pdf", "png"):
        path = output / ("monthly_costs." + extension)
        fig.savefig(path, dpi=170)
        monthly_files[str(path.relative_to(repo))] = sha256_file(path)
    plt.close(fig)
    figures.append({"kind": "monthly_actual_cost_comparison", "files": monthly_files})
    evidence = {
        "schema_version": 1,
        "is_synthetic": False,
        "candidate_only": True,
        "selection_status": "not_selected_by_this_script",
        "human_review_status": "pending",
        "source_script_sha256": sha256_file(Path(__file__)),
        "action_period": [str(ACTION_START), str(YEAR_END)],
        "continuous_days": 334,
        "runs": list(records.values()),
        "comparisons": comparisons,
        "monthly_costs": monthly,
        "figures": figures,
        "sampling": "6h: disjoint first36 targets per refresh;24h: issue/target pairs overlap, clipped to each executed prefix or successful annual endpoint;monthly errors grouped by target interval start;annual differences unavailable for incomplete pairs",
    }
    atomic_json(output / "comparison.json", evidence)
    fields = [
        "case_id",
        "price_method",
        "run_id",
        "cost_scope",
        "interval_count",
        "planned_cost_cny",
        "adjustment_cost_cny",
        "emergency_cost_cny",
        "total_cost_cny",
        "emergency_kwh",
        "pv_curtailed_kwh",
        "unused_contract_kwh",
        "actual_external_kwh",
        "planned_purchase_kwh",
        "final_contract_kwh",
        "battery_throughput_kwh",
        "initial_energy_kwh",
        "terminal_energy_kwh",
        "solve_count",
        "reused_tail_count",
        "elapsed_seconds",
    ]
    with (output / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records.values():
            values = {**record["summary"], **record["summary"]["costs"]}
            writer.writerow({key: values[key] for key in fields})
    with (output / "prediction_metrics.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("case_id", "price_method", "variable", "window", "count", "mae", "rmse"),
        )
        writer.writeheader()
        for record in records.values():
            for name, metric in record["summary"]["prediction_metrics"].items():
                variable, window = name.split(":", 1)
                writer.writerow(
                    {
                        "case_id": record["case_id"],
                        "price_method": record["price_method"],
                        "variable": variable,
                        "window": window,
                        **metric,
                    }
                )
    lines = [
        "# Q4 全年候选证据",
        "",
        "各轨迹从6000 kWh独立连续运行。具体run选择和AI人工核验仍待参赛队确认。"
        + (
            "失败前缀单独标记，费用范围为已执行及结算区间。"
            if has_failed
            else "四条均为完整全年结果。"
        ),
        "",
        "| Case | 价格预测 | 范围/区间 | 费用/元 | 紧急电量/kWh | 最后SOC/kWh |",
        "|---|---|---|---:|---:|---:|",
    ]
    for record in records.values():
        s = record["summary"]
        lines.append(
            f"| {record['case_id']} | {record['price_method']} | {'全年' if s['full_annual'] else '失败前缀'}/{s['interval_count']} | {s['total_cost_cny']:.2f} | {s['emergency_kwh']:.3f} | {s['terminal_energy_kwh']:.6f} |"
        )
    lines.extend(["", "同case主方案减lag1：", ""])
    for c in comparisons:
        if not c["annual_comparison_valid"]:
            lines.append(
                f"- {c['case_id']}：对照未完成，不报告年度费用差。失败现场与已执行前缀物理、来源、账本核验详见comparison.json。"
            )
            continue
        lines.append(
            f"- {c['case_id']}：费用差{c['main_minus_lag1_cost_cny']:+.2f}元（{c['main_minus_lag1_cost_percent']:+.3f}%）；6h价格MAE差{c['main_minus_lag1_price_6h_mae']:+.6f}，24h差{c['main_minus_lag1_price_24h_mae']:+.6f}。"
        )
    observations = []
    for comparison in comparisons:
        if comparison["annual_comparison_valid"]:
            case = comparison["case_id"]
            change = (
                records[(case, "main")]["summary"]["emergency_kwh"]
                - records[(case, "lag1")]["summary"]["emergency_kwh"]
            )
            observations.append(
                f"{case.replace('_', '-').upper()}主方案相对lag1紧急购电量{'增加' if change >= 0 else '减少'}{abs(change):.2f} kWh。"
            )
    if all(records[(case, "main")]["summary"]["full_annual"] for case in ("q4_2", "q4_3")):
        pv2, pv3 = (
            records[(case, "main")]["summary"]["prediction_metrics"] for case in ("q4_2", "q4_3")
        )
        observations.append(
            f"光伏MAE：Q4-3的6h/24h为{pv3['pv_kw:6h']['mae']:.2f}/{pv3['pv_kw:24h']['mae']:.2f} kW，Q4-2为{pv2['pv_kw:6h']['mae']:.2f}/{pv2['pv_kw:24h']['mae']:.2f} kW。短窗与长窗结论分别报告。"
        )
    lines.extend(["", " ".join(observations)])
    lines.extend(
        [
            "",
            "精度提高不预设为费用减少；跨case比较同时改变光伏信息和合同权限，无法单独识别某项的因果收益。完整未舍入分项、预测/月指标、run/source/hash及八张展示日图见comparison.json、summary.csv。",
        ]
    )
    (output / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    table = [
        "% Generated candidate Q4 evidence; explicit run IDs; no run selection approval.",
        "\\subsection{全年回放结果}",
        "各主方案及前一日同slot价格对照从6\\,000 kWh独立连续运行。"
        + (
            "成功主方案覆盖334日、48\\,096区间，失败前缀单独标记。"
            if has_failed
            else "四条轨迹均覆盖334日、48\\,096区间。"
        )
        + "完整轨迹实际年末储能6\\,000 kWh，连续SOC、合同权限、实际输入及账本复核通过。表中只在展示时舍入。",
        "\\begin{table}[htbp]\\centering\\small",
        "\\caption{Q4实际费用与价格预测对照（费用单位：万元）}\\label{tab:q4-annual}",
        "\\begin{tabular}{llrrrr}\\toprule",
        "条件 & 价格预测 & 计划费 & 调整费 & 紧急费 & 总费 \\\\ \\midrule",
    ]
    for record in records.values():
        s = record["summary"]
        costs = s["costs"]
        table.append(
            f"{record['case_id'].replace('_', '-').upper()} & {'主方案' if record['price_method'] == 'main' else 'lag1'}{'*' if not s['full_annual'] else ''} & {costs['planned_cost_cny'] / 1e4:.4f} & {costs['adjustment_cost_cny'] / 1e4:.4f} & {costs['emergency_cost_cny'] / 1e4:.4f} & {s['total_cost_cny'] / 1e4:.4f} \\\\"
        )
    table.extend(["\\bottomrule\\end{tabular}\\end{table}"])
    if has_failed:
        table.append("星号行仅为已执行及结算前缀费用，不是全年总费，不作为满足年度端点的可行对照。")
    for record in records.values():
        if "failure" in record:
            f = record["failure"]
            table.append(
                f"{record['case_id'].replace('_', '-').upper()}的{record['price_method']}在{f['time']}停止，完成{f['completed_steps']}区间，最后实际储能{f['energy_kwh']:.6f} kWh。失败现场和缺失区间的验证报告保留，不称为完整全年对照。"
            )
            if record["run_id"] == "q4-2-annual-lag1-003" and f["time"] == "2025-12-31 23:50:00":
                table.append(
                    "该末区间需要母线侧充电0.3553 kWh才能达到年度目标，但冻结合同590.7609 kWh小于预测负载590.7620 kWh，且预测光伏为零。由能量平衡及紧急电量不得同时充电的约束，预测问题不可行。这说明连续反馈与冻结合同下，必要SOC可达界不能保证实际年度闭合；保留失败，不用实际未来负载或紧急充电修补。"
                )
    table.extend(
        [
            "\\begin{table}[htbp]\\centering\\small",
            "\\caption{Q4预测误差（每格为MAE/RMSE；负载、光伏单位kW，价格单位元/kWh）}",
            "\\begin{tabular}{lllrrr}\\toprule",
            "条件 & 价格预测 & 窗口 & 负载 & 光伏 & 价格 \\\\ \\midrule",
        ]
    )
    for record in records.values():
        metrics = record["summary"]["prediction_metrics"]
        for window in ("6h", "24h"):
            formatted = []
            for variable in ("load_kw", "pv_kw", "price_cny_per_kwh"):
                metric = metrics[variable + ":" + window]
                digits = 4 if variable == "price_cny_per_kwh" else 2
                formatted.append(f"{metric['mae']:.{digits}f}/{metric['rmse']:.{digits}f}")
            table.append(
                f"{record['case_id'].replace('_', '-').upper()} & {'主方案' if record['price_method'] == 'main' else 'lag1'}{'*' if not record['summary']['full_annual'] else ''} & {window} & "
                + " & ".join(formatted)
                + " \\\\"
            )
    table.extend(["\\bottomrule\\end{tabular}\\end{table}"])
    for c in comparisons:
        if not c["annual_comparison_valid"]:
            table.append(
                f"{c['case_id'].replace('_', '-').upper()}的lag1未完成年度轨迹，因此不报告主方案相对该对照的全年费用差或策略节省。"
            )
            continue
        direction = "增加" if c["main_minus_lag1_cost_cny"] > 0 else "减少"
        table.append(
            f"{c['case_id'].replace('_', '-').upper()}主方案相对lag1的实际费用{direction}{abs(c['main_minus_lag1_cost_cny']):.2f}元（{abs(c['main_minus_lag1_cost_percent']):.3f}\\%），6小时价格MAE差为{c['main_minus_lag1_price_6h_mae']:+.6f}元/kWh。保留这一结果，不根据全年费用重新调整预测权重。"
        )
    table.append(" ".join(observations))
    table.append(
        "6小时误差取每次刷新后首36格、不重叠；24小时误差按发布与目标对计数、窗口重叠，并截到观测末端。"
        + (
            "星号对照仅统计失败前已结束的区间，不能混用样本数。"
            if has_failed
            else "6小时样本数为48\\,096，24小时为192\\,168。"
        )
        + "Q4-2和Q4-3同时改变光伏信息及合同权限，费用差不单独归因于官方预报。预测样本数、月指标和费用分项保存在绑定显式run的证据目录。"
    )
    for case in ("q4_2", "q4_3"):
        table.extend(
            [
                f"\\IfFileExists{{../outputs/evidence/q4_annual/{case}_2025-12-21.pdf}}{{",
                "\\begin{figure}[htbp]\\centering",
                f"\\includegraphics[width=0.92\\textwidth]{{../outputs/evidence/q4_annual/{case}_2025-12-21.pdf}}",
                f"\\caption{{{case.replace('_', '-').upper()}主方案12月21日的实际量、合同、充放电和连续储能；取自同一全年轨迹。}}",
                "\\end{figure}",
                "}{}",
            ]
        )
    table.append(
        "\\noindent\\textit{复现绑定：}候选运行编号为\\texttt{"
        + ", ".join(record["run_id"] for record in records.values())
        + "}；代码与配置快照、逐格账本及SHA256见各run目录及\\texttt{outputs/evidence/q4\\_annual/comparison.json}。具体运行选择与人工核验仍待参赛队确认。"
    )
    table_path = repo / "paper" / "tables" / "q4_results.tex"
    table_path.parent.mkdir(parents=True, exist_ok=True)
    table_path.write_text("\n".join(table) + "\n", encoding="utf-8")
    print(output / "comparison.md")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    for _, _, field in RUN_SPECS:
        parser.add_argument("--" + field, required=True)
    build_report(parser.parse_args())


if __name__ == "__main__":
    main()
