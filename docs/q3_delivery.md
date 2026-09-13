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

## 已完成的交付核验

实现提交`054668a`在干净工作树生成正式目录，manifest为success、validation_ok=true、
is_synthetic=false、diagnostic_only=false、formal_delivery=true，明确标注
artifact_regeneration_only=true及原轨迹/修正账本来源。正式选定Q3检查阻断项为[]。
新增来源范围只覆盖该Q3交付，不伪称重新求解，也不把原诊断manifest改为正式状态。

- 全年48096格、实际初始/最终SOC均6000，跨日连续，末日145个状态边界检查通过。
- 122785个单元格独立读回通过，包括两表各48096格、2004行实际充放电块、
  4647行逐日紧急购电记录及费用/状态/原标签/日期/合并范围。
- 全部原始输入未变，三份sidecar原字节不变，完整费用与原汇总在既有阈值内一致。
- 正式库存全部文件哈希、论文资产哈希及发布后的同源文件核对通过。
- 原Q1导出快照status/choice/confirmed_by/confirmed_at逐字段核对未变。
- ruff lint/format通过，启用可选solver测试的全部pytest为303 passed，无跳过。
- `paper/sections/07_q3.tex`补齐模型、算法、结果、预报引入讨论及局限；同源表图
  来自选定正式目录，未编造单版本反事实节费结果。Q3独立PDF共6页，LaTeX编译成功，
  无undefined references、missing characters、overfull或致命错误。

交付位置：`outputs/runs/q3/q3-formal-delivery-20260913/results/result3.xlsx`；
同源CSV、表图及报告在该目录`paper_assets/`；独立论文为`paper/build/q3_review.pdf`。

| 文件 | SHA-256 |
|---|---|
| `result3.xlsx` | `c9c4a22901aacd906449b746b8b0ef24009ccbbb398c338cb707d40776b0d012` |
| 正式`manifest.json` | `50377eeb3355830b0129d18f9e7e63b304a2bbc4b955c5e7c187b86e06bb74cd` |
| 正式`domain_result.json` | `4d3561ef9c3680797bd1c3c0ead107bdd92159badb8e329db97401aeed8b3075` |
| 正式`validation.json` | `a5cf6226b2886ff08e2843b09cb632ac4bb427b6815837427fa710485a6a659e` |
| `export_manifest.json` | `52c16e3b27ac22762dffd967b4b8d86b1816bd7507c3769a70b75784c0c99c68` |
| `paper_assets/asset_manifest.json` | `23c15823781adb27891256f28233af0daaad2e7c0548b111b1072d4280d2302f` |
| `paper/build/q3_review.pdf` | `9e8627a84a06642975f50511489173fdf875bce3d64651cdd36a9c17b999e68b` |
| 导出日志 | `e4412ccce51011699937b8e3a62ebe1a86e2d6247b83b6d9a8b3283a28200cf9` |
| 论文编译日志 | `90f143dddee32f14b1d57e46e9f1ca4af51053bf717bb4feb7ef87bb38d53433` |

其余文件SHA-256完整记录在正式manifest/资产清单。AI人工核验和代码PR人工审核仍
待完成，不自动合并。全局final仍受其他case、评价及整个论文审批约束，不据此放行。
