# CUMCM 2026 C — 微网与外部电网电力调控策略（Stage 0）

本仓库是 **工程脚手架**，不是解题方案。它负责数据盘点、无损读取、时间标签解析、结果模板适配接口、运行记录、LaTeX 论文草稿、AI 使用记录、工程测试和提交预检。

## Stage 0 明确不做什么

- 不实现 Q1/Q2/Q3/Q4 的预测、优化、调度或费用模型；
- 不生成正式结果；
- 不替参赛队选择模型、求解器、储能方程、结算公式、预测方法或对照实验；
- 不执行全年策略、参数搜索或正式比较实验；
- 不安装/默认使用 Gurobi、CPLEX、CVXPY、Pyomo、PyTorch 等未批准建模依赖；
- 不自动发布、自动提交或自动合并。

## 已实现命令

```bash
# 仅首次初始化
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

## 预留且当前应当失败的命令

```bash
uv run --locked python -m microgrid run --case q1
uv run --locked python -m microgrid run --case q2
uv run --locked python -m microgrid run --case q3
uv run --locked python -m microgrid run --case q4_2
uv run --locked python -m microgrid run --case q4_3
uv run --locked python scripts/prepare_submission.py --mode final
```

这些失败是 Stage 0 的预期行为，原因包括：模型未实现、D-MODEL 等决策仍为 pending、五个正式结果尚未产生。它们 **不是** 成功状态。

## 数据保护

原始题包、附件、模板和 `resources/` 不提交到 Git，也不进入 CI。请把官方附件放在本地对应目录或通过 `ingest` 导入。`records/inputs_manifest.json` 记录哈希，不记录可识别个人的绝对路径。

## 当前环境状态（交付时更新）

- 论文 TeX 工具：以 `uv run --locked python -m microgrid doctor` 的结果为准。
- TeX 缺包时，`latex_draft` 只能记为 `fail` 或 `not_run`，不能写成“论文已通过”。详见 `docs/handoff.md`。
- GitHub remote 的 private 可见性必须由有认证的参赛队核验后才能推送；本仓库不自动 push。

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
