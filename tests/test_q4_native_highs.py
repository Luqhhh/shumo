from __future__ import annotations

import builtins
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.optimize import Bounds, LinearConstraint
from scipy.sparse import csc_matrix

from microgrid.problem.q4_native_highs import NativeHighs
from microgrid.schemas import InputError


def test_native_pinned_version_is_required(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "highspy",
        SimpleNamespace(Highs=lambda: SimpleNamespace(version=lambda: "9.9.9")),
    )
    with pytest.raises(InputError, match="exactly highspy 1.12.0"):
        NativeHighs()


def test_native_missing_optional_dependency_is_explicit(monkeypatch):
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "highspy":
            raise ImportError("missing native extra")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(InputError, match="locked solver-native extra"):
        NativeHighs()


def test_native_model_updates_preserve_bounds_costs_and_certificate():
    pytest.importorskip("highspy", reason="locked solver-native extra is optional")
    solver = NativeHighs()
    matrix = csc_matrix([[1.0, 1.0], [0.0, 1.0]])
    integer = np.array([0, 1])
    options = {
        "presolve": True,
        "mip_rel_gap": 1e-4,
        "time_limit": 10.0,
        "mip_feasibility_tolerance": 1e-9,
        "primal_feasibility_tolerance": 1e-8,
    }
    a = solver(
        np.array([2.0, 1.0]),
        integrality=integer,
        bounds=Bounds([0, 0], [10, 1]),
        constraints=LinearConstraint(matrix, [2, 0], [np.inf, 1]),
        options=options,
    )
    b = solver(
        np.array([1.0, 5.0]),
        integrality=integer,
        bounds=Bounds([0, 0], [10, 1]),
        constraints=LinearConstraint(matrix, [3, 0], [np.inf, 1]),
        options=options,
    )
    assert a.status == b.status == 0 and a.fun == b.fun == 3.0
    assert (
        b.solver_method_record["model_reused"] and b.solver_method_record["model_readback_checked"]
    )
    np.testing.assert_allclose(a.x, [1, 1])
    np.testing.assert_allclose(b.x, [3, 0])
