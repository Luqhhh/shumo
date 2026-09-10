# 协作约定

## 阶段边界

- **Stage 0 — completed**：数据层、时间标签、模板契约、smoke、论文层、release guards 已完成；不保留“模型永久不可实现”的常量阻断。
- **Stage 1 — active**：在参赛队人工批准 shared semantics 后实现正式模型。
- 未经批准的 decision 不得进入模型代码；已批准的 decision 只能按批准 contract 严格实现。

## 分支与 PR

- `main` 保持可复现；不要强推。
- Stage 1 常用分支：`infra/*`、`feat/q1-*`、`feat/q2-*`、`fix/*`、`docs/*`。
- PR 必须写明：
  - 改动范围和所依赖的 decision ID；
  - 实际执行的测试命令与退出码；
  - dispatch / release guard 是否受影响；
  - 未解决事项与跳过项；
  - AI 使用记录 segment 是否新增。
- 参赛队决定是否接纳和合并；Agent 不自动合并。

## Pre-division ownership

- A：Q1 + shared core（`problem/contracts.py`、`problem/common.py`）。
- B：Q2 / Q4-2。
- C：Q3 / Q4-3。
- 共享单点 owner：
  - `configs/decisions.toml`、`configs/selected_runs.toml`：A 为 primary owner；
  - `src/microgrid/schemas.py`、`src/microgrid/problem/contracts.py`、`src/microgrid/problem/common.py`：A 为 primary owner；
  - `pyproject.toml`、`uv.lock`：A 为 primary owner；
  - 修改共享 contract 至少需要另一名成员 review。
- `src/microgrid/dataio.py`、`src/microgrid/timekeys.py` 默认冻结；除非有可复现 bug，并且由 A 审核。

## Decision 门槛

- `configs/decisions.toml` 的 `status/choice/rationale/confirmed_by/confirmed_at` 不得由工程 Agent 修改。
- 模型 gate 按 case 拆分：`D_MODEL_Q1`、`D_MODEL_Q2`、`D_MODEL_Q3`、`D_MODEL_Q4_2`、`D_MODEL_Q4_3`。
- 共享物理语义由 `D_TIME`、`D_EFF`、`D_STATE`、`D_INFO` 承担；Q4-2 明确不依赖 `D_RESAMPLE`。
- required decision 仍为 pending 时，对应 case 必须被 `PendingDecisionError` 阻断。
- decision 已 approved 后，dispatcher 才允许调用 `src/microgrid/problem/q*.py`。
- 尚未实现的 runner 必须明确抛出 `ModelNotImplementedError`，不得返回伪结果。

## AI 使用记录

- `records/ai_usage.jsonl` 是 Stage 0 历史，只读保留。
- Stage 1 新记录写入 `records/ai_usage/entries/<contributor-or-session>.jsonl`，每行一个 JSON 对象。
- 只追加自己的 segment，不修改他人的 segment；最后用 `scripts/aggregate_ai_usage.py` 聚合。
- `human_review_status` 初始只能是 pending；只有参赛队实际核验后才能改为 confirmed 并补充 reviewed_by / reviewed_at。

## 远程仓库安全

- remote 可见性由参赛队确认；不要在未授权时改变可见性或创建镜像。
- remote 上不得包含原始题包、附件、模板、个人身份、绝对主目录、API 密钥或未筛选对话。
- 不自动公开发布原始材料；不把 CI 用于上传年度数据。
- 推送前检查 `git status`、`git diff --stat` 和候选文件清单。

## 模型与导出边界

- `problem/q*.py` 只产生统一的 `CaseResult` / domain results；不得直接操作 Excel。
- 官方 `result*.xlsx` 只能由 `excel_export.py` 从统一结果快照转换。
- 禁止每个 case 分别返回 DataFrame、dict 和直接写 workbook 三套口径。

## Formal run 最低契约

每个正式 run 的 manifest 至少包含：

- `case_id`、`run_id`；
- `input_hashes`、`config_snapshot`（包含 decision snapshot）；
- `code_commit`、`code_dirty`、`source_hash`；
- `status`、`is_synthetic`；
- `result_files`，可选 `result_sha256`。

`selected_runs.toml` 只允许显式 run_id，release guard 不读取“最新目录”。

## CI 边界

- `quality` job：锁文件检查、ruff、pytest（含 decision/release guard 测试）、合成 smoke。
- `paper` job：安装 TeX，编译 main.pdf 草稿和 AI 详情草稿。
- CI 不运行模型训练、优化求解、全年策略或正式实验。
- 真实数据集成检查只在本地显式执行。
