# 当前稿件：按最新Q4重写（2026-09-13）

按用户“按最新Q4重写对应部分”的要求，以 `docs/q4_optimization_conclusion.md` 和 `outputs/evidence/q4_all_optimization_completion/selection.json` 的明确采用结论为依据。Q4主方案更新为独立的联合误差情景采购，没有将其他候选叠加成未经回放的新模型。

## 模型与文字

Q4正文按最新模型卡与已冻结运行源码重写：保留原负载、光伏、电价点预测；按过去28日已实现发布--目标联合误差、四个原始时距组、净需求误差20%/80%分位数的最近完整历史行构造低/名义/高情景。七日冷启动及0.25/0.50/0.25权重事前固定。全窗口合同、交易及电池动作共享，情景供需分配与紧急量分别约束，以加权规划费减原名义均值末态价值为目标；实际只执行共同首动作并按原保护和真实交易结算。

合同权限、求解及物理容差、最后24小时6000 kWh预留和硬实际末态保留。该模型是全窗口电池动作共享的受限情景候选，不宣称完整未来追索策略最优。摘要、问题分析、假设、符号表、检验、结论与AI详情同步，题目更新为“基于混合整数规划与情景调度的微网购电优化”。Q1、Q2、Q3的建模口径和Q1数值及图表保持原版本。

## 显式结果来源

| 条件 | 最新主方案run_id | 实际费用/元 | 比同条件点预测基准节省/元 |
|---|---|---:|---:|
| Q4-2每日冻结 | `q4-2-v4-scenario-procurement-annual-main-002` | 15,919,083.43 | 1,262,236.91（7.3466%） |
| Q4-3允许调整 | `q4-3-v4-scenario-procurement-annual-main-002` | 15,402,970.92 | 463,192.69（2.9194%） |

两基准分别为 `q4-2-v3-engineering-annual-main-001`、`q4-3-v3-engineering-annual-main-001`，由相应年度单因素比较的来源哈希显式绑定。其他候选表也读取各自完整非合成回放及绑定汇总文件，未以周费用替代年度费。原点预测及来源逐项相同，情景节费来自采购安排改变；合同余电、弃光和吞吐增加。冻结条件2月、可调条件9月费用略增，正文保留这两处月度负结果。开发年份比较与决策时间因果检验分别说明，未称未见独立测试集。

## 图表、复现与核验

继续采用前次优秀论文参考形成的白底细线、宋体、少量配色、单位及三线表风格。Q4两幅12月21日实际运行图已改为最新情景轨迹；新增情景流程图和实际费用分解/月费用差图。当前清单共八幅图（Q1四幅、Q4四幅），每幅有矢量PDF和300 dpi PNG。原v3证据图、旧价格比较图和旧结果表留存，但新正文使用最新呈现文件。

```bash
MPLCONFIGDIR=/tmp/cumcm-matplotlib .venv/bin/python scripts/build_paper_figures.py \
  --q4-evidence outputs/evidence/q4_all_optimization_completion/selection.json
.venv/bin/python scripts/aggregate_ai_usage.py
.venv/bin/python scripts/build_paper.py --mode draft
.venv/bin/python scripts/build_paper.py --target ai-details --mode draft
(cd paper && latexmk -quiet -xelatex -interaction=nonstopmode -halt-on-error -outdir=build review.tex)
.venv/bin/python outputs/paper_q4_rewrite/validate_current.py
```

绘图入口默认绑定上述明确选择文件，仍可显式传入旧比较证据；不按目录更新时间选择运行。新增 `build_q4_paper_assets.py` 读取各原运行证据清单，对完整预测/合同/执行/账本/求解及三情景原计划哈希、年度物理/控制链审计和Excel来源核对，输出最新表格与图。未重新求解、变更原题或调整配置及批准状态。

独立论文复核逐项读取两条最新实际执行和账本，各48096步，检查供需、储能递推、状态连接、充放电和紧急互斥、动作保护、初始费及紧急费，并重算费用和运行分项；年末各145个状态通过预留下界。图表哈希、摘要数值、结果总量、候选来源和PDF编译检查通过。63个受保护文件哈希不变，包括模型源码、配置、Q1/Q2/Q3章节和Q1图表。两个新增/修改绘图脚本Ruff及格式检查通过。

完整稿27页，阅读版23页（摘要1页、正文及参考文献22页），AI详情2页。无缺字、未定义引用及行溢出，已渲染检查摘要、完整符号表、情景输入与目标、费用分析、候选表和实际运行图，修正长标题、长公式及流程框文字布局。

[完整稿](../paper/build/main.pdf)、[阅读版](../paper/build/review.pdf)、[AI详情](../paper/build/ai_details.pdf)。本次修改前备份和独立核验见 `outputs/paper_q4_rewrite/20260913_105251`；当前图表来源清单为 `paper/figures/figure_manifest.json`。新增AI使用分片已聚合，人工核验仍pending；保留Q2/Q3待补充及全局final原阻断，实验采用不代替全局提交批准。

提交前兼容性：新增 `PaperGraphic` 文件存在检查，本地图文件存在时绘制原图，无图的代码检出仅编译带明确待生成提示的草稿。`paper/figures/` 已加入Git忽略规则。提交范围包括TeX、汇总结果表、绘图程序、本文档与真实AI记录，不包含生成图表、完整PDF、Excel、原始题包或逐格年度材料。

提交前检查通过：锁文件离线一致性、全仓库Ruff和格式检查通过；本地测试376 passed、2 optional skipped（86.02秒）。从暂存文件建立不含本地数据和图表的代码检出，论文草稿与AI详情均成功编译；本地含真实图表完整稿仍为27页。提交前检查另存 `outputs/paper_q4_rewrite/push_validation.json`，Q1新增改动仅为图引用接口，数学口径与全部图文件不变。

桌面交付：已替换 `C:/Users/lqh22/Desktop/数模论文.pdf`，与核验后的27页完整稿SHA-256一致。桌面旧稿已备份在上述本次备份目录中。

---

# 历史记录：论文风格优化与图表补全（2026-09-13，Q4更新前）

## 当前稿件范围

公共部分、问题一和已有问题四正文已统一修订，包括摘要、问题分析、假设、符号表、数据说明、结果分析、检验评价和结论。问题二、三仍保留原待补充内容，不将其对应的波动电价回放当作独立Q2/Q3结果。稿件题目为“基于混合整数规划与滚动预测的微网购电优化”，保留阶段稿标识。

## 参考论文及采用方式

用户提供的桌面优秀论文集为本次体裁参考。PDF均为扫描页，文字抽取为空，因此先渲染页面再审阅。本次实际参考页包括：

- 2024 A053：第1、10、15页，参考按问题组织方法与数值结果、推导后接图表解释的写法。
- 2024 C038：第4、9、14页，参考符号三线表、优化约束的层次及条形图标注。
- 2025 C132：第1、12、18页，参考摘要中的方法与定量结果组织、模型段落和流程图。
- 2025 B060：第7、12、18页，参考白底细线、少量配色、坐标与单位标注、图后结合计算结果讨论。

仅参考体裁和呈现，不移植参考论文的措辞、模型、数据或结论，不把它们列为本题技术依据。现稿压缩重复的“首先／其次／再次”和空泛评价，改为结合设备约束、实际时段与定量结果解释。既有效率、合同、反馈、预测及末态口径均保持原模型含义。

## 图表与结果来源

已替换全部四幅Q1图占位，并统一已有Q4图表，共七幅：

1. `q1_input`：144个区间的负载、光伏预测及电价，上下分图。
2. `q1_workflow`：数据对齐、电量换算、约束构造、联合求解、独立复算与汇总流程。
3. `q1_dispatch`：十分钟购电与充放电计划，充电为正、放电为负。
4. `q1_soc`：145个储电量边界及上下界、初末状态。
5. `q4_2_2025-12-21`：合同冻结条件的同一完整回放日，分列实际功率、电价、合同、动作及储电量。
6. `q4_3_2025-12-21`：允许调整合同条件的同一完整回放日。
7. `q4_price_cost_comparison`：价格MAE和主方案减价格对照的月费用差，保留费用差为正的月份。

各图同时输出矢量PDF和300 dpi PNG，位于 `paper/figures/`。采用中文宋体、统一时刻刻度、细线与少量配色；非填充阶梯曲线不人为连接到零，以免制造日初日末跳变。流程图按模型步骤绘制，不含模拟计算曲线。

Q1全部图表继续绑定 `q1-a-formal-003`；Q1结果表按原题表1、表2的成组版式重排，仅展示值舍入。Q4绑定下列四条已有v3完整轨迹及原 `comparison.json`：

- `q4-2-v3-annual-main-001`
- `q4-2-v3-annual-lag1-001`
- `q4-3-v3-annual-main-001`
- `q4-3-v3-annual-lag1-001`

原Q4证据图、数据、Excel和 `paper/tables/q4_results.tex` 均保留。新呈现表由脚本输出到 `paper/tables/q4_results_paper.tex`，正文优先引用这一版；原报告生成器不会覆盖新的呈现文件。本次未重新求解或新增对照、敏感性实验，不调整预测权重，不代替人工批准运行选择。

## 复现

在仓库根目录执行：

```bash
MPLCONFIGDIR=/tmp/cumcm-matplotlib .venv/bin/python scripts/build_paper_figures.py
.venv/bin/python scripts/build_paper.py --mode draft
.venv/bin/python scripts/build_paper.py --target ai-details --mode draft
```

绘图默认读取 `configs/selected_runs.toml` 的显式Q1运行及已绑定的Q4比较证据，不选择最新目录。可用 `--q1-run` 和 `--q4-evidence` 指定来源。Q4证据缺失时只画Q1图，不补造Q4数值。

在 `paper/` 下编译阅读版：

```bash
latexmk -quiet -xelatex -interaction=nonstopmode -halt-on-error -outdir=build review.tex
```

## 验证与交付

Q1原附件哈希、图表来源及输出哈希、指定区间与六个四小时块、摘要与正文总量一致性通过；Q1能量平衡、储能递推与费用直接复算通过。四条Q4比较证据所绑定的结果、配置、执行、预测、账本及快照文件哈希均保持一致，月费用与回放期费用相符。Q2/Q3章节、模型源码、决策及运行选择配置未由本次修改。会话期间另一项工作更新了Q4实施状态文档，未回写或恢复该文档。

完整稿22页，正文阅读版18页（摘要1页、正文及参考文献17页），AI详情2页。三个编译目标均成功，无缺字、未定义引用和行溢出；已渲染检查摘要、输入图、Q1结果与轨迹、Q4误差表及对照图、两幅实际运行图。图表与对应小节顺序一致。新增绘图脚本通过Ruff检查与格式检查。

[完整稿](../paper/build/main.pdf)、[阅读版](../paper/build/review.pdf)、[AI详情](../paper/build/ai_details.pdf)。图表来源清单为 `paper/figures/figure_manifest.json`，核对证据为 `outputs/paper_style/20260913_validation.json`。修订前论文源码与PDF备份位于 `outputs/paper_style/20260913_before/`。

AI辅助修订与绘图已列入正文声明、详情及新增分段记录；已聚合，人工核验仍为pending。问题二、三和最终采用仍须按原流程补全。

---

# 历史记录：公共部分与问题一论文阶段稿

## 范围

按用户要求，完成摘要（仅对应已完成工作）、问题重述与分析、模型假设、符号说明、数据说明与处理、问题一、问题一检验评价、结论及 AI 使用声明草稿。问题二、三、四文件未修改；原章节占位仍保留。四幅图仅设置图框、图题和绘制说明，未生成模拟曲线。全文仍是阶段稿，不宣称其余问题完成。

## 结果来源

所有数值取自 `outputs/runs/q1/q1-a-formal-003/` 的 `domain_result.json`、`summary.json`、`validation.json` 和 `solver.log`，未运行新的优化实验，未使用 smoke 数据。`paper/tables/q1_results.tex` 是该快照的三位小数排版表，文件首行记录完整结果的 SHA-256；保留原 `paper/generated/q1_result_tables.tex` 与 Excel 导出链。若将来更换所选 run，必须同步更新摘要、正文数值、验证表、结论和排版表，不能仅重新生成原始表格。

本稿据已批准 Q1 模型补全数学表达；未变更决策配置或结果选择批准状态。最大残差、最优间隙、费用、光伏消纳与能量损耗均对应同一 run。检验部分只报告既有 Q1 复算证据，未新增跨模型比较、敏感性实验或节省率。

## 写作与文献依据

- 参考组委会在中国大学生在线展示的 2024 C038 论文组织结构： https://dxs.moe.gov.cn/zx/a/hd_sxjm_sxjmlw_2024qgdxssxjmjslwzs_2024ctlw/241104/1977952.shtml 。仅参考论文体裁、层次和呈现方式，不移植其模型、结论或措辞，不作为本题技术依据列入参考文献。
- 求解器技术引用采用官方资料： https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.milp.html 与 https://highs.dev/ 。已核对接口的 MILP 用途、状态及最优间隙定义。在线文档当前展示版本不作为本地计算环境版本声明。
- 格式依据仓库既有 `docs/rules.md` 与原始官方材料；不将格式文件当作数据处理方法的文献。

## 后续衔接

问题二至四完成后扩展摘要与总体结论；届时再确定全文题目。图形按占位说明从正式快照绘制。AI 人工核验、最终源码快照及支撑文件核对仍按既有流程保留待核验状态。

## 编译与审阅

- 完整稿：`.venv/bin/python scripts/build_paper.py --mode draft`，产物 `paper/build/main.pdf`。
- 正文阅读版：在 `paper/` 下运行 `latexmk -xelatex -interaction=nonstopmode -halt-on-error -outdir=build review.tex`，产物 `paper/build/review.pdf`；保留各章节与参考文献，省略源码及文件列表附录。
- AI 详情：`.venv/bin/python scripts/build_paper.py --target ai-details --mode draft`。
- 本次论文数值与原始快照复核，未改变模型参数或重新求解。

验证结果：三个PDF均编译成功，阅读版13页（摘要1页、正文及参考文献12页）；摘要与问题一结果页已渲染检查。144区间完整性、物理约束、日末状态、全天能量收支及求解上下界复核通过。新增正文无溢出；保留的原问题二占位文本有约1.74 pt轻微行溢出，其源文件未修改。问题二至四文件和决策配置通过零差异检查。

## 源码附录精简

按用户后续要求，附录改为“核心源程序”，仅直接引用 `q1.py` 中约束矩阵构建（89–127行）、变量边界及MILP调用（142–183行）、物理残差复核（343–367行）与费用复核（376–383行），共114行。排版不再引用自动生成的全仓库源码清单，重新构建不会恢复长附录。源码文件、模型与完整支撑程序均未删改。上述方法按本题建模处理介绍，不宣称常规MILP本身为新算法。若后续修改 `q1.py`，应同步核对引用行段。
