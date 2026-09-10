## 改动范围

<!-- 说明本 PR 修改了哪些职责模块；是否触及原始数据、decisions、problem runners、checks、CI 或论文。 -->

## Decision 门槛

<!-- 本 PR 依赖哪些 decision ID？如果依赖 pending decision，则不得实现对应模型。 -->

- [ ] 未修改 `configs/decisions.toml` 的 status/choice/rationale/confirmed_by/confirmed_at
- [ ] 依赖的 decision 已 approved（如本 PR 实现模型）
- [ ] 未实现任何 pending decision 对应的模型、费用公式、预测或 resampling 方法

## 实际执行的命令与结果

<!-- 粘贴真实命令、退出码和日志路径；不要提前勾选未执行的命令。 -->

- [ ] `uv lock --check`
- [ ] `uv run --locked ruff check .`
- [ ] `uv run --locked ruff format --check .`
- [ ] `uv run --locked pytest -q`
- [ ] `uv run --locked python -m microgrid smoke`
- [ ] `uv run --locked python scripts/build_paper.py --mode draft`
- [ ] `uv run --locked python scripts/build_paper.py --target ai-details --mode draft`

## Dispatch / Release Guard

- [ ] pending decision 仍会阻断 `microgrid run`
- [ ] approved 后 dispatcher 会进入对应 `problem/q*.py`
- [ ] 未实现 runner 仍抛出 `ModelNotImplementedError`
- [ ] release guard 基于 selected_runs.toml + run manifest + result 文件，而不是 Stage 0 永久常量
- [ ] synthetic artifact 不会作为正式结果放行

## AI 使用记录

<!-- Stage 1 新记录应追加到 records/ai_usage/entries/ 下；不要修改 records/ai_usage.jsonl 历史。 -->

- [ ] 未修改他人的 AI usage segment
- [ ] 新记录 human_review_status 仍为 pending
- [ ] 必要时已运行 `uv run --locked python scripts/aggregate_ai_usage.py`

## 未解决事项

<!-- 列出 pending 决策、环境缺口、skip 的集成测试。未通过的测试不得写成通过。 -->

## 人工验收

- [ ] 参赛队已检查代码、原始数据对应关系与 decision contract
- [ ] 参赛队已查看论文草稿和 AI 详情草稿（如本 PR 涉及论文）
- [ ] 由参赛队决定是否合并；Agent 不自动合并
