# Shared API（B/C 接入说明）

本文件对应 A 的 shared-core 接口。它不选择任何模型、求解器、结算公式或模板映射。

## 1. 决策与批准

所有正式入口必须通过：

```python
from microgrid.approvals import decision_issues, require_approved_decisions
```

规则：

- required decision 必须存在；
- `status == "approved"`；
- `confirmed_by`、`confirmed_at` 必须是非空字符串；
- `pending` / `proposed` / `rejected` / 未知状态都阻断；
- case 只检查自己的 `CASE_DECISIONS`；Q1 不会因为 Q3 未批准而被阻断；
- `D_TIME_INTERNAL` 与 `D_TIME_TEMPLATE_EXPORT` 分开；内部模型不看导出 decision。

## 2. 时间与电池 contract

```python
from microgrid.problem.contracts import TimeGrid, BatteryState, BatteryAction, apply_battery_action
```

- `TimeGrid`：自然日 144 个区间、145 个边界；输入按右端点对齐：
  - `0:10 -> [00:00,00:10)`
  - `0:00+1 -> [23:50,次日00:00)`
- `BatteryState`：电池内部储能，`soc` 是比例不是百分数。
- `BatteryAction`：母线侧充电/放电量，均非负；不允许同时充放电；`5000 kW` 按母线侧解释。
- `apply_battery_action`：`E_next = E + 0.9*c - d/0.9`；越界抛错，不 clip。

## 3. 统一结果载体

```python
from microgrid.problem.contracts import CaseResult, IntervalResult
```

`IntervalResult` 固定字段：

| 字段 | 含义 |
|---|---|
| `day` | 内部参考日期 |
| `slot` | `0..143` |
| `load_kw` / `pv_kw` | 区间负载、光伏功率 |
| `planned_purchase_kwh` | 计划购电量 |
| `adjusted_purchase_kwh` | 调整后总购电量，不是增量 |
| `emergency_purchase_kwh` | 紧急购电量 |
| `action` | `BatteryAction` |
| `state_start` / `state_end` | 电池内部储能 |
| `source_ref` | 来源标识 |
| `pv_used_kwh` | 实际消纳光伏电量，kWh；必须 `0 <= pv_used_kwh <= pv_kw/6` |

`CaseResult.intervals` 必须按 `(day, slot)` 有序且唯一。不要把主数据塞进 `metadata`。

合成运行必须显式设置 `CaseContext(is_synthetic=True, output_dir=...)`；正式运行使用默认 `is_synthetic=False`。合成标识会写入 `CaseResult`、manifest 和 summary，不能靠文件名或 monkeypatch 猜测。

注意：

- Q1 的 `pv_kw` 来自附件1给定的光伏预测，不得称为实际发电；
- Q1 若无调整，`adjusted_purchase_kwh` 应按已批准 Q1 模型卡投影，不要用 `0` 表示不适用；
- 是否允许正式结果中的 `emergency_purchase_kwh` 取决于已批准模型；Q1 不能默认填 0 掩盖不可行。

## 4. 完整运行验证

```python
from microgrid.problem.validation import validate_complete_run, require_valid_complete_run
```

- 检查预期日期集合内每日 `slot 0..143` 完整、唯一、有序；
- 检查相邻区间 `state_end -> state_start` 连续，包括跨午夜；
- `require_daily_equal_ends=True` 只用于 Q1 日终等式等明确约束；
- 部分片段不要强制 144 段；完整运行验证使用独立入口。

## 5. 序列化

```python
from microgrid.problem.result_io import save_case_result, load_case_result
```

- 当前 `RESULT_SCHEMA_VERSION = 1`；
- JSON 禁止 NaN / Infinity；
- 写入使用临时文件 + `os.replace`；
- `save_case_result` 默认不覆盖已有文件；
- `load_case_result` 重建现有 dataclass；传入 `expected_days` 时执行完整运行验证；
- 序列化只搬运结果，不求解、不插值、不读取未来数据。

## 6. InfoSet 因果隔离

```python
from microgrid.problem.contracts import InfoItem, InfoSet
```

只使用：

```python
info = InfoSet.from_raw(decision_time, raw_items)
```

- 只保留 `available_at <= decision_time` 的可见项；
- 直接构造未来项会被拒绝；
- 计划器拿到的就是过滤后的完整可见集合，不能再读原始 future actual。

## 7. 运行目录与 manifest

正式 run 目录：

```text
outputs/runs/<case_id>/<run_id>/
├── manifest.json
├── input_snapshot.json
├── domain_result.json
├── validation.json
├── summary.json
├── solver.log
├── export_manifest.json   # 模板批准且实际导出后
└── results/
    └── resultN.xlsx       # 同上
```

现有 helper：

```python
from microgrid.artifacts import (
    ensure_run_id_available,
    build_manifest,
    write_manifest,
    source_tree_hash,
    verify_imported_inputs,
)
```

- `source_tree_hash` 覆盖递归的 `src/microgrid/**/*.py`、`scripts/**/*.py`、`configs/*.toml`、`pyproject.toml`、`uv.lock`、`.python-version`；
- `verify_imported_inputs` 将当前实际文件 SHA-256 与 `records/inputs_manifest.json` 比较，不自动改写 manifest；
- `ensure_run_id_available` 防止覆盖已有 run；
- `write_manifest` 默认不覆盖已有 manifest。

## 8. 最小合成示例

```python
import datetime as dt
from microgrid.problem.contracts import (
    BatteryAction,
    BatteryState,
    InfoItem,
    InfoSet,
    IntervalResult,
    apply_battery_action,
)

state = BatteryState(6000.0)
action = BatteryAction(charge_kwh=10.0)
end = apply_battery_action(state, action)
interval = IntervalResult(
    day=dt.date(2025, 2, 1),
    slot=0,
    load_kw=1000.0,
    pv_kw=0.0,
    planned_purchase_kwh=1000.0,
    adjusted_purchase_kwh=1000.0,
    emergency_purchase_kwh=0.0,
    action=action,
    state_start=state,
    state_end=end,
)

# InfoSet：只看 decision_time 之前可获得的信息
item = InfoItem("load_kw", available_at=dt.datetime(2025, 2, 1), valid_time=dt.datetime(2025, 2, 1))
info = InfoSet.from_raw(dt.datetime(2025, 2, 1), (item,))
```

注意：上面示例不构成 Q2/Q3 策略；实际 `state_end` 必须由共享 `apply_battery_action` 构造并保持一致。
