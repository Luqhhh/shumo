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
- 正式导出 gate `D_TIME_TEMPLATE_EXPORT` 仍 pending；
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
