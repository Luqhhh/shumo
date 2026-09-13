from __future__ import annotations

import numpy as np
import pytest
from scipy.optimize import Bounds, LinearConstraint, OptimizeResult, milp
from scipy.sparse import csc_matrix

from microgrid.problem.q4_solver_methods import solve_with_method, solver_method_parameters
from microgrid.schemas import InputError


def test_fixed_elimination_reconstructs_original_problem_and_integer_layout():
    c = np.array([0.0, 3.0, 1.0])
    bounds = Bounds([2.0, 0.0, 0.0], [2.0, 4.0, 4.0])
    matrix = csc_matrix([[1.0, 1.0, 1.0], [0.0, 1.0, -1.0]])
    constraints = LinearConstraint(matrix, [5.0, -np.inf], [5.0, 0.0])
    calls = []

    def original(cost, **kwargs):
        calls.append((cost.copy(), kwargs))
        return milp(cost, **kwargs)

    integrality = np.array([0, 1, 1])
    result = solve_with_method(
        "eliminate-fixed",
        original,
        c,
        integrality=integrality,
        bounds=bounds,
        constraints=constraints,
        options={},
    )
    assert len(calls[0][0]) == 2
    np.testing.assert_array_equal(calls[0][1]["integrality"], [1, 1])
    np.testing.assert_array_equal(result.x, [2.0, 0.0, 3.0])
    assert result.fun == c @ result.x == 3.0
    np.testing.assert_allclose(matrix @ result.x, [5.0, -3.0])
    assert result.solver_method_record["fixed_variable_count"] == 1
    assert result.solver_method_record["full_variable_count"] == 3


@pytest.mark.parametrize(
    "cost, lower, upper", [([5.0, 3.0], [2.0, 0.0], [2.0, 4.0]), ([0.0], [2.0], [2.0])]
)
def test_fixed_nonzero_offset_and_no_free_variables_use_original_stopping_rule(cost, lower, upper):
    c = np.array(cost)
    bounds = Bounds(lower, upper)
    constraint = LinearConstraint(csc_matrix(np.eye(len(c))), lower, upper)
    seen = []

    def original(c, **kwargs):
        seen.append(len(c))
        return OptimizeResult(status=0, x=np.array(lower), fun=float(c @ lower))

    result = solve_with_method(
        "eliminate-fixed",
        original,
        c,
        integrality=np.zeros(len(c)),
        bounds=bounds,
        constraints=constraint,
        options={"mip_rel_gap": 1e-4},
    )
    assert seen == [len(c)] and result.solver_method_record["fallback"]


def test_fixed_solver_failure_is_not_reconstructed_as_success():
    c = np.zeros(2)
    result = solve_with_method(
        "eliminate-fixed",
        lambda *args, **kwargs: OptimizeResult(status=2, x=None, fun=None),
        c,
        integrality=np.zeros(2),
        bounds=Bounds([2, 0], [2, 1]),
        constraints=LinearConstraint(csc_matrix([[1, 1]]), 4, 4),
        options={},
    )
    assert result.status == 2 and result.x is None and result.fun is None


def test_unknown_solver_method_is_rejected():
    with pytest.raises(InputError):
        solver_method_parameters("relax-everything")


def test_lp_certificate_canonical_modes_at_same_lower_bound():
    from microgrid.problem.q4_solver_methods import _lp_certificate

    c = np.zeros(14)
    c[1] = 1.0
    integer = np.zeros(14)
    integer[[7, 8, 11]] = 1
    x = np.zeros(14)
    x[1] = 2
    x[[7, 8, 11]] = [0.2, 0.0, 0.0]
    bound = Bounds(np.zeros(14), np.full(14, 10.0))
    constraint = LinearConstraint(csc_matrix(np.eye(14)), np.zeros(14), np.full(14, 10.0))
    candidate, reason = _lp_certificate(x, c, integer, bound, constraint, 2.0)
    assert reason is None and candidate[7] == 1.0 and candidate[1] == 2.0
    assert candidate[8] == candidate[11] == 0.0 and c @ candidate == 2.0


@pytest.mark.parametrize("broken", ["matrix", "objective", "integrality", "nonfinite"])
def test_lp_certificate_rejects_rounding_without_full_certificate(broken):
    from microgrid.problem.q4_solver_methods import _lp_certificate

    c = np.zeros(14)
    c[1] = 1.0
    integer = np.zeros(14)
    integer[[7, 8, 11]] = 1
    x = np.zeros(14)
    x[1] = 2
    bound = Bounds(np.zeros(14), np.full(14, 10.0))
    matrix = np.eye(14)
    lower = np.zeros(14)
    upper = np.full(14, 10.0)
    lower_bound = 2.0
    if broken == "matrix":
        upper[7] = 0.5
    elif broken == "objective":
        lower_bound = 1.0
    elif broken == "integrality":
        integer[1] = 1
    else:
        x[1] = np.nan
    candidate, reason = _lp_certificate(
        x, c, integer, bound, LinearConstraint(csc_matrix(matrix), lower, upper), lower_bound
    )
    assert candidate is None and reason


def test_lp_failure_falls_back_to_unchanged_original_milp():
    c = np.zeros(14)
    integer = np.zeros(14)
    integer[[7, 8, 11]] = 1
    bounds = Bounds(np.zeros(14), np.ones(14))
    constraint = LinearConstraint(csc_matrix(np.eye(14)), 0, 1)
    seen = []

    def original(cost, **kwargs):
        seen.append(kwargs["integrality"].copy())
        return (
            OptimizeResult(status=2, x=None, fun=None)
            if len(seen) == 1
            else OptimizeResult(status=0, x=np.zeros(14), fun=0)
        )

    result = solve_with_method(
        "lp-certified",
        original,
        c,
        integrality=integer,
        bounds=bounds,
        constraints=constraint,
        options={},
    )
    assert result.status == 0 and not result.solver_method_record["certified"]
    np.testing.assert_array_equal(seen[0], np.zeros(14))
    np.testing.assert_array_equal(seen[1], integer)
