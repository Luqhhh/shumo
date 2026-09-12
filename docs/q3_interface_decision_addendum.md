# Q3 已冻结接口澄清文本

2026-09-12，队长提出以下接口建议，C 成员明确回复“认可”，队长随后确认四项
可冻结。本文件记录该人工结论；受 `AGENTS.md` 约束，Agent 不修改既有 approved
decision，也不代填 `confirmed_by/confirmed_at`。尚未选择的 tail 算法仍由
`D_PV_TAIL_BASELINE=pending` 阻断。

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
- baseline 的具体历史算法另立小型 forecast decision，在其 machine 状态批准前
  不进入正式 Q3 runner。

发布时刻先一次性保存144点不可变 `PVForecastSnapshot`；中间 MPC 只切片，不重新
调用附件3 combiner/resampler。人工仍需明确 baseline 算法、冷启动、历史缺失和
非负处理；该 pending decision 已加入 Q3 与 final gate。

## 2. 实际短缺：charge curtailment recourse

实际回放只允许削减直至取消计划充电，不允许增加放电、把充电反转为放电，或
对整格电池动作重新求解。令本格正常可用能量为

```text
available = q_final + PV_actual + D_plan
C_exec = min(C_plan, max(available - Load_actual, 0))
q_emergency = max(Load_actual + C_exec - available, 0)
```

因此运行顺序固定为：先削减 `C_plan`，当 `C_exec=0` 后仍有真实负荷缺口时才
使用紧急购电。继续满足：

```text
q_emergency > 0 => C_exec=0, S_grid=0, S_PV=0
D_exec = D_plan
```

该机制称为 `charge curtailment recourse`，不能在代码或论文中泛称“任意实时
redispatch”。它不允许利用本格结束后才知道的 actual 反向优化已经执行的动作。

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
