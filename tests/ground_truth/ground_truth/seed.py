"""The run's seed (#1544): ``SEED``, resolved once per process.

``GROUND_TRUTH_SEED`` when it is set, else a new seed drawn when this module
is first imported. The clean replay (``tests/ground_truth/test_replay.py``)
and the fault scenarios (``tests/ground_truth/test_faults.py``) both take
``SEED`` from here, so one run has one seed and one command reproduces all of
it: a second ``resolve_seed`` in another module would draw a second seed, and
a failure would then print a seed that does not reproduce it
(``backend/tests/unit/ground_truth/test_fault_gates.py`` pins that both see
the same one when ``GROUND_TRUTH_SEED`` is unset).

Imports only the standard library.
"""

from __future__ import annotations

import os
import random
from collections.abc import Mapping


def resolve_seed(environ: Mapping[str, str]) -> int:
    """``GROUND_TRUTH_SEED``, or a new seed for this run."""
    text = environ.get("GROUND_TRUTH_SEED", "").strip()
    if not text:
        return random.SystemRandom().randrange(1, 2**31)
    if not text.isdigit():
        raise ValueError("GROUND_TRUTH_SEED must be a non-negative integer")
    return int(text)


#: The seed of this run.
SEED = resolve_seed(os.environ)
