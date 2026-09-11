# A 组 shared-core 交接

本文件记录 A 的第一批共享交付（A0–A2）。它不是 Q1 模型完成报告，也不是正式导出完成报告。

## 基线与范围

- 使用分支：`feat/q1-shared-core`
- 核对基线：`cbb3cf4ae50ff6a40be911ba5d5aff0acd2000cc`
- 本地执行后未修改真实 `confirmed_by` / `confirmed_at`，未批准任何 decision
- 未实现 Q1/Q2/Q3/Q4 模型，未选择求解器，未实现模板映射，未写正式 Excel

## A0 本地验证

日志：`outputs/quality/a0_baseline.log`

| 命令 | 退出码 |
|---|---:|
| `git rev-parse HEAD` | 0；`cbb3cf4...` |
| `git merge-base --is-ancestor cbb3cf4... HEAD` | 0 |
| `uv --version` | 0；`uv 0.10.6` |
| `uv lock --check` | 0 |
| `uv sync --locked` | 0 |
| `python -m microgrid doctor` | 0 |
| `ruff check .` | 0 |
| `ruff format --check .` | 0（当时 50 tests 通过） |
| `pytest -q` | 0；50 passed |
| `python -m microgrid smoke` | 0 |

正式附件未参与 CI 或合成测试；真实附件集成和 Q1 求解仍需后续显式执行。

## A1 工程修复

### 统一批准检查

新增 `src/microgrid/approvals.py`，dispatcher、export、release guard 统一使用：

- required decision 必须存在；
- `status == "approved"`；
- `confirmed_by`、`confirmed_at` 必须为非空字符串；
- `pending` / `proposed` / `rejected` / 未知状态都阻断；
- 缺失必需条目也阻断；
- case 只检查自己的 `CASE_DECISIONS`，Q1 不被 Q3 pending 阻断；
- `D_TIME_INTERNAL` 与 `D_TIME_TEMPLATE_EXPORT` 分离。

测试：`tests/test_approvals.py`、`tests/test_release_guards.py`。

### 源码与输入指纹

`src/microgrid/artifacts.py` 已修复：

- `source_tree_hash` 递归覆盖 `src/microgrid/**/*.py`、`scripts/**/*.py`、`configs/*.toml`、`pyproject.toml`、`uv.lock`、`.python-version`；
- 新增 `verify_imported_inputs`，比较当前文件与 `records/inputs_manifest.json`，冲突只报告不覆盖；
- 新增 `ensure_run_id_available`，防止覆盖已有 run；
- `write_manifest` 默认拒绝覆盖已有 manifest；
- `build_manifest(..., verify_inputs=True)` 可记录输入校验问题。

测试：`tests/test_artifacts.py`。

## A2 共享结果交付

### 数值与完整运行验证

`src/microgrid/problem/contracts.py` 增强：

- `BatteryState`、`BatteryAction`、`IntervalResult`、`PurchasePlan`、`CostBreakdown` 拒绝 NaN / ±Inf；
- `TimeInterval` 校验 day + slot 与 start/end 网格一致；
- `IntervalResult` 要求起止电池参数一致，并明确使用 `ENERGY_ABS_TOL_KWH` 容差；
- 不允许事后 clip 越界状态。

新增 `src/microgrid/problem/validation.py`：

- 完整运行检查每日 slot 0–143 完整、唯一、有序；
- 相邻区间 `state_end -> state_start` 连续，包括跨午夜；
- Q1 日终等式可通过 `require_daily_equal_ends=True` 检查；
- 部分片段不强制 144 段，完整运行使用独立验证入口。

### 结果序列化

新增 `src/microgrid/problem/result_io.py`：

- `RESULT_SCHEMA_VERSION = 1`；
- 保存/读取 `CaseResult` / `IntervalResult`；
- JSON 禁止 NaN / Infinity；
- 原子写入、默认不覆盖已有文件；
- 读取后重建现有对象，可执行完整运行验证；
- 不求解、不插值、不读取未来数据。

### B/C 接入说明

新增 `docs/shared_api.md`，包含：

- approval gate 用法；
- `TimeGrid` / `BatteryState` / `BatteryAction` 语义；
- `IntervalResult` / `CaseResult` 字段；
- 完整运行验证；
- `result_io` 序列化；
- `InfoSet.from_raw` 因果隔离；
- 运行目录和 manifest 字段；
- 合成示例。

## 当前状态

- shared-core 接口已可交 B/C 接入；
- 共享物理语义 `D_TIME_INTERNAL`、`D_EFF`、`D_STATE`、`D_INFO` 已于 2026-09-10 由用户明确批准；
- Q1 模型 gate `D_MODEL_Q1` 仍 pending；
- 正式导出 gate `D_TIME_TEMPLATE_EXPORT` 当时 pending；后续已批准，见 A6。
- Q1 runner 仍抛 `ModelNotImplementedError`；
- 等待 H-S（共享语义批准）和 H-M（Q1 模型卡）后再进入 A3/A4；
- 等待 H-X 后才进入 A6 正式导出。

## 本地测试

最终验证日志：`outputs/quality/a_shared_core.log`

| 命令 | 退出码 |
|---|---:|
| `uv lock --check` | 0 |
| `ruff check .` | 0 |
| `ruff format --check .` | 0 |
| `pytest -q` | 0；73 passed |
| `python -m microgrid smoke` | 0 |
| `scripts/build_paper.py --mode draft` | 0 |
| `scripts/build_paper.py --target ai-details --mode draft` | 0 |
| `run --case q1`（预期阻断） | 4 |
| `prepare_submission.py --mode final`（预期阻断） | 5 |

## B/C 接入方式

B/C 从 `feat/q1-shared-core` 合并后的 main 同步，使用：

```bash
uv sync --locked
uv run --locked pytest -q
```

接口阅读顺序：

1. `docs/shared_api.md`
2. `src/microgrid/approvals.py`
3. `src/microgrid/problem/contracts.py`
4. `src/microgrid/problem/validation.py`
5. `src/microgrid/problem/result_io.py`

B/C 不应：

- 各自复制 approval/gate 逻辑；
- 修改共享 contract 绕过接口；
- 把未来 actual 直接传入计划器；
- 把策略数据塞进 `metadata`；
- 在 D_TIME_TEMPLATE_EXPORT 未批准时写正式 result*.xlsx。

## A4–A5：Q1 内部结果

`D_MODEL_Q1` 已由用户于 2026-09-10 批准，求解器采用 `scipy.optimize.milp` / HiGHS。

正式附件 1 的内部运行（本地，非正式导出）：

- branch: `feat/q1-shared-core`
- code commit: `60eab7183e6587d3c8406eacbf1be6afd18d79d5`
- run_id: `q1-a-formal-002`
- code_dirty: `false`
- input manifest verification: `[]`
- reference_day: `2025-01-01`（内部坐标，不是附件1观测日期）
- attachment1 SHA-256: `66b87134f5ecccd6...`
- solver status: `0` / HiGHS Optimal
- solver mip_gap: `0.0`
- interval count: `144`
- validation: `ok=true`，最大供需残差约 `1.14e-13 kWh`，最大电池动态残差约 `9.09e-13 kWh`
- total load: `111024.8081 kWh`
- total PV forecast: `55482.8357 kWh`
- total planned purchase: `59482.6990 kWh`
- total planned cost: `35126.9486 CNY`
- initial/terminal energy: `6000 / 6000 kWh`

已生成内部运行文件：

```text
outputs/runs/q1/q1-a-formal-002/
├── manifest.json
├── input_snapshot.json
├── domain_result.json
├── validation.json
├── summary.json
└── solver.log
```

尚未生成：

```text
results/result1.xlsx
```

原因（当时）：`D_TIME_TEMPLATE_EXPORT` 仍为 pending。后续 A6 已采用显式行序映射并完成 result1.xlsx 导出，见下文。

## P1 修复后的 Q1 复运行

针对独立校验、合成标识和输入来源检查的 P1 修复后，重新生成新 run：

- commit: `357b99440d5bb121c5bdc6e4a313e6755d380d28`
- run_id: `q1-a-formal-003`
- manifest:
  - `status=success`
  - `model_status=implemented`
  - `is_synthetic=false`
  - `code_dirty=false`
  - `input_verification_issues=[]`
- validation:
  - `ok=true`
  - `violations=[]`
  - 最大供需残差约 `1.14e-13 kWh`
  - 最大电池动态残差约 `9.09e-13 kWh`
- 逐段 `pv_used_kwh` 已写入 `domain_result.json`；例如 slot 72 的 `pv_used_kwh=1268.7193333333335`。
- 合成 mock 测试现在必须显式传入 `CaseContext(is_synthetic=True, output_dir=...)`；manifest、`CaseResult` 和 summary 均保留 `is_synthetic=true`。
- 失败运行现在会保留 `manifest.json`（`status=failed`）、`failure.json`（阶段、错误类型、错误消息）。
- 附件1 来源检查现在要求清单中恰好一条有效记录、非空 SHA-256，并与实际读取文件绑定。


## A6–A7：正式交付

`D_TIME_TEMPLATE_EXPORT` 已批准 Q1 行序映射：

- 内部 slot 0..143 按行写入 `计划购电量!B2:B145`；
- 保留官方模板原标签，不修改表头；
- `充放电量!B2:C7` 按六个四小时块汇总；
- `E2/E3` 写 0:00/24:00 储电量；
- 该映射是团队为正式交付采用的显式约定，不声称官方标签已被勘误。

正式导出：

- run_id：`q1-a-formal-003`
- result1.xlsx：`outputs/runs/q1/q1-a-formal-003/results/result1.xlsx`
- export_manifest：`outputs/runs/q1/q1-a-formal-003/export_manifest.json`
- output SHA-256：`2ea2a2efb90ff4a8facad26dfbbfa8205c7bf73eede8aba1fb476d8e8ccb21cb`
- manifest 已登记 `result_files` 和 `result_sha256`。

论文表格：

- `paper/generated/q1_result_tables.tex` 由同一 `domain_result.json` 生成；
- 表 1、表 2 与 Excel 结果同源；
- 支持宏：`\QOneTotalPurchaseKwh`、`\QOneTotalCostCny`、`\QOneRunId`。

选择记录：

- `configs/selected_runs.toml` 已记录 `q1 = "q1-a-formal-003"`；
- `selection_status` 仍为 pending，因为 Q2–Q4 尚未完成；这不是 Q1 失败。

仍待完成：

- Q2–Q4 模型与正式结果；
- Q1 之外 case 的模板映射；
- 最终全队 submissions selected run 和支撑材料预检。
