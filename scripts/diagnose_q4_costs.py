"""Diagnose four existing annual Q4 trajectories without changing their records."""

from __future__ import annotations

import argparse
from pathlib import Path

from microgrid.artifacts import source_tree_hash, utc_now
from microgrid.dataio import sha256_file
from microgrid.problem.q4_common import atomic_json
from microgrid.problem.q4_diagnostics import HORIZONS, analyze_run
from microgrid.problem.q4_evidence import check_inventory, check_model_binding, read_json
from microgrid.problem.q4_inputs import load_q4_inputs
from microgrid.problem.q4_performance import Performance
from microgrid.schemas import InputError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--output", type=Path, default=Path("outputs/evidence/q4_optimization/cost_diagnostics")
    )
    for name in ("main2", "lag2", "main3", "lag3"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    output = args.output if args.output.is_absolute() else repo / args.output
    if output.exists():
        raise InputError("diagnostic output already exists; preserve existing evidence")
    output.mkdir(parents=True)
    performance = Performance()
    performance.switch_phase("report")
    source_hash = source_tree_hash(repo)
    results, hashes = [], {}
    inputs_by_case = {}
    for case, method, run_id in (
        ("q4_2", "main", args.main2),
        ("q4_2", "lag1", args.lag2),
        ("q4_3", "main", args.main3),
        ("q4_3", "lag1", args.lag3),
    ):
        run = repo / "outputs/runs" / case / run_id
        issues = check_model_binding(repo, run) + check_inventory(repo, run)
        if issues:
            raise InputError(";".join(issues[:8]))
        manifest, summary, config = (
            read_json(run / name)
            for name in ("manifest.json", "summary.json", "effective_config.json")
        )
        if (
            manifest["is_synthetic"]
            or manifest["case_id"] != case
            or manifest["run_id"] != run_id
            or summary["run_id"] != run_id
            or config["case_id"] != case
            or not summary["full_annual"]
            or config["price_method"] != method
            or manifest["status"] != ("success" if method == "main" else "diagnostic_success")
        ):
            raise InputError(
                "diagnostics require matching nonsynthetic completed annual trajectories"
            )
        if source_tree_hash(run / "source_snapshot") != manifest["source_hash"]:
            raise InputError("original run source snapshot differs from its recorded source hash")
        for name in (
            "forecasts.jsonl",
            "execution_feedback.jsonl",
            "cost_ledger.jsonl",
            "summary.json",
            "manifest.json",
            "effective_config.json",
            "input_snapshot.json",
        ):
            path = run / name
            hashes[str(path.relative_to(repo))] = sha256_file(path)
        if case not in inputs_by_case:
            with performance.measure("input_preflight"):
                inputs_by_case[case] = load_q4_inputs(repo, case)
        inputs = inputs_by_case[case]
        if inputs.source_hashes != read_json(run / "input_snapshot.json")["source_hashes"]:
            raise InputError("diagnostic input hashes differ from original inputs")
        with performance.measure("metrics_and_report"):
            result = analyze_run(run, inputs)
        results.append(result)
        print(case, method, "diagnostic intervals", result["interval_count"], flush=True)
    unchanged = all(sha256_file(repo / name) == digest for name, digest in hashes.items())
    if not unchanged or source_tree_hash(repo) != source_hash:
        raise InputError("diagnostic source or original artifacts changed during analysis")
    report = {
        "schema_version": 1,
        "ok": True,
        "created_at": utc_now(),
        "source_hash": source_hash,
        "original_artifacts_unchanged": unchanged,
        "source_artifact_hashes": hashes,
        "evaluation_scope": "Read-only causal-history diagnostics on previously inspected development data; not an untouched independent test set.",
        "interpretation": {
            "errors": "actual minus forecast; PV/load kW, net load kWh per ten-minute slot",
            "effective_horizon": "issue-to-target-right-endpoint; bins are (0,6], (6,12], (12,18], (18,24] hours",
            "original_vintage_leads": "original issue-to-hourly-valid-time lead; one visible vintage per refresh/valid-time pair; repeated use retained",
            "sampling": "one error per refresh/target; horizons and decision roles overlap. Active-control errors have one sample per executed slot.",
            "constraint_tags": "overlapping observations, not mutually exclusive causes; costs across tags must not be added",
            "daypart": "post-run actual PV >1 kW versus <=1 kW; not a forecasting input",
            "shortfall_value": "positive net-load error times actual normal price; diagnostic exposure, not an attribution of emergency fees",
            "historical_baseline": "seven previous days at the same slot, all visible at each issue; diagnostic comparison only",
        },
        "runs": results,
    }
    atomic_json(output / "q4_cost_diagnostics.json", report)
    lines = [
        "# Q4 净负荷、时距与费用诊断",
        "",
        "对已用于开发判断的年度数据作只读诊断，不称为未查看过的独立测试集。约束标签可重叠，关联不能直接当作因果归因。",
        "",
        "| 模型 / 价格 | 总费用（元） | 紧急费用占比 | 控制时净负荷偏差（kWh） |",
        "|---|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result['case_id']} / {result['price_method']} | {result['costs']['total_cost_cny']:.2f} | {result['emergency_cost_share']:.2%} | {result['active_control_net_load_errors_kwh']['bias_actual_minus_forecast']:.3f} |"
        )
    lines.extend(
        [
            "",
            "PV误差按刷新到目标右端点的时距分组；原始预报版本lead另列于JSON。",
            "",
            "| 模型 / 价格 | 时距 | 当前PV MAE（kW） | 因果七日基线MAE（kW） | 样本数 |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for result in results:
        for horizon in HORIZONS:
            current = result["horizon_errors"][horizon + ":current_pv_kw"]
            baseline = result["horizon_errors"][horizon + ":historical_pv_kw"]
            lines.append(
                f"| {result['case_id']} / {result['price_method']} | {horizon} | {current['mae']:.3f} | {baseline['mae']:.3f} | {current['count']} |"
            )
    lines.extend(
        [
            "",
            "JSON另含月份/有光伏与无光伏分组、初始合同/调整/电池控制/look-ahead误差、净负荷短缺与过剩分组、约束标签及100个最高紧急费区间。",
            "",
            "未改变预测器、目标函数、合同权限、终端规则或运行选择。",
        ]
    )
    (output / "diagnostics.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    atomic_json(output / "performance_summary.json", performance.summary())
    print(output / "diagnostics.md")


if __name__ == "__main__":
    main()
