# C 线 Q3 求解器开工检查表

审查起点：`feat/q3-forecast-control@7b90599`；当前已同步
`origin/main@f8b4af0`。本文件只记录实现准备和接口矛盾，不改写人工批准口径。

## 1. 当前状态

| 模块 | 状态 | 证据或代码 | 开工前动作 |
|---|---|---|---|
| 时间、电池、InfoSet | 已批准并有 contract tests | `problem/contracts.py` | 保持不变 |
| 附件3版本读取 | 已实现方法中立适配器 | `problem/q3_inputs.py` | 正式组合器只能接收可见版本 |
| 小时到十分钟重采样 | 已批准并实现 | `problem/q3_resampling.py` | 直接复用，不在 solver 重写 |
| PV 版本组合 | 已批准并有生产实现 | `problem/q3_pv_forecast.py` | 接入统一 Q3 input bundle |
| 负载预测 | 已批准并有生产实现 | `problem/q3_load_forecast.py` | 接入统一 Q3 input bundle |
| 年度 actual adapter | 已选择性整合 | `problem/q2_inputs.py` | 复用，不另写全年读取器 |
| 统一 Q3 input bundle | 缺失 | 预测层、actual adapter 均已就绪 | machine decision 补录后组装 |
| 计划版本与冻结 | 已有内存实现 | `problem/q3_plan_ledger.py` | 已接结构化 sidecar |
| 结算账本 | 计划/调整部分已实现 | `problem/q3_plan_ledger.py` | 紧急费用等待实际回放规则澄清 |
| 结构化 sidecar | 已实现接口与往返测试 | `problem/q3_sidecars.py` | runner 后续登记路径与哈希 |
| Q3 gate | 已闭合 | 已检查 `D_LOAD_FORECAST/D_SETTLE/D_MODEL_Q3` | 保持阻断回归测试 |
| 实际回放 | 缺失 | 批准模型卡已有物理式 | 先澄清预测充电遇实际短缺的动作规则 |
| Q3 solver/runner | 未实现 | `problem/q3.py` 明确报错 | 预测与 ledger 输入准备后实现 |
| result3.xlsx 导出 | 未批准/未实现 | 当前模板 decision 只覆盖 Q1 | 不阻塞 solver，但阻塞最终交付 |

## 2. 仅剩的 P0 接口阻断

人工 gate 已由队长闭合，不需要再次表决 `LOAD-A/LF-A`、`SETTLE-A-v2`、
`VERSION-B/WEIGHT-B/HISTORY-A` 或 `epsilon=1`。队长提出的 HORIZON-B、charge
curtailment recourse 和结构化 sidecar 已得到 C 成员明确认可，精确文本见
[`q3_interface_decision_addendum.md`](q3_interface_decision_addendum.md)。现在只剩：

1. **machine decision 待人工补录**：Agent 不得改写 approved decision。需由队长
   将 HORIZON-B 和 charge curtailment recourse 亲自补入 Q3 的机器批准记录。
2. **PV tail baseline 算法仍未选**：HORIZON-B 只确定“因果补尾”，没有批准
   baseline 公式。需单独形成小 decision 并加入 Q3 gate 后才能实现。

以上均不能由 Agent自行补进正式模型。

## 3. 已提前补强的绿色测试

本分支在不采用任何未批准模型口径的前提下，补充：

- 00/06/12/18 四个发布时间边界的可见性；
- actual 只在十分钟区间右端可见；
- actual 跨午夜的真实时间可见性；
- 附件3的跨午夜 `valid_time`；
- 重复 `issue_time + lead_hours` 拒绝；
- LIN 全部 24 个小时节点精确命中；
- LIN/PCHIP 确定性和输入不变性。
- 正式 `VERSION-B + WEIGHT-B` 组合、平方倒数权重、历史截止、provenance
  和缺失因果历史显式失败；
- 正式 `LOAD-A + LF-A` 的 24 小时输出、四周滞后、AR(1)、非负截断、
  provenance、未来 actual 不可见和历史缺口显式失败。
- P00/P06/P12/P18 不可变快照、逐版 delta、已执行时隙冻结和
  `100 -> 110 -> 90 -> 95` 的 SETTLE-A-v2 手算费用。
- 三份 JSONL sidecar 的完整版本/来源保留、行数、SHA-256、相对路径和拒绝覆盖；
- B actual adapter 的每日144格、expected-day、负载/PV网格与右端点测试。

以下测试将在对应生产 contract 出现后立即加入，不使用 `xfail` 掩盖：

- 预测 provenance 的正式 sidecar 序列化往返；
- 紧急购电、合同电未利用与完整实际费用总计；
- 求解器可行解、独立残差检查、两日 SOC 连续和年末条件。

## 4. 最短实施路径

```text
1. 队长人工补录 HORIZON-B 与 charge curtailment recourse
2. 团队批准 PV tail baseline 小 decision
3. 组装不含未来 actual 的 Q3WindowInput
4. 实现单窗口 Q3 MILP + 独立 validator
5. 接入十分钟回放、settlement 和 run artifacts
6. 按合成窗口、1日、7日、1月、全年逐级验证
```

## 5. 求解器开工验收

只有以下项目全部成立，才把 `problem/q3.py` 从明确报错改成正式 runner：

- [x] 当前分支已同步队长批准 Q3 decision 的 main；
- [x] `D_LOAD_FORECAST/D_SETTLE/D_MODEL_Q3` approved 且确认字段完整；
- [x] Q3 gate 与已批准 decision 一致；
- [x] PV/负载候选有可复现的真实数据证据；
- [x] PV/负载生产预测器及因果、缺失历史、provenance 测试通过；
- [x] 计划版本、冻结和逐版调整费用的内存 contract 与手算测试通过；
- [ ] HORIZON-B 已由队长人工补入 machine decision，tail baseline 已批准；
- [ ] charge curtailment 已由队长人工补入 machine decision；
- [x] shared ledger/provenance 采用结构化 sidecar；
- [x] 修正版年度 actual adapter 已选择性整合；
- [x] 三份 sidecar 的结构、往返、拒绝覆盖和哈希测试通过；
- [ ] 不含未来 actual 的 `Q3WindowInput` 已通过 contract test；
- [x] 全量质量检查、synthetic smoke 和 Q3 gate 测试通过。

在最后五项完成前，可以实现独立的生产预测层，但不能让正式年度 runner返回成功。
