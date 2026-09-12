# Q2 Rolling Optimization Model Design

## Status and scope

This design turns the Q2 placeholder runner into a testable, strictly causal
engineering model. It does not approve any decision, export an official
workbook, or create a final submission. The formal `q2` dispatcher remains
blocked while any required decision is not fully approved by the team.

The implementation covers the Q2 model only:

- actual load and PV inputs from the explicit B-side input bundle;
- a causal 24-hour rolling MILP with one ten-minute action executed per step;
- planned normal purchase, battery action, PV use, SOC continuity, and actual
  replay with emergency purchase;
- independent validation, result serialization, and reproducible metadata;
- tests for data semantics, causality, optimization constraints, replay, and
  approval gating.

Q3 forecast-vintage selection, Q3 contract adjustments, Q4 price forecasts,
and official Excel export are out of scope.

## Decisions and implementation gate

The model consumes the following already approved shared semantics:

- `D_TIME_INTERNAL`: 144 ten-minute intervals per natural day; source power is
  aligned to the right endpoint; ten-minute energy is `power_kw * 1/6`.
- `D_EFF`: bus-side charge/discharge energy, efficiency `0.9` in both
  directions, and the 5000 kW bus-side limit.
- `D_STATE`: January is a standby initialization period from 6000 kWh;
  February onward is continuous with SOC bounds 1200–10800 kWh.
- `D_INFO`: an item is available only when `available_at <= decision_time`.

The formal Q2 runner must additionally require these decision cards before it
can run:

- `D_MODEL_Q2` — rolling forecast and optimization policy;
- `D_MPC` — ten-minute action cadence and continuous rolling control;
- `D_TERMINAL` — rolling-window terminal value policy;
- `D_YEAR_BOUNDARY` — annual terminal SOC policy;
- `D_SETTLE` — planned, adjusted-total, and emergency settlement semantics.

The implementation may use direct, clearly labelled engineering functions in
tests while these cards are proposed. It must not change their status or
confirmation fields. The formal runner must raise `PendingDecisionError`
until all of the above are fully approved.

## Architecture

### Input and forecasting boundary

`q2_inputs.py` remains the only source adapter. The model receives typed
records rather than opening workbooks. A new Q2 forecast module constructs a
forecast from actual history and exposes the information set used at the
current decision time.

For each target ten-minute slot, the baseline is the non-negative weighted
combination of the same slot from 7, 14, 21, and 28 days earlier. The caller
supplies the four weights; the default engineering candidate is equal weights
and is not an approval of a final coefficient choice. The latest visible
residual series may be fit with a bounded AR(1) coefficient and added to the
baseline. Forecasts are clipped at zero only after the complete forecast is
formed. No actual value whose interval end is after the decision time may
enter the forecast, residual fit, or weight fit.

The forecast API records `decision_time`, `valid_time`, `available_at`,
`training_cutoff`, model version, data version, source hashes, and fallback
reasons. Q2 starts its formal action period on 2025-02-01, so the January
history supplies the four weekly lags. A small synthetic fixture may pass an
explicitly shorter history only when its fallback is recorded and asserted.

### Rolling MILP

For a horizon of `H=144` ten-minute intervals, the optimizer creates these
variables for each horizon interval `j`:

- `q_plan[j] >= 0`: normal planned grid purchase in kWh;
- `c[j] >= 0`, `d[j] >= 0`: bus-side charge and discharge energy in kWh;
- `pv_used[j] >= 0`: forecast PV consumed at the bus;
- `e_plan[j] >= 0`: forecast-scenario emergency recourse, penalized at five
  times the interval price. It is a planning-side feasibility slack against the
  *forecast* load, not a settlement quantity — see "Emergency recourse" below;
- `E[j]`: battery-internal SOC at each boundary, including `E[0]` and
  `E[H]`;
- binary `z[j]`: charge/discharge mutual exclusion.

For every interval, the optimization uses the inequality

```text
q_plan[j] + e_plan[j] + d[j] + pv_used[j] >= load_forecast[j] + c[j]
```

with `0 <= pv_used[j] <= pv_forecast[j]`, `0 <= c[j], d[j] <= 5000/6`,
`1200 <= E[j] <= 10800`, and

```text
E[j+1] = E[j] + 0.9*c[j] - d[j]/0.9
```

The binary constraints select at most one of charge and discharge. The
objective is planned normal purchase cost plus a linear terminal value. The
terminal value is parameterized so sensitivity runs can compare zero,
`0.8*v`, `v`, and `1.2*v`; the default Q2 engineering candidate follows
`D_TERMINAL` and uses the known fixed-price forecast. The annual hard terminal
SOC constraint is applied only when the active window reaches the final
annual boundary and the caller explicitly enables the approved policy. It is
never combined with an annual terminal value.

Only the first action `(q_plan[0], c[0], d[0], pv_used[0])` is committed to
the replay. The next decision time rebuilds the information set and resolves
the horizon from the resulting SOC. Q2 has no adjustment delta:

```text
q_adjusted[t] = q_plan[t]
delta_plus[t] = delta_minus[t] = 0
```

### Emergency recourse

Two different quantities have historically both been called `e`. They are
separated throughout the model, the artifacts and the cost fields:

- `e_plan[j]` — the MILP variable above. It is evaluated against the *forecast*
  load and exists so the horizon stays feasible under forecast error. It is a
  planning quantity: it is **never billed** and never reaches the shared
  `IntervalResult` contract. The engine sums `e_plan[0]` over the committed
  steps and reports it as `accounting.e_plan_kwh`.
- `e_realized[t]` — computed during replay against *actual* load and PV:

  ```text
  e_realized[t] = max(
      load_kwh[t] + charge_kwh[t]
      - q_adjusted[t] - pv_available_kwh[t] - discharge_kwh[t],
      0,
  )
  ```

  A committed charge is executed in full: `e_realized` covers the load **and**
  the charge, so a supply shortfall becomes emergency purchase rather than a
  silent cancellation of the planned battery action. The MILP proves its plan
  feasible against the *forecast*; if the realised interval comes in short, the
  plan must still be what was settled, or the annual terminal SOC hard
  constraint -- which the MILP enforces on the plan -- could never be met.

  Settlement counts `e_realized` **exactly once**, at five times the interval
  price. It is the only source of `emergency_cost_cny` and of
  `IntervalResult.emergency_purchase_kwh`, and the engine reports its sum as
  `accounting.e_realized_kwh`.

### Actual replay and cost accounting

The replay uses the actual interval load and PV, the committed first action,
and the fixed Attachment 1 price for the execution interval. It does not
retroactively change the battery action or planned purchase. PV consumption is
bounded by actual PV. PV curtailment and unused grid supply are derived
non-negative replay audit quantities; they are not extra shared result fields
and are never used to manufacture supply. Any residual shortfall is emergency
purchase:

```text
q_emergency[t] = max(
    load_kwh[t] + charge_kwh[t]
    - q_adjusted[t] - pv_used_kwh[t] - discharge_kwh[t],
    0,
)
```

The engineering result records planned cost, adjustment cost (zero for Q2),
and emergency cost separately in the existing result contracts. The replay
validator also reports the derived PV-curtailment and grid-spill audit values.
Planned cost is always
`sum(q_plan[t] * fixed_price[t])`; it is never recomputed from actual load or
actual utilization.

The physical replay ledger is the bus balance. PV enters it through
`pv_used_kwh` only — curtailed PV is not supply and must never appear on the
right-hand side, or PV would be double-counted and the identity would hold only
when nothing is curtailed:

```text
q_adjusted[t] + q_emergency[t] + pv_used_kwh[t] + discharge_kwh[t]
    = load_kwh[t] + charge_kwh[t] + grid_spill_kwh[t]
```

Available PV is partitioned separately, by definition:

```text
pv_available_kwh[t] = pv_used_kwh[t] + pv_curtail_kwh[t]
```

Both identities are checked independently at replay time (`ledger_residual`
and `pv_partition_residual`), and the maximum absolute value of each is
reported in the run's accounting block. Note that `pv_curtail_kwh` is the
*complement* of `pv_used_kwh` under the frozen attribution rule below, so the
partition holds by construction; its check exists to catch a future change that
starts computing either side separately.

### PV attribution rule

Grid purchase and battery discharge are booked to the load first, so
`pv_used_kwh` is whatever residual demand is left for PV to cover:

```text
pv_used_kwh[t] = min(
    pv_available_kwh[t],
    max(load_kwh[t] + charge_kwh[t] - q_adjusted[t] - discharge_kwh[t], 0),
)
```

This is recorded in the artifacts as
`pv_accounting_policy = "grid_and_discharge_first_residual_pv"`. Under this
rule `pv_used_kwh` and `pv_curtail_kwh` are **attribution-rule artifacts**, not
an unconditional measure of actual PV utilization or of true curtailment, and
must not be quoted as such until the PV dispatch semantics themselves are
revisited.

The reduced feasibility check remains `grid + discharge + PV >= load +
charge`. Violations are errors; the implementation never clips SOC, invents
PV, or adds supply merely to make a result pass validation.

### Case and result integration

`cases.py` owns the required-decision list. `q2.py` owns formal-runner
orchestration and delegates model work to focused Q2 modules. The optimizer
returns a typed solution; the replay/validator converts its primary quantities
to the existing shared `IntervalResult` and `CaseResult` contracts. Derived
spill/curtailment values remain in the independent validation report rather
than being hidden in the shared result metadata. Metadata includes model
version, forecast parameters, solver status, source hashes, decision IDs, and
a clear `synthetic`/`engineering` label.

Formal artifact creation must reuse the existing manifest and result-I/O
helpers. The model does not write `data/raw`, `data/templates`,
`CUMCM2026Problems`, or official result templates. Formal execution remains
blocked until the approval gate passes.

## Error handling

- Invalid or incomplete input records raise the existing `InputError` with
  logical name, date/slot, and source reference.
- A missing expected date, missing weekly lag, or future-visible item is an
  input/causality error, not a silent fallback.
- A permitted small-fixture fallback must be explicit in metadata and tests.
- A non-successful SciPy/HiGHS solve raises a Q2-specific solve error with
  status and message; no partial result is serialized as a formal result.
- Independent validation rejects non-finite values, negative purchases,
  simultaneous charge/discharge, SOC violations, PV overuse, balance gaps,
  and incorrect cost recomputation.

## Test strategy

Tests are written before each implementation unit. They cover:

1. forecast causality: changing future actuals does not change a forecast at
   an earlier decision time;
2. weekly-lag and AR(1) boundary behavior, non-negative clipping, metadata,
   and explicit fallback recording;
3. a small MILP with known fixed-price behavior, including zero adjustment,
   planned-quantity billing, mutual exclusion, SOC bounds, and the `>=`
   supply constraint;
4. rolling execution: only the first ten-minute action is committed and SOC
   is continuous between adjacent windows;
5. actual replay: emergency purchase covers shortfall without double-counting
   planned purchase or PV, and all cost components are separate;
6. malformed solutions and missing expected dates are rejected;
7. the formal `q2` dispatcher remains blocked while any Q2 dependency is
   proposed, and synthetic direct tests cannot accidentally produce a formal
   submission artifact;
8. full repository gates, including lock, Ruff, pytest, and release guards.

## Non-goals and unresolved decisions

The following remain deliberately configurable or proposed rather than
silently settled by code:

- final 7/14/21/28-day weights and AR(1) fitting/evaluation protocol;
- whether and how the annual terminal state should be enforced at the exact
  boundary before `D_YEAR_BOUNDARY` is approved;
- any Q3 forecast-vintage, resampling, contract-adjustment, or Q4 price
  forecast behavior;
- official Excel template mapping and final submission generation.

The Q2 implementation is complete only as a gated, reproducible engineering
model until the team approves the relevant decision cards and runs the formal
case through the release workflow.
