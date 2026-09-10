# AGENTS.md — Stage 1 工作边界

本文件是给未来执行 Agent / 协作者的硬约束。Stage 0 的工程脚手架已经完成；Stage 1 只允许在人工批准决策后实现模型。

1. **不得修改原始附件**：`data/raw/`、`data/templates/`、`resources/` 中的原件只读；所有处理输出到 `data/processed/`、`outputs/` 或 `dist/`。
2. **不得把合成数据当结果**：任何 `is_synthetic=true` 的产物不得进入 `dist/`，不得用于论文正式数值。
3. **未经批准的 decision 不得实现**：`configs/decisions.toml` 中 pending 的事项只能由参赛队人工批准；Agent 只可整理证据。模型 gate 按 case 拆分，不得用一个全局 D_MODEL 代替五个 case 的批准范围。
4. **已批准的 decision 可以严格实现**：Agent 可以按 approved 的 `choice/rationale/source` 实现对应 contract；不得自行补充或改写批准口径。
5. **不得自行修改 decisions.toml 的 approved 状态**：不得把 pending 改成 approved，不得代填 `confirmed_by` / `confirmed_at`。
6. **不得伪造结果**：未实现 case 必须抛出 `ModelNotImplementedError` 或 `PendingDecisionError`，不得返回 0、optimal、空结果冒充解。
7. **不得篡改预报发布时间**：附件3的日期块填充只允许明显属于同一天的表头元数据；禁止对功率、价格、预测值全表 ffill。
8. **不得伪造文献或 AI 核验**：`records/ai_usage.jsonl` 中的人工核验初始为 pending；不得由 Agent 代填确认人、时间或“已核验”。
9. **不得自动批准、自动合并或公开原始材料**：不得强推，不得自动合并 PR，不得公开发布赛题原始附件和年度数据。
10. **不得上传原始数据**：原始题包、附件、模板、resources、processed、outputs、dist、local 和秘密信息默认不跟踪；GitHub 操作仅限代码与工程文档。
11. **不得绕开正式阻断**：`prepare_submission.py --mode final` 和 `microgrid run --case ...` 必须按 pending decisions 与正式 run artifacts 的真实状态放行或阻断。
12. **保护用户已存在文件**：执行前读取 README、AGENTS、配置和 `git status`；不 reset --hard、不强制推送、不删除原始数据。

环境：Python 3.11 + uv；CLI 用 `uv run --locked python -m microgrid ...`。
