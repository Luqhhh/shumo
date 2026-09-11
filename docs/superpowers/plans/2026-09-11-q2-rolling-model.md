# Q2 Rolling Optimization Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a strictly causal, gated Q2 24-hour rolling MILP with actual replay, reproducible evidence, and tests while preserving the team approval boundary.

**Architecture:** Keep `q2_inputs.py` as the explicit source adapter, add focused forecast, optimization, replay, and orchestration modules, and keep `q2.py` responsible for formal-run artifacts and approval-gated execution. The optimizer works on typed engineering inputs; the replay converts committed ten-minute actions into the existing `IntervalResult`/`CaseResult` contracts. Proposed decisions remain blocking gates.

**Tech Stack:** Python 3.11, dataclasses, NumPy, SciPy `optimize.milp`/HiGHS, pytest, Ruff, TOML decision records, and the existing manifest/result-I/O helpers.

**Spec:** `docs/superpowers/specs/2026-09-11-q2-rolling-model-design.md`

## Global Constraints

- Use the shared natural-day grid: 144 ten-minute intervals and right-endpoint input alignment.
- Convert source power to interval energy exactly once with `power_kw * (1/6)`.
- Use `grid + discharge + pv >= load + charge`; never force a false equality or invent supply.
- Q2 adjustment increment is zero, while adjusted purchase is the planned total.
- Planned cost uses planned purchase quantity; emergency cost is a separate actual-replay component.
- January is a standby initialization period from 6000 kWh; February onward is continuous.
- Do not change any decision status, `confirmed_by`, or `confirmed_at` field.
- Formal Q2 execution must remain blocked unless `D_MODEL_Q2`, `D_MPC`, `D_TERMINAL`, `D_YEAR_BOUNDARY`, and `D_SETTLE` are fully approved.
- Do not read or modify `data/raw/`, `data/templates/`, `resources/`, or `CUMCM2026Problems/` as part of implementation.
- Synthetic fixtures live under pytest `tmp_path`; no synthetic data goes to `dist/`.
- Every task ends with its focused tests and a local commit; pushing requires separate user authorization.

---

### Task 1: Close Q2 input completeness and approval gates

**Files:**
- Modify: `src/microgrid/problem/q2_inputs.py`
- Modify: `src/microgrid/problem/cases.py`
- Modify: `src/microgrid/problem/q2.py`
- Test: `tests/test_q2_inputs.py`
- Test: `tests/test_approvals.py`
- Test: `tests/test_release_guards.py`

**Interfaces:**
- Extend `load_q2_inputs(..., expected_days: tuple[date, ...] | None = None) -> Q2InputBundle`.
- Add `require_expected_days(bundle: Q2InputBundle, expected_days: tuple[date, ...]) -> None` or an equivalent private validator used by the loader.
- Set Q2 dependencies to `D_TIME_INTERNAL`, `D_EFF`, `D_STATE`, `D_INFO`, `D_MODEL_Q2`, `D_MPC`, `D_TERMINAL`, `D_YEAR_BOUNDARY`, and `D_SETTLE`.
- Direct calls to `q2.run` must call `require_approved_decisions` with the same tuple that the dispatcher uses.

- [ ] **Step 1: Write failing completeness tests**

Add tests that create two complete synthetic dates with the existing workbook helpers and assert that:

```python
load_q2_inputs(
    attachment1_path=price_path,
    attachment2_path=actual_path,
    load_sheet_name="负载",
    pv_sheet_name="光伏",
    expected_days=(date(2025, 1, 1), date(2025, 1, 3)),
)
```

raises `InputError` when 2025-01-02 is absent. Add separate cases for a missing first day, missing last day, and a single table missing one day. The error must include the expected range and missing date; it must not infer completeness from observed dates.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --locked pytest -q tests/test_q2_inputs.py -k "expected_days or missing_day"
```

Expected result: FAIL because the loader has no explicit expected-day validation.

- [ ] **Step 3: Implement explicit expected-day validation**

Keep `_require_complete_days` for per-day 144-slot completeness. Add a second validation after load/pairing that compares the observed date set with the caller-provided set. Preserve the existing `expected_days=None` behavior for small adapter-only callers, but require a non-None range in the formal runner.

Update `CASE_DECISIONS["q2"]` and the direct runner gate without adding Q3-only `D_RESAMPLE` or Q4-only price decisions.

- [ ] **Step 4: Add gate regression tests**

Create a temporary decisions TOML with the four shared decisions approved and one new Q2 dependency proposed. Assert `run_case("q2")` raises `PendingDecisionError` and names the proposed ID. Add a fixture with every listed dependency approved and assert the guard reaches the runner's explicit input error rather than a model-not-implemented error. Do not edit the repository decision file.

- [ ] **Step 5: Run the focused tests**

Run:

```bash
uv run --locked pytest -q tests/test_q2_inputs.py tests/test_approvals.py tests/test_release_guards.py
```

Expected result: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/microgrid/problem/q2_inputs.py src/microgrid/problem/cases.py src/microgrid/problem/q2.py tests/test_q2_inputs.py tests/test_approvals.py tests/test_release_guards.py
git commit -m "fix: close Q2 completeness and approval gates"
```

---

### Task 2: Build the causal Q2 weekly-plus-AR(1) forecast

**Files:**
- Create: `src/microgrid/problem/q2_forecast.py`
- Test: `tests/test_q2_forecast.py`
- Modify: `src/microgrid/problem/q2_inputs.py` only if a typed history helper is needed

**Interfaces:**
- Create immutable `ForecastConfig` with:
  `weights: tuple[float, float, float, float]`,
  `ar1_phi: float | None`, `model_version: str`, and `allow_short_history: bool`.
- Create immutable `ForecastPoint` with `valid_time`, `available_at`, `load_kw`, `pv_kw`, `training_cutoff`, `model_version`, `data_version`, and `fallback_reason`.
- Implement `build_q2_forecast(actuals: tuple[ActualInterval, ...], decision_time: datetime, horizon_start: datetime, config: ForecastConfig, horizon_steps: int = 144) -> tuple[ForecastPoint, ...]`.
- Implement `estimate_ar1_phi(residuals: Sequence[float]) -> float` with a bounded result in `[-0.99, 0.99]`.

- [ ] **Step 1: Write failing causality and lag tests**

Use deterministic `ActualInterval` fixtures covering at least 35 days. Assert:

```python
forecast = build_q2_forecast(
    actuals,
    decision_time=dt.datetime(2025, 2, 1),
    horizon_start=dt.datetime(2025, 2, 1),
    config=ForecastConfig(weights=(0.25, 0.25, 0.25, 0.25), ar1_phi=0.0),
)
assert len(forecast) == 144
assert forecast[0].load_kw == pytest.approx(
    mean(loads_on(2025-01-25, 2025-01-18, 2025-01-11, 2025-01-04))
)
```

Change actuals after the decision time by a large amount and assert every forecast value, cutoff, and fallback is unchanged. Add tests for negative weights, all-zero weights, non-finite values, and a forecast that would become negative after AR(1) adjustment; the latter must clip to zero after adjustment.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --locked pytest -q tests/test_q2_forecast.py
```

Expected result: FAIL with a missing `microgrid.problem.q2_forecast` module or missing forecast symbol.

- [ ] **Step 3: Implement the typed causal forecast**

Sort actuals by `(day, slot)`. Treat an interval as visible only when `actual.end <= decision_time`. Normalize four non-negative weights and reject an all-zero vector.

For every target interval, use the same natural-day slot at target date minus 7, 14, 21, and 28 days. Build residuals only from visible actuals for which all required lag values are visible. If `ar1_phi` is `None`, estimate it from visible residual pairs and clip it to `[-0.99, 0.99]`; otherwise validate the supplied value. Apply the latest visible residual multiplied by `phi ** age_steps` to the weighted baseline, then clip each final load/PV forecast at zero.

Set `available_at=decision_time`, `training_cutoff` to the latest visible actual end, and `fallback_reason=""` when all four lags are present. In short-history mode, use only available lags, record the exact missing lags, and never make short history the formal default.

- [ ] **Step 4: Run causality and metadata tests**

Run:

```bash
uv run --locked pytest -q tests/test_q2_forecast.py
```

Expected result: PASS, including the future-perturbation test.

- [ ] **Step 5: Commit**

```bash
git add src/microgrid/problem/q2_forecast.py tests/test_q2_forecast.py
git commit -m "feat: add causal Q2 weekly AR1 forecast"
```

---

### Task 3: Add the Q2 rolling MILP builder and solver

**Files:**
- Create: `src/microgrid/problem/q2_model.py`
- Test: `tests/test_q2_model.py`

**Interfaces:**
- Create immutable `Q2WindowInput` with ordered `valid_times`, `price_cny_per_kwh`, `load_forecast_kwh`, `pv_forecast_kwh`, `initial_soc_kwh`, `terminal_value_cny_per_kwh`, optional `annual_terminal_soc_kwh`, and `is_annual_endpoint: bool = False`.
- Create immutable `Q2ModelConfig` with `horizon_steps=144`, `time_limit_s`, and `terminal_value_multiplier=1.0`.
- Create immutable `Q2Plan` with tuples `planned_purchase_kwh`, `charge_kwh`, `discharge_kwh`, `pv_used_kwh`, `soc_kwh`, `objective_cny`, `solver_status`, `solver_message`, and `solver_metadata`.
- Implement `solve_q2_window(window: Q2WindowInput, config: Q2ModelConfig) -> Q2Plan`.

- [ ] **Step 1: Write failing small-MILP tests**

Build a two-step or four-step window using a test-only horizon override. Assert:

- high PV can reduce planned purchase but `pv_used_kwh <= pv_forecast_kwh`;
- planned purchase is non-negative and fixed-price objective equals the sum of planned quantity times price, before any terminal term;
- `q + d + pv >= load + c` holds for every step;
- SOC follows `E_next = E + 0.9*c - d/0.9`;
- SOC stays in [1200, 10800] and starts at the supplied state;
- charge and discharge are never both positive;
- a supplied annual terminal state is enforced only when the window reaches the annual endpoint and `is_annual_endpoint` is true;
- a non-successful solver result raises `Q2SolveError`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --locked pytest -q tests/test_q2_model.py
```

Expected result: FAIL because `q2_model.py` and `solve_q2_window` do not exist.

- [ ] **Step 3: Implement deterministic variable indexing**

Use contiguous slices for `q_plan`, `c`, `d`, `pv_used`, `soc`, and binary `z`. The horizon has `H` action intervals and `H+1` SOC boundaries. Bound grid purchase by `load_forecast_kwh + MAX_BUS_ENERGY_KWH` to keep the LP finite; bound PV use by the forecast.

Add one inequality row per interval:

```text
q_plan[j] + d[j] + pv_used[j] - c[j] >= load_forecast[j]
```

Add one equality row for battery dynamics and two inequalities for charge/discharge mutual exclusion. Fix `soc[0]` to the input state and fix `soc[H]` only when `annual_terminal_soc_kwh` is supplied and `is_annual_endpoint` is true.

- [ ] **Step 4: Implement the objective and solve**

Minimize:

```text
sum(price[j] * q_plan[j]) - terminal_value_cny_per_kwh * soc[H]
```

The terminal coefficient is `D_TERMINAL`'s known fixed-price candidate supplied by the caller; the model does not choose or approve it. Use SciPy `milp`, `Bounds`, and `LinearConstraint`, return solver status/message/HiGHS metadata, and raise `Q2SolveError` when status is not zero or `result.x` is absent.

- [ ] **Step 5: Run the solver tests**

Run:

```bash
uv run --locked pytest -q tests/test_q2_model.py
```

Expected result: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/microgrid/problem/q2_model.py tests/test_q2_model.py
git commit -m "feat: add Q2 rolling MILP solver"
```

---

### Task 4: Add independent Q2 plan validation

**Files:**
- Modify: `src/microgrid/problem/q2_model.py`
- Test: `tests/test_q2_model.py`

**Interfaces:**
- Create immutable `Q2ValidationReport` with `ok`, `issues`, `max_balance_residual_kwh`, `max_dynamics_residual_kwh`, `cost_gap_cny`, and `violations`.
- Implement `validate_q2_plan(window: Q2WindowInput, plan: Q2Plan) -> Q2ValidationReport`.

- [ ] **Step 1: Write failing tamper tests**

Solve a feasible synthetic window, then construct tampered copies with:

- one supply inequality violation;
- a SOC dynamics gap;
- PV use above the forecast;
- simultaneous charge/discharge;
- a planned-cost/objective mismatch;
- non-finite or negative values.

Assert the report is false and identifies the affected slot and violation type.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run --locked pytest -q tests/test_q2_model.py -k "validation or tamper"
```

Expected result: FAIL because the independent validator does not exist.

- [ ] **Step 3: Implement checks independently from solver construction**

Recompute all residuals from the typed arrays rather than trusting solver metadata. Check lengths, finiteness, non-negativity, PV upper bounds, charge/discharge exclusivity, SOC bounds, initial/annual terminal states, supply inequality, battery dynamics, and objective recomputation including the terminal term.

Use `ENERGY_ABS_TOL_KWH` for energy residuals and a named cost tolerance. Do not clip or repair a plan.

- [ ] **Step 4: Run focused validation tests**

Run:

```bash
uv run --locked pytest -q tests/test_q2_model.py
```

Expected result: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/microgrid/problem/q2_model.py tests/test_q2_model.py
git commit -m "test: independently validate Q2 plans"
```

---

### Task 5: Implement actual replay and Q2 settlement accounting

**Files:**
- Create: `src/microgrid/problem/q2_replay.py`
- Test: `tests/test_q2_replay.py`

**Interfaces:**
- Create immutable `Q2ReplayRow` containing the actual interval, `PurchasePlan`, `BatteryAction`, `state_start`, `state_end`, `pv_used_kwh`, `grid_spill_kwh`, `pv_curtail_kwh`, and `CostBreakdown`.
- Implement `replay_q2_actions(actuals: tuple[ActualInterval, ...], prices: Mapping[tuple[date, int], float], planned: Mapping[tuple[date, int], tuple[float, float, float]], initial_state: BatteryState) -> tuple[Q2ReplayRow, ...]`.
- Implement `rows_to_interval_results(rows: tuple[Q2ReplayRow, ...]) -> tuple[IntervalResult, ...]`.
- Implement `summarize_q2_cost(rows: tuple[Q2ReplayRow, ...]) -> CostBreakdown`.

- [ ] **Step 1: Write failing replay and settlement tests**

Assert the following exact cases:

```python
# Planned 100 kWh covers 100 kWh load; no actual PV or battery action.
# Emergency purchase is zero, adjusted total remains 100, adjustment cost is zero.
assert row.purchase_plan.planned_kwh == pytest.approx(100.0)
assert row.purchase_plan.adjusted_kwh == pytest.approx(100.0)
assert row.purchase_plan.emergency_kwh == pytest.approx(0.0)
assert row.costs.planned_cost_cny == pytest.approx(100.0 * price)
```

Also test an actual shortfall, actual PV partially covering demand, planned purchase spill, SOC continuity, negative/invalid action rejection, and a case where the old incorrect PV-plus-curtailment equality would fail. Assert the derived audit ledger satisfies:

```text
adjusted + emergency + pv_used + discharge
= load + charge + grid_spill + pv_curtail
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run --locked pytest -q tests/test_q2_replay.py
```

Expected result: FAIL with a missing replay module or symbol.

- [ ] **Step 3: Implement Q2 dispatch and costs**

For each interval, take the committed planned purchase, charge, and discharge action. Set adjusted purchase equal to planned purchase and all Q2 deltas to zero. Compute actual PV used deterministically as the smaller of actual PV and the positive residual demand after planned grid and discharge:

```text
pv_used = min(actual_pv, max(load + charge - planned - discharge, 0))
```

Then compute:

```text
emergency = max(load + charge - adjusted - pv_used - discharge, 0)
planned_cost = planned * price
adjustment_cost = 0
emergency_cost = emergency * 5 * price
```

Derive non-negative grid spill and PV curtailment for audit. Apply `apply_battery_action` without clipping. Raise `Q2ReplayError` on any state or ledger violation.

- [ ] **Step 4: Map primary quantities to shared results**

Use actual `load_kw`/`pv_kw`, `planned_purchase_kwh`, `adjusted_purchase_kwh`, `emergency_purchase_kwh`, `BatteryAction`, continuous states, source references, and `pv_used_kwh`. Keep derived spill/curtailment in the validation/audit report rather than adding unapproved shared fields.

- [ ] **Step 5: Run replay tests**

Run:

```bash
uv run --locked pytest -q tests/test_q2_replay.py
```

Expected result: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/microgrid/problem/q2_replay.py tests/test_q2_replay.py
git commit -m "feat: add Q2 actual replay and settlement accounting"
```

---

### Task 6: Add the continuous rolling Q2 engineering engine

**Files:**
- Create: `src/microgrid/problem/q2_engine.py`
- Test: `tests/test_q2_engine.py`

**Interfaces:**
- Create immutable `Q2EngineConfig` containing `action_start_day`, `action_end_day`, forecast/model configs, `annual_terminal_soc_kwh`, and `is_synthetic`. The engine sets `is_annual_endpoint=True` only for the final active window.
- Create immutable `Q2EngineeringResult` containing ordered `intervals`, `costs`, `forecast_records`, `solver_records`, `metadata`, and `is_synthetic`.
- Implement `run_q2_engineering(bundle: Q2InputBundle, config: Q2EngineConfig) -> Q2EngineeringResult`.

- [ ] **Step 1: Write failing rolling tests**

Create a 35-day synthetic actual bundle and assert:

- January intervals are used as history but no January action is emitted;
- the first action interval is 2025-02-01 00:00;
- each decision uses only actuals whose interval end is at or before its decision time;
- exactly one action from each solved horizon is committed;
- adjacent result rows have continuous SOC;
- a 144-step horizon is rebuilt at every ten-minute action;
- metadata records training cutoff, forecast version, solver status, and source hashes;
- the annual terminal state is applied only on the final active window when enabled.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run --locked pytest -q tests/test_q2_engine.py
```

Expected result: FAIL with a missing engine module or symbol.

- [ ] **Step 3: Implement the January warm-up and action range**

Index actuals by `(day, slot)`. Start battery state at `BatteryState(6000.0)`. Skip emitted actions before `action_start_day`, preserving the state and recording the warm-up range. For each active interval, set `decision_time=interval.start`, build the causal forecast, map the next 144 valid times to forecasts and repeated fixed daily prices, and solve from the current SOC.

- [ ] **Step 4: Commit only the first rolling action**

Take index zero of the plan for planned purchase, charge, discharge, and forecast PV use. Pass the committed action to the replay for the actual interval, update SOC from the replay row, and discard the rest of the plan except solver evidence. Never let the next solve mutate an already emitted row.

- [ ] **Step 5: Assemble and validate the engineering result**

Convert replay rows to ordered `IntervalResult` values. Run `validate_complete_run` over the active date range, verify all state transitions, and aggregate planned/emergency costs. Include a `warmup` metadata block and a `decision_trace` count; do not write artifacts from this pure engine function.

- [ ] **Step 6: Run rolling tests**

Run:

```bash
uv run --locked pytest -q tests/test_q2_engine.py
```

Expected result: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/microgrid/problem/q2_engine.py tests/test_q2_engine.py
git commit -m "feat: add continuous Q2 rolling engine"
```

---

### Task 7: Replace the Q2 placeholder with a gated formal runner

**Files:**
- Modify: `src/microgrid/problem/q2.py`
- Test: `tests/test_q2_runner.py`
- Modify: `src/microgrid/problem/result_io.py` only if a serialization defect is exposed

**Interfaces:**
- Preserve `run(context: CaseContext) -> CaseResult`.
- Use context metadata keys `attachment1_path`, `attachment2_path`, `load_sheet_name`, `pv_sheet_name`, `action_start_day`, and `action_end_day` for explicit test/source paths.
- Default formal paths are `data/raw/附件1.xlsx` and `data/raw/附件2.xlsx`; missing paths fail with `InputError`, not synthetic fallback.

- [ ] **Step 1: Write failing runner integration tests**

Create a temporary repository with synthetic attachments, an inputs manifest, and all Q2 decision cards approved in the fixture only. Assert that a synthetic `CaseContext` run creates `manifest.json`, `input_snapshot.json`, `domain_result.json`, `validation.json`, `summary.json`, and `solver.log`, with `is_synthetic=true`, no official Excel file, and a result-I/O round trip.

Add a second test with one decision proposed and assert `PendingDecisionError` before any run directory is created. Add a missing-input test and assert a failed manifest records the input stage.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run --locked pytest -q tests/test_q2_runner.py
```

Expected result: FAIL because `q2.run` still raises `ModelNotImplementedError`.

- [ ] **Step 3: Implement the runner lifecycle**

Call `require_approved_decisions` first. Allocate a unique run ID and write a running manifest. Verify explicit input paths and manifest hashes, load `Q2InputBundle` with an explicit expected day range, run the pure engine, independently validate the rows and complete run, then write JSON evidence through `write_json`, `save_case_result`, and `write_manifest`.

On any exception after allocation, write `failure.json` and a failed manifest with the stage and error type. Never serialize a partial success.

- [ ] **Step 4: Preserve the formal boundary**

Keep official workbook export out of `q2.run`. Mark synthetic runs clearly. Ensure a formal call with the repository's current proposed decisions stops at the gate and does not create a formal result.

- [ ] **Step 5: Run runner and existing guard tests**

Run:

```bash
uv run --locked pytest -q tests/test_q2_runner.py tests/test_release_guards.py tests/test_result_io.py
```

Expected result: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/microgrid/problem/q2.py tests/test_q2_runner.py
git commit -m "feat: implement gated Q2 formal runner"
```

---

### Task 8: Add reproducible actual-data evidence and complete verification

**Files:**
- Create: `scripts/evaluate_q2_forecast.py`
- Modify: `docs/b_model_card.md`
- Modify: `docs/b_decision_evidence.md`
- Test: `tests/test_q2_evidence.py`

**Interfaces:**
- CLI accepts explicit `--attachment2`, `--load-sheet`, `--pv-sheet`, `--start-date`, `--end-date`, and `--output` paths.
- The evaluator writes a JSON evidence file only to the caller-provided output path and includes source hashes, sample range, training cutoff, model version, weights, AR(1) coefficient, MAE/RMSE definitions, and causal visibility counts.
- It never writes to `data/raw`, `data/templates`, `resources`, or `dist`.

- [ ] **Step 1: Write failing evidence tests**

Use a temporary attachment2 workbook and assert the evaluator:

- accepts explicit paths only;
- changes no source file;
- reports the four lag counts and no future-visible records;
- records model parameters and source hash;
- produces deterministic metrics for deterministic input;
- rejects an output path inside a protected raw/template directory.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run --locked pytest -q tests/test_q2_evidence.py
```

Expected result: FAIL because the evaluator does not exist.

- [ ] **Step 3: Implement the evaluator**

Reuse `load_q2_inputs`, the causal forecast builder, and the same forecast/error definitions as the model. Compare each forecast only with actuals that become visible after the forecast decision time, report MAE and RMSE separately, and record fallback counts. Keep final weights and AR(1) selection labelled as candidate engineering parameters, not approved decisions.

- [ ] **Step 4: Update the model card and evidence document**

Document that the Q2 engine is implemented but formally gated. Separate approved shared semantics, proposed Q2/MPC/terminal/year/settlement policies, and data-supported observations. Remove stale statements that the adapter or decision configuration has no changes. Do not write any new approval record in these documents.

- [ ] **Step 5: Run evidence and full gates**

Run:

```bash
uv run --locked pytest -q tests/test_q2_evidence.py
uv lock --check
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
uv run --locked pytest -q tests/test_release_guards.py tests/test_approvals.py
git diff --check origin/main...HEAD
```

Expected result: every command exits 0; the full suite may retain only the existing opt-in solver skip. Confirm `git status --short` lists only intentionally untracked local planning/raw files and that no protected decision status changed.

- [ ] **Step 6: Commit**

```bash
git add scripts/evaluate_q2_forecast.py docs/b_model_card.md docs/b_decision_evidence.md tests/test_q2_evidence.py
git commit -m "docs: add reproducible Q2 evidence and model card"
```

- [ ] **Step 7: Final audit**

Inspect:

```bash
git diff --stat origin/main...HEAD
git diff origin/main...HEAD -- configs/decisions.toml
git status --short
```

Expected result: the decision diff contains no status or human-confirmation changes, no raw attachment is tracked, and all Q2 formal runs remain blocked until the team approves the required cards. Report local HEAD and upstream separately; ask before any push.
