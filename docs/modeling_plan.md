# Stage 1 建模计划（架构冻结版）

本文档冻结 Q1/Q2/Q3/Q4 共享的建模边界、信息时序、物理账本、职责与实施顺序。它不代替人工模型决策，也不自动改变任何 decision 的状态。

> 冻结 architecture / semantics 不等于批准 decision。尚未由参赛队核对的项目仍保持 `pending` 或 `proposed`；只有人工确认并填写 `confirmed_by` / `confirmed_at` 后才能进入 `approved`。

`configs/decisions.toml` 仍是机器可读状态的唯一来源。本文档中的“建议默认值”在 decision 批准前不得进入正式模型。

## 1. 冻结范围与变更规则

本轮后停止新增 shared architecture。后续按以下顺序推进：

```text
decision
  -> contract
  -> contract test
  -> implementation
  -> validation
```

只有下列情况可以重新打开 shared semantics：

1. 题面、附件或官方澄清提供了新证据；
2. contract test 证明当前规则自相矛盾或无法实现；
3. 正式数据审计发现时间、单位或信息可见性错位。

变更时必须记录证据、影响的 decision ID、使用的 case 和新增/修改的 contract tests。

## 2. 两个分类维度

每项规则同时记录“为什么这么规定”和“怎样保证它被遵守”。两者不是同一类标签。

| 维度 | 允许值 | 含义 |
|---|---|---|
| 依据类型 | `SOURCE_FACT` | 题面、附件或官方说明直接给出 |
| 依据类型 | `MODELING_ASSUMPTION` | 题目未完整规定，但模型必须统一的机制 |
| 依据类型 | `EMPIRICAL_CHOICE` | 通过滚动回测或敏感性实验选出的方法/参数 |
| 执行机制 | `INFORMATION_CONTRACT` | 限制决策时刻能看到的数据 |
| 执行机制 | `DOMAIN_CONTRACT` | 统一时间、单位、状态、行动、购电和费用语义 |
| 执行机制 | `VALIDATION` | 独立检查前述规则在运行中未被破坏 |

decision 至少记录 `kind`、`source`、`rationale`、`alternatives` 和 `sensitivity`。`approved` 只表示团队选择采用，不会把建模假设变成题目事实。

## 3. 唯一信息可见性规则

所有 actual、forecast、price 和训练记录统一使用：

```text
available_at <= decision_time
```

PV / Load / Price 使用相同的 `InfoItem` 结构：

```text
value
valid_time
available_at
source
```

训练数据不另设一套 `training_time < decision_time` 规则。每条训练记录同样必须满足 `available_at <= decision_time`；原始时间标签不能代替可获得时间。

### 十分钟时隙的事件顺序

令第 `k` 个时隙为 `[t, t+10min)`：

1. 在 `decision_time=t` 构造 `InfoSet`；
2. 只使用 `available_at <= t` 的信息决定本时隙电池动作；
3. 基线模型不允许在这 10 分钟内再次重优化；
4. 时隙内真实 Load / PV 实现；
5. 在 `t+10min` 获得完整区间量，核算本时隙的紧急平衡电量和状态转移；
6. 状态推进至 `t+10min`，再决定下一时隙。

紧急购电在计算上由区间实现后的缺口核算，物理上代表外网在该区间内提供的实时平衡电量，不表示区间结束后才回头补电。

更细粒度的时隙内控制只能作为扩展实验，不属于 shared semantics。

## 4. 计划、回放与物理账本

### 4.1 计划阶段

计划器只使用当时可见的预测和状态，产生合同购电计划及预计电池动作。一个可审批的候选账本为：

```text
G_contract + PV_hat_use + D_plan
  = Load_hat + C_plan + S_grid_plan
```

正常购电费用按 `G_contract` 结算，不按事后实际利用量结算。Q3 保留 00/06/12/18 四版计划及每次 delta；已执行时隙不可修改。

### 4.2 实际运行回放

建议的主账本为：

```text
G_contract + PV_use + D + E_emergency
  = Load + C + S_grid

PV = PV_use + S_PV
G_use = G_contract - S_grid
```

各量含义：

- `G_contract`：已承诺并按计划量付费的合同电；
- `S_grid >= 0`：已购买、已付费但未利用的合同电；
- `G_use`：派生量，不得与 `S_grid` 脱离独立优化；
- `PV_use`：实际利用的光伏电量；
- `S_PV >= 0`：弃光；
- `E_emergency >= 0`：正常资源和因果电池响应后仍存在的本时隙残余短缺。

`S_grid` 的现实含义是 `MODELING_ASSUMPTION`，不是题目直接给出的 `SOURCE_FACT`。在 `D_SETTLE` 批准前，必须由参赛队确认“合同电可不利用但仍按计划付费”的解释。

### 4.3 紧急购电是 residual slack

`E_emergency` 不是可以自由套利的普通能源。运行顺序是：合同电 + 光伏 + 正常电池响应仍不足时，再用紧急购电填补当前负载缺口。

必须满足：

```text
E_emergency > 0  =>  C = 0
E_emergency > 0  =>  S_grid = 0
```

即不得同时紧急购电与电池充电，也不得在浪费合同电的同时紧急购电。实现可以使用残余缺口定义或经批准的互斥约束，但不得仅依赖求解器“大概不会这么做”。

## 5. 单位与电池语义

内部优化统一使用每个 10 分钟时隙的电量 `kWh`。附件中的功率 `kW` 只在输入边界转换：

```text
energy_kwh = power_kw * (1/6)
```

电池行动 `C` / `D` 是母线侧区间电量，状态 `E` 是电池内部储能：

```text
E_next = E + eta_c * C - D / eta_d
```

必须继续遵守：

- `1200 <= E <= 10800 kWh`；
- `0 <= C,D <= 5000*(1/6) kWh/slot`；
- 同一时隙不得同时充放电；
- 购电、紧急购电、弃光与合同电未利用量均非负；
- 不允许通过负购电隐式向外网售电。

当前 `configs/decisions.toml` 中 `D_EFF` 已是 `approved`，口径为 `eta_c=eta_d=0.9`，因此往返效率为 81%。若参赛队并未真正核对该口径，应由参赛队重新打开 decision；代码不得一边保留 `approved` 一边实现其他效率语义。

## 6. 需要人工确认的 decision 边界

### 6.1 `D_LOAD_FORECAST`（建议新增）

题目提供全年实际负载，但未提供未来负载预测。Q2/Q3 不得把当天未来实际负载当作计划输入。

本 decision 至少需要批准：

- 0:00 如何生成当天负载预测；
- 6:00 / 12:00 / 18:00 是否使用截至当时的真实负载重新校正剩余预测；
- 一月初的 cold start 和历史窗口；
- rolling-origin 训练、调参和评价规则。

日内更新负载预测属于 `MODELING_ASSUMPTION`；题目明确的 00/06/12/18 发布安排只适用于附件 3 光伏预测。

候选基线可包含前一天同时刻、上周同日同时刻、同星期类型的滚动加权均值。采用哪个方法属于 `EMPIRICAL_CHOICE`，必须由滚动回测证据支持。

### 6.2 `D_REALTIME_DISPATCH`（建议新增）

本 decision 只回答实际运行时物理上如何调电池，不回答费用怎样结算。它与 `D_SETTLE` 分开：

- 基线实时控制步长为 10 分钟；
- 本时隙电池动作只用时隙左端已可见的信息；
- 不使用本时隙或后续时隙的完整真实曲线；
- B 的 actual replay 与 C 的 forecast adjustment 必须调用同一套语义。

### 6.3 `D_INFO`（保留总 decision）

`D_INFO` 保留一个总 decision，内部分为 PV / Load / Price 三类同构可见性表。Q4 必须人工确认未来价格是否提前可见：

- 若附件 4 是事后实时价格，计划时不得直接使用未来真值，应新增 `D_PRICE_FORECAST`；
- 若团队将其解释为日前公布的动态价格，可以在对应时刻可见，但必须把这一解释写进 decision。

当前 `D_INFO` 虽已是 `approved`，但本轮计划新增了统一信息结构和时隙事件顺序。参赛队需要判断是否修订该 decision；在此之前实现仍以现有机器可读 choice 为准。

### 6.4 `D_STATE`

储能不每日重置，但一月处理仍需参赛队重新核对：

- 从 1 月 1 日 0:00、6000 kWh 开始因果模拟一月，作为 warm-up / 历史积累阶段；或
- 明确声明一月只用于训练，并在 2 月 1 日重新设置初始储能。

不应默认“一月待机一个月”而不标注建模假设。当前 `D_STATE` 已批准的 choice 确实是“一月待机”；如果团队采用新口径，必须由参赛队正式修订并重新确认 `D_STATE`。

### 6.5 `D_SETTLE`

本 decision 只管经济结算，至少要确认：

- 正常费用按 `G_contract` 还是其他量计算；
- `S_grid` 是否仍按合同计划付费；
- 紧急购电的5倍基准价格和对应时刻；
- Q3 每次调整与 0:00 原计划比较，还是与上一版已确认计划比较；
- 取消、追加、紧急购电和合同电未利用量如何分项记账。

### 6.6 `D_RESAMPLE`

附件 3 的整点光伏预报到 10 分钟控制输入的转换仍由 C 整理证据。分段常数、线性插值或历史日内形状都只是候选方案；正式采用方案必须由 rolling-origin 评价与下游费用/紧急购电指标支持。

## 7. 五个人工口径

正式修订 machine-readable decisions 前，三人共同回答：

1. 一月因果 warm-up，还是 2 月 1 日重置？
2. 6:00 / 12:00 / 18:00 是否重新估计剩余负载？
3. `S_grid` 的现实含义是什么，合同电是否可不利用但仍按计划付费？
4. Q4 决策时是否知道未来价格？如果不知道，怎样因果预测？
5. Q3 的调整结算相对 0:00 原计划，还是相对上一版已确认计划？

C 线主责的第 2、4、5 项候选口径、题面证据和表决模板见 [`docs/c_decision_cards.md`](c_decision_cards.md)。

五项结论必须标明 `SOURCE_FACT` / `MODELING_ASSUMPTION` / `EMPIRICAL_CHOICE`，并由参赛队修改 `configs/decisions.toml`。Agent 可以整理选项和证据，不得代填批准信息。

## 8. 因果预测与 provenance

所有正式预测记录至少保留：

```text
forecast_id
decision_time
valid_time
available_at
training_cutoff
training_window
model_version
data_version
value
unit
```

PV forecast 另保留官方 `issue_time`、`lead_hours` 和原始来源定位。负载/价格预测的 `training_cutoff` 必须能回溯到实际可用记录，不只保存一个最终预测数字。

Q3 计划建议使用长表保存：

```text
issue_time
target_slot
previous_committed_kwh
new_committed_kwh
delta_kwh
is_frozen
```

这使 `D_SETTLE` 的比较基准确定后可以重算费用，而不用推翻计划器。

## 9. 优化目标与稳定解

正式方法仍需各 `D_MODEL_*` 人工批准。建议模型卡使用真正的分阶段求解，不用一个随意的小权重把不同目标混在经济成本中。

第一阶段：

```text
C_star = min C_economic
```

后续阶段加入带绝对/相对数值容差的经济成本约束：

```text
C_economic <= C_star + epsilon_abs + epsilon_rel * abs(C_star)
```

然后按经批准的 lexicographic 顺序依次最小化：

```text
合同电未利用量 -> 弃光 -> 电池吞吐量
```

这些是同经济成本解之间的 tie-breaker，必须与题目费用分开报告。

## 10. 连续状态、滚动时域与年末边界

共享方向为：

- 储能跨日连续，不每日重置；
- 基线 look-ahead 使用当前正式可获得的 24 h 预测；
- commit horizon 只包含当前允许提交/调整的时隙；
- 终端价值只用于缓解滚动窗口短视，数值必须有基准或敏感性实验支持；
- `E_year_end = 6000 kWh` 若被团队批准，应明确称为“年度评价边界条件”，不得写成题目明文规定的日常运行规则。

年末约束由两部分共同组成，不可相互替代：

```text
E_year_end = 6000
E_t in R_t
```

前者保证最终真正达到 6000 kWh；后者是临近年末的 backward reachable condition，保证先前决策没有使终点变得不可达。`R_t` 至少考虑剩余时间、最大充放电功率和 SOC 范围；如果用于严格可行性保证，还需考虑禁止售电和可用的负载/光伏信息。

## 11. Contract tests 与 validation

正式实现前至少覆盖：

### 时间与信息

- 144 个时隙与 145 个状态边界；
- 原始右端点标签到内部时隙的映射；
- 跨午夜的原始单元格 -> 内部 slot -> 导出单元格审计；
- `available_at <= decision_time`；
- 未发布 forecast 不可见；
- 右端点 actual 不得被用于其所在时隙的左端决策；
- 训练记录的 `available_at` 不得越过预测决策时刻。

### 物理与单位

- `kW * 1/6 -> kWh`；
- 充放电状态转移与不越界；
- 充放电互斥；
- 日内及跨日状态连续；
- 无负购电、无隐式售电；
- 每个时隙能量平衡残差在数值容差内；
- `PV_use + S_PV = PV`；
- `G_use + S_grid = G_contract`；
- 紧急购电不与充电或合同电未利用同时发生。

### 计划、结算与可追溯性

- 已执行计划时隙不可修改；
- 00/06/12/18 版本计划与 delta 可完整对账；
- 费用分解和总计一致；
- forecast provenance 完整且可回溯；
- rolling-origin 训练/调参不使用未来数据；
- 年末最终状态和提前可达性均满足已批准口径。

## 12. 优化器、回放器与导出层边界

- 计划/调整优化器只获取经 `InfoSet` 过滤的输入；
- actual replay 才使用真实 Load / PV 实现量核算状态和 residual emergency energy；
- 优化器和回放器使用统一 domain result，不直接操作 Excel；
- `excel_export.py` 只负责已批准的模板映射，仍受 `D_TIME_TEMPLATE_EXPORT` 独立门槛控制；
- 任何 `is_synthetic=true` 的运行不得成为正式结果。

## 13. Ownership 和下一阶段交付

### A：shared core / 集成

- 保持 decision 配置和 case dependency 为唯一机器可读真相；
- 在人工批准后落地时间、电池、InfoSet、购电账本和验证 contract；
- 审核 shared files 及最终集成。

### B：Q2 / Q4-2 actual replay

- 实现经批准的 `D_REALTIME_DISPATCH` 回放语义；
- 核算合同电未利用量、紧急平衡电量和实际费用；
- 与 C 共同完成 `D_SETTLE`；
- 审核共享负载预测在 Q2 中的信息边界。

### C：Q3 / Q4-3 forecast-aware control

- 主责 `D_LOAD_FORECAST` 候选方法、rolling-origin 回测与 provenance；
- 主责 `D_RESAMPLE` 证据和预报时间映射；
- 准备 `D_MODEL_Q3` / `D_MODEL_Q4_3` 模型卡；
- 与 B 共同完成 `D_SETTLE`；
- 保存 00/06/12/18 各版计划、delta 及完整预测来源。

## 14. 实施 gate

架构冻结后，仍必须依次通过：

1. 三人对第 7 节五个口径作出人工结论；
2. A 按团队结论更新 `configs/decisions.toml` 和 case dependencies；
3. 对新增/修订的 decision 填写真实 `confirmed_by` / `confirmed_at`；
4. A 先落地 shared contracts，至少一名 B/C 成员 review；
5. 先写 contract tests，再进入 Q2/Q3/Q4 正式实现；
6. 所有实现通过因果性、物理账本、费用、provenance 和完整运行 validation；
7. `D_TIME_TEMPLATE_EXPORT` 批准前不导出正式 `result*.xlsx`。

在上述 gate 完成前，B/C 可以整理证据、写模型卡、接入已批准接口和使用合成数据测试，不得运行未批准的正式模型。
