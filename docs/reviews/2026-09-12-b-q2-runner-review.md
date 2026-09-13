# B 线 Q2 求解器与年度交付审核

审核日期：2026-09-12。对象：`codex/b-q2-input-adapter`，精确提交
`ba02252cf8dce0016adf41f988f7da56608bc130`；main 对照为 `5c7d719`。

结论：**不通过正式模型/全年结果验收，暂不建议合并 B 整条分支。**
代码已有预测、MILP、滚动引擎、回放和产物写入，不能再称为“只有输入适配器”；
但以下反例说明其执行语义不是已冻结合同的 Q2。此次只审核，未修改 B 的实现或批准状态。

## 已确认问题（按影响排序）

### R1 / P1：正常购电合同被每十分钟重新提交

位置：`src/microgrid/problem/q2_engine.py:89–142`，尤其129–135；
`q2_model.py:230–236` 中每个窗口的 q 都是自由变量，没有固定合同输入。

引擎每个执行时隙重算预测与MILP，直接把当次 `plan.planned_purchase_kwh[0]`
作为该时隙正常合同量；没有保存00:00整日144格合同并冻结。Q2因此获得了
原题未授予的日内正常购电改写权，预测变化会改变购电费用与紧急电量，不能作为
Q2基线与Q3比较。`adjustment_cost=0` 并不能弥补这一问题。

最小合成探针：00:00的144格计划全为100；下一轮返回全101。最终slot1按101
执行，末格按243执行，完整日校验仍返回 `ok=true, checked_intervals=144`。
探针只替换预测/求解返回值，保留真实引擎、回放和校验，目的是检验合同冻结边界。

修复：将00:00合同作为独立不可变账本传入后续窗口；日内只调整获准电池动作，
当前短缺计入紧急购电。增加“后续预测/求解候选变化不能改写当天合同”的测试。

### R2 / P1：紧急购电可以全部用于电池充电

位置：`src/microgrid/problem/q2_replay.py:94–119`。

回放用 `load+charge-purchase-pv_used-discharge` 的正部计算紧急电量，
却未检查紧急购电与充电互斥。负载100、合同100、PV0、充电50、放电0 kWh时，
返回紧急50 kWh、期末SOC6045 kWh，且可成功转换为共享 `IntervalResult`。
这直接违背当前main已批准的D_SETTLE“紧急电量不可给电池充电”。

修复需要明确实时行动的可行性处理：禁止此组合或按批准的实时策略处理，
不能在actual到达后静默重优化过去的动作；补充回放与整年校验反例。

### R3 / P1：批准的终端条件没有成为正式默认配置

位置：`src/microgrid/problem/q2.py:151–169`，`q2_engine.py:112–121`。

B分支的D_TERMINAL/D_YEAR_BOUNDARY已标approved，要求经济终端价值和年度末
SOC6000；但空metadata的正式配置得到 `terminal_value=0.0, annual_soc=None`。
因此标准入口可在决策gate通过后忽略这两个模型条件。仅允许调用者任意传metadata
不等于按照approved choice实现。

此外，年度约束定位使用 `actual.day == action_end_day`，会把任意测试运行的结束日
当作“年度末”，而非明确的2026-01-01 00:00；末日窗口也继续保留年后的预测成本。

修复：从批准口径生成正式配置；正式运行期、真正年度终点与短期工程验证显式区分，
并验证年末约束、残值移除和末日窗口边界。不能把关闭约束的测试模式当成正式结果。

### R4 / P1：标准命令无法直接读取官方附件，也不默认运行全年

位置：`src/microgrid/problem/q2.py:189–190, 228–239`；
`q2_inputs.py:266–289`。

- 默认sheet是“实际负荷/实际光伏”，官方附件2实际为“小区负载/光伏发电实际功率”。
  只读实测直接抛 `Worksheet 实际负荷 does not exist`。
- 即使修正sheet，默认expected_days是2025-01-04至2025-02-01，而读取器要求
  与整份附件日期集合完全相等，故全年附件报大量extra_days。即使设置action_end为
  12月31日，仍遗漏1月1日至3日。
- `action_end_day` 默认为 `action_start`，并非12月31日；CLI未暴露这些metadata参数。

修复：分开完整输入覆盖验证与正式行动区间选择；标准正式入口使用真实sheet和
2025-02-01至12月31日的行动范围，读取一月全部历史；用官方附件做入口集成检查。

### R5 / P2：正式默认预测没有AR(1)修正或历史估计权重

位置：`src/microgrid/problem/q2.py:152–160`；`q2_forecast.py:176–180`。

空metadata配置为四周等权、`ar1_phi=0.0`。未来h>0时残差修正恒为0，
并不执行B分支D_MODEL_Q2记载的历史估计权重与AR(1)修正。只有显式传None才拟合phi。
这不是C线Q3的LOAD-A参数问题：Q2必须以其自身获批算法和24小时证据为准。

修复：冻结Q2精确预测参数/估计规则并由正式入口读取，记录实际有效配置，
增加验证默认运行确实调用残差拟合而非仅检查输出非负的测试。

### R6 / P1：自定义实际输入路径未与登记输入哈希绑定

位置：`src/microgrid/problem/q2.py:39–41, 177–182, 217–225, 234–239`。

允许用metadata替换attachment路径，但正式provenance预检只验证repo登记路径，
随后读取的是覆盖后的另一文件，未比较其哈希与登记哈希。默认文件正确并不能证明
实际求解文件相同；这会让“登记输入已验证”的manifest不足以证明使用了官方附件。
`input_snapshot`虽保留实际读入哈希，但不能替代前置绑定检查。

修复：对真正读取的文件校验登记SHA-256，或显式登记新的输入来源并记录其批准范围。
需要“登记文件正确、override内容不同”的拒绝测试。

### R7 / P2：运行落盘丢弃逐版预测与有效参数，无法核验计算来源

位置：`src/microgrid/problem/q2.py:275–315`；`q2_engine.py:160–183`。

引擎收集了 `forecast_records` 和详细求解metadata，runner却仅保存输入快照、
最终区间结果、汇总和 `decision_time/status` 文本日志。预测值、每版training_cutoff、
权重/phi、fallback和完整求解信息未持久化；运行metadata覆盖的有效参数也未完整存档。
因此同一代码+输入可以使用不同隐含metadata而没有足够记录解释差异。

修复：保存实际有效配置、可重建的逐版预测/动作来源及完整solver记录，全部纳入
manifest哈希；长期运行宜逐步落盘，避免只在结束时生成证据。

### R8 / P2：计划校验器对年度末仍扣残值，且未接入引擎

位置：`src/microgrid/problem/q2_model.py:458–475`。

求解器在年度硬约束进入窗口时去掉残值，但校验器两分支都取同一SOC并无条件
减去 `terminal_value*SOC`，也未处理terminal_value_multiplier。真实HiGHS两步
探针：年度末6000、v=0.5、购电目标200元，求解status0；校验器却报成本差3000元。
引擎没有调用 `validate_q2_plan`，目前成功状态只经过回放与coverage校验。

修复：统一年度残值开关、倍率与目标口径，并在接受计划前运行独立计划校验。

## 已做的验证与限制

- 隔离worktree完整测试：**151 passed, 1 skipped**；跳过的是可选Q1实际MILP测试。
- GitHub [CI #67](https://github.com/Luqhhh/shumo/actions/runs/34686040797) 对应精确head，success。
- 执行R1/R2的合成边界探针、R3/R5默认配置检查、R4官方附件只读检查、R8实际HiGHS探针。
- 探针与机器输出仅在本地 `local/reviews/b-q2-ba02252/` 保存；均非正式运行产物。
- 未执行昂贵的全年重跑，也未把合成探针、测试fixture或提交标题当成全年结果。
- B分支文档 `b_model_card.md`、`b_decision_evidence.md` 仍写未实现，与代码不一致。
  B与main的D_LOAD_FORECAST/D_MODEL_Q3/D_SETTLE也已分歧；集成时不能覆盖A的新批准。

## 全年产物：尚不能验收

本地当前工程的outputs/local没有找到正式Q2 run；B分支selected_runs没有选定q2。
原始模板 `data/templates/result2.xlsx` 和pytest伪造的结果文件不是正式结果。
已向用户询问B执行环境中的run_id与目录，尚未获得可访问的产物路径。
这表示**证据未取得**，不声称B机器上一定没有产物。

取得产物后必须独立核验：

1. manifest的code_commit、dirty状态、真实输入文件哈希、is_synthetic=false、成功状态；
2. 2025-02-01至12月31日334天、48,096条十分钟行动，完整且无重复；
3. 初始SOC6000、跨日连续、年度最终SOC按批准边界，效率/功率/SOC约束；
4. 全年正常费用、紧急5倍费用的逐格重算与汇总一致；不允许R1/R2；
5. 每个00:00合同版本及逐版预测/训练截止，与实际执行量可追溯；
6. `validation.json`的成功标志与独立重算一致；正式result2.xlsx的映射、哈希及导出来源。

即使现有文件存在，R1/R2仍使其不能直接作为合规Q2结果采用，需修复后重跑；
现阶段不推荐仅补文档或仅等待CI后合并。
