# Q3 真实数据审计记录

审计日期：2026-09-13。该记录只保存可复核证据，不批准或修改建模口径。

## 1. 输入与来源

通过仓库导入器从 `CUMCM2026Problems.zip` 只导入 C 题文件。绝对源路径不写入
manifest。导入后 `records/inputs_manifest.json` 的冲突数为0，Q3所需附件哈希为：

- 附件1：`66b87134f5ecccd68184d3539bb1293ef039f9e0fdd955a589b9bfa7f227c377`
- 附件2：`2e95fd446bfafa0d8c59577b5c2e2ea8b3f1def20dde54a3062556f4da9b4c72`
- 附件3：`8a61b06c52bd0d639a1cc37c61a7d9f5b75edcbca718f64c1bd3498ec9f9d843`

生产 loader 校验结果：固定电价144格，实际数据365天、52,560格，附件3共有
1,460个预测版本，原始因果信息记录140,160条。输入 provenance 检查无问题。

## 2. 真实单日审计

审计日为2025-02-01，初始储能为6000 kWh。预测 snapshot 成功生成4版，随后按
每10分钟一次的既定流程求解和回放。该诊断不登记为正式run，也不强制当天末储能
回到6000 kWh。

运行在2025-02-01 05:40（slot 34）显式失败：

| 量 | kWh |
|---|---:|
| 计划放电 | 559.900448 |
| 合同购电 | 0.000000 |
| 实际负荷 | 531.227400 |
| 实际光伏 | 0.046233 |
| 无法吸收的剩余供给 | 28.673048 |

本格中，合同电已经为0，实际PV也几乎为0，所以该剩余供给不能通过
`ATTR-PV-FIRST` 的合同电未利用或弃光账本消除。当前冻结规则又要求实际放电严格
等于计划放电，不允许向下调整，因此回放正确地判为不可行。

## 3. 人工确认与修复

该失败不是求解器返回错误，也不是输入缺失。它暴露的是实时回放口径边界：是否
允许在真实负荷低于预测时，仅向下削减计划放电，但仍禁止增加放电、充放电反转和
重新优化。

C成员于2026-09-13明确确认 `DISCHARGE-CURTAIL-PV-FIRST`：实际过供依次通过
合同电未利用、向下削减计划放电、最后弃光处理。执行动作满足
`0<=D_exec<=D_plan`；仍禁止增加放电、反转方向或重新优化。

## 4. 修复后分段审计

同一真实单日重跑结果：

| 项目 | 结果 |
|---|---:|
| 日期 | 2025-02-01 |
| 区间数 | 144 |
| 计划版本数 | 4 |
| 紧急购电事件数 | 19 |
| 初始/期末储能 | 6000.000000 / 10765.735278 kWh |
| 诊断总费用 | 26112.095838 元 |
| 运行时间 | 10.298 秒 |

真实连续7日（2025-02-01至2025-02-07）重跑结果：

| 项目 | 结果 |
|---|---:|
| 区间数 | 1008 |
| 计划版本数 | 28 |
| 紧急购电事件数 | 111 |
| 初始/期末储能 | 6000.000000 / 10786.221200 kWh |
| 跨午夜SOC连续 | 是 |
| 诊断总费用 | 343789.609364 元 |
| 运行时间 | 48.910 秒 |

7日首次运行在2025-02-04 17:00发现HiGHS默认整数可行性容差产生数值尾数：
充电10.1612675 kWh时伴随0.000222052 kWh放电。该量来自二进制变量默认
`1e-6`整数容差经833.33 kWh big-M放大，不是实质性物理调度。生产求解器将HiGHS
的 `mip_feasibility_tolerance` 收紧至 `1e-9`，共享物理验证阈值仍保持不变；随后
7日运行完整通过。

真实完整2月（2025-02-01至2025-02-28）重跑结果：

| 项目 | 结果 |
|---|---:|
| 区间数 | 4032 |
| 计划版本数 | 112 |
| 紧急购电事件数 | 932 |
| PV tail调用次数 | 3920 |
| 初始/期末储能 | 6000.000000 / 10745.301616 kWh |
| 跨午夜SOC连续 | 是 |
| 计划购电费用 | 1179058.626424 元 |
| 调整费用 | 14108.319007 元 |
| 紧急购电费用 | 85994.418760 元 |
| 诊断总费用 | 1279161.364191 元 |
| 运行时间 | 193.986 秒 |

完整2月验证覆盖28个连续自然日；它仍是分段诊断，不施加年度最终6000 kWh边界，
也不登记为正式run。该结果用于证明长于7日的SOC传递、计划版本、tail补齐和实际
回放能够连续执行，不能替代全年终点验证。

## 5. 全年首次审计失败证据

首次全年诊断使用 run ID `q3-full-validation-20260913`，代码提交为 `12eaf8f`，从
2026-09-13 12:04运行至13:03左右。输入 provenance 校验通过，但在
`rolling_solve` 阶段失败：

| 项目 | 记录 |
|---|---|
| 状态 | failed |
| failure stage | rolling_solve |
| error type | Q3WindowSolveError |
| solver status | HiGHS status 8 / infeasible |
| 是否合成数据 | 否 |
| 已生成文件 | `manifest.json`、`failure.json`、运行日志 |
| 未生成文件 | `summary.json`、`domain_result.json`、三份sidecar |

当前异常包装只记录了窗口MILP不可行，没有带出 `decision_time/day/slot`，且 period
在异常前没有写出中间 checkpoint。因此运行时长不能作为失败日期的可靠证据，也不能
据此判断是年末边界、合同冻结还是其他实现问题。接手人应先让 solver 异常携带决策
时刻、当前SOC、窗口终端模式和剩余格数，再复现定位；在证据明确前不得改变已批准
口径、放宽约束或把该失败run登记为正式结果。

### 5.1 跨设备证据保全与诊断补强

已接收并解压 `q3-c-handoff-20260913-failed-v2.zip`，全部文件保存在本地忽略目录
`outputs/evidence/q3-failed-handoff-v2/q3-c-handoff-20260913-failed/`，不提交Git。
以下SHA-256按原始文件字节计算，未转换Windows换行或重写原失败文件：

| 文件 | SHA-256 |
|---|---|
| 证据包ZIP | `1a36180b09038fa73a5366c330ce5cfcc9ebb384122be5d5b1c19bf7062363b9` |
| `run/failure.json` | `98f0e50bb552d96d2256e6870d20622035b85e3406df29b6577d21ff1c9719c2` |
| `run/manifest.json` | `3a212fa90161458725bc7cfa0762dd66e177a800637b02e8589b8686a800c0f3` |
| `logs/q3-full-validation-20260913.log` | `2cbea069f50a97ae75f2a831451112f94844c15aacb8509c6cf12202341f7100` |

本地附件1/2/3与原manifest的输入哈希一致，完整config snapshot也一致。
NumPy、SciPy等主要依赖版本与原运行一致；操作系统及Python补丁版本不同
（原Windows/Python 3.11.4，本地Linux/Python 3.11.9），复现轨迹不预先宣称逐位相同。

`run_q3_day`现在仅在`Q3WindowSolveError`处包装`decision_time/day/slot/soc_kwh/
terminal_mode/window_length`，保留原异常类型、退出码和原因链。现有runner会将完整
错误文本写入`failure.json`与`manifest.json`；测试覆盖普通144格窗口及年末1格窗口的
诊断信息和两份失败文件。未修改MILP、预测器、回放、decision、共享物理容差或
HiGHS整数容差。

完整2月原诊断仅持久化上述审计数值，没有独立月度run目录或逐格artifacts。
不得将该表补造成月度manifest或sidecar；若需要逐格证据，必须由相同哈希输入重跑。

### 5.2 本地连续复现与年末不可达证据

以`584b66c`加上述异常包装补丁，从2025-02-01、SOC=6000 kWh重新连续执行，
本地诊断ID为`q3-context-repro-20260913`。复用原生产loader、release builder、window
factory、MILP与`run_q3_day`；仅将catalog按每天四个不可变发布版本分批构建，最新
发布版本的查找规则、24小时窗口、年度终点和跨日SOC传递均不变。没有从checkpoint
跳过前段，也未替换预测算法或实际回放。该本地诊断不是正式run。

本地完整2月期末SOC为`10745.301616126822` kWh，与原汇总记录一致；没有据此补造
原月度artifacts。连续复现完成至12月30日，其期末SOC为`10740.506741045345` kWh。
12月31日的窗口失败位置为：

| 项目 | 本地复现记录 |
|---|---|
| `decision_time` | `2025-12-31T22:10:00` |
| day / slot | `2025-12-31` / `133` |
| 当前SOC | `2657.079008047366` kWh |
| terminal mode | `year_end_equality`，目标6000 kWh，残值系数0 |
| 窗口长度 | 11个十分钟区间，止于`2026-01-01T00:00:00` |
| 当时合同 | 18:00版本3；11格全部`fixed_commitment`，与完整ledger逐格一致 |
| 求解状态 | SciPy status 2 / HiGHS status 8，infeasible |

该时刻属于本地复现；原Windows失败记录仍没有decision time，不能倒填原失败文件
或宣称两台机器的轨迹逐位相同。

独立验证步骤已经执行：从相同哈希输入重新生成18:00生产预测，用保存的ledger和
真实SOC重建窗口，所得`Q3WindowInput`与失败窗口完全相等；单独再次求解该窗口仍
返回status 2 / HiGHS status 8。22:00保存的上一窗口解通过独立validator，供需及
SOC方程最大残差分别为`1.1368683772161603e-13`、`9.094947017729282e-13` kWh。
该解预测年末SOC为6000 kWh，但执行第一格后的实际SOC与预测不同：

| 22:00—22:10量 | kWh |
|---|---:|
| 预测负载 | 524.116389616332 |
| 实际负载 / 实际PV | 571.865150000000 / 0 |
| 冻结合同 | 1357.434428889642 |
| 计划 / 实际充电 | 833.318039273310 / 785.569278889642 |
| 预测 / 实际格末SOC | 2700.052892392668 / 2657.079008047366 |

使用原生产recourse单独回放上述已结束区间，格末SOC与失败窗口的初态完全相等。
实际负载比预测高`47.748760383668` kWh，获批charge curtailment因此减少同量充电，
使SOC较上一窗口预测少`42.973884345301` kWh。这不是提前读取未来actual得到的调整。

对剩余固定合同格，若`C_k>0`，充放电互斥强制`D_k=0`，禁止紧急充电又强制
`q_emergency=0`。供需等式、非负grid spill及`PV_use<=PV_forecast`给出必要上界：

```text
C_k <= max(0, min(5000/6, q_fixed[k] + PV_forecast[k] - Load_forecast[k]))
E_end <= E_current + 0.9 * sum_k C_max[k]
      = 2657.079008047366 + 0.9 * 3666.641096664480
      = 5957.055995045398 kWh < 6000 kWh
```

即使忽略所有放电的储能损失，期末仍至少缺`42.944004954602` kWh。因此这次本地
失败具有独立物理不可达证据，不是通过调大容差、重试求解或接受非最优解能解决的
HiGHS误报。它暴露的是“预测年末等式 + 18:00后合同冻结 + 仅向下削减实际动作”
未保证偏差回放后的递归可达性。对年度边界、合同权限、响应或额外可达性保证的
任何实质变更须先交团队讨论和批准；本补丁不作这些变更。

本地失败证据目录为`outputs/runs/q3/q3-context-repro-20260913/`，含failure、manifest、
失败窗口、完整当日ledger、上一窗口解和独立`reachability_certificate.json`；日志与
本地诊断脚本在`outputs/evidence/q3-context-repro-20260913/`，均不提交Git。未生成
summary、domain result或三份成功sidecar，manifest中诊断文件哈希已逐份核对通过。

| 本地证据文件 | SHA-256 |
|---|---|
| `failure.json` | `62c511a7422ba070d3b03bf24075c6b0ed28c4c30566f5faedef5cec5414ddbd` |
| `manifest.json` | `409188c9740be724a86eb0fa41055c1723a68a45794183c5e4f9719a18113854` |
| `failed_window.json` | `fd1f10ac1c7a0674462049e00ac12f03793f99688d21520247dc6e967c18cb11` |
| `failed_ledger.json` | `3f31bfc32e594c9df6a9bf2781ab1cfbf3da328fa30a520b8c7cef41d14f9943` |
| `previous_successful_solve.json` | `b48adb0e6785160f706bc1a8a799ca8ca1a41645a5efa6a4aad483ca4a3a0d66` |
| `reachability_certificate.json` | `739a3599f12a6f64941f76954285997c83d8cc6fa7ef476a79de13bfbc9879b6` |
| `reproduce.log` | `0996c97cfde5aebedb409618f89a3bec70dc688b637bc9dab5174d8d50e955bd` |

以上数值和失败记录都只是诊断证据，不是正式Q3结果。正式Excel导出继续受独立模板
口径约束。

## 6. 明确批准后的全年轨迹与修正结算证据

### 6.1 批准范围与运行来源

本会话用户对限定范围明确回复“批准”：仅Q3在
`2025-12-31 00:00 <= tau <= 2026-01-01 00:00`的所有窗口状态边界增加SOC>=6000，
包括初态、跨日前瞻与终态；此前下界1200、上界10800、年度等式6000及残值0均不变。
这是新增的保守模型假设，不是题面事实，不保证任意输入的递归可行性或实际终点等式。
精确范围及局限见[`年末储备补充口径`](q3_terminal_reserve_proposal.md)。
既有`D_MODEL_Q3`仅choice/source补记本次批准，原status和确认字段未代填或改写。
预测、合同冻结、SETTLE-A、实际响应、共享物理阈值及HiGHS整数容差未改变。

生产年度诊断`q3-terminal-reserve-validation-20260913`以干净提交`6e5ca4e`运行，
source hash为`3b3fab47ed8b89479bcbc828131987f96a5320d90325958d3a71912793078631`。
调用完整生产`q3.run`链，从2月1日SOC=6000连续执行334天、48096格，未每日重置、
跳日、利用未来actual或拼接旧末态。一次构建1336个因果发布snapshot；观察脚本只
增加进度和失败上下文，不改变生产构建、求解、回放或写出。运行耗时4052.171秒。
日志在`outputs/evidence/q3-terminal-reserve-validation-20260913/run_diagnostic.log`。

### 6.2 发现并修复的独立写出问题

原年度滚动求解、实际回放和年度等式验证成功，生成summary、domain result及三份
sidecar；但首次独立逐笔对账失败。原writer把增减量均不超过物理阈值`1e-6`的调整
交易过滤了，尽管同一交易已按SETTLE-A计入账本和汇总。漏写2455笔非零交易，
最大单笔增减量`9.876982858258998e-07` kWh，合计费用
`0.00006788339322532307`元。原sidecar调整费用为`431146.9103190109`元；完整
持久化版本链重算为`431146.91038689425`元，与原summary/domain费用完全一致。
故此为序列化实现问题，不是模型不可行、汇总计算错误或应容差核销的交易。

`94f04c5`将过滤条件改为增减量均**精确为零**才跳过；回归测试保留
`100 -> 100+1e-7 -> 100`的两笔费用，净变化为零也不抵销。没有修改任何模型约束、
decision、物理/整数容差、费用公式或原年度轨迹与汇总。

原年度目录及失败审计日志原字节保留。使用
`scripts/rebuild_q3_settlement_evidence.py`从完整持久化四版计划链重建结算，写入
新诊断目录`outputs/runs/q3/q3-terminal-reserve-evidence-20260913/`：

```bash
uv run --locked python scripts/rebuild_q3_settlement_evidence.py \
  --source-run-id q3-terminal-reserve-validation-20260913 \
  --run-id q3-terminal-reserve-evidence-20260913
```

工具先核对输入和源文件哈希、完整版本、冻结及相邻计划量，使用现有生产ledger和
结算函数重算，拒绝覆盖源目录或已有目标目录。forecast与plan sidecar按字节复制，
执行区间字段完全相同；domain/summary仅更新run ID、sidecar哈希/行数和来源metadata。
新manifest为`status=success`、`validation_ok=true`、`diagnostic_only=true`、
`is_synthetic=false`、`artifact_regeneration_only=true`，写出代码为`94f04c5`，
source hash为`2ffa43999ff86fb232354da25c8e15b6e3c9f0a8bf62a2911b07557fcb3a5363`。
`trajectory_source`固定记录原`6e5ca4e`及原manifest、summary、四份结果SHA-256。
**新目录是修正写出证据，不是第二次全年MPC运行。**

### 6.3 全量独立复核结果

只读审计脚本`outputs/evidence/q3-terminal-reserve-validation-20260913/audit_result.py`
对新目录逐行复核，报告`independent_audit.json`为`validation_ok=true`。另核对
原目录文件未变、两份复制sidecar字节相同、全部执行区间及原metadata费用未变。
以下均为诊断证据，团队人工审核仍待完成。

| 项目 | 独立复核 |
|---|---|
| 日期/区间 | 2025-02-01至2025-12-31，334天、48096格 |
| 实际初始/最终SOC | 6000 / 6000 kWh |
| 全程SOC最小/最大 | 1199.9999999999786 / 10800.000000000004 kWh，原1e-6阈值内，无clip |
| 相邻/跨日连续 | 最大残差0；333个跨日边界完全相等 |
| 电池状态方程最大残差 | 0 kWh |
| complete-run validation | ok=true，48096格，issues=[] |
| 最后一天储备 | 145个状态边界，最小6000，最大短缺0，ok=true |
| actual来源 | 全部48096格与同哈希原始输入精确相同 |
| 预测引用可见性 | 7,717,800次原始来源核验，均available_at<=decision_time |
| 计划完整性 | 1336版，48096个完整四版链，已执行格计划量及预测引用冻结 |
| 行为/物理检查 | 充放电互斥、功率限值、非负购电、PV消纳/弃光、合同余电、禁止紧急充电均通过 |

预测sidecar含load 240480行、pv_attachment3 192384行、pv_tail 839160行。
结算含基准计划48096笔、非零调整4592笔、紧急购电15690笔，逐笔核对价格时间与
公式、完整非零调整事件及紧急事件。全年实际计划链没有方向反转实例（计数0）；
因此不伪造真实往返案例，净零往返的独立contract回归测试仍保留。

| 全年诊断费用 | 元 |
|---|---:|
| 计划购电 | 12453547.681701016 |
| 逐版调整 | 431146.91038689425 |
| 紧急购电 | 2130969.5233601714 |
| 独立fsum总费用 | 15015664.115448082 |

独立fsum总费用与原summary的`15015664.11544808`仅差浮点求和尾数，各分项与
汇总的比较均使用原费用阈值`1e-5`，未放宽。

### 6.4 完整2月的新增逐格证据

第4节原月度诊断仍只有汇总值，没有补造旧月度目录。新年度运行由相同输入重新
执行，其持久化结果内可逐格复核完整2月：4032格、112版、932笔紧急购电，
SOC由6000连续到`10745.301616126822` kWh，与旧审计数值一致。新结算证据含
2月基准计划4032笔、非零调整240笔，逐笔费用为：

| 完整2月分项 | 元 |
|---|---:|
| 计划购电 | 1179058.6264242507 |
| 逐版调整 | 14108.319007030788 |
| 紧急购电 | 85994.41875962359 |
| 独立fsum总费用 | 1279161.3641909051 |

这些是新全年artifacts中的实际2月子集，不是旧月度诊断的追造manifest或sidecar；
审计报告记录该子集数值，完整逐格证据保存在全年文件中。

### 6.5 文件哈希与证据保全

以下为新派生目录的原始字节SHA-256，已同时核对manifest与domain sidecar metadata：

| 文件 | 行数 | SHA-256 |
|---|---:|---|
| `manifest.json` | — | `28139fb5f7787e7cfce7c3eaadb7077f4ceb0f1e0209c0399cd4daf925d65924` |
| `summary.json` | — | `979a7eee8f68280b21ec0b8dc288110a2424e1e4545d7b86d2c144a7c3f0385b` |
| `domain_result.json` | — | `d2a7bb2cb61270b268155d229a3f7c719e4dabdedb9b996ba5f39487af7b8d55` |
| `forecast_provenance.jsonl` | 1272024 | `3700ce6052bcd187bd2b8cd2486bfc31dca823224a9764433c9771a1a17de429` |
| `plan_versions.jsonl` | 192384 | `2fd4cfdd8db4bc3e8b5022837ed0fe0b5d21a429afc60d57e5d5f5c1bf14619e` |
| `settlement_ledger.jsonl` | 68378 | `b42f3f5dcb5df36cb8618191e3b9dfb8fa872b380e5eb957c19e6f2aa6b38530` |

原生产目录manifest为`dc7c3c11690febc5f2f17ee3b49146a6120c8157dc907e443ab5daa4e89297f6`，
summary为`755836306c55a7a263a862f5e14cb6dd360933d8effccbe0a2596643db7e46f2`，
原settlement为`5d95be23239c2d6cab81e97c043c0c2515aeff80050897cfbfb8aaca51f20f2a`。
其他源结果SHA-256完整保存在新manifest的`trajectory_source.result_sha256`。
以下审计证据都在`outputs/evidence/q3-terminal-reserve-validation-20260913/`：

| 文件 | SHA-256 |
|---|---|
| `run_diagnostic.log` | `cab67e4399e856e5f3b1e10c163970113c120a951a206d4fc3d5337baa4ced2b` |
| 首次失败`audit_result.log` | `7948e817048f764093866f34e8c53cd02f2c5333265b115e706a44baaa7d20b5` |
| `audit_failure.json` | `087709783e433c1b32c44e06a2dc4e2f4b23a66f45dc1412f53555fb1942d4bc` |
| `cost_reconciliation.json` | `cc5e57d5370c15a94306ce06f63ecb6874cc37013660d2cd928efc014ea3fe1d` |
| `rebuild_settlement.log` | `a9f6ae850c43a68b58c470dac34d0b719b50368fc76cda71fc4766bf7f04ae1f` |
| `independent_audit.json` / `audit_result-corrected.log` | `c7451872268e57949164d476fb1999270ffae2be135c1781dd08021aa5bad840` |
| `audit_result.py` | `0cf11125772d7324299db10d530536a54bf1fc287c5f7e24f9741b5ba423ddb2` |

质量检查：ruff lint/format通过；`MICROGRID_RUN_SOLVER_TESTS=1 uv run --locked pytest -q`
全量289 passed，无跳过。代码和文档提交审核PR <https://github.com/Luqhhh/shumo/pull/3>；
本次独立技术复核不代替团队人工核验。所有原失败、新生产及派生证据继续只作诊断；
未写入selected_runs.toml，未导出result3.xlsx，未开始Q4-3，原始题包/附件/模板/outputs
均不提交Git。
