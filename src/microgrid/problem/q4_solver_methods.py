"""Optional solver experiments, authorized by D_OPTIMIZATION_Q4."""

from __future__ import annotations

import numpy as np
from scipy.optimize import Bounds, LinearConstraint

from ..schemas import InputError

METHODS = ("scipy", "eliminate-fixed")


def solver_method_parameters(method):
    if method == "scipy":
        return None
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
