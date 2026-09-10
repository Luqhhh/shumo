# CUMCM 2026 C — 微网与外部电网电力调控策略

本仓库当前处于 **Stage 1 过渡期**。Stage 0 已完成：工程脚手架、数据层、时间标签解析、结果模板适配接口、运行记录、LaTeX 论文层、AI 使用记录、工程测试和提交预检。Stage 1 的工作是在人工批准 shared semantics 后实现正式模型。

## 阶段状态

### Stage 0 — completed（保留历史）

- 建立了数据导入、无损读取、时间标签解析、模板契约、smoke、论文草稿和 release guards；
- 未实现 Q1/Q2/Q3/Q4 的预测、优化、调度或费用模型；
- 未选择模型、求解器、储能方程、结算公式、预测方法或对照实验；
- 未产生正式结果。

### Stage 1 — active

- `microgrid run --case ...` 现在经过 dispatcher：
  - required decisions 仍有 pending：`PendingDecisionError`；
  - 全部 approved：调用 `src/microgrid/problem/q*.py`；
  - runner 尚未实现：`ModelNotImplementedError`。
- Agent 未经批准不得实现决策相关模型。
- 已经批准的 decision：Agent 可以严格按批准 contract 实现，不得自行扩展口径。
- `configs/decisions.toml` 的 approved 状态只能由参赛队人工修改。
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
