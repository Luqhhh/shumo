# C 线 Q3 工作交接

交接日期：2026-09-13

审核分支：`audit/q3-handoff-20260913`；交接源：`feat/q3-forecast-control@584b66c`

已推送的实现：`dec302e`（失败上下文）、`6e5ca4e`（明确批准的年末储备）、
`94f04c5`（非零微小结算交易写出）；审核PR：<https://github.com/Luqhhh/shumo/pull/3>。

这份文档用于让下一位协作者直接继续 Q3 验证与交付，不重新打开已经由团队确认的
架构和建模口径。接手前先阅读根目录 `AGENTS.md`；其中关于 decision gate、原始数据、
正式结果和 Excel 导出的限制继续有效。

## 1. 一句话状态

Q3 的因果预测、24小时滚动 MILP、实际回放、版本账本和三份 sidecar 已经接通；
真实1日、连续7日和完整2月均已通过。首次全年诊断已在 `rolling_solve` 阶段失败，
原失败没有具体决策时刻。本地连续复现已定位12月31日22:10的年度终点不可达，
独立上界证明当时冻结合同下无法恢复到6000。用户随后明确批准限定最后24小时
状态边界SOC>=6000，新生产全年334天/48096格成功，实际终点6000。
原结算sidecar微小交易漏写已修复，另目录派生证据通过独立逐笔及哈希审核；不是
第二次全年MPC运行。原失败和原全年文件保留。团队人工审核待完成，未选正式run。

## 2. 已经完成的内容

- C 线完整运行链：正式输入读取、PV/负载预测、24小时窗口、MILP、每10分钟执行、
  actual replay、结算和 artifact 写出。
- 原交接全量测试为`263 passed, 1 skipped`；失败上下文补丁后为
  `265 passed, 1 skipped`，启用可选Q1 MILP测试后`266 passed`，ruff lint/format通过。
- 最新全量测试为`289 passed`（启用可选solver测试，无跳过），ruff lint/format通过。
- 真实1日审计通过：144格，4个计划版本，19次紧急购电，总费用
  `26112.095838` 元；这只是诊断值。
- 真实连续7日审计通过：1008格，SOC跨午夜连续，28个计划版本，111次紧急购电，
  总费用 `343789.609364` 元；这只是诊断值。
- 真实完整2月审计通过：4032格，SOC从 `6000` 到 `10745.301616126822` kWh，
  跨日连续，112个计划版本，932次紧急购电，总费用 `1279161.364191` 元；
  这只是诊断值，已补入 `docs/q3_real_data_audit.md`；原月度诊断没有单独run目录。
- HiGHS 的整数可行性容差已收紧到 `1e-9`，避免 big-M 放大后出现极小的反向动作；
  共享物理验证阈值没有放宽。
- 新生产全年诊断`q3-terminal-reserve-validation-20260913`：333个跨日边界完全连续，
  末日145个状态边界最低6000；原全年文件保留在忽略的outputs中。
- 修正写出证据`q3-terminal-reserve-evidence-20260913`：forecast/plan文件原字节不变，
  全部实际动作/SOC不变，结算补全2455笔原本已计费的微小调整；原汇总费用不变。
  三份sidecar共1272024/192384/68378行，独立validation、逐笔费用和哈希均通过。
  manifest明确`artifact_regeneration_only=true`与源轨迹提交，不能称为新全年重跑。
- 新全年artifacts内完整2月4032格已复核，SOC/费用与旧月度汇总一致；没有补造
  旧月度run目录。完整来源、结果及文件SHA-256见审计文档第6节。

关键实现集中在：

- `src/microgrid/problem/q3.py`
- `src/microgrid/problem/q3_solver.py`
- `src/microgrid/problem/q3_controller.py`
- `src/microgrid/problem/q3_replay.py`
- `src/microgrid/problem/q3_rolling.py`
- `src/microgrid/problem/q3_artifacts.py`
- `src/microgrid/problem/q3_sidecars.py`
- `src/microgrid/problem/q3_terminal_reserve.py`
- `scripts/rebuild_q3_settlement_evidence.py`

详细语义与证据见：

- `docs/q3_model.md`
- `docs/q3_interface_decision_addendum.md`
- `docs/q3_solver_preflight.md`
- `docs/q3_solver_readiness.md`
- `docs/q3_real_data_audit.md`

## 3. 不要自行更改的已确认口径

- PV：`VERSION-B + WEIGHT-B + HISTORY-A`；权重为历史 MAE 平方倒数，
  epsilon 为1 kW。
- 重采样：`RESAMPLE-LIN` 为主方案，PCHIP 仅作敏感性对照。
- 负载：`LOAD-A + LF-A + LOAD-HORIZON-A`；只在00/06/12/18发布不可变的
  未来30小时版本，中间10分钟 MPC 只切片，不重新预测。
- 价格：Q4 主语义为 `PRICE-A`，但 Q4-3 尚未批准和实现。
- 调整结算：`SETTLE-A`，相对上一版已确认计划逐次记录 delta。
- PV 尾部：`TAIL-EXP2`，使用过去1至7天同 slot、2天半衰期的归一化权重；
  `TAIL-MEAN7` 仅作敏感性对照。
- 过剩归属：`ATTR-PV-FIRST`。
- 实际回放：`DISCHARGE-CURTAIL-PV-FIRST`。供给不足时先向下削减计划充电，
  仍不足才紧急购电；供给过剩时先记合同电未利用，再向下削减计划放电，最后弃光。
  不得增加充放电动作、反转方向或在 replay 中重新求解。
- 所有可见信息统一满足 `available_at <= decision_time`，不得把未来 actual 传入计划器。
- 本会话明确批准的`Q3-TERMINAL-RESERVE-24H`只限12月31日至年度终点所有窗口
  状态边界SOC>=6000；不扩大到此前日期、合同权限、安全裕度或Q4。

如果真实全年运行失败，应先保存失败证据并定位实现问题；不要为了让结果通过而自行
改变上述 decision。确实需要更改口径时，必须回到团队进行人工确认。

## 4. 首次全年诊断失败

运行标识：`q3-full-validation-20260913`

运行目录：

```text
outputs/runs/q3/q3-full-validation-20260913/
```

日志：

```text
outputs/logs/q3-full-validation-20260913.log
```

启动命令是：

```powershell
.venv\Scripts\python.exe -m microgrid run --case q3 --run-id q3-full-validation-20260913
```

该进程从12:04运行至13:03左右，最终以退出码2结束。输入检查通过，但某个滚动窗口
返回 HiGHS status 8 / infeasible。目录中保留 `manifest.json` 和 `failure.json`，日志
反复出现的 `HighsMipSolverData::transformNewIntegerFeasibleSolution tmpSolver.run();`
是 HiGHS 底层输出，不是独立的失败原因。

本次没有生成以下成功产物：

```text
summary.json
domain_result.json
forecast_provenance.jsonl
plan_versions.jsonl
settlement_ledger.jsonl
```

下一次全年成功后必须核对：

1. `status`/`validation_ok` 为成功；
2. 全年区间数、日期范围和输入哈希正确；
3. SOC 全程在边界内、跨日连续，年度最终 SOC 为6000 kWh；
4. 没有同时充放电、负购电、未来信息泄漏或能量守恒残差；
5. 三份 sidecar 都存在，manifest 中的相对路径和 SHA-256 与文件一致；
6. `100 -> 80 -> 100` 之类计划变化保留完整版本和两笔交易，不能只留净变化。

本次失败证据已经保留：

```text
outputs/runs/q3/q3-full-validation-20260913/failure.json
outputs/runs/q3/q3-full-validation-20260913/manifest.json
outputs/logs/q3-full-validation-20260913.log
```

不要用 `xfail`、放宽物理阈值或删除失败目录来掩盖问题。

原失败异常没有记录窗口的 `decision_time`。接手后已在 `run_q3_day` 的
`Q3WindowSolveError`处补充decision time、日期、slot、当前SOC、terminal mode和
窗口长度；这些字段保留在runner两份失败文件的error message中。该改动仅增加
可观察性，未改变任何模型约束。原证据包及三份失败文件哈希见
`docs/q3_real_data_audit.md`第5.1节。

本地诊断`q3-context-repro-20260913`从2月1日、SOC=6000连续重跑，在12月31日22:10
（slot 133）失败，SOC=2657.079008047366 kWh，年度硬等式窗口剩11格。18:00合同
冻结、禁止紧急充电时，期末SOC上界仅5957.055995045398 kWh；上一格实际负载偏高
导致获批charge curtailment。重建窗口、重复MILP、上一格独立验证和回放均已复核。
证据及全部文件哈希见审计文档第5.2节。本地失败目录、窗口、ledger、上一格解、
可达性证书及日志都在忽略的outputs中；原Windows失败时刻仍未知，不倒填其记录。

## 5. 原始数据与本机准备

原题包只在本机：

```text
D:\2863949663\CUMCM2026Problems.zip
```

原始附件和模板被 `.gitignore` 排除，不会跟随分支交给下一台电脑。接手人若不在当前
电脑，需要取得相同题包并通过仓库导入器导入，不能把题包或原始附件提交到 Git。

Q3 所需源文件哈希：

- 附件1：`66b87134f5ecccd68184d3539bb1293ef039f9e0fdd955a589b9bfa7f227c377`
- 附件2：`2e95fd446bfafa0d8c59577b5c2e2ea8b3f1def20dde54a3062556f4da9b4c72`
- 附件3：`8a61b06c52bd0d639a1cc37c61a7d9f5b75edcbca718f64c1bd3498ec9f9d843`

导入时曾发现并隔离5份 A 题同名附件，位置为忽略目录：

```text
outputs/ingest-quarantine-20260913/
```

模板还存在6处跨午夜字面时间警告；在 `D_TIME_TEMPLATE_EXPORT` 的 Q3 导出口径批准
前不要自动修模板。

## 6. 接手后的最短行动顺序

1. 同步审核分支，检查无原始数据、模板或outputs混入Git；不要自动合并或覆盖他人改动。
2. 审核审计文档第5节原失败证据、第6节明确批准后的全年轨迹及派生结算证据，
   区分`6e5ca4e`的真实全年运行和`94f04c5`的写出重建，不扩大模型修改。
3. 完整2月和全年证据已补入审计文档，readiness与本交接已更新；人工审核仍待完成。
4. 运行完整质量检查：

   ```powershell
   uv run --locked ruff check .
   uv run --locked ruff format --check .
   $env:MICROGRID_RUN_SOLVER_TESTS="1"
   uv run --locked pytest -q
   ```

5. 文档与必要修复已提交审核PR #3，请团队人工核验代码、全年及派生证据。
6. 团队审核通过前，不得把该 run 写入 `configs/selected_runs.toml`。
7. `result3.xlsx` 的正式导出仍被 Q3 模板时间映射口径阻断；批准后再实现导出层，
   runner 不直接操作 Excel。
8. `D_MODEL_Q4_3` 未批准前，不要开始 Q4-3 正式模型。

## 7. 接手完成的判定

只有同时满足以下条件，才算完成 C 线当前阶段：

- 全年真实数据运行完成并通过独立验证；
- 运行 manifest、summary、domain result 和三份 sidecar 完整、哈希一致；
- 全量质量检查通过；
- 真实数据审计文档已更新并经过团队复核；
- 正式 run 的选择和 Excel 导出均由团队按 gate 另行批准，没有被自动越过。

当前技术运行与独立审核已完成，提交团队审核不等于团队已经批准。
