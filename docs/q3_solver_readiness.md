# C 线 Q3 求解器开工检查表

审核分支：`audit/q3-handoff-20260913`；交接源为 `feat/q3-forecast-control@584b66c`。
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
| 结算账本 | 计划/调整/紧急事件已实现，非零微小调整不再漏写 | `q3_plan_ledger.py`、`q3_sidecars.py` | 逐笔与汇总对账，不按物理容差核销费用 |
| 结构化 sidecar/CaseResult | 一日/跨日artifact封装已实现 | `q3_sidecars.py`、`q3_artifacts.py` | 完成 |
| Q3 gate | tail decision 已批准 | `D_PV_TAIL_BASELINE=approved` | 保留 gate 防回退 |
| 实际回放 | DISCHARGE-CURTAIL-PV-FIRST已接入 | `q3_replay.py`、`q3_controller.py` | 完整2月通过；年末charge curtailment已独立复算 |
| 单窗口 Q3 MILP | 已实现并独立验证 | `problem/q3_solver.py` | 接入滚动控制器 |
| Forecast release builder | 已实现因果InfoSet生成 | `problem/q3_release_snapshots.py` | runner提供实际/预报archive |
| Forecast window factory | 已接不可变真实domain snapshot | `problem/q3_window_factory.py` | 完成 |
| Q3 滚动 driver | 一日/跨日连续版已实现 | `problem/q3_rolling.py` | 接正式runner |
| 窗口失败上下文 | 已实现并验证失败文件留存 | `q3_rolling.py`、`tests/test_q3_rolling.py` | 本地复现定位到12月31日22:10、slot 133 |
| Q3 年末储备 | 用户明确批准限定最后24小时所有状态边界SOC>=6000 | `q3_terminal_reserve.py`、`q3_solver.py` | 不扩大范围；仍独立检查实际年度等式 |
| Q3 年度 runner | 334天、48096格成功，实际终点6000；原轨迹保全 | `problem/q3.py`、审计文档第6—7节 | 不把原诊断直接选入；正式交付记录来源 |
| result3.xlsx 导出 | 用户单独批准Q3专用范围，已导出并核验122785格 | `q3_export.py`、`docs/q3_delivery.md` | 完成；原Q1快照不变，原模板不变 |
| Q3论文同源表图 | 四展示日、全年费用、交易统计、CSV及6页独立PDF已生成 | `q3_reporting.py`、`paper/sections/07_q3.tex` | 完成；不声称未验证的反事实节费 |

## 2. 剩余的 P0 阻断

Q3技术P0已清零：用户进一步明确批准限定模板映射及新正式来源后，完整导出、
独立读回与同源论文PDF均完成。剩余为代码/AI人工复核和团队集成，不自动合并；
不据此解除其他case或全局final阻断。以下保留此前问题与批准的历史依据。

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

2026-09-13真实附件单日审计曾在05:40暴露固定计划放电不可行。C成员确认
`DISCHARGE-CURTAIL-PV-FIRST` 后，真实1日完整通过；随后真实连续7日也完整通过，
跨午夜SOC连续。详见 `docs/q3_real_data_audit.md`。7日审计另发现HiGHS默认整数
容差经833.33 kWh big-M放大后会产生0.000222 kWh反向放电尾数；生产求解器只收紧
HiGHS整数可行性容差，不放宽共享物理验证阈值。

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
- 一日滚动driver严格按144格执行“先构造预测窗口并求解、后传入当格actual回放”，
  保留四版计划、冻结格的原预测引用、紧急账本、连续SOC和统一IntervalResult。

上述能力均已有合成 contract tests。真实1日、7日和完整2月审计均已通过；首次全年
诊断在 `rolling_solve` 阶段因窗口MILP不可行而失败，且现有异常证据未带出具体
decision time。失败 manifest、failure.json 和日志已经保留。不得使用 `xfail`、放宽
物理阈值或更改已批准口径来掩盖该问题。

接手后已为`run_q3_day`的`Q3WindowSolveError`增加`decision_time/day/slot/soc_kwh/
terminal_mode/window_length`，保留原异常类型及原因链，并验证现有runner会将全部
上下文写入failure和manifest的error message。该补丁仅涉及异常包装，MILP、预测、
实际回放、decision和物理/整数容差均未改变。质量检查为ruff lint/format通过、
`265 passed, 1 skipped`；新增测试覆盖普通144格与年末1格失败窗口。
另以`MICROGRID_RUN_SOLVER_TESTS=1 uv run --locked pytest -q`启用可选Q1 MILP测试，
全量`266 passed`，无跳过。

原失败ZIP、manifest、failure与日志均已按原始字节保全在本地忽略目录，哈希及
跨设备输入/配置核对结果见`docs/q3_real_data_audit.md`第5.1节。完整2月原诊断没有
独立月度run目录；已记录的汇总数值不能冒充逐格artifacts。

本地从2月1日SOC=6000连续复现，在12月31日22:10（slot 133）返回不可行；当前SOC
为2657.079008047366 kWh，剩11格为18:00版本的冻结合同，年度6000硬等式及残值0
设置正确。22:00实际负载高于预测，获批recourse削减充电，真实格末SOC低于上一
窗口预测。独立重建窗口、重复MILP及单格回放全部复核，剩余格可充电量上界证明
年末SOC至多5957.055995045398 kWh，必然达不到6000；详见审计文档第5.2节。

因此当时P0为年末合同冻结、预测等式与受限实际响应之间的递归可达性问题，而非输入
缺失或该窗口HiGHS误报。须将证据交团队讨论；不得自行新增储能储备、额外交易、
紧急充电、放宽年度等式或修改其他获批语义。原Windows失败时刻仍未知，不倒填原
记录。本地复现的失败文件、窗口/ledger/前格解、可达性证书和日志完整保留，均未
登记为正式结果，亦未伪造缺失的成功sidecar。

后续补充已整理为[`Q3年末储备补充口径`](q3_terminal_reserve_proposal.md)：
仅在最后24小时所有状态边界增加SOC>=6000，保留年度等式及其他获批口径。
本会话用户已对限定范围明确回复“批准”；窗口约束、独立验证、回放失败留证和
artifacts独立复核已接入。该下界仍不保证实际跨入储备期或实际终点等式，必须
连续全年诊断与独立复核，不能把旧失败证据当成新口径的验证结果。
补充实现后ruff lint/format通过，启用可选solver测试的全量pytest为`280 passed`，
新增14项检查覆盖储备边界、跨日前瞻、初始状态、年度等式、实际失败留证和写出前复核。

批准后，以`6e5ca4e`从2月1日SOC=6000完整执行生产年度链，未重置或跳日。
`q3-terminal-reserve-validation-20260913`完成334天、48096格，实际年末SOC=6000，
最后一天145个真实状态边界最低6000；全程SOC连续。该诊断不是储备规则对任意
输入必然成功的证明。

逐笔审计发现原结算sidecar漏写2455笔已计费的非零微小调整，共
`0.00006788339322532307`元；原计划账本、汇总费用和执行轨迹正确。
`94f04c5`仅修复写出过滤，所有非零调整均保留，不改变模型、容差或费用公式。
新目录`q3-terminal-reserve-evidence-20260913`使用完整持久化版本链重建结算证据，
两份预测/计划sidecar保持原字节，动作与SOC保持原值；manifest明确标注
`artifact_regeneration_only=true`和源轨迹提交。这不是第二次全年MPC运行。
原目录及首次独立对账失败日志完整保留；最终独立复核及哈希见审计文档第6节。
该阶段全量质量检查为ruff lint/format通过、`289 passed`（启用可选solver测试，无跳过）。
随后单独批准Q3导出/新正式来源，`q3-formal-delivery-20260913`已生成result3.xlsx、
validation、完整来源/48096格映射、三份sidecar及同源表图。122785个单元格读回成功，
正式Q3检查阻断项为[]，Q1原快照未变。最新质量检查为303 passed、ruff通过；
Q3独立论文6页编译成功。详细文件哈希及复现见`docs/q3_delivery.md`。

## 4. 最短实施路径

```text
1. 真实1日、7日和完整2月审计已通过
2. 窗口求解失败上下文已补充且通过失败文件留存测试
3. 年末缺口已定位；用户明确批准限定储备，完整真实全年已执行成功
4. 微小调整写出问题已修复，来源明确的派生证据提交独立审计与团队审核
5. 用户已单独批准Q3模板与新正式来源，导出及同源表图/PDF完成；人工PR复核待进行
6. 不自动合并、不批准其他case或全局final，原诊断及失败证据不改
```

## 5. 正式 runner 实现验收

以下项目均已成立，因此 `problem/q3.py` 已由明确报错的占位实现升级为正式
runner；这不代表正式run选择或结果导出已经批准：

- [x] 当前分支已同步队长批准 Q3 decision 的 main；
- [x] `D_LOAD_FORECAST/D_SETTLE/D_MODEL_Q3` approved 且确认字段完整；
- [x] Q3 gate 与已批准 decision 一致；
- [x] PV/负载候选有可复现的真实数据证据；
- [x] PV/负载生产预测器及因果、缺失历史、provenance 测试通过；
- [x] 计划版本、冻结和逐版调整费用的内存 contract 与手算测试通过；
- [x] HORIZON-B snapshot/window 接口已冻结并通过切片、补尾范围和禁止重采样测试；
- [x] DISCHARGE-CURTAIL-PV-FIRST 已由C成员确认并实现公式级测试；
- [x] `D_PV_TAIL_BASELINE` 已批准为 `TAIL-EXP2` 并有生产实现与证据；
- [x] LOAD-HORIZON-A 已由队长确认并实现30 h版本/24 h切片 contract；
- [x] shared ledger/provenance 采用结构化 sidecar；
- [x] 修正版年度 actual adapter 已选择性整合；
- [x] 三份 sidecar 的精确结构、预测引用、往返、拒绝覆盖和哈希测试通过；
- [x] 不含未来 actual 的 `Q3WindowInput` 已通过对齐、版本、ledger和年末contract test；
- [x] 单窗口 Q3 MILP 与独立 validator 已通过1格和完整144格合成测试；
- [x] 单步计划版本更新、第一格回放和紧急结算 sidecar 已通过合成测试；
- [x] `ATTR-PV-FIRST` 与计划放电向下削减顺序已由C成员确认；
- [x] 合成一日滚动driver覆盖144格、四版计划、冻结引用和紧急结算；
- [x] window factory只选择最近已发布的不可变snapshot，并保留tail provenance；
- [x] release builder先过滤InfoSet，再生成LOAD-A与组合/线性重采样PV snapshot；
- [x] 一日结果可写三份带哈希sidecar及全局schema v1 CaseResult；
- [x] 跨日driver每天重建合同账本，但SOC从前日末连续传入次日初；
- [x] 跨日结果统一写入三份sidecar和一个schema v1 CaseResult；
- [x] runtime input总线校验附件1/2/3全年网格、四次发布和来源哈希；
- [x] 全量质量检查、synthetic smoke 和 Q3 gate 测试通过。

正式runner已实现，原始附件及哈希已经就位，真实1日、连续7日和完整2月审计已经
通过。首次全年失败及本地不可达证据继续保留；经明确批准限定储备后，新全年真实
运行已成功，修正结算证据另目录派生并可复核。用户后来明确批准Q3专用模板映射及
新正式交付来源，正式目录、独立validation/逐格读回和同源论文PDF已完成。
原诊断不直接选入或改写；代码/AI人工审核仍pending，全局final及Q4范围未获本次批准。
