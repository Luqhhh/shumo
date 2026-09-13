# Q2 交接 brief（完整版）

**读者：接手 Q2 剩余工作的 Codex。** 本文所有"已核验"结论均由 `local/q2-perf/verify_*.py` 与 `local/q2-perf/check_r11.py` 在 2026-09-13 实际跑出，脚本可重跑复现。

---

## 0. 一句话状态

Q2 的**数值计算已全部完成且合格**：全年 334 天滚动 MPC 跑通，`status=success`、`is_synthetic=false`、`validation_ok=true`、48096 个决策区间，账目闭合到浮点精度。

**卡住交付的不是模型，是答案文件生不出来。** 三个阻断：

1. `data/templates/` 里 result1/2/3 放的是 **A 题**的模板（q4_2/q4_3 是对的）—— 修复需用户操作或明确授权；
2. `export_case_result()` **只实现了 Q1**，q2 直接抛 `NotImplementedError`；
3. 两项指标未达标（总计超 18.6 万、紧急超 68.6 万），且**已用三个完整全年运行证明当前确定性 MILP 下无解**，不要再去调参。

---

## 1. 仓库、环境与硬约束

- 仓库 `/home/clairvoyant/code/shumo`，分支 `codex/b-q2-input-adapter`，HEAD `9c37b9e`。
- Python 3.11 + uv。一律 `uv run --locked python -m microgrid ...`。
- **从 Windows 侧调用必须包一层**：`wsl.exe -d Ubuntu -- bash -lc "<cmd>"`。直接 `cd /home/...` 在 Git Bash 里不存在，会 exit 1。
- **`bash -lc "..."` 会吞掉 `$VAR`** —— 我在本次交接中因此栽了两次（`cat $R/summary.json` → `cat /summary.json`）。带变量的逻辑请**写成脚本文件再跑**，不要内联。
- 长任务用 `setsid nohup ... < /dev/null &` 脱离。

**硬约束（来自 `AGENTS.md`，逐条遵守）：**

- **不得修改 `data/raw/`、`data/templates/`、`resources/`、`CUMCM2026Problems/`。** 这直接决定阻断 1 必须由用户来做或明确授权。
- 不得修改 `configs/decisions.toml` 的 approved 状态，**不得代填 `confirmed_by` / `confirmed_at`**。
- 不得伪造结果、文献或 AI 核验。
- `is_synthetic=true` 的产物不得进入 `dist/`。
- 不得绕过 InfoSet 因果隔离（未来 actual 只用于回放结算）。
- 不得上传原始数据。
- 不得 `reset --hard`、不得强推、不得删除用户原始数据。
- **每次 push 都需要用户重新明确确认。**

---

## 2. Q2 当前情况

### 2.1 正式运行 `r11` 已完成

目录 `outputs/runs/q2/q2-2025-approved-20260913-r11/`，产物清单（字节数为实测）：

| 文件 | 字节 |
|---|---:|
| `forecast_records.json` | 2,181,670,874 |
| `domain_result.json` | 43,338,928 |
| `solver_records.json` | 29,551,998 |
| `input_snapshot.json` | 18,438,253 |
| `solver.log` | 1,827,683 |
| `manifest.json` | 26,778 |
| `summary.json` | 791 |
| `effective_config.json` | 788 |
| `validation.json` | 95 |

`manifest.json` 关键字段（实测）：

```
status = 'success'
is_synthetic = False
validation_ok = True
model_status = 'implemented'
case_id = 'q2'
run_id = 'q2-2025-approved-20260913-r11'
action_start_day = '2025-02-01'
action_end_day = '2025-12-31'
input_verification_issues = []
```

**`result_files` 共 8 项，全是 json/log，`.xlsx` 列表为空** —— 这正是阻断 2 的表现。

`formal_r11.log` 末行：`case q2: status=success run_id=q2-2025-approved-20260913-r11`。当前 `ps` 无任何 microgrid 进程。

### 2.2 四项费用（`summary.json` 原文）

```json
"costs": {
  "planned_cost_cny":     13500196.168395123,
  "adjustment_cost_cny":  0.0,
  "emergency_cost_cny":   1685776.1411151376,
  "total_cost_cny":       15185972.30951026
}
```

| 项 | 元 | 万元 |
|---|---:|---:|
| 计划购电 | 13,500,196.17 | **1350.0** |
| 紧急购电 | 1,685,776.14 | **168.6** |
| 调整购电 | 0.0 | 0 |
| **总计** | 15,185,972.31 | **1518.6** |

调整购电为 0 是**设计如此**，不是漏算：`D_SETTLE` 第 4 条规定 Q2 的 `q_adjusted_t = q_plan_t`。

### 2.3 会计与守恒量（`summary.json` 的 `accounting`）

```
interval_count              = 48096
e_plan_kwh                  =    807,357.5951616482
e_realized_kwh              =    471,973.68859546207
pv_available_kwh            = 19,068,889.994283173
pv_used_kwh                 = 16,807,935.050847158
pv_curtail_kwh              =  2,260,954.9434361043
max_pv_partition_residual_kwh = 0.0
max_abs_ledger_residual_kwh   = 4.547473508864641e-13
pv_accounting_policy        = "grid_and_discharge_first_residual_pv"
```

两项守恒残差都干净：PV 分流残差恰好 0，账目残差 4.5e-13（浮点极限）。且 `pv_used + pv_curtail = 19,068,889.99 = pv_available` ✓。

> **字段语义陷阱（我本人在此栽过，请注意）：** `e_plan_kwh` **不是合约电量，也从不计费**。见 `src/microgrid/problem/q2_engine.py:54` 的 docstring —— 它是 MILP 在**预测场景**下对已承诺首步动作算出的规划期 recourse 量，仅用于规划，不进入 `IntervalResult` 契约。真正被计费的是 `e_realized_kwh`，即回放时对**实际**负荷/光伏不得不买的紧急电量，按 5 倍时段电价计一次，是 `emergency_cost_cny` 的唯一来源。
>
> 我一开始用 `planned_cost_cny / e_plan_kwh` 算出 16.7 元/kWh，据此怀疑单位有问题 —— **那是除错了字段，该疑虑已撤回。** 正确的自洽验算：`1,685,776.14 / 471,973.69 = 3.572 元/kWh`，除以 5 倍得时段均价 ≈ 0.714 元/kWh，完全合理。

### 2.4 指标达成情况

| 指标 | 目标 | 实际 | 结果 |
|---|---:|---:|---|
| 总费用 | ≤ 1500 万 | 1518.6 万 | **超 18.6 万** |
| 紧急购电 | ≤ 100 万 | 168.6 万 | **超 68.6 万** |

### 2.5 裕度扫描 —— 无解的证明（**不要重复跑**）

三个点都是**完整全年运行**，不是估计：

| α (`purchase_margin`) | 计划(万) | 紧急(万) | 总计(万) |
|---:|---:|---:|---:|
| 0.050（= r11） | 1350.0 | 168.6 | 1518.6 |
| 0.060 | 1374.8 | 147.6 | 1522.4 |
| 0.075 | 1412.2 | 123.1 | 1535.3 |

结论：**总计随 α 单调上升，紧急随 α 单调下降，两条约束没有可行交集。** 用 0.06→0.075 段外推，要把紧急压到 100 万需 α≈0.089，届时总计约 1547 万 —— 比现在还差。**不要再花时间扫 α。**

### 2.6 配置与生效值

`configs/q2_run.toml`（已生效）：

```toml
[q2]
purchase_margin  = 0.05
forecast_lags    = [7, 14, 21, 28]
forecast_weights = [0.25, 0.25, 0.25, 0.25]
load_bias_window = 4
load_bias_scale  = 0.8
pv_lags          = [1, 2, 3, 4, 5]
pv_weights       = [0.35, 0.25, 0.2, 0.12, 0.08]
```

`effective_config.json`（**实测，全部已生效**）：

```json
{
  "action_start_day": "2025-02-01",
  "action_end_day": "2025-12-31",
  "is_synthetic": false,
  "forecast": {
    "model_version": "q2-weekly-ar1-v2",
    "lags": [7, 14, 21, 28],
    "weights": [0.25, 0.25, 0.25, 0.25],
    "pv_lags": [1, 2, 3, 4, 5],
    "pv_weights": [0.35, 0.25, 0.2, 0.12, 0.08],
    "ar1_phi": null,
    "load_bias_window": 4,
    "load_bias_scale": 0.8
  },
  "settlement": {
    "purchase_margin": 0.05,
    "terminal_value_cny_per_kwh": 0.6895775
  },
  "model": { "horizon_steps": 144, "solver_time_limit_s": null },
  "annual": { "annual_terminal_soc_kwh": 6000.0, "annual_endpoint_day": "2025-12-31" }
}
```

`configs/selected_runs.toml`：`q2 = "q2-2025-approved-20260913-r11"`，`selection_status` **有意保持 `"pending"`**（改成 `"approved"` 会做出全局发布声明，而 q3/q4 未完成、论文仍有占位符、缺 AI 详情 PDF，该声明目前不成立）。

### 2.7 预测模型（本轮改进）

- **负荷**：7/14/21/28 周滞后等权（各 0.25）+ 近 4 天残差偏差校正 ×0.8 → RMSE **262 → 182 kW**
- **光伏**：改用 1–5 日短滞后、权重递减（光伏受天气驱动，周滞后预测很差）→ RMSE **447 → 305 kW**
- 全部严格因果；旧默认值未动，新行为通过 config 显式启用。

代码侧配套改动（`src/microgrid/problem/q2.py`）：新增 `_load_q2_run_settings()` 与 `_Q2_SETTING_DEFAULTS`，解析层级为 **metadata > `configs/q2_run.toml` > 代码内默认值**；`effective_config.json` 改为从**真正交给引擎的那一份 config 对象**导出（原来是一份只有 7 个手写字段的 dict，根本读不回预测模型），config 也只构造一次而非五次。

### 2.8 测试

`tests/test_q2_runner.py` 已随新 schema 更新：Q2 相关 **57 个测试通过**，decision/approval **20 个通过**。

---

## 3. 阻断链（按处理顺序）

### 阻断 1 —— `data/templates/` 里是 **A 题**的模板（需用户操作或授权）

已用 sha256 逐字节对齐，**不是"同一文件复制两遍"**：

| `data/templates/` | 字节 | sha256[:16] | sheet | 实际是 |
|---|---:|---|---|---|
| `result1.xlsx` | 10,073 | `23b261b295c1b787` | `['温度','水分浓度']` | **A题/附件3/result1.xlsx** |
| `result2.xlsx` | 10,073 | `23b261b295c1b787` | `['温度','水分浓度']` | **A题/附件3/result1.xlsx** |
| `result3.xlsx` | 9,345 | `07e4793d620a7f89` | `['Sheet1']` | **A题/附件3/result3.xlsx** |
| `result4-2.xlsx` | 18,397 | `1c26494cfc6d754e` | `['计划购电量','充放电量','紧急购电量']` | ✅ 正确 |
| `result4-3.xlsx` | 265,429 | `c59da470cabd0be2` | `['计划购电量','调整购电量','充放电量','紧急购电量']` | ✅ 正确 |

**受影响的是 q1 / q2 / q3，q4_2 与 q4_3 的模板本来就是对的。**

正确原件在 `CUMCM2026Problems/C题/附件/附件5/`：

| 目标 | 复制自 | 字节 | 期望 sha256[:16] |
|---|---|---:|---|
| `data/templates/result1.xlsx` | `附件5/result1.xlsx` | 13,323 | `28360e0974e7d606` |
| `data/templates/result2.xlsx` | `附件5/result2.xlsx` | 18,397 | `1c26494cfc6d754e` |
| `data/templates/result3.xlsx` | `附件5/result3.xlsx` | 265,429 | `c59da470cabd0be2` |

（官方包里 `附件5/result2.xlsx` 与 `result4-2.xlsx` 本身就逐字节相同，`result3` 与 `result4-3` 同理 —— 那是官方自己的安排，不是错误。）

**判据：** 复制后跑仓库自带的 `validate_template()`，q1/q2/q3 应全部由 `ok=False` 转为 `ok=True`。当前实测：

```
q1   ok=False  got ['温度','水分浓度']
q2   ok=False  got ['温度','水分浓度']
q3   ok=False  got ['Sheet1']
q4_2 ok=True
q4_3 ok=True
```

> `validate_template()` 位于 `src/microgrid/excel_export.py`，**已经把官方每个 case 的布局期望写全了**（表头文字、首末时段标签、日期列位置、`last_interval_col = max_column - 2`）。改完模板先跑它，不要凭肉眼判断。

### 阻断 2 —— `export_case_result()` 只实现了 Q1

`src/microgrid/excel_export.py`，`export_case_result()` 内：

```python
if case_result.case_id != "q1":
    raise NotImplementedError(
        f"numeric template export is implemented only for Q1, not {case_result.case_id!r}"
    )
```

**决策闸门这一层不用等**：`D_TIME_TEMPLATE_EXPORT` 已核验为 `status='approved'`，`confirmed_by='参赛队（用户批准）'`、`confirmed_at='2026-09-10'`。纯粹是代码没写。

**另外有个现成 bug：** 该函数默认落盘路径把文件名**硬编码成了 `result1.xlsx`**，给 q2 用会写错名字，需改成 `TEMPLATE_BY_CASE[case_id]`。

实现 q2 writer 时请照抄已有 `_export_q1` / `_validate_q1_export` 这对的做法：**先写，再从落盘文件逐格读回比对，不一致就抛**。

**q2 模板布局**（上一轮用 openpyxl 读出，**本轮未复验，请自行复跑确认再动手**）：

- `计划购电量`：范围 `A1:EQ335`，共 147 列。`A1='日期\时间'`；时段标签**按右端点标注**，`B1='0:10-0:20'` … 末位 `'0:00-0:10+1'`；**最后 2 列为 `全天购电量`、`全天购电费`**（即最后一个时段列是 `max_column - 2`，与 `validate_template` 的算法一致）。`A2:A335` 为日期，`2025-02-01` → `2025-12-31`，**正好 334 天，与 r11 的动作区间完全吻合**。
- `充放电量`：`A1:F20`，表头 `['日期','时间段','充电量','放电量','时刻','储电量']`；`时刻` 列含 `time(0,0)` 与字符串 `'24:00'`。**每天 6 个四小时块，但模板只留了 20 行（约 3 天）；写满全年需扩到 334×6+1 = 2005 行** —— 这是 q2 相对 q1 的主要新增工作量。
- `紧急购电量`：`A1:C11`，表头 `['日期','购电时间段','购电量']`；模板是示意块（行间有 `⁝` 省略符），写全年同样需重整行。

**q1 与 q2 的布局不同**，别把 q1 writer 直接套过来：q1 的 `计划购电量` 是 `A1='时间段'`、`B1='购电量'`、时段在 A 列按**行**方向排 144 行；q1 的 `充放电量` 的 `时刻`/`储电量` 在 **D/E** 列，q2 在 **E/F** 列。

### 阻断 3 —— 指标未达标，已证明当前模型下无解

见 2.5。**调参无出路。**

唯一有原则的修法是**风险感知 / 场景 MILP**：让电池为预测误差预留备用，把 5 倍的紧急购电换成 1 倍的预购电。量级依据：每时段电池在 5000 kW 功率下可充放 833 kWh，而净预测误差约 354 kW，所以留备用是划算的。代价是决策变量从约 1007 增到约 2307，求解时间约 2–3 倍（全年单次约 30 分钟 → 60–90 分钟）。

用户此前明确选择"先交当前最优（α≈0.05）"，**此项为延期项**。

---

## 4. 其他已核验事实

- **`origin/main` 不能直接合。** 已 fetch 并试合过，在 `configs/decisions.toml` 与 `tests/test_release_guards.py` 冲突，已 `git merge --abort` 中止。原因是架构性分叉：**`origin/main` 的 `q2.py` 只有 23 行、直接抛 `ModelNotImplementedError`，且 `problem/` 下没有 q2_model / q2_engine / q2_forecast / q2_inputs / q2_replay 任何一个文件**；main 还从 `decisions.toml` 删掉了 `D_MPC` / `D_TERMINAL` / `D_YEAR_BOUNDARY`。**当前分支是仓库里唯一可用的 Q2。** 用户当时的决定是"保留当前分支，不动 main"。
- **Q1 也没有 run。** `outputs/runs/q1/q1-a-formal-003/` 目录**不存在**，但 `selected_runs.toml` 里登记着它。同一个模板缺陷也阻塞 Q1 导出，所以修模板的收益覆盖 q1/q2/q3。
- `collect_blockers(mode='final')` 当前完整输出：
  - `D-MODEL-Q3` / `D-MODEL-Q4-2` / `D-MODEL-Q4-3` / `D-EVAL` 四个决策仍为 `proposed`
  - `selected_runs.toml selection_status is not approved (got 'pending')`
  - `case q1: selected run manifest not found: outputs/runs/q1/q1-a-formal-003/manifest.json`
  - `case q2: selected run has no owned result file result2.xlsx`
  - `case q3 / q4_2 / q4_3: no selected run_id`
  - 论文 11 个文件仍有 `\PH{` / `占位` / `待计算` / `待确认` 占位符（`00..11` 各节、`preamble.tex`、`settings.tex`、`appendices/*.tex`）
  - `missing AI details PDF: AI 工具使用详情.pdf`
- **`D_MODEL_Q2` 需要用户重新确认。** 其 `choice` 文本已改写为描述实际跑的模型，但 `status` / `confirmed_by` / `confirmed_at` **有意未动**（仍为 `approved` / `参赛队（用户批准）` / `2026-09-12`）。**Codex 不得代填这三个字段。**

---

## 5. 建议的处理顺序

1. **先只处理阻断 1 并验证**：请用户把 `CUMCM2026Problems/C题/附件/附件5/` 的 `result1/2/3.xlsx` 复制进 `data/templates/`（或明确授权你复制）。然后用 `validate_template()` 确认 q1/q2/q3 全部 `ok=True`。**模板正确之前，写 q2 writer 无法验证 —— 不要先写代码。**
2. 实现 q2 writer + readback 校验；顺手修掉默认落盘文件名硬编码 `result1.xlsx` 的 bug；跑 `export_case_result()` 生成 `result2.xlsx`。
3. 在修好的模板上一并跑通 q1 的 writer（同一批修复的顺带收益）。但注意 **q1 缺 run 目录**，要先生成 Q1 的正式 run。
4. 若时间允许再做阻断 3 的风险感知 MILP；否则直接进入论文与交付流程。
5. 交付前决定 2.18 GB 的 `forecast_records.json` 是否保留在 run 目录里。

---

## 6. 附录：复现本文所有结论

`local/q2-perf/` 下四个核验脚本（**建议保留**，只读、无副作用）：

| 脚本 | 作用 |
|---|---|
| `verify_state.py` | run 目录产物清单、manifest 关键字段、模板 sheet、导出器覆盖、`forecast_records.json` 体积 |
| `verify_templates.py` | 逐字节 sha256 对齐 `data/templates/` 与官方包，定位错放来源 |
| `verify_validate.py` | 对 5 个 case 跑仓库自带 `validate_template()`，给出当前通过/失败矩阵 |
| `verify_gates.py` | 决策状态、required decisions、selected_runs、`collect_blockers(mode='final')` 全量阻断清单 |
| `check_r11.py` | r11 完成度 + `purchase_margin` 是否生效 + 残留进程 |

统一调用方式：

```
wsl.exe -d Ubuntu -- bash -lc "cd /home/clairvoyant/code/shumo && uv run --locked python local/q2-perf/<script>.py"
```

**可清理的过期产物**：`local/q2-perf/annual_a090.log`、`local/q2-perf/annual_.log`、`local/q2-perf/rss_samples.txt`。
