# B 任务决策证据

本文只整理证据和人工审批问题，不改变 `configs/decisions.toml`，也不代表任何模型已获批准。

| Decision | 当前状态 | 已有证据 | 仍需参赛队确认 | 可执行核验 |
|---|---|---|---|---|
| `D_STATE` | proposed | 2025-01 初始 6000 kWh；shared-core 保存连续状态语义 | Q2/Q4-2 连续期末状态如何进入最终比较 | 检查跨日状态连续且没有每日重置 |
| `D_INFO` | proposed | actual 只在十分钟区间结束时可见；固定价格与历史 actual 分开 | 计划时刻可使用的历史窗口与预测器输入 | 对每个 decision_time 验证 `available_at <= decision_time` |
| `D_SETTLE` | pending | 题面给出紧急购电 5 倍、调整差异 50%/1.5 倍 | 完整结算分量、调整比较基准和余电处理 | 按分量对账，禁止把计划量替换成实际使用量 |
| `D_MODEL_Q2` | pending | 已有内部时间、电池和结果契约；本分支只提供输入适配 | 预测方法、变量、目标函数、约束和求解器 | 模型批准前 runner 必须继续抛出未实现错误 |
| `D_MODEL_Q4_2` | pending | 附件4可作为历史/回放价格；Q4-2 不依赖 `D_RESAMPLE` | 未来价格信息条件与 Q2 模型扩展 | 检查价格因果边界并保持 Q2/Q4-2 独立批准 |
| `D_TIME_TEMPLATE_EXPORT` | pending | 附件右端点网格与 result 模板字面标签存在异常 | 正式 Excel 的列到内部 slot 映射 | 批准前不得导出正式 result 工作簿 |

## 已确认需要保留的模型不变量

1. 附件1/2/4按右端点对齐；附件3“预报1小时”为发布后一小时，日期只在同日四行发布块内继承，数值禁止 ffill。
2. 负载和光伏是 kW；十分钟电量只换算一次：`energy_kwh = power_kw * (1 / 6 h)`。
3. 未来供需约束方向为 `grid_supplied_kwh + discharge_kwh + pv_kwh >= load_kwh + charge_kwh`。
4. 计划购电费按 `planned_purchase_kwh` 结算，不按实际使用量、净负荷或事后剩余量结算。

## 当前工程结论

输入适配、因果过滤和合成结果载体可以测试；Q2/Q4-2 的预测、优化、结算和正式导出仍未获批准、未实现。
