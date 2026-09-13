from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "audit_q3_resampling_candidates.py"
_SPEC = importlib.util.spec_from_file_location("q3_resampling_audit", _SCRIPT)
assert _SPEC and _SPEC.loader
_AUDIT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_AUDIT)


@pytest.mark.parametrize("epsilon", [0.0, -1.0, float("nan"), float("inf"), -float("inf")])
def test_resampling_audit_rejects_invalid_epsilon_before_reading_inputs(monkeypatch, epsilon):
    def forbidden(*args):
        pytest.fail("invalid epsilon must be rejected before reading data")

    monkeypatch.setattr(_AUDIT, "build_inputs", forbidden)
    with pytest.raises(ValueError, match="finite and positive"):
        _AUDIT.evaluate(SimpleNamespace(weight_epsilon_kw=epsilon))
