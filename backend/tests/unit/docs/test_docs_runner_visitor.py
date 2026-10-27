"""A journey's ShopLab visitor: chosen offline by the documented hashes, seeded
into the storefront before the journey's first page.

ShopLab keeps its visitor id in localStorage and makes up a random UUIDv4 when
there is none, so a journey that walks it cannot know the visitor's arm, and a
new visitor may land in the demo's global holdout (2 % of new users), where the
API answers control in every experiment and counts the visitor in none. A
journey on demo-local may name the visitor instead (``visitor``): the runner puts
it into the storefront's localStorage before the first page.

Held here:

* the visitor the journeys use, chosen by ``values.choose_visitor``: a UUIDv4,
  its bucket outside the demo holdout, and its arm in each ShopLab experiment
  the documented assignment hash places users in (the bandit routes by its
  weights instead), pinned as numbers and names;
* what those arms rest on: the API walks an experiment's variants in the order
  they were created (``created_at``, stamped per row by Python, then ``id``),
  and its own assignment, walking the seed's variants in that order, gives them;
* what the chooser and the seed are made of, read from their sources rather
  than typed here: the holdout's salt and share (the StreamPulse seed), the
  holdout bucket (the API's own function, and the README's ``sp-holdout-15``),
  the storefront's key, its UUIDv4 rule and its address;
* the loader's refusals: a visitor that is not a UUIDv4, one inside the
  holdout, one on a stack with no storefront;
* the runner: the browser starts with the visitor in the storefront's
  localStorage, on the address the stack serves it on, and a journey whose
  stack serves no storefront fails at its first step that runs.
"""

from __future__ import annotations

import copy
import datetime
import re
import sys
import types
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml
from pydantic import ValidationError

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import loader, log, traffic, values
from docs_runner.model import Journey

from backend.scripts import seed_shoplab, seed_streampulse

VISITOR_TS = REPO_ROOT / "demo" / "shoplab" / "src" / "lib" / "visitor.ts"
SHOPLAB_README = REPO_ROOT / "demo" / "shoplab" / "README.md"
STREAMPULSE_README = REPO_ROOT / "demo" / "streampulse" / "README.md"

#: The visitor ``values.choose_visitor()`` gives: the first candidate outside the
#: demo holdout. A journey that seeds a ShopLab visitor names this one.
VISITOR = "d2e601e3-374a-4e49-89ab-e1e613123a87"
#: Its bucket under the demo holdout's salt: 57, not below 2.
VISITOR_BUCKET = 57
#: Its arm in each ShopLab experiment the assignment hash places users in.
VISITOR_ARMS = {
    "shoplab_hero_banner": "video_hero",
    "shoplab_pdp_buy_button": "green_add",
    "shoplab_checkout_flow": "standard",
}
#: The third candidate, which the holdout keeps out (bucket 1).
HELD_OUT = "02107c82-e278-433b-996a-f5a65c3d4b42"


def _allocations(key: str) -> List[tuple]:
    """The seed's variants of one experiment, in the order it creates them."""
    spec = next(e for e in seed_shoplab.EXPERIMENTS if e["key"] == key)
    return [(v["name"], v["traffic_allocation"]) for v in spec["variants"]]


def _hashed_experiments() -> List[str]:
    """The ShopLab experiments whose arm the assignment hash decides."""
    return [
        spec["key"]
        for spec in seed_shoplab.EXPERIMENTS
        if spec["experiment_type"].value != "bandit"
    ]


# ---------------------------------------------------------------------------
# The chosen visitor
# ---------------------------------------------------------------------------
def test_the_chooser_gives_the_visitor_the_journeys_name():
    assert values.choose_visitor() == VISITOR


def test_the_visitor_is_a_uuid_v4_the_storefront_keeps():
    parsed = uuid.UUID(VISITOR)
    assert parsed.version == 4
    assert parsed.variant == uuid.RFC_4122
    assert str(parsed) == VISITOR
    assert values.is_uuid_v4(VISITOR)
    assert _storefront_uuid_rule().fullmatch(VISITOR)


def test_the_visitor_is_outside_the_demo_holdout():
    bucket = values.holdout_bucket(VISITOR, seed_streampulse.DEMO_HOLDOUT_SALT)
    assert bucket == VISITOR_BUCKET
    assert bucket >= seed_streampulse.HOLDOUT_PERCENTAGE
    assert not values.in_demo_holdout(VISITOR)


def test_the_visitors_arm_in_each_hashed_experiment():
    assert sorted(_hashed_experiments()) == sorted(VISITOR_ARMS)
    arms = {
        key: traffic.variant_for(VISITOR, key, _allocations(key))
        for key in _hashed_experiments()
    }
    assert arms == VISITOR_ARMS


def test_the_api_orders_an_experiments_variants_as_they_were_created():
    """The arms above are the visitor's only while the API walks an experiment's
    variants in the order the seed creates them (the next test: reversed, each
    of the three changes).

    The API orders them by ``created_at``, then ``id``, both ascending. The
    seed adds an experiment's variants in its own order and flushes them
    together; ``created_at`` is stamped by Python for each row as it is
    inserted, so it follows that order. A database default such as ``now()``
    gives every row of one transaction the same time, which would leave the
    order, and so the visitor's arms, to the variants' random ids.
    """
    from sqlalchemy import Column
    from sqlalchemy import inspect as sa_inspect

    from backend.app.models import register_core_models
    from backend.app.models.experiment import Experiment, Variant

    register_core_models()
    variants = Variant.__table__
    order = sa_inspect(Experiment).relationships["variants"].order_by or ()
    assert [
        column.name
        for column in order
        if isinstance(column, Column) and column.table is variants
    ] == ["created_at", "id"], order
    assert len(order) == 2, order

    created_at = variants.c.created_at
    assert created_at.server_default is None, created_at.server_default
    assert created_at.default is not None and created_at.default.is_callable, (
        created_at.default
    )


def test_the_apis_own_assignment_gives_the_arms_in_creation_order():
    from backend.app.services.assignment_service import AssignmentService

    api = AssignmentService.__new__(AssignmentService)  # the hash reads no session
    for key in _hashed_experiments():
        created = [
            types.SimpleNamespace(id=name, traffic_allocation=share)
            for name, share in _allocations(key)
        ]
        in_order = types.SimpleNamespace(id=key, key=key, variants=created)
        reversed_ = types.SimpleNamespace(id=key, key=key, variants=created[::-1])
        assert api._hash_user_to_variant(VISITOR, in_order) == VISITOR_ARMS[key]
        assert api._hash_user_to_variant(VISITOR, reversed_) != VISITOR_ARMS[key]


def test_the_chooser_finds_a_visitor_in_the_arm_asked_for():
    checkout = "shoplab_checkout_flow"
    allocations = _allocations(checkout)
    for arm, _ in allocations:
        chosen = values.choose_visitor({checkout: (allocations, arm)})
        assert traffic.variant_for(chosen, checkout, allocations) == arm
        assert not values.in_demo_holdout(chosen)
        assert values.is_uuid_v4(chosen)
        # The same question, the same answer: the choice is offline and fixed.
        assert values.choose_visitor({checkout: (allocations, arm)}) == chosen


def test_the_chooser_passes_over_a_held_out_candidate():
    """The third candidate is held out; asked for its own arms, the chooser
    must not give it, although no earlier candidate has them."""
    assert values.visitor_candidate(3) == HELD_OUT
    assert values.holdout_bucket(HELD_OUT, values.DEMO_HOLDOUT_SALT) == 1
    arms = {
        key: (_allocations(key), traffic.variant_for(HELD_OUT, key, _allocations(key)))
        for key in _hashed_experiments()
    }
    for number in (1, 2):
        earlier = values.visitor_candidate(number)
        assert not all(
            traffic.variant_for(earlier, key, allocations) == arm
            for key, (allocations, arm) in arms.items()
        ), earlier
    chosen = values.choose_visitor(arms)
    assert chosen != HELD_OUT
    assert not values.in_demo_holdout(chosen)


def test_the_chooser_says_when_no_candidate_will_do():
    impossible = {"k": ([("a", 100)], "b")}
    with pytest.raises(ValueError, match="none of 1000 candidates"):
        values.choose_visitor(impossible)


# ---------------------------------------------------------------------------
# What the chooser and the seed are made of, read from their sources
# ---------------------------------------------------------------------------
def test_the_holdout_salt_and_share_are_the_demo_seeds():
    assert values.DEMO_HOLDOUT_SALT == seed_streampulse.DEMO_HOLDOUT_SALT
    assert values.DEMO_HOLDOUT_PERCENTAGE == seed_streampulse.HOLDOUT_PERCENTAGE


def test_the_holdout_bucket_is_the_apis():
    from backend.app.services.global_holdout_service import holdout_bucket

    ids = [f"user-{n}" for n in range(300)] + [
        values.visitor_candidate(n) for n in range(1, 300)
    ]
    for salt in (values.DEMO_HOLDOUT_SALT, "holdout:other"):
        for user in ids:
            assert values.holdout_bucket(user, salt) == holdout_bucket(user, salt), user


def test_the_holdout_bucket_gives_the_readmes_example():
    readme = STREAMPULSE_README.read_text(encoding="utf-8")
    match = re.search(r"`(sp-holdout-\d+)` \(bucket (\d+) under the demo", readme)
    assert match, "the StreamPulse README no longer gives a held-out device id"
    user, bucket = match.group(1), int(match.group(2))
    assert values.holdout_bucket(user, values.DEMO_HOLDOUT_SALT) == bucket
    assert values.in_demo_holdout(user)


def test_the_visitor_key_is_the_storefronts():
    source = VISITOR_TS.read_text(encoding="utf-8")
    match = re.search(r"export const VISITOR_ID_KEY = '([^']+)';", source)
    assert match, "visitor.ts no longer names its key VISITOR_ID_KEY"
    assert values.VISITOR_KEY == match.group(1)


def _storefront_uuid_rule() -> "re.Pattern[str]":
    source = VISITOR_TS.read_text(encoding="utf-8")
    match = re.search(r"^const UUID_V4 = /(.+)/i;$", source, re.M)
    assert match, "visitor.ts no longer checks ids with UUID_V4"
    return re.compile(match.group(1), re.I)


def test_the_uuid_rule_is_the_storefronts_in_lower_case():
    assert values.UUID_V4.pattern == _storefront_uuid_rule().pattern
    assert values.is_uuid_v4(VISITOR)
    assert not values.is_uuid_v4(VISITOR.upper())
    assert _storefront_uuid_rule().fullmatch(VISITOR.upper())


def test_the_storefront_address_is_the_readmes():
    assert values.STOREFRONT in SHOPLAB_README.read_text(encoding="utf-8")


def test_a_candidate_is_a_uuid_v4_made_from_its_number():
    first = values.visitor_candidate(1)
    assert first == values.visitor_candidate(1)
    assert first != values.visitor_candidate(2)
    for number in range(1, 200):
        candidate = values.visitor_candidate(number)
        assert values.is_uuid_v4(candidate), candidate
        assert _storefront_uuid_rule().fullmatch(candidate)


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------
TODAY = datetime.date(2026, 10, 27)
GUIDE = """# ShopLab

## Running it

The storefront is at http://localhost:3200.
"""
GOOD: Dict[str, Any] = {
    "guide": "shoplab.md",
    "stack": "demo-local",
    "profile": "full",
    "video": False,
    "written": TODAY,
    "visitor": VISITOR,
    "steps": [
        {
            "id": "storefront",
            "doc": "running-it",
            "do": {"open": "http://localhost:3200"},
            "expect": {"status": 200, "aria": '- heading "ShopLab" [level=1]'},
            "fail": "the storefront does not answer",
        }
    ],
}


def _load(tmp_path: Path, data: Dict[str, Any]) -> Journey:
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "shoplab.md").write_text(GUIDE, encoding="utf-8")
    path = tmp_path / "journeys" / "shoplab.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    context = loader.Context(
        docs_root=docs,
        inventory={
            "pages": {
                "shoplab.md": {"class": "journey", "journey": "shoplab", "reason": "x"}
            }
        },
        doc_examples={},
        oracles={},
        today=TODAY,
    )
    return loader.load(path, context)


def _with(**changes: Any) -> Dict[str, Any]:
    data = copy.deepcopy(GOOD)
    data.update(changes)
    return data


def test_a_journey_with_the_chosen_visitor_loads(tmp_path):
    assert _load(tmp_path, GOOD).visitor == VISITOR
    without = copy.deepcopy(GOOD)
    del without["visitor"]
    assert _load(tmp_path, without).visitor is None


@pytest.mark.parametrize(
    "visitor",
    [
        pytest.param(VISITOR.upper(), id="upper-case"),
        pytest.param("6ba7b810-9dad-11d1-80b4-00c04fd430c8", id="uuid-v1"),
        pytest.param(VISITOR.replace("-4", "-5", 1), id="version-5"),
        pytest.param(VISITOR[:-1], id="short"),
        pytest.param("docs-journey-visitor", id="not-a-uuid"),
    ],
)
def test_a_visitor_that_is_not_a_uuid_v4_is_refused(tmp_path, visitor):
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, _with(visitor=visitor))
    assert any(
        p.startswith("visitor: not a UUIDv4 in lower case")
        for p in refused.value.problems
    ), refused.value.problems


def test_a_visitor_inside_the_holdout_is_refused(tmp_path):
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, _with(visitor=HELD_OUT))
    assert refused.value.problems == [
        f"visitor {HELD_OUT} is in the demo's global holdout (bucket 1 under"
        " holdout:streampulse-demo-22, below 2): the API answers it control in every"
        " experiment and counts it in none; choose one with values.choose_visitor"
    ]


def test_a_visitor_on_a_stack_with_no_storefront_is_refused(tmp_path):
    data = _with(stack="compose-dev", profile="core")
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, data)
    assert refused.value.problems == [
        "visitor is ShopLab's, seeded on the storefront the stack demo-local serves"
        " at http://localhost:3200; compose-dev serves none"
    ]
    # Positive control: the same journey without the visitor loads there.
    del data["visitor"]
    assert _load(tmp_path, data).visitor is None


def test_a_refused_journey_still_checks_its_visitor(tmp_path):
    """The model refuses a step; the visitor's own refusal is still reported."""
    data = _with(visitor=HELD_OUT)
    data["steps"][0]["fail"] = "two\nlines"
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, data)
    assert any("global holdout" in p for p in refused.value.problems)
    assert any("fail" in p for p in refused.value.problems)


# ---------------------------------------------------------------------------
# The runner (Playwright stood in for, as the unit job lacks it)
# ---------------------------------------------------------------------------
@pytest.fixture
def execute_module(monkeypatch):
    import importlib

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.Error = type("Error", (Exception,), {})
    sync_api.Browser = sync_api.Page = sync_api.Playwright = object
    sync_api.expect = types.SimpleNamespace(set_options=lambda **kwargs: None)
    package = types.ModuleType("playwright")
    package.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.delitem(sys.modules, "docs_runner.execute", raising=False)
    module = importlib.import_module("docs_runner.execute")
    yield module
    sys.modules.pop("docs_runner.execute", None)


def _demo_local(published: Dict[int, str]):
    from docs_runner.stacks import Running

    return Running(
        name="demo-local",
        base_url="http://localhost:3100/",
        published=published,
    )


SERVED = {3100: "http://localhost:3100", 3200: "http://localhost:3200"}


def _journey(visitor: Any = VISITOR, not_run: tuple = (0, 1)) -> Journey:
    """GOOD and an api step; the steps numbered in *not_run* (from 0) are not run."""
    data = copy.deepcopy(GOOD)
    if visitor is None:
        del data["visitor"]
    else:
        data["visitor"] = visitor
    data["steps"].append(
        {
            "id": "list",
            "doc": "running-it",
            "do": {"api": {"method": "GET", "path": "/x", "as": "anonymous"}},
            "expect": {"status": 200},
            "fail": "x",
        }
    )
    for index in not_run:
        data["steps"][index]["not_run"] = "needs-aws"
    return Journey.model_validate(data)


def _runner(execute_module, tmp_path, browser=None):
    return execute_module.JourneyRunner(
        None,
        browser,
        log.Log(tmp_path / "results.jsonl"),
        execute_module.Settings(run_dir=tmp_path, run_id="r", sha="s"),
    )


def test_the_seed_is_the_visitor_on_the_storefronts_origin(execute_module):
    seeded, problem = execute_module.JourneyRunner._seed(
        _journey(), _demo_local(SERVED)
    )
    assert problem == ""
    assert seeded == {
        "cookies": [],
        "origins": [
            {
                "origin": "http://localhost:3200",
                "localStorage": [{"name": "shoplab.visitor_id", "value": VISITOR}],
            }
        ],
    }


def test_the_seed_goes_where_the_stack_serves_the_storefront(execute_module):
    elsewhere = {3200: "http://127.0.0.1:43200/"}
    seeded, problem = execute_module.JourneyRunner._seed(
        _journey(), _demo_local(elsewhere)
    )
    assert problem == ""
    assert seeded["origins"][0]["origin"] == "http://127.0.0.1:43200"


def test_a_journey_without_a_visitor_seeds_nothing(execute_module):
    assert execute_module.JourneyRunner._seed(
        _journey(visitor=None), _demo_local(SERVED)
    ) == (None, "")


def test_a_stack_that_serves_no_storefront_cannot_be_seeded(execute_module):
    seeded, problem = execute_module.JourneyRunner._seed(
        _journey(), _demo_local({3100: "http://localhost:3100"})
    )
    assert seeded is None
    assert problem == (
        "the visitor cannot be seeded: the demo-local stack serves nothing on port"
        " 3200, where the storefront is (http://localhost:3200)"
    )


class _Page:
    video = None

    def on(self, event, handler):
        return None


class _Context:
    def __init__(self):
        self.closed = False

    def set_default_timeout(self, ms):
        return None

    def new_page(self):
        return _Page()

    def close(self):
        self.closed = True


class _Browser:
    def __init__(self):
        self.options: List[Dict[str, Any]] = []

    def new_context(self, **options):
        self.options.append(options)
        return _Context()


def _guide():
    return types.SimpleNamespace(anchors={"running-it": "Running it"})


def test_the_browser_starts_with_the_visitor_in_its_storage(execute_module, tmp_path):
    browser = _Browser()
    runner = _runner(execute_module, tmp_path, browser)
    runner.run(_journey(), "shoplab", _demo_local(SERVED), _guide())
    runner.run(_journey(visitor=None), "other", _demo_local(SERVED), _guide())
    first, second = browser.options
    assert first["storage_state"]["origins"][0]["localStorage"] == [
        {"name": "shoplab.visitor_id", "value": VISITOR}
    ]
    assert "storage_state" not in second


def test_an_unseeded_journey_fails_at_its_first_step_that_runs(
    execute_module, tmp_path
):
    from docs_runner.log import FAIL, NOT_RUN

    journey = _journey(not_run=(0,))  # the first step is not run; the second is
    browser = _Browser()
    runner = _runner(execute_module, tmp_path, browser)
    records = runner.run(
        journey, "shoplab", _demo_local({3100: "http://localhost:3100"}), _guide()
    )
    assert [r.result for r in records] == [NOT_RUN, FAIL]
    assert records[1].observed.startswith("the visitor cannot be seeded")
    assert "storage_state" not in browser.options[0]
    assert runner.unseeded == ""
