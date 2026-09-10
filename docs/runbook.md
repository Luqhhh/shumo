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

## 正式结果与发布（当前预期失败）

```bash
uv run --locked python -m microgrid run --case q1
uv run --locked python scripts/prepare_submission.py --mode final
```

Stage 0 中必须非零退出。

## 私密仓库与本地数据

- 原始题包、附件、模板、`resources/`、`local/` 不提交。
- 若 remote 是 public 或可见性无法确认，不推送。
- 不运行模型训练、全年策略或正式实验。
- 任何提交前先看 `git status` 和 `git diff --stat`。
