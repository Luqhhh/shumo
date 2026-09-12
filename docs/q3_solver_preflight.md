# Q3 求解器预审附录

本文把 C 线已经完成的证据、接口断点和求解器验收样例集中到一处，供批准模型卡
实现前复核。本文件不是批准记录；`configs/decisions.toml` 仍是唯一机器可读状态
来源。若本附录与已批准的 [`docs/q3_model.md`](q3_model.md) 冲突，以机器 decision
及已批准模型卡为准；发现自相矛盾时必须先由参赛队澄清，不能由实现者自行补齐。

## 1. 当前已有基础

以下内容已有代码或可复核证据：

- `D_TIME_INTERNAL / D_EFF / D_STATE / D_INFO` 已批准；
- `Q3ForecastArchive` 保留全部 `issue_time / valid_time / lead_hours`，并通过
  `InfoSet.from_raw` 执行 `available_at <= decision_time`；
- `D_RESAMPLE` 已批准并实现：组合后的唯一小时节点先用 `RESAMPLE-LIN` 转成
  十分钟右端点，PCHIP 只作敏感性；
- `VERSION-B + WEIGHT-B + HISTORY-A` 已有严格因果回测证据，但仍属于
  `D_MODEL_Q3` 候选；
- `LOAD-A + LF-A` 已有严格因果回测证据，但机器配置中尚无获批的
  `D_LOAD_FORECAST`；
- Q3 正式 runner 仍明确抛出 `ModelNotImplementedError`，没有伪造结果。

## 2. 求解器开工前的人工确认清单

以下每项都必须在团队作答后进入相应 decision。实现者不得代选。

| 编号 | 必须确认的问题 | 当前候选或证据 |
|---|---|---|
| Q3-M1 | PV 多版本怎样组合 | `VERSION-B + WEIGHT-B + HISTORY-A`，56 日历史作敏感性 |
| Q3-M2 | 平方倒数权重的 `epsilon_kw` | 0.1、1、5、10 kW 回测接近；必须指定一个正式值 |
| Q3-M3 | 负载预测与日内更新 | `LOAD-A + LF-A`，`LF-B` 作机制对照 |
| Q3-M4 | 每十分钟重算时，24 h PV 曲线尾部怎样处理 | 见第 4 节三种互斥方案 |
| Q3-M5 | 00/06/12/18 能修改哪些未来交付时隙 | 必须明确是否跨日、当前时隙是否可改 |
| Q3-M6 | 调整费的逐笔账单 | `SETTLE-A` 只确定“相对上一版”；完整费用含义仍需手算确认 |
| Q3-M7 | 实际回放时电池怎样因果响应 | 是否采用每十分钟 MPC、残余紧急购电及动作取消规则 |
| Q3-M8 | 窗口终端与年度边界 | 终端价值、年末 6000 kWh、提前可达域是否采用及参数 |
| Q3-M9 | 同成本解的二级目标 | 是否按合同电未利用、弃光、电池吞吐量做词典序优化 |
| Q3-M10 | 求解器和数值标准 | 是否沿用 SciPy/HiGHS；时限、MIP gap、绝对/相对容差 |

若团队把 `D_MPC / D_TERMINAL / D_YEAR_BOUNDARY / D_REALTIME_DISPATCH` 保留为
独立 decision，A 必须把它们接入 Q3 gate；若决定全部并入 `D_MODEL_Q3`，应在
模型卡中完整写明且删除悬空的独立 proposed 口径。不能出现“代码在使用、gate
却没有检查”的状态。

## 3. 候选分层与数据流

建议把实现保持为六层，便于单独验证和替换：

```text
原始附件（只读）
  -> InfoSet 因果过滤
  -> Load/PV forecast + provenance
  -> 每个 valid_time 唯一小时 PV -> 已批准的十分钟重采样
  -> Q3WindowInput（不含未来 actual）
  -> 计划/调整优化器
  -> 当前十分钟实际回放 + settlement
  -> CaseResult + 审计账本
```

优化器不得持有全年 actual bundle。实际负载与实际 PV 只在对应十分钟区间结束后
进入回放；当前计划输入只能由 `InfoSet` 和预测结果构造。

### 3.1 建议的窗口输入（接口草案）

```text
Q3WindowInput
- decision_time
- initial_battery_state
- slots: tuple[Q3PlanningSlot, ...]
- terminal_condition

Q3PlanningSlot
- interval_start / interval_end
- load_forecast_kwh / load_forecast_id
- pv_forecast_kwh / pv_forecast_id
- normal_price_cny_per_kwh
- previous_committed_kwh
- purchase_is_fixed
```

`purchase_is_fixed` 应由 commitment policy 在求解器外计算。求解器只服从给定
掩码，不自行猜测哪些时隙已经冻结。

### 3.2 预测 provenance 最低字段

```text
forecast_id
kind                       # load / pv
decision_time
valid_time
available_at
value_kw / value_kwh
training_cutoff
model_version / data_version
source_refs
```

PV 组合还应保存候选 `issue_time`、`lead_hours`、历史 MAE、归一化权重、
`epsilon_kw` 和 fallback 原因。负载预测还应保存 7/14/21/28 日滞后值、权重、
`ar1_phi`、最新可见残差和是否截断到 0。

## 4. 十分钟 MPC 与 24 小时预报的接口冲突

这是进入求解器前必须新回答的问题。已批准的重采样器以 00/06/12/18 的发布
时刻为锚点，只生成 `issue_time+10min ... issue_time+24h`。如果 06:10 又要求
固定 24 h MPC，06:00 的曲线只剩 23 h 50 min；到 11:50 时只剩 18 h 10 min。

团队需要在下列方案中选择并写入 `D_MPC` 或 `D_MODEL_Q3`：

| 方案 | 做法 | 主要影响 |
|---|---|---|
| `HORIZON-A` | 发布时刻生成 24 h 曲线；其后十分钟控制使用剩余曲线，窗口逐渐缩短，下一发布时刻再恢复 24 h | 最简单，但 look-ahead 在 18–24 h 之间变化，终端价值需匹配 |
| `HORIZON-B` | 每十分钟保持固定 24 h；最新附件3曲线覆盖的部分优先使用，尾部由获批的因果基线预测补齐并记录 fallback | 窗口固定，但依赖正式 Q2 型预测及完整 provenance |
| `HORIZON-C` | 只在 00/06/12/18 求完整计划；中间十分钟只执行经批准的实时电池规则，不重做 24 h 优化 | 求解次数少，但与“每十分钟 MPC”提案不同 |

不得把 06:00 的整点节点平移成 06:10、07:10 等伪造节点，也不得用下一次尚未
发布的 forecast 补尾。

## 5. 候选数学结构（待 `D_MODEL_Q3` 批准）

对当前窗口内每个十分钟时隙 `k`，候选变量为：

```text
q_new[k]              新确认合同购电量
delta_plus[k]         相对上一版的增加量
delta_minus[k]        相对上一版的减少量
c[k], d[k]            母线侧充、放电量
z[k]                  充放电模式二进制变量
pv_use[k]             预测光伏消纳量
s_grid[k]             预测场景下未利用合同电
E[k], E[k+1]          电池内部储能
```

候选约束：

```text
q_new = q_previous + delta_plus - delta_minus
q_new + pv_use + d = load_hat + c + s_grid
0 <= pv_use <= pv_hat
E[k+1] = E[k] + 0.9*c[k] - d[k]/0.9
1200 <= E[k] <= 10800
0 <= c[k] <= (5000/6)*z[k]
0 <= d[k] <= (5000/6)*(1-z[k])
q_new, delta_plus, delta_minus, pv_use, s_grid >= 0
```

对 `purchase_is_fixed=true` 的时隙，要求 `q_new=q_previous` 且本版 delta 为 0。
历史版本本身永远不可改写；Q3 在允许的发布时间，只能为尚未执行且团队允许调整
的未来目标追加新版本。“已提交”不能被解释成当天所有目标从 00:00 起永久冻结，
否则 06/12/18 的合法调整会被完全禁止。

### 5.1 经济目标仍需确认

题面给出的比例必须保留，但完整账单仍由 `D_SETTLE` 决定。至少要用手算例明确：

- 正常计划费是否始终为 `sum(p_base[k] * q_plan[k])`；
- 减少部分的 `0.5*p_trade*delta_minus` 是追加违约费，还是替代原购买费用；
- 增加部分是否为 `1.5*p_trade*delta_plus`；
- 06:00 等交易时刻究竟映射到哪一个十分钟价格；
- 紧急购电是否为 `5*p_execute*emergency`；
- 合同电未利用是否仍全额付费且没有退款。

正式评价必须从实际执行和逐笔账本重新计算，不能把预测优化目标直接当作全年真实
成本。

## 6. 实际回放候选账本

若团队批准当前建模草案，物理账本应避免重复计算弃光：

```text
q_confirmed + pv_use + d + emergency
  = load_actual + c + grid_spill

pv_available = pv_use + pv_curtailment
```

这里左侧使用的是 `pv_use`，因此右侧不能再次把 `pv_curtailment` 加进同一条平衡
等式。紧急购电是 residual balancing energy，不是可自由套利的能源。还需由
`D_REALTIME_DISPATCH` 明确：若时隙开始安排了充电但本格实际供给不足，是取消
充电、改变电池动作，还是允许紧急电维持动作；实现者不得自行选。

## 7. 版本计划与结果审计接口

现有 `IntervalResult` 能保存每个已执行时隙的初始计划量、最终确认量和紧急量，
但无法表达 `100 -> 80 -> 100` 这种“最终净变化为 0、过程中有两笔交易”的情况。

低侵入候选是为每个 Q3 run 保存三个独立 sidecar：

```text
forecast_provenance.jsonl
plan_versions.jsonl
settlement_ledger.jsonl
```

`CaseResult.metadata` 只记录这些文件的相对路径和 SHA-256，不把主表内容塞进
metadata。另一方案是提升 `CaseResult/result_io` schema 并增加结构化字段。
两者都涉及 A 所有的 shared contract，需由 A 选择并 review；C 不应先写一套私有
结果格式。

计划快照最低字段：

```text
version_id / issue_time
target_slot_start / target_slot_end
committed_kwh
forecast_ids
```

结算账本在 `D_SETTLE` 批准后再增加：

```text
previous_committed_kwh / new_committed_kwh
delta_plus_kwh / delta_minus_kwh
transaction_price / cost_cny
```

## 8. 必须通过的手算验收样例

这些样例先作为审批材料；只有对应 decision 批准后才转成强制测试。

1. **可见性**：06:00 可见 00:00 与 06:00 发布的版本，不可见 12:00/18:00
   版本；`[06:00,06:10)` actual 在 06:00 不可见、06:10 可见。
2. **版本组合**：若两个可见版本的历史 MAE 为 1、2 kW，`epsilon=1`，则平方
   倒数原始权重为 `1/4、1/9`，归一化为 `9/13、4/13`。输入预测 100、200 kW
   时组合值为 `1700/13 = 130.7692 kW`。
3. **负载预测**：一周半衰期的 7/14/21/28 日权重必须为
   `8/15、4/15、2/15、1/15`，并且修改 decision time 之后的 actual 不改变
   当前预测。
4. **计划版本**：`P00=100, P06=110, P12=90, P18=95`，逐版 delta 为
   `+10,-20,+5`，最终确认量为 `95=100+10-20+5`；费用期望等待
   `D_SETTLE` 的完整账单批准。
5. **冻结**：06:00 追加版本时，已经结束的目标不能修改；允许调整的未来目标
   可以追加新版本，但旧版本记录不变。
6. **能量账本**：逐格验证平衡残差、SOC 转移、充放电互斥、PV 上限、无负购电
   和无隐式售电。
7. **跨午夜**：18:00 发布的 +6 h 节点必须是次日 00:00，+24 h 节点必须是
   次日 18:00；状态和 commitment key 均使用真实日期时间。

## 9. 性能门槛

正式结果区间为 334 天：

- 只在 00/06/12/18 做计划，共 1,336 次计划事件；
- 若每十分钟都解一次 144 格 MILP，则约有 48,096 次求解。

因此实现后不能直接启动全年。建议按以下顺序做计时和正确性验收：

```text
1 个合成窗口 -> 1 个真实日 -> 7 个真实日 -> 1 个月 -> 全年
```

每级记录单次/总耗时、不可行次数、MIP gap、峰值内存和 fallback 次数。若七日外推
已不能在剩余时间内完成全年，应先由团队调整求解频率、窗口或求解器，不能静默
降低约束或关闭验证。

## 10. 人工批准记录区

本区只供参赛队在实际讨论时复制到 decision；Agent 不填写确认字段。

```text
Q3-M1 版本组合：
Q3-M2 epsilon_kw：
Q3-M3 负载预测：
Q3-M4 horizon/tail：
Q3-M5 可调整时隙范围：
Q3-M6 结算手算账单：
Q3-M7 实时回放：
Q3-M8 终端/年度边界：
Q3-M9 二级目标：
Q3-M10 求解器/容差：

confirmed_by：
confirmed_at：
```
