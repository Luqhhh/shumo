# 运行记录说明

- `ai_usage.jsonl` 是一行一条 JSON 的实际使用记录。真实型号未知时写 `未记录`。
- `human_review_status` 初始为 `pending`，只能由参赛队实际核验后更新。
- `inputs_manifest.json` 由 `python -m microgrid ingest` 生成，记录官方附件的相对路径、大小、SHA-256 和导入状态；不记录绝对主目录。
- 任何运行 manifest 若被用于正式支撑材料，需再次检查其中是否含个人路径或身份信息。

## 并行追加（Stage 1）

- Stage 0 的 `records/ai_usage.jsonl` 保留为历史，不再追加。
- 新记录写入 `records/ai_usage/entries/<contributor-or-session>.jsonl`，每行一个 JSON 对象。
- 使用 `uv run --locked python scripts/aggregate_ai_usage.py` 聚合；聚合输出 `paper/generated/ai_usage_aggregate.json`。
- 人工核验仍保持 pending，只有参赛队可确认。
