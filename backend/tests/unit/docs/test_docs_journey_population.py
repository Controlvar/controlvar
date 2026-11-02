"""The docs-journey runner's saved values, its population and its oracles (#939, D3b).

The experiment journeys send an experiment a population they choose (so many
users per variant, so many of them converting) and hold the results to an
oracle computed with scipy. The parts that decide whether those checks can fail
are pure, and are tested here, in the unit job:

* ``traffic.py`` reimplements the documented assignment hash; it must agree
  with the SDK contract's golden vectors, give each variant exactly its count,
  and report an assignment the API answers differently;
* ``oracles.fisher_exact_p`` must be Fisher's two-sided exact test, and must be
  told apart from the tests a defect would put in its place;
* ``checks.agrees`` and ``checks.within`` compare at the precision shown and
  within the tolerance given, and no looser;
* the loader refuses a saved value used before it is saved, a secret put where
  it would be written, and the new expectations where they cannot apply;
* a key kept from the screen by its documented prefix (``keep: {prefix:
  eptk_}``) is a secret a later step sends, and a prefix the guide does not
  give is refused.

Temporary files only; no network, no browser, no git.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import checks, loader, oracles, traffic, values
from docs_runner.model import Journey

TODAY = datetime.date(2026, 10, 6)
COUNTS = {
    "control_users": 400,
    "control_converted": 40,
    "treatment_users": 400,
    "treatment_converted": 64,
}


# ---------------------------------------------------------------------------
# Saved values
# ---------------------------------------------------------------------------
def test_placeholders_are_found_and_filled_in():
    assert values.placeholders("/api/v1/results/{{experiment-id}}") == ["experiment-id"]
    assert values.placeholders_in({"a": ["{{x}}", {"{{y}}": 1}]}) == ["x", "y"]
    assert (
        values.substitute(
            "/r/{{experiment-id}}?q={{n}}", {"experiment-id": "e1", "n": "2"}
        )
        == "/r/e1?q=2"
    )
    assert values.substitute_in({"k": ["{{x}}"], "n": 3}, {"x": "v"}) == {
        "k": ["v"],
        "n": 3,
    }
    assert values.placeholders("{{Not-A-Slug}} {{ x }}") == []


def test_an_unknown_placeholder_names_itself():
    with pytest.raises(KeyError, match="'nope'"):
        values.substitute("/r/{{nope}}", {})


@pytest.mark.parametrize("value", [None, True, {"a": 1}, [1]])
def test_only_a_string_or_a_number_is_put_into_a_path(value):
    with pytest.raises(ValueError):
        values.as_text(value)


def test_numbers_and_strings_are_put_into_a_path_as_text():
    assert values.as_text("e1") == "e1"
    assert values.as_text(7) == "7"


# ---------------------------------------------------------------------------
# The population
# ---------------------------------------------------------------------------
def test_the_hash_agrees_with_the_sdk_contract():
    """Every golden vector's bucket is the hash's integer part of 100 x position."""
    vectors = json.loads(
        (REPO_ROOT / "tests" / "sdk-contract" / "golden-vectors.json").read_text(
            encoding="utf-8"
        )
    )["hash_vectors"]
    assert vectors
    for vector in vectors:
        assert traffic.bucket(vector["user_id"], vector["flag_key"]) == int(
            vector["expected_hash"] * 100
        ), vector


def test_the_variants_take_the_buckets_in_order():
    split = [("Control", 50), ("Treatment", 50)]
    # user-123 / my-flag is bucket 69 in the contract.
    assert traffic.bucket("user-123", "my-flag") == 69
    assert traffic.variant_for("user-123", "my-flag", split) == "Treatment"
    assert traffic.variant_for("user-123", "my-flag", [("A", 70), ("B", 30)]) == "A"
    assert traffic.variant_for("user-123", "my-flag", [("A", 69), ("B", 31)]) == "B"
    # A tail left by allocations under 100 goes to the last variant.
    assert traffic.variant_for("user-123", "my-flag", [("A", 10), ("B", 10)]) == "B"


def test_each_variant_gets_exactly_its_count():
    split = [("Control", 50), ("Treatment", 50)]
    chosen = traffic.choose(
        "docs_journey_results", split, {"Control": 400, "Treatment": 400}, "u"
    )
    assert {name: len(users) for name, users in chosen.items()} == {
        "Control": 400,
        "Treatment": 400,
    }
    for name, users in chosen.items():
        assert len(set(users)) == len(users)
        for user in users:
            assert traffic.variant_for(user, "docs_journey_results", split) == name
    assert set(chosen["Control"]).isdisjoint(chosen["Treatment"])
    # The same answer every time: the ids are a fixed sequence.
    again = traffic.choose(
        "docs_journey_results", split, {"Control": 400, "Treatment": 400}, "u"
    )
    assert again == chosen


def test_a_variant_the_experiment_does_not_have_is_refused():
    with pytest.raises(traffic.TrafficError, match="no variant 'Treatmnet'"):
        traffic.choose("k", [("Control", 50), ("Treatment", 50)], {"Treatmnet": 1}, "u")


def test_a_count_no_candidate_can_fill_is_refused():
    with pytest.raises(traffic.TrafficError, match="candidate users gave 'B'"):
        traffic.choose("k", [("A", 100), ("B", 0)], {"B": 1}, "u")


class FakeApi:
    """The tracking API, assigning by the documented hash unless told otherwise."""

    def __init__(
        self,
        allocations: List[Tuple[str, int]],
        *,
        swap: Optional[str] = None,
        status: int = 200,
        track_status: int = 200,
    ):
        self.allocations = allocations
        self.swap = swap
        self.status = status
        self.track_status = track_status
        self.assigned: Dict[str, str] = {}
        self.tracked: List[Tuple[str, str]] = []

    def __call__(self, method: str, path: str, body: Optional[Dict[str, Any]]):
        if path == traffic.ASSIGN_PATH:
            if self.status != 200:
                return self.status, {"detail": "Invalid API Key"}
            user = body["user_id"]
            name = traffic.variant_for(user, body["experiment_key"], self.allocations)
            if user == self.swap:
                name = next(n for n, _ in self.allocations if n != name)
            self.assigned[user] = name
            return 200, {"variant_name": name, "assigned": True, "reason": "assigned"}
        if path == traffic.TRACK_PATH:
            self.tracked.append((body["user_id"], body["event_type"]))
            return self.track_status, {"event_type": body["event_type"]}
        raise AssertionError(path)


SPLIT = [("Control", 50), ("Treatment", 50)]


def _send(api: FakeApi, counts=(("Control", 40, 4), ("Treatment", 40, 7))):
    chosen = traffic.choose("exp", SPLIT, {name: n for name, n, _ in counts}, "u")
    return traffic.send(
        api,
        experiment_key="exp",
        event="checkout_completed",
        chosen=chosen,
        converted={name: c for name, _, c in counts},
        prefix="u",
    )


def test_the_population_is_sent_as_chosen():
    api = FakeApi(SPLIT)
    outcome = _send(api)
    assert outcome.problems == []
    assert outcome.variants == {
        "Control": {"assigned": 40, "as_chosen": 40, "converted": 4},
        "Treatment": {"assigned": 40, "as_chosen": 40, "converted": 7},
    }
    assert outcome.requests == 80 + 11
    assert len(api.tracked) == 11
    assert {event for _, event in api.tracked} == {"checkout_completed"}
    # Each converter is a user assigned to the variant it converts in.
    converters = [user for user, _ in api.tracked]
    assert sum(api.assigned[u] == "Treatment" for u in converters) == 7
    record = outcome.record()
    assert record["users"] == "u-NNNNN"
    assert "key" not in json.dumps(record).lower().replace("experiment_key", "")


def test_an_assignment_other_than_the_hash_fails_and_nothing_is_tracked():
    chosen = traffic.choose("exp", SPLIT, {"Control": 5, "Treatment": 5}, "u")
    api = FakeApi(SPLIT, swap=chosen["Treatment"][2])
    outcome = traffic.send(
        api,
        experiment_key="exp",
        event="e",
        chosen=chosen,
        converted={"Control": 1, "Treatment": 1},
        prefix="u",
    )
    assert len(outcome.problems) == 1
    assert "the documented hash puts it in 'Treatment'" in outcome.problems[0]
    assert api.tracked == []


def test_a_refused_key_stops_after_the_problems_kept():
    api = FakeApi(SPLIT, status=401)
    outcome = _send(api)
    assert len(outcome.problems) == traffic.PROBLEMS_KEPT
    assert outcome.requests == traffic.PROBLEMS_KEPT
    assert all("answered 401" in p for p in outcome.problems)


def test_a_refused_event_is_a_problem():
    outcome = _send(FakeApi(SPLIT, track_status=422))
    assert outcome.problems
    assert "tracking checkout_completed" in outcome.problems[0]


# ---------------------------------------------------------------------------
# The oracle
# ---------------------------------------------------------------------------
def _fisher_by_hand(a: int, b: int, c: int, d: int) -> float:
    """Fisher's two-sided p from the hypergeometric terms: the tables as likely or less."""
    row1, col1, total = a + b, a + c, a + b + c + d

    def p(x: int) -> float:
        return (
            math.comb(col1, x)
            * math.comb(total - col1, row1 - x)
            / math.comb(total, row1)
        )

    observed = p(a)
    low, high = max(0, row1 + col1 - total), min(row1, col1)
    return sum(p(x) for x in range(low, high + 1) if p(x) <= observed * (1 + 1e-7))


def test_the_oracle_is_fishers_two_sided_exact_test():
    p = oracles.fisher_exact_p(**COUNTS)
    assert p == pytest.approx(_fisher_by_hand(64, 336, 40, 360), rel=1e-9)
    # Pinned: the value the results journey expects for 400/40 and 400/64.
    assert p == pytest.approx(0.015299254059095983, rel=1e-12)
    # Which variant is the control does not change a two-sided p.
    swapped = oracles.fisher_exact_p(400, 64, 400, 40)
    assert swapped == pytest.approx(p, rel=1e-12)


def test_the_oracle_is_told_apart_from_the_tests_a_defect_would_use():
    """Each other test is outside the API tolerance and differs at four places."""
    from scipy import stats

    p = oracles.fisher_exact_p(**COUNTS)
    table = [[64, 336], [40, 360]]
    pooled = 104 / 800
    z = (0.16 - 0.10) / math.sqrt(pooled * (1 - pooled) * (2 / 400))
    others = {
        "z-test": 2 * stats.norm.sf(abs(z)),
        "chi-square with Yates": stats.chi2_contingency(table)[1],
        "chi-square without Yates": stats.chi2_contingency(table, correction=False)[1],
        "one-sided Fisher": stats.fisher_exact(table, alternative="greater")[1],
    }
    for name, other in others.items():
        assert not checks.within(other, p, 1e-9), name
        assert not checks.agrees(f"{other:.4f}", p), name
    assert checks.agrees(f"{p:.4f}", p)


def test_more_converted_than_assigned_is_refused():
    with pytest.raises(ValueError):
        oracles.fisher_exact_p(10, 11, 10, 1)


# ---------------------------------------------------------------------------
# Comparisons
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "shown, value, agreed",
    [
        ("0.0153", 0.015299254059095983, True),
        ("0.0153", 0.0156, False),
        ("0.0153", 0.01535, True),
        ("0.0153", 0.015351, False),
        ("47,036", 47036, True),
        ("47,036", 47036.6, False),
        ("16.00%", 16.0, True),
        ("p = 1", 1.0, True),
        ("p = 1", 0.4, False),
    ],
)
def test_a_shown_number_agrees_at_the_precision_shown(shown, value, agreed):
    assert checks.agrees(shown, value) is agreed


def test_a_number_within_a_relative_tolerance():
    assert checks.within(0.0153, 0.0153 * (1 + 5e-10), 1e-9)
    assert not checks.within(0.0153, 0.0153 * (1 + 2e-9), 1e-9)
    assert not checks.within(True, 1.0, 1e-9)
    assert not checks.within("0.0153", 0.0153, 1e-9)
    assert not checks.within(None, 0.0153, 1e-9)


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------
GUIDE = """# Reading results

## Results {#results}

Open **Results**: the **p-value** of **Treatment** is in the table.
"""

INVENTORY: Dict[str, Any] = {
    "pages": {
        "guides/results.md": {
            "class": "journey",
            "journey": "results",
            "reason": "walked",
        },
    },
}

FISHER = {"name": "fisher_exact_p", "args": dict(COUNTS)}

GOOD: Dict[str, Any] = {
    "guide": "guides/results.md",
    "stack": "compose-dev",
    "profile": "core",
    "video": False,
    "written": TODAY,
    "steps": [
        {
            "id": "find",
            "doc": "results",
            "do": {
                "api": {"method": "GET", "path": "/api/v1/experiments/", "as": "admin"}
            },
            "expect": {"status": 200},
            "save": {"experiment-id": {"path": "items.0.id"}},
            "fail": "the experiment is not found",
        },
        {
            "id": "key",
            "doc": "results",
            "do": {
                "api": {
                    "method": "POST",
                    "path": "/api/v1/api-keys",
                    "as": "admin",
                    "body": {"name": "k"},
                }
            },
            "expect": {"status": 201},
            "save": {"sdk-key": {"path": "key", "secret": True}},
            "fail": "no key",
        },
        {
            "id": "traffic",
            "doc": "results",
            "do": {
                "traffic": {
                    "experiment": "experiment-id",
                    "key": "sdk-key",
                    "as": "admin",
                    "event": "checkout_completed",
                    "users": "results-user",
                    "variants": {
                        "Control": {"assigned": 400, "converted": 40},
                        "Treatment": {"assigned": 400, "converted": 64},
                    },
                }
            },
            "fail": "a user is assigned other than chosen",
        },
        {
            "id": "evaluate",
            "doc": "results",
            "do": {
                "api": {
                    "method": "GET",
                    "path": "/api/v1/feature-flags/evaluate/f?user_id=u",
                    "as": "anonymous",
                    "key": "sdk-key",
                }
            },
            "expect": {"status": 200},
            "fail": "the key is refused",
        },
        {
            "id": "results-api",
            "doc": "results",
            "do": {
                "api": {
                    "method": "GET",
                    "path": "/api/v1/results/{{experiment-id}}",
                    "as": "admin",
                }
            },
            "expect": {
                "status": 200,
                "computed": {
                    "metrics.0.variants.1.p_value": {"oracle": FISHER, "rel": 1e-9}
                },
            },
            "fail": "the p-value is not Fisher's",
        },
        {
            "id": "results-page",
            "doc": "results",
            "do": {"goto": "/results/{{experiment-id}}"},
            "expect": {
                "cells": [{"row": "Treatment", "column": "p-value", "oracle": FISHER}],
                "aria": "- table",
            },
            "fail": "the page's p-value is not Fisher's",
        },
    ],
}


def _context(tmp_path: Path) -> loader.Context:
    docs = tmp_path / "docs"
    (docs / "guides").mkdir(parents=True, exist_ok=True)
    (docs / "guides" / "results.md").write_text(GUIDE, encoding="utf-8")
    return loader.Context(
        docs_root=docs,
        inventory=INVENTORY,
        doc_examples={},
        oracles=oracles.ORACLES,
        today=TODAY,
    )


def _load(tmp_path: Path, data: Dict[str, Any]) -> Journey:
    path = tmp_path / "journeys" / "results.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return loader.load(path, _context(tmp_path))


def _with(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    data = copy.deepcopy(GOOD)
    change(data)
    return data


def _step(data: Dict[str, Any], step_id: str) -> Dict[str, Any]:
    return next(step for step in data["steps"] if step["id"] == step_id)


def test_a_journey_with_a_population_loads(tmp_path):
    journey = _load(tmp_path, GOOD)
    assert [step.do.kind for step in journey.steps] == [
        "api",
        "api",
        "traffic",
        "api",
        "api",
        "goto",
    ]
    assert journey.steps[1].save["sdk-key"].secret is True
    assert journey.steps[2].do.traffic.variants["Treatment"].converted == 64
    assert journey.steps[3].do.api.key == "sdk-key"


PLANTS = [
    pytest.param(
        lambda d: d["steps"].insert(0, d["steps"].pop(4)),
        "step 1 (results-api): 'experiment-id' is used before any step saves it",
        id="value-used-before-it-is-saved",
    ),
    pytest.param(
        lambda d: _step(d, "results-page")["do"].update(goto="/results/{{sdk-key}}"),
        "step 6 (results-page): 'sdk-key' is a secret; a secret is sent only as a key",
        id="secret-put-in-an-address",
    ),
    pytest.param(
        lambda d: _step(d, "results-api")["do"]["api"].update(
            path="/api/v1/results/{{sdk-key}}"
        ),
        "step 5 (results-api): 'sdk-key' is a secret",
        id="secret-put-in-a-path",
    ),
    pytest.param(
        lambda d: _step(d, "key")["save"]["sdk-key"].update(secret=False),
        "step 3 (traffic): the key 'sdk-key' is not a secret",
        id="key-saved-as-a-plain-value",
    ),
    pytest.param(
        lambda d: _step(d, "find")["save"].update({"sdk-key": {"path": "items.0.key"}}),
        "step 2 (key): 'sdk-key' is saved twice",
        id="a-name-saved-twice",
    ),
    pytest.param(
        lambda d: _step(d, "traffic")["do"]["traffic"].update(experiment="nothing"),
        "step 3 (traffic): 'nothing' is used before any step saves it",
        id="traffic-names-no-saved-experiment",
    ),
    pytest.param(
        lambda d: _step(d, "results-page").update(save={"x": {"path": "id"}}),
        "step 6 (results-page): save keeps values from an api step's answer only",
        id="save-on-a-screen-step",
    ),
    pytest.param(
        lambda d: _step(d, "traffic").update(expect={"status": 200}),
        "step 3 (traffic): traffic checks what the action says",
        id="traffic-with-an-expect",
    ),
    pytest.param(
        lambda d: d.update(stack="docs-local"),
        "step 3 (traffic): a traffic step needs the compose stack's API; docs-local has none",
        id="traffic-on-a-stack-with-no-api",
    ),
    pytest.param(
        lambda d: _step(d, "results-page")["expect"].update(
            computed={"p": {"oracle": FISHER, "rel": 1e-9}}
        ),
        "step 6 (results-page): a step in a browser cannot expect ['computed']",
        id="computed-on-a-screen-step",
    ),
    pytest.param(
        lambda d: _step(d, "results-api")["expect"].update(
            cells=[{"row": "Treatment", "column": "p-value", "oracle": FISHER}]
        ),
        "step 5 (results-api): an api step expects only status or json",
        id="cells-on-an-api-step",
    ),
    pytest.param(
        lambda d: _step(d, "results-api")["expect"]["computed"][
            "metrics.0.variants.1.p_value"
        ]["oracle"].update(name="z_test_p"),
        "step 5 (results-api): no oracle named 'z_test_p'",
        id="computed-names-an-unknown-oracle",
    ),
    pytest.param(
        lambda d: _step(d, "results-page")["expect"]["cells"][0]["oracle"].update(
            name="z_test_p"
        ),
        "step 6 (results-page): no oracle named 'z_test_p'",
        id="cells-names-an-unknown-oracle",
    ),
    pytest.param(
        lambda d: _step(d, "results-page")["expect"]["cells"][0].update(
            column="Adjusted p"
        ),
        "expect.cells[0].column 'Adjusted p' is not in the guide's text",
        id="cells-column-the-guide-does-not-name",
    ),
    pytest.param(
        lambda d: _step(d, "evaluate")["do"]["api"].update({"as": "admin"}),
        "an api step with a key sends the key alone: give as: anonymous",
        id="key-sent-with-a-login",
    ),
    pytest.param(
        lambda d: _step(d, "traffic")["do"]["traffic"]["variants"]["Control"].update(
            converted=401
        ),
        "converted cannot be more than assigned",
        id="more-converting-than-assigned",
    ),
    pytest.param(
        lambda d: _step(d, "results-api")["expect"]["computed"][
            "metrics.0.variants.1.p_value"
        ].update(rel=0.5),
        "p_value.rel: Input should be less than or equal to 0.01",
        id="a-tolerance-too-loose-to-tell-tests-apart",
    ),
]


@pytest.mark.parametrize("change, expected", PLANTS)
def test_planted_defect_is_refused(tmp_path, change, expected):
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, _with(change))
    problems = refused.value.problems
    assert any(expected in problem for problem in problems), problems


def test_the_log_names_the_key_and_never_its_value(tmp_path):
    journey = _load(tmp_path, GOOD)
    described = [checks.describe_action(step) for step in journey.steps]
    assert described[2] == (
        "send 800 users through the tracking API with the API key sdk-key"
        " (Control 400, 40 sending checkout_completed; Treatment 400, 64 sending"
        " checkout_completed)"
    )
    assert described[3] == (
        "GET /api/v1/feature-flags/evaluate/f?user_id=u with the API key sdk-key"
    )
    assert described[4] == "GET /api/v1/results/{{experiment-id}} as admin"
    expectations = [checks.describe_expect(step) for step in journey.steps]
    assert expectations[2] == checks.TRAFFIC_EXPECTS
    assert "fisher_exact_p(control_users=400" in expectations[4]
    assert "within 1e-09 of it" in expectations[4]
    assert expectations[5].startswith(
        'the p-value of the row "Treatment" shows fisher_exact_p('
    )


# ---------------------------------------------------------------------------
# A key kept from the screen by its prefix
# ---------------------------------------------------------------------------
KEY_GUIDE = """# Keys

## Create {#create}

The key starts `eptk_` and is shown once. Click **Create Key**.
"""


def _keep_journey(keep: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "guide": "guides/results.md",
        "stack": "compose-dev",
        "profile": "core",
        "video": False,
        "written": TODAY,
        "steps": [
            {
                "id": "keep-key",
                "doc": "create",
                "do": {"keep": keep},
                "fail": "no key on the screen",
            },
            {
                "id": "use-key",
                "doc": "create",
                "do": {
                    "api": {
                        "method": "GET",
                        "path": "/api/v1/feature-flags/evaluate/f?user_id=u",
                        "as": "anonymous",
                        "key": "sdk-key",
                    }
                },
                "expect": {"status": 200},
                "fail": "the kept key is refused",
            },
        ],
    }


def _load_keys(tmp_path: Path, data: Dict[str, Any]) -> Journey:
    context = _context(tmp_path)
    (context.docs_root / "guides" / "results.md").write_text(
        KEY_GUIDE, encoding="utf-8"
    )
    path = tmp_path / "journeys" / "results.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return loader.load(path, context)


def test_a_key_kept_by_its_prefix_is_a_secret_a_later_step_sends(tmp_path):
    journey = _load_keys(
        tmp_path,
        _keep_journey({"secret": "sdk-key", "role": "code", "prefix": "eptk_"}),
    )
    keep = journey.steps[0].do.keep
    assert keep.within is None and keep.prefix == "eptk_"
    assert checks.describe_action(journey.steps[0]) == (
        'keep the text of the code starting "eptk_" as sdk-key'
    )


@pytest.mark.parametrize(
    "keep, expected",
    [
        pytest.param(
            {"secret": "sdk-key", "role": "code"},
            "keep finds its element by exactly one of within or prefix",
            id="neither-within-nor-prefix",
        ),
        pytest.param(
            {
                "secret": "sdk-key",
                "role": "code",
                "prefix": "eptk_",
                "within": {"role": "dialog", "name": "Create Key"},
            },
            "keep finds its element by exactly one of within or prefix",
            id="both-within-and-prefix",
        ),
        pytest.param(
            {"secret": "sdk-key", "role": "code", "prefix": "sk_live_"},
            "keep.prefix 'sk_live_' is not in the guide's text",
            id="a-prefix-the-guide-does-not-give",
        ),
    ],
)
def test_a_planted_keep_is_refused(tmp_path, keep, expected):
    with pytest.raises(loader.Refused) as refused:
        _load_keys(tmp_path, _keep_journey(keep))
    assert any(expected in p for p in refused.value.problems), refused.value.problems


# ---------------------------------------------------------------------------
# Flag evaluations
# ---------------------------------------------------------------------------
def test_the_flag_bucket_is_the_documented_md5_mod_100():
    """docs/sdk/javascript.md: user-123 / my-flag is bucket 79 for a flag rollout."""
    assert traffic.flag_bucket("user-123", "my-flag") == 79
    # The whole digest, big-endian, mod 100; not the assignment hash's 69.
    assert traffic.bucket("user-123", "my-flag") == 69
    for n in range(1, 50):
        user = traffic.user_id("u", n)
        digest = hashlib.md5(f"{user}:f".encode(), usedforsecurity=False).digest()
        assert traffic.flag_bucket(user, "f") == int.from_bytes(digest, "big") % 100


class FakeFlags:
    """The evaluate route, bucketing by the documented rollout hash."""

    def __init__(
        self,
        rollout: int,
        *,
        state: str = "on",
        wrong: Optional[str] = None,
        values: Optional[Tuple[Any, Any]] = None,
    ):
        self.rollout = rollout
        self.state = state
        self.wrong = wrong
        #: A value flag's (on_value, off_value): each answer carries ``value``.
        self.values = values
        self.paths: List[str] = []

    def __call__(self, method: str, path: str, body: Optional[Dict[str, Any]]):
        assert method == "GET" and body is None
        self.paths.append(path)
        user = path.split("user_id=", 1)[1].split("&", 1)[0]
        if self.state == "off":
            return 200, self._valued({"enabled": False, "reason": "inactive"})
        if self.state == "rule":
            return 200, self._valued({"enabled": True, "reason": "targeting_rule"})
        enabled = traffic.flag_bucket(user, "f") < self.rollout
        if user == self.wrong:
            enabled = not enabled
        return 200, self._valued({"enabled": enabled, "reason": "rollout"})

    def _valued(self, answer: Dict[str, Any]) -> Dict[str, Any]:
        if self.values is not None:
            answer["value"] = self.values[0] if answer["enabled"] else self.values[1]
        return answer


USERS = [traffic.user_id("u", n) for n in range(1, 201)]


def test_evaluations_as_documented_have_no_problem():
    api = FakeFlags(10)
    outcome = traffic.evaluate(
        api, flag_key="f", users=USERS, reason="rollout", rollout=10, query="&context=x"
    )
    counts = outcome.variants["f"]
    assert outcome.problems == []
    assert counts["evaluated"] == 200
    assert counts["enabled"] == counts["expected_enabled"]
    assert 0 < counts["enabled"] < 200
    assert all(path.endswith("&context=x") for path in api.paths)
    assert api.paths[0].startswith("/api/v1/feature-flags/evaluate/f?user_id=u-00001")


def test_one_user_on_the_wrong_side_of_the_percentage_fails():
    outcome = traffic.evaluate(
        FakeFlags(10, wrong=USERS[7]),
        flag_key="f",
        users=USERS,
        reason="rollout",
        rollout=10,
    )
    assert len(outcome.problems) == 1
    assert outcome.problems[0].startswith(f"{USERS[7]}: enabled")


@pytest.mark.parametrize(
    "state, reason, rollout, problems",
    [
        ("off", "inactive", None, 0),
        ("rule", "targeting_rule", None, 0),
        ("on", "inactive", None, traffic.PROBLEMS_KEPT),
        ("off", "rollout", 0, traffic.PROBLEMS_KEPT),
        ("on", "rollout", 50, traffic.PROBLEMS_KEPT),
    ],
)
def test_every_answer_must_give_the_reason_named(state, reason, rollout, problems):
    outcome = traffic.evaluate(
        FakeFlags(10, state=state),
        flag_key="f",
        users=USERS,
        reason=reason,
        rollout=rollout,
    )
    assert len(outcome.problems) == problems


# A value flag's evaluations (#1497): each answer's value, on and off.
LABELS = ("Complete purchase", "Buy now")


@pytest.mark.parametrize(
    "state, reason, rollout",
    [
        ("on", "rollout", 10),
        ("off", "inactive", None),
        ("rule", "targeting_rule", None),
    ],
)
def test_a_value_flag_answered_as_documented_has_no_problem(state, reason, rollout):
    outcome = traffic.evaluate(
        FakeFlags(10, state=state, values=LABELS),
        flag_key="f",
        users=USERS,
        reason=reason,
        rollout=rollout,
        values=LABELS,
    )
    assert outcome.problems == []
    assert outcome.variants["f"]["evaluated"] == 200


def test_a_form_that_swapped_the_two_values_fails_every_user():
    """C8's planted defect: the on value stored as the off value and back."""
    outcome = traffic.evaluate(
        FakeFlags(10, values=LABELS[::-1]),
        flag_key="f",
        users=USERS,
        reason="rollout",
        rollout=10,
        values=LABELS,
    )
    assert len(outcome.problems) == traffic.PROBLEMS_KEPT
    assert "expected the" in outcome.problems[0]
    assert (
        "value 'Complete purchase'" in outcome.problems[0]
        or "value 'Buy now'" in (outcome.problems[0])
    )


def test_an_answer_without_a_value_fails_a_value_step_and_passes_one_without():
    outcome = traffic.evaluate(
        FakeFlags(10, state="off"),
        flag_key="f",
        users=USERS[:3],
        reason="inactive",
        rollout=None,
        values=LABELS,
    )
    assert outcome.problems == [
        f"{user}: value no value; expected the off value 'Buy now'"
        for user in USERS[:3]
    ]
    unchecked = traffic.evaluate(
        FakeFlags(10, state="off"),
        flag_key="f",
        users=USERS[:3],
        reason="inactive",
        rollout=None,
    )
    assert unchecked.problems == []


@pytest.mark.parametrize(
    "answered, expected, same",
    [
        ("Buy now", "Buy now", True),
        (10, 10.0, True),
        ({"a": [1, {"b": "c"}]}, {"a": [1, {"b": "c"}]}, True),
        (True, 1, False),
        (1, True, False),
        ([1, 2], [2, 1], False),
        ({"a": 1}, {"a": 1, "b": 2}, False),
        ("10", 10, False),
        (None, "Buy now", False),
    ],
)
def test_a_value_is_compared_as_json(answered, expected, same):
    assert traffic.same_value(answered, expected) is same


def _evaluations_step(**changes: Any) -> Dict[str, Any]:
    step = {
        "id": "users",
        "doc": "results",
        "do": {
            "evaluations": {
                "flag": "f",
                "key": "sdk-key",
                "users": "flag-user",
                "count": 200,
                "context": {"plan": "free"},
                "reason": "rollout",
                "rollout": 10,
            }
        },
        "fail": "a user gets the flag other than the rollout decides",
    }
    step["do"]["evaluations"].update(changes)
    return step


def test_a_journey_with_evaluations_loads(tmp_path):
    data = _with(lambda d: d["steps"].insert(2, _evaluations_step()))
    journey = _load(tmp_path, data)
    step = journey.steps[2]
    assert step.do.kind == "evaluations"
    assert checks.describe_action(step) == (
        'evaluate f for 200 users with context {"plan": "free"} with the API key sdk-key'
    )
    assert checks.describe_expect(step).startswith(
        "exactly the users the documented rollout hash puts below 10%"
    )


def test_a_value_flags_evaluations_load_and_say_which_value_each_user_gets(tmp_path):
    data = _with(
        lambda d: d["steps"].insert(
            2, _evaluations_step(on_value="Complete purchase", off_value="Buy now")
        )
    )
    step = _load(tmp_path, data).steps[2]
    assert (step.do.evaluations.on_value, step.do.evaluations.off_value) == LABELS
    assert checks.describe_expect(step) == (
        "exactly the users the documented rollout hash puts below 10% get the flag,"
        ' each with reason rollout; each user who gets it gets the on value "Complete'
        ' purchase", and each other user the off value "Buy now"'
    )


@pytest.mark.parametrize(
    "change, expected",
    [
        pytest.param(
            lambda d: d["steps"].insert(2, _evaluations_step(rollout=None)),
            "rollout is the percentage for reason: rollout, and only for it",
            id="rollout-reason-without-a-percentage",
        ),
        pytest.param(
            lambda d: d["steps"].insert(
                2, _evaluations_step(reason="inactive", rollout=10)
            ),
            "rollout is the percentage for reason: rollout, and only for it",
            id="a-percentage-for-another-reason",
        ),
        pytest.param(
            lambda d: d["steps"].insert(0, _evaluations_step()),
            "step 1 (users): 'sdk-key' is used before any step saves it",
            id="evaluations-before-the-key-is-saved",
        ),
        pytest.param(
            lambda d: d["steps"].insert(2, _evaluations_step(on_value="Buy now")),
            "on_value and off_value are a flag's two values: give both, or neither",
            id="an-on-value-without-an-off-value",
        ),
        pytest.param(
            lambda d: d["steps"].insert(
                2, _evaluations_step(on_value=True, off_value=False)
            ),
            "on_value",
            id="a-boolean-value-a-boolean-flag-answers-in-enabled",
        ),
        pytest.param(
            lambda d: (
                d["steps"].insert(2, _evaluations_step()),
                d.update(stack="docs-local"),
            ),
            "step 3 (users): an evaluations step needs the compose stack's API",
            id="evaluations-on-a-stack-with-no-api",
        ),
        pytest.param(
            lambda d: d["steps"].insert(
                2, {**_evaluations_step(), "expect": {"status": 200}}
            ),
            "step 3 (users): evaluations checks what the action says",
            id="evaluations-with-an-expect",
        ),
        pytest.param(
            lambda d: _step(d, "results-api")["expect"].update(
                json={"experiment_id": "{{not-saved}}"}
            ),
            "step 5 (results-api): 'not-saved' is used before any step saves it",
            id="expected-json-names-an-unsaved-value",
        ),
    ],
)
def test_a_planted_evaluations_defect_is_refused(tmp_path, change, expected):
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, _with(change))
    assert any(expected in p for p in refused.value.problems), refused.value.problems


def test_a_stack_brought_up_again_is_signed_in_to_again():
    """A token from the core stack is refused by the full one (new accounts, 401).

    Measured: before this, the first full-profile journey's first api step
    answered 401 on every run (docs-journeys.yml, tamper/d3b2-green). The
    runner imports Playwright, which the unit job does not install, so its
    source is read: ``run`` forgets the tokens when the stack is not the one
    they were signed in on.
    """
    import ast

    tree = ast.parse((RUNNER_ROOT / "docs_runner" / "execute.py").read_text())
    run = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    source = ast.unparse(run)
    assert "if running is not self.stack:" in source
    assert "self.tokens.clear()" in source
    assert source.index("self.tokens.clear()") < source.index("self._step(")


def test_a_recorded_journey_that_saves_a_secret_from_any_route_is_refused(tmp_path):
    """A secret saved from a route that is not a credential's still counts:
    the journey has a secret, so it is not recorded (model.reveals_credential)."""
    data = _with(
        lambda d: (
            d.update(video=True),
            _step(d, "key").update(
                do={"api": {"method": "GET", "path": "/api/v1/things", "as": "admin"}},
                expect={"status": 200},
                save={"sdk-key": {"path": "data.value", "secret": True}},
            ),
        )
    )
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, data)
    assert any("an api step reveals one" in p for p in refused.value.problems), (
        refused.value.problems
    )


# ---------------------------------------------------------------------------
# The order a session walks the journeys in
# ---------------------------------------------------------------------------
def _session_order() -> List[Path]:
    """The journey files in the order the runner's session walks them."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "docs_journeys_conftest", RUNNER_ROOT / "conftest.py"
    )
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)
    return sorted((RUNNER_ROOT / "journeys").glob("*.yaml"), key=conftest._order)


def _makes_an_admin_key(path: Path) -> bool:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for step in data.get("steps", []):
        api = (step.get("do") or {}).get("api") or {}
        if (
            api.get("method") == "POST"
            and api.get("path", "").rstrip("/") == "/api/v1/api-keys"
            and api.get("as") == "admin"
        ):
            return True
    return False


def _keys_made_before(order: List[Path], journey: str) -> List[str]:
    """The journeys that make an administrator's API key before *journey* runs."""
    names = [path.stem for path in order]
    before = order[: names.index(journey)]
    return [path.stem for path in before if _makes_an_admin_key(path)]


def test_no_journey_leaves_an_admin_key_before_the_api_key_journey():
    """api-key-first-assignment ends on the keys page reading "No API keys
    found", so no journey before it in the session may leave the demo
    administrator a key; experiment-results and the flag journeys make one."""
    order = _session_order()
    assert _keys_made_before(order, "api-key-first-assignment") == []
    makers = [path for path in order if _makes_an_admin_key(path)]
    assert makers, "no journey makes an administrator's key: the check reads nothing"
    planted = [makers[0]] + [path for path in order if path != makers[0]]
    assert _keys_made_before(planted, "api-key-first-assignment") == [makers[0].stem]


# ---------------------------------------------------------------------------
# The flag journey's evaluations tell its rule apart from a looser one
# ---------------------------------------------------------------------------
#: The rule builder's operator labels, as the journey picks them, and the
#: operators the dashboard stores.
BUILDER_OPERATORS = {"equals": "equals", "is one of": "in"}
CONDITION_FIELD = re.compile(
    r"^Group 1, condition (?P<n>[0-9]+) (?P<part>attribute|operator|value)$"
)


def _built_rule(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The rule the journey's fills and selects build in the dashboard."""
    conditions: Dict[int, Dict[str, str]] = {}
    for step in steps:
        do = step.get("do") or {}
        for kind, field in (("fill", "value"), ("select", "option")):
            action = do.get(kind)
            match = CONDITION_FIELD.match(action["label"]) if action else None
            if match:
                condition = conditions.setdefault(
                    int(match["n"]), {"operator": "equals"}
                )
                condition[match["part"]] = action[field]
    return {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {
                        "attribute": c["attribute"],
                        "operator": BUILDER_OPERATORS[c["operator"]],
                        "value": c["value"],
                    }
                    for _, c in sorted(conditions.items())
                ],
            }
        ],
    }


def _contradicted(rule: Dict[str, Any], steps: List[Dict[str, Any]]) -> List[str]:
    """The evaluations steps whose expected reason the rule does not give."""
    from backend.app.core.targeting_adapter import (
        expand_context,
        match_targeting_rule,
        normalise_targeting_rules,
    )

    rules = normalise_targeting_rules(rule, owner="flag:docs-journey")
    found = []
    for step in steps:
        plan = (step.get("do") or {}).get("evaluations")
        if not plan or plan["reason"] == "inactive":
            continue
        matched = match_targeting_rule(rules, expand_context(plan["context"]))
        if (matched is not None) != (plan["reason"] == "targeting_rule"):
            found.append(step["id"])
    return found


def test_the_flag_journeys_value_steps_check_the_values_its_form_typed():
    """C8 (#1497): the string flag's evaluations expect exactly the two values
    the journey types into On value and Off value, while it is off, on at its
    rollout and off again; so a form that swapped them fails the journey."""
    data = yaml.safe_load(
        (RUNNER_ROOT / "journeys" / "feature-flag-create.yaml").read_text(
            encoding="utf-8"
        )
    )
    typed = {
        step["do"]["fill"]["label"]: step["do"]["fill"]["value"]
        for step in data["steps"]
        if (step.get("do") or {}).get("fill", {}).get("label")
        in ("On value", "Off value")
    }
    assert typed == {"On value": "Complete purchase", "Off value": "Buy now"}
    plans = [
        step["do"]["evaluations"]
        for step in data["steps"]
        if (step.get("do") or {}).get("evaluations", {}).get("flag")
        == "docs_journey_label"
    ]
    assert [(p["reason"], p.get("rollout")) for p in plans] == [
        ("inactive", None),
        ("rollout", 10),
        ("inactive", None),
    ]
    for plan in plans:
        assert (plan["on_value"], plan["off_value"]) == (
            typed["On value"],
            typed["Off value"],
        )


def test_the_flag_journeys_evaluations_hold_both_conditions_of_its_rule():
    """Every evaluations step of feature-flag-create gives the reason the rule
    it builds decides; a rule without the country condition would let the
    DE enterprise users in by the rule, and the plan-only step tells it apart."""
    data = yaml.safe_load(
        (RUNNER_ROOT / "journeys" / "feature-flag-create.yaml").read_text(
            encoding="utf-8"
        )
    )
    rule = _built_rule(data["steps"])
    attributes = [c["attribute"] for c in rule["groups"][0]["conditions"]]
    assert attributes == ["user.country", "user.plan"]
    assert _contradicted(rule, data["steps"]) == []
    looser = copy.deepcopy(rule)
    looser["groups"][0]["conditions"] = [
        c for c in looser["groups"][0]["conditions"] if c["attribute"] != "user.country"
    ]
    assert _contradicted(looser, data["steps"]) == ["plan-only-users"]
