"""The results states the user guide promises, and an API key's refusals (#939, T147).

The results journey (``experiment-results.yaml``) walks two states of "Reading
Results" beside its A/B lift: a sample-ratio mismatch (Control 300, Treatment
200 on a 50/50 split) and a significant result under the minimum sample (60
users a variant). The API key journey (``api-key-first-assignment.yaml``) sends
its key where the guide says it is refused. What decides whether those checks
can fail is pure, and is tested here, in the unit job:

* ``oracles.sample_ratio_p`` is Pearson's chi-square goodness of fit against
  the allocation, with its p-value pinned for the journey's split, and it is
  told apart from the tests a defect would use instead;
* each dataset can fail the way the guide's state can: the mismatch is below
  the guide's 0.001 with both variants past the floor and a significant winner
  to withhold, and the minimum-sample population is under the default floor
  of 100 and significant, so a recommendation to ship is withheld for the
  floor alone; the oracles' arguments are the counts the traffic steps send;
* every results read of an experiment comes after its traffic, so the
  five-minute results cache cannot hold an answer from before it (the two
  revenue experiments at the end are the fourth and fifth with traffic);
* the key is refused on the management route and the ruleset between a call
  that accepts it and its deletion, and each refusal's body is in the guide.

Temporary files only; no network, no browser, no git.
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import oracles

JOURNEYS = RUNNER_ROOT / "journeys"
#: The guide's threshold for a sample-ratio mismatch ("Sample Ratio Check").
SRM_THRESHOLD = 0.001
#: The minimum sample a metric gets when it names none ("Experiment Summary Card").
DEFAULT_FLOOR = 100
#: The significance the results journey's experiments are judged at (95%).
ALPHA = 0.05


def _steps(journey: str) -> List[Dict[str, Any]]:
    data = yaml.safe_load((JOURNEYS / f"{journey}.yaml").read_text(encoding="utf-8"))
    return data["steps"]


def _step(steps: List[Dict[str, Any]], step_id: str) -> Dict[str, Any]:
    return next(step for step in steps if step["id"] == step_id)


def _index(steps: List[Dict[str, Any]], step_id: str) -> int:
    return next(i for i, step in enumerate(steps) if step["id"] == step_id)


def _population(step: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    return step["do"]["traffic"]["variants"]


def _fisher(population: Dict[str, Dict[str, int]]) -> float:
    return oracles.fisher_exact_p(
        control_users=population["Control"]["assigned"],
        control_converted=population["Control"]["converted"],
        treatment_users=population["Treatment"]["assigned"],
        treatment_converted=population["Treatment"]["converted"],
    )


def _chi_square_by_hand(observed: List[int], shares: List[float]) -> float:
    """Pearson's statistic and its one-degree p-value, with the standard library.

    For one degree of freedom the chi-square survival function is
    ``erfc(sqrt(x / 2))``.
    """
    total = sum(observed)
    expected = [total * s / sum(shares) for s in shares]
    statistic = sum((o - e) ** 2 / e for o, e in zip(observed, expected))
    return math.erfc(math.sqrt(statistic / 2))


# ---------------------------------------------------------------------------
# The sample-ratio oracle
# ---------------------------------------------------------------------------
def test_the_ratio_oracle_is_pearsons_chi_square_against_the_allocation():
    p = oracles.sample_ratio_p(300, 200)
    assert p == pytest.approx(_chi_square_by_hand([300, 200], [50, 50]), rel=1e-9)
    # Pinned: the value the results journey expects for 300/200 on 50/50.
    assert p == pytest.approx(7.744216431044084e-06, rel=1e-12)
    assert p < SRM_THRESHOLD
    # An exact split: the p-value the page shows as "p = 1".
    assert oracles.sample_ratio_p(400, 400) == 1.0
    assert oracles.sample_ratio_p(60, 60) == 1.0


def test_the_allocation_is_normalised_and_decides_the_expected_counts():
    assert oracles.sample_ratio_p(300, 200, 0.5, 0.5) == pytest.approx(
        oracles.sample_ratio_p(300, 200), rel=1e-12
    )
    # 300/200 is exactly what a 60/40 allocation expects.
    assert oracles.sample_ratio_p(300, 200, 60, 40) == pytest.approx(1.0)
    uneven = oracles.sample_ratio_p(300, 200, 70, 30)
    assert uneven == pytest.approx(_chi_square_by_hand([300, 200], [70, 30]), rel=1e-9)


def test_the_ratio_oracle_is_told_apart_from_the_tests_a_defect_would_use():
    """Each other test is outside the API step's 1e-9 tolerance."""
    from scipy import stats

    p = oracles.sample_ratio_p(300, 200)
    others = {
        "G-test": stats.power_divergence(
            [300, 200], [250, 250], lambda_="log-likelihood"
        ).pvalue,
        "chi-square with Yates": stats.chi2.sf((abs(300 - 250) - 0.5) ** 2 / 125, 1),
        "exact binomial": stats.binomtest(300, 500, 0.5).pvalue,
        "two degrees of freedom": stats.chi2.sf(20.0, 2),
        "one-sided normal": stats.norm.sf(math.sqrt(20.0)),
    }
    for name, other in others.items():
        assert not math.isclose(other, p, rel_tol=1e-9), name


@pytest.mark.parametrize(
    "args",
    [
        {"control_users": -1, "treatment_users": 10},
        {"control_users": 0, "treatment_users": 0},
        {"control_users": 10, "treatment_users": 10, "control_allocation": 0},
    ],
)
def test_a_count_or_allocation_the_check_cannot_take_is_refused(args):
    with pytest.raises(ValueError):
        oracles.sample_ratio_p(**args)


def test_the_ratio_oracle_is_registered():
    assert oracles.ORACLES["sample_ratio_p"] is oracles.sample_ratio_p


# ---------------------------------------------------------------------------
# The datasets can fail the way the guide's states can
# ---------------------------------------------------------------------------
def test_the_mismatch_withholds_a_winner_that_is_past_the_floor():
    steps = _steps("experiment-results")
    population = _population(_step(steps, "ratio-traffic"))
    users = [population["Control"]["assigned"], population["Treatment"]["assigned"]]
    assert oracles.sample_ratio_p(*users) < SRM_THRESHOLD
    # The floor is reached and Treatment is better and significant, so without
    # the check the recommendation would be SHIP VARIANT: INCONCLUSIVE is the
    # check's doing, and a defect that ignores it is seen.
    assert min(users) >= DEFAULT_FLOOR
    rates = {n: v["converted"] / v["assigned"] for n, v in population.items()}
    assert rates["Treatment"] > rates["Control"]
    assert _fisher(population) < ALPHA


def test_the_minimum_sample_dataset_is_significant_under_the_default_floor():
    steps = _steps("experiment-results")
    population = _population(_step(steps, "floor-traffic"))
    assert max(v["assigned"] for v in population.values()) < DEFAULT_FLOOR
    # Significant, so "not SHIP VARIANT" is the floor's doing, not the p-value's.
    assert _fisher(population) < ALPHA
    # The metric names no minimum, so the documented default of 100 applies.
    metrics = _step(steps, "floor-experiment")["do"]["api"]["body"]["metrics"]
    assert all("minimum_sample_size" not in metric for metric in metrics)
    expected = _step(steps, "floor-results-api")["expect"]["json"]
    assert expected["summary.recommendation"] == "CONTINUE_TESTING"
    assert expected["summary.has_winner"] is True
    assert expected["sample_size_adequate"] is False
    assert expected["metrics.0.variants.1.is_significant"] is True


def _oracle_args(node: Any) -> List[Dict[str, Any]]:
    """Every oracle call (name and args) under *node*."""
    found: List[Dict[str, Any]] = []
    if isinstance(node, dict):
        if "name" in node and "args" in node and node["name"] in oracles.ORACLES:
            found.append(node)
        for value in node.values():
            found.extend(_oracle_args(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_oracle_args(value))
    return found


@pytest.mark.parametrize(
    "traffic, readers",
    [
        ("ratio-traffic", ["ratio-results-api", "ratio-results-page"]),
        ("floor-traffic", ["floor-results-api", "floor-results-page"]),
        ("revenue-traffic", ["revenue-results"]),
        ("doubled-traffic", ["doubled-results"]),
    ],
)
def test_each_oracle_is_given_the_counts_its_traffic_sends(traffic, readers):
    steps = _steps("experiment-results")
    population = _population(_step(steps, traffic))
    sent = {
        "control_users": population["Control"]["assigned"],
        "control_converted": population["Control"]["converted"],
        "treatment_users": population["Treatment"]["assigned"],
        "treatment_converted": population["Treatment"]["converted"],
    }
    calls = [call for step in readers for call in _oracle_args(_step(steps, step))]
    assert calls, "no oracle is named: the check reads nothing"
    for call in calls:
        for name, value in call["args"].items():
            if name in sent:
                assert value == sent[name], (call["name"], name)


def test_the_mismatch_is_read_from_the_api_before_the_page():
    """A page that drops the notice fails on its own step, the API step passed."""
    steps = _steps("experiment-results")
    assert _index(steps, "ratio-results-api") < _index(steps, "ratio-results-page")


# ---------------------------------------------------------------------------
# The results cache cannot hold an answer from before the traffic
# ---------------------------------------------------------------------------
PLACEHOLDER = re.compile(r"\{\{([a-z0-9-]+)\}\}")


def _results_reads(step: Dict[str, Any]) -> List[str]:
    """The saved experiment ids whose results this step reads (API or page)."""
    do = step.get("do") or {}
    targets = []
    if "api" in do:
        targets.append(do["api"]["path"])
    if "goto" in do:
        targets.append(do["goto"])
    names = []
    for target in targets:
        if "/results/" in target:
            names.extend(PLACEHOLDER.findall(target.split("/results/", 1)[1]))
    return names


def test_every_results_read_comes_after_its_experiments_traffic():
    steps = _steps("experiment-results")
    traffic_at = {
        step["do"]["traffic"]["experiment"]: index
        for index, step in enumerate(steps)
        if "traffic" in (step.get("do") or {})
    }
    # The two revenue experiments (test_docs_oracles_revenue.py) are the
    # fourth and fifth, each with its own traffic step and user prefix.
    assert set(traffic_at) == {
        "experiment-id",
        "ratio-experiment-id",
        "floor-experiment-id",
        "revenue-experiment-id",
        "doubled-experiment-id",
    }
    reads = 0
    for index, step in enumerate(steps):
        for name in _results_reads(step):
            reads += 1
            assert index > traffic_at[name], step["id"]
        click = (step.get("do") or {}).get("click") or {}
        if click.get("name") == "View results":
            assert index > traffic_at["experiment-id"], step["id"]
    assert reads >= 5


def test_a_read_before_the_traffic_would_be_seen():
    steps = _steps("experiment-results")
    early = dict(_step(steps, "ratio-results-api"))
    planted = list(steps)
    planted.insert(_index(steps, "ratio-traffic"), early)
    traffic = _index(planted, "ratio-traffic")
    assert any(
        "ratio-experiment-id" in _results_reads(step) for step in planted[:traffic]
    )


# ---------------------------------------------------------------------------
# The API key's refusals
# ---------------------------------------------------------------------------
def _sends_the_key(step: Dict[str, Any]) -> bool:
    call = (step.get("do") or {}).get("api") or {}
    return call.get("key") == "sdk-key"


def test_the_refusals_come_while_the_key_is_valid():
    """Between an SDK call the key passes and the key's deletion."""
    steps = _steps("api-key-first-assignment")
    deleted = _index(steps, "confirm-delete")
    for refusal, status in (("management-route", 401), ("ruleset-scope", 403)):
        at = _index(steps, refusal)
        step = steps[at]
        assert _sends_the_key(step) and step["expect"]["status"] == status
        assert at < deleted
        accepted_before = [
            s for s in steps[:at] if _sends_the_key(s) and s["expect"]["status"] == 200
        ]
        accepted_after = [
            s
            for s in steps[at + 1 : deleted]
            if _sends_the_key(s) and s["expect"]["status"] == 200
        ]
        assert accepted_before and accepted_after, refusal


def test_the_management_route_takes_a_user_login_and_is_no_sdk_route():
    step = _step(_steps("api-key-first-assignment"), "management-route")
    path = step["do"]["api"]["path"]
    assert path.startswith("/api/v1/experiments")
    for sdk in ("/api/v1/tracking/", "/feature-flags/evaluate/", "/api/v1/sdk/"):
        assert sdk not in path


@pytest.mark.parametrize(
    "journey, step_id, guide",
    [
        ("api-key-first-assignment", "management-route", "security/api-keys.md"),
        ("api-key-first-assignment", "ruleset-scope", "security/api-keys.md"),
    ],
)
def test_each_refusal_body_is_the_guides(journey, step_id, guide):
    detail = _step(_steps(journey), step_id)["expect"]["json"]["detail"]
    text = " ".join((REPO_ROOT / "docs" / guide).read_text(encoding="utf-8").split())
    assert detail in text


def test_the_minimum_sample_reason_the_page_shows_is_the_guides():
    page = _step(_steps("experiment-results"), "floor-results-page")["expect"]["aria"]
    # The recommendation reason, not the Planned sample card's progress line.
    reason = re.search(r"paragraph: /(Not every [^/]+)/", page)
    assert reason is not None
    text = " ".join(
        (REPO_ROOT / "docs" / "guides" / "user-guide.md").read_text("utf-8").split()
    )
    assert reason.group(1) in text
