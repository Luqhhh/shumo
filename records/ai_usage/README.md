# AI usage 并行追加结构

为适配多人并行开发，AI 使用记录不再要求所有贡献者同时编辑一个文件。

## 目录约定

- `records/ai_usage.jsonl`：Stage 0 历史记录，只读保留，不再追加。
- `records/ai_usage/entries/*.jsonl`：后续新增记录。每个贡献者或每个会话一个文件，示例：
  - `2026-09-10-q1-author.jsonl`
  - `2026-09-11-q2-modeler.jsonl`
- 每行一个 JSON 对象，UTF-8，无绝对路径、无姓名/学校/邮箱/API key。
- 只追加自己负责的文件，不修改他人的 segment，减少 Git 冲突。

## 必填字段

`entry_id`、`timestamp_utc`、`tool_name`、`version_model`、`purpose`、
`affected_files`、`prompt_summary`、`adoption`、`human_modification`、
`human_review_status`。

`human_review_status` 初始只能是 `pending`。只有参赛队实际核验后可改为
`confirmed`，且必须补充 `reviewed_by`、`reviewed_at`。

## 聚合

```bash
uv run --locked python scripts/aggregate_ai_usage.py
```

默认输出 `paper/generated/ai_usage_aggregate.json`，用于最终 AI 详情 PDF
和人工核验；同一 `entry_id` 重复会被拒绝。
