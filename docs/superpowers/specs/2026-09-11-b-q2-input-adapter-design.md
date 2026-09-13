# B 任务 Q2/Q4-2 输入适配器设计

- 日期：2026-09-11
- 状态：已吸收队伍复核提出的时间、单位、平衡与结算约束；等待再次确认
- 分支：`codex/b-q2-input-adapter`

## 1. 目标

在正式优化模型仍被人工决策门禁阻断的前提下，为 B 任务建立一层可验证、可追溯、因果安全的输入适配器。适配器读取附件1固定电价、附件2实际负载/实际光伏和附件4波动电价，复用 shared-core 的时间、电池、信息集和结果契约，但不选择预测方法、结算公式、优化变量、目标函数或求解器。

本阶段交付的是“模型之前的数据边界”和“结果载体接线”，不是 Q2 或 Q4-2 的数值答案。

## 2. 硬约束

- 原始附件和题包只读；测试数据只写入 pytest 的临时目录。
- `configs/decisions.toml` 中所有状态和人工确认字段保持原样。
- `src/microgrid/problem/q2.py` 与 `q4_2.py` 保持 `ModelNotImplementedError`。
- 不运行正式 case，不写 `outputs/runs/` 的正式结果，不导出 `result2.xlsx` 或 `result4-2.xlsx`。
- 合成测试产物必须标记 `is_synthetic=True`，不得进入 `dist/` 或论文正式数值。
- 内部对齐只复用 `TimeGrid` 的自然日 144 区间语义；不解释或修复未批准的模板时间标签。

## 3. 范围

### 3.1 本阶段包含

- 显式路径、显式 sheet 名称的附件读取入口。
- 固定电价曲线、实际负载/PV、波动电价的内部十分钟键对齐。
- 重复键、缺失键、非有限值、负值、网格不一致和输入文件错误的拒绝。
- 实际量在区间结束后才可见的 `InfoItem` 生成与 `InfoSet.from_raw()` 验证。
- B 数据可组装进 shared `IntervalResult` / `CaseResult` 并经 `result_io` 往返的合成测试。
- B 决策证据和未批准模型卡文档。

### 3.2 本阶段不包含

- 负载或光伏预测算法。
- Q2/Q4-2 计划购电、充放电、紧急购电或费用结算策略。
- 附件4未来电价的预测方法；附件4真实价格只能作为历史/回放数据。
- `D_STATE`、`D_INFO`、`D_SETTLE`、`D_MODEL_Q2` 或 `D_MODEL_Q4_2` 的批准。
- 正式 Excel 模板映射和导出。

## 4. 模块与公开接口

新增 `src/microgrid/problem/q2_inputs.py`。该模块只依赖现有 `dataio` readers、`schemas.InputError`、`TimeGrid`、`InfoItem` 和 `InfoSet`，不依赖 case dispatcher 或求解器。

### 4.1 数据类型

```python
@dataclass(frozen=True)
class FixedPricePoint:
    slot: int
    price_cny_per_kwh: float
    source_ref: str


@dataclass(frozen=True)
class ActualInterval:
    day: date
    slot: int
    start: datetime
    end: datetime
    load_kw: float
    pv_kw: float
    load_source_ref: str
    pv_source_ref: str

    @property
    def load_kwh(self) -> float:
        return TimeGrid.power_to_energy_kwh(self.load_kw)

    @property
    def pv_kwh(self) -> float:
        return TimeGrid.power_to_energy_kwh(self.pv_kw)


@dataclass(frozen=True)
class VariablePricePoint:
    day: date
    slot: int
    start: datetime
    end: datetime
    price_cny_per_kwh: float
    source_ref: str


@dataclass(frozen=True)
class Q2InputBundle:
    fixed_prices: tuple[FixedPricePoint, ...]
    actuals: tuple[ActualInterval, ...]
    input_hashes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class VariablePriceBundle:
    prices: tuple[VariablePricePoint, ...]
    input_hashes: tuple[tuple[str, str], ...]
```

`input_hashes` 使用排序后的 `(logical_name, sha256)` 元组，Q2 的逻辑名固定为 `attachment1`、`attachment2`，波动电价逻辑名固定为 `attachment4`；哈希保存完整 SHA-256。这样 dataclass 不暴露可变字典，同时保留来源版本。主数据不得塞入 metadata。`source_ref` 固定为 `<文件名>!<sheet>!<cell> sha256=<前12位>`，便于与现有 forecast provenance 一致。

### 4.2 读取函数

```python
def load_q2_inputs(
    *,
    attachment1_path: str | Path,
    attachment2_path: str | Path,
    load_sheet_name: str,
    pv_sheet_name: str,
) -> Q2InputBundle: ...


def load_q4_2_prices(
    *,
    attachment4_path: str | Path,
    price_sheet_name: str,
) -> VariablePriceBundle: ...
```

调用方必须传入文件路径和 sheet 名称；模块不扫描 `data/raw/`，也不从题包目录猜测文件。Q2 和 Q4-2 读取函数分开，使基础 Q2 不被附件4或 Q4-2 专属决策绑住。

### 4.3 因果信息函数

```python
def historical_info_items(
    q2_inputs: Q2InputBundle,
    *,
    variable_prices: VariablePriceBundle | None = None,
) -> tuple[InfoItem, ...]: ...


def info_set_at(
    decision_time: datetime,
    items: tuple[InfoItem, ...],
) -> InfoSet: ...
```

`info_set_at` 只是 `InfoSet.from_raw(decision_time, items)` 的窄包装，便于 B 测试和调用方使用统一入口；它不得保留未过滤的 future actual。

## 5. 对齐与验证规则

### 5.1 固定电价（附件1）

- 调用 `read_attachment1`，只选择 `kind == "price_cny_per_kwh"` 的记录。
- 使用记录的原始右端点标签和 `TimeGrid.interval_from_right_endpoint` 映射到 `slot 0..143`。
- 为无日期曲线使用仅限计算的哨兵日期；该日期不得写回 `ObservationRecord` 或冒充来源日期。
- 要求 144 个唯一 slot，价格有限且非负；重复、缺失或越界均抛 `InputError`。
- 输出按 slot 排序，并保留文件名、sheet、单元格和源哈希组成的 `source_ref`。

### 5.2 实际负载和实际光伏（附件2）

- 分别调用 `read_wide_attachment`，kind 为 `load_kw` 与 `pv_actual_kw`，单位为 `kW`。
- 将 `source_date` 解析为基准日，并以 `raw_time_label` 调用 `TimeGrid.interval_from_right_endpoint` 得到 `(day, slot, start, end)`；同时要求 reader 的 `parsed_timestamp` 与所得 `end` 完全相等。
- 两个 sheet 必须拥有完全相同的 `(day, slot)` 键集合；每类记录内部不得重复；输入不得为空，且每个出现的日期必须完整包含 `slot 0..143`。
- 数值必须有限且非负；输出按 `(day, slot)` 排序。`load_kw` / `pv_kw` 保留附件原始功率单位，`load_kwh` / `pv_kwh` 属性只通过 `TimeGrid.power_to_energy_kwh(..., minutes=10)` 派生。
- 适配器保留全年记录，包括历史初始化期；不裁剪月份、不设置电池状态、不添加每日重置。

### 5.3 波动电价（附件4）

- 调用 `read_wide_attachment`，kind 为 `price_actual_cny_per_kwh`，单位为 `元/kWh`。
- 使用与附件2相同的右端点交叉校验和唯一键规则；输入不得为空，每个出现的日期必须完整包含 `slot 0..143`，值必须有限且非负。
- 该数据表示已实现/历史价格，不表示某个决策时刻已知的未来价格。
- Q4-2 后续调用方若要与 Q2 actual 联合回放，必须显式检查两者日期/slot 键集合一致；本阶段测试覆盖该检查函数。

### 5.4 联合网格检查

```python
def require_matching_q4_2_grid(
    q2_inputs: Q2InputBundle,
    variable_prices: VariablePriceBundle,
) -> None: ...
```

函数仅比较 `(day, slot)` 集合并在不一致时抛 `InputError`；它不合并价格、不计算成本、不创建模型输入。

## 6. 信息可见性

`historical_info_items` 为每个 `ActualInterval` 生成两个 item：

- `kind="load_actual_kw"`，value 为 `load_kw`；
- `kind="pv_actual_kw"`，value 为 `pv_kw`。

若提供附件4，再为每个 `VariablePricePoint` 生成 `kind="price_actual_cny_per_kwh"`。输出顺序固定为 `(valid_time, kind, source_ref)`，使测试和下游快照可重复。

三类历史实际量统一设置：

- `valid_time = interval.end`；
- `available_at = interval.end`；
- `source_ref` 直指源文件、sheet、单元格和哈希。

因此决策时刻 `tau` 只能看到 `end <= tau` 的完整区间 actual。固定电价曲线是题目给定的外生曲线，不混入历史 actual items；后续模型在 `D_INFO` 获人工批准后再决定如何把已知固定曲线交给计划器。

## 7. 后续模型前置不变量（本阶段不实现）

以下四项来自参赛队本次书面复核，必须进入 `docs/b_decision_evidence.md` 和 `docs/b_model_card.md` 的强制约束与验收清单。它们是人工决策证据，但在队伍按流程填写确认人、确认时间并修改状态前，Agent 不得把 `configs/decisions.toml` 中任何条目标记为 `approved`。

### 7.1 时间陷阱

- 附件1/2/4 的时间标签是功率记录的右端点：`0:10` 对应 `[00:00, 00:10)`，`0:00+1` 对应 `[23:50, 次日 00:00)`；不得整体错移一个区间。
- result 模板的首末标签与附件网格存在字面异常，`D_TIME_TEMPLATE_EXPORT` 批准前不得用模板标签反推或改写内部时间键。
- 附件3 的“预报1小时”是发布时刻后一小时；日期只允许在同日四行发布块内继承，功率、价格和预测值禁止 ffill。

### 7.2 功率与电量单位

- 附件负载和光伏值是 `kW`；计划购电、调整购电、紧急购电以及 `BatteryAction` 是每个十分钟区间的 `kWh`。
- 每个区间只允许一次显式换算：`energy_kwh = power_kw * (1 / 6 h)`。禁止把 kW 直接与 kWh 相加、比较或送入费用公式。
- `IntervalResult.load_kw` / `pv_kw` 继续保存原始功率；模型约束使用派生的 `load_kwh` / `pv_kwh`。

### 7.3 供需约束使用不等式

- 统一到 kWh 后，约束方向固定为：`grid_supplied_kwh + discharge_kwh + pv_kwh >= load_kwh + charge_kwh`，不得改成等号。
- `grid_supplied_kwh` 在 Q2、Q3、Q4 中由哪些计划/调整/紧急分量组成，必须由对应已批准模型卡定义；适配器不提前选择。
- `>=` 允许未利用供给或弃光语义；实现不得为了凑等号伪造负荷、充电量或实际使用量。

### 7.4 计划购电费用

- 计划购电费按计划量结算：`planned_cost_cny = sum(planned_purchase_kwh[k] * tariff_cny_per_kwh[k])`。
- 不得用实际使用量、净负荷、`min(计划量, 实际使用量)` 或事后剩余量替代 `planned_purchase_kwh`。
- 调整费、违约费和紧急购电费保持独立分量，只有 `D_SETTLE` 与对应 model decision 正式批准后才能实现完整结算。

## 8. 错误处理

所有调用方可修复的输入问题统一转为带上下文的 `InputError`：

- 路径不存在或不是文件；
- sheet 不存在；
- reader 返回缺少 `parsed_timestamp` 的宽表记录；
- 重复/缺失 slot 或负值/NaN/Infinity；
- 负载与 PV 键集合不一致；
- Q2 actual 与附件4价格网格不一致。

错误消息必须包含逻辑输入名以及路径、sheet、key 或 cell 中至少一个可定位字段。适配器不得静默删除坏记录、补零、ffill、clip 或自动修复时间标签。

## 9. 测试设计

新增 `tests/test_q2_inputs.py`，工作簿全部在 `tmp_path` 创建并在读取前后比较 SHA-256。

1. `test_load_q2_inputs_aligns_fixed_prices_and_actuals`：构造 144 点固定曲线和两天 actual，断言 slot 顺序、`0:10 -> slot 0`、`0:00+1 -> slot 143`、数值和 provenance。
2. `test_actual_interval_converts_ten_minute_power_to_energy`：断言 `600 kW -> 100 kWh`，同时确认 `load_kw/pv_kw` 原值未被覆盖。
3. `test_load_q2_inputs_does_not_modify_sources`：比较三个输入文件读取前后哈希。
4. `test_load_q2_inputs_rejects_mismatched_actual_grids`：负载/PV 缺一个 key 时抛 `InputError`。
5. `test_load_q4_2_prices_and_require_matching_grid`：附件4对齐成功，缺 key 时联合网格检查失败。
6. `test_historical_info_items_are_causal_at_interval_end`：在首区间结束前不可见，恰好结束时可见，第二个区间仍不可见；同时覆盖实际价格。
7. `test_q2_case_result_roundtrip_preserves_adapter_provenance`：测试代码用已适配的一小段数据和零动作构造 `is_synthetic=True` 的 q2 `CaseResult`，经现有 `save_case_result` / `load_case_result` 后保持相等。
8. `test_rejects_non_finite_or_negative_values`：对价格、负载或 PV 的坏值给出可定位错误。

第 7 项只是接口兼容测试，不在生产代码中根据输入制造购电计划，也不声称模型已实现。现有通用 result-I/O、审批门禁和 runner 阻断测试继续作为回归保护。

## 10. 工程文档

新增：

- `docs/b_decision_evidence.md`：按 decision ID 列出当前状态、现有题面/共享证据、仍需队伍决定的问题、可执行验证；不写批准结论。
- `docs/b_model_card.md`：明确 Q2/Q4-2 模型状态为“未批准、未实现”，记录候选输入、输出契约、时间陷阱、kW→kWh 换算、`>=` 供需约束、按计划量计费规则、已知风险、未来批准条件和验证清单。

两份文档必须覆盖 `D_STATE`、`D_INFO`、`D_SETTLE`、`D_MODEL_Q2`、`D_MODEL_Q4_2`，并注明 `D_TIME_TEMPLATE_EXPORT` 继续阻断正式 Excel。它们不得填写 `confirmed_by`、`confirmed_at` 或把 proposed 解释描述成正式结论。

## 11. 数据流

```text
显式路径 + sheet 名称
        |
        v
现有 lossless readers
        |
        v
右端点对齐 + 值/键/来源验证
        |
        +--> Q2InputBundle（固定曲线 + 历史 actual）
        |
        +--> VariablePriceBundle（历史波动电价）
                    |
                    v
historical_info_items -> InfoSet.from_raw(decision_time)
                    |
                    v
未来已批准的预测器/计划器（本阶段不存在）
                    |
                    v
IntervalResult -> CaseResult -> result_io（仅合成接口测试）
```

## 12. 完成标准

- 新模块只读取显式输入，不扫描仓库数据目录。
- 所有新增测试先失败再通过，并使用临时合成工作簿。
- 两个正式 runner 的内容和行为未改变。
- decision 状态及人工确认字段未改变。
- 设计、决策证据和模型卡都明确记录时间陷阱、`kW * 1/6 h -> kWh`、供需 `>=` 和按计划量计费；本阶段不实现模型公式。
- 目标测试与全量 `uv run --locked pytest -q`、Ruff lint/format 均通过。
- Git diff 不含 `CUMCM2026Problems/`、`data/raw/`、`data/templates/`、`resources/`、`outputs/` 或 `dist/` 内容。
- 正式 case 与 final submission 的既有非零阻断保持有效。
