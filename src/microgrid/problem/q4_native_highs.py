"""Pinned native HiGHS adapter with bounded immutable-layout model reuse."""

from __future__ import annotations

from collections import OrderedDict
from functools import lru_cache

import numpy as np
from scipy.optimize import OptimizeResult
from scipy.sparse import csc_matrix

from ..schemas import InputError


class NativeHighs:
    def __init__(self):
        try:
            import highspy
        except ImportError as exc:
            raise InputError("native-highs requires the locked solver-native extra") from exc
        self.api = highspy
        self.version = highspy.Highs().version()
        if self.version != "1.12.0":
            raise InputError("native-highs requires exactly highspy 1.12.0")
        self.pool = OrderedDict()

    def __call__(self, c, *, integrality, bounds, constraints, options):
        api = self.api
        matrix = constraints.A
        key = (
            matrix.shape,
            matrix.indptr.tobytes(),
            matrix.indices.tobytes(),
            integrality.tobytes(),
        )
        entry = self.pool.pop(key, None)
        warnings = []

        def checked(status, operation, allow_small_warning=False):
            if status == api.HighsStatus.kWarning and allow_small_warning:
                warnings.append(operation)
            elif status != api.HighsStatus.kOk:
                raise InputError(f"native HiGHS {operation} failed: {status}")

        if entry is None:
            h = api.Highs()
            checked(h.setOptionValue("output_flag", False), "output setting")
            checked(h.setOptionValue("threads", 1), "thread setting")
            lp = api.HighsLp()
            lp.num_col_ = len(c)
            lp.num_row_ = matrix.shape[0]
            lp.col_cost_ = c
            lp.col_lower_ = bounds.lb
            lp.col_upper_ = bounds.ub
            lp.row_lower_ = constraints.lb
            lp.row_upper_ = constraints.ub
            lp.integrality_ = [
                api.HighsVarType.kInteger if value else api.HighsVarType.kContinuous
                for value in integrality
            ]
            lp.a_matrix_.format_ = api.MatrixFormat.kColwise
            lp.a_matrix_.start_ = matrix.indptr
            lp.a_matrix_.index_ = matrix.indices
            lp.a_matrix_.value_ = matrix.data
            checked(h.passModel(lp), "pass model", True)
        else:
            h, old_data, old_lb, old_ub = entry
            indices = np.arange(len(c), dtype=np.int32)
            checked(h.changeColsCost(len(c), indices, c), "change costs")
            checked(
                h.changeColsBounds(len(c), indices, bounds.lb, bounds.ub), "change column bounds"
            )
            for row in np.flatnonzero((old_lb != constraints.lb) | (old_ub != constraints.ub)):
                checked(
                    h.changeRowBounds(
                        int(row), float(constraints.lb[row]), float(constraints.ub[row])
                    ),
                    "change row bounds",
                )
            for position in np.flatnonzero(old_data != matrix.data):
                col = int(np.searchsorted(matrix.indptr, position, side="right") - 1)
                checked(
                    h.changeCoeff(int(matrix.indices[position]), col, float(matrix.data[position])),
                    "change coefficient",
                    True,
                )
        for name, value in options.items():
            native_value = ("on" if value else "off") if name == "presolve" else value
            checked(h.setOptionValue(name, native_value), "set " + name)
            status, readback = h.getOptionValue(name)
            checked(status, "read " + name)
            if readback != native_value:
                raise InputError("native HiGHS option readback mismatch: " + name)
        status, small = h.getOptionValue("small_matrix_value")
        checked(status, "read small matrix threshold")
        if small != 1e-9:
            raise InputError(
                "native HiGHS small coefficient threshold differs from pinned baseline"
            )
        stored = h.getLp()
        for actual, expected in (
            (stored.col_cost_, c),
            (stored.col_lower_, bounds.lb),
            (stored.col_upper_, bounds.ub),
            (stored.row_lower_, constraints.lb),
            (stored.row_upper_, constraints.ub),
        ):
            if not np.array_equal(np.asarray(actual), expected):
                raise InputError("native HiGHS model readback mismatch")
        if (
            stored.num_col_ != len(c)
            or stored.num_row_ != matrix.shape[0]
            or [int(value) for value in stored.integrality_] != list(integrality.astype(int))
        ):
            raise InputError("native HiGHS dimensions/integrality mismatch")
        native_matrix = csc_matrix(
            (stored.a_matrix_.value_, stored.a_matrix_.index_, stored.a_matrix_.start_),
            shape=matrix.shape,
        )
        delta = native_matrix - matrix
        if delta.nnz and np.max(np.abs(delta.data)) > small:
            raise InputError(
                "native HiGHS coefficient readback differs beyond the pinned small coefficient threshold"
            )
        self.pool[key] = (
            h,
            matrix.data.copy(),
            np.array(constraints.lb, copy=True),
            np.array(constraints.ub, copy=True),
        )
        while len(self.pool) > 32:
            self.pool.popitem(last=False)
        checked(h.run(), "solve")
        status = h.getModelStatus()
        info = h.getInfo()
        solution = h.getSolution()
        ok = status == api.HighsModelStatus.kOptimal
        result = OptimizeResult(
            status=0 if ok else 1 if status == api.HighsModelStatus.kTimeLimit else 2,
            success=ok,
            message=h.modelStatusToString(status),
            x=np.array(solution.col_value) if solution.value_valid else None,
            fun=info.objective_function_value if solution.value_valid else None,
            mip_gap=info.mip_gap,
            mip_dual_bound=info.mip_dual_bound,
            mip_node_count=info.mip_node_count,
        )
        result.solver_method_record = {
            "method": "native-highs",
            "highspy_version": self.version,
            "model_reused": entry is not None,
            "cache_size": len(self.pool),
            "cache_limit": 32,
            "model_readback_checked": True,
            "small_coefficient_threshold": small,
            "small_coefficient_warning_operations": warnings,
            "threads": 1,
        }
        return result


@lru_cache(maxsize=1)
def native_highs():
    return NativeHighs()
