"""Same-source Q3 settlement summaries, manuscript tables and dispatch figures."""

from __future__ import annotations

import csv
import datetime as dt
import math
from collections import defaultdict
from pathlib import Path

from ..artifacts import file_digest, write_json
from ..schemas import InputError
from .contracts import ENERGY_ABS_TOL_KWH, CaseResult, IntervalResult

DISPLAY_DAYS = tuple(dt.date(2025, m, d) for m, d in ((3, 20), (6, 21), (9, 23), (12, 21)))
COST_KEYS = {
    "base_plan": "planned_cost_cny",
    "adjustment": "adjustment_cost_cny",
    "emergency": "emergency_cost_cny",
}


def emergency_segments(intervals: tuple[IntervalResult, ...]) -> tuple[dict, ...]:
    """Merge consecutive realized emergency slots, strictly within one day."""
    if len(intervals) != 144 or len({i.day for i in intervals}) != 1:
        raise InputError("emergency grouping requires one complete natural day")
    if tuple(i.slot for i in intervals) != tuple(range(144)):
        raise InputError("emergency grouping requires ordered slots 0..143")
    groups = []
    first = None
    amounts: list[float] = []

    def endpoint(slot: int) -> str:
        return "0:00+1" if slot == 144 else f"{slot // 6}:{slot % 6 * 10:02d}"

    for slot in range(145):
        active = slot < 144 and intervals[slot].emergency_purchase_kwh > ENERGY_ABS_TOL_KWH
        if active:
            if first is None:
                first = slot
            amounts.append(intervals[slot].emergency_purchase_kwh)
        elif first is not None:
            groups.append(
                {
                    "first_slot": first,
                    "end_slot_exclusive": slot,
                    "label": f"{endpoint(first)}-{endpoint(slot)}",
                    "energy_kwh": math.fsum(amounts),
                }
            )
            first, amounts = None, []
    return tuple(groups)


def settlement_summary(rows: tuple[dict, ...]) -> tuple[dict, dict]:
    terms = defaultdict(lambda: defaultdict(list))
    trades = defaultdict(lambda: defaultdict(list))
    seen = set()
    for row in rows:
        kind = row["record_type"]
        if kind not in COST_KEYS:
            raise InputError("unknown Q3 settlement record type")
        start = dt.datetime.fromisoformat(row["target_slot_start"])
        issue = dt.datetime.fromisoformat(row["issue_time"])
        key = (kind, issue, start)
        if key in seen:
            raise InputError("duplicate Q3 settlement event")
        seen.add(key)
        price, cost = row["price_cny_per_kwh"], row["cost_cny"]
        if not all(math.isfinite(v) and v >= 0 for v in (price, cost)):
            raise InputError("Q3 settlement price/cost must be finite and nonnegative")
        if kind == "adjustment":
            plus, minus = row["delta_plus_kwh"], row["delta_minus_kwh"]
            if (
                not all(math.isfinite(v) and v >= 0 for v in (plus, minus))
                or (plus == 0 and minus == 0)
                or (plus > 0 and minus > 0)
                or issue.date() != start.date()
                or issue.time() not in (dt.time(6), dt.time(12), dt.time(18))
                or start < issue
            ):
                raise InputError("invalid nonzero Q3 adjustment event")
            expected = price * (1.5 * plus + 0.5 * minus)
            trade = trades[issue.hour]
            trade["delta_plus_kwh"].append(plus)
            trade["delta_minus_kwh"].append(minus)
            trade["cost_cny"].append(cost)
            trade["material_events"].append(int(max(plus, minus) > ENERGY_ABS_TOL_KWH))
        else:
            energy = row["energy_kwh"]
            if not math.isfinite(energy) or energy < 0:
                raise InputError("invalid Q3 settlement energy")
            expected = price * energy * (5 if kind == "emergency" else 1)
        if abs(cost - expected) > 1e-5:
            raise InputError("Q3 settlement fee violates SETTLE-A")
        terms[start.date()][COST_KEYS[kind]].append(cost)
    daily = {
        day: {key: math.fsum(values.get(key, [])) for key in COST_KEYS.values()}
        for day, values in terms.items()
    }
    for values in daily.values():
        values["total_cost_cny"] = math.fsum(values.values())
    releases = {
        hour: {
            "nonzero_events": len(values["cost_cny"]),
            "material_events": sum(values["material_events"]),
            **{
                key: math.fsum(values[key])
                for key in ("delta_plus_kwh", "delta_minus_kwh", "cost_cny")
            },
        }
        for hour, values in sorted(trades.items())
    }
    return daily, releases


def _number(value: float) -> str:
    return f"{value:.6f}"


def write_q3_paper_assets(result: CaseResult, rows: tuple[dict, ...], output: Path) -> Path:
    """Called only after the delivery pipeline has validated and bound its source."""
    if output.exists():
        raise InputError("Q3 paper asset directory already exists")
    daily, releases = settlement_summary(rows)
    by_day = {d: tuple(i for i in result.intervals if i.day == d) for d in DISPLAY_DAYS}
    if any(len(v) != 144 for v in by_day.values()):
        raise InputError("Q3 paper assets require all four specified display days")
    output.mkdir(parents=True)
    report = {
        "case_id": "q3",
        "run_id": result.run_id,
        "is_synthetic": result.is_synthetic,
        "display_days": [str(d) for d in DISPLAY_DAYS],
        "daily_costs_cny": {str(d): v for d, v in sorted(daily.items())},
        "revision_release_totals": releases,
        "costs_cny": {k: result.metadata[k] for k in (*COST_KEYS.values(), "total_cost_cny")},
        "interval_count": len(result.intervals),
        "state_end_kwh": result.intervals[-1].state_end.energy_kwh,
        "emergency_segments": {str(d): emergency_segments(v) for d, v in by_day.items()},
    }
    write_json(output / "q3_report.json", report)
    with (output / "q3_trajectory.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            (
                "day",
                "slot",
                "load_kwh",
                "pv_kwh",
                "G0_kwh",
                "Gfinal_kwh",
                "emergency_kwh",
                "charge_kwh",
                "discharge_kwh",
                "SOC_start_kwh",
                "SOC_end_kwh",
            )
        )
        for i in result.intervals:
            writer.writerow(
                (
                    str(i.day),
                    i.slot,
                    i.load_kw / 6,
                    i.pv_kw / 6,
                    i.planned_purchase_kwh,
                    i.adjusted_purchase_kwh,
                    i.emergency_purchase_kwh,
                    i.action.charge_kwh,
                    i.action.discharge_kwh,
                    i.state_start.energy_kwh,
                    i.state_end.energy_kwh,
                )
            )
    lines = [
        f"% Q3 same-source assets; run={result.run_id}",
        r"\begin{table}[htbp]\centering",
        r"\caption{问题三指定时段的最终确认购电量及日总量、总费用（kWh、元）}",
        r"\begin{tabular}{lrlrlr}\toprule",
        r"时间段 & 购电量 & 时间段 & 购电量 & 时间段 & 购电量 \\",
    ]
    labels = (
        "10:00--10:10",
        "12:00--12:10",
        "14:00--14:10",
        "16:00--16:10",
        "18:00--18:10",
        "20:00--20:10",
    )
    slots = (60, 72, 84, 96, 108, 120)
    for day, intervals in by_day.items():
        lines.append(r"\midrule\multicolumn{6}{c}{" + str(day) + r"} \\")
        for offset in (0, 3):
            lines.append(
                " & ".join(
                    f"{labels[k]} & {_number(intervals[slots[k]].adjusted_purchase_kwh)}"
                    for k in range(offset, offset + 3)
                )
                + r" \\"
            )
        lines.append(
            f"全天最终量 & {_number(math.fsum(i.adjusted_purchase_kwh for i in intervals))} & 全天总费用 & {_number(daily[day]['total_cost_cny'])} & &"
            + r" \\"
        )
    lines.extend(
        (
            r"\bottomrule\end{tabular}\end{table}",
            r"\begin{table}[htbp]\centering",
            r"\caption{问题三指定日期实际充放电量及日初、日末储电量（kWh）}",
            r"\resizebox{\linewidth}{!}{\begin{tabular}{lrrrrrrrr}\toprule",
            r"\multicolumn{1}{c}{时间段} & \multicolumn{2}{c}{2025-03-20} & \multicolumn{2}{c}{2025-06-21} & \multicolumn{2}{c}{2025-09-23} & \multicolumn{2}{c}{2025-12-21} \\",
            r" & 充电量 & 放电量 & 充电量 & 放电量 & 充电量 & 放电量 & 充电量 & 放电量 \\\midrule",
        )
    )
    for block in range(6):
        values = []
        for intervals in by_day.values():
            part = intervals[block * 24 : (block + 1) * 24]
            values.extend(
                (
                    _number(math.fsum(i.action.charge_kwh for i in part)),
                    _number(math.fsum(i.action.discharge_kwh for i in part)),
                )
            )
        lines.append(f"{block * 4}:00--{(block + 1) * 4}:00 & " + " & ".join(values) + r" \\")
    for label, endpoint in (("0:00储电量", "state_start"), ("24:00储电量", "state_end")):
        values = [
            f"\\multicolumn{{2}}{{c}}{{{_number(getattr(v[0 if endpoint == 'state_start' else -1], endpoint).energy_kwh)}}}"
            for v in by_day.values()
        ]
        lines.append(label + " & " + " & ".join(values) + r" \\")
    lines.extend(
        (
            r"\bottomrule\end{tabular}}\end{table}",
            r"\begin{table}[htbp]\centering",
            r"\caption{问题三指定日期的紧急购电时间段及购电量（kWh）}",
            r"\resizebox{\linewidth}{!}{\begin{tabular}{lrlrlrlr}\toprule",
            r"\multicolumn{2}{c}{2025-03-20} & \multicolumn{2}{c}{2025-06-21} & \multicolumn{2}{c}{2025-09-23} & \multicolumn{2}{c}{2025-12-21} \\",
            r"时间段 & 购电量 & 时间段 & 购电量 & 时间段 & 购电量 & 时间段 & 购电量 \\\midrule",
        )
    )
    groups = [emergency_segments(v) for v in by_day.values()]
    for index in range(max(1, *(len(v) for v in groups))):
        values = []
        for group in groups:
            values.extend(
                (group[index]["label"], _number(group[index]["energy_kwh"]))
                if index < len(group)
                else ("无" if index == 0 else "", "0" if index == 0 else "")
            )
        lines.append(" & ".join(values) + r" \\")
    lines.extend(
        (
            r"\bottomrule\end{tabular}}\end{table}",
            r"\begin{table}[htbp]\centering",
            r"\caption{问题三全年费用与储能端点}",
            r"\begin{tabular}{lr}\toprule 项目 & 数值 \\\midrule",
        )
    )
    for name, key in (
        ("计划购电费（元）", "planned_cost_cny"),
        ("逐版调整费（元）", "adjustment_cost_cny"),
        ("紧急购电费（元）", "emergency_cost_cny"),
        ("总费用（元）", "total_cost_cny"),
    ):
        lines.append(name + " & " + _number(result.metadata[key]) + r" \\")
    lines.extend(
        (
            r"实际年度最终储电量（kWh） & "
            + _number(result.intervals[-1].state_end.energy_kwh)
            + r" \\",
            r"\bottomrule\end{tabular}\end{table}",
            r"\begin{table}[htbp]\centering",
            r"\caption{三个调整发布时刻的全年实际交易记录}",
            r"\begin{tabular}{lrrr}\toprule 发布时间 & 非零调整笔数 & 大于物理阈值笔数 & 调整费用（元） \\\midrule",
        )
    )
    for hour, values in releases.items():
        lines.append(
            f"{hour}:00 & {values['nonzero_events']} & {values['material_events']} & {_number(values['cost_cny'])}"
            + r" \\"
        )
    lines.extend(
        (
            r"\bottomrule\end{tabular}\end{table}",
            r"\begin{figure}[htbp]\centering",
            r"\includegraphics[width=\linewidth]{generated/q3_dispatch_days.pdf}",
            r"\caption{四个指定日期的实际负载、光伏、购电计划及储能状态。电量按十分钟区间统计，SOC为状态边界值。}\label{fig:q3-dispatch}",
            r"\end{figure}",
        )
    )
    (output / "q3_result_tables.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 2, figsize=(10, 6.4), layout="constrained")
    for axis, (day, intervals) in zip(axes.flat, by_day.items(), strict=True):
        hours = [i.slot / 6 for i in intervals]
        for label, data in (
            ("Load", [i.load_kw / 6 for i in intervals]),
            ("PV", [i.pv_kw / 6 for i in intervals]),
            ("G0", [i.planned_purchase_kwh for i in intervals]),
            ("Gfinal", [i.adjusted_purchase_kwh for i in intervals]),
        ):
            axis.plot(hours, data, label=label, linewidth=1)
        axis.set(title=str(day), xlabel="Hour", ylabel="kWh / 10 min", xlim=(0, 24))
        axis.grid(alpha=0.2)
        axis.legend(fontsize=7, loc="upper right")
        storage_axis = axis.twinx()
        storage_axis.plot(
            [k / 6 for k in range(145)],
            [intervals[0].state_start.energy_kwh] + [i.state_end.energy_kwh for i in intervals],
            color="black",
            linestyle=":",
            linewidth=1,
            label="SOC",
        )
        storage_axis.set(ylabel="SOC (kWh)", ylim=(0, 12000))
        storage_axis.legend(fontsize=7, loc="upper left")
    for suffix in ("pdf", "png"):
        fig.savefig(output / f"q3_dispatch_days.{suffix}", dpi=160)
    plt.close(fig)
    names = (
        "q3_report.json",
        "q3_trajectory.csv",
        "q3_result_tables.tex",
        "q3_dispatch_days.pdf",
        "q3_dispatch_days.png",
    )
    manifest = {
        "case_id": "q3",
        "run_id": result.run_id,
        "is_synthetic": result.is_synthetic,
        "source_result_sha256": file_digest(output.parent / "domain_result.json"),
        "files_sha256": {name: file_digest(output / name) for name in names},
    }
    return write_json(output / "asset_manifest.json", manifest)
