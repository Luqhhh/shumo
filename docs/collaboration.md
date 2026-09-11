# 协作约定

## 阶段边界

- **Stage 0 — completed**：数据层、时间标签、模板契约、smoke、论文层、release guards 已完成；不保留“模型永久不可实现”的常量阻断。
- **Stage 1 — active**：在参赛队人工批准 shared semantics 后实现正式模型。
- 未经批准的 decision 不得进入模型代码；已批准的 decision 只能按批准 contract 严格实现。
- Stage 1 共享建模边界以 [`docs/modeling_plan.md`](modeling_plan.md) 为架构冻结版计划。该计划不代替 `configs/decisions.toml` 的人工批准状态。
- C 线三个待表决口径及回复模板见 [`docs/c_decision_cards.md`](c_decision_cards.md)；参赛队表决前它们不构成批准。
- 架构冻结后统一按 `decision -> contract -> contract test -> implementation -> validation` 推进；除非新题面证据、contract test 矛盾或正式数据审计发现问题，不再扩展 shared architecture。

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
  - `src/microgrid/schemas.py`、`src/microgrid/approvals.py`、`src/microgrid/problem/contracts.py`、`src/microgrid/problem/common.py`、`src/microgrid/problem/validation.py`、`src/microgrid/problem/result_io.py`：A 为 primary owner；
  - `pyproject.toml`、`uv.lock`：A 为 primary owner；
  - 修改共享 contract 至少需要另一名成员 review。
- `src/microgrid/dataio.py`、`src/microgrid/timekeys.py` 默认冻结；除非有可复现 bug，并且由 A 审核。

## A shared-core handoff

A 的第一批共享交付（不依赖 Q1 求解器完成）包括：

- 统一审批检查：`src/microgrid/approvals.py`；
- 递归源码指纹、运行指纹与输入来源检查：`src/microgrid/artifacts.py`；
- 结果载体验证：`CaseResult` / `IntervalResult`、`problem/validation.py`；
- 结果序列化：`problem/result_io.py`；
- B/C 接入说明：`docs/shared_api.md`。

B/C 可以从共享交付合并后的 main 同步并接入这些接口。共享物理语义 decision 未 approved 前，可以接入接口和写测试，但不得实现正式模型。接口细节以 `docs/shared_api.md` 为准。

## Decision 门槛

- `configs/decisions.toml` 的 `status/choice/rationale/confirmed_by/confirmed_at` 只能由参赛队修改。
- `status=proposed` 只表示已记录候选解释和理由，未经批准仍然阻断；只有 `approved` 且 confirmed 字段完整才放行。
- 模型 gate 按 case 拆分：`D_MODEL_Q1`、`D_MODEL_Q2`、`D_MODEL_Q3`、`D_MODEL_Q4_2`、`D_MODEL_Q4_3`。
- 共享物理语义由 `D_TIME_INTERNAL`、`D_EFF`、`D_STATE`、`D_INFO` 承担；Q4-2 明确不依赖 `D_RESAMPLE`。
- 内部时间网格（`D_TIME_INTERNAL`）与正式 Excel 映射（`D_TIME_TEMPLATE_EXPORT`）分开审批；前者不自动放行后者。
- 计划中建议新增的 `D_LOAD_FORECAST` 和 `D_REALTIME_DISPATCH` 尚未进入机器可读 gate；必须先由参赛队确认口径，再由 A 统一更新 decision 配置、case dependencies 和 tests。
- 当前 `D_STATE` 的已批准口径为“一月待机”。如果团队选择因果 warm-up 或 2 月 1 日重置，必须由参赛队正式修订并重新确认 `D_STATE`，不得只改代码。
- required decision 非 approved 时，对应 case 必须被 `PendingDecisionError` 阻断。
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

- `problem/q*.py` 只产生统一的 `CaseResult` / `IntervalResult` domain results；不得直接操作 Excel。
- 每个区间使用同一个 `IntervalResult` 载体：日期、slot、负载、光伏、计划/调整/紧急购电量、电池 action、起止储能，不允许把不同结构塞进 `metadata` 绕过。
- 官方 `result*.xlsx` 只能由 `excel_export.py` 从统一结果快照转换；`export_case_result` 受 `D_TIME_TEMPLATE_EXPORT` 独立门槛控制。
- `InfoSet.from_raw` 只保存 `available_at <= decision_time` 的可见项；禁止把包含未来 actual 的原始集合直接传给计划器。
- 十分钟基线控制不在时隙内重优化；时隙左端的动作不得使用该时隙右端才完整可得的 actual。
- 紧急购电是时隙内的 residual balancing energy；可在时隙结束后按实现量核算，但不得解释为停电后回头补电。

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
