"""Read-only cost diagnostics; no fitted weights or dispatch decisions."""

from __future__ import annotations

import datetime as dt
import heapq
import math
from collections import defaultdict
from dataclasses import dataclass

from ..schemas import InputError
from .dispatch_feedback import Measurement
from .dispatch_milp import M
from .q4_common import ACTION_START, STEP, YEAR_END, energy_lower_bound, reserve_start_from_config
from .q4_evidence import json_rows, read_json

HORIZONS = ("0-6h", "6-12h", "12-18h", "18-24h")


@dataclass
class Errors:
    count: int = 0
    absolute: float = 0.0
    square: float = 0.0
    signed: float = 0.0
    positive: float = 0.0
    negative: float = 0.0
    shortfall_value: float = 0.0
    price_count: int = 0

    def add(self, actual, estimate, price=None):
        error = actual - estimate
        self.count += 1
        self.absolute += abs(error)
        self.square += error * error
        self.signed += error
        self.positive += max(error, 0.0)
        self.negative += max(-error, 0.0)
        if price is not None:
            self.shortfall_value += max(error, 0.0) * price
            self.price_count += 1

    def summary(self):
        return {
            "count": self.count,
            "bias_actual_minus_forecast": self.signed / self.count if self.count else None,
            "mae": self.absolute / self.count if self.count else None,
            "rmse": math.sqrt(self.square / self.count) if self.count else None,
            "positive_shortfall_sum": self.positive,
            "negative_surplus_sum": self.negative,
            "shortfall_value_at_actual_price_cny": self.shortfall_value
            if self.price_count
            else None,
        }


def historical_pv(inputs, issue_index, target_index):
    indices = [target_index - 144 * day for day in range(1, 8)]
    if min(indices) < 0 or max(indices) >= issue_index:
        raise InputError("seven-day PV diagnostic baseline is not visible at issue time")
    # Same weights and addition order as the approved Q4-2 historical baseline.
    return sum((1 / 7) * inputs.pv_kw[index] for index in indices)


def constraint_tags(slot, execution, net_error, case, reserve_start):
    state = execution["state_start"]["energy_kwh"]
    lower = energy_lower_bound(slot, reserve_start)
    tags = []
    event = (
        slot.time() == dt.time()
        or case == "q4_3"
        and slot.time() in (dt.time(6), dt.time(12), dt.time(18))
    )
    if not event:
        tags.append("contract_frozen_at_control_time")
    if state <= lower + 100.0:
        tags.append("soc_within_100kwh_of_lower_bound")
    if execution["action"]["discharge_kwh"] >= M - 1e-6:
        tags.append("executed_discharge_at_power_limit")
    if execution["charge_reduction_kwh"] > 1e-6:
        tags.append("planned_charge_reduced")
    if reserve_start is not None and slot >= reserve_start:
        tags.append("terminal_reserve_period")
    if net_error > 1e-6:
        tags.append("positive_net_load_error")
    return tags


def analyze_run(run, inputs):
    config, summary = read_json(run / "effective_config.json"), read_json(run / "summary.json")
    case = config["case_id"]
    reserve_start = reserve_start_from_config(config)
    forecast_count = 0
    errors = defaultdict(Errors)
    original_leads = defaultdict(Errors)
    role_errors = defaultdict(Errors)
    issued = {}
    for snapshot in json_rows(run / "forecasts.jsonl"):
        forecast_count += 1
        issue = dt.datetime.fromisoformat(snapshot["issue_time"])
        issue_index = inputs.index(issue)
        issued[issue] = {name: snapshot[name] for name in ("load_kwh", "pv_kwh", "prices")}
        for h, (load, pv, price) in enumerate(
            zip(snapshot["load_kwh"], snapshot["pv_kwh"], snapshot["prices"], strict=True)
        ):
            slot = issue + h * STEP
            if slot >= YEAR_END:
                break
            index = inputs.index(slot)
            horizon = HORIZONS[h // 36]
            actual_pv, actual_load = inputs.pv_kw[index], inputs.load_kw[index]
            baseline = historical_pv(inputs, issue_index, index)
            daypart = "actual_pv_gt_1kw" if actual_pv > 1.0 else "actual_pv_le_1kw"
            groups = (
                horizon,
                horizon + ":month:" + slot.strftime("%Y-%m"),
                horizon + ":" + daypart,
            )
            for group in groups:
                errors[(group, "current_pv_kw")].add(actual_pv, pv * 6)
                errors[(group, "historical_pv_kw")].add(actual_pv, baseline)
                errors[(group, "net_load_kwh")].add(
                    (actual_load - actual_pv) / 6, load - pv, inputs.prices[index]
                )
                errors[(group, "price_cny_per_kwh")].add(inputs.prices[index], price)
            roles = []
            if issue.time() == dt.time():
                roles.append("midnight_initial_contract")
            elif case == "q4_3" and slot.date() == issue.date():
                roles.append("daytime_contract_adjustment")
            if h < 36:
                roles.append("battery_control_frozen_snapshot")
            if slot.date() > issue.date():
                roles.append("cross_day_lookahead")
            for role in roles:
                role_errors[(role, "net_load_kwh")].add(
                    (actual_load - actual_pv) / 6, load - pv, inputs.prices[index]
                )
                role_errors[(role, "price_cny_per_kwh")].add(inputs.prices[index], price)
        if case == "q4_3":
            for trace in snapshot["traces"]["pv"]:
                valid = dt.datetime.fromisoformat(trace["valid_time"])
                # Match approved original-vintage scoring: ten-minute interval ending at valid_time.
                actual = inputs.pv_kw[inputs.index(valid - STEP)]
                for vintage in trace["versions"]:
                    original_leads[str(vintage["lead_hours"])].add(actual, vintage["forecast_kw"])

    tagged = defaultdict(lambda: defaultdict(float))
    net_groups = defaultdict(lambda: defaultdict(float))
    monthly = defaultdict(lambda: defaultdict(float))
    costs = defaultdict(float)
    control_errors = Errors()
    top = []
    count = 0
    bills = iter(json_rows(run / "cost_ledger.jsonl"))
    for k, row in enumerate(json_rows(run / "execution_feedback.jsonl")):
        slot = ACTION_START + k * STEP
        if row["slot_start"] != str(slot):
            raise InputError("diagnostic execution time grid mismatch")
        bill = next(bills, None)
        if bill is None or bill["slot_start"] != str(slot):
            raise InputError("diagnostic execution/cost time mismatch")
        issue = slot.replace(hour=(slot.hour // 6) * 6, minute=0)
        snapshot = issued.get(issue)
        if snapshot is None:
            raise InputError("missing active forecast for cost diagnostics")
        h = int((slot - issue) / STEP)
        execution = row["execution"]
        index = inputs.index(slot)
        measurement = Measurement(inputs.load_kw[index] / 6, inputs.pv_kw[index] / 6)
        predicted_net = snapshot["load_kwh"][h] - snapshot["pv_kwh"][h]
        actual_net = measurement.load_kwh - measurement.pv_kwh
        net_error = actual_net - predicted_net
        control_errors.add(actual_net, predicted_net, inputs.prices[index])
        group = (
            "shortfall"
            if net_error > 1e-6
            else "surplus"
            if net_error < -1e-6
            else "within_tolerance"
        )
        tags = constraint_tags(slot, execution, net_error, case, reserve_start)
        observations = {
            "interval_count": 1,
            "emergency_kwh": execution["emergency_kwh"],
            "unused_contract_kwh": execution["unused_grid_kwh"],
            "curtailed_pv_kwh": execution["curtailed_pv_kwh"],
            "soc_start_kwh_sum": execution["state_start"]["energy_kwh"],
        }
        for name in ("planned_cost_cny", "adjustment_cost_cny", "emergency_cost_cny"):
            costs[name] += bill[name]
            observations[name] = bill[name]
        observations["total_cost_cny"] = sum(
            bill[name] for name in ("planned_cost_cny", "adjustment_cost_cny", "emergency_cost_cny")
        )
        for target in [
            net_groups[group],
            monthly[slot.strftime("%Y-%m")],
            *(tagged[tag] for tag in tags),
        ]:
            for key, value in observations.items():
                target[key] += value
        item = {
            "slot_start": str(slot),
            "emergency_cost_cny": bill["emergency_cost_cny"],
            "emergency_kwh": execution["emergency_kwh"],
            "actual_price_cny_per_kwh": inputs.prices[index],
            "net_load_error_kwh": net_error,
            "actual_net_load_kwh": actual_net,
            "forecast_net_load_kwh": predicted_net,
            "soc_start_kwh": execution["state_start"]["energy_kwh"],
            "contract_kwh": execution["grid_kwh"],
            "tags": tags,
        }
        heapq.heappush(top, (bill["emergency_cost_cny"], k, item))
        if len(top) > 100:
            heapq.heappop(top)
        count += 1
    if next(bills, None) is not None or count != summary["interval_count"] or count != 48096:
        raise InputError("annual diagnostic log coverage mismatch")
    costs["total_cost_cny"] = sum(costs.values())
    if abs(costs["total_cost_cny"] - summary["total_cost_cny"]) > 0.01:
        raise InputError("diagnostic bill totals differ from original summary")

    def groups(values):
        result = {}
        for name, group in values.items():
            group = dict(group)
            group["mean_soc_start_kwh"] = group.pop("soc_start_kwh_sum") / group["interval_count"]
            result[name] = group
        return result

    return {
        "case_id": case,
        "run_id": summary["run_id"],
        "price_method": config["price_method"],
        "interval_count": count,
        "forecast_count": forecast_count,
        "costs": dict(costs),
        "emergency_cost_share": costs["emergency_cost_cny"] / costs["total_cost_cny"],
        "horizon_errors": {
            group + ":" + variable: stats.summary() for (group, variable), stats in errors.items()
        },
        "decision_role_errors": {
            role + ":" + variable: stats.summary()
            for (role, variable), stats in role_errors.items()
        },
        "original_vintage_lead_errors_kw": {
            lead: stats.summary() for lead, stats in original_leads.items()
        },
        "active_control_net_load_errors_kwh": control_errors.summary(),
        "constraint_tags": groups(tagged),
        "net_error_groups": groups(net_groups),
        "monthly": groups(monthly),
        "highest_emergency_cost_intervals": [item for _, _, item in sorted(top, reverse=True)],
    }
