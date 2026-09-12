# C 线 PV tail baseline 候选证据

状态：候选回测本身仍为 `candidate_evidence_only`；2026-09-13 队长在审阅后确认
`TAIL-EXP2`，正式 machine decision 见 `configs/decisions.toml`，可追溯精简记录见
`records/evidence/q3_pv_tail_baseline_summary.json`。本文中的回测脚本不会自行批准算法。

## 问题范围

HORIZON-B 已冻结：00/06/12/18 发布时生成一份覆盖未来24小时的附件3快照；
中间每10分钟 MPC 继续使用这份快照，只对其末端之后尚未覆盖的连续后缀调用
PV tail baseline。因此 baseline 实际需要预测的不是任意未来24小时，而是：

- 每个6小时发布周期中的非发布时间，共35个十分钟决策时刻；
- 最短补1个点，最长补35个点，即10分钟至5小时50分钟的尾部；
- 这些目标相对当前决策时刻仍位于未来18小时20分钟至24小时；
- baseline 只能补 `target_slot_end > attachment3_coverage_end` 的点。

## 固定候选

所有候选都只读取目标时刻之前若干天的“相同十分钟右端点”实际 PV。由于
`target_time <= decision_time + 24h`，最短的1日滞后也满足
`target_time - 1 day <= decision_time`，不会读取未来实际值。

| 候选 | 公式 |
|---|---|
| `TAIL-NAIVE1` | 前1天同一时刻实际PV |
| `TAIL-MEAN3` | 前1/2/3天同一时刻等权均值 |
| `TAIL-MEAN7` | 前1至7天同一时刻等权均值 |
| `TAIL-MEDIAN7` | 前1至7天同一时刻中位数 |
| `TAIL-EXP2` | 前1至7天同一时刻指数加权，预设半衰期2天 |
| `TAIL-WEEKLY4` | 前7/14/21/28天同一时刻按8/15、4/15、2/15、1/15加权 |

`TAIL-EXP2` 的精确公式为：

```text
raw_weight[d] = 2^(-(d-1)/2),  d = 1,...,7
weight[d] = raw_weight[d] / sum(raw_weight)
PV_hat(valid_time) = sum(weight[d] * PV_actual(valid_time-d days))
```

对应权重约为：

```text
(0.321292, 0.227188, 0.160646, 0.113594,
 0.080323, 0.056797, 0.040161)
```

该候选不做 AR 修正，不用未来附件3版本，不把快照末端平移成新预测，也不在
baseline 内改变重采样方法。

## 官方数据回测

输入为附件2“光伏发电实际功率”52,560个连续十分钟点，SHA-256 为
`2e95fd446bfafa0d8c59577b5c2e2ea8b3f1def20dde54a3062556f4da9b4c72`。
回测从2025-02-01开始，按每一次真实 HORIZON-B 补尾调用计权；所有候选共用
839,160个 `(snapshot_issue_time, decision_time, target_time)` 样本。同时另按
`(snapshot_issue_time, target_time)` 去重得到46,620个样本，排名没有变化。

| 候选 | MAE (kW) | RMSE (kW) | 实际PV>0时 MAE (kW) | 9–12月 MAE (kW) |
|---|---:|---:|---:|---:|
| `TAIL-EXP2` | 150.02 | 300.87 | 267.75 | 115.00 |
| `TAIL-MEAN7` | 153.88 | 304.01 | 274.60 | 119.45 |
| `TAIL-MEAN3` | 154.99 | 312.32 | 276.64 | 118.49 |
| `TAIL-MEDIAN7` | 158.46 | 312.12 | 282.78 | 124.12 |
| `TAIL-NAIVE1` | 185.43 | 372.84 | 331.00 | 141.69 |
| `TAIL-WEEKLY4` | 218.72 | 405.27 | 389.57 | 189.30 |

`TAIL-EXP2` 相对 `TAIL-NAIVE1` 的 MAE 下降35.41 kW；按决策日作2,000次配对
bootstrap，差异95%区间为 `[-40.33, -30.62] kW`。相对更简单的
`TAIL-MEAN7`，MAE下降3.86 kW，95%区间为 `[-6.25, -1.53] kW`。

四个快照发布时间对应的 `TAIL-EXP2` MAE 分别为：00:00 后0.27 kW、06:00 后
225.25 kW、12:00 后370.61 kW、18:00 后3.95 kW。00:00和18:00后大部分目标
位于夜间，因此不能只看全体平均；在实际PV严格大于0的470,109个调用样本中，
`TAIL-EXP2` 仍然是六个固定候选中误差最小的方案。

12月31日的2,520个计划外目标超出附件2最后时点，脚本显式记为缺失。正式 Q3
在年度终点进入窗口后本来就会截断窗口，因此这些点不应由 tail baseline 伪造。

## 团队选择及表述边界

队长于2026-09-13确认把 `TAIL-EXP2` 作为主方案，把无参数、较容易解释的
`TAIL-MEAN7` 留作敏感性；`TAIL-NAIVE1` 作为最低基线。选择 `TAIL-EXP2` 的主要
代价是“2天半衰期”属于经验参数，论文必须写成 `EMPIRICAL_CHOICE`，不能写成
题目规定。当前比较使用同一年度数据，9–12月拆分只是稳定性诊断，并不是从未参与
选择的严格独立测试集，因此不应宣称该参数具有普适最优性。

正式 contract 同时冻结：

- `training_cutoff = decision_time`；
- 每个点保留7条实际PV `source_refs`；
- 任一所需滞后缺失时显式失败，不静默退化成其他候选；
- `model_version = TAIL-EXP2/v1`；
- `fallback_reason = attachment3_horizon_exhausted`；
- 只返回调用者给出的尾部 `target_slot_ends`，不得覆盖附件3快照点；
- 输出功率非负，十分钟电量仍由共享 `kWh = kW / 6` 转换。

## 复核入口

脚本：`scripts/audit_q3_pv_tail_candidates.py`

```text
python scripts/audit_q3_pv_tail_candidates.py \
  --actual <附件2路径> \
  --output <本地证据JSON路径>
```

输出只保存指标定义、汇总、脚本哈希与输入哈希，不保存或上传原始数据。
在相同输入和固定 bootstrap seed 下连续运行两次，证据 JSON 的 SHA-256 均为
`8d6e47a2d5dc7852090b88b7a906b63df0a7045cad1eed358e8ca854c0b89bb2`。
