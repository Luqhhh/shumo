# Runbook

## 首次初始化

Linux / macOS：

```bash
uv lock
uv sync --locked
uv run --locked python -m microgrid doctor
```

若 `$HOME` 缓存目录不可写：

```bash
export UV_CACHE_DIR="$PWD/.uv-cache"
export UV_PYTHON_INSTALL_DIR="$PWD/.uv-python"
export TMPDIR="$PWD/.tmp"
```

Windows PowerShell：

```powershell
uv lock
uv sync --locked
uv run --locked python -m microgrid doctor
```

## 导入官方附件

```bash
uv run --locked python -m microgrid ingest --source "/path/to/CUMCM2026Problems.zip"
# 或
uv run --locked python -m microgrid ingest --source "/path/to/CUMCM2026Problems"
```

导入命令只复制 C 题及相关规范性文件，跳过 A/B/D/E；写入 `resources/`、`data/raw/`、`data/templates/` 和 `records/inputs_manifest.json`。

## 盘点

```bash
uv run --locked python -m microgrid inspect-data
```

报告写入 `outputs/inventory/inventory.json` 和 `inventory.md`。盘点只读，不清洗、不插值、不删负值。

## 工程检查与合成 smoke

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
uv run --locked python -m microgrid smoke
```

smoke 产物在 `outputs/smoke/<run_id>/`，`is_synthetic=true`、`model_status=not_implemented`。

## 论文草稿

```bash
uv run --locked python scripts/build_paper.py --mode draft
uv run --locked python scripts/build_paper.py --target ai-details --mode draft
```

Linux 需要 `xelatex`、`latexmk`、`ctex`、Fandol；若安装 bib 相关包，则主稿使用 biblatex + Biber，否则使用内置的草稿参考文献 fallback。缺少 TeX 工具时退出码非零，并在 handoff 中记录 `latex_verified=false`。

## 正式运行与发布

```bash
uv run --locked python -m microgrid run --case q1
uv run --locked python scripts/prepare_submission.py --mode final
```

Stage 1 行为：

- required decisions 未批准：`microgrid run` 以 `PendingDecisionError` 非零退出；
- decisions 已批准：dispatcher 调用 `src/microgrid/problem/q*.py`；runner 未实现时抛出 `ModelNotImplementedError`；
- final submission 只有在决策 approved、`configs/selected_runs.toml` 显式指定五个 run_id、对应 run manifest 合法且非合成、五个 result 文件存在、论文无占位内容并具备 AI 详情 PDF 时才放行。

正式 run 的约定目录：

```text
outputs/runs/<case_id>/<run_id>/
├── manifest.json
└── results/
    └── resultN.xlsx
```

`manifest.json` 必须包含 `run_id`、`case_id`、`status="success"`、
`is_synthetic=false`、`result_files`，可选的 `result_sha256` 会被校验。

正式比较实验、求解器和论文结论仍由参赛队人工验收。

## 远程与本地数据

- 原始题包、附件、模板、`resources/`、`local/` 不提交。
- remote 的可见性由参赛队确认；只提交代码、工程文档和哈希 manifest，不提交原始年度数据。
- 不自动合并、不强推、不自动公开发布原始材料。
- 任何提交前先看 `git status` 和 `git diff --stat`。
