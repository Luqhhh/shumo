# C 线 Q3 求解器开工检查表

工作分支：`feat/q3-forecast-control`；共享基线为 `origin/main@f8b4af0`。
本文件只记录实现准备和剩余门禁，不改写人工批准口径。

## 1. 当前状态

| 模块 | 状态 | 证据或代码 | 开工前动作 |
|---|---|---|---|
| 时间、电池、InfoSet | 已批准并有 contract tests | `problem/contracts.py` | 保持不变 |
| 附件3版本读取 | 已实现方法中立适配器 | `problem/q3_inputs.py` | 正式组合器只能接收可见版本 |
| 小时到十分钟重采样 | 已批准并实现 | `problem/q3_resampling.py` | 直接复用，不在 solver 重写 |
| PV 版本组合 | 已批准并有生产实现 | `problem/q3_pv_forecast.py` | 接入统一 Q3 input bundle |
| 负载预测 | 已批准并有生产实现 | `problem/q3_load_forecast.py` | 接入统一 Q3 input bundle |
| 年度 actual adapter | 已选择性整合 | `problem/q2_inputs.py` | 复用，不另写全年读取器 |
| PV snapshot/window | 接口与测试已实现 | `problem/q3_pv_snapshot.py` | 接入获批 `TAIL-EXP2` |
| 统一 Q3 input bundle | 缺失 | 预测层、actual adapter、PV window 均已就绪 | tail 算法与负载覆盖澄清后组装 |
| 计划版本与冻结 | 已有内存实现 | `problem/q3_plan_ledger.py` | 已接结构化 sidecar |
| 结算账本 | 计划/调整部分已实现 | `problem/q3_plan_ledger.py` | 紧急费用等待实际回放实现 |
| 结构化 sidecar | 已实现精确字段与引用检查 | `problem/q3_sidecars.py` | runner 后续登记路径与哈希 |
| Q3 gate | tail decision 已批准 | `D_PV_TAIL_BASELINE=approved` | 保留 gate 防回退 |
| 实际回放 | 语义已冻结、实现缺失 | charge curtailment recourse | 不得扩展成任意 redispatch |
| Q3 solver/runner | 未实现 | `problem/q3.py` 明确报错 | 预测与 ledger 输入准备后实现 |
| result3.xlsx 导出 | 未批准/未实现 | 当前模板 decision 只覆盖 Q1 | 不阻塞 solver，但阻塞最终交付 |

## 2. 剩余的 P0 阻断

队长已确认 HORIZON-B、charge curtailment recourse、结构化 sidecar 与 B adapter
整合边界；2026-09-13 又单独确认 C-6 `TAIL-EXP2`。该算法已按1至7日同slot、
2日半衰期固定权重实现，仍由 `D_PV_TAIL_BASELINE` gate 防止决策状态回退。

实现 `Q3WindowInput` 时又发现一个对称的负载覆盖断点：现有 LOAD-A 只允许
00/06/12/18生成最多24小时预测，不能覆盖中间 MPC 的固定24小时窗口尾部。
这不一定需要新算法，但必须由队长确认是“发布时间生成30小时、随后切片”还是
“每10分钟重新预测24小时”。精确选项见 Card C-7；Agent 不擅自改变已批准的
LF-A 更新频率或 horizon。

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
- 发布时刻一次性生成不可变的144点 PV snapshot，中间 MPC 只切片并只向 tail
  协议请求未覆盖后缀，不重新组合或重采样；
- 三份 JSONL sidecar 的精确字段、完整版本链、预测ID引用、行数、SHA-256、
  相对路径和拒绝覆盖；
- B actual adapter 的每日144格、expected-day、负载/PV网格与右端点测试。

以下测试将在对应生产 contract 出现后立即加入，不使用 `xfail` 掩盖：

- 紧急购电、合同电未利用与完整实际费用总计；
- 求解器可行解、独立残差检查、两日 SOC 连续和年末条件。

## 4. 最短实施路径

```text
1. 单独确认并实现 LOAD-HORIZON-A
2. 组装不含未来 actual 的 Q3WindowInput
3. 验证 PV/Load 两类不可变版本均只按最新发布时间切片
4. 实现单窗口 Q3 MILP + 独立 validator
5. 接入 charge curtailment 回放、settlement 和 run artifacts
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
- [x] HORIZON-B snapshot/window 接口已冻结并通过切片、补尾范围和禁止重采样测试；
- [x] charge curtailment recourse 已由队长与 C 成员冻结；
- [x] charge curtailment 生产 contract 与公式级测试已实现，不做 spill 来源归属；
- [x] `D_PV_TAIL_BASELINE` 已批准为 `TAIL-EXP2` 并有生产实现与证据；
- [ ] LOAD-A 对中间 MPC 的24小时覆盖方式已由队长确认；
- [x] shared ledger/provenance 采用结构化 sidecar；
- [x] 修正版年度 actual adapter 已选择性整合；
- [x] 三份 sidecar 的精确结构、预测引用、往返、拒绝覆盖和哈希测试通过；
- [ ] 不含未来 actual 的 `Q3WindowInput` 已通过 contract test；
- [x] 全量质量检查、synthetic smoke 和 Q3 gate 测试通过。

在 tail decision、窗口输入、求解器和回放均完成前，正式年度 runner不能返回成功。
