"""Stage 1 problem runner registry.

The dispatcher in :mod:`microgrid.cases` selects a case runner only after all
required decisions are approved.  Every runner currently raises
``ModelNotImplementedError``; this is intentional and prevents placeholder
models from being mistaken for results.
"""

from __future__ import annotations

from .q1 import run as run_q1
from .q2 import run as run_q2
from .q3 import run as run_q3
from .q4_2 import run as run_q4_2
from .q4_3 import run as run_q4_3

CASE_RUNNERS = {
    "q1": run_q1,
    "q2": run_q2,
    "q3": run_q3,
    "q4_2": run_q4_2,
    "q4_3": run_q4_3,
}

__all__ = ["CASE_RUNNERS"]
