"""Observational timing; never used by prediction, dispatch or acceptance gates."""

from __future__ import annotations

import os
import platform
import time
from collections import defaultdict
from contextlib import contextmanager
from importlib.metadata import version

import numpy as np

STAGES = (
    "input_preflight",
    "forecast_ingest",
    "forecast_refresh",
    "window_slice",
    "matrix_build",
    "solver",
    "tail_reuse_validation",
    "dispatch_validation",
    "forecast_evidence_hash",
    "feedback_and_settlement",
    "log_serialization_and_write",
    "checkpoint",
    "physical_validation",
    "controller_chain_audit",
    "metrics_and_report",
    "excel_export_and_readback",
)


class Performance:
    def __init__(self, *, clock=time.perf_counter):
        self.clock = clock
        self.started = self.phase_started = clock()
        self.phase_name = "simulation"
        self.phases = defaultdict(float)
        self.samples = defaultdict(list)
        self.counters = defaultdict(int)

    def switch_phase(self, name):
        now = self.clock()
        self.phases[self.phase_name] += now - self.phase_started
        self.phase_name, self.phase_started = name, now

    def record(self, name, seconds):
        self.samples[name].append(seconds)

    @contextmanager
    def measure(self, name):
        started = self.clock()
        try:
            yield
        finally:
            self.samples[name].append(self.clock() - started)

    def summary(self):
        now = self.clock()
        phases = dict(self.phases)
        phases[self.phase_name] = phases.get(self.phase_name, 0.0) + now - self.phase_started
        stages = {}
        for name in STAGES:
            values = self.samples[name]
            stages[name] = {
                "count": len(values),
                "total_seconds": sum(values),
                "p50_seconds": float(np.percentile(values, 50)) if values else None,
                "p95_seconds": float(np.percentile(values, 95)) if values else None,
                "p99_seconds": float(np.percentile(values, 99)) if values else None,
            }
        return {
            "schema_version": 1,
            "measurement_scope": "current_process_attempt",
            "simulation_seconds": phases.get("simulation", 0.0),
            "validation_seconds": phases.get("validation", 0.0),
            "report_seconds": phases.get("report", 0.0),
            "export_seconds": phases.get("export", 0.0),
            "end_to_end_seconds": now - self.started,
            "stages": stages,
            "counters": dict(self.counters),
            "stage_accounting": "Stage timers may be nested; phase times are disjoint. Solver counts native calls only.",
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "cpu_count": os.cpu_count(),
                "dependencies": {name: version(name) for name in ("numpy", "scipy", "openpyxl")},
                "thread_environment": {
                    key: os.environ.get(key)
                    for key in (
                        "OMP_NUM_THREADS",
                        "OPENBLAS_NUM_THREADS",
                        "MKL_NUM_THREADS",
                        "HIGHS_THREADS",
                    )
                },
            },
        }
