# 决策记录

机器可读状态以 `configs/decisions.toml` 为唯一来源。本文件只解释背景与依赖，不代替人工批准。

Stage 1 架构冻结版计划见 [`docs/modeling_plan.md`](modeling_plan.md)。该计划建议新增 `D_LOAD_FORECAST` 和 `D_REALTIME_DISPATCH`，并要求团队重新核对一月状态与 Q4 价格可见性。在参赛队修改 `configs/decisions.toml` 前，这些都只是计划中的候选决策，不是已批准 gate。

状态含义：

- `pending`：尚无候选解释或尚未开始判断，必须阻断。
- `proposed`：已记录候选解释和理由，但仍未人工批准，仍然阻断。
- `approved`：只有同时具备 `confirmed_by`、`confirmed_at` 才视为人工批准。
- `rejected`：明确拒绝该口径。

## 共享语义

| ID | 候选/最终口径 | 阻断 |
|---|---|---|
| D-TIME-INTERNAL | 自然日 144 区间、145 边界；输入功率标签按右端点对齐，原始样本作为前十分钟代表平均功率；内部 `e_k=P_k*(1/6)` | 内部模型计算 |
| D-TIME-TEMPLATE-EXPORT | Q1 result1.xlsx 采用显式行序映射：内部 slot 0..143 写入计划购电量行序，保留官方标签不改写；其他 case 模板仍待核对 | Q1 正式导出已实现；其他 case 正式导出未实现 |
| D-EFF | `eta_c=eta_d=0.9`；行动量母线侧、储能电池内部；`E_{k+1}=E_k+0.9c_k-d_k/0.9` | 储能状态更新 |
| D-STATE | 一月待机 6000 kWh；二月起连续运行、不每日重置；Q1 单独日终等式 | 跨日状态与终端条件 |
| D-INFO | `available_at <= decision_time`；只按可获得时间使用信息；未来 actual 只用于回放结算 | 预测与计划输入 |
| D-RESAMPLE | 小时预报到 10 分钟输入及边界；owner C | Q3/Q4-3 预报处理 |
| D-SETTLE | 调整、紧急费用与余电处理；owner B/C | 正式费用计算 |

## 架构冻结后建议新增的决策

下表只记录计划中的 decision 边界。当前配置中尚无这些 ID，因此不能写成“已批准”或认为现有 gate 已经检查它们。

| 建议 ID | 负责边界 | 不负责 |
|---|---|---|
| D-LOAD-FORECAST | Q2/Q3 的因果负载预测、日内是否更新、cold start、rolling-origin 与 provenance | 光伏重采样、费用结算 |
| D-REALTIME-DISPATCH | actual replay 中每 10 分钟如何因果调整电池、紧急电量何时成为 residual | 调整/紧急费用怎样结算 |
| D-PRICE-FORECAST（条件新增） | 仅当 Q4 未来实时价格不可提前见时，定义价格的因果预测 | 价格已日前公布的情形 |

## 当前已批准口径与新计划的差异

- `D_EFF` 当前已批准 `eta_c=eta_d=0.9`，往返效率为 81%。如无新的人工决定，实现必须继续使用该口径。
- `D_STATE` 当前已批准“一月待机”，而架构冻结计划要求重新比较因果 warm-up 与 2 月 1 日重置。若改口径，必须先修订并重新确认 decision。
- `D_INFO` 当前已批准 Q4 未来价格使用历史信息预测。架构冻结计划仍要求团队审计题意；如改为未来价格已知，同样必须先重新批准。

## 模型 gate（按 case 拆分）

| ID | 需要参赛队判断 | 阻断 |
|---|---|---|
| D-MODEL_Q1 | Q1 单日目标、变量、约束、方法和求解器？ | Q1 模型实现 |
| D-MODEL_Q2 | Q2 连续运行、紧急购电与历史信息模型？ | Q2 模型实现 |
| D-MODEL_Q3 | Q3 预报发布时刻、调整购电与费用模型？ | Q3 模型实现 |
| D-MODEL_Q4_2 | 波动电价下重算 Q2；需覆盖 Q2 基础模型与价格信息条件扩展 | Q4-2 模型实现 |
| D-MODEL_Q4_3 | 波动电价下重算 Q3；需覆盖 Q3 基础模型与价格信息条件扩展 | Q4-3 模型实现 |
| D-EVAL | 正式比较、验证设计、证据标准及结论边界？ | 正式实验与结论 |

D-INFO 是共享语义；每个 case 仍需自己的 `D_MODEL_*` 批准。

## D-TIME 特别记录

当前题包中的字面观察：

- 附件1 A2 是数值 0:10，A145 是字符串 `0:00+1`；
- 附件2/4 时间表头从 0:10 到 `0:00+1`；
- result1 计划网格 A2=`0:10-0:20`，A145=`0:00+1-0:10+1`；
- result2/3/4 多日计划网格 B1=`0:10-0:20`，EO1=`0:00-0:10+1`。

`0:00-0:10+1` 按字面两端解析为一个超过 24 小时的区间；测试必须把它标为异常，不能自动补起点 `+1`。

内部计算与正式导出分属两个 decision：

- `D_TIME_INTERNAL` 控制 144 区间内部网格和输入右端点对齐；
- `D_TIME_TEMPLATE_EXPORT` 控制 `result*.xlsx` 的正式数值映射。

`D_TIME_INTERNAL` 已批准只放行内部模型输入；`D_TIME_TEMPLATE_EXPORT` 已批准 Q1 的行序映射。其他 case 的正式导出仍需分别实现和核对。

## 如何批准

由参赛队编辑 `configs/decisions.toml`：

```toml
[decisions.D_MODEL_Q3]
status = "approved"
choice = "人工填写"
rationale = "人工填写"
confirmed_by = "人工姓名"
confirmed_at = "2026-09-xx"
source = "原始讨论或官方说明定位"
```

Agent 不得代填 `confirmed_by` / `confirmed_at`，也不得自动把 `pending` / `proposed` 改成 `approved`。
