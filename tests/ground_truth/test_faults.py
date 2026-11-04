"""The fault scenarios (#1544 part D): realistic traffic with one fault planted
in it, sent through the public API to a running Controlvar, and its results
page checked against the event contract and the independent reference, as
``test_replay.py`` checks the clean scenarios.

Each scenario is a clean ``ground_truth.generator`` plan with a
``ground_truth.faults`` definition planted in it. Every count a fault plants is
a named integer of its definition, written out below, so the run must show
exactly that many (the sensitivity check, S). Pull-request scale only: nothing
here reads ``GROUND_TRUTH_SCALE``.

====  ===========================================  =================================
Id    Clean traffic (2 arms, 50/50)                The fault
====  ===========================================  =================================
F1    2,000 users; ``purchase``: buyers,            duplicates: 60 repeats R4 does
      purchases (count) and revenue                 not store, 10 sent again with no
                                                    ``event_id`` (both stored)
F3    2,000 users, the same                         late events: 40 received while
                                                    paused or completed, around a
                                                    pause, a resume and a complete
F5    2,000 users, the same                         40 events received before their
                                                    user's assignment
F6    2,000 users, the same                         ``/events`` naming another arm
                                                    (40) or a user never assigned
                                                    (40); its own arm (20 + 20)
F7    10,000 users; ``signup``: signups             1,000 users of the treatment
                                                    never assigned
F9    1,000 users; ``purchase``: buyers and         hostile payloads, each class
      purchases                                     once
====  ===========================================  =================================

Every scenario also carries F4, clock skew: three users of each arm send every
event dated 2099, and every third other accepted event carries a skewed
``timestamp``. ``GET /api/v1/results/{id}`` reads no timestamp (R7), so F4
moves no number and has no check of its own here: a results reader that drops
future-dated events turns every scenario of this file red, and the clean ones
of ``test_replay.py`` stay green.

The gates, per scenario:

* **P** every answer is the one its op expects (``faults.unexpected``, kept by
  the driver), in place of the clean replay's check that the plan was stored
  as sent: a fault plan is not stored as sent, by design.
* **G0** as the clean replay's, with at least one assignment answer for each
  user the plan assigns (a fault can leave users unassigned).
* **G1**, **G2** and **Z** as the clean replay's.
* **S** the fault reached the ledger exactly as its definition plants it
  (``sensitivity.sensitivity``): otherwise the gates above pass on a server
  that has the defect the fault is there to catch.

And per fault:

* **F1d** (F1) every ``/track`` and ``/events`` answer's ``duplicate``, and
  every ``/batch`` answer's ``duplicate_count``, is what R4 says, exactly.
* **F7** (F7) the sample-ratio check warns, and the recommendation is
  ``INCONCLUSIVE`` with a reason starting "Sample-ratio mismatch:".
* **F9** (F9) each hostile class got the answer it is listed with
  (``faults.F9_VALID``, ``F9_REFUSED``, ``F9_HOSTILE``), on its route.

A failure names the gate, the scenario and the seed, what disagreed, and the
command that reproduces it. Environment: as ``test_replay.py``'s, but for
``GROUND_TRUTH_SCALE``, which this file does not read.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import driver
import pytest
import test_replay as clean
from ground_truth import contract, faults, generator, ledger
from ground_truth.generator import Arm, Metric, Scenario, Values
from ground_truth.seed import SEED
from sensitivity import sensitivity

#: This file, as the reproduce command names it.
FILE = "tests/ground_truth/test_faults.py"
#: The recommendation and the start of its reason when the sample-ratio check
#: warns (``backend/app/services/srm_service.py``, #880).
SRM_RECOMMENDATION = "INCONCLUSIVE"
SRM_REASON_PREFIX = "Sample-ratio mismatch:"

#: The metrics of the ``purchase`` scenarios: a conversion metric, a count
#: metric and a revenue metric on the same event.
PURCHASE = (
    Metric("buyers", "purchase", "conversion"),
    Metric("purchases", "purchase", "count"),
    Metric("revenue", "purchase", "revenue"),
)
#: F4, in every scenario.
CLOCK_SKEW = faults.ClockSkew(future_users_per_arm=3)


@dataclass(frozen=True)
class FaultScenario:
    """A clean scenario and the faults planted in it."""

    scenario: Scenario
    planted: tuple[Any, ...]

    def plan(self, seed: int) -> generator.Plan:
        return faults.inject(generator.simulate(seed, self.scenario), *self.planted)


def scenarios(seed: int) -> dict[str, FaultScenario]:
    """F1, F3, F5, F6, F7 and F9 (the table above), each under its own
    experiment key."""

    def key(name: str) -> str:
        return f"gt-{name.lower()}-{seed}"

    def shop(name: str, users: int, metrics: tuple[Metric, ...]) -> Scenario:
        return Scenario(
            name=name,
            experiment_key=key(name),
            arms=(
                Arm("control", 50, 0.3, repeat=0.3, values=Values(3.0, 1.0)),
                Arm("treatment", 50, 0.3, repeat=0.3, values=Values(3.2, 1.0)),
            ),
            users=users,
            event_name="purchase",
            metrics=metrics,
        )

    signup = Scenario(
        name="F7",
        experiment_key=key("F7"),
        arms=(Arm("control", 50, 0.10), Arm("treatment", 50, 0.10)),
        users=10000,
        event_name="signup",
        metrics=(Metric("signups", "signup", "conversion"),),
    )
    return {
        "F1": FaultScenario(
            shop("F1", 2000, PURCHASE),
            (faults.Duplicates(r4_drops=60, no_id_repeats=10), CLOCK_SKEW),
        ),
        "F3": FaultScenario(
            shop("F3", 2000, PURCHASE),
            (
                faults.LateEvents(
                    r2_status_untags=40,
                    paused_assigns=20,
                    completed_assigns=10,
                    paused_resends=10,
                ),
                CLOCK_SKEW,
            ),
        ),
        "F5": FaultScenario(
            shop("F5", 2000, PURCHASE),
            (faults.EarlyEvents(r2_order_untags=40), CLOCK_SKEW),
        ),
        "F6": FaultScenario(
            shop("F6", 2000, PURCHASE),
            (
                faults.VariantMismatch(
                    other_arm=40, never_assigned=40, tagged_then_assigned=20, own_arm=20
                ),
                CLOCK_SKEW,
            ),
        ),
        "F7": FaultScenario(signup, (faults.ExposureLoss(lost_users=1000), CLOCK_SKEW)),
        "F9": FaultScenario(
            shop("F9", 1000, PURCHASE[:2]), (faults.Hostile(per_class=1), CLOCK_SKEW)
        ),
    }


SCENARIOS = scenarios(SEED)


def reproduce(name: str = "", *, seed: int = SEED) -> str:
    """The one command that runs fault scenario *name* (every one when empty)
    again with *seed*."""
    return clean.reproduce(name, seed=seed, scale="pr", path=FILE)


# ---------------------------------------------------------------------------
# A replayed fault scenario
# ---------------------------------------------------------------------------
@dataclass
class Faulted(clean.Replayed):
    """A clean replay's record, and what the faults add: the seed the plan was
    made with, the definitions, the answers that were not the ones expected,
    and the hostile sends."""

    seed: int = SEED
    planted: tuple[Any, ...] = ()
    unexpected: list[str] = field(default_factory=list)
    hostile: list[tuple[str, str, int]] = field(default_factory=list)

    @property
    def hostile_sends(self) -> dict[str, int]:
        return dict(Counter(cls for cls, _, _ in self.hostile))


def run(
    case: FaultScenario, live: driver.Driver, directory: Any, *, seed: int = SEED
) -> Faulted:
    """Send *case*'s plan under *seed*, read its ledger back (into
    *directory*) and fetch its results."""
    scenario = case.scenario
    plan = case.plan(seed)
    faulted = Faulted(
        scenario=scenario,
        plan=plan,
        ledger_path=directory / f"{scenario.name}.jsonl",
        seed=seed,
        planted=case.planted,
    )
    before = live.server_errors
    experiment_id = None
    with open(faulted.ledger_path, "w", encoding="utf-8", newline="\n") as stream:
        try:
            experiment_id = live.replay(
                plan, stream, declared=faults.DECLARED, label=scenario.name
            )
        except (ledger.LedgerRefused, driver.ReplayError) as exc:
            faulted.failure = str(exc)
    faulted.unexpected = list(live.unexpected)
    faulted.hostile = list(live.hostile)
    if faulted.failure is None and experiment_id is not None:
        try:
            faulted.records = ledger.read_path(
                faulted.ledger_path, secrets=live.secrets
            )
            faulted.expected = contract.valid_events(faulted.records)
            faulted.results = live.results(experiment_id, label=scenario.name)
        except (
            ledger.LedgerRefused,
            contract.ContractRefused,
            driver.ReplayError,
        ) as exc:
            faulted.failure = str(exc)
    faulted.server_errors = live.server_errors - before
    return faulted


class Runs:
    """Each fault scenario, sent once, when a test first asks for it."""

    def __init__(self, live: driver.Driver, factory: pytest.TempPathFactory) -> None:
        self._live = live
        self._factory = factory
        self._done: dict[str, Faulted] = {}

    def get(self, name: str) -> Faulted:
        if name not in self._done:
            directory = self._factory.mktemp("ground-truth-faults")
            self._done[name] = run(SCENARIOS[name], self._live, directory)
        return self._done[name]


@pytest.fixture(scope="module")
def runs(live: driver.Driver, tmp_path_factory: pytest.TempPathFactory) -> Runs:
    return Runs(live, tmp_path_factory)


@pytest.fixture
def faulted(request: pytest.FixtureRequest, runs: Runs) -> Faulted:
    """The scenario the test is parametrised with, sent once per run."""
    return runs.get(request.param)


# ---------------------------------------------------------------------------
# What a gate found
# ---------------------------------------------------------------------------
def report(
    gate: str,
    faulted: Faulted,
    lines: Sequence[str],
    *,
    headline: str = "",
) -> str:
    """A failure: the gate, scenario and seed, what disagreed, and how to
    reproduce it."""
    name, seed = faulted.scenario.name, faulted.seed
    out = [
        f"{gate} failed: scenario {name}, seed {seed}"
        + (f": {headline}" if headline else "")
    ]
    out += [f"  {line}" for line in lines[: clean.SHOWN]]
    if len(lines) > clean.SHOWN:
        out.append(f"  ... and {len(lines) - clean.SHOWN} more")
    out.append(f"  ledger (synthetic ids only, never uploaded): {faulted.ledger_path}")
    out.append(
        "  reproduce, from the repository root against a server started with"
        " ENVIRONMENT=development on an empty database:"
        f" {reproduce(name, seed=seed)}"
    )
    return "\n".join(out)


def _fail(
    gate: str, faulted: Faulted, findings: Sequence[clean.Finding], headline: str = ""
) -> None:
    pytest.fail(
        report(gate, faulted, [f.line() for f in findings], headline=headline),
        pytrace=False,
    )


def _completed(gate: str, faulted: Faulted) -> None:
    if faulted.failure is not None:
        pytest.fail(
            report(
                gate,
                faulted,
                [],
                headline=f"the replay did not complete: {faulted.failure}",
            ),
            pytrace=False,
        )


def assigned_by_plan(plan: generator.Plan) -> int:
    """The users the plan assigns: each user of an ``/assign`` that expects
    an assignment or of an ``/assign/batch``, once."""
    users: set[str] = set()
    for op in plan.ops:
        if op.kind == "assign" and faults.check_expect(op) == faults.ASSIGNED:
            users.add(op.body["user_id"])
        elif op.kind == "assign_batch":
            users.update(entry["user_id"] for entry in op.body["users"])
    return len(users)


def _ok(status: Any) -> bool:
    return isinstance(status, int) and 200 <= status < 300


def f1d_findings(
    records: Sequence[Mapping[str, Any]], expected: contract.Expected
) -> list[clean.Finding]:
    """F1d: each 2xx ``/track`` and ``/events`` answer's ``duplicate``, and
    each 2xx ``/batch`` answer's ``duplicate_count``, against R4's repeats."""
    dropped: Counter[Any] = Counter()
    for number, record in enumerate(records, 1):
        if (
            record["kind"] == "event"
            and record.get("route") == "/batch"
            and number in expected.repeats
        ):
            dropped[record.get("seq")] += 1
    found: list[clean.Finding] = []
    labels = {"got_label": "answered", "want_label": "R4"}
    for number, record in enumerate(records, 1):
        if not _ok(record.get("status")):
            continue
        if record["kind"] == "event" and record.get("route") in ("/track", "/events"):
            clean._exact(
                found,
                f"ledger line {number} ({record['route']}) 'duplicate'",
                record.get("duplicate"),
                number in expected.repeats,
                **labels,
            )
        elif record["kind"] == "batch":
            clean._exact(
                found,
                f"ledger line {number} (/batch of {record['size']}) 'duplicate_count'",
                record.get("duplicate_count"),
                dropped[record.get("seq")],
                **labels,
            )
    return found


def f7_findings(results: Any) -> list[clean.Finding]:
    """F7: the sample-ratio check warns, and it overrides the recommendation."""
    results = results if isinstance(results, Mapping) else {}
    srm = results.get("srm")
    srm = srm if isinstance(srm, Mapping) else {}
    summary = results.get("summary")
    summary = summary if isinstance(summary, Mapping) else {}
    labels = {"want_label": "the SRM rule"}
    found: list[clean.Finding] = []
    clean._exact(found, "srm 'warning'", srm.get("warning"), True, **labels)
    clean._exact(
        found,
        "summary 'recommendation'",
        summary.get("recommendation"),
        SRM_RECOMMENDATION,
        **labels,
    )
    reason = summary.get("recommendation_reason")
    if not (isinstance(reason, str) and reason.startswith(SRM_REASON_PREFIX)):
        found.append(
            clean.Finding(
                "summary 'recommendation_reason'",
                reason,
                SRM_REASON_PREFIX,
                "starts with",
                want_label="the SRM rule",
            )
        )
    return found


def _meets(want: str, status: int, errors: Sequence[int]) -> bool:
    if want.isdigit():
        return status == int(want)
    if not _ok(status):
        return False
    if want.startswith(faults.ITEM_ERRORS):
        positions = want[len(faults.ITEM_ERRORS) :].split(",")
        return list(errors) == sorted(int(p) for p in positions)
    return want == faults.ACCEPTED and not errors


def f9_findings(
    records: Sequence[Mapping[str, Any]],
    hostile: Sequence[tuple[str, str, int]],
    per_class: int,
) -> list[clean.Finding]:
    """F9: each hostile class's answer, on its route, against the answer it is
    listed with; and every class answered as often as planted."""
    wanted = {
        **dict.fromkeys(faults.F9_VALID, faults.ACCEPTED),
        **faults.F9_REFUSED,
        **faults.F9_HOSTILE,
    }
    batches = {
        r["seq"]: r
        for r in records
        if r["kind"] == "batch" and r.get("seq") is not None
    }
    sends: dict[tuple[str, Any, Any], tuple[int, tuple[int, ...]]] = {}
    for record in records:
        cls = faults.f9_class(record["user"]) if record["kind"] == "event" else None
        if cls is None:
            continue
        route, seq = record.get("route"), record.get("seq")
        answer = batches.get(seq, record) if route == "/batch" else record
        errors = tuple(sorted(answer.get("errors") or ())) if route == "/batch" else ()
        sends[(cls, route, seq)] = (answer["status"], errors)
    answered = [
        (cls, route, status, errors)
        for (cls, route, _), (status, errors) in sends.items()
    ]
    answered += [
        (cls, f"{ledger.ROUTES.get(route, route)}, raw", status, ())
        for cls, route, status in hostile
    ]
    found: list[clean.Finding] = []
    seen: Counter[str] = Counter()
    for cls, route, status, errors in answered:
        seen[cls] += 1
        want = wanted.get(cls, "a class F9 lists")
        if cls not in wanted or not _meets(want, status, errors):
            got = f"{status}" + (f", errors at {list(errors)}" if errors else "")
            found.append(
                clean.Finding(
                    f"class {cls!r} ({route})",
                    got,
                    want,
                    "exact",
                    got_label="answered",
                    want_label="listed",
                )
            )
    for cls in wanted:
        want = per_class * faults.F9_SENDS.get(cls, 1)
        if seen[cls] != want:
            found.append(
                clean.Finding(
                    f"class {cls!r}",
                    seen[cls],
                    want,
                    "exact",
                    got_label="sends answered",
                    want_label="planted",
                )
            )
    return found


def _planted(faulted: Faulted, kind: type) -> Any:
    (fault,) = [f for f in faulted.planted if isinstance(f, kind)]
    return fault


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------
ALL = sorted(SCENARIOS)


@pytest.mark.parametrize("faulted", ALL, indirect=True)
def test_p_every_answer_is_the_one_its_op_expects(faulted: Faulted) -> None:
    lines = list(faulted.unexpected)
    if faulted.failure is not None:
        lines.append(f"the replay did not complete: {faulted.failure}")
    if lines:
        pytest.fail(
            report(
                "P",
                faulted,
                lines,
                headline=f"{len(faulted.unexpected)} answers are not the ones the"
                " plan expects",
            ),
            pytrace=False,
        )


@pytest.mark.parametrize("faulted", ALL, indirect=True)
def test_g0_every_assignment_answer_is_o_assign(faulted: Faulted) -> None:
    _completed("G0", faulted)
    assert faulted.records is not None
    answers = clean.assignment_answers(faulted.records, faulted.scenario.experiment_key)
    findings = clean.g0_findings(
        faulted.experiment, answers=answers, users=assigned_by_plan(faulted.plan)
    )
    if findings:
        _fail(
            "G0",
            faulted,
            findings,
            headline=f"variant mismatches: {len(faulted.experiment.variant_mismatches)}"
            f" of {answers} answers",
        )


@pytest.mark.parametrize("faulted", ALL, indirect=True)
def test_g1_counts_and_means_equal_the_ledger(faulted: Faulted) -> None:
    _completed("G1", faulted)
    findings = clean.g1_findings(faulted.scenario, faulted.experiment, faulted.results)
    if findings:
        _fail("G1", faulted, findings)


@pytest.mark.parametrize("faulted", ALL, indirect=True)
def test_g2_statistics_equal_the_reference(faulted: Faulted) -> None:
    _completed("G2", faulted)
    findings = clean.g2_findings(faulted.scenario, faulted.experiment, faulted.results)
    if findings:
        _fail("G2", faulted, findings)


@pytest.mark.parametrize("faulted", ALL, indirect=True)
def test_z_no_answer_is_a_5xx(faulted: Faulted) -> None:
    if faulted.server_errors:
        pytest.fail(
            report(
                "Z",
                faulted,
                [],
                headline=f"5xx seen: {faulted.server_errors}"
                + (f" ({faulted.failure})" if faulted.failure else ""),
            ),
            pytrace=False,
        )


@pytest.mark.parametrize("faulted", ALL, indirect=True)
def test_s_the_fault_reached_the_ledger_as_planted(faulted: Faulted) -> None:
    _completed("S", faulted)
    assert faulted.records is not None
    name = faulted.scenario.name
    sensitivity(
        faulted.planted,
        faulted.plan,
        faulted.records,
        where=f"scenario {name}, seed {faulted.seed}",
        hostile_sends=faulted.hostile_sends,
        reproduce=reproduce(name, seed=faulted.seed),
    )


@pytest.mark.parametrize("faulted", ["F1"], indirect=True)
def test_f1d_every_repeat_is_answered_as_one(faulted: Faulted) -> None:
    _completed("F1d", faulted)
    assert faulted.records is not None and faulted.expected is not None
    findings = f1d_findings(faulted.records, faulted.expected)
    if findings:
        _fail(
            "F1d",
            faulted,
            findings,
            headline=f"R4 drops {len(faulted.expected.repeats)} events",
        )


@pytest.mark.parametrize("faulted", ["F7"], indirect=True)
def test_f7_the_sample_ratio_check_overrides_the_recommendation(
    faulted: Faulted,
) -> None:
    _completed("F7", faulted)
    findings = f7_findings(faulted.results)
    if findings:
        _fail("F7", faulted, findings)


@pytest.mark.parametrize("faulted", ["F9"], indirect=True)
def test_f9_each_hostile_class_gets_its_listed_answer(faulted: Faulted) -> None:
    _completed("F9", faulted)
    assert faulted.records is not None
    hostile = _planted(faulted, faults.Hostile)
    findings = f9_findings(faulted.records, faulted.hostile, hostile.per_class)
    if findings:
        _fail("F9", faulted, findings)
