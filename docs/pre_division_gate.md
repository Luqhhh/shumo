# Pre-division Gate

基线：`main@bc73d66` + 本分支 `infra/pre-division-contracts`。

本 gate 只验收工程控制面与共享 contract，不验收模型质量，也不批准任何 pending decision。

## Gate 结果

| 检查 | 结果 |
|---|---|
| `uv lock --check` | pass |
| `ruff check .` | pass |
| `ruff format --check .` | pass |
| `pytest -q` | 43 passed |
| `python -m microgrid smoke` | pass；`is_synthetic=true` |
| `paper/main.pdf` draft | pass |
| `AI 工具使用详情.pdf` draft | pass |
| `run --case q4_2` | 预期退出 4；pending guard 阻断 |
| `prepare_submission.py --mode final` | 预期退出 5；selected run / paper / AI details / pending decisions 阻断 |
| `git diff --check` | pass |

详细日志：`outputs/quality/pre_division_gate.log`（本地忽略，不提交）。

## 已完成

1. **D-MODEL 拆分**：五个 case gate 分别为 `D_MODEL_Q1`、`D_MODEL_Q2`、`D_MODEL_Q3`、`D_MODEL_Q4_2`、`D_MODEL_Q4_3`。
2. **移除 q4_2 → D_RESAMPLE**：Q4-2 保持“波动电价下重算 Q2”的信息边界；Q4-3 继续依赖 D_RESAMPLE。
3. **共享 contract**：`src/microgrid/problem/contracts.py` 已包含
   - `TimeGrid` / `TimeInterval`：144 区间、145 边界、右端点对齐；
   - `BatteryState` / `BatteryAction` / `apply_battery_action`：母线侧电量、电池内部储能、0.9/0.9 效率、5000 kW 母线侧约束、非同时充放电；
   - `InfoSet` / `InfoItem`：`available_at <= decision_time` 因果可见性；
   - `PurchasePlan` / `CostBreakdown`：计划、调整、紧急和费用分项；
   - `CaseContext` / `CaseResult` / `CaseRunner`：runner 与导出层的统一边界。
4. **Contract tests**：`tests/test_shared_contracts.py` 覆盖时间 slot、kW→kWh、充放电状态变化、往返效率、边界越界、跨午夜连续性和预报可见性。
5. **Ownership 与流程**：已写入 `docs/collaboration.md`。
6. **模型/导出边界**：`problem/q*.py` 不直接写 Excel；官方模板由 `excel_export.py` 负责。
7. **Formal run 最低契约**：见 `docs/collaboration.md` 与 `docs/runbook.md`。

## 尚待人工批准

以下 decision 仍为 pending，未修改任何 `status/choice/rationale/confirmed_by/confirmed_at`：

- `D_TIME`
- `D_EFF`
- `D_STATE`
- `D_INFO`
- `D_RESAMPLE`
- `D_SETTLE`
- `D_MODEL_Q1`
- `D_MODEL_Q2`
- `D_MODEL_Q3`
- `D_MODEL_Q4_2`
- `D_MODEL_Q4_3`
- `D_EVAL`

注意：`problem/contracts.py` 已按团队提供的 pre-division 语义编码，但 decision 状态仍为 pending；正式模型与正式导出仍需人工批准。

## Gate 通过后的下一步

从合并后的 main commit 创建三条长期分支：

- A：`feat/q1-shared-core`
- B：`feat/q2-actual-replay`
- C：`feat/q3-forecast-control`

该步骤在本 gate 合并到 main 后执行，本分支不提前创建。
