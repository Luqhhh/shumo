# Q3 正式交付范围与复现

2026-09-13，本会话用户对以下限定方案明确回复“批准”：自然日144格按模板
B:EO列序写入、保留原标签，调整表写最终确认量、调整费用逐笔累计，完整展开
充放电与紧急购电表；以已审核修正证据生成`q3-formal-delivery-20260913`，作为
Q3的选定正式来源。此前原诊断禁止直接选入的限制未被绕过：原目录及其诊断
manifest不动，另建有显式用户授权、完整来源绑定的新交付目录，且不是新MPC运行。

批准范围补入既有approved的`D_TIME_TEMPLATE_EXPORT`的`q3_*`专用字段。
原Q1 choice/status/confirmed_by/confirmed_at均不改，保留原Q1导出快照兼容。
Q3独立gate同时要求原decision完整批准、专用scope/mapping和非空补充批准来源。
`selected_runs.toml`只新增Q3选定run及其case级明确批准；全局pending保持，不批准
Q2/Q4、D_EVAL或整个final提交。AI人工核验仍pending，不自动合并PR。

## 模板映射

| 位置 | 同源数值 |
|---|---|
| 计划/调整表row=i+2、B:EO | 2月1日至12月31日；自然日slot0..143 |
| 计划表144格、EP/EQ | 00:00版本0、计划总kWh、计划费用 |
| 调整表144格、EP/EQ | 最终确认购电量、最终总kWh、逐版调整费用（不按净差计算） |
| 充放电表row=2+6*i至7+6*i、C/D | 六个四小时块的实际充/放电kWh |
| 每个充放电块首两行E/F | 0:00/24:00及真实日初/日末SOC；日期六行合并 |
| 紧急表 | 每日相邻正紧急区间合并，真实起止/总kWh；不跨日，零事件保留日期/空区间/0 |

模板B1为`0:10-0:20`，EO1为`0:00-0:10+1`，与内部自然日网格有字面冲突。
本次采用明确批准的列序而非标签推导；原144个表头和日期全部保留，完整真实
区间/原标签/目标cell映射写入export_manifest。费用总计含紧急费写summary和论文表，
计划EQ与调整EQ不是总费用。原始模板只读；只生成新副本，并逐格独立读回。

## 来源与命令

真实全年轨迹来自`q3-terminal-reserve-validation-20260913@6e5ca4e`，修正结算
来自`q3-terminal-reserve-evidence-20260913@94f04c5`，独立审计报告SHA-256为
`c7451872268e57949164d476fb1999270ffae2be135c1781dd08021aa5bad840`。
原年度及失败证据与哈希详见`q3_real_data_audit.md`第5—6节。
正式交付复制已审核三份sidecar并改写新的run identity/来源metadata；所有实际区间、
动作、SOC、计划链和费用不变，manifest显式记录artifact_regeneration_only/no MPC rerun。

```bash
uv run --locked python scripts/export_q3_run.py \
  --source-run-id q3-terminal-reserve-evidence-20260913 \
  --run-id q3-formal-delivery-20260913 \
  --audit-report outputs/evidence/q3-terminal-reserve-validation-20260913/independent_audit.json
```

完整交付应包含result3.xlsx、domain_result、summary、validation、三份sidecar、
export_manifest、正式run manifest及同源论文表/四展示日图/CSV/资产哈希清单。
源文件、已有交付目录或输出文件均不得覆盖；失败要保留新目录的failure/manifest/日志。
不更改模型、已批准forecast/结算/回放口径或物理容差，不开始Q4-3。
