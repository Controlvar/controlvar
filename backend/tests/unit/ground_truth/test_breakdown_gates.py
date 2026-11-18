"""G1g, the results by group (#1897, S5 of the live replay), with no server:
S5 sent to the fake server of ``test_balanced_gates`` (which balances as #448
does), and a ``?breakdown=country`` answer and a ``balanced-groups`` answer
built from what that server stored, which every G1g gate accepts; then the
defects the live run plants in the product, applied to those answers instead,
which their gates refuse with the lines the live run prints:

* the breakdown's rows count events, not users: G1g (1),
  ``S5 breakdown US/treatment users <n> != 1,400``;
* the balanced-groups rows count events as conversions: G1g (2),
  ``S5 balanced-groups US/treatment conversions <n> != <m>``;
* the overall estimate weights every group equally: G1g (3),
  ``S5 overall absolute_difference <a> != reference <b>``;
* the breakdown puts each user in the group they were balanced in, not in the
  value of their context: G1g (1), ``BR`` and ``us`` have no rows;
* a user in no row of the breakdown: the rows no longer add up to the headline.

Also ``reference.stratified`` against a worked example, against the product's
own estimate on S5's counts, and each gate's edges (no rows, the tolerance,
a merged group, a null estimate, two treatments).
"""

from __future__ import annotations

import importlib
import math
import re
from collections import Counter

import pytest

from backend.app.services import balanced_groups_estimate as product
from backend.tests.unit.ground_truth.library import generator, reference

suite = importlib.import_module("test_replay")
balanced = importlib.import_module(
    "backend.tests.unit.ground_truth.test_balanced_gates"
)

pytestmark = pytest.mark.unit

S5 = balanced.S5
EVENT = S5.metrics[0].event_name
HEADLINE = S5.metrics[0].name


def _first_country(plan: generator.Plan) -> dict[str, str]:
    """Each user's ``country`` as a breakdown reads it, written out here from
    the docs rather than from ``first_context_values``: the first assign's
    top-level key, else ``unknown``."""
    found: dict[str, str] = {}
    for op in plan.ops:
        if op.kind == "assign" and op.body["user_id"] not in found:
            found[op.body["user_id"]] = op.body["context"].get("country") or "unknown"
    return found


class Answers:
    """What a correct server answers the two reads of S5 after the sends, from
    what the fake server stored: each user's variant and group, the context of
    their first assign, and whether they sent a ``purchase``."""

    def __init__(self, plan: generator.Plan, fake: balanced.FakeBalancedServer):
        self.names = [name for name, _ in plan.allocations()]
        self.variant = {user: v for user, (v, _, _) in fake.stored.items()}
        self.group = {user: g for user, (_, g, _) in fake.stored.items()}
        self.value = _first_country(plan)
        self.converting = {
            op.body["user_id"]
            for op in plan.ops
            if op.kind == "track" and op.body["event_type"] == EVENT
        }
        self.events = Counter(
            op.body["user_id"]
            for op in plan.ops
            if op.kind == "track" and op.body["event_type"] == EVENT
        )
        self.assigns = Counter(
            op.body["user_id"] for op in plan.ops if op.kind == "assign"
        )

    def counts(self, row_of, *, users=None, converted=None):
        """row -> variant -> (users, conversions); *users* and *converted*
        replace the per-user quantity (a plant counts events)."""
        users = users or (lambda user: 1)
        converted = converted or (lambda user: int(user in self.converting))
        rows: dict[str, dict[str, list[int]]] = {}
        for user, variant in self.variant.items():
            cell = rows.setdefault(row_of[user], {}).setdefault(variant, [0, 0])
            cell[0] += users(user)
            cell[1] += converted(user)
        return {
            row: {v: (n, c) for v, (n, c) in cells.items()}
            for row, cells in rows.items()
        }

    @staticmethod
    def segments(counts):
        return [
            {
                "segment_value": row,
                "sample_size": sum(n for n, _ in cells.values()),
                "variants": [
                    {"variant_name": v, "sample_size": n, "conversions": c}
                    for v, (n, c) in cells.items()
                ],
            }
            for row, cells in counts.items()
        ]

    def results(self, counts) -> dict:
        """``GET /results/{id}?breakdown=country``: the headline over all
        users, and the rows."""
        headline = []
        for v in self.names:
            users = [u for u, name in self.variant.items() if name == v]
            headline.append(
                {
                    "variant_name": v,
                    "sample_size": len(users),
                    "conversions": sum(1 for u in users if u in self.converting),
                }
            )
        return {
            "metrics": [{"metric_name": HEADLINE, "variants": headline}],
            "breakdown": {
                "dimension": "country",
                "segments": self.segments(counts),
            },
        }

    def overall(self, counts, *, weights=None) -> dict:
        """The product's own estimate over *counts* (or, planted, with equal
        weights)."""
        arms = [product.Arm(v, v, i == 0) for i, v in enumerate(self.names)]
        groups = [product.GroupCounts(g, cells) for g, cells in counts.items()]
        if weights == "equal":
            return {"overall": [self._equal_weights(counts)], "merged_groups": []}
        return product.overall_estimate(
            groups,
            arms,
            confidence_level=S5.confidence_level,
            correction_method=S5.correction_method,
            is_proportion=True,
            sequential_testing=False,
        )

    def _equal_weights(self, counts) -> dict:
        """The planted estimate: each group counts the same, whatever its size."""
        control, treatment = self.names
        diffs = [
            cells[treatment][1] / cells[treatment][0]
            - cells[control][1] / cells[control][0]
            for cells in counts.values()
        ]
        difference = math.fsum(diffs) / len(diffs)
        right = self.overall(counts)["overall"][0]
        return {**right, "absolute_difference": difference}

    def balanced_groups(self, counts, overall) -> dict:
        found = {"overall": None, "merged_groups": [], **overall}
        found["overall"] = [
            {
                **e,
                "absolute_difference_ci": None
                if e["absolute_difference_ci"] is None
                else list(e["absolute_difference_ci"]),
            }
            for e in (found["overall"] or [])
        ]
        return {
            "groups": {"segments": self.segments(counts)},
            "overall": found["overall"],
            "reason": found.get("reason"),
            "merged_groups": found["merged_groups"],
            "users_outside_blocks": 0,
        }


@pytest.fixture(scope="module")
def sent():
    plan, records, experiment, fake = balanced._sent(S5)
    return plan, experiment, Answers(plan, fake)


def _gates(sent, *, breakdown=None, groups=None, overall=None, plant_rows=None):
    """Every G1g gate's lines for the answers of a correct server, with any
    of the three answers replaced."""
    plan, experiment, answers = sent
    by_value = answers.counts(answers.value)
    by_group = answers.counts(answers.group)
    results = answers.results(by_value if breakdown is None else breakdown)
    served = answers.balanced_groups(
        by_group if groups is None else groups,
        overall if overall is not None else answers.overall(by_group),
    )
    return {
        "breakdown": suite.g1g_breakdown_lines(S5, experiment, plan, results),
        "groups": suite.g1g_balanced_lines(S5, experiment, plan, served),
        "overall": suite.g1g_overall_lines(S5, experiment, plan, served),
    }


# ---------------------------------------------------------------------------
# A correct server passes; each planted defect is refused with its line
# ---------------------------------------------------------------------------
def test_a_correct_server_passes_every_g1g_gate(sent):
    assert _gates(sent) == {"breakdown": [], "groups": [], "overall": []}


def test_the_breakdown_has_a_row_for_each_value_and_not_each_group(sent):
    """S5's ``BR`` and ``us`` are rows of their own in a breakdown (and
    ``other`` in the balanced groups); a nested or dotted key is ``unknown``."""
    _, _, answers = sent
    assert {
        row: sum(n for n, _ in c.values())
        for row, c in answers.counts(answers.value).items()
    } == {
        "US": 2000,
        "DE": 1500,
        "FR": 1400,
        "BR": 400,
        "us": 100,
        "unknown": 600,
    }
    assert set(answers.counts(answers.group)) == {"US", "DE", "FR", "other", "unknown"}


def test_g1g1_rows_that_count_events_not_users_are_refused(sent):
    _, _, answers = sent
    events = answers.counts(answers.value, users=lambda user: answers.assigns[user])
    lines = _gates(sent, breakdown=events)["breakdown"]
    assert any(
        re.fullmatch(r"S5 breakdown US/treatment users [\d,]+ != 1,400", line)
        and line != "S5 breakdown US/treatment users 1,400 != 1,400"
        for line in lines
    )
    assert any("rows add up to" in line for line in lines)


def test_g1g1_a_user_in_no_row_does_not_add_up_to_the_headline(sent):
    _, _, answers = sent
    rows = answers.counts(answers.value)
    del rows["unknown"]
    lines = _gates(sent, breakdown=rows)["breakdown"]
    assert "S5 breakdown unknown/control users 0 != 180" in lines
    assert (
        "S5 breakdown control: rows add up to 1,620 sample_size, the headline 1,800"
        in lines
    )


def test_g1g1_a_breakdown_by_the_balanced_group_is_refused(sent):
    """The breakdown reads the context's value, not the group the user was
    balanced in: ``BR`` and ``us`` are not ``other``."""
    _, _, answers = sent
    lines = _gates(sent, breakdown=answers.counts(answers.group))["breakdown"]
    assert "S5 breakdown BR/control users 0 != 120" in lines
    assert "S5 breakdown other/control users 150 != 0" in lines


def test_g1g1_the_breakdown_is_not_the_balanced_groups_answer(sent):
    """The same defect in the other route is refused by the other gate."""
    _, _, answers = sent
    lines = _gates(sent, groups=answers.counts(answers.value))["groups"]
    assert "S5 balanced-groups BR/control users 120 != 0" in lines


def test_g1g2_conversions_that_count_events_are_refused(sent):
    _, _, answers = sent
    events = answers.counts(answers.group, converted=lambda user: answers.events[user])
    lines = _gates(sent, groups=events)["groups"]
    assert any(
        re.fullmatch(
            r"S5 balanced-groups US/treatment conversions [\d,]+ != [\d,]+", line
        )
        for line in lines
    )
    assert not any(" users " in line for line in lines)


def test_g1g3_an_estimate_that_weights_every_group_equally_is_refused(sent):
    _, _, answers = sent
    equal = answers.overall(answers.counts(answers.group), weights="equal")
    lines = _gates(sent, overall=equal)["overall"]
    assert len(lines) == 1
    assert re.fullmatch(
        r"S5 overall absolute_difference [-\d.e]+ != reference [-\d.e]+", lines[0]
    ), lines


def test_g1g3_the_products_estimate_equals_the_reference_on_s5_s_counts(sent):
    """The same agreement the live run asserts, offline: any drift in the
    product's formula that moves S5's answer by 1e-9 is a defect."""
    _, _, answers = sent
    counts = answers.counts(answers.group)
    (got,) = answers.overall(counts)["overall"]
    want = reference.stratified(list(counts.values()), "control", "treatment", 0.95)
    assert abs(got["absolute_difference"] - want.absolute_difference) <= 1e-12
    assert abs(got["standard_error"] - want.standard_error) <= 1e-12
    assert abs(got["p_value"] - want.p_value) <= 1e-12


# ---------------------------------------------------------------------------
# The edges of each gate
# ---------------------------------------------------------------------------
def test_an_answer_without_rows_is_refused_by_every_row_gate(sent):
    plan, experiment, _ = sent
    assert suite.g1g_breakdown_lines(S5, experiment, plan, {}) == [
        "S5 breakdown: the results answered no breakdown rows"
    ]
    assert suite.g1g_balanced_lines(S5, experiment, plan, None) == [
        "S5: balanced-groups answered no group rows"
    ]
    (line,) = suite.g1g_overall_lines(S5, experiment, plan, None)
    assert line == (
        "S5 overall: None (reason None), expected one estimate per treatment"
    )


def test_a_null_estimate_names_its_reason(sent):
    plan, experiment, _ = sent
    body = {"overall": None, "reason": "too_few_users"}
    (line,) = suite.g1g_overall_lines(S5, experiment, plan, body)
    assert line == (
        "S5 overall: None (reason 'too_few_users'), expected one estimate per treatment"
    )


def _served_overall(sent, **changed):
    plan, experiment, answers = sent
    counts = answers.counts(answers.group)
    body = answers.balanced_groups(counts, answers.overall(counts))
    body["overall"] = [{**body["overall"][0], **changed}]
    return plan, experiment, body


def test_the_tolerance_is_1e_9_on_each_field(sent):
    plan, experiment, body = _served_overall(sent)
    base = body["overall"][0]["absolute_difference"]
    plan, experiment, near = _served_overall(sent, absolute_difference=base + 5e-10)
    assert suite.g1g_overall_lines(S5, experiment, plan, near) == []
    plan, experiment, off = _served_overall(sent, absolute_difference=base + 2e-9)
    (line,) = suite.g1g_overall_lines(S5, experiment, plan, off)
    assert line.startswith("S5 overall absolute_difference ")
    plan, experiment, off = _served_overall(sent, standard_error=None)
    (line,) = suite.g1g_overall_lines(S5, experiment, plan, off)
    assert line.startswith("S5 overall standard_error None != reference ")


def test_a_wrong_interval_and_a_wrong_verdict_are_each_named(sent):
    plan, experiment, body = _served_overall(sent)
    low, high = body["overall"][0]["absolute_difference_ci"]
    plan, experiment, off = _served_overall(
        sent, absolute_difference_ci=[low - 1e-6, high]
    )
    (line,) = suite.g1g_overall_lines(S5, experiment, plan, off)
    assert line.startswith("S5 overall absolute_difference_ci lower ")
    plan, experiment, off = _served_overall(
        sent, is_significant=not body["overall"][0]["is_significant"]
    )
    (line,) = suite.g1g_overall_lines(S5, experiment, plan, off)
    assert line.startswith("S5 overall is_significant ")


def test_a_merged_group_is_named_because_the_reference_does_not_model_it(sent):
    plan, experiment, answers = sent
    counts = answers.counts(answers.group)
    body = answers.balanced_groups(counts, answers.overall(counts))
    body["merged_groups"] = [{"group": "unknown", "into": "US"}]
    (line,) = suite.g1g_overall_lines(S5, experiment, plan, body)
    assert line == "S5 overall: merged_groups [{'group': 'unknown', 'into': 'US'}]"


def test_an_estimate_missing_for_a_treatment_is_named(sent):
    plan, experiment, body = _served_overall(sent)
    body["overall"][0]["variant_name"] = "other"
    (line,) = suite.g1g_overall_lines(S5, experiment, plan, body)
    assert line == "S5 overall: no estimate for treatment"


# ---------------------------------------------------------------------------
# What the gates read: the breakdown's rows, and the reference
# ---------------------------------------------------------------------------
def test_a_context_value_is_read_from_the_top_level_text_only():
    plan = generator.simulate(balanced.SEED, S5)
    values = suite.first_context_values(plan, "country")
    assert Counter(values.values()) == {
        "US": 2000,
        "DE": 1500,
        "FR": 1400,
        "BR": 400,
        "us": 100,
        None: 600,
    }
    assert suite.breakdown_rows(values).keys() == values.keys()


def test_more_than_twenty_values_leave_the_rest_in_other():
    values = {f"u{i}": f"v{i % 22:02d}" for i in range(220)}
    values["x1"] = "other"
    values["x2"] = "unknown"
    values["x3"] = None
    rows = suite.breakdown_rows(values)
    assert (
        Counter(rows.values())["other"] == 20 + 1
    )  # two values of ten users, and the literal
    assert Counter(rows.values())["unknown"] == 2
    assert {rows[f"u{i}"] for i in range(220)} == {f"v{i:02d}" for i in range(20)} | {
        "other"
    }


def test_a_context_value_that_is_not_text_is_refused_not_modelled():
    plan = generator.simulate(1, S5)
    op = next(op for op in plan.ops if op.kind == "assign")
    odd = generator.Plan(
        seed=plan.seed,
        scenario=plan.scenario,
        ops=(generator.Op(op.kind, op.route, {**op.body, "context": {"country": 3}}),),
    )
    with pytest.raises(ValueError, match="is not text"):
        suite.first_context_values(odd, "country")


def test_stratified_equals_a_worked_example():
    groups = [
        {"control": (10, 2), "treatment": (10, 4)},
        {"control": (30, 3), "treatment": (30, 6)},
    ]
    got = reference.stratified(groups, "control", "treatment", 0.95)
    # weights 20/80 and 60/80: 0.25 * (0.4 - 0.2) + 0.75 * (0.2 - 0.1)
    assert got.absolute_difference == pytest.approx(0.125, abs=1e-15)
    variance = 0.25**2 * (0.4 * 0.6 / 9 + 0.2 * 0.8 / 9) + 0.75**2 * (
        0.2 * 0.8 / 29 + 0.1 * 0.9 / 29
    )
    assert got.standard_error == pytest.approx(math.sqrt(variance), abs=1e-15)
    assert got.relative_improvement_pct == pytest.approx(
        0.125 / (0.25 * 0.2 + 0.75 * 0.1) * 100, abs=1e-12
    )
    z = 1.959963984540054
    assert got.absolute_difference_ci == pytest.approx(
        (0.125 - z * math.sqrt(variance), 0.125 + z * math.sqrt(variance)), abs=1e-12
    )
    assert got.p_value == pytest.approx(
        math.erfc(0.125 / math.sqrt(variance) / math.sqrt(2)), abs=1e-15
    )


def test_stratified_with_no_variance_has_no_interval_and_no_p_value():
    groups = [{"control": (5, 0), "treatment": (5, 0)}] * 2
    got = reference.stratified(groups, "control", "treatment", 0.95)
    assert (got.standard_error, got.absolute_difference_ci, got.p_value) == (
        0.0,
        None,
        None,
    )
    assert got.relative_improvement_pct is None


def test_stratified_refuses_a_group_the_product_would_merge():
    with pytest.raises(ValueError, match="fewer than 2 users"):
        reference.stratified(
            [{"control": (1, 0), "treatment": (5, 1)}], "control", "treatment", 0.95
        )
