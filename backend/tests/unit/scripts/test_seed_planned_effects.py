"""Each demo seed's primary metric plans a sample its own history can reach (#1257).

The results page's Planned sample uses the primary metric's ``expected_effect``
as the relative lift to plan for. Until 0.34.3 nothing read it, and the seeds
stored absolute differences there: read as relative lifts they planned
129,560 users per variant for ShopLab's hero banner and 25,597,285 for
StreamPulse's upsell modal.

This pins, for every seeded experiment, the plan at the seed's control rate
with its stored effect, at the experiment's default analysis settings (95%
confidence, 80% power, Benjamini-Hochberg: alpha / (variants - 1)), and holds
each plan to at most ten times the users per variant the seed's history
creates. Nothing reaches a database.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.app.services.power_calculator_service import sample_size_two_proportions
from backend.scripts import seed_demo_data, seed_shoplab, seed_streampulse

pytestmark = [pytest.mark.unit]

SEED_DEMO_DATA = Path(seed_demo_data.__file__)

# (seed, experiment, control rate, relative effect, plan per variant or None,
#  history users per variant). None: the control never converts, so the
#  results page has no rate to plan from (no_control_conversions).
TABLE: List[Tuple[str, str, float, float, Optional[int], int]] = [
    ("seed_demo_data", "homepage_hero_copy", 0.075, 0.10, 20_246, 25_000),
    ("seed_demo_data", "checkout_button_color", 0.08, 0.10, 18_872, 15_000),
    ("seed_demo_data", "recommendation_algorithm_mab", 0.05, 0.40, 2_680, 6_666),
    ("seed_shoplab", "shoplab_hero_banner", 0.12, 0.25, 2_036, 2_000),
    ("seed_shoplab", "shoplab_plp_sort", 0.22, 0.14, 3_606, 1_333),
    ("seed_shoplab", "shoplab_pdp_buy_button", 0.08, 0.25, 4_286, 1_000),
    ("seed_shoplab", "shoplab_checkout_flow", 0.62, 0.08, 1_461, 2_000),
    ("seed_streampulse", "streampulse_push_frequency", 0.22, 0.23, 1_134, 1_500),
    ("seed_streampulse", "streampulse_wrapped", 0.0, 0.10, None, 1_500),
    ("seed_streampulse", "streampulse_profile_badges", 0.03, 0.50, 2_518, 1_500),
    ("seed_streampulse", "streampulse_onboarding_steps", 0.58, 0.09, 1_375, 1_500),
    ("seed_streampulse", "streampulse_upsell_modal", 0.024, 0.25, 11_455, 1_500),
]

# seed_demo_data builds each experiment in its own function.
DEMO_FUNCTIONS = {
    "homepage_hero_copy": "seed_homepage_hero_experiment",
    "checkout_button_color": "seed_checkout_button_experiment",
    "recommendation_algorithm_mab": "seed_recommendation_mab_experiment",
}
DEMO_VARIANTS = {
    "homepage_hero_copy": 2,
    "checkout_button_color": 2,
    "recommendation_algorithm_mab": 3,
}


def _demo_primary_effects() -> Dict[str, Any]:
    """expected_effect of the ``Metric(is_primary=True, ...)`` in each function."""
    tree = ast.parse(SEED_DEMO_DATA.read_text())
    found: Dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "Metric"
            ):
                continue
            kwargs = {k.arg: k.value for k in call.keywords}
            primary = kwargs.get("is_primary")
            if isinstance(primary, ast.Constant) and primary.value is True:
                effect = kwargs.get("expected_effect")
                found[node.name] = (
                    ast.literal_eval(effect) if effect is not None else None
                )
    return found


def _catalogue(module: Any) -> Dict[str, Dict[str, Any]]:
    return {spec["key"]: spec for spec in module.EXPERIMENTS}


def _stored(seed: str, key: str) -> Tuple[Optional[float], int, float]:
    """(primary expected_effect, variant count, control rate) as the seed has them."""
    if seed == "seed_demo_data":
        effect = _demo_primary_effects()[DEMO_FUNCTIONS[key]]
        return effect, DEMO_VARIANTS[key], float("nan")
    module = {"seed_shoplab": seed_shoplab, "seed_streampulse": seed_streampulse}[seed]
    spec = _catalogue(module)[key]
    (primary,) = [m for m in spec["metrics"] if m.get("is_primary")]
    control = next(v["name"] for v in spec["variants"] if v["is_control"])
    rate = module.RATES[key][primary["event_name"]][control]
    return primary.get("expected_effect"), len(spec["variants"]), rate


def _plan(rate: float, effect: float, variants: int) -> Optional[int]:
    if rate <= 0:
        return None
    alpha = 0.05 / max(1, variants - 1)
    return sample_size_two_proportions(
        rate, rate * (1 + effect), alpha, 0.80, two_tailed=True
    )


def test_the_table_covers_every_seeded_experiment():
    keys = {(seed, key) for seed, key, *_ in TABLE}
    expected = {("seed_demo_data", key) for key in DEMO_FUNCTIONS}
    expected |= {("seed_shoplab", k) for k in _catalogue(seed_shoplab)}
    expected |= {("seed_streampulse", k) for k in _catalogue(seed_streampulse)}
    assert keys == expected
    assert set(_demo_primary_effects()) == set(DEMO_FUNCTIONS.values())


@pytest.mark.regression
def test_every_seeded_primary_plans_the_pinned_sample():
    """On v0.34.2 the stored effects were absolute differences; this failed."""
    actual = []
    for seed, key, rate, _, _, _ in TABLE:
        effect, variants, seed_rate = _stored(seed, key)
        if not math.isnan(seed_rate):
            rate = seed_rate
        plan = (
            _plan(rate, effect, variants)
            if effect is not None and 0 < effect < 1
            else "unusable"
        )
        actual.append((seed, key, rate, effect, plan))
    assert actual == [(s, k, r, e, p) for s, k, r, e, p, _ in TABLE]


@pytest.mark.parametrize("row", TABLE, ids=[row[1] for row in TABLE])
def test_every_plan_is_within_ten_times_the_seeded_history(row):
    """Computed from what the seed stores, not from the table."""
    seed, key, rate, _, _, history_per_variant = row
    stored, variants, seed_rate = _stored(seed, key)
    assert stored is not None and math.isfinite(stored) and 0 < stored < 1
    if not math.isnan(seed_rate):
        rate = seed_rate
    plan = _plan(rate, stored, variants)
    if plan is not None:
        assert plan <= 10 * history_per_variant, (key, plan)


def test_the_history_sizes_are_the_seeds():
    assert seed_shoplab.DEFAULT_HISTORY_USERS == 4000
    assert seed_streampulse.DEFAULT_HISTORY_USERS == 3000
    for key, spec in _catalogue(seed_shoplab).items():
        row = next(r for r in TABLE if r[1] == key)
        assert row[5] == 4000 // len(spec["variants"])
    for key, spec in _catalogue(seed_streampulse).items():
        row = next(r for r in TABLE if r[1] == key)
        assert row[5] == 3000 // len(spec["variants"])
    assert seed_demo_data.CHECKOUT_CVR["blue_button"] == 0.08


def test_no_seeded_secondary_metric_stores_a_negative_effect():
    """A guardrail's -0.005 was the old absolute reading; none is stored now."""
    for module in (seed_shoplab, seed_streampulse):
        for spec in module.EXPERIMENTS:
            for metric in spec["metrics"]:
                effect = metric.get("expected_effect")
                assert effect is None or 0 < effect < 1, (spec["key"], metric)


class _Found:
    """A query whose every lookup finds a row already there."""

    def __init__(self, row: Any) -> None:
        self.row = row

    def filter(self, *args: Any, **kwargs: Any) -> "_Found":
        return self

    def first(self) -> Any:
        return self.row


class _SeededSession:
    def __init__(self, row: Any) -> None:
        self.row = row
        self.added: list = []

    def query(self, *args: Any) -> _Found:
        return _Found(self.row)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def add_all(self, objs: Any) -> None:
        self.added.extend(objs)

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        pass


def test_a_seed_run_on_a_seeded_database_leaves_its_metrics_as_they_are():
    """So demo data seeded before 0.34.3 keeps its old expected_effect values
    until it is re-seeded with --reset (the demo READMEs say so)."""
    import uuid
    from types import SimpleNamespace

    admin = SimpleNamespace(id=uuid.uuid4())
    group = SimpleNamespace(id=uuid.uuid4())
    row = SimpleNamespace(id=uuid.uuid4(), mutual_exclusion_group_id=group.id)

    shoplab = _SeededSession(row)
    assert sorted(seed_shoplab.seed_experiments(shoplab, admin)) == sorted(
        seed_shoplab.EXPERIMENT_KEYS
    )
    streampulse = _SeededSession(row)
    assert sorted(
        seed_streampulse.seed_experiments(streampulse, admin, group)
    ) == sorted(seed_streampulse.EXPERIMENT_KEYS)
    assert shoplab.added == [] and streampulse.added == []
