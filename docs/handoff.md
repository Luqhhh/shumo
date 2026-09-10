# Stage 0 交接报告

生成日期：2026-09-10。以下状态来自本机实际执行结果；不是人工验收结论。

## 状态总表

| 项目 | 状态 | 证据 / 说明 |
|---|---|---|
| input_inventory | pass | `outputs/inventory/inventory.json` / `inventory.md`；14 个导入项；发现 6 条多日模板字面区间警告 |
| raw_data_unchanged | pass | 14 个导入项与原始本地输入 SHA-256 一致 |
| unit_tests | pass | `uv run --locked pytest -q`：24 passed |
| ruff | pass | `uv run --locked ruff check .`、`ruff format --check .` |
| synthetic_smoke | pass | `outputs/smoke/smoke-2026-09-10T122347+0000-f808ddd9/`；is_synthetic=true；model_status=not_implemented |
| latex_draft | pass | `paper/build/main.pdf`，75 页（正文开始于第 2 页；正文结束标记在第 7 页，附录另计） |
| ai_details_draft | pass | `paper/build/AI 工具使用详情.pdf`，2 页；仍标 pending 人工核验 |
| ci_config | configured_not_run | `.github/workflows/ci.yml` 已写入；因推送被阻断，GitHub Actions 未实际执行 |
| template_time_mapping | pending_human_decision | D-TIME 等 8 项 pending |
| model_implementation | not_implemented | `microgrid run --case ...` 非零退出 |
| formal_experiments | not_started | 无正式 run、无选中的结果快照 |
| final_submission | blocked | `prepare_submission.py --mode final` 与 `build_paper.py --mode final` 均退出 5 |
| push_to_github | blocked | remote 实际可见性与用户假设不一致，且无认证凭据 |

## 环境与执行说明

本机 `$HOME` 所在根文件系统为只读挂载，因此使用仓库内缓存目录运行 uv：

```bash
export UV_CACHE_DIR="$PWD/.uv-cache"
export UV_PYTHON_INSTALL_DIR="$PWD/.uv-python"
export TMPDIR="$PWD/.tmp"
```

这些目录均在 `.gitignore` 中，不提交。

## 实际执行命令与结果

| 命令 | 退出码 | 日志 / 产物 |
|---|---|---|
| `uv lock` | 0 | `uv.lock` |
| `uv sync --locked` | 0 | `.venv/` |
| `uv run --locked ruff check .` | 0 | `outputs/quality/final_quality.log` |
| `uv run --locked ruff format --check .` | 0 | `outputs/quality/final_quality.log` |
| `uv run --locked pytest -q` | 0 | 24 passed |
| `uv run --locked python -m microgrid doctor` | 0 | `outputs/quality/doctor.log`；警告 `biber NOT FOUND` |
| `uv run --locked python -m microgrid ingest --source "<本地原始输入目录>"` | 0 | `records/inputs_manifest.json`；14 items，0 conflicts |
| `uv run --locked python -m microgrid inspect-data` | 0 | `outputs/inventory/inventory.json`、`inventory.md` |
| `uv run --locked python -m microgrid smoke` | 0 | `outputs/smoke/smoke-2026-09-10T122347+0000-f808ddd9/` |
| `uv run --locked python scripts/build_paper.py --mode draft` | 0 | `paper/build/main.pdf` |
| `uv run --locked python scripts/build_paper.py --target ai-details --mode draft` | 0 | `paper/build/AI 工具使用详情.pdf` |
| `uv run --locked python -m microgrid run --case q1` | 4（预期失败） | `outputs/quality/q1_block.log` |
| `uv run --locked python scripts/build_paper.py --mode final` | 5（预期失败） | `outputs/quality/build_final_block.log` |
| `uv run --locked python scripts/prepare_submission.py --mode final` | 5（预期失败） | `outputs/quality/final_block.log` |
| `uv run --locked python scripts/check_submission.py --paper paper/build/main.pdf --support outputs/quality/empty_support.zip` | 1（预期失败） | 验证候选包缺少五个正式结果和 AI 详情 PDF 时被阻断 |

## 原始数据保全

`records/inputs_manifest.json` 中的 14 个导入项，已逐一与 `<本地原始输入目录>` 中原始文件比较，SHA-256 全部一致。由此确认本机导入未改变原始附件、题面和规范文件。

## 关键工程发现

1. 附件 1 的首个时间值是 0:10（Excel 数值），不是 0:00；A145 为 `0:00+1`。
2. 附件 2/4 是 365 个日期行 × 144 个时间列，时间表头从 0:10 到 `0:00+1`。
3. 附件 3 的后续预报行日期为**空字符串**，不是空单元格；只在该 4 行预报块内按非空日期局部继承；预报值没有 ffill。
4. result2/3/4 多日计划表最后一列的字面标签是 `0:00-0:10+1`，按字面解析跨度 1450 分钟；盘点已报告 6 条此类警告，没有自动修正。
5. result2/3/4 的 `充放电量` 和 `紧急购电量` 只有示例块与 `⁝` 行，不能按现有行数直接覆盖 334 天。
6. 五个模板当前无公式、批注、合并单元格或数据验证；result4-2 与 result2、result4-3 与 result3 初始哈希相同。

## 环境缺口

- `biber`、`biblatex`、`gb7714-2015` 当前本机未安装；主稿草稿使用内置 fallback 参考文献。若 CI/本机安装这些包，`preamble.tex` 会优先使用 biblatex。
- `latexmk` 与 `xelatex` 可用；Fandol、ctex、fvextra 可用。
- GitHub Actions 尚未实际运行；CI 中的 TeX 安装步骤只能算配置，不算已验证。
- 本机 Python 3.11.9 由 uv 下载到仓库内 `.uv-python/`；系统 Python 为 3.10。

## GitHub 推送状态

用户给定的 remote 地址保存在本地 Git 配置中，不写入论文支撑材料。执行时通过 GitHub API 未认证读取到：

```text
private: false
```

这与“private GitHub repo”的假设不一致。当前环境没有 `GH_TOKEN`、没有凭据 helper、`gh` 未登录。`git push --dry-run -u origin main` 退出 128：`could not read Username for 'https://github.com': terminal prompts disabled`。因此本轮**未 push、未创建 public 镜像、未开启 Actions**。按任务书“首次推送前核对 private 可见性”的要求，推送被正确阻断。

若参赛队把 remote 改为 private 并提供可用认证，可在本地仓库重新执行：

```bash
git push -u origin main
```

不要强推，不要创建 public 镜像。实际 push 成功与否以当时的认证和仓库可见性为准。

## 待参赛队决策

以下事项仍为 pending：

- D-TIME：输入点值/区间量与模板 0:10、`+1` 映射；
- D-EFF：90% 效率含义与充放电量侧；
- D-STATE：1 月到 2 月及 Q2–Q4 的跨日状态/终端条件；
- D-INFO：各决策时刻实际可见信息；
- D-RESAMPLE：小时预报到 10 分钟输入的转换与边界；
- D-SETTLE：计划/调整/紧急费用结算与余电处理；
- D-MODEL：目标、变量、约束、方法和求解器；
- D-EVAL：正式比较、验证设计与证据标准。

## 交接建议

1. 先由参赛队人工核验 `records/ai_usage.jsonl` 并更新核验状态。
2. 人工确认 D-TIME、D-EFF、D-STATE、D-INFO 后，再单独下达模型实现任务。
3. 如需推送，先把用户指定 remote 的可见性和权限处理到参赛队确认的状态。
4. 任何 CI 运行、正式结果、结算公式和论文结论都必须重新人工验收。
