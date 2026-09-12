"""Optional solver experiments, authorized by D_OPTIMIZATION_Q4."""

from __future__ import annotations

import numpy as np
from scipy.optimize import Bounds, LinearConstraint

from ..schemas import InputError

METHODS = ("scipy", "eliminate-fixed", "lp-certified")


def solver_method_parameters(method):
    if method == "scipy":
        return None
    if method == "lp-certified":
        return {
            "schema_version": 1,
            "method": method,
            "certificate": "optimal LP lower bound; original physical modes, matrix, bounds and integrality; objective within 1e-5",
            "fallback": "original full MILP",
            "physical_tolerance_kwh": 1e-6,
        }
    if method != "eliminate-fixed":
        raise InputError("unknown Q4 solver experiment")
    return {
        "schema_version": 1,
        "method": method,
        "fixed": "finite equal lower and upper bounds",
        "objective_offset": "fallback to original MILP if nonzero",
        "certificate": "reconstruct full original vector; retain full matrix and physical checks",
    }


def solve_with_method(method, original, c, *, integrality, bounds, constraints, options):
    solver_method_parameters(method)
    if method == "scipy":
        return original(
            c, integrality=integrality, bounds=bounds, constraints=constraints, options=options
        )
    if method == "lp-certified":
        return _solve_lp_certified(
            original,
            c,
            integrality=integrality,
            bounds=bounds,
            constraints=constraints,
            options=options,
        )
    fixed = np.flatnonzero(bounds.lb == bounds.ub)
    free = np.flatnonzero(bounds.lb != bounds.ub)
    values = bounds.lb[fixed]
    if np.any(~np.isfinite(values)):
        raise InputError("fixed variable is nonfinite")
    offset = float(c[fixed] @ values)
    if offset != 0 or not len(free):
        result = original(
            c, integrality=integrality, bounds=bounds, constraints=constraints, options=options
        )
        result.solver_method_record = {
            "method": method,
            "fallback": "nonzero offset or no free variables",
            "fixed_variable_count": len(fixed),
        }
        return result
    shift = constraints.A[:, fixed] @ values
    result = original(
        c[free],
        integrality=integrality[free],
        bounds=Bounds(bounds.lb[free], bounds.ub[free]),
        constraints=LinearConstraint(
            constraints.A[:, free], constraints.lb - shift, constraints.ub - shift
        ),
        options=options,
    )
    if result.x is not None:
        full = np.array(bounds.lb, copy=True)
        full[free] = result.x
        result.x = full
    result.solver_method_record = {
        "method": method,
        "fixed_variable_count": len(fixed),
        "reduced_variable_count": len(free),
        "full_variable_count": len(c),
        "objective_offset": offset,
        "fallback": None,
    }
    return result


def _lp_certificate(x, c, integrality, bounds, constraints, lower_bound):
    """Canonical physical modes must satisfy the original full integer problem."""
    if len(c) <= 1 or (len(c) - 1) % 13:
        return None, "unknown dispatch layout"
    count = (len(c) - 1) // 13
    mode_columns = np.array([12 * k + field for k in range(count) for field in (7, 8, 11)])
    if not np.array_equal(np.flatnonzero(integrality), mode_columns) or not np.all(
        integrality[mode_columns] == 1
    ):
        return None, "unknown integrality layout"
    candidate = np.array(x, dtype=float, copy=True)
    if (
        len(candidate) != len(c)
        or not np.all(np.isfinite(candidate))
        or not np.isfinite(lower_bound)
    ):
        return None, "nonfinite LP certificate"
    for k in range(count):
        for mode, physical in ((7, 1), (8, 3), (11, 9)):
            candidate[12 * k + mode] = float(candidate[12 * k + physical] > 1e-6)
    ax = constraints.A @ candidate
    residual = max(
        float(np.max(np.maximum(bounds.lb - candidate, 0))),
        float(np.max(np.maximum(candidate - bounds.ub, 0))),
        float(np.max(np.maximum(constraints.lb - ax, 0))),
        float(np.max(np.maximum(ax - constraints.ub, 0))),
    )
    if residual > 1e-6:
        return None, "original matrix or bounds residual"
    if abs(float(c @ candidate) - float(lower_bound)) > 1e-5:
        return None, "objective does not attain LP lower bound"
    return candidate, None


def _solve_lp_certified(original, c, *, integrality, bounds, constraints, options):
    import time

    started = time.perf_counter()
    lp = original(
        c,
        integrality=np.zeros_like(integrality),
        bounds=bounds,
        constraints=constraints,
        options=options,
    )
    lp_seconds = time.perf_counter() - started
    reason = "LP is not optimal with finite objective"
    if lp.status == 0 and lp.x is not None and lp.fun is not None and np.isfinite(lp.fun):
        candidate, reason = _lp_certificate(lp.x, c, integrality, bounds, constraints, lp.fun)
        if candidate is not None:
            lp.x = candidate
            lp.mip_dual_bound = float(lp.fun)
            lp.mip_gap = 0.0
            lp.mip_node_count = 0
            lp.message = (
                "Certified original feasible integer modes attaining optimal LP lower bound"
            )
            lp.solver_method_record = {
                "method": "lp-certified",
                "certified": True,
                "lp_seconds": lp_seconds,
                "fallback_seconds": 0.0,
                "fallback_reason": None,
                "lp_lower_bound": float(lp.fun),
            }
            return lp
    started = time.perf_counter()
    result = original(
        c, integrality=integrality, bounds=bounds, constraints=constraints, options=options
    )
    result.solver_method_record = {
        "method": "lp-certified",
        "certified": False,
        "lp_seconds": lp_seconds,
        "fallback_seconds": time.perf_counter() - started,
        "fallback_reason": reason,
        "lp_lower_bound": float(lp.fun)
        if lp.status == 0 and lp.fun is not None and np.isfinite(lp.fun)
        else None,
    }
    return result
