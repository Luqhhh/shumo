from __future__ import annotations

import datetime as dt
import json
import runpy
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from microgrid.problem.q4_common import ACTION_START, STEP
from microgrid.schemas import InputError

compare = runpy.run_path(str(Path(__file__).parents[1] / "scripts/compare_q4_pv_blend.py"))[
    "compare_forecasts"
]


def forecasts(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    base, candidate = [], []
    for k in range(29):
        issue = ACTION_START + k * dt.timedelta(hours=6)
        row = {
            "issue_time": str(issue),
            "case_id": "q4_3",
            "slots": [str(issue + j * STEP) for j in range(144)],
            "load_kwh": [100.0] * 144,
            "pv_kwh": [100 / 6] * 144,
            "prices": [1.0] * 144,
            "terminal_value": 0.9,
            "price_method": "main",
            "source_hashes": {"actual": "hash"},
            "traces": {"pv": {"official_source": "visible"}},
        }
        base.append(row)
        new = deepcopy(row)
        new["traces"]["pv_blend"] = {"points": []}
        for j in range(144):
            value = 60.0 if k == 28 and j >= 72 else 100.0
            new["pv_kwh"][j] = value / 6
            new["traces"]["pv_blend"]["points"].append(
                {"original_v3_kw": 100.0, "prediction_kw": value}
            )
        candidate.append(new)

    def save():
        for path, rows in ((before, base), (after, candidate)):
            (path / "forecasts.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows))

    save()
    args = (
        before,
        after,
        ACTION_START + dt.timedelta(days=7, hours=6),
        SimpleNamespace(pv_kw=[0.0] * 52560, index=lambda slot: int((slot - ACTION_START) / STEP)),
    )
    return args, candidate, save


def test_comparison_accepts_only_long_active_changes(tmp_path):
    args, _, _ = forecasts(tmp_path)
    stats, counts = compare(*args)
    assert counts["forecasts"] == 29 and counts["changed_pv_points"] == 72
    assert stats["v3"]["6h"] == stats["blend"]["6h"]


@pytest.mark.parametrize("field", ["load_kwh", "prices", "terminal_value", "official_provenance"])
def test_comparison_rejects_changes_to_other_mechanisms(tmp_path, field):
    args, rows, save = forecasts(tmp_path)
    if field == "official_provenance":
        rows[-1]["traces"]["pv"]["official_source"] = "future"
    elif field == "terminal_value":
        rows[-1][field] += 0.1
    else:
        rows[-1][field][0] += 1
    save()
    with pytest.raises(InputError, match="changed another forecast or provenance"):
        compare(*args)


@pytest.mark.parametrize("record,point", [(0, 80), (28, 0)])
def test_comparison_rejects_modified_cold_or_short_prediction(tmp_path, record, point):
    args, rows, save = forecasts(tmp_path)
    rows[record]["pv_kwh"][point] = 90 / 6
    rows[record]["traces"]["pv_blend"]["points"][point]["prediction_kw"] = 90.0
    save()
    with pytest.raises(InputError, match="short/cold point"):
        compare(*args)


def test_comparison_rejects_changed_original_pv_even_when_output_is_same(tmp_path):
    args, rows, save = forecasts(tmp_path)
    rows[-1]["traces"]["pv_blend"]["points"][80]["original_v3_kw"] = 101.0
    save()
    with pytest.raises(InputError, match="not bound to its forecast point"):
        compare(*args)
