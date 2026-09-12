# Q3 批准模型卡

2026-09-12，A（本会话用户）明确确认 `LOAD-A + LF-A`，要求记录
`D_LOAD_FORECAST`、接入 Q3 gate，并批准 `D_SETTLE`、`D_MODEL_Q3`。
本卡将现有 C 线证据与 B 分支 `b4113b0` 的滚动模型提案整理成实现口径。
确认日期按本会话日期记录，不虚构精确签署时间或其他成员签名。
机器状态以 `configs/decisions.toml` 为准；批准模型不等于已经实现或验证求解器。

## 预测和信息

- 每次决策先构造 `InfoSet.from_raw`，只读 `available_at <= decision_time`。
  完整十分钟 actual 仅在右端点可见，未来 actual 只在回放结算阶段使用。
- 负载采用 `D_LOAD_FORECAST` 的四周加权与 expanding AR(1)，权重精确为
  `(8/15, 4/15, 2/15, 1/15)`，不得替换为连续 28 日同 slot 均值。
- 按 `LOAD-HORIZON-A`，LOAD-A 只在00/06/12/18冻结未来30小时版本；中间
  10分钟 MPC 只从最近发布版本切取未来24小时，不重新预测或读取新版 actual。
- 00/06/12/18 先发布后计划。PV 对相同 `valid_time` 的全部可见版本组合，
  以 `lead_hours` 分组、截至当前决策时刻已经实现的误差计算 MAE，使用
  `w_j ∝ 1 / (MAE_j + 1 kW)^2`。历史截止为当前决策时刻，而非旧版本发布时间。
  `epsilon=1 kW` 为既有复算基准，未声称优于其他正 epsilon。
- 上游唯一小时序列按 `D_RESAMPLE` 线性插值到十分钟右端点；当前边界代理
  来自刚结束区间的实际 PV 均值。下一发布前保留本次预测版本，不创建虚假新预报。
- 每版保存时间、训练截止、来源哈希、算法参数、负载滞后值和 PV 权重来源。
  一月作初始化历史，二月起正式运行；缺失所需历史、预测或非有限值必须报错。
  不将尚未批准的 Q2 预测器作为静默回退。

## 合同与结算：SETTLE-A-v2

全部能量单位为 kWh，费用为元。`q0[k]` 为当天 00:00 版本0；
`qv[k]` 为后续版本，`qprev[k]` 为上一版已确认量。

```text
delta_plus[v,k]  = max(qv[k] - qprev[k], 0)
delta_minus[v,k] = max(qprev[k] - qv[k], 0)
qv[k] = qprev[k] + delta_plus[v,k] - delta_minus[v,k] >= 0

planned_cost    = sum_k p_base[k] * q0[k]
adjustment_cost = sum_v,k p_trade[v] * (1.5*delta_plus[v,k] + 0.5*delta_minus[v,k])
emergency_cost  = sum_k 5*p_exec[k]*q_emergency[k]
total_cost      = planned_cost + adjustment_cost + emergency_cost
```

合同未利用仍付费，减购没有额外退款，不允许售电收入；上述均作为明确建模解释。
例如原计划10、改为8、再改为9 kWh，初始费仍为 `10*p_base`，调整费分别为
`0.5*2*p_trade1` 与 `1.5*1*p_trade2`，不能抵销成最终减少1 kWh，也不能再收
`9*p_base`。`SETTLE-B` 的最终版一次差额只作敏感性。

Q3 的 `p_base[k]`、`p_exec[k]` 为执行区间固定电价；`p_trade[v]` 为发布时刻
所在区间固定电价。按附件1右端点标签确定区间：06:00发布使用
`[06:00,06:10)` 的价格，即标签06:10，而非已经结束区间的价格。
Q4 对应使用附件4实际结算价格，其计划价格可见性仍需各自模型批准。

Q3 仅在00/06/12/18改变当日尚未开始的合同。已执行时隙冻结；“之前已确认”
不等于“未来永不准调整”。发布之间合同固定，十分钟电池控制不得重写合同。
跨日 lookahead 变量不是次日已经提交的合同，次日00:00才生成自己的版本0。
其固定价成本只作为当前优化的 `lookahead_surrogate_cost`，避免把次日外网电当成
免费能源；不写交易账本，也不进入正式累计结算。
账本保存每次 `issue_time/target_slot/previous_committed_kwh/new_committed_kwh/
delta_kwh/transaction_price/is_frozen`；最终调整表输出总量而不是增量。

实际物理记账采用两个等式，避免 PV_use 与弃光重复扣除：

```text
q_final + PV_use + D + q_emergency = Load + C + S_grid
PV_actual = PV_use + S_PV
0 <= S_grid <= q_final;  PV_use, S_PV, q_emergency >= 0
q_emergency = max(Load + C - q_final - PV_use - D, 0)
q_emergency > 0 => C = 0, S_grid = 0, S_PV = 0
```

紧急电量是实际剩余短缺，不是可自由套利或给电池充电的正常电源。
实时可行性检查必须验证以上约束，不能仅依赖高价格期待互斥自然成立。
实际量不得返回计划器重写过去的决策；不可行回放必须报告失败。

2026-09-13，C成员明确选择 `ATTR-PV-FIRST` 作为Q3结果归属的
`MODELING_ASSUMPTION`。若实际回放产生总剩余供给，先令
`S_grid=min(q_final, surplus)`，再将剩余部分记为 `S_PV`，因此优先保留光伏
用于本地消纳。若 `q_final` 与 `PV_actual` 全部归属后仍有 surplus，说明固定的
计划放电本身造成过供；基线回放必须显式失败，不能把电池放电伪装成弃光。
`ATTR-GRID-FIRST` 仅可作为后续敏感性对照，不进入当前主方案。

## 优化、行动与年度边界

采用 SciPy `milp` / HiGHS，24小时窗口，每十分钟重算并只执行当前电池动作。
变量包括合同量、发布时刻的增减量、电池充放电、SOC、光伏利用/弃光、合同余电
和预测紧急缺口；充放电与必要的物理互斥由二进制变量约束，不能事后 clip 修补。
供需在计划中使用预测量，在回放中使用实际量，分别校验。

`E_next = E + 0.9*C - D/0.9`，`1200 <= E <= 10800`，
`0 <= C,D <= 5000/6`，不同时充放电。一月待机6000，二月起跨日连续。
目标计算窗口内可变的新增计划费、调整费、预测紧急费和终端价值；历史支付为常数，
不得将每轮重叠窗口目标累加成正式成本。

窗口未到年度终点时，终端项为 `-v_t*E_end`，其中
`v_t = 0.9 * mean(窗口结束后144个十分钟固定电价)`，固定日电价按时隙循环。
年度最终时点进入窗口后，在 `2026-01-01 00:00` 截断窗口，去除残值项，
改为 `E_end=6000`。只在年度末施加等式，不每日重置。终点可达性必须检查，
不能通过越界行动或伪造紧急充电实现。

滚动、终端和年度语义纳入 `D_MODEL_Q3` 本身，不暗中依赖配置中不存在的
`D_MPC/D_TERMINAL/D_YEAR_BOUNDARY`。本次并不批准 Q2、Q4 的模型或正式结果导出。

## 接入与验收

Q3 gate 必须检查共享四项、`D_RESAMPLE/D_SETTLE/D_LOAD_FORECAST/D_MODEL_Q3`。
全局 final gate 也检查 `D_LOAD_FORECAST`，防止仅凭旧运行产物绕过负载决策。
删除、改为pending/proposed、清空确认人或日期必须重新阻断；全部批准后，当前
runner 应抛出 `ModelNotImplementedError`，不得返回假成功。

后续求解器验收还需覆盖未来信息扰动不影响当前决策、逐版交易手算、冻结时隙、
午夜价格对齐、实际能量等式、紧急互斥、跨日/年度SOC、求解状态和预测provenance。
正式输出必须来自 `CaseResult/IntervalResult`，另行完成模板映射和导出验证。

预测敏感性保留 LF-B、半周半衰期、PV最新版本、56日误差历史、其他正epsilon及
PCHIP；终端价值比较0.8v/v/1.2v。已有六小时预测误差证据不构成全年费用改善证据。
