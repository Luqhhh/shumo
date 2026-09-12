# Q4原生HiGHS：验证通过，净耗时未改善

2026-09-13，依据[事前工程卡](q4_native_highs_model.md)完成highspy 1.12.0可选锁定接口试验；匹配SciPy内置1.12.0，不同时改变后端版本和模型。原目标/矩阵/整数/边界、收费/反馈/预留及物理/求解容差保持，32实例缓存有界，所有模型/选项readback和完整原矩阵/物理/目标下界复核保留。

真实运行私有冻结Git对象d14991f76ca35dc4288f238086f382d6b8a54657，源码SHA256 bd833023ae51f9b96bbc4011488614de9084330502fd0e694e9d278b506cf63b。锁文件只新增highspy及项目可选extra，其他原锁定包逐项相同；正式试验使用该隔离检出的locked solver-native环境，旧run保留。

## 正式窗口与闭环

410真实成对窗口0失败，基线17.769067秒，原生22.275265秒，基线/候选=0.797704，约慢25.36%。410例首意图/目标精确相同。模型更新和readback的开销抵消接口收益，本次共机测量不支持默认替换。

Q4-2/Q4-3各从初态连续两周2016步，完整56原预测及provenance、域轨迹和三项费用与已核验v3年度同时间前缀精确相同。物理/费用/状态连续、runtime母计划/后缀和执行链核验均通过。两周不是新的全年结果，不把窗口匹配外推成所有年度窗口自动一致。

## 过程、质量与保留

首次私有探索把kWarning一概当失败，410窗口拒绝73例。定位为极小系数按固定1e-9阈值忽略；原失败日志/报告保留。修正版只允许导入/系数更新warning，并核对成本/边界/整数/矩阵readback，其他错误拒绝，仍由原完整矩阵1e-6 kWh和独立物理检查成功解；没有放宽容差来通过。

3项新增回归覆盖缺失optional extra、版本冲突拒绝和真实成本/边界更新证书；锁定extra环境冻结全量本地359 passed、1 skipped（可选Q1），原环境不装extra时对应真实native单测为明确可选skip。Ruff、格式和lock检查通过。

结论：保留 `--solver-method native-highs` 可复现实验，不默认采用；使用时显式 `uv sync --locked --extra solver-native`。本项没有稳定净加速，因此不为它再启动默认替换全年；不将后端API的setSolution/setBasis能力等同MIP搜索树复用或加速保证。

本地完整证明在outputs/evidence/q4_solver_candidates/native_validation/及native_preparation/；原探索/修正版/正式冻结窗口和两周运行、源码/锁文件、complete和comparison均保留，不上传原题/年度/逐格输出。具体版本/缓存实现由Agent按用户委托事前确定；AI人工核验仍pending，原产物和用户并行论文修改保留。
