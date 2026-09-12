# C 线 Q3 求解器开工检查表

审查起点：`feat/q3-forecast-control@7b90599` 与 `origin/main@bb95e16`。
本文件记录开工条件，不批准任何 decision，也不实现正式模型。

## 1. 当前状态

| 模块 | 状态 | 证据或代码 | 开工前动作 |
|---|---|---|---|
| 时间、电池、InfoSet | 已批准并有 contract tests | `problem/contracts.py` | 保持不变 |
| 附件3版本读取 | 已实现方法中立适配器 | `problem/q3_inputs.py` | 正式组合器只能接收可见版本 |
| 小时到十分钟重采样 | 已批准并实现 | `problem/q3_resampling.py` | 直接复用，不在 solver 重写 |
| PV 版本组合 | 仅证据层 | `audit_q3_forecast_versions.py` | 团队批准公式、epsilon、fallback 后实现生产模块 |
| 负载预测 | 仅证据层 | `audit_q3_load_forecast_candidates.py` | A 增加/批准 `D_LOAD_FORECAST` 并接入 gate |
| 统一 Q3 input bundle | 缺失 | 依赖 B 的实际数据适配器 | 等 B 修复整日缺失检查后复用，不另写全年读取器 |
| 计划版本与冻结 | 缺失 | 只有文档候选 | shared contract + 序列化 + 测试先行 |
| 结算账本 | 缺失 | `D_SETTLE` pending | 修正 B 草案冲突并人工批准 |
| 实际回放 | 缺失 | `D_REALTIME_DISPATCH` 未进机器配置 | 人工批准因果动作规则 |
| Q3 solver/runner | 未实现 | `problem/q3.py` 明确报错 | 上述 gate 全绿后实现 |
| result3.xlsx 导出 | 未批准/未实现 | 当前模板 decision 只覆盖 Q1 | 不阻塞 solver，但阻塞最终交付 |

## 2. P0 阻断项

1. `D_SETTLE` 与 `D_MODEL_Q3` 当前仍为 `pending`；`D_LOAD_FORECAST` 在当前
   `decisions.toml` 中不存在。
2. 团队必须决定：`D_MPC / D_TERMINAL / D_YEAR_BOUNDARY /
   D_REALTIME_DISPATCH` 是独立 decision，还是全部并入 `D_MODEL_Q3`。
3. Q3 case gate 必须覆盖实际使用的全部 decision，并新增“proposed 仍阻断”的
   临时配置测试。
4. 必须回答“每十分钟 MPC 与每六小时 24 h forecast 的尾部怎样衔接”；三种
   方案见 [`docs/q3_solver_preflight.md`](q3_solver_preflight.md) 第 4 节。
5. B 的 `d1df0b0` 不能原样合并：冻结规则会禁止 Q3 日内调整，PV 账本重复计算
   弃光，Q2 总量/增量混淆，整日缺失检查和交易价格索引仍未解决。
6. A 必须确认计划版本与预测 provenance 的正式承载方式。不能把逐版主数据
   临时塞进 `CaseResult.metadata`。

## 3. 已提前补强的绿色测试

本分支在不采用任何未批准模型口径的前提下，补充：

- 00/06/12/18 四个发布时间边界的可见性；
- actual 只在十分钟区间右端可见；
- actual 跨午夜的真实时间可见性；
- 附件3的跨午夜 `valid_time`；
- 重复 `issue_time + lead_hours` 拒绝；
- LIN 全部 24 个小时节点精确命中；
- LIN/PCHIP 确定性和输入不变性。

以下测试必须等 decision 批准和 shared contract 出现后再写，不使用 `xfail`
掩盖：

- 正式 `VERSION-B + WEIGHT-B` 组合 provenance 和 cold-start/fallback；
- 正式 `LOAD-A + LF-A` 记录、序列化和未来扰动不变性；
- P00/P06/P12/P18 不可变快照、逐版 delta 和已执行时隙冻结；
- 结算账单、紧急购电、合同电未利用与费用总计；
- 求解器可行解、独立残差检查、两日 SOC 连续和年末条件。

## 4. 队长回复后的最短实施路径

```text
1. 同步队长合并后的 main
2. 核对 machine decisions、confirmed 字段和 Q3 CASE_DECISIONS
3. 合并 A 的 ledger/provenance contract 与 B 的修正版 actual adapter/replay
4. 先写 contract tests
5. 实现生产 PV 组合器
6. 实现生产负载预测器
7. 实现 plan version / commitment ledger
8. 实现单窗口 Q3 MILP + 独立 validator
9. 接入十分钟回放、settlement 和 run artifacts
10. 按合成窗口、1日、7日、1月、全年逐级验证
```

## 5. 开工验收口令

只有以下项目全部成立，才把 `problem/q3.py` 从明确报错改成正式 runner：

- [ ] 当前分支已同步最新 main，工作区无不明修改；
- [ ] Q3 所需全部 decision 为 `approved` 且确认字段完整；
- [ ] Q3 gate 与实际依赖一致；
- [ ] horizon/tail、可调整目标范围、结算账单均有人工结论；
- [ ] shared ledger/provenance/replay contract 已合并并通过 review；
- [ ] 不含未来 actual 的 `Q3WindowInput` 接口已通过 contract test；
- [ ] 全量质量检查、synthetic smoke 和 Q3 gate 测试通过。

在队长回复前，C 线允许完成模型卡、证据、已批准 contract 的测试和接口审计；
不允许提前实现或运行未批准的正式 Q3 模型。
