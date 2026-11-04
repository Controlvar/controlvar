"""A fault scenario's sensitivity check (#1544 part D): ``sensitivity()``.

A fault scenario's gates compare ``GET /api/v1/results/{id}`` with the
contract's reference on the ledger. If the fault never reached the server (an
injector that planted nothing, a fault placed where it moves no number, a
driver that skipped its sends) those gates pass on a server that has the very
defect the fault is there to catch. So this checks the ledger against the
fault's definition (``ground_truth.faults``) and fails the test, never skips
it, naming the fault and saying that the TEST is broken:

* (P-a) the count the fault's rule changes equals the named integer of its
  definition: the contract and the contract without that rule
  (``valid_events(records, without={rule})``) differ by exactly what was
  planted, so a fault that reached the server short fails too;
* (P-b) the reference without the rule differs in a number the gates compare;
* for the faults with no rule: F7, exactly the planted users unassigned and
  the reference's sample-ratio p-value below 0.001; F9, every class sent as
  often as planted and a hostile but valid event counted in a variant; F4,
  nothing at run time (its sensitivity is its planted defect).

With ``combined=True`` the faults were placed together (``faults.combine``,
the nightly FX scenario) and every row is an exact sum of their named
integers (``faults.combined_findings``): R4 is F1's drops plus F3's paused
events resent, and every hostile but valid user of F9 counts.

This is the one caller of ``valid_events`` that passes ``without=``
(``backend/tests/unit/ground_truth/test_sensitivity.py`` pins it): a gate
compared with a reference missing a rule would pass a server missing it too.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from ground_truth import contract, faults
from ground_truth.generator import Plan


def failure(where: str, findings: Sequence[str], reproduce: str = "") -> str:
    """The failure message: where, every finding, and what to do."""
    lines = [
        f"sensitivity failed: {where}: the TEST is broken: a fault that does not"
        " reach the ledger as its definition plants it leaves the scenario's gates"
        " passing on a server that has the defect",
        *(f"  {finding}" for finding in findings),
        "  Fix the scenario or tests/ground_truth/ground_truth/faults.py. If gate P"
        " failed too, read it first: the server refused or mis-answered what the plan"
        " sent.",
    ]
    if reproduce:
        lines.append(f"  reproduce: {reproduce}")
    return "\n".join(lines)


def sensitivity(
    planted: Sequence[faults.Fault],
    plan: Plan,
    records: Sequence[Mapping[str, Any]],
    *,
    where: str,
    hostile_sends: Mapping[str, int] | None = None,
    reproduce: str = "",
    combined: bool = False,
) -> None:
    """Fail unless every fault in *planted* reached *records* (the ledger of
    *plan*) as its definition plants it. *where* names the scenario and the
    seed; *hostile_sends* counts the sends of each ``faults.F9_HOSTILE`` class
    the driver made and had answered; *combined* says the faults were placed
    together by ``faults.combine``."""
    if not planted:
        pytest.fail(failure(where, ["no fault is planted"], reproduce), pytrace=False)
    expected = contract.valid_events(records)
    rules = sorted({fault.RULE for fault in planted if fault.RULE is not None})
    without = {rule: contract.valid_events(records, without={rule}) for rule in rules}
    findings: list[str] = []
    if combined:
        findings = faults.combined_findings(
            planted, plan, records, expected, without, hostile_sends=hostile_sends
        )
    else:
        for fault in planted:
            findings += faults.sensitivity_findings(
                fault,
                plan,
                records,
                expected,
                None if fault.RULE is None else without[fault.RULE],
                hostile_sends=hostile_sends,
            )
    if findings:
        pytest.fail(failure(where, findings, reproduce), pytrace=False)
