## 改动范围

<!-- 说明本 PR 修改了哪些职责模块，是否触及原始数据、决策、模型或论文结论。 -->

## 实际执行的命令与结果

<!-- 粘贴真实命令、退出码和日志路径；不要把未执行的命令写成已验证。 -->

- [ ] `uv lock --check`
- [ ] `uv run --locked ruff check .`
- [ ] `uv run --locked ruff format --check .`
- [ ] `uv run --locked pytest -q`
- [ ] `uv run --locked python -m microgrid smoke`
- [ ] `uv run --locked python scripts/build_paper.py --mode draft`
- [ ] `uv run --locked python scripts/build_paper.py --target ai-details --mode draft`
- [ ] 正式运行入口与 `prepare_submission.py --mode final` 仍然非零阻断

## 未解决事项

<!-- 列出 pending 决策、环境缺口、skip 的集成测试。未通过的测试不得写成通过。 -->

## AI 使用记录更新情况

<!-- 说明 records/ai_usage.jsonl 是否新增或修改；人工核验仍保持 pending。 -->

## 人工验收

- [ ] 参赛队已检查代码和原始数据对应关系
- [ ] 参赛队已查看论文草稿和 AI 详情草稿
- [ ] 不自动合并，由参赛队决定
