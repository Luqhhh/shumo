from __future__ import annotations

from pathlib import Path

import pytest

from microgrid.cases import required_decisions, run_case
from microgrid.schemas import InputError


def test_approved_q4_2_dispatches_to_real_input_preflight(tmp_path: Path) -> None:
    config = tmp_path / "configs" / "decisions.toml"
    config.parent.mkdir(parents=True)
    cards = []
    for decision_id in required_decisions("q4_2"):
        cards.extend(
            [
                f"[decisions.{decision_id}]",
                'status = "approved"',
                'choice = "test"',
                'rationale = "test"',
                'confirmed_by = "tester"',
                'confirmed_at = "2026-09-13"',
                'source = "test fixture"',
                "",
            ]
        )
    config.write_text("\n".join(cards), encoding="utf-8")

    with pytest.raises(InputError, match="attachment1"):
        run_case("q4_2", tmp_path)
