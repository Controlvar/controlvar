"""What the interaction pair analysis tests, and what its answer can mean.

The service runs on fake readers, as in ``test_interaction_pair_analysis.py``,
except that here each user converts per event name, so an experiment's two
metrics can tell different stories.

* only each experiment's primary metric is tested: a planted interaction on
  another conversion metric is not seen;
* a pair is tested on its own, with the tested experiment's correction across
  that experiment's own treatments and nothing across pairs: one treatment
  under Bonferroni keeps its p-value;
* an experiment that changes who enters the other can make the test report an
  interaction that no user has: A lifts each kind of user by the same amount
  whatever B shows them, but B's treatment sends A a different mix of users;
* with few users the test has little power: the page's constant-relative-lift
  table, an interaction at 2,000 users per group, is not significant at 200.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Dict, Set, Tuple
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from backend.app.models.experiment import MetricType
from backend.app.services.interaction_analysis import floor_reason, interaction_test
from backend.app.services.interaction_detection_service import (
    InteractionDetectionService,
)

pytestmark = pytest.mark.unit

SERVICE = "backend.app.services.interaction_detection_service"


def _metric(name: str, event: str, primary: bool):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        event_name=event,
        metric_type=MetricType.CONVERSION,
        is_primary=primary,
    )


def _experiment(name: str, metrics, correction="benjamini_hochberg", level=0.95):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        variants=[
            SimpleNamespace(id=uuid4(), name="control", is_control=True),
            SimpleNamespace(id=uuid4(), name="treatment", is_control=False),
        ],
        metric_definitions=metrics,
        mutual_exclusion_group_id=None,
        correction_method=correction,
        confidence_level=level,
    )


class Population:
    """Users in both experiments; each converts on a set of event names in A."""

    def __init__(self, exp_a, exp_b):
        self.a, self.b = exp_a, exp_b
        self.users: Dict[str, Tuple[str, str, Set[str]]] = {}

    def add(self, a: str, b: str, n: int, converts: Dict[str, int], tag: str):
        for i in range(n):
            events = {event for event, k in converts.items() if i < k}
            self.users[f"{tag}-{a}-{b}-{i}"] = (a, b, events)

    @staticmethod
    def _vid(exp, name):
        return str(next(v.id for v in exp.variants if v.name == name))

    def arm_totals(self, db, experiment_id):
        exp, side = (self.a, 0) if str(experiment_id) == str(self.a.id) else (self.b, 1)
        out: Dict[str, int] = {}
        for user in self.users.values():
            vid = self._vid(exp, user[side])
            out[vid] = out.get(vid, 0) + 1
        return out

    def shared(self, db, smaller_id, larger_id):
        first = self.a if str(smaller_id) == str(self.a.id) else self.b
        second = self.b if first is self.a else self.a
        side = 0 if first is self.a else 1
        return {
            uid: (self._vid(first, u[side]), self._vid(second, u[1 - side]))
            for uid, u in self.users.items()
        }

    def converters(self, db, experiment_id, variant_id, event_name):
        if str(experiment_id) != str(self.a.id):
            return set()
        return {
            uid
            for uid, (a, _b, events) in self.users.items()
            if self._vid(self.a, a) == str(variant_id) and event_name in events
        }


def _analyse(population: Population):
    service = InteractionDetectionService()
    with (
        patch.object(
            InteractionDetectionService,
            "_arm_totals",
            side_effect=population.arm_totals,
        ),
        patch.object(
            InteractionDetectionService,
            "_shared_assignments",
            side_effect=population.shared,
        ),
        patch(f"{SERVICE}.converting_user_ids", side_effect=population.converters),
    ):
        return service.analyze_pair_interactions(
            population.a, population.b, MagicMock()
        )


def _row_of(response, experiment):
    (row,) = [
        r for r in response.interaction_results if r.experiment_id == str(experiment.id)
    ]
    return row


@pytest.mark.regression
def test_only_the_primary_metric_is_tested():
    primary = _metric("Signup", "signup", True)
    # Named to sort first, so only "primary first" puts Signup ahead of it.
    other = _metric("Aardvark purchase", "purchase", False)
    a = _experiment("A", [other, primary])
    b = _experiment("B", [_metric("B signup", "b_signup", True)])
    population = Population(a, b)
    # Signup: +10 points in both of B's arms (no interaction). Purchase: +20
    # points in B's control and none in its treatment (a strong interaction).
    population.add("control", "control", 1000, {"signup": 100, "purchase": 100}, "c")
    population.add("treatment", "control", 1000, {"signup": 200, "purchase": 300}, "c")
    population.add("control", "treatment", 1000, {"signup": 200, "purchase": 100}, "t")
    population.add(
        "treatment", "treatment", 1000, {"signup": 300, "purchase": 100}, "t"
    )

    row = _row_of(_analyse(population), a)
    assert row.metric_id == str(primary.id)
    assert row.metric_name == "Signup"
    assert row.statistic == pytest.approx(0.0, abs=1e-9)
    assert row.is_significant is False

    # The same users with Purchase as the primary metric: found at once.
    other.is_primary, primary.is_primary = True, False
    row = _row_of(_analyse(population), a)
    assert row.metric_name == "Aardvark purchase"
    assert row.p_value < 1e-6 and row.is_significant is True


@pytest.mark.regression
def test_a_pair_is_corrected_only_across_the_experiments_own_treatments():
    a = _experiment("A", [_metric("Signup", "signup", True)], correction="bonferroni")
    b = _experiment("B", [_metric("B signup", "b_signup", True)])
    population = Population(a, b)
    population.add("control", "control", 3000, {"signup": 300}, "c")
    population.add("treatment", "control", 3000, {"signup": 375}, "c")
    population.add("control", "treatment", 3000, {"signup": 300}, "t")
    population.add("treatment", "treatment", 3000, {"signup": 300}, "t")
    row = _row_of(_analyse(population), a)
    assert row.correction_method == "bonferroni"
    # One treatment: Bonferroni over one comparison leaves the p-value alone.
    assert row.corrected_p_value == pytest.approx(row.p_value, rel=1e-12)
    assert 0.0 < row.p_value < 0.05


@pytest.mark.regression
def test_a_change_in_who_enters_a_can_read_as_an_interaction():
    """Two kinds of user: 'new' converts at 10% and A lifts them by 10 points;
    'returning' converts at 40% and A does not move them. Neither lift depends
    on B. B's treatment sends A mostly returning users, so A's lift is about
    9 points among B's control users and about 1 point among B's treatment
    users, and the test reports an interaction."""
    a = _experiment("A", [_metric("Signup", "signup", True)])
    b = _experiment("B", [_metric("B signup", "b_signup", True)])
    population = Population(a, b)
    mix = {"control": (900, 100), "treatment": (100, 900)}  # (new, returning)
    for b_arm, (new, returning) in mix.items():
        population.add("control", b_arm, new, {"signup": new // 10}, f"new-{b_arm}")
        population.add("treatment", b_arm, new, {"signup": new // 5}, f"new-{b_arm}")
        for a_arm in ("control", "treatment"):
            population.add(
                a_arm, b_arm, returning, {"signup": returning * 4 // 10}, f"ret-{b_arm}"
            )
    row = _row_of(_analyse(population), a)
    lifts = {}
    for arm in row.arms:
        lifts[arm.other_variant_name] = arm.effect
    assert lifts["control"] == pytest.approx(0.09, abs=1e-12)
    assert lifts["treatment"] == pytest.approx(0.01, abs=1e-12)
    assert row.is_significant is True


#: The page's constant-relative-lift example: 10% to 15% in one of B's
#: variants, 20% to 30% in the other.
def _relative_lift_table(n: int):
    return [[n, n], [n, n]], [[n // 10, n // 5], [n * 15 // 100, n * 3 // 10]]


@pytest.mark.regression
def test_the_relative_lift_example_is_not_significant_at_200_users_per_group():
    n, x = _relative_lift_table(2000)
    assert interaction_test(n, x).p_value < 0.05
    n, x = _relative_lift_table(200)
    assert floor_reason(n, x) is None
    small = interaction_test(n, x)
    assert round(small.statistic, 2) == 0.85
    assert round(small.p_value, 2) == 0.36
    assert small.p_value > 0.05
