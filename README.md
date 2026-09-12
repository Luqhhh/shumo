# CUMCM 2026 C — 微网与外部电网电力调控策略

本仓库当前处于 **Stage 1 过渡期**。Stage 0 已完成：工程脚手架、数据层、时间标签解析、结果模板适配接口、运行记录、LaTeX 论文层、AI 使用记录、工程测试和提交预检。Stage 1 的工作是在人工批准 shared semantics 后实现正式模型。

## 阶段状态

### Stage 0 — completed（保留历史）

- 建立了数据导入、无损读取、时间标签解析、模板契约、smoke、论文草稿和 release guards；
- 未实现 Q1/Q2/Q3/Q4 的预测、优化、调度或费用模型；
- 未选择模型、求解器、储能方程、结算公式、预测方法或对照实验；
- 未产生正式结果。

### Stage 1 — active（pre-division）

- `microgrid run --case ...` 经过 dispatcher：
  - required decisions 仍有 pending：`PendingDecisionError`；
  - 全部 approved：调用 `src/microgrid/problem/q*.py`；
  - runner 尚未实现：`ModelNotImplementedError`。
- 模型 gate 按 case 拆分：`D_MODEL_Q1`、`D_MODEL_Q2`、`D_MODEL_Q3`、`D_MODEL_Q4_2`、`D_MODEL_Q4_3`。
- 共享物理语义由 `D_TIME_INTERNAL`、`D_EFF`、`D_STATE`、`D_INFO` 承担。
- 内部时间与正式导出分开审批：原 `D_TIME_TEMPLATE_EXPORT` 保留Q1映射，Q4使用专用 `D_TIME_TEMPLATE_EXPORT_Q4`。
- `proposed` 表示已有候选解释但未批准，仍然阻断。
- Q4-2 是波动电价下重算 Q2，**不依赖 `D_RESAMPLE`**；Q4-3 保持 Q3 预报信息边界。
- `src/microgrid/problem/contracts.py` 已建立 TimeGrid / BatteryState / BatteryAction / InfoSet / PurchasePlan / CostBreakdown 等领域 contract；共享四项 D_TIME_INTERNAL / D_EFF / D_STATE / D_INFO 已于 2026-09-10 经用户批准；D_MODEL_Q1、D_TIME_TEMPLATE_EXPORT 也已批准。D_RESAMPLE 已于 2026-09-11 经三位队员批准并实现独立重采样 contract；Q2–Q4 模型、D_SETTLE、D_EVAL 仍受各自 decision 状态约束。
- 模型 runner 只产生统一 `CaseResult` / `IntervalResult`，不直接写 Excel；官方模板映射由 `excel_export.py` 负责，并受各case导出decision独立门槛控制。
- `InfoSet.from_raw` 只保留 `available_at <= decision_time` 的可见项，计划器不能通过原始容器读取未来 actual。
- A 组 shared-core 交付说明见 `docs/shared_api.md` 与 `docs/a_handoff.md`；B/C 接入接口，但在共享 decisions 未批准前不得实现正式模型。
- Stage 1 共享建模架构的冻结版计划见 `docs/modeling_plan.md`；其中的候选口径不代替 `configs/decisions.toml` 的人工批准状态。
- 2026-09-12 A明确批准 Q3 的 `D_LOAD_FORECAST`（LOAD-A + LF-A）、`D_SETTLE` 和 `D_MODEL_Q3`，完整口径见 `docs/q3_model.md`。Q3及final gate已接入负载批准；Q3求解器尚未实现，运行仍抛出 `ModelNotImplementedError`。
- 2026-09-12本会话用户批准Q4 v2：`D_MODEL_Q4_2`、`D_MODEL_Q4_3` 完整包含各自预测/反馈/窗口/年度范围；新增并批准 `D_TIME_TEMPLATE_EXPORT_Q4`、`D_EVAL_Q4`。gate检查状态、确认字段和精确case范围，Q4导出快照独立于Q1。见 [批准记录](docs/approvals/2026-09-12-q4.md)。Q4求解器已实现；用户进一步批准 `D_TERMINAL_RESERVE_Q4`，最后24小时E≥6000并保留硬实际末态，形成[v3补充](docs/q4_v3_terminal_reserve.md)。241项工程测试通过，四条全年轨迹从初态重跑各48096步通过，实际末态6000及两主方案Excel读回通过；见 [实施状态](docs/q4_implementation_status.md)。Q2模型和全局D_EVAL仍待批准。
- release guard 不再使用“Stage 0 永久阻断”常量，而是检查显式选择的正式 run artifacts。

## 常用命令

```bash
# 初始化
uv lock
uv sync --locked

# 环境与输入
uv run --locked python -m microgrid doctor
uv run --locked python -m microgrid ingest --source "/path/to/CUMCM2026Problems.zip-or-dir"
uv run --locked python -m microgrid inspect-data

# 工程检查 / 合成 smoke / 论文草稿
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
uv run --locked python -m microgrid smoke
uv run --locked python scripts/build_paper.py --mode draft
uv run --locked python scripts/build_paper.py --target ai-details --mode draft
```

## 正式运行与发布状态

```bash
uv run --locked python -m microgrid run --case q1
uv run --locked python scripts/prepare_submission.py --mode final
```

这两个命令的状态不是固定“永远失败”：

- 决策未批准时，`run` 必须被 `PendingDecisionError` 阻断；
- 决策批准后，dispatcher 会进入对应 runner；在模型实现前，runner 抛出 `ModelNotImplementedError`；
- final submission 只有在决策 approved、五个 case 均有显式选中的正式 run、`selection_status=approved`、run manifest 合法且非合成、五个 result 文件存在、论文无占位内容并具备 AI 详情 PDF 时才可能放行。

## 数据保护

原始题包、附件、模板和 `resources/` 不提交到 Git，也不进入 CI。请把官方附件放在本地对应目录或通过 `ingest` 导入。`records/inputs_manifest.json` 记录哈希，不记录可识别个人的绝对路径。

## 平台命令

Linux / macOS：

```bash
uv sync --locked
uv run --locked python -m microgrid doctor
```

若 `$HOME` 缓存目录不可写（例如只读挂载），可在当前仓库内指定：

```bash
export UV_CACHE_DIR="$PWD/.uv-cache"
export UV_PYTHON_INSTALL_DIR="$PWD/.uv-python"
export TMPDIR="$PWD/.tmp"
```

Windows PowerShell：

```powershell
uv sync --locked
uv run --locked python -m microgrid doctor
```

GNU Make 不是唯一入口；不要依赖修改 `PYTHONPATH`，完整命令统一通过 `python -m microgrid`。
