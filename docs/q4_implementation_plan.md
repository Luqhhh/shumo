# Q4-2 / Q4-3 完整实现规范 v2

日期：2026-09-12。状态：**proposed，设计交付，不是已运行的模型**。
本文件对用户提供的 v1 进行审阅和补全；配套审阅记录见
[review](reviews/2026-09-12-q4-design-review.md)，模型卡见
[q4_model_cards.md](q4_model_cards.md)。核对代码基线为 c07c675。

## 1. 设计与批准边界

采用一个共享滚动 MILP、两个 case 配置。Q4-2 在波动价格下重算 Q2：
历史实际负载/PV形成预测，每天00:00提交合同，日内仅控制电池。
Q4-3 在相同框架增加已发布官方PV预报以及06/12/18当日未来合同调整。
点预测为主方案，不引入随机规划、CVaR或深度学习。

沿用 D_TIME_INTERNAL、D_EFF、D_STATE、D_INFO、D_SETTLE；Q4-3另用
D_RESAMPLE。Q4模型卡明确包含完整基础模型，不能把尚未批准、实现的Q2
当作已完成依赖。LOAD-A在Q4的使用、下面的控制/窗口/年度边界均为本卡
新增范围，不借用Q3批准状态。Q3现有固定24小时规范不在本轮改写。
Q4正式导出需单独覆盖模板范围，现有Q1导出批准不足。
所有新增模型状态保持proposed，确认人和时间为空；未形成正式run或selected_runs。

## 2. 时间、数据与因果性

一月仅初始化历史，E=6000 kWh；行动期为2025-02-01 00:00至
2026-01-01 00:00，334日，每case 48,096步。每天144行动、145状态边界。
右端点记录s表示[s-10min,s)，功率在输入层乘1/6转kWh，价格不换算。
所有预测器只接收InfoSet过滤后的历史，available_at<=decision_time。
完整当前区间实际价格到右端点才可见；交易时刻tau的结算价为
[tau,tau+10min)对应实际价，先记待结算交易，结束后补费用。
执行器读取实际区间值只用于固定保护律和结算，不回传经济优化器。

每次r=00/06/12/18刷新负载、价格、相应PV；非刷新时只切片已保存版本。
窗口为[t,min(r+24h,2026-01-01 00:00))，发布间逐步收缩。
必须检查该窗口完整预测覆盖；若官方覆盖不足，停止并记录missing_forecast，
**不静默缩短00:00整日合同窗口**，也不伪造尾部。正常24小时附件覆盖下
该规则解决“固定24h窗口+发布间冻结预测”尾部缺失。修改Q3对照必须另记版本。
年末只预测必要目标；终端进入窗口后不用残值，不要求不存在的全年外actual。

预检每份输入实际读取路径、SHA256、单位、时区、唯一时间网格和缺失；
附件4必须全年52,560条有限非负价格。负价或缺失时停止，不截断原始值。
本轮已核对价格范围0.0076—1.7936元/kWh，无负价/零价/缺格；这不是预测效果证据。

## 3. 预测公式与记录

以下t是最新已结束区间的右端点，h为未来十分钟步数，s=t+h。
LOAD-A用功率kW计算（也可一致转能量，日志标单位）：

```text
bL[s] = (8 L[s-1008] + 4 L[s-2016] + 2 L[s-3024] + L[s-4032])/15
eL[s] = L[s] - bL[s]
phiL = clip(sum(eL[s-1]*eL[s])/sum(eL[s-1]^2), 0, .999)
Lhat[t+h] = max(0, bL[t+h] + phiL^h * eL[t])
```

AR拟合使用截至t所有有效连续残差对；必须先有全部非零权重滞后再构造残差，
不能把历史第一天当28天基线起点。分母零取phi=0；缺少最新残差、滞后或
连续记录报错，不跨缺口连接AR样本。固定经验权重，不用全年误差反向调参。

Q4-2：PVhat[t+h]=max(0,sum(PV[t+h-144*j],j=1..7)/7)，h<=144，
所用滞后均在刷新时可见。禁止读取附件3，禁止依赖D_RESAMPLE。

Q4-3：对同valid_time的全部可见官方版本，按原始lead_hours历史已实现
误差MAE赋权 raw_w=1/(MAE_lead(t)+1 kW)^2，再归一化组合。
误差统计截止当前刷新t，不冻结在旧issue_time。无有效误差样本时报错，
不能暗设零误差；预检一月是否足以初始化各需要的lead桶。
先形成唯一小时预测，再用RESAMPLE-LIN和最新结束区间PV均值左端代理
插值到十分钟右端点，最后转kWh。刷新之间不更新代理或权重。

价格主方案：

```text
bp[s] = .5 p[s-144] + .3 p[s-1008] + .2 p[s-2016]
ep[s] = p[s] - bp[s]
phip = clip(sum(ep[s-1]*ep[s])/sum(ep[s-1]^2), 0, .999)
phat[t+h] = max(0, bp[t+h] + phip^h * ep[t])
```

AR规则同负载，经验权重不是已验证最优参数。对照仅替换为p[s-144]。
每个预测快照保存id、case、变量与单位、issue/valid/available时间、训练截止、
实际文件hash、代码/模型版本、滞后值和权重、phi、最新残差、预测值、历史样本数。
预测刷新不会赋予Q4-2日内合同权限。

## 4. 统一MILP

变量每slot包括G合同总量、C/D母线充放电、E内部储能、U预测紧急量、
PV_use、S_PV弃光、S_grid未用合同；非负。z为充放电模式、y为紧急模式。
给定当前真实E，M=5000/6 kWh：

```text
G + PV_use + D + U = Lhat + C + S_grid
PV_use + S_PV = PVhat;  0 <= S_grid <= G
E[k+1] = E[k] + .9 C[k] - D[k]/.9
1200 <= E[k] <= 10800
C <= M*z; D <= M*(1-z); D <= Lhat
U <= Lhat*y; C <= M*(1-y)
S_grid <= Gmax*(1-y); S_PV <= PVhat*(1-y)
z,y binary
```

D<=Lhat防止电池直接倒送/丢弃；紧急互斥不依赖价格自动保证。
new_day/lookahead的Gmax=Lhat+M；adjustable取max(Gprev,Lhat+M)，fixed为
已确认合同值。这是非负价格、免费弃余电条件下的优化界，不是外网物理上限。

合同权限由独立ledger生成：

|分类|约束与交易|
|---|---|
|new_day|00:00选择当天完整144格，提交唯一G0|
|fixed|当日其他时刻G等于最近确认量，Q4-2全天适用|
|adjustable|仅Q4-3在06/12/18修改当日未开始slot|
|lookahead|次日未提交虚拟G，只进预测目标、不进合同或费用账本|

调整Gnew=Gprev+delta_plus-delta_minus。用方向w约束
0<=delta_plus<=Gmax*w、0<=delta_minus<=Gprev*(1-w)。
交易记录由相邻版本G重新计算正负部，禁止用最终对G0替代逐版差额。
不得在当前模型内免费实施尚未到来的调整机会。

```text
min J = sum(phat[k]*G[k], new_day/lookahead)
      + phat_trade(t)*sum(1.5*delta_plus + .5*delta_minus)
      + sum(5*phat[k]*U[k]) - v_r*E_end
```

交易价为当前第一个区间的预测价，不是被调整目标slot价。已提交计划费和
过去调整费都是当前不可变常数。不把窗口目标相加形成年度费用。
v_r=.9*mean(刷新时完整144格预测价)，版本内保持不变；窗口触及年度终点
时去掉残值、施加E_end=6000。普通日不施加首尾相等。

年度剩余n步时，必要可达条件为
6000-n*.9*M<=E<=6000+n*M/.9，与SOC边界相交。
这不是实际负载/合同下充分条件；末日预测可行不保证真实限充后达到端点。
实际末态不满足6000则terminal_infeasible并阻断正式导出，不应宣称有保底可行解。

## 5. 实际反馈与费用

先验证意图Cbar/Dbar对当前E的功率、SOC、互斥合法性；不clip非法意图。
在“十分钟平均功率恒定且本地控制器能及时限幅”的明确附加假设下：

```text
C = min(Cbar, max(G + PV - L, 0))
D = min(Dbar, L)
U = max(L + C - G - PV - D, 0)
PV_use = min(PV, L + C - D)
G_use = L + C - D - U - PV_use
S_grid = G - G_use; S_PV = PV - PV_use
E_next = E + .9*C - D/.9
```

PV优先、合同补足的余量分配只规范指标，不修改已购费用。固定保护只减少
原意图动作，不用实际量重选经济动作。记录意图/实际/削减量/原因、供需测量、
控制假设和实际后继SOC。如果控制系统不能在该区间及时限充，则保留原动作
与违规证据，标记execution_infeasible，不能事后改写为上述可行轨迹。

独立检查能量守恒、所有流非负、0<=G_use<=G、PV利用界、紧急与充电/弃电
互斥、SOC连续和范围。共享BatteryAction负责物理动作，不能替代完整流账本校验。
完美预测时，任一可行计划的动作都不会被这条保护律改变。

```text
planned_cost = sum(p_actual[k]*G0[k])
adjustment_cost = sum_v p_trade_actual[v]*sum_k(1.5*plus[v,k]+.5*minus[v,k])
emergency_cost = sum(5*p_actual[k]*U[k])
total_cost = planned_cost + adjustment_cost + emergency_cost
```

合同未用仍付费，减购无退款，不再次按最终G全额收费。Q4-2调整费严格0。
手算10->8->9，执行价2、交易价1/3、紧急1，费用20+5.5+10=35.5元。
非负交易价、免费弃合同余电且无退款时，“保留合同并弃余量”弱支配主动减购；
正价下大量减购应检查求解gap和实现，不为图形添加退款。零价可能退化多解。

## 6. 工程接口与事件流程

优先复用已有InfoSet、BatteryState、BatteryAction、apply_battery_action、
CaseResult、IntervalResult和独立导出层。当前main没有可直接调用的完整Q2/Q3
runner；B侧合同/回放缺陷见既有审核记录，不能只凭CI通过复用。

|建议模块|接口/职责|
|---|---|
|forecast_price.py|forecast_price(info,issue_time,target_slots)->ForecastSnapshot|
|forecast_pv_history.py|同slot历史PV；不读取附件3|
|purchase_ledger.py|合同权限掩码、版本、待结算交易及实际账单|
|dispatch_milp.py|solve_dispatch(state,forecasts,ledger,permissions,terminal)->DispatchPlan|
|dispatch_feedback.py|apply_feedback(intent,state,committed_grid,current_measurement)->ExecutionRecord|
|rolling_engine.py|事件循环、预测快照、实际状态、缓存、checkpoint|
|q4_2.py / q4_3.py|case配置和编排，返回领域结果，不写Excel|
|q4_validation.py|独立重算合同、物理、费用及来源，不调用求解器|

每步顺序：加入截至左端已结束actual→InfoSet→必要时刷新预测→窗口和权限
→求解并独立验证→按合法事件提交合同→保存动作意图→执行固定保护→区间结束
结算→实际SOC推进→留存区间记录。跨日E连续，先完成前一日最后区间再做00:00事件。
checkpoint保存实际E、时刻、未完成账单、全合同版本、预测与训练状态、输入hash、
有效配置和schema版本；恢复须拒绝来源不匹配并避免重复提交/结算。

## 7. 求解、缓存与失败

使用锁定环境的SciPy milp/HiGHS：presolve=true,mip_rel_gap=1e-4,
time_limit=10秒，超时最多重试60秒。正式主轨迹只接受status=0且独立校验通过；
status=1是限制触发，2不可行，3无界，4其他错误，不伪装成功。
保存status/message、耗时、objective、gap、dual_bound、nodes和约束残差。
[官方接口](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.milp.html)
无x0参数；不声称已经热启动。使用稀疏矩阵和结构缓存。

解后缀复用须同时满足：同预测id/窗口终点/终端价值/约束、无合同事件、
实际SOC等于上解对应预测SOC、固定合同未变、全后缀可行。
母解固定前缀后的目标常数必须正确移除，包括00:00合同从变量变已付常数；
保存母解绝对误差界，并按尾问题重新判断容许gap。没有可验证界则重解，不能
把母解相对gap直接移植。精确最优母解满足这些条件时才有最优后缀结论。
记录parent_solve_id、shift_count、reused_tail、contract_hash和证书。

没有可行解、缺预报、实际反馈/年度端点失败都保留失败现场并停止正式轨迹。
不回退未批准策略、不填零续跑。两case共96,192步；先测真实单日、跨日、一周
耗时/求解比例再估全年时间。独立case可并行，同case不切断状态链按日拼接。

## 8. 模板映射提案（尚未导出验证）

原模板只读，写副本。两份计划表均335行147列，334日期齐全；Q4-3调整表同构。
B1=0:10-0:20，EO1=0:00-0:10+1，与内部自然日网格有偏移。
采用**显式列序**：日期序号i=0..333对应row=i+2；slot k=0..143对应col=k+2
即B:EO，保留原标签并在export_manifest列出真实区间及原标签冲突。
这是一项候选交付解释，不声称官方已经勘误。

计划表：B:EO写G0，EP写sum(G0)，EQ写planned_cost。
Q4-3调整表：B:EO写最终确认G，EP写sum(Gfinal)，EQ写adjustment_cost。
EQ的费用分项解释必须随映射说明保存；全费用含紧急费写summary/论文表，
不能把此列分项冒充总费用。若要求Excel本体显示总费，可在输出副本新增明确命名
“费用汇总”表，列计划/调整/紧急/总费，而不改变官方原列含义；该新增表也须纳入映射核验。

充放电量表目前只有示例日期和省略行，不能直接填少数日期当全年结果：输出副本
展开334个六行块，首行2+6*i，B为六个四小时区间，C/D为实际C/D块汇总；
E/F在块第1/2行分别写0:00及24:00标签与真实E日初/日末，其余状态格空白。
日期A按模板样式合并六行；复制样式需清除原示例合并范围，不能改原文件。

紧急表按日合并相邻U>共享容差的slot，写起止/总kWh，不跨日；每日至少一行，
无紧急量时日期+空时间段+0。不得省略中间日期或保留“⁝”冒充全年。
完整source_slot->sheet/cell映射和读回须核验144格、334天、六块、费用与SOC。
四个展示日03-20/06-21/09-23/12-21均从同一全年轨迹提取。

## 9. 验证、比较与产物

物理能量沿用共享绝对容差1e-6 kWh、相对容差0，年度端点同口径；不能采用
v1随规模增大的相对容差放宽已有contract。费用用未舍入值独立重算，候选总差
<=0.01元；展示舍入只发生在最后，逐日舍入不能再累计冒充精确年费。

必须覆盖：未来actual扰动不改当前意图；Q4-2附件3扰动无影响；日内G0冻结；
Q4-3未来/已执行权限；刷新尾部覆盖；虚拟合同不落账；残差起点和连续性；
功率/SOC/互斥/跨日；限充手例(100,100,0,50)->C0/U0及(130,100,0,50)->C0/U30；
无法及时限充不可行分支；完美预测；35.5元逐版结算；非负价减购支配；
实际年度末；求解失败；缓存条件破坏；checkpoint等价；来源override和模板读回。

四条连续回放：Q4-2/3各自价格主方案和一日同slot对照，其余假设完全一致。
各自真实SOC轨迹连续，不能每日重置或共用另一策略实际状态。报告6h、24h预测
MAE/RMSE及重叠窗口样本口径，月份指标；实际计划/调整/紧急/总费、G0/Gfinal/
外网实际用量(sum(G_use+U))、紧急量/区间/天数、弃光、未用合同、吞吐、初末SOC、
保护次数和运行/求解统计。预测误差下降不等于费用下降，负结果保留。
不同实际价格制度间成本差不称策略节省；price-oracle仅为诊断，不自动是严格下界。

每case保存manifest.json、input_snapshot.json、effective_config.json、domain_result.json、
forecasts/contracts/execution_feedback/cost_ledger/solver_records.jsonl、daily_summary.csv、
summary.json、validation.json、export_manifest.json和results/result4-2.xlsx或result4-3.xlsx。
主数据使用类型字段和schema版本，不塞入不受校验metadata；planned_purchase_kwh=G0，
adjusted_purchase_kwh=最终G，action=实际动作，意图与版本账本为明确伴随记录。
源代码/配置/实际输入hash全绑定；未运行项写not_run。

## 10. 实施任务与验收顺序

1. 落实两张模型卡及Q4范围的预测/反馈/窗口/年度/模板/比较决定；保留真实批准记录。
   正式gate需逐项检查范围和确认字段，不能仅用Q1导出approved放行Q4。
2. 共享合同ledger和反馈层：先通过冻结/版本/负荷优先/35.5元反例，独立验证器先于全年运行。
3. 预测实现和可复现历史预检；复用C已验证公式但重新核验Q4完整24小时跨度。
4. 共用MILP和两case编排；合成完美预测、实际单日/跨日/一周验证与性能评估。
5. 绑定run来源及checkpoint，跑两主方案；只有独立年度/端点/导出读回通过才形成候选正式结果。
6. 两价格对照与论文表；不倒推主参数，不预写改善。记录AI用于模型设计及实际人工核验状态。

本轮只完成设计和只读证据脚本，不把模型实现、全年求解、模板读回或人工核验写为通过。
