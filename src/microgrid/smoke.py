"""Deterministic synthetic smoke test.

This is an I/O and unit-conversion example, not a strategy run.  All products
are clearly marked synthetic and cannot become formal results.
"""

from __future__ import annotations

import csv
import datetime as _dt
import os
from pathlib import Path
from typing import Any

from .artifacts import build_manifest, new_run_id, write_json, write_manifest
from .dataio import ensure_dir, utc_now

INTERVAL_MINUTES = 10
POWER_KW = 600.0
ENERGY_KWH = POWER_KW * INTERVAL_MINUTES / 60.0  # 100.0 kWh, explicit example


def _synthetic_rows() -> list[dict[str, Any]]:
    start = _dt.datetime(2025, 1, 1, 0, 0)
    rows = []
    for i in range(6):
        interval_start = start + _dt.timedelta(minutes=INTERVAL_MINUTES * i)
        interval_end = interval_start + _dt.timedelta(minutes=INTERVAL_MINUTES)
        rows.append(
            {
                "interval_index": i,
                "interval_start": interval_start.isoformat(sep=" "),
                "interval_end": interval_end.isoformat(sep=" "),
                "load_kw": POWER_KW,
                "pv_kw": 0.0,
                "price_cny_per_kwh": 0.5,
                "duration_minutes": INTERVAL_MINUTES,
                "energy_from_constant_power_kwh": ENERGY_KWH,
                "is_synthetic": True,
                "model_status": "not_implemented",
            }
        )
    return rows


def _write_placeholder_figure(run_dir: Path) -> Path | None:
    os.environ.setdefault("MPLCONFIGDIR", str(run_dir / "matplotlib-cache"))
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # pragma: no cover - environment-dependent
        return None
    fig, ax = plt.subplots(figsize=(4, 2.2))
    ax.add_patch(
        __import__("matplotlib.patches", fromlist=["Rectangle"]).Rectangle(
            (0.1, 0.2), 0.8, 0.6, fill=False, edgecolor="0.3", linewidth=2
        )
    )
    ax.text(0.5, 0.5, "SYNTHETIC PLACEHOLDER\nNO STRATEGY RESULT", ha="center", va="center")
    ax.set_axis_off()
    path = run_dir / "placeholder_figure.png"
    fig.savefig(path, dpi=100, bbox_inches="tight")
    plt.close(fig)
    return path


def run_smoke(repo_root: str | Path = ".") -> Path:
    repo = Path(repo_root)
    run_id = new_run_id("smoke", repo)
    run_dir = ensure_dir(repo / "outputs" / "smoke" / run_id)
    command = ["python", "-m", "microgrid", "smoke"]
    manifest = build_manifest(
        repo,
        run_id=run_id,
        case_id="smoke",
        command=command,
        status="pass",
        is_synthetic=True,
        model_status="not_implemented",
        random_seed=None,
    )
    manifest["decision_status"] = "pending_decisions_do_not_block_smoke"
    manifest["synthetic_definition"] = {
        "power_kw": POWER_KW,
        "interval_minutes": INTERVAL_MINUTES,
        "energy_kwh": ENERGY_KWH,
        "formula": "power_kw * interval_minutes / 60",
        "note": "Unit-conversion example only; not an official interval-integration decision.",
    }
    write_manifest(run_dir, manifest)

    rows = _synthetic_rows()
    csv_path = run_dir / "synthetic_intervals.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "run_id": run_id,
        "case_id": "smoke",
        "is_synthetic": True,
        "model_status": "not_implemented",
        "interval_count": len(rows),
        "constant_power_kw": POWER_KW,
        "interval_minutes": INTERVAL_MINUTES,
        "energy_kwh_per_interval": ENERGY_KWH,
        "total_energy_kwh": round(ENERGY_KWH * len(rows), 6),
        "unit": "kWh",
        "created_at": utc_now(),
        "status": "pass",
        "warning": "Synthetic engineering example only; not a strategy or cost result.",
    }
    write_json(run_dir / "summary.json", summary)
    write_json(
        run_dir / "status.json",
        {
            "status": "pass",
            "is_synthetic": True,
            "model_status": "not_implemented",
            "formal_result_eligible": False,
        },
    )
    (run_dir / "run.log").write_text(
        "smoke run completed\n"
        f"run_id={run_id}\n"
        f"is_synthetic=true\n"
        f"energy_kwh_per_interval={ENERGY_KWH}\n",
        encoding="utf-8",
    )
    _write_placeholder_figure(run_dir)
    return run_dir
