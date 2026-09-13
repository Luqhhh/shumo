"""Three joint-error scenarios with shared procurement and battery actions.

Full scenario certificates live in immutable native plans. JSONL solver rows bind
those certificates by hash; replay reconstructs every shifted certificate.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace

import numpy as np
from scipy.sparse import block_diag, csc_matrix, vstack

from .contracts import ENERGY_ABS_TOL_KWH as TOL
from .q4_common import SCENARIO_MODEL_VERSION, STEP, Q4Error, scenario_procurement_parameters

PROBABILITIES = (0.25, 0.5, 0.25)
SHARED_FIELDS = (0, 1, 2, 9, 10, 11)


def is_active(forecast):
    trace = forecast.traces.get("scenario_procurement")
    return trace is not None and trace.get("active") is True


def certificate_digest(certificate):
    return hashlib.sha256(
        json.dumps(certificate, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def compact_solver_record(record):
    if "scenario_certificate" not in record:
        return record
    result = {key: value for key, value in record.items() if key != "scenario_certificate"}
    result["scenario_certificate_sha256"] = certificate_digest(record["scenario_certificate"])
    return result


def scenario_forecasts(forecast):
    trace = forecast.traces["scenario_procurement"]
    if forecast.model_version != SCENARIO_MODEL_VERSION or json.dumps(
        trace.get("parameters"), sort_keys=True
    ) != json.dumps(scenario_procurement_parameters(), sort_keys=True):
        raise Q4Error("invalid_forecast", "scenario parameter/model mismatch")
    points = {point["valid_time"]: point for point in trace["points"]}
    arrays = [[[], [], []] for _ in PROBABILITIES]
    for slot, load, pv, price in zip(
        forecast.slots, forecast.load_kwh, forecast.pv_kwh, forecast.prices, strict=True
    ):
        point = points[str(slot + STEP)]
        if (
            point["original_load_kw"] / 6 != load
            or point["original_pv_kw"] / 6 != pv
            or point["original_price"] != price
        ):
            raise Q4Error("invalid_forecast", "scenario not bound to original point forecasts")
        errors = point["joint_error_scenarios"]
        if len(errors) != 3:
            raise Q4Error("invalid_forecast", "scenario count mismatch")
        for index, error in enumerate(errors):
            deltas = [error[key] for key in ("load_error_kw", "pv_error_kw", "price_error")]
            if not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in deltas
            ) or (index == 1 and any(v != 0 for v in deltas)):
                raise Q4Error("invalid_forecast", "invalid scenario joint errors/nominal")
            arrays[index][0].append(max(0.0, load * 6 + deltas[0]) / 6)
            arrays[index][1].append(max(0.0, pv * 6 + deltas[1]) / 6)
            arrays[index][2].append(max(0.0, price + deltas[2]))
    # Preserve the nominal arrays bit for bit rather than multiplying/dividing.
    arrays[1] = [forecast.load_kwh, forecast.pv_kwh, forecast.prices]
    return tuple(
        replace(
            forecast,
            snapshot_id=forecast.snapshot_id + f":scenario-{index}",
            load_kwh=tuple(a[0]),
            pv_kwh=tuple(a[1]),
            prices=tuple(a[2]),
            traces={
                **forecast.traces,
                "scenario_procurement": {
                    "parameters": scenario_procurement_parameters(),
                    "active": False,
                    "component_index": index,
                },
            },
        )
        for index, a in enumerate(arrays)
    )


def build_scenario_problem(state, forecast, ledger, *, reserve_start):
    from .dispatch_milp import WIDTH, DispatchProblem, build_dispatch_problem

    components = scenario_forecasts(forecast)
    problems = [
        build_dispatch_problem(state, leaf, ledger, reserve_start=reserve_start)
        for leaf in components
    ]
    size = len(problems[0].objective)
    if any(p.permissions != problems[1].permissions or len(p.objective) != size for p in problems):
        raise Q4Error("invalid_model", "scenario structure/permissions mismatch")
    rows, columns, data = [], [], []
    count = 0
    for index in (0, 2):
        for k in range(len(forecast.slots)):
            for field in SHARED_FIELDS:
                rows.extend((count, count))
                columns.extend((index * size + k * WIDTH + field, size + k * WIDTH + field))
                data.extend((1.0, -1.0))
                count += 1
    shared = csc_matrix((data, (rows, columns)), shape=(count, size * 3))
    return DispatchProblem(
        problems[1].permissions,
        np.concatenate(
            [weight * p.objective for weight, p in zip(PROBABILITIES, problems, strict=True)]
        ),
        np.concatenate([p.integrality for p in problems]),
        np.concatenate([p.lower for p in problems]),
        np.concatenate([p.upper for p in problems]),
        vstack((block_diag([p.matrix for p in problems], format="csc"), shared), format="csc"),
        np.concatenate([*[p.rlo for p in problems], np.zeros(count)]),
        np.concatenate([*[p.rhi for p in problems], np.zeros(count)]),
    )


def solution_certificate(x, state, forecast, ledger):
    from .dispatch_milp import WIDTH, C, D

    n = len(forecast.slots)
    size = WIDTH * n + n + 1
    if len(x) != size * 3:
        raise Q4Error("solver_validation_failed", "scenario solution dimensions")
    raw = [
        [
            list(map(float, x[index * size + k * WIDTH : index * size + (k + 1) * WIDTH]))
            for k in range(n)
        ]
        for index in range(3)
    ]
    for flows in raw:
        for flow in flows:
            for field, value in enumerate(flow):
                if -1e-9 <= value < 0:
                    flow[field] = 0.0
    # Canonicalize equality-linked fields to the nominal action only within
    # roundoff tolerance; the complete physical and objective checks follow.
    for index in (0, 2):
        for k in range(n):
            for field in SHARED_FIELDS:
                if abs(raw[index][k][field] - raw[1][k][field]) > 1e-9:
                    raise Q4Error("solver_validation_failed", "scenario shared action residual")
                raw[index][k][field] = raw[1][k][field]
    components = []
    for flows in raw:
        energy = [state.energy_kwh]
        for flow in flows:
            energy.append(energy[-1] + 0.9 * flow[C] - flow[D] / 0.9)
        components.append({"flows": flows, "energy": energy})
    return {"schema_version": 1, "probabilities": list(PROBABILITIES), "components": components}


def scenario_value(certificate, forecast, ledger):
    from .dispatch_milp import objective_value

    leaves = scenario_forecasts(forecast)
    permissions = ledger.permissions(forecast.slots[0], forecast.slots)
    return sum(
        weight * objective_value(component["flows"], component["energy"], leaf, permissions)
        for weight, component, leaf in zip(
            PROBABILITIES, certificate["components"], leaves, strict=True
        )
    )


def validate_scenarios(plan, state, forecast, ledger, *, reserve_start):
    from .dispatch_milp import DispatchPlan, objective_value, validate_dispatch

    issues = []
    try:
        certificate = plan.solver_record["scenario_certificate"]
        if (
            certificate["schema_version"] != 1
            or certificate["probabilities"] != list(PROBABILITIES)
            or len(certificate["components"]) != 3
            or plan.forecast_id != forecast.snapshot_id
        ):
            return ("scenario_certificate_schema_or_forecast",)
        leaves = scenario_forecasts(forecast)
        permissions = ledger.permissions(forecast.slots[0], forecast.slots)
        for index, (component, leaf) in enumerate(
            zip(certificate["components"], leaves, strict=True)
        ):
            flows = tuple(tuple(row) for row in component["flows"])
            energy = tuple(component["energy"])
            if not all(math.isfinite(v) for row in flows for v in row) or not all(
                math.isfinite(v) for v in energy
            ):
                return ("scenario_nonfinite",)
            value = objective_value(flows, energy, leaf, permissions)
            leaf_plan = DispatchPlan(
                plan.solve_id, leaf.snapshot_id, flows, energy, value, plan.absolute_gap, {}
            )
            issues.extend(
                f"scenario-{index}:{item}"
                for item in validate_dispatch(
                    leaf_plan, state, leaf, ledger, reserve_start=reserve_start
                )
            )
            if index == 1:
                if flows != plan.flows or energy != plan.energy:
                    issues.append("scenario_nominal_plan_mismatch")
            else:
                for k, (flow, nominal) in enumerate(zip(flows, plan.flows, strict=True)):
                    if max(abs(flow[field] - nominal[field]) for field in SHARED_FIELDS) > TOL:
                        issues.append(f"scenario-{index}:{k}:unshared_decision")
        value = scenario_value(certificate, forecast, ledger)
        if not math.isfinite(plan.objective) or abs(value - plan.objective) > 1e-5:
            issues.append("scenario_expected_objective")
    except (KeyError, TypeError, ValueError, IndexError, Q4Error, OverflowError):
        issues.append("scenario_certificate_invalid")
    return tuple(issues)


def reuse_scenario_tail(previous, state, forecast, ledger, *, reserve_start, diagnostics=None):
    from .dispatch_milp import A, B, W, contract_hash, forecast_reserve_start, validate_dispatch

    def reject(reason):
        if diagnostics is not None:
            diagnostics["tail_reuse_rejected:" + reason] += 1
        return None

    if reserve_start != forecast_reserve_start(forecast):
        return reject("reserve_version")
    if (
        previous.forecast_id != forecast.snapshot_id
        or len(previous.flows) != len(forecast.slots) + 1
    ):
        return reject("forecast_or_window")
    if previous.energy[1] != state.energy_kwh:
        return reject("actual_soc")
    permissions = ledger.permissions(forecast.slots[0], forecast.slots)
    if any(permission in ("new_day", "adjustable") for permission in permissions):
        return reject("contract_permissions")
    try:
        old = previous.solver_record["scenario_certificate"]
        components = []
        for component in old["components"]:
            flows = [
                [0.0 if field in (A, B, W) else value for field, value in enumerate(flow)]
                for flow in component["flows"][1:]
            ]
            components.append(
                {"flows": flows, "energy": [state.energy_kwh, *component["energy"][2:]]}
            )
        certificate = {
            "schema_version": 1,
            "probabilities": list(PROBABILITIES),
            "components": components,
        }
        value = scenario_value(certificate, forecast, ledger)
        if previous.absolute_gap > 1e-4 * max(abs(value), 1e-10):
            return reject("absolute_gap")
        nominal = components[1]
        shift = previous.shift_count + 1
        record = {
            **previous.solver_record,
            "scenario_certificate": certificate,
            "reused_tail": True,
            "parent_solve_id": previous.solve_id,
            "shift_count": shift,
            "elapsed_seconds": 0.0,
            "objective": value,
            "gap": previous.absolute_gap / max(abs(value), 1e-10),
            "dual_bound": value - previous.absolute_gap,
            "absolute_gap_bound": previous.absolute_gap,
            "window_start": str(forecast.slots[0]),
            "contract_hash": contract_hash(ledger, forecast),
        }
        result = replace(
            previous,
            flows=tuple(tuple(row) for row in nominal["flows"]),
            energy=tuple(nominal["energy"]),
            objective=value,
            solver_record=record,
            shift_count=shift,
        )
        if validate_dispatch(result, state, forecast, ledger, reserve_start=reserve_start):
            return reject("constraint_validation")
        return result
    except (KeyError, TypeError, ValueError, IndexError, Q4Error):
        return reject("scenario_certificate")
