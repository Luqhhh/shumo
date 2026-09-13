from __future__ import annotations

import json
from pathlib import Path

import pytest

from microgrid.approvals import DECISION_CASE_SCOPES
from microgrid.cases import required_decisions, run_case
from microgrid.schemas import InputError


def test_approved_q4_2_dispatches_to_real_input_preflight(tmp_path: Path) -> None:
    config = tmp_path / "configs" / "decisions.toml"
    config.parent.mkdir(parents=True)
    cards = []
    for decision_id in required_decisions("q4_2"):
        scope = DECISION_CASE_SCOPES.get(decision_id)
        cards.extend(
            [
                f"[decisions.{decision_id}]",
                'status = "approved"',
                'choice = "test"',
                'rationale = "test"',
                'confirmed_by = "tester"',
                'confirmed_at = "2026-09-13"',
                'source = "test fixture"',
                *([f"scope_cases = {json.dumps(list(scope))}"] if scope else []),
                "",
            ]
        )
    config.write_text("\n".join(cards), encoding="utf-8")

    # main's Q4-2 model verifies the Q4 inputs (附件2/附件4), not 附件1
    with pytest.raises(InputError, match="inputs_manifest"):
        run_case("q4_2", tmp_path)
