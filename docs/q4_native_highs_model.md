# Q4原生HiGHS：事前工程试验

2026-09-13。Agent依据D_OPTIMIZATION_Q4/用户委托选择highspy 1.12.0，匹配当前SciPy内置HiGHS 1.12.0以隔离接口变化；它是试验固定版本，不声称最新版本。可选锁定solver-native依赖，CLI `--solver-method native-highs`，仅v3/main。保留默认SciPy后端。

沿用原目标、CSC、边界、整数及presolve/1e-4 relative gap/10和60秒重试/1e-9整数容差/1e-8原始容差，原生线程1。缓存键覆盖shape、CSC索引/指针和整数布局，最多32个模型实例，更新列成本/边界、行边界和变化系数；readback逐项核对成本/边界/维数/整数及矩阵。原生API参考[HiGHS官方Python示例](https://ergo-code.github.io/HiGHS/stable/interfaces/python/example-py/)。1.12接口不支持批量changeRowsBounds，逐行更新，不假定新版函数可用。

原探索接口把所有kWarning当失败，在410窗口中拒绝73例；定位日志为0到5.68e-14等极小系数按固定1e-9 small_matrix_value被忽略。保留原失败包。仅模型导入/系数更新允许这类warning，仍核对原生模型readback；系数差异不能超过该固定阈值，其他设置/求解错误拒绝。阈值与原内置后端一致，不放宽完整原矩阵和独立物理1e-6 kWh检查，所有成功解必须保留原目标/下界证书。

先真实410窗口交替顺序成对基准及两case各初态连续两周闭环，比较每个原问题证书、原预测不变、动作/费用差异和真实耗时。原探索修正后410例通过且首动作/目标一致，但共机测试约20%更慢；正式受控writer/reader版本另记录，不因API支持warm start就承诺加速。无可测净收益不默认启用；周结果不冒称年度，各失败/负结果保留。运行/导出/配置绑定工程方法、源码和委托快照，原基线/年末预留/实际末态不改。
