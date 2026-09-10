# AGENTS.md — Stage 0 工作边界

本文件是给未来执行 Agent / 协作者的硬约束。

1. **不得修改原始附件**：`data/raw/`、`data/templates/`、`resources/` 中的原件只读；所有处理输出到 `data/processed/`、`outputs/` 或 `dist/`。
2. **不得把合成数据当结果**：任何 `is_synthetic=true` 的产物不得进入 `dist/`，不得用于论文正式数值。
3. **不得替参赛队做模型决策**：`configs/decisions.toml` 的 pending 事项只能由参赛队人工批准；Agent 只可整理证据。
4. **不得伪造结果**：未实现 case 必须抛出 `ModelNotImplementedError` 或 `PendingDecisionError`，不得返回 0、optimal、空结果冒充解。
5. **不得篡改预报发布时间**：附件3的日期块填充只允许明显属于同一天的表头元数据；禁止对功率、价格、预测值全表 ffill。
6. **不得伪造文献或 AI 核验**：`records/ai_usage.jsonl` 中的人工核验初始为 pending；不得由 Agent 代填确认人、时间或“已核验”。
7. **不得自动批准、合并或发布**：不得改写 decisions 状态为 approved，不得强推、不得自动合并 PR，不得公开发布赛题材料。
8. **不得上传原始数据**：原始题包、附件、模板、resources、processed、outputs、dist、local 和秘密信息默认不跟踪；GitHub 操作仅限代码与工程文档。
9. **不得绕开正式阻断**：`prepare_submission.py --mode final` 和 `microgrid run --case ...` 在 Stage 0 必须非零退出。
10. **保护用户已存在文件**：执行前读取 README、AGENTS、配置和 `git status`；不 reset --hard、不强制推送、不删除原始数据。

环境：Python 3.11 + uv；CLI 用 `uv run --locked python -m microgrid ...`。
