"""Frozen pre-cache matrix builder from 97cc6e9; equivalence oracle only.

Original full-file SHA256: cc8891b760aef2fe62ae1caa005f5f3155a6d15a39dce0a8fc5b05d09e131bf2. Keep its row and coefficient construction
independent of the new CSC template/scatter implementation.
"""

import numpy as np
from scipy.optimize import LinearConstraint
from scipy.sparse import coo_matrix

from microgrid.problem.contracts import ENERGY_ABS_TOL_KWH as TOL
from microgrid.problem.contracts import MAX_BUS_ENERGY_KWH as M
from microgrid.problem.contracts import BatteryState
from microgrid.problem.dispatch_milp import (
    WIDTH,
    A,
    B,
    C,
    D,
    G,
    P,
    S,
    U,
    V,
    W,
    Y,
    Z,
    forecast_reserve_start,
    variable_indices,
)
from microgrid.problem.purchase_ledger import PurchaseLedger
from microgrid.problem.q4_common import RESERVE_START, STEP, YEAR_END, Q4Error, energy_lower_bound
from microgrid.problem.q4_forecasts import ForecastSnapshot


def legacy_problem(
    state: BatteryState,
    forecast: ForecastSnapshot,
    ledger: PurchaseLedger,
    *,
    reserve_start=RESERVE_START,
    performance=None,
):
    n = len(forecast.slots)
    if reserve_start != forecast_reserve_start(forecast):
        raise Q4Error("config_mismatch", "dispatch reserve does not match forecast model version")
    if state.energy_kwh < energy_lower_bound(forecast.slots[0], reserve_start) - TOL:
        raise Q4Error(
            "terminal_reserve_infeasible", "current actual SOC below approved lower bound"
        )
    permissions = ledger.permissions(forecast.slots[0], forecast.slots)
    columns = variable_indices(n)
    size = WIDTH * n + n + 1
    lower, upper = np.zeros(size), np.full(size, np.inf)
    objective, integrality = np.zeros(size), np.zeros(size, dtype=int)
    row_index, col_index, coefficients, rlo, rhi = [], [], [], [], []

    def row(terms, lo=-np.inf, hi=np.inf):
        index = len(rlo)
        for column, coefficient in terms:
            row_index.append(index)
            col_index.append(column)
            coefficients.append(coefficient)
        rlo.append(lo)
        rhi.append(hi)

    ebase = WIDTH * n
    lower[ebase:] = 1200
    upper[ebase:] = 10800
    lower[ebase] = upper[ebase] = state.energy_kwh
    objective[-1] = -forecast.terminal_value
    for k, (load, pv, price, permission, slot) in enumerate(
        zip(
            forecast.load_kwh,
            forecast.pv_kwh,
            forecast.prices,
            permissions,
            forecast.slots,
            strict=True,
        )
    ):

        def idx(field, column=columns[k]):
            return column[field]

        previous = ledger.committed(slot) if permission in ("fixed", "adjustable") else 0.0
        gmax = previous if permission == "fixed" else max(previous, load + M)
        upper[idx(G)] = gmax
        if permission == "fixed":
            lower[idx(G)] = previous
        if permission in ("new_day", "lookahead"):
            objective[idx(G)] = price
        upper[idx(C)] = upper[idx(D)] = M
        upper[idx(D)] = min(M, load)
        upper[idx(U)] = load
        upper[idx(P)] = upper[idx(S)] = pv
        upper[idx(V)] = gmax
        for field in (Z, Y, W):
            upper[idx(field)] = 1
            integrality[idx(field)] = 1
        upper[idx(A)] = upper[idx(B)] = upper[idx(W)] = 0
        if permission == "adjustable":
            upper[idx(A)], upper[idx(B)], upper[idx(W)] = gmax, previous, 1
            objective[idx(A)], objective[idx(B)] = (
                1.5 * forecast.prices[0],
                0.5 * forecast.prices[0],
            )
            row(((idx(G), 1), (idx(A), -1), (idx(B), 1)), previous, previous)
            row(((idx(A), 1), (idx(W), -gmax)), hi=0)
            row(((idx(B), 1), (idx(W), previous)), hi=previous)
        objective[idx(U)] = 5 * price
        row(
            ((idx(G), 1), (idx(P), 1), (idx(D), 1), (idx(U), 1), (idx(C), -1), (idx(V), -1)),
            load,
            load,
        )
        row(((idx(P), 1), (idx(S), 1)), pv, pv)
        row(((ebase + k + 1, 1), (ebase + k, -1), (idx(C), -0.9), (idx(D), 1 / 0.9)), 0, 0)
        row(((idx(V), 1), (idx(G), -1)), hi=0)
        row(((idx(C), 1), (idx(Z), -M)), hi=0)
        row(((idx(D), 1), (idx(Z), M)), hi=M)
        row(((idx(U), 1), (idx(Y), -load)), hi=0)
        row(((idx(C), 1), (idx(Y), M)), hi=M)
        row(((idx(V), 1), (idx(Y), gmax)), hi=gmax)
        row(((idx(S), 1), (idx(Y), pv)), hi=pv)
        remaining = int((YEAR_END - (slot + STEP)) / STEP)
        lower[ebase + k + 1] = max(
            energy_lower_bound(slot + STEP, reserve_start), 6000 - remaining * 0.9 * M
        )
        upper[ebase + k + 1] = min(10800, 6000 + remaining * M / 0.9)
    if forecast.slots[-1] + STEP == YEAR_END:
        lower[-1] = upper[-1] = 6000
    matrix = coo_matrix((coefficients, (row_index, col_index)), shape=(len(rlo), size)).tocsc()
    LinearConstraint(matrix, np.array(rlo), np.array(rhi))
    return objective, integrality, lower, upper, matrix, np.array(rlo), np.array(rhi)
