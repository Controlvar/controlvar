"""A fault scenario's sensitivity check (``tests/ground_truth/sensitivity.py``),
with no server: each fault planted and answered as its ops expect passes it;
the same check on the clean plan, a fault planted short, or a fault placed
where it moves no number, fails, as a failure (never a skip) that names the
fault and says the TEST is broken.

And the pin: only ``sensitivity()`` passes ``without=`` to ``valid_events``.
A gate compared with a reference missing a rule would pass a server missing
it too.
"""

from __future__ import annotations

import ast
import importlib

import pytest

from backend.tests.unit.ground_truth.library import (
    LIBRARY_ROOT,
    REPO_ROOT,
    faults,
    generator,
)

# tests/ground_truth is on sys.path once library is imported.
check = importlib.import_module("sensitivity")

pytestmark = pytest.mark.unit

g = generator
SEED = 20261103
SCENARIO = g.Scenario(
    name="faults",
    experiment_key="gt-faults",
    arms=(
        g.Arm("control", 50, 0.3, repeat=0.3, values=g.Values(3.0, 1.0)),
        g.Arm("treatment", 50, 0.3, repeat=0.3, values=g.Values(3.0, 1.0)),
    ),
    users=600,
    event_name="purchase",
    metrics=(
        g.Metric("buyers", "purchase", "conversion"),
        g.Metric("purchases", "purchase", "count"),
        g.Metric("revenue", "purchase", "revenue"),
    ),
    event_batch_size=50,
)
CLEAN = g.simulate(SEED, SCENARIO)
SMALL = {
    "F1": faults.Duplicates(r4_drops=12, no_id_repeats=3),
    "F3": faults.LateEvents(
        r2_status_untags=8, paused_assigns=3, completed_assigns=2, paused_resends=2
    ),
    "F5": faults.EarlyEvents(r2_order_untags=8),
    "F6": faults.VariantMismatch(
        other_arm=5, never_assigned=4, tagged_then_assigned=3, own_arm=2
    ),
    "F7": faults.ExposureLoss(lost_users=150),
    "F9": faults.Hostile(per_class=1),
    "F4": faults.ClockSkew(future_users_per_arm=2),
}
#: What each fault's check says when the fault touched nothing.
NOTHING = {
    "F1": "F1 (R4): the definition plants 12 R4 drops (repeats not stored); the"
    " ledger shows 0 (the fault touched nothing)",
    "F3": "F3 (R2-status): the definition plants 8 events R2 untags by status; the"
    " ledger shows 0 (the fault touched nothing)",
    "F5": "F5 (R2-order): the definition plants 8 events R2 untags by order; the"
    " ledger shows 0 (the fault touched nothing)",
    "F6": "F6 (R8): the definition plants 9 /events events counted in no variant"
    " (cases i and ii); the ledger shows 0 (the fault touched nothing)",
    "F7": "F7: the definition plants 150 users never assigned; the ledger shows 0"
    " (the fault touched nothing)",
    "F9": "F9: class 'emoji-user-id': the definition plants 1 sends; the run shows 0",
}


def run(fault, plan, *, hostile_sends=None, records=None):
    """The sensitivity check of *plan*, answered as its ops expect."""
    records = (
        faults.expected_records(plan, faults.DECLARED) if records is None else records
    )
    check.sensitivity(
        [fault],
        plan,
        records,
        where=f"scenario {fault.NAME}, seed {SEED}",
        hostile_sends=faults.hostile_counts(plan)
        if hostile_sends is None
        else hostile_sends,
    )


def failed(fault, plan, **kwargs) -> str:
    with pytest.raises(pytest.fail.Exception) as raised:
        run(fault, plan, **kwargs)
    message = str(raised.value)
    assert message.startswith(
        f"sensitivity failed: scenario {fault.NAME}, seed {SEED}:"
    )
    assert "the TEST is broken" in message
    return message


@pytest.mark.parametrize("name", sorted(SMALL))
def test_each_fault_planted_passes_its_sensitivity_check(name):
    fault = SMALL[name]
    run(fault, faults.inject(CLEAN, fault))
    if name != "F4":  # every fault scenario carries F4's timestamps too
        run(fault, faults.inject(CLEAN, fault, SMALL["F4"]))


@pytest.mark.parametrize("name", sorted(NOTHING))
def test_a_fault_that_touched_nothing_fails_and_says_the_test_is_broken(name):
    """The clean plan, checked as the fault's scenario: an injector that
    returned the plan unchanged, or a driver that skipped what it planted."""
    message = failed(SMALL[name], faults.inject(CLEAN))
    assert NOTHING[name] in message, message


@pytest.mark.parametrize(
    "planted, claimed",
    [
        (faults.EarlyEvents(r2_order_untags=8), faults.EarlyEvents(r2_order_untags=9)),
        (
            faults.Duplicates(r4_drops=12, no_id_repeats=3),
            faults.Duplicates(r4_drops=11, no_id_repeats=3),
        ),
        (
            faults.VariantMismatch(
                other_arm=5, never_assigned=4, tagged_then_assigned=3, own_arm=2
            ),
            faults.VariantMismatch(
                other_arm=5, never_assigned=4, tagged_then_assigned=3, own_arm=3
            ),
        ),
        (faults.ExposureLoss(lost_users=150), faults.ExposureLoss(lost_users=151)),
        (faults.Hostile(per_class=1), faults.Hostile(per_class=2)),
    ],
    ids=["F5", "F1", "F6-controls", "F7", "F9"],
)
def test_a_count_other_than_the_definition_s_fails(planted, claimed):
    """P-a: the ledger shows exactly what the definition plants, not more and
    not less, positive controls included."""
    plan = faults.inject(CLEAN, planted)
    message = failed(
        claimed, plan, records=faults.expected_records(plan, faults.DECLARED)
    )
    assert "the definition plants" in message, message


def test_a_fault_placed_where_it_moves_no_number_fails():
    """P-b: one late event from a user who has converted already, on a
    conversion metric: R2 untags it (P-a holds) and no number the gates
    compare moves with or without the rule."""
    key = "gt-p-b"
    scenario = g.Scenario(
        name="p-b",
        experiment_key=key,
        arms=(g.Arm("control", 50, 0.5), g.Arm("treatment", 50, 0.5)),
        users=1,
        event_name="purchase",
        metrics=(g.Metric("buyers", "purchase", "conversion"),),
    )

    def step(action):
        return g.Op("lifecycle", None, {"action": action, "experiment_key": key})

    event = {"event_type": "purchase", "user_id": "u-1", "experiment_key": key}
    plan = faults.inject(
        g.Plan(
            1,
            scenario,
            (
                step("create"),
                step("start"),
                g.Op(
                    "assign",
                    "/api/v1/tracking/assign",
                    {"experiment_key": key, "user_id": "u-1"},
                ),
                g.Op("track", "/api/v1/tracking/track", event),
                step("pause"),
                g.Op("track", "/api/v1/tracking/track", event),
                step("resume"),
            ),
        )
    )
    fault = faults.LateEvents(
        r2_status_untags=1, paused_assigns=1, completed_assigns=1, paused_resends=1
    )
    message = failed(fault, plan)
    assert "F3 (R2-status) moves no number the gates compare" in message, message
    assert "the definition plants" not in message, message


def test_f7_fails_when_the_loss_is_too_small_to_show():
    fault = faults.ExposureLoss(lost_users=10)
    message = failed(fault, faults.inject(CLEAN, fault))
    assert "sample-ratio p-value" in message and "not below 0.001" in message, message


def test_f9_counts_the_raw_sends_the_driver_reports():
    fault = SMALL["F9"]
    message = failed(fault, faults.inject(CLEAN, fault), hostile_sends={})
    for cls in faults.F9_HOSTILE:
        assert (
            f"class {cls!r}: the definition plants 1 sends; the run shows 0" in message
        )


def test_f9_fails_when_no_hostile_but_valid_event_counts():
    fault = SMALL["F9"]
    plan = faults.inject(CLEAN, fault)
    refused = g.Plan(
        plan.seed,
        plan.scenario,
        tuple(
            g.Op(op.kind, op.route, op.body, "404")
            if op.kind == "assign" and faults.f9_class(op.body["user_id"])
            else op
            for op in plan.ops
        ),
    )
    message = failed(fault, refused)
    assert "no hostile but valid event counts in a variant" in message, message


def test_f4_has_no_check_at_run_time():
    """F4 moves no number by the contract (R7): its sensitivity is its planted
    defect, run against the server, not this check."""
    run(SMALL["F4"], faults.inject(CLEAN))


def test_no_fault_planted_fails():
    with pytest.raises(pytest.fail.Exception, match="no fault is planted"):
        check.sensitivity(
            [], CLEAN, faults.expected_records(faults.inject(CLEAN)), where="x"
        )


# ---------------------------------------------------------------------------
# Only sensitivity() passes without=
# ---------------------------------------------------------------------------
#: Every file that could call ``valid_events``: the library, the live suite and
#: its helpers, and these unit gates.
SCANNED = sorted(LIBRARY_ROOT.rglob("*.py")) + sorted(
    (REPO_ROOT / "backend/tests/unit/ground_truth").glob("*.py")
)
ALLOWED = (LIBRARY_ROOT / "sensitivity.py", "sensitivity")


class _Calls(ast.NodeVisitor):
    """Calls of ``valid_events`` passing ``without=`` (or ``**``), with the
    function each is in."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.found: list[tuple[int, str | None]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = (
            func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        )
        if name == "valid_events" and any(
            k.arg in ("without", None) for k in node.keywords
        ):
            self.found.append((node.lineno, self.stack[-1] if self.stack else None))
        self.generic_visit(node)


def without_calls() -> list[tuple[str, int, str | None]]:
    found = []
    for path in SCANNED:
        calls = _Calls()
        calls.visit(ast.parse(path.read_text(encoding="utf-8")))
        found += [
            (str(path.relative_to(REPO_ROOT)), line, f) for line, f in calls.found
        ]
    return found


def test_only_sensitivity_passes_without_to_valid_events():
    allowed = (str(ALLOWED[0].relative_to(REPO_ROOT)), ALLOWED[1])
    calls = without_calls()
    assert calls, "the scan found no valid_events(..., without=...) call at all"
    offenders = [
        f"{path}:{line} (in {function or 'the module'})"
        for path, line, function in calls
        if (path, function) != allowed
    ]
    assert offenders == [], (
        "valid_events(..., without=...) outside sensitivity(): a gate compared with"
        f" a reference missing a rule passes a server missing it: {offenders}"
    )


def test_the_scan_reads_the_live_suite_and_the_library():
    names = {path.name for path in SCANNED}
    assert {"sensitivity.py", "faults.py", "contract.py", "test_replay.py"} <= names
