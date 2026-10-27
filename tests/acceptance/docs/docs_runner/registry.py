"""The fixed set of reasons a step may be NOT RUN.

A step that cannot run is reported NOT RUN with one of these reasons, never
skipped: a skip would read as green. The set is fixed so that a report reader
knows every reason there can be.

Declared in a journey file, before the run (a step's ``not_run``):

* ``needs-aws``: the step needs an AWS account;
* ``needs-founder-account``: the step needs an account the operator holds and
  the runner does not;
* ``waived #<issue>``: the step fails today for a known reason, tracked in that
  issue.

A step that waits for a background job of the stack (the safety monitor, say)
is not NOT RUN: it polls for what the job does, up to a bound
(``waiting.py``), and fails if that has not happened by then.

Set by the runner, never written in a journey file:

* ``earlier-step-failed``: a step before it failed, so the guide stopped there
  (one fault never shows as ten);
* ``doc-examples``: the step is ``ref: doc-examples``, so its shell blocks are
  run by Doc Examples (``scripts/doc_examples.py``), not by this runner.

A journey whose verdict must be PASS or FAIL, never PARTIAL, is named in
``EVERY_STEP_RUN``: the loader refuses a ``not_run`` or a ``ref:
doc-examples`` on any of its steps, so each is run by this runner every time,
and a step that fails today is FAIL, not waived. A PARTIAL verdict does not
turn the run red, which is why the list is needed where a release counts on a
journey passing. Each name is a journey file's (``journeys/<name>.yaml``); a
unit test holds the list and that each file exists, so a renamed journey does
not leave it.
"""

from __future__ import annotations

import re
from typing import Tuple

NEEDS_AWS = "needs-aws"
NEEDS_FOUNDER_ACCOUNT = "needs-founder-account"
EARLIER_STEP_FAILED = "earlier-step-failed"
DOC_EXAMPLES = "doc-examples"

#: Reasons a journey file may declare on a step.
DECLARED: Tuple[str, ...] = (NEEDS_AWS, NEEDS_FOUNDER_ACCOUNT)
#: Reasons only the runner sets.
RUNTIME: Tuple[str, ...] = (EARLIER_STEP_FAILED, DOC_EXAMPLES)
#: Journeys every step of which this runner runs: no ``not_run``, no ``ref:``.
EVERY_STEP_RUN: Tuple[str, ...] = ("demo-shoplab",)

WAIVED = re.compile(r"waived #[1-9][0-9]*")


def is_declarable(reason: str) -> bool:
    """True when a journey file may give *reason* as a step's ``not_run``."""
    return reason in DECLARED or WAIVED.fullmatch(reason) is not None


def is_known(reason: str) -> bool:
    """True when *reason* is in the registry at all."""
    return is_declarable(reason) or reason in RUNTIME
