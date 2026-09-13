# Q3 已冻结接口澄清文本

2026-09-12，队长提出以下接口建议，C 成员明确回复“认可”，队长随后确认四项
可冻结。本文件记录该人工结论。后续 C-6 已于2026-09-13由队长单独确认：
`D_PV_TAIL_BASELINE` 采用 `TAIL-EXP2`，精确算法和证据以 machine decision 与
`records/evidence/q3_pv_tail_baseline_summary.json` 为准。

## 1. 固定 24 h 窗口：HORIZON-B

为保持已批准的“每十分钟重算、固定 24 h MPC”，采用严格因果的 PV tail
baseline 补齐最新附件3 snapshot 无法覆盖的窗口尾部：

- 最新可见附件3预测覆盖的 `valid_time` 永远优先；
- baseline 只补没有附件3覆盖的尾部，不覆盖、不平均附件3已有节点；
- baseline 不得读取 `available_at > decision_time` 的数据；
- 每个补尾点保存 `forecast_id/decision_time/valid_time/value_kw/model_version/
  training_cutoff/source_refs`，并固定
  `fallback_reason="attachment3_horizon_exhausted"`；
- 不得把补尾值伪装成附件3发布值；缺少获批 baseline 时显式阻断；
- baseline 的具体历史算法另立小型 forecast decision；该算法现已由 C-6 单独
  批准为 `TAIL-EXP2`。

发布时刻先一次性保存144点不可变 `PVForecastSnapshot`；中间 MPC 只切片，不重新
调用附件3 combiner/resampler。获批 baseline 缺少任一日滞后时显式失败，不静默
切换算法；`D_PV_TAIL_BASELINE` 继续保留在 Q3 与 final gate 中防止状态回退。

## 2. 实际偏差：DISCHARGE-CURTAIL-PV-FIRST

2026-09-13真实单日审计证明固定计划放电在负荷预测偏高时可能造成无法吸收的
供给。C成员随后明确确认 `DISCHARGE-CURTAIL-PV-FIRST`。实际回放只允许向下
削减计划充电或计划放电，不允许增加任一动作、反转方向或重新求解。

供给不足时：

```text
available = q_final + PV_actual + D_plan
C_exec = min(C_plan, max(available - Load_actual, 0))
q_emergency = max(Load_actual + C_exec - available, 0)
```

先削减 `C_plan`，当 `C_exec=0` 后仍有真实负荷缺口时才使用紧急购电。

供给过剩时按以下顺序消除 surplus：

```text
1. S_grid = min(q_final, surplus)
2. D_curtail = min(D_plan, surplus - S_grid)
3. D_exec = D_plan - D_curtail
4. 剩余部分才记为 S_PV
```

始终满足 `0<=C_exec<=C_plan`、`0<=D_exec<=D_plan`，且
`q_emergency>0 => C_exec=0, S_grid=0, S_PV=0`。该机制仍是受限的最小响应，
不能泛称“任意实时redispatch”；它只保证真实区间的物理可行，不利用未来 actual
优化后续动作。执行后的真实SOC传给下一次10分钟MPC。

## 3. 一对多审计记录：结构化 sidecar

保持全局 `CaseResult/IntervalResult` schema v1 不变：

```text
case_result.json              最终执行轨迹
forecast_provenance.jsonl     负载预测与PV版本组合来源
plan_versions.jsonl           P00/P06/P12/P18的不可变逐slot快照
settlement_ledger.jsonl       计划、逐版调整及后续实际结算事件
```

- `CaseResult.intervals` 只保存最终执行轨迹；
- `CaseResult.result_files` 纳入三份 sidecar；
- `metadata/manifest` 只保存相对路径、schema version、SHA-256 等索引信息；
- 不把整张预测或交易账本塞入 `metadata`；
- sidecar 写入必须拒绝静默覆盖，并通过 JSONL 往返、行数和哈希测试。

当前 `problem/q3_sidecars.py` 已实现精确字段、完整计划版本链、预测ID引用检查和
计划/调整事件。紧急结算事件由后续实际回放提供，不能由 sidecar writer 猜测。

## 4. B actual adapter 的选择性整合

只从 `origin/codex/b-q2-input-adapter@ba02252` 取：

```text
src/microgrid/problem/q2_inputs.py
tests/test_q2_inputs.py
```

不 merge B 分支，不 cherry-pick 其整条提交历史，不带入旧 `decisions.toml`。
该 adapter 必须继续验证每天144格完整、指定 `expected_days` 不缺不多、负载/PV
网格一致、右端点对齐和输入哈希。

## 5. C-7 负载版本：LOAD-HORIZON-A

2026-09-13 队长单独确认：Q3 的 LOAD-A 仍只在00/06/12/18发布，每版一次性冻结
未来30小时（180个十分钟点）；中间10分钟 MPC 只从最近已发布版本切取未来24小时
（144点），不得重新运行负载预测。30小时只用于覆盖两个发布时间之间最长5小时
50分钟的窗口缺口，不改变Q2更新机制。下一发布时间到达后必须换用新版本；实现
不得读取未来 forecast version 或实际负载。
