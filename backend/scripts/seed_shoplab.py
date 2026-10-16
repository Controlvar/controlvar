#!/usr/bin/env python3
"""
ShopLab demo seeder.

Creates the catalogue the ShopLab storefront (``demo/shoplab``) and its traffic
simulator rely on:

- the demo users (same as ``seed_demo_data.py``; ``admin@demo.com`` owns everything)
- 4 ACTIVE experiments with stable ``key`` values, variants and Metric rows
  (A/B with sequential + Bayesian, a Thompson-sampling bandit, a multivariate
  test, and a CUPED + Bayesian A/B test)
- 2 ACTIVE feature flags (one with a rollout schedule + safety config)
- the ``shoplab-storefront`` API key, written to ``demo/shoplab/.api_key`` and
  ``demo/shoplab/.env.local`` (both gitignored)
- 14 days of history (assignments, exposure and conversion events) per
  experiment using the same true conversion rates as the live simulator

This script is IDEMPOTENT: a second run is a no-op.  ``--reset`` removes
everything it created.

Usage:
    source venv/bin/activate
    python backend/scripts/seed_shoplab.py [--no-history] [--history-users N]
    python backend/scripts/seed_shoplab.py --reset
"""

import argparse
import math
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Importing seed_demo_data applies the shared environment defaults (APP_ENV,
# POSTGRES_*) *before* any backend.app module reads settings, and gives us
# the helpers we reuse.  The module only runs its seeder under __main__.
from sqlalchemy import func

# Registers the "Report" class referenced by name in User/Experiment/FeatureFlag
# relationships; without it the first ORM query fails to configure mappers.
import backend.app.models.report  # noqa: F401
from backend.app.core.bandit_scheduler import BanditScheduler
from backend.app.core.security import hash_api_key
from backend.app.db.session import SessionLocal
from backend.app.models.api_key import APIKey, generate_api_key
from backend.app.models.assignment import Assignment
from backend.app.models.bandit_state import BanditState
from backend.app.models.event import Event
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import (
    AggregatedMetric,
    ErrorLog,
    RawMetric,
)
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)
from backend.app.models.safety import FeatureFlagSafetyConfig
from backend.app.schemas.bayesian import BayesianConfig
from backend.app.schemas.experiment import SequentialTestingConfigInput
from backend.app.schemas.variance_reduction import (
    VarianceReductionConfig,
    VarianceReductionMethod,
)
from backend.scripts.seed_demo_data import (
    _bulk_insert,
    days_ago,
    ensure_schema,
    ensure_tables,
    now_utc,
    seed_users,
)
from backend.scripts.seed_guard import require_development_environment

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADMIN_EMAIL = "admin@demo.com"
API_KEY_NAME = "shoplab-storefront"
ENV_KEY_NAME = "NEXT_PUBLIC_CONTROLVAR_API_KEY"
#: The line an .env.local written before the rename holds; replaced, not kept.
FORMER_ENV_KEY_NAME = "NEXT_PUBLIC_EXPERIMENTLY_API_KEY"

# SHOPLAB_DEMO_DIR lets CI / verification runs redirect the generated key
# files away from the checked-in demo app directory.
SHOPLAB_DIR = Path(
    os.environ.get("SHOPLAB_DEMO_DIR") or PROJECT_ROOT / "demo" / "shoplab"
)
API_KEY_FILE = SHOPLAB_DIR / ".api_key"
ENV_LOCAL_FILE = SHOPLAB_DIR / ".env.local"

HISTORY_DAYS = 14
DEFAULT_HISTORY_USERS = 4000
HISTORY_USER_PREFIX = "shoplab_hist"

# Order values are lognormal: median $85, sigma 0.5 (spec §3).
ORDER_VALUE_MEDIAN = 85.0
ORDER_VALUE_SIGMA = 0.5
# Unit prices for add_to_cart events: lognormal, median $40.
UNIT_PRICE_MEDIAN = 40.0
UNIT_PRICE_SIGMA = 0.45

# True conversion rates (spec §3). The live simulator uses the same numbers.
RATES: Dict[str, Dict[str, Any]] = {
    "shoplab_hero_banner": {
        "hero_cta_click": {"control": 0.12, "video_hero": 0.15},
        "purchase_after_click": 0.25,
    },
    "shoplab_plp_sort": {
        "product_click": {
            "relevance": 0.22,
            "price_low_high": 0.19,
            "ml_personalized": 0.28,
        },
        "add_to_cart_after_click": 0.30,
    },
    "shoplab_pdp_buy_button": {
        "add_to_cart": {
            "control": 0.080,
            "green_add": 0.085,
            "orange_buy_now": 0.105,
            "green_buy_now": 0.095,
        },
    },
    "shoplab_checkout_flow": {
        "purchase": {"standard": 0.62, "one_page": 0.68},
    },
}

PLP_SORT_BY_VARIANT = {
    "relevance": "relevance",
    "price_low_high": "price_asc",
    "ml_personalized": "ml",
}
PDP_BUTTON_TEXT = {
    "control": "Add to cart",
    "green_add": "Add to cart",
    "orange_buy_now": "Buy now",
    "green_buy_now": "Buy now",
}

# ---------------------------------------------------------------------------
# Catalogue (spec §2)
# ---------------------------------------------------------------------------

# A metric's expected_effect is a RELATIVE lift: 0.25 means 12% -> 15%. The
# primary metric's is the effect the results page plans the sample for, so
# backend/tests/unit/scripts/test_seed_planned_effects.py pins each plan.
EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "key": "shoplab_hero_banner",
        "name": "ShopLab: Hero banner — image vs video",
        "description": "Home page hero: static seasonal image vs autoplaying product video.",
        "hypothesis": "A video hero lifts hero CTA click-through by 20%+ without hurting purchases.",
        "experiment_type": ExperimentType.A_B,
        "optimization_type": "fixed",
        "sequential": True,
        "bayesian": True,
        "cuped": False,
        "variants": [
            {
                "name": "control",
                "is_control": True,
                "traffic_allocation": 50,
                "description": "Static seasonal image hero",
                "configuration": {
                    "media": "image",
                    "headline": "Gear up for the season",
                    "cta": "Shop the collection",
                },
            },
            {
                "name": "video_hero",
                "is_control": False,
                "traffic_allocation": 50,
                "description": "Autoplaying product video hero",
                "configuration": {
                    "media": "video",
                    "headline": "See it in motion",
                    "cta": "Watch & shop",
                },
            },
        ],
        "metrics": [
            {
                "name": "Hero CTA click-through",
                "event_name": "hero_cta_click",
                "metric_type": MetricType.CONVERSION,
                "is_primary": True,
                "minimum_sample_size": 2000,
                "expected_effect": 0.25,
            },
            {
                "name": "Purchase",
                "event_name": "purchase",
                "metric_type": MetricType.CONVERSION,
                "is_primary": False,
                "minimum_sample_size": 2000,
                "expected_effect": 0.33,
            },
        ],
    },
    {
        "key": "shoplab_plp_sort",
        "name": "ShopLab: Product list default sort (bandit)",
        "description": "Thompson-sampling bandit over the default sort order on the product listing page.",
        "hypothesis": "ML-personalised ordering drives more product clicks than relevance or price sort.",
        "experiment_type": ExperimentType.BANDIT,
        "optimization_type": "thompson_sampling",
        "sequential": False,
        "bayesian": False,
        "cuped": False,
        "variants": [
            {
                "name": "relevance",
                "is_control": True,
                "traffic_allocation": 34,
                "description": "Relevance sort (current default)",
                "configuration": {"algorithm": "relevance"},
            },
            {
                "name": "price_low_high",
                "is_control": False,
                "traffic_allocation": 33,
                "description": "Price ascending",
                "configuration": {"algorithm": "price_asc"},
            },
            {
                "name": "ml_personalized",
                "is_control": False,
                "traffic_allocation": 33,
                "description": "ML-personalised ranking",
                "configuration": {"algorithm": "ml"},
            },
        ],
        "metrics": [
            {
                "name": "Product click",
                "event_name": "product_click",
                "metric_type": MetricType.CONVERSION,
                "is_primary": True,
                "minimum_sample_size": 1000,
                "expected_effect": 0.14,
            },
            {
                "name": "Add to cart",
                "event_name": "add_to_cart",
                "metric_type": MetricType.CONVERSION,
                "is_primary": False,
                "minimum_sample_size": 1000,
                "expected_effect": 0.15,
            },
        ],
    },
    {
        "key": "shoplab_pdp_buy_button",
        "name": "ShopLab: PDP buy button colour × copy",
        "description": "Multivariate test of the product page primary button: colour (navy/green/orange) × copy (Add to cart/Buy now).",
        "hypothesis": "A high-contrast button with urgency copy raises add-to-cart rate.",
        "experiment_type": ExperimentType.MULTIVARIATE,
        "optimization_type": "fixed",
        "sequential": True,
        "bayesian": False,
        "cuped": False,
        "variants": [
            {
                "name": "control",
                "is_control": True,
                "traffic_allocation": 25,
                "description": "Navy 'Add to cart'",
                "configuration": {"color": "#0f172a", "text": "Add to cart"},
            },
            {
                "name": "green_add",
                "is_control": False,
                "traffic_allocation": 25,
                "description": "Green 'Add to cart'",
                "configuration": {"color": "#16a34a", "text": "Add to cart"},
            },
            {
                "name": "orange_buy_now",
                "is_control": False,
                "traffic_allocation": 25,
                "description": "Orange 'Buy now'",
                "configuration": {"color": "#ea580c", "text": "Buy now"},
            },
            {
                "name": "green_buy_now",
                "is_control": False,
                "traffic_allocation": 25,
                "description": "Green 'Buy now'",
                "configuration": {"color": "#16a34a", "text": "Buy now"},
            },
        ],
        "metrics": [
            {
                "name": "Add to cart",
                "event_name": "add_to_cart",
                "metric_type": MetricType.CONVERSION,
                "is_primary": True,
                "minimum_sample_size": 4000,
                "expected_effect": 0.25,
            },
            {
                "name": "Purchase",
                "event_name": "purchase",
                "metric_type": MetricType.CONVERSION,
                "is_primary": False,
                "minimum_sample_size": 4000,
                "expected_effect": None,
            },
        ],
    },
    {
        "key": "shoplab_checkout_flow",
        "name": "ShopLab: Checkout flow — 3 steps vs one page",
        "description": "Standard 3-step checkout vs a single-page checkout. CUPED-adjusted with Bayesian decisioning.",
        "hypothesis": "A one-page checkout lifts purchase completion by 5+ points among users who begin checkout.",
        "experiment_type": ExperimentType.A_B,
        "optimization_type": "fixed",
        "sequential": False,
        "bayesian": True,
        "cuped": True,
        "variants": [
            {
                "name": "standard",
                "is_control": True,
                "traffic_allocation": 50,
                "description": "3-step checkout",
                "configuration": {"steps": 3},
            },
            {
                "name": "one_page",
                "is_control": False,
                "traffic_allocation": 50,
                "description": "Single-page checkout",
                "configuration": {"steps": 1},
            },
        ],
        "metrics": [
            {
                "name": "Purchase",
                "event_name": "purchase",
                "metric_type": MetricType.CONVERSION,
                "is_primary": True,
                "minimum_sample_size": 2000,
                "expected_effect": 0.08,
            },
            {
                "name": "Order value",
                "event_name": "purchase",
                "metric_type": MetricType.REVENUE,
                "is_primary": False,
                "aggregation_method": "sum",
                "event_value_path": "value",
                "minimum_sample_size": 2000,
                "expected_effect": None,
            },
        ],
    },
]

EXPERIMENT_KEYS = [spec["key"] for spec in EXPERIMENTS]

FLAG_KEYS = ["shoplab_new_search", "shoplab_free_shipping_banner"]

SAFETY_METRICS = {
    "error_rate": {
        "warning_threshold": 0.02,
        "critical_threshold": 0.05,
        "comparison_type": "greater_than",
    }
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _lognormal(rng: random.Random, median: float, sigma: float) -> float:
    return round(math.exp(math.log(median) + sigma * rng.gauss(0.0, 1.0)), 2)


def _order_value(rng: random.Random) -> float:
    return _lognormal(rng, ORDER_VALUE_MEDIAN, ORDER_VALUE_SIGMA)


def _unit_price(rng: random.Random) -> float:
    return _lognormal(rng, UNIT_PRICE_MEDIAN, UNIT_PRICE_SIGMA)


def _product_id(rng: random.Random) -> str:
    return f"sl-{rng.randint(1, 12):03d}"


def _short(key: str) -> str:
    return key[len("shoplab_") :] if key.startswith("shoplab_") else key


def _display(path: Path) -> str:
    """Repo-relative path for messages; absolute when outside the repo."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _legacy_metrics_json(spec: Dict[str, Any]) -> Dict[str, Any]:
    primary = next(m for m in spec["metrics"] if m["is_primary"])
    secondary = [m["event_name"] for m in spec["metrics"] if not m["is_primary"]]
    return {
        "primary_metric": primary["event_name"],
        "metric_type": primary["metric_type"].value,
        "secondary_metrics": secondary,
    }


def _write_env_line(path: Path, name: str, value: Optional[str]) -> None:
    """Create/replace the ``name=`` line in ``path`` (or drop it when value is None)."""
    lines = path.read_text().splitlines() if path.exists() else []
    names = (name, FORMER_ENV_KEY_NAME) if name == ENV_KEY_NAME else (name,)
    kept = [
        line for line in lines if not line.startswith(tuple(f"{n}=" for n in names))
    ]
    if value is not None:
        kept.append(f"{name}={value}")
    if not kept and not path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(kept) + ("\n" if kept else ""))


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------


def seed_experiments(db, admin_user) -> Dict[str, Experiment]:
    """Create the four ShopLab experiments (idempotent by ``key``)."""
    print("  Seeding ShopLab experiments...")
    experiments: Dict[str, Experiment] = {}

    for spec in EXPERIMENTS:
        existing = db.query(Experiment).filter(Experiment.key == spec["key"]).first()
        if existing:
            print(f"    '{spec['key']}' already exists (id={existing.id}), skipping.")
            experiments[spec["key"]] = existing
            continue

        exp = Experiment(
            key=spec["key"],
            name=spec["name"],
            description=spec["description"],
            hypothesis=spec["hypothesis"],
            status=ExperimentStatus.ACTIVE,
            experiment_type=spec["experiment_type"],
            optimization_type=spec["optimization_type"],
            owner_id=admin_user.id,
            start_date=days_ago(HISTORY_DAYS),
            end_date=days_ago(-30),
            targeting_rules={},
            metrics=_legacy_metrics_json(spec),
            tags=["shoplab", "demo"],
        )
        if spec["sequential"]:
            exp.sequential_testing_enabled = True
            exp.sequential_testing_method = "msprt"
            exp.sequential_testing_config = SequentialTestingConfigInput().model_dump()
        if spec["bayesian"]:
            exp.bayesian_enabled = True
            exp.bayesian_config = BayesianConfig(rope=[-0.005, 0.005]).model_dump(
                mode="json"
            )
        db.add(exp)
        db.flush()

        for v in spec["variants"]:
            db.add(
                Variant(
                    experiment_id=exp.id,
                    name=v["name"],
                    description=v["description"],
                    is_control=v["is_control"],
                    traffic_allocation=v["traffic_allocation"],
                    configuration=v["configuration"],
                )
            )

        metric_rows: List[Metric] = []
        for m in spec["metrics"]:
            metric = Metric(experiment_id=exp.id, **m)
            db.add(metric)
            metric_rows.append(metric)
        db.flush()

        if spec["cuped"]:
            primary = next(m for m in metric_rows if m.is_primary)
            exp.variance_reduction_config = VarianceReductionConfig(
                method=VarianceReductionMethod.CUPED,
                covariate_metric_id=str(primary.id),
                covariate_lookback_days=7,
            ).model_dump(mode="json")

        db.commit()
        db.refresh(exp)
        experiments[spec["key"]] = exp
        print(
            f"    Created '{spec['key']}' ({spec['experiment_type'].value}, "
            f"{spec['optimization_type']}, {len(spec['variants'])} variants)."
        )

    return experiments


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------


def seed_feature_flags(db, admin_user) -> Dict[str, FeatureFlag]:
    """Create ``shoplab_new_search`` (with rollout + safety) and ``shoplab_free_shipping_banner``."""
    print("  Seeding ShopLab feature flags...")
    flags: Dict[str, FeatureFlag] = {}

    key = "shoplab_new_search"
    existing = db.query(FeatureFlag).filter(FeatureFlag.key == key).first()
    if existing:
        print(f"    '{key}' already exists, skipping.")
        flags[key] = existing
    else:
        flag = FeatureFlag(
            key=key,
            name="ShopLab: New search engine",
            description="Gradual rollout of the new search engine on the ShopLab storefront.",
            status=FeatureFlagStatus.ACTIVE,
            owner_id=admin_user.id,
            rollout_percentage=10,
            targeting_rules={},
            tags=["search", "rollout"],
        )
        db.add(flag)
        db.flush()

        schedule = RolloutSchedule(
            feature_flag_id=flag.id,
            name="New search engine rollout",
            description="10% → 50% → 100% over two weeks",
            status=RolloutScheduleStatus.ACTIVE,
            start_date=days_ago(2),
            end_date=days_ago(-30),
            max_percentage=100,
            min_stage_duration=24,
            owner_id=admin_user.id,
        )
        db.add(schedule)
        db.flush()

        db.add_all(
            [
                RolloutStage(
                    rollout_schedule_id=schedule.id,
                    name="Canary 10%",
                    description="First 10% of visitors",
                    stage_order=1,
                    target_percentage=10,
                    trigger_type=TriggerType.TIME_BASED,
                    start_date=days_ago(2),
                    status=RolloutStageStatus.IN_PROGRESS,
                ),
                RolloutStage(
                    rollout_schedule_id=schedule.id,
                    name="Expand to 50%",
                    description="Half of all visitors",
                    stage_order=2,
                    target_percentage=50,
                    trigger_type=TriggerType.TIME_BASED,
                    start_date=days_ago(-5),
                    status=RolloutStageStatus.PENDING,
                ),
                RolloutStage(
                    rollout_schedule_id=schedule.id,
                    name="Full rollout",
                    description="Everyone",
                    stage_order=3,
                    target_percentage=100,
                    trigger_type=TriggerType.TIME_BASED,
                    start_date=days_ago(-12),
                    status=RolloutStageStatus.PENDING,
                ),
            ]
        )
        db.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics=SAFETY_METRICS,
                rollback_percentage=0,
            )
        )
        db.commit()
        flags[key] = flag
        print(f"    Created '{key}' (10% rollout, 3-stage schedule, safety config).")

    key = "shoplab_free_shipping_banner"
    existing = db.query(FeatureFlag).filter(FeatureFlag.key == key).first()
    if existing:
        print(f"    '{key}' already exists, skipping.")
        flags[key] = existing
    else:
        flag = FeatureFlag(
            key=key,
            name="ShopLab: Free shipping banner",
            description="Kill switch for the free-shipping promo banner in the header.",
            status=FeatureFlagStatus.ACTIVE,
            owner_id=admin_user.id,
            rollout_percentage=100,
            targeting_rules={},
            tags=["promo", "kill-switch"],
        )
        db.add(flag)
        db.commit()
        flags[key] = flag
        print(f"    Created '{key}' (100% rollout).")

    return flags


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------


def seed_api_key(db, admin_user) -> str:
    """
    Ensure the ``shoplab-storefront`` API key exists and is written to disk.

    Keeps the existing row when ``demo/shoplab/.api_key`` still holds the
    matching plaintext; otherwise the stale row is replaced by a fresh key.
    Returns the plaintext key.
    """
    print("  Seeding ShopLab API key...")
    existing = (
        db.query(APIKey)
        .filter(APIKey.name == API_KEY_NAME, APIKey.user_id == admin_user.id)
        .first()
    )

    if existing is not None and API_KEY_FILE.exists():
        plaintext = API_KEY_FILE.read_text().strip()
        if plaintext and hash_api_key(plaintext) == existing.key:
            _write_env_line(ENV_LOCAL_FILE, ENV_KEY_NAME, plaintext)
            print(f"    Existing key kept ({_display(API_KEY_FILE)}).")
            return plaintext
        print("    demo/shoplab/.api_key does not match the stored hash; rotating.")

    if existing is not None:
        db.delete(existing)
        db.commit()
        print("    Removed stale 'shoplab-storefront' key row.")

    plaintext = os.environ.get("SHOPLAB_API_KEY") or generate_api_key()
    db.add(
        APIKey(
            user_id=admin_user.id,
            key=hash_api_key(plaintext),
            name=API_KEY_NAME,
            description="ShopLab demo storefront + traffic simulator",
            scopes="read,write",
            is_active=True,
        )
    )
    db.commit()

    SHOPLAB_DIR.mkdir(parents=True, exist_ok=True)
    API_KEY_FILE.write_text(plaintext + "\n")
    os.chmod(API_KEY_FILE, 0o600)
    _write_env_line(ENV_LOCAL_FILE, ENV_KEY_NAME, plaintext)
    print(f"    Created key, written to {_display(API_KEY_FILE)}.")
    return plaintext


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def _pick_variant(rng: random.Random, variants: List[Variant]) -> Variant:
    total = sum(v.traffic_allocation or 0 for v in variants) or len(variants)
    roll = rng.uniform(0, total)
    cumulative = 0.0
    for variant in variants:
        cumulative += variant.traffic_allocation or (total / len(variants))
        if roll < cumulative:
            return variant
    return variants[-1]


def _funnel_events(
    key: str,
    variant_name: str,
    user_id: str,
    experiment_id,
    variant_id,
    exposure_at: datetime,
    rng: random.Random,
) -> List[Event]:
    """Conversion events for one seeded visitor, following the §3 rates."""
    events: List[Event] = []
    clock = {"t": exposure_at}

    def later(lo: int, hi: int) -> str:
        clock["t"] = clock["t"] + timedelta(seconds=rng.randint(lo, hi))
        return clock["t"].isoformat()

    def event(
        event_type: str, created_at: str, value: float = 1.0, metadata=None
    ) -> Event:
        return Event(
            event_type=event_type,
            event_name=event_type,
            user_id=user_id,
            experiment_id=experiment_id,
            variant_id=variant_id,
            value=value,
            event_metadata=metadata,
            created_at=created_at,
        )

    rates = RATES[key]
    if key == "shoplab_hero_banner":
        if rng.random() < rates["hero_cta_click"][variant_name]:
            events.append(event("hero_cta_click", later(2, 60), metadata={"page": "/"}))
            if rng.random() < rates["purchase_after_click"]:
                items = rng.randint(1, 3)
                events.append(
                    event(
                        "purchase",
                        later(120, 1800),
                        value=_order_value(rng),
                        metadata={
                            "items": items,
                            "flow": "standard",
                            "order_id": f"SL-{user_id[-5:]}",
                        },
                    )
                )
    elif key == "shoplab_plp_sort":
        if rng.random() < rates["product_click"][variant_name]:
            product_id = _product_id(rng)
            events.append(
                event(
                    "product_click",
                    later(3, 90),
                    metadata={
                        "product_id": product_id,
                        "position": rng.randint(1, 12),
                        "sort": PLP_SORT_BY_VARIANT[variant_name],
                    },
                )
            )
            if rng.random() < rates["add_to_cart_after_click"]:
                events.append(
                    event(
                        "add_to_cart",
                        later(10, 240),
                        value=_unit_price(rng),
                        metadata={
                            "product_id": product_id,
                            "quantity": 1,
                            "button": "Add to cart",
                        },
                    )
                )
    elif key == "shoplab_pdp_buy_button":
        if rng.random() < rates["add_to_cart"][variant_name]:
            events.append(
                event(
                    "add_to_cart",
                    later(5, 180),
                    value=_unit_price(rng),
                    metadata={
                        "product_id": _product_id(rng),
                        "quantity": rng.choice([1, 1, 1, 2]),
                        "button": PDP_BUTTON_TEXT[variant_name],
                    },
                )
            )
    elif key == "shoplab_checkout_flow":
        flow = "standard" if variant_name == "standard" else "one_page"
        items = rng.randint(1, 4)
        total = _order_value(rng)
        events.append(
            event(
                "begin_checkout",
                later(1, 30),
                value=total,
                metadata={"items": items, "flow": flow},
            )
        )
        if rng.random() < rates["purchase"][variant_name]:
            events.append(
                event(
                    "purchase",
                    later(60, 900),
                    value=total,
                    metadata={
                        "items": items,
                        "flow": flow,
                        "order_id": f"SL-{user_id[-5:]}",
                    },
                )
            )
    return events


def seed_history(
    db, experiments: Dict[str, Experiment], users_per_experiment: int
) -> Dict[str, Dict[str, int]]:
    """Seed ``HISTORY_DAYS`` of assignments + events per experiment (skips experiments with data)."""
    print(
        f"  Seeding {HISTORY_DAYS}-day history (~{users_per_experiment:,} users per experiment)..."
    )
    summary: Dict[str, Dict[str, int]] = {}
    now = now_utc()

    for key, exp in experiments.items():
        existing = (
            db.query(func.count(Assignment.id))
            .filter(Assignment.experiment_id == exp.id)
            .scalar()
            or 0
        )
        if existing:
            print(f"    '{key}' already has {existing:,} assignments, skipping.")
            summary[key] = {"assignments": 0, "events": 0}
            continue

        variants = sorted(exp.variants, key=lambda v: v.name)
        rng = random.Random(f"shoplab-history:{key}")
        assignments: List[Assignment] = []
        events: List[Event] = []

        for i in range(users_per_experiment):
            user_id = f"{HISTORY_USER_PREFIX}_{_short(key)}_{i:05d}"
            variant = _pick_variant(rng, variants)
            exposure_at = now - timedelta(days=rng.uniform(0.05, HISTORY_DAYS))
            assignments.append(
                Assignment(
                    experiment_id=exp.id,
                    variant_id=variant.id,
                    user_id=user_id,
                    created_at=exposure_at,
                    context={"source": "seed_shoplab"},
                )
            )
            events.append(
                Event(
                    event_type="experiment_exposure",
                    event_name="experiment_exposure",
                    user_id=user_id,
                    experiment_id=exp.id,
                    variant_id=variant.id,
                    value=1.0,
                    created_at=exposure_at.isoformat(),
                )
            )
            events.extend(
                _funnel_events(
                    key, variant.name, user_id, exp.id, variant.id, exposure_at, rng
                )
            )

        _bulk_insert(db, assignments, batch_size=1000)
        _bulk_insert(db, events, batch_size=1000)
        db.commit()
        summary[key] = {"assignments": len(assignments), "events": len(events)}
        print(f"    '{key}': {len(assignments):,} assignments, {len(events):,} events.")

    return summary


def ensure_bandit_state(db, experiment: Experiment) -> Optional[BanditState]:
    """Compute initial bandit weights from the seeded history (only when missing)."""
    state = (
        db.query(BanditState).filter(BanditState.experiment_id == experiment.id).first()
    )
    if state is not None:
        print("  Bandit state already present, skipping weight bootstrap.")
        return state

    print("  Bootstrapping bandit weights from seeded history...")
    scheduler = BanditScheduler(db)
    # History lives in PostgreSQL; skip the DynamoDB probe so the seed never
    # waits on AWS credentials/network.
    scheduler._stats_from_dynamodb = lambda *args, **kwargs: None  # type: ignore[assignment]
    try:
        scheduler.update_experiment(experiment)
    except Exception as exc:  # pragma: no cover - best effort for the demo
        db.rollback()
        print(f"    Could not compute bandit weights (the scheduler will retry): {exc}")
        return None

    state = (
        db.query(BanditState).filter(BanditState.experiment_id == experiment.id).first()
    )
    if state is not None:
        names = {str(v.id): v.name for v in experiment.variants}
        for vid, data in state.variant_weights.items():
            print(
                f"    {names.get(vid, vid):<16} weight={data['weight']:.3f} "
                f"({data['successes']}/{data['pulls']} conversions)"
            )
    return state


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def reset_shoplab(db) -> Dict[str, int]:
    """Delete everything this script creates. Returns per-table delete counts."""
    counts: Dict[str, int] = {
        "experiments": 0,
        "assignments": 0,
        "events": 0,
        "bandit_states": 0,
        "feature_flags": 0,
        "api_keys": 0,
    }

    for exp in db.query(Experiment).filter(Experiment.key.in_(EXPERIMENT_KEYS)).all():
        counts["events"] += (
            db.query(Event)
            .filter(Event.experiment_id == exp.id)
            .delete(synchronize_session=False)
        )
        counts["assignments"] += (
            db.query(Assignment)
            .filter(Assignment.experiment_id == exp.id)
            .delete(synchronize_session=False)
        )
        counts["bandit_states"] += (
            db.query(BanditState)
            .filter(BanditState.experiment_id == exp.id)
            .delete(synchronize_session=False)
        )
        db.commit()
        db.expire_all()
        # ORM delete cascades to variants / metrics / reports
        db.delete(db.query(Experiment).filter(Experiment.id == exp.id).one())
        db.commit()
        counts["experiments"] += 1
        print(f"    Deleted experiment '{exp.key}'.")

    for flag in db.query(FeatureFlag).filter(FeatureFlag.key.in_(FLAG_KEYS)).all():
        counts["events"] += (
            db.query(Event)
            .filter(Event.feature_flag_id == flag.id)
            .delete(synchronize_session=False)
        )
        for model in (RawMetric, AggregatedMetric, ErrorLog):
            db.query(model).filter(model.feature_flag_id == flag.id).delete(
                synchronize_session=False
            )
        db.commit()
        db.expire_all()
        # ORM delete cascades to rollout schedules/stages, safety config, overrides
        db.delete(db.query(FeatureFlag).filter(FeatureFlag.id == flag.id).one())
        db.commit()
        counts["feature_flags"] += 1
        print(f"    Deleted feature flag '{flag.key}'.")

    counts["api_keys"] = (
        db.query(APIKey)
        .filter(APIKey.name == API_KEY_NAME)
        .delete(synchronize_session=False)
    )
    db.commit()

    if API_KEY_FILE.exists():
        API_KEY_FILE.unlink()
        print(f"    Removed {_display(API_KEY_FILE)}.")
    if ENV_LOCAL_FILE.exists():
        _write_env_line(ENV_LOCAL_FILE, ENV_KEY_NAME, None)
        print(f"    Removed {ENV_KEY_NAME} from {_display(ENV_LOCAL_FILE)}.")

    return counts


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def print_summary(
    db, experiments: Dict[str, Experiment], flags: Dict[str, FeatureFlag]
) -> None:
    print("\n" + "=" * 64)
    print("  ShopLab Seeding Complete!")
    print("=" * 64)
    print(
        f"  {'experiment':<26}{'type':<10}{'variants':>9}{'assign.':>10}{'events':>9}"
    )
    for key, exp in experiments.items():
        n_assign = (
            db.query(func.count(Assignment.id))
            .filter(Assignment.experiment_id == exp.id)
            .scalar()
            or 0
        )
        n_events = (
            db.query(func.count(Event.id))
            .filter(Event.experiment_id == exp.id)
            .scalar()
            or 0
        )
        print(
            f"  {key:<26}{exp.experiment_type.value:<10}{len(exp.variants):>9}"
            f"{n_assign:>10,}{n_events:>9,}"
        )
    print()
    for key, flag in flags.items():
        print(
            f"  flag {key:<32} {flag.status.value:<8} rollout {flag.rollout_percentage}%"
        )
    print()
    print(f"  API key file:  {_display(API_KEY_FILE)}  (X-API-Key)")
    print(f"  Env file:      {_display(ENV_LOCAL_FILE)}  ({ENV_KEY_NAME})")
    print(f"  Owner:         {ADMIN_EMAIL} / Demo1234!")
    print("=" * 64 + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    require_development_environment("shoplab")
    parser = argparse.ArgumentParser(description="Seed the ShopLab demo catalogue")
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Skip the 14-day assignment/event history",
    )
    parser.add_argument(
        "--history-users",
        type=int,
        default=DEFAULT_HISTORY_USERS,
        metavar="N",
        help=f"Users per experiment for the history (default {DEFAULT_HISTORY_USERS})",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete every ShopLab experiment/flag/event/API key this script created and exit",
    )
    args = parser.parse_args(argv)

    print("\n" + "=" * 64)
    print("  ShopLab Demo Seeder")
    print("=" * 64)

    with SessionLocal() as db:
        ensure_schema(db)
    ensure_tables()

    if args.reset:
        print("\nResetting ShopLab data...")
        with SessionLocal() as db:
            counts = reset_shoplab(db)
        print("\n  Removed: " + ", ".join(f"{k}={v:,}" for k, v in counts.items()))
        print()
        return 0

    with SessionLocal() as db:
        print("\n[1/6] Users")
        users = seed_users(db)
        admin_user = users[ADMIN_EMAIL]

        print("\n[2/6] Experiments")
        experiments = seed_experiments(db, admin_user)

        print("\n[3/6] Feature flags")
        flags = seed_feature_flags(db, admin_user)

        print("\n[4/6] API key")
        seed_api_key(db, admin_user)

        print("\n[5/6] History")
        if args.no_history:
            print("  --no-history given, skipping.")
        else:
            seed_history(db, experiments, max(args.history_users, 0))

        print("\n[6/6] Bandit state")
        ensure_bandit_state(db, experiments["shoplab_plp_sort"])

        print_summary(db, experiments, flags)

    return 0


if __name__ == "__main__":
    sys.exit(main())
