# 数据字典（Stage 0）

## 原始文件定位

| 逻辑名 | 本地路径 | 说明 |
|---|---|---|
| C题 PDF | `resources/C题.pdf` | 题目原文，只读 |
| 附件1 | `data/raw/附件1.xlsx` | 单日电价、负载、光伏预测 |
| 附件2 | `data/raw/附件2.xlsx` | 全年负载、光伏实际功率 |
| 附件3 | `data/raw/附件3.xlsx` | 全年 4 个发布时刻的 24 小时光伏预报 |
| 附件4 | `data/raw/附件4.xlsx` | 全年波动电价 |
| result1 | `data/templates/result1.xlsx` | 单日结果模板 |
| result2 | `data/templates/result2.xlsx` | 多日结果模板 |
| result3 | `data/templates/result3.xlsx` | 多日调整模板 |
| result4-2 | `data/templates/result4-2.xlsx` | 波动电价 Q2 模板 |
| result4-3 | `data/templates/result4-3.xlsx` | 波动电价 Q3 模板 |

原件不上传 Git；`records/inputs_manifest.json` 记录相对路径、大小、SHA-256 和导入时间。

## 原始观测记录（建议字段）

| 字段 | 含义 |
|---|---|
| `source_file` | 仓库内相对路径 |
| `sheet_name` | 工作表名 |
| `cell_ref` | 源单元格坐标，例如 `附件2.xlsx!小区负载!C42` |
| `source_hash` | 源文件 SHA-256，便于回查版本 |
| `source_date` | 源日期（若有） |
| `raw_time_label` | 原样时间/区间标签 |
| `parsed_day_offset` | 相对于基准日的天数偏移 |
| `parsed_minute_of_day` | 当天分钟偏移，0–1439；24:00 规范到次日 0:00 |
| `parsed_timestamp` | 仅在源记录明确带有日期时生成；附件1不生成伪造日期 |
| `value` | 原值 |
| `unit` | 题面单位：kW、元/kWh、kWh 等 |
| `kind` | `load_kw` / `pv_actual_kw` / `pv_forecast_kw` / `price_cny_per_kwh` 等 |

## 预报记录（附件3）

| 字段 | 含义 |
|---|---|
| `issue_time` | 该行 `日期` + `预报时刻` |
| `lead_hours` | `预报1小时` 中的 1…24 |
| `valid_time` | `issue_time + lead_hours`，只做索引还原，不插值 |
| `pv_forecast_kw` | 预报功率，kW |
| `source_ref` | 源文件、sheet、单元格 |

同一 `valid_time` 的不同 `issue_time` 必须全部保留，不得相互覆盖。
附件3 后续行的空字符串日期，只允许用同一 4 行预报块的非空日期做局部填充；不得对预报值 ffill。

## 时间标签

- Excel 日期系统按工作簿实际 epoch 解释；测试需覆盖 1900 与 1904 两种。
- 支持 `datetime`、`date`、Excel 序列值、`HH:MM`、`24:00`、`0:00+1`、`HH:MM+1` 及区间两端。
- `parsed_minute_of_day` 跨日不取 `% 24`；`0:00+1` 表示次日 00:00。
- 区间标签 `0:00-0:10+1` 在 result2/3/4 中出现，如果按字面两端解析会得到超过 24 小时的区间，应标记为异常/待确认，不得自动改写为 `0:00+1-0:10+1`。

## 模板结构（只读观测）

- result1：2 个 sheet，计划网格 144 个 10 分钟标签，充放电 6 个四小时段。
- result2/result4-2：3 个 sheet；计划网格 334 个日期行 × 144 个时间列；充放电量与紧急购电量只有示例块。
- result3/result4-3：4 个 sheet；计划购电量与调整购电量有完整日期网格；充放电量、紧急购电量只有示例块。
- 五个模板均没有公式、批注、合并单元格或数据验证。
- 模板 `docProps/core.xml` 带有官方模板创建者信息；写入输出时应避免把身份信息带入正式交付物，同时不得修改原件。

## 结果模板契约

Stage 0 只允许：
- 生成空白预览到 `outputs/template_preview/`；
- 或者把合成记录写入 `tests` / `outputs/smoke/` 下的合成模板，文件名带 `SMOKE_`。

正式时间映射确认前：
- 不得向真实模板写入声称有效的数值；
- 不得扩大示例块、推断 `⁝` 行的实际行数；
- 不得用 0、NaN 或空字符串掩盖缺失结果。
