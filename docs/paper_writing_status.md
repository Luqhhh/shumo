# 公共部分与问题一论文阶段稿

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
