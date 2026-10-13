"""The demo seed makes the numbers the demo guide's scenes quote (#1142).

``backend/scripts/seed_demo_data.py`` seeds the three experiments the guide
walks through: Checkout Button Color (Scene 2), Homepage Hero Copy Test
(Scene 3) and Recommendation Algorithm MAB (Scene 4). Here the seed's own
functions run against a session that records what they add and writes
nothing, so every count below is what a fresh ``./demo/setup-local.sh`` puts
in the database, drawn from the seed's fixed random generators.

The live event simulator (``backend/scripts/simulate_live_events.py``) is read
from its source, and the rate ``demo/setup-local.sh`` starts it with from the
script, without running either.

No database, no network.
"""

from __future__ import annotations

import ast
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from scipy import stats

from backend.app.models import register_core_models
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant
from backend.app.services.bandit_service import BanditService
from backend.scripts import seed_demo_data as seed

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
SETUP_LOCAL = REPO_ROOT / "demo" / "setup-local.sh"
LIVE_SIMULATOR = REPO_ROOT / "backend" / "scripts" / "simulate_live_events.py"


class _NoRows:
    def filter(self, *args: Any, **kwargs: Any) -> "_NoRows":
        return self

    def first(self) -> None:
        return None


class _RecordingSession:
    """Takes what a seed function adds, gives an id where a flush would."""

    def __init__(self) -> None:
        self.added: List[Any] = []

    def query(self, *args: Any) -> _NoRows:
        return _NoRows()

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def add_all(self, objs: List[Any]) -> None:
        self.added.extend(objs)

    def flush(self) -> None:
        for obj in self.added:
            if isinstance(obj, (Experiment, Variant)) and obj.id is None:
                obj.id = uuid.uuid4()

    def commit(self) -> None:
        self.flush()


class _Seeded:
    def __init__(self, experiment: Experiment, added: List[Any]) -> None:
        self.experiment = experiment
        self.variants: Dict[str, Variant] = {
            o.name: o for o in added if isinstance(o, Variant)
        }
        self.assignments = [o for o in added if isinstance(o, Assignment)]
        self.events = [o for o in added if isinstance(o, Event)]

    def users(self, variant: str) -> int:
        wanted = self.variants[variant].id
        return sum(1 for a in self.assignments if a.variant_id == wanted)

    def conversions(self, variant: str, event_name: str) -> int:
        wanted = self.variants[variant].id
        return sum(
            1
            for e in self.events
            if e.event_name == event_name
            and e.experiment_id == self.experiment.id
            and e.variant_id == wanted
        )


def _run(function: Any) -> _Seeded:
    session = _RecordingSession()
    admin = SimpleNamespace(id=uuid.uuid4())
    experiment = function(session, admin)
    return _Seeded(experiment, session.added)


@pytest.fixture(scope="module")
def scenes() -> Dict[str, _Seeded]:
    register_core_models()
    return {
        "checkout": _run(seed.seed_checkout_button_experiment),
        "hero": _run(seed.seed_homepage_hero_experiment),
        "bandit": _run(seed.seed_recommendation_mab_experiment),
    }


def _days_ago(moment: datetime) -> float:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment) / timedelta(days=1)


def test_the_seed_writes_more_than_100000_events(scenes):
    events = sum(len(s.events) for s in scenes.values())
    assert events > 100_000, events


def test_checkout_button_color_started_14_days_ago_with_15000_users_in_each_variant(
    scenes,
):
    checkout = scenes["checkout"]
    assert checkout.experiment.name == "Checkout Button Color"
    assert round(_days_ago(checkout.experiment.start_date), 3) == 14
    assert checkout.users("blue_button") == 15_000
    assert checkout.users("green_button") == 15_000
    assert len(checkout.events) > 30_000


def test_checkout_button_color_converts_about_9_against_8_percent_and_is_significant(
    scenes,
):
    checkout = scenes["checkout"]
    blue = checkout.conversions("blue_button", "checkout_completed")
    green = checkout.conversions("green_button", "checkout_completed")
    blue_rate = blue / checkout.users("blue_button")
    green_rate = green / checkout.users("green_button")
    assert round(blue_rate * 100) == 8, blue_rate
    assert round(green_rate * 100) == 9, green_rate
    _, p_value = stats.fisher_exact([[green, 15_000 - green], [blue, 15_000 - blue]])
    assert p_value < 0.05, p_value


def _setup_local_rate(program: str) -> int:
    """The ``--rate`` setup-local.sh starts *program* with."""
    text = SETUP_LOCAL.read_text(encoding="utf-8")
    start = text.index(program)
    command = re.match(r"(?:[^\n]*\\\n)*[^\n]*", text[start:]).group(0)
    rate = re.search(r"--rate (\d+)", command)
    assert rate, f"setup-local.sh starts {program} without --rate"
    return int(rate.group(1))


def _live_simulator_config() -> Dict[str, Any]:
    tree = ast.parse(LIVE_SIMULATOR.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "EXPERIMENT_CONFIG"
            for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("simulate_live_events.py has no EXPERIMENT_CONFIG")


def test_the_live_simulator_sends_checkout_button_color_about_3_events_a_second():
    config = _live_simulator_config()
    checkout = config["Checkout Button Color"]
    rate = _setup_local_rate("backend/scripts/simulate_live_events.py")
    share = checkout["weight"] / sum(c["weight"] for c in config.values())
    assert rate * share == pytest.approx(3.0)
    assert set(checkout["variant_rates"]) == {"blue_button", "green_button"}
    assert checkout["event_name"] == "checkout_completed"


def test_homepage_hero_copy_ran_35_days_with_25000_users_in_each_variant(scenes):
    hero = scenes["hero"]
    assert hero.experiment.status == ExperimentStatus.COMPLETED
    days = (hero.experiment.end_date - hero.experiment.start_date) / timedelta(days=1)
    assert round(days, 6) == 35
    assert hero.users("control") == 25_000
    assert hero.users("new_copy") == 25_000
    assert len(hero.events) > 50_000


def test_the_bandit_starts_at_33_33_34_with_thompson_sampling(scenes):
    bandit = scenes["bandit"]
    assert bandit.experiment.status == ExperimentStatus.ACTIVE
    assert bandit.experiment.optimization_type == "thompson_sampling"
    shares = {name: v.traffic_allocation for name, v in bandit.variants.items()}
    assert shares == {"algo_v1": 33, "algo_v2": 33, "algo_v3": 34}


def _bandit_rates(bandit: _Seeded) -> Dict[str, float]:
    return {
        name: bandit.conversions(name, "item_clicked") / bandit.users(name)
        for name in bandit.variants
    }


def test_algo_v2_clicks_about_9_percent_against_5_and_6(scenes):
    rates = _bandit_rates(scenes["bandit"])
    assert round(rates["algo_v2"] * 100) == 9, rates
    assert round(rates["algo_v1"] * 100) == 5, rates
    assert round(rates["algo_v3"] * 100) == 6, rates


def test_thompson_sampling_gives_algo_v2_the_largest_share(scenes):
    bandit = scenes["bandit"]
    data = {}
    for name in bandit.variants:
        pulls = bandit.users(name)
        successes = bandit.conversions(name, "item_clicked")
        data[name] = {
            "successes": successes,
            "failures": pulls - successes,
            "pulls": pulls,
            "total_reward": float(successes),
        }
    weights = BanditService.compute_weights("thompson_sampling", data, seed=7)
    assert max(weights, key=weights.get) == "algo_v2", weights
