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
| 负载预测 | 已批准并有30 h不可变版本 | `problem/q3_load_forecast.py`、`q3_load_snapshot.py` | 中间 MPC 仅切24 h窗口 |
| 年度 actual adapter | 已选择性整合 | `problem/q2_inputs.py` | 复用，不另写全年读取器 |
| PV snapshot/window | 接口与测试已实现 | `problem/q3_pv_snapshot.py` | 接入获批 `TAIL-EXP2` |
| 统一 Q3 input bundle | 已实现并有 contract tests | `problem/q3_window.py` | solver 直接消费，不接 raw actual |
| 计划版本与冻结 | 已有内存实现 | `problem/q3_plan_ledger.py` | 已接结构化 sidecar |
| 结算账本 | 计划/调整/紧急事件已实现 | `q3_plan_ledger.py`、`q3_sidecars.py` | 汇总进年度 artifacts |
| 结构化 sidecar | 已实现精确字段与引用检查 | `problem/q3_sidecars.py` | runner 后续登记路径与哈希 |
| Q3 gate | tail decision 已批准 | `D_PV_TAIL_BASELINE=approved` | 保留 gate 防回退 |
| 实际回放 | 单步控制器与ATTR-PV-FIRST已接入 | `q3_replay.py`、`q3_controller.py` | 组装完整滚动driver |
| 单窗口 Q3 MILP | 已实现并独立验证 | `problem/q3_solver.py` | 接入滚动控制器 |
| Q3 年度 runner | 未实现 | `problem/q3.py` 明确报错 | 组装滚动、回放和 artifacts |
| result3.xlsx 导出 | 未批准/未实现 | 当前模板 decision 只覆盖 Q1 | 不阻塞 solver，但阻塞最终交付 |

## 2. 剩余的 P0 阻断

队长已确认 HORIZON-B、charge curtailment recourse、结构化 sidecar 与 B adapter
整合边界；2026-09-13 又单独确认 C-6 `TAIL-EXP2`。该算法已按1至7日同slot、
2日半衰期固定权重实现，仍由 `D_PV_TAIL_BASELINE` gate 防止决策状态回退。

对称的负载覆盖断点也已由队长单独确认 C-7 `LOAD-HORIZON-A`：LOAD-A 仍只在
00/06/12/18发布，每版冻结30小时；中间 MPC 仅从最近发布版本切取24小时，
不重新预测。30小时只覆盖最长5小时50分钟缺口，不改变 Q2 更新机制。

预测与模型 decision blocker 已清零，统一 `Q3WindowInput`、单窗口 MILP、独立
validator 和单步计划/回放控制器均已完成。C成员于2026-09-13选择
`ATTR-PV-FIRST`，回放现已将 surplus 明确拆成 `grid_spill` 与
`pv_curtailment`，并能生成现有 `IntervalResult`；该归属是建模假设，不写成题面事实。

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
- 正式 `LOAD-A + LF-A + LOAD-HORIZON-A` 的30小时不可变版本、四周滞后、
  AR(1)、非负截断、provenance、未来 actual 不可见和历史缺口显式失败；中间
  11:50 决策从06:00版完整切出未来24小时且不重新预测。
- P00/P06/P12/P18 不可变快照、逐版 delta、已执行时隙冻结和
  `100 -> 110 -> 90 -> 95` 的 SETTLE-A-v2 手算费用。
- 发布时刻一次性生成不可变的144点 PV snapshot，中间 MPC 只切片并只向 tail
  协议请求未覆盖后缀，不重新组合或重采样；
- 三份 JSONL sidecar 的精确字段、完整版本链、预测ID引用、行数、SHA-256、
  相对路径和拒绝覆盖；
- B actual adapter 的每日144格、expected-day、负载/PV网格与右端点测试。
- 统一 `Q3WindowInput` 对齐144格负载/PV/右端点价格/合同状态/SOC；00:00新计划、
  06/12/18可调整、非发布时间固定、次日仅lookahead四种模式互斥；未来版本、
  过期 ledger 和年末未截断窗口均显式失败；年末改用6000 kWh硬约束且移除残值。
- 单窗口 SciPy/HiGHS MILP 覆盖合同量、逐版增减、充放电互斥、SOC、PV消纳、
  合同余电、预测紧急电量及其互斥；独立 validator 重新计算全部物理残差和经济
  分项。完整144格合成窗口、固定合同短缺、调整增加和年末不可达均已测试。
- 单步控制器把00:00解转换成版本0、把06/12/18解追加为新版本，只执行第一格
  电池动作，并使用真实区间计算 charge curtailment 与紧急购电；紧急费用以区间
  右端点入账，sidecar 不再推测或伪造 replay 事件。

以下测试将在对应生产 contract 出现后立即加入，不使用 `xfail` 掩盖：

- 紧急购电、合同电未利用与完整实际费用总计；
- 求解器可行解、独立残差检查、两日 SOC 连续和年末条件。

## 4. 最短实施路径

```text
1. 组装合成1日滚动 driver
2. 写入三份 sidecar 和 CaseResult
3. 再依次做真实1日、7日、1月、全年
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
- [x] LOAD-HORIZON-A 已由队长确认并实现30 h版本/24 h切片 contract；
- [x] shared ledger/provenance 采用结构化 sidecar；
- [x] 修正版年度 actual adapter 已选择性整合；
- [x] 三份 sidecar 的精确结构、预测引用、往返、拒绝覆盖和哈希测试通过；
- [x] 不含未来 actual 的 `Q3WindowInput` 已通过对齐、版本、ledger和年末contract test；
- [x] 单窗口 Q3 MILP 与独立 validator 已通过1格和完整144格合成测试；
- [x] 单步计划版本更新、第一格回放和紧急结算 sidecar 已通过合成测试；
- [x] `ATTR-PV-FIRST` 已由C成员确认，单步回放可生成 `IntervalResult`；
- [x] 全量质量检查、synthetic smoke 和 Q3 gate 测试通过。

在完整滚动 driver 和年度 runner 完成前，正式年度 runner仍不能返回成功。
