# B 任务 Q2/Q4-2 模型卡

- 状态：未批准、未实现
- 当前可执行范围：输入适配、来源追踪、内部时间对齐、因果信息过滤、合成结果接口测试
- 当前禁止范围：正式预测、优化、结算、case 运行、结果 Excel 导出和论文数值引用

## 输入契约

- 附件1固定电价：144 个唯一右端点 slot，有限且非负。
- 附件2实际负载/PV：每个日期各 144 个唯一 slot，两个 sheet 键集合完全一致，保留 kW。
- 附件4实际波动电价：每个日期 144 个唯一 slot，有限且非负；只表示历史或回放事实。
- 每个来源保留文件名、sheet、cell 和 SHA-256 前缀；bundle 保留完整文件 SHA-256。

## 时间与信息边界

- `0:10` 对应 `[00:00, 00:10)`；`0:00+1` 对应 `[23:50, 次日 00:00)`。
- 完整区间 actual 的 `valid_time` 与 `available_at` 都是区间结束时刻。
- 固定电价不混入历史 actual 信息项；未来波动电价不得由历史实测价格冒充。
- `D_TIME_TEMPLATE_EXPORT` 获批前，内部 slot 不映射到正式 result 模板。

## 单位与未来模型验收条件

- `load_kw`、`pv_kw` 保留功率；模型量使用 `load_kwh = load_kw / 6`、`pv_kwh = pv_kw / 6`。
- 供需约束必须使用 `>=`，允许未利用供给或弃光，不为凑等式伪造量。
- 计划购电费必须由计划购电量乘对应电价得到。
- 调整、违约、紧急购电费用保持独立，等待 `D_SETTLE` 和相应模型 decision 获批。

## 输出契约

批准后的模型必须用 shared `IntervalResult` / `CaseResult` 承载计划、调整、紧急购电、电池动作和状态轨迹。当前测试结果必须设置 `is_synthetic=True`，不得进入 `dist/`。

## 批准前置

参赛队需要分别审批 `D_STATE`、`D_INFO`、`D_SETTLE`、`D_MODEL_Q2` 和 `D_MODEL_Q4_2`；正式 Excel 还需要 `D_TIME_TEMPLATE_EXPORT`。审批必须在配置中由人工填写状态、确认人和确认时间，本分支不代填。

## 回归核验

- 目标与全量 pytest 通过。
- Ruff lint 和 format check 通过。
- `q2.py`、`q4_2.py` 仍抛出 `ModelNotImplementedError`。
- decisions 配置无差异。
- Git diff 不含原始题包、raw、templates、resources、outputs 或 dist 文件。
