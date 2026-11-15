"""FX, the nightly fault scenario (#1544 part D3): every fault of
``ground_truth.faults`` planted in one realistic plan at the nightly scale,
sent through the public API to a running Controlvar, and its results page
checked as ``test_faults.py`` checks each fault scenario.

Not a ``test_*.py`` file, so ``pytest tests/ground_truth`` (the pull-request
arm) does not collect it; ``_platform.yml``'s ``ground-truth-faults-nightly``
arm names it, and nightly-qa.yml's ``ground-truth-faults`` job runs that arm
(``backend/tests/unit/ground_truth/test_collection_count.py`` pins what each
arm collects).

FX is 40,000 users (10,000 at the nightly scale, four times), two arms 50/50,
30% of users buying, a third of buyers buying again, a lognormal value per
purchase; a conversion, a count and a revenue metric on ``purchase``. Its
faults are placed together by ``faults.combine``: each picks users no fault
placed before it touched, F3's pause and ``complete`` enclose every other
fault's sends, and F4 dates what the others sent. So every count the
sensitivity check reads is an exact sum of these named integers:

=====  =====================================================================
Fault  Named integers
=====  =====================================================================
F1     120 repeats R4 does not store; 20 sent again with no ``event_id``
F2     20 bots sending 250 events each; 20 of those values beyond 10^12,
       each refused in its ``/batch``'s ``errors``
F3     80 events received while paused or completed; 40 assigned users
       asking again while paused, 20 after the ``complete``; 20 paused
       events resent after the resume
F4     6 users of each arm send every event dated 2099; every third other
       accepted event carries a skewed ``timestamp``
F5     80 events received before their user's assignment
F6     ``/events`` naming another arm (80) or a user never assigned (80);
       its own arm, before (40) and after (40) the assignment
F7     4,000 users of the treatment never assigned: 10% of FX's users
F9     each hostile class once; the valid ones carry a value
=====  =====================================================================

The rows (``faults.combined_findings``): R4 = 120 + 20 = 140; R2-status 80;
R2-order 80; R8 160 counted nowhere and 80 counted; F2 20 refused and 4,980
stored; F7 4,000 users the
scenario defines never assigned; F9 each class as planted and its 6 hostile
but valid users counting. The gates are ``test_faults.py``'s, on FX: P, G0,
G1, G2, Z, S (``sensitivity(..., combined=True)``), F1d, F7 and F9.

FX is sized at the nightly scale whatever ``GROUND_TRUTH_SCALE`` says; the
arm sets ``GROUND_TRUTH_SCALE=nightly`` and the reproduce command names it,
so the run announces the scale FX ran at.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import driver
import pytest
import test_faults as suite
import test_replay as clean
from ground_truth import faults, generator
from ground_truth.generator import Arm, Scenario, Values
from ground_truth.seed import SEED
from sensitivity import sensitivity

#: This file, as the reproduce command names it.
FILE = "tests/ground_truth/nightly_fx.py"
#: The scale FX is sized at, as ``test_replay.SCALES`` names it.
SCALE = "nightly"
#: FX's users: 10,000 at the nightly scale.
USERS = 10000 * clean.SCALES[SCALE]


@dataclass(frozen=True)
class Combined(suite.FaultScenario):
    """A clean scenario and the faults placed together in it
    (``faults.combine``)."""

    def plan(self, seed: int) -> generator.Plan:
        return faults.combine(generator.simulate(seed, self.scenario), *self.planted)


def scenario(seed: int) -> Combined:
    """FX under *seed* (the table above)."""
    shop = Scenario(
        name="FX",
        experiment_key=f"gt-fx-{seed}",
        arms=(
            Arm("control", 50, 0.3, repeat=0.3, values=Values(3.0, 1.0)),
            Arm("treatment", 50, 0.3, repeat=0.3, values=Values(3.2, 1.0)),
        ),
        users=USERS,
        event_name="purchase",
        metrics=suite.PURCHASE,
    )
    return Combined(
        shop,
        (
            faults.Duplicates(r4_drops=120, no_id_repeats=20),
            faults.BotTraffic(bots=20, events_per_bot=250, huge_values=20),
            faults.LateEvents(
                r2_status_untags=80,
                paused_assigns=40,
                completed_assigns=20,
                paused_resends=20,
            ),
            faults.ClockSkew(future_users_per_arm=6),
            faults.EarlyEvents(r2_order_untags=80),
            faults.VariantMismatch(
                other_arm=80, never_assigned=80, tagged_then_assigned=40, own_arm=40
            ),
            faults.ExposureLoss(lost_users=4000),
            faults.Hostile(per_class=1, value=1.0),
        ),
    )


FX = scenario(SEED)


def reproduce(*, seed: int = SEED) -> str:
    """The one command that runs FX again with *seed*."""
    return clean.reproduce(seed=seed, scale=SCALE, path=FILE)


@pytest.fixture(scope="module")
def fx(live: driver.Driver, tmp_path_factory: pytest.TempPathFactory) -> suite.Faulted:
    """FX sent once, its ledger read back, its results fetched."""
    return suite.run(FX, live, tmp_path_factory.mktemp("ground-truth-fx"))


# ---------------------------------------------------------------------------
# What a gate found
# ---------------------------------------------------------------------------
def report(
    gate: str, fx: suite.Faulted, lines: Sequence[str], *, headline: str = ""
) -> str:
    """A failure: the gate, the seed and scale, what disagreed, and how to
    reproduce it."""
    out = [
        f"{gate} failed: scenario {fx.scenario.name}, seed {fx.seed}, scale {SCALE}"
        + (f": {headline}" if headline else "")
    ]
    out += [f"  {line}" for line in lines[: clean.SHOWN]]
    if len(lines) > clean.SHOWN:
        out.append(f"  ... and {len(lines) - clean.SHOWN} more")
    out.append(f"  ledger (synthetic ids only, never uploaded): {fx.ledger_path}")
    out.append(
        "  reproduce, from the repository root against a server started with"
        f" ENVIRONMENT=development on an empty database: {reproduce(seed=fx.seed)}"
    )
    return "\n".join(out)


def _fail(gate: str, fx: suite.Faulted, findings: Sequence[clean.Finding]) -> None:
    pytest.fail(report(gate, fx, [f.line() for f in findings]), pytrace=False)


def _completed(gate: str, fx: suite.Faulted) -> None:
    if fx.failure is not None:
        headline = f"the replay did not complete: {fx.failure}"
        pytest.fail(report(gate, fx, [], headline=headline), pytrace=False)


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------
def test_p_every_answer_is_the_one_its_op_expects(fx: suite.Faulted) -> None:
    lines = list(fx.unexpected)
    if fx.failure is not None:
        lines.append(f"the replay did not complete: {fx.failure}")
    if lines:
        headline = f"{len(fx.unexpected)} answers are not the ones the plan expects"
        pytest.fail(report("P", fx, lines, headline=headline), pytrace=False)


def test_g0_every_assignment_answer_is_o_assign(fx: suite.Faulted) -> None:
    _completed("G0", fx)
    assert fx.records is not None
    answers = clean.assignment_answers(fx.records, fx.scenario.experiment_key)
    findings = clean.g0_findings(
        fx.experiment, answers=answers, users=suite.assigned_by_plan(fx.plan)
    )
    if findings:
        _fail("G0", fx, findings)


def test_g1_counts_and_means_equal_the_ledger(fx: suite.Faulted) -> None:
    _completed("G1", fx)
    findings = clean.g1_findings(fx.scenario, fx.experiment, fx.results)
    if findings:
        _fail("G1", fx, findings)


def test_g2_statistics_equal_the_reference(fx: suite.Faulted) -> None:
    _completed("G2", fx)
    findings = clean.g2_findings(fx.scenario, fx.experiment, fx.results)
    if findings:
        _fail("G2", fx, findings)


def test_z_no_answer_is_a_5xx(fx: suite.Faulted) -> None:
    if fx.server_errors:
        headline = f"5xx seen: {fx.server_errors}" + (
            f" ({fx.failure})" if fx.failure else ""
        )
        pytest.fail(report("Z", fx, [], headline=headline), pytrace=False)


def test_s_every_fault_reached_the_ledger_as_planted(fx: suite.Faulted) -> None:
    _completed("S", fx)
    assert fx.records is not None
    sensitivity(
        fx.planted,
        fx.plan,
        fx.records,
        where=f"scenario {fx.scenario.name}, seed {fx.seed}",
        hostile_sends=fx.hostile_sends,
        reproduce=reproduce(seed=fx.seed),
        combined=True,
    )


def test_f1d_every_repeat_is_answered_as_one(fx: suite.Faulted) -> None:
    _completed("F1d", fx)
    assert fx.records is not None and fx.expected is not None
    findings = suite.f1d_findings(fx.records, fx.expected)
    if findings:
        _fail("F1d", fx, findings)


def test_f7_the_sample_ratio_check_overrides_the_recommendation(
    fx: suite.Faulted,
) -> None:
    _completed("F7", fx)
    findings = suite.f7_findings(fx.results)
    if findings:
        _fail("F7", fx, findings)


def test_f9_each_hostile_class_gets_its_listed_answer(fx: suite.Faulted) -> None:
    _completed("F9", fx)
    assert fx.records is not None
    per_class = suite._planted(fx, faults.Hostile).per_class
    findings = suite.f9_findings(fx.records, fx.hostile, per_class)
    if findings:
        _fail("F9", fx, findings)
