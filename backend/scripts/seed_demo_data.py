#!/usr/bin/env python3
"""
Demo data seeder for the Controlvar platform.

Creates all demo DB records including:
- 4 demo users (ADMIN, DEVELOPER, ANALYST, VIEWER)
- 3 experiments with 100K+ synthetic events
- 2 feature flags with rollout schedules and safety config
- Audit log entries and global safety settings

This script is IDEMPOTENT: safe to re-run. Checks for existing data before inserting.

Usage:
    cd /path/to/controlvar
    source venv/bin/activate
    python backend/scripts/seed_demo_data.py
"""

import argparse
import copy
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to Python path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Set environment before importing app modules
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("POSTGRES_SERVER", "localhost")
os.environ.setdefault("POSTGRES_USER", "postgres")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres")
os.environ.setdefault("POSTGRES_DB", "experimentation")
os.environ.setdefault("POSTGRES_SCHEMA", "experimentation")

from sqlalchemy import text

from backend.app.core.database_config import get_schema_name
from backend.app.core.security import get_password_hash
from backend.app.db.session import SessionLocal, engine
from backend.app.models.assignment import Assignment
from backend.app.models.audit_log import ActionType, AuditLog, EntityType
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
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)
from backend.app.models.safety import FeatureFlagSafetyConfig, SafetySettings
from backend.app.models.user import User, UserRole
from backend.app.services.audit_service import role_value
from backend.scripts import seed_audit
from backend.scripts.seed_audit import seeded_entry
from backend.scripts.seed_guard import require_development_environment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def days_ago(n: int) -> datetime:
    return now_utc() - timedelta(days=n)


def ensure_schema(db):
    """Create schema if it doesn't exist (idempotent)."""
    schema = get_schema_name()
    db.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    db.commit()


# Public ``experiment_key`` values used by the SDK-facing tracking API
# (``POST /api/v1/tracking/assign`` etc.) to find the demo experiments.
DEMO_EXPERIMENT_KEYS = {
    "Homepage Hero Copy Test": "homepage_hero_copy",
    "Checkout Button Color": "checkout_button_color",
    "Recommendation Algorithm MAB": "recommendation_algorithm_mab",
}


def backfill_experiment_keys(db) -> int:
    """Give demo experiments seeded before ``key`` existed their public key.

    Returns the number of rows updated.  Idempotent: rows that already have a
    key are left alone.
    """
    updated = 0
    for name, key in DEMO_EXPERIMENT_KEYS.items():
        rows = (
            db.query(Experiment)
            .filter(Experiment.name == name, Experiment.key.is_(None))
            .all()
        )
        for row in rows:
            taken = db.query(Experiment).filter(Experiment.key == key).first()
            if taken is not None and taken.id != row.id:
                print(
                    f"    Key '{key}' already used by experiment {taken.id}; leaving {row.id} without a key."
                )
                continue
            row.key = key
            updated += 1
            print(f"    Backfilled key '{key}' on experiment '{name}' ({row.id}).")
    if updated:
        db.commit()
    return updated


def ensure_tables():
    """Create every table of the running profile if it does not exist yet.

    The same registration bootstrap uses -- the core models, then whatever
    the modules' registration adds -- so a database seeded by hand (the
    by-hand demo setup in demo/DEMO_GUIDE.md) has the tables the module routers
    need.  Importing a handful of model modules and calling ``create_all``,
    as this once did, built the 37 core tables and left
    ``GET /api/v1/workspaces`` to fail on a missing relation.
    """
    from backend.app.db.bootstrap import create_from_models

    create_from_models(engine, get_schema_name())


# ---------------------------------------------------------------------------
# User seeding
# ---------------------------------------------------------------------------

DEMO_USERS = [
    {
        "username": "demo_admin",
        "email": "admin@demo.com",
        "password": "Demo1234!",
        "role": UserRole.ADMIN,
        "is_superuser": True,
        "first_name": "Admin",
        "last_name": "Demo",
    },
    {
        "username": "demo_dev",
        "email": "dev@demo.com",
        "password": "Demo1234!",
        "role": UserRole.DEVELOPER,
        "is_superuser": False,
        "first_name": "Dev",
        "last_name": "Demo",
    },
    {
        "username": "demo_analyst",
        "email": "analyst@demo.com",
        "password": "Demo1234!",
        "role": UserRole.ANALYST,
        "is_superuser": False,
        "first_name": "Analyst",
        "last_name": "Demo",
    },
    {
        "username": "demo_viewer",
        "email": "viewer@demo.com",
        "password": "Demo1234!",
        "role": UserRole.VIEWER,
        "is_superuser": False,
        "first_name": "Viewer",
        "last_name": "Demo",
    },
]


def seed_users(db) -> dict:
    """Seed demo users. Returns {email: User} mapping."""
    print("  Seeding users...")
    users = {}
    for u_data in DEMO_USERS:
        existing = db.query(User).filter(User.email == u_data["email"]).first()
        if existing:
            users[u_data["email"]] = existing
            continue
        user = User(
            username=u_data["username"],
            email=u_data["email"],
            hashed_password=get_password_hash(u_data["password"]),
            role=u_data["role"],
            is_superuser=u_data["is_superuser"],
            is_active=True,
            first_name=u_data["first_name"],
            last_name=u_data["last_name"],
        )
        db.add(user)
        db.flush()
        users[u_data["email"]] = user
        print(f"    Created user: {u_data['email']} ({u_data['role'].value})")
    db.commit()
    return users


# ---------------------------------------------------------------------------
# Experiment 1: Homepage Hero Copy Test (COMPLETED — clear winner)
# ---------------------------------------------------------------------------


def seed_homepage_hero_experiment(db, admin_user) -> Experiment:
    """50K events, treatment wins at p < 0.001, ~12% lift. Status: COMPLETED."""
    print("  Seeding experiment: Homepage Hero Copy Test...")

    exp_name = "Homepage Hero Copy Test"
    existing = db.query(Experiment).filter(Experiment.name == exp_name).first()
    if existing:
        print(f"    Already exists (id={existing.id}), skipping.")
        return existing

    exp = Experiment(
        name=exp_name,
        key=DEMO_EXPERIMENT_KEYS[exp_name],
        description="Testing new hero copy to improve signup conversion.",
        hypothesis="The new, value-focused copy will increase sign-ups by 10%+.",
        status=ExperimentStatus.COMPLETED,
        experiment_type=ExperimentType.A_B,
        owner_id=admin_user.id,
        start_date=days_ago(45),
        end_date=days_ago(10),
        targeting_rules={},
        metrics={"primary_metric": "demo_signup", "metric_type": "conversion"},
        sequential_testing_enabled=True,
        sequential_testing_method="msprt",
    )
    db.add(exp)
    db.flush()

    # Variants
    control = Variant(
        experiment_id=exp.id,
        name="control",
        description="Original hero copy",
        is_control=True,
        traffic_allocation=50,
        configuration={"copy": "Build Better Products"},
    )
    treatment = Variant(
        experiment_id=exp.id,
        name="new_copy",
        description="Value-focused hero copy",
        is_control=False,
        traffic_allocation=50,
        configuration={"copy": "Ship 2x Faster With Data-Driven Decisions"},
    )
    db.add_all([control, treatment])
    db.flush()

    # Primary metric. expected_effect is the RELATIVE lift the results page
    # plans the sample for (0.10 means 7.5% -> 8.25%); each seed's plan is
    # pinned by backend/tests/unit/scripts/test_seed_planned_effects.py.
    metric = Metric(
        experiment_id=exp.id,
        name="Signup Conversion",
        event_name="demo_signup",
        metric_type=MetricType.CONVERSION,
        is_primary=True,
        minimum_sample_size=1000,
        expected_effect=0.10,
    )
    db.add(metric)
    db.flush()

    # Synthetic events: 25K per variant, treatment wins at ~12% lift
    # Control: 7.5% conversion, Treatment: 8.4% conversion
    TOTAL_PER_VARIANT = 25_000
    CONTROL_CVR = 0.075
    TREATMENT_CVR = 0.084  # ~12% relative lift

    print(f"    Generating {TOTAL_PER_VARIANT * 2:,} events for homepage_hero_copy...")
    events_to_add = []
    assignments_to_add = []

    rng = random.Random(42)
    exp_duration_days = 35

    for i in range(TOTAL_PER_VARIANT):
        user_id = f"user_hero_{i:06d}"
        days_offset = rng.uniform(0, exp_duration_days)
        event_time = days_ago(45) + timedelta(days=days_offset)

        # Control assignment + exposure
        assignments_to_add.append(
            Assignment(
                experiment_id=exp.id,
                variant_id=control.id,
                user_id=user_id,
                created_at=event_time,
            )
        )
        events_to_add.append(
            Event(
                event_type="experiment_exposure",
                event_name="experiment_exposure",
                user_id=user_id,
                experiment_id=exp.id,
                variant_id=control.id,
                value=1.0,
                created_at=event_time.isoformat(),
            )
        )
        if rng.random() < CONTROL_CVR:
            conv_time = event_time + timedelta(minutes=rng.randint(1, 60))
            events_to_add.append(
                Event(
                    event_type="demo_signup",
                    event_name="demo_signup",
                    user_id=user_id,
                    experiment_id=exp.id,
                    variant_id=control.id,
                    value=1.0,
                    created_at=conv_time.isoformat(),
                )
            )

    for i in range(TOTAL_PER_VARIANT):
        user_id = f"user_hero_t_{i:06d}"
        days_offset = rng.uniform(0, exp_duration_days)
        event_time = days_ago(45) + timedelta(days=days_offset)

        assignments_to_add.append(
            Assignment(
                experiment_id=exp.id,
                variant_id=treatment.id,
                user_id=user_id,
                created_at=event_time,
            )
        )
        events_to_add.append(
            Event(
                event_type="experiment_exposure",
                event_name="experiment_exposure",
                user_id=user_id,
                experiment_id=exp.id,
                variant_id=treatment.id,
                value=1.0,
                created_at=event_time.isoformat(),
            )
        )
        if rng.random() < TREATMENT_CVR:
            conv_time = event_time + timedelta(minutes=rng.randint(1, 60))
            events_to_add.append(
                Event(
                    event_type="demo_signup",
                    event_name="demo_signup",
                    user_id=user_id,
                    experiment_id=exp.id,
                    variant_id=treatment.id,
                    value=1.0,
                    created_at=conv_time.isoformat(),
                )
            )

    # Bulk insert in batches for performance
    _bulk_insert(db, assignments_to_add, batch_size=1000)
    _bulk_insert(db, events_to_add, batch_size=1000)
    db.commit()
    print(
        f"    Created {len(assignments_to_add):,} assignments and {len(events_to_add):,} events."
    )
    return exp


# ---------------------------------------------------------------------------
# Experiment 2: Checkout Button Color (ACTIVE — live simulator target)
# ---------------------------------------------------------------------------

#: Each arm's conversion rate after assignment.
CHECKOUT_CVR = {"blue_button": 0.08, "green_button": 0.09}

#: Pre-experiment history for CUPED (#217): the probability that a user sent a
#: ``checkout_completed`` with no key before their assignment, given whether
#: they convert in the experiment, per arm, as ``(if converts, if not)``.
#: History is drawn from a latent habit that exists before assignment, so its
#: share must be the same in both arms: ``p1 * cvr + p0 * (1 - cvr)`` is
#: 0.09802 for blue and 0.09803 for green.  Equal probabilities in both arms
#: would make history depend on the arm (green converts more), and CUPED would
#: then remove part of the real effect.
#: ``test_seed_demo_history.py`` holds the two shares within 1e-3.
CHECKOUT_HISTORY_P = {"blue_button": (0.449, 0.0675), "green_button": (0.436, 0.0646)}


def checkout_history_share(arm: str) -> float:
    """The share of an arm's users the seed gives pre-experiment history."""
    p1, p0 = CHECKOUT_HISTORY_P[arm]
    cvr = CHECKOUT_CVR[arm]
    return p1 * cvr + p0 * (1 - cvr)


def _checkout_history_event(history_rng, arm, user_id, assigned_at, converts):
    """One key-less ``checkout_completed`` 1-6.5 days before ``assigned_at``, or None.

    Its ``updated_at``, the time the server stored it, is set to the same
    moment as naive UTC: CUPED reads history only if it was stored before the
    assignment, and a day's margin keeps that true even on a database whose
    session is not in UTC (``assigned_at`` is written as an aware value).
    """
    p1, p0 = CHECKOUT_HISTORY_P[arm]
    if history_rng.random() >= (p1 if converts else p0):
        return None
    happened = assigned_at - timedelta(days=history_rng.uniform(1.0, 6.5))
    return Event(
        event_type="checkout_completed",
        event_name="checkout_completed",
        user_id=user_id,
        value=1.0,
        created_at=happened.isoformat(),
        updated_at=happened.astimezone(timezone.utc).replace(tzinfo=None),
    )


def seed_checkout_button_experiment(db, admin_user) -> Experiment:
    """30K pre-seeded events, no clear winner yet. Status: ACTIVE."""
    print("  Seeding experiment: Checkout Button Color...")

    exp_name = "Checkout Button Color"
    existing = db.query(Experiment).filter(Experiment.name == exp_name).first()
    if existing:
        print(f"    Already exists (id={existing.id}), skipping.")
        return existing

    exp = Experiment(
        name=exp_name,
        key=DEMO_EXPERIMENT_KEYS[exp_name],
        description="Testing blue vs. green checkout button to improve conversions.",
        hypothesis="A green CTA button will increase checkout completions.",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        owner_id=admin_user.id,
        start_date=days_ago(14),
        end_date=days_ago(-30),  # 30 days in the future
        targeting_rules={},
        metrics={"primary_metric": "checkout_completed", "metric_type": "conversion"},
    )
    db.add(exp)
    db.flush()

    blue_btn = Variant(
        experiment_id=exp.id,
        name="blue_button",
        description="Current blue checkout button",
        is_control=True,
        traffic_allocation=50,
        configuration={"button_color": "#1a73e8", "button_text": "Complete Purchase"},
    )
    green_btn = Variant(
        experiment_id=exp.id,
        name="green_button",
        description="New green checkout button",
        is_control=False,
        traffic_allocation=50,
        configuration={"button_color": "#34a853", "button_text": "Complete Purchase"},
    )
    db.add_all([blue_btn, green_btn])
    db.flush()

    metric = Metric(
        experiment_id=exp.id,
        name="Checkout Completion",
        event_name="checkout_completed",
        metric_type=MetricType.CONVERSION,
        is_primary=True,
        minimum_sample_size=5000,
        expected_effect=0.10,
    )
    db.add(metric)
    db.flush()

    # Historical events: 30K total, no clear winner yet
    # Blue: 8%, Green: 9% — trending but not significant
    TOTAL_PER_VARIANT = 15_000
    BLUE_CVR = CHECKOUT_CVR["blue_button"]
    GREEN_CVR = CHECKOUT_CVR["green_button"]

    print(
        f"    Generating {TOTAL_PER_VARIANT * 2:,} events for checkout_button_color..."
    )
    events_to_add = []
    assignments_to_add = []
    rng = random.Random(123)
    # History has its own generator, so rng(123)'s draws, and every other
    # number the demo shows, are what they were before it existed.
    history_rng = random.Random(217)
    history = 0

    for i in range(TOTAL_PER_VARIANT):
        user_id = f"user_checkout_b_{i:06d}"
        days_offset = rng.uniform(0, 14)
        event_time = days_ago(14) + timedelta(days=days_offset)

        assignments_to_add.append(
            Assignment(
                experiment_id=exp.id,
                variant_id=blue_btn.id,
                user_id=user_id,
                created_at=event_time,
            )
        )
        events_to_add.append(
            Event(
                event_type="experiment_exposure",
                event_name="experiment_exposure",
                user_id=user_id,
                experiment_id=exp.id,
                variant_id=blue_btn.id,
                value=1.0,
                created_at=event_time.isoformat(),
            )
        )
        converts = rng.random() < BLUE_CVR
        prior = _checkout_history_event(
            history_rng, "blue_button", user_id, event_time, converts
        )
        if prior is not None:
            events_to_add.append(prior)
            history += 1
        if converts:
            conv_time = event_time + timedelta(minutes=rng.randint(1, 30))
            events_to_add.append(
                Event(
                    event_type="checkout_completed",
                    event_name="checkout_completed",
                    user_id=user_id,
                    experiment_id=exp.id,
                    variant_id=blue_btn.id,
                    value=1.0,
                    created_at=conv_time.isoformat(),
                )
            )

    for i in range(TOTAL_PER_VARIANT):
        user_id = f"user_checkout_g_{i:06d}"
        days_offset = rng.uniform(0, 14)
        event_time = days_ago(14) + timedelta(days=days_offset)

        assignments_to_add.append(
            Assignment(
                experiment_id=exp.id,
                variant_id=green_btn.id,
                user_id=user_id,
                created_at=event_time,
            )
        )
        events_to_add.append(
            Event(
                event_type="experiment_exposure",
                event_name="experiment_exposure",
                user_id=user_id,
                experiment_id=exp.id,
                variant_id=green_btn.id,
                value=1.0,
                created_at=event_time.isoformat(),
            )
        )
        converts = rng.random() < GREEN_CVR
        prior = _checkout_history_event(
            history_rng, "green_button", user_id, event_time, converts
        )
        if prior is not None:
            events_to_add.append(prior)
            history += 1
        if converts:
            conv_time = event_time + timedelta(minutes=rng.randint(1, 30))
            events_to_add.append(
                Event(
                    event_type="checkout_completed",
                    event_name="checkout_completed",
                    user_id=user_id,
                    experiment_id=exp.id,
                    variant_id=green_btn.id,
                    value=1.0,
                    created_at=conv_time.isoformat(),
                )
            )

    _bulk_insert(db, assignments_to_add, batch_size=1000)
    _bulk_insert(db, events_to_add, batch_size=1000)
    db.commit()
    print(
        f"    Created {len(assignments_to_add):,} assignments and {len(events_to_add):,} events"
        f" ({history:,} of them pre-experiment history)."
    )
    return exp


# ---------------------------------------------------------------------------
# Experiment 3: Recommendation Algorithm MAB (ACTIVE — Thompson Sampling)
# ---------------------------------------------------------------------------


def seed_recommendation_mab_experiment(db, admin_user) -> Experiment:
    """20K events, algo_v2 pulling ahead. Status: ACTIVE (Thompson Sampling MAB)."""
    print("  Seeding experiment: Recommendation Algorithm MAB...")

    exp_name = "Recommendation Algorithm MAB"
    existing = db.query(Experiment).filter(Experiment.name == exp_name).first()
    if existing:
        print(f"    Already exists (id={existing.id}), skipping.")
        return existing

    exp = Experiment(
        name=exp_name,
        key=DEMO_EXPERIMENT_KEYS[exp_name],
        description="Multi-armed bandit to find best recommendation algorithm.",
        hypothesis="Thompson Sampling will route traffic to best-performing algorithm.",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.BANDIT,
        owner_id=admin_user.id,
        start_date=days_ago(7),
        end_date=days_ago(-60),
        targeting_rules={},
        optimization_type="thompson_sampling",
        metrics={"primary_metric": "item_clicked", "metric_type": "conversion"},
    )
    db.add(exp)
    db.flush()

    v1 = Variant(
        experiment_id=exp.id,
        name="algo_v1",
        description="Collaborative filtering baseline",
        is_control=True,
        traffic_allocation=33,
        configuration={"algorithm": "collaborative_filtering", "version": 1},
    )
    v2 = Variant(
        experiment_id=exp.id,
        name="algo_v2",
        description="Neural network recommendations",
        is_control=False,
        traffic_allocation=33,
        configuration={"algorithm": "neural_network", "version": 2},
    )
    v3 = Variant(
        experiment_id=exp.id,
        name="algo_v3",
        description="Hybrid content+collaborative",
        is_control=False,
        traffic_allocation=34,
        configuration={"algorithm": "hybrid", "version": 3},
    )
    db.add_all([v1, v2, v3])
    db.flush()

    metric = Metric(
        experiment_id=exp.id,
        name="Item Click Rate",
        event_name="item_clicked",
        metric_type=MetricType.CONVERSION,
        is_primary=True,
        minimum_sample_size=2000,
        expected_effect=0.40,
    )
    db.add(metric)
    db.flush()

    # Historical events: 20K total, v2 ahead
    # v1: 5%, v2: 9%, v3: 6% conversion
    PER_VARIANT = {
        "algo_v1": (v1, 0.05, 6667),
        "algo_v2": (v2, 0.09, 6667),
        "algo_v3": (v3, 0.06, 6666),
    }

    print("    Generating ~20,000 events for recommendation_algorithm MAB...")
    events_to_add = []
    assignments_to_add = []
    rng = random.Random(456)

    for vname, (variant, cvr, count) in PER_VARIANT.items():
        for i in range(count):
            user_id = f"user_rec_{vname}_{i:06d}"
            days_offset = rng.uniform(0, 7)
            event_time = days_ago(7) + timedelta(days=days_offset)

            assignments_to_add.append(
                Assignment(
                    experiment_id=exp.id,
                    variant_id=variant.id,
                    user_id=user_id,
                    created_at=event_time,
                )
            )
            events_to_add.append(
                Event(
                    event_type="experiment_exposure",
                    event_name="experiment_exposure",
                    user_id=user_id,
                    experiment_id=exp.id,
                    variant_id=variant.id,
                    value=1.0,
                    created_at=event_time.isoformat(),
                )
            )
            if rng.random() < cvr:
                conv_time = event_time + timedelta(seconds=rng.randint(5, 300))
                events_to_add.append(
                    Event(
                        event_type="item_clicked",
                        event_name="item_clicked",
                        user_id=user_id,
                        experiment_id=exp.id,
                        variant_id=variant.id,
                        value=1.0,
                        created_at=conv_time.isoformat(),
                    )
                )

    _bulk_insert(db, assignments_to_add, batch_size=1000)
    _bulk_insert(db, events_to_add, batch_size=1000)
    db.commit()
    print(
        f"    Created {len(assignments_to_add):,} assignments and {len(events_to_add):,} events."
    )
    return exp


# ---------------------------------------------------------------------------
# Feature Flags
# ---------------------------------------------------------------------------

# Safety thresholds in the shape the safety check reads (``MetricThreshold``,
# backend/app/schemas/safety.py): an error rate above 5% is critical. Both
# new_dashboard_ui's safety config and the global ``default_metrics`` use it.
SAFETY_METRICS = {
    "error_rate": {
        "critical_threshold": 0.05,
        "comparison_type": "greater_than",
    }
}

# What this seed wrote before #1205. ``MetricThreshold`` reads none of these
# keys, so the check saw no threshold at all. A re-run replaces exactly this
# value and leaves any other alone.
UNREAD_SAFETY_METRICS = {
    "error_rate": {
        "threshold": 0.05,
        "window_minutes": 10,
        "comparison": "greater_than",
    }
}


def replace_unread_safety_metrics(row, column: str) -> bool:
    """Set ``row.<column>`` to ``SAFETY_METRICS`` if it holds the unread keys.

    Returns True when it changed the row. A row with any other value,
    including one changed on the dashboard, is left as it is.
    """
    if row is None or getattr(row, column) != UNREAD_SAFETY_METRICS:
        return False
    setattr(row, column, copy.deepcopy(SAFETY_METRICS))
    return True


def seed_feature_flags(db, admin_user) -> dict:
    """Seed new_dashboard_ui and beta_features feature flags."""
    print("  Seeding feature flags...")
    flags = {}

    # --- new_dashboard_ui ---
    key1 = "new_dashboard_ui"
    existing1 = db.query(FeatureFlag).filter(FeatureFlag.key == key1).first()
    if existing1:
        flags[key1] = existing1
        config1 = (
            db.query(FeatureFlagSafetyConfig)
            .filter(FeatureFlagSafetyConfig.feature_flag_id == existing1.id)
            .first()
        )
        if replace_unread_safety_metrics(config1, "metrics"):
            print(
                f"    '{key1}' already exists; rewrote its safety thresholds "
                "in the shape the safety check reads."
            )
        else:
            print(f"    '{key1}' already exists, skipping.")
    else:
        flag1 = FeatureFlag(
            key=key1,
            name="New Dashboard UI",
            description="Gradual rollout of the redesigned analytics dashboard.",
            status=FeatureFlagStatus.ACTIVE,
            owner_id=admin_user.id,
            rollout_percentage=50,
            # No rules: the global rollout decides.
            targeting_rules={},
            tags=["ui", "dashboard", "redesign"],
        )
        db.add(flag1)
        db.flush()
        flags[key1] = flag1

        # Rollout schedule with 3 stages (already at stage 3)
        schedule = RolloutSchedule(
            feature_flag_id=flag1.id,
            name="Dashboard UI Gradual Rollout",
            description="Staged rollout: 10% → 25% → 50%",
            status=RolloutScheduleStatus.ACTIVE,
            start_date=days_ago(30),
            end_date=days_ago(-30),
            max_percentage=100,
            min_stage_duration=24,
            owner_id=admin_user.id,
        )
        db.add(schedule)
        db.flush()

        stages = [
            RolloutStage(
                rollout_schedule_id=schedule.id,
                name="Initial 10%",
                description="First 10% of users",
                stage_order=1,
                target_percentage=10,
                trigger_type=TriggerType.TIME_BASED,
                start_date=days_ago(30),
                status=RolloutStageStatus.COMPLETED,
            ),
            RolloutStage(
                rollout_schedule_id=schedule.id,
                name="Expand to 25%",
                description="Expand to quarter of users",
                stage_order=2,
                target_percentage=25,
                trigger_type=TriggerType.TIME_BASED,
                start_date=days_ago(20),
                status=RolloutStageStatus.COMPLETED,
            ),
            RolloutStage(
                rollout_schedule_id=schedule.id,
                name="Expand to 50%",
                description="Half of all users",
                stage_order=3,
                target_percentage=50,
                trigger_type=TriggerType.TIME_BASED,
                start_date=days_ago(10),
                status=RolloutStageStatus.IN_PROGRESS,
            ),
        ]
        db.add_all(stages)
        db.flush()

        # Safety config: auto-rollback if error_rate > 5%
        safety_cfg = FeatureFlagSafetyConfig(
            feature_flag_id=flag1.id,
            enabled=True,
            metrics=copy.deepcopy(SAFETY_METRICS),
            rollback_percentage=0,
        )
        db.add(safety_cfg)
        print(
            f"    Created feature flag '{key1}' with rollout schedule + safety config."
        )

    # --- beta_features ---
    key2 = "beta_features"
    existing2 = db.query(FeatureFlag).filter(FeatureFlag.key == key2).first()
    if existing2:
        flags[key2] = existing2
        print(f"    '{key2}' already exists, skipping.")
    else:
        flag2 = FeatureFlag(
            key=key2,
            name="Beta Features",
            description="Advanced targeting for beta users and internal team.",
            status=FeatureFlagStatus.ACTIVE,
            owner_id=admin_user.id,
            rollout_percentage=100,
            # Dashboard-editor shape (what the platform UI reads and writes):
            # user.role in [beta, internal]  OR  user.email ends with @acme.com.
            # SDK contexts {"role": "beta"} / {"email": "x@acme.com"} match via
            # the user.<key> alias (see backend/app/core/targeting_adapter.py).
            targeting_rules={
                "logical_operator": "OR",
                "groups": [
                    {
                        "logical_operator": "AND",
                        "conditions": [
                            {
                                "attribute": "user.role",
                                "operator": "in",
                                "value": ["beta", "internal"],
                            }
                        ],
                    },
                    {
                        "logical_operator": "AND",
                        "conditions": [
                            {
                                "attribute": "user.email",
                                "operator": "ends_with",
                                "value": "@acme.com",
                            }
                        ],
                    },
                ],
            },
            tags=["beta", "internal", "advanced"],
        )
        db.add(flag2)
        db.flush()
        flags[key2] = flag2
        print(f"    Created feature flag '{key2}' with advanced targeting rules.")

    db.commit()
    return flags


# ---------------------------------------------------------------------------
# Audit logs
# ---------------------------------------------------------------------------


def seed_audit_logs(db, users: dict, flags: dict, experiments: list):
    """Seed an audit trail in the form the platform writes it (#221).

    Each entry goes through ``seed_audit.seeded_entry`` (the platform's own
    writer and value helpers), dated in the past. Only actions the platform
    writes are seeded, and never a safety rollback.
    """
    print("  Seeding audit log entries...")

    admin = users.get("admin@demo.com")
    dev = users.get("dev@demo.com")
    if not admin or not dev:
        return

    # Check if we already have demo audit logs
    existing_count = db.query(AuditLog).filter(AuditLog.user_id == admin.id).count()
    if existing_count > 5:
        print("    Audit logs already exist, skipping.")
        return

    log_entries = []

    def add(at, **entry):
        log_entries.append(seeded_entry(db, at=at, **entry))

    # Sign-ins with a password
    for i, user in enumerate(users.values()):
        for day in [44, 30, 14, 7, 3, 1]:
            add(
                days_ago(day) + timedelta(hours=i),
                actor=user,
                action=ActionType.USER_LOGIN,
                entity_type=EntityType.USER,
                entity_id=user.id,
                entity_name=user.username or str(user.id),
                after={"provider": "local"},
            )

    # One experiment's lifecycle: created, its description changed, started,
    # paused, completed.
    if experiments:
        exp = experiments[0]
        E = EntityType.EXPERIMENT
        status = ExperimentStatus

        def lifecycle(action, day, before=None, after=None):
            add(
                days_ago(day),
                actor=admin,
                action=action,
                entity_type=E,
                entity_id=exp.id,
                entity_name=exp.name,
                before=before,
                after=after,
            )

        lifecycle(
            ActionType.EXPERIMENT_CREATE,
            45,
            after=seed_audit.identity(E, exp, status=status.DRAFT),
        )
        for action, day, old, new in [
            (ActionType.EXPERIMENT_UPDATE, 44, {"description": "(draft)"}, {}),
            (
                ActionType.EXPERIMENT_START,
                44,
                {"status": status.DRAFT},
                {"status": status.ACTIVE},
            ),
            (
                ActionType.EXPERIMENT_PAUSE,
                25,
                {"status": status.ACTIVE},
                {"status": status.PAUSED},
            ),
            (
                ActionType.EXPERIMENT_COMPLETE,
                10,
                {"status": status.PAUSED},
                {"status": status.COMPLETED},
            ),
        ]:
            before, after = seed_audit.changes(E, exp, old, new)
            lifecycle(action, day, before, after)

    # Feature flags: created by the developer; the ones that are on now were
    # turned on by the admin.
    for flag in flags.values():
        add(
            days_ago(30),
            actor=dev,
            action=ActionType.FEATURE_FLAG_CREATE,
            entity_type=EntityType.FEATURE_FLAG,
            entity_id=flag.id,
            entity_name=flag.name,
            after=seed_audit.identity(
                EntityType.FEATURE_FLAG, flag, status=FeatureFlagStatus.INACTIVE
            ),
        )
        if getattr(flag.status, "value", flag.status) == FeatureFlagStatus.ACTIVE.value:
            add(
                days_ago(29),
                actor=admin,
                action=ActionType.TOGGLE_ENABLE,
                entity_type=EntityType.FEATURE_FLAG,
                entity_id=flag.id,
                entity_name=flag.name,
                before=FeatureFlagStatus.INACTIVE.value,
                after=FeatureFlagStatus.ACTIVE.value,
            )

    # The developer's role, given by the admin.
    add(
        days_ago(60),
        actor=admin,
        action=ActionType.ROLE_ASSIGN,
        entity_type=EntityType.USER,
        entity_id=dev.id,
        entity_name=dev.username or str(dev.id),
        before={"role": UserRole.VIEWER.name, "is_superuser": False},
        after=role_value(dev),
    )

    db.flush()
    db.commit()
    print(f"    Created {len(log_entries)} audit log entries.")


# ---------------------------------------------------------------------------
# Safety Settings (global)
# ---------------------------------------------------------------------------


def seed_safety_settings(db):
    """Create global safety settings if not present."""
    print("  Seeding safety settings...")

    existing = db.query(SafetySettings).first()
    if existing:
        if replace_unread_safety_metrics(existing, "default_metrics"):
            db.commit()
            print(
                "    Safety settings already exist; rewrote their default "
                "thresholds in the shape the safety check reads."
            )
        else:
            print("    Safety settings already exist, skipping.")
        return

    settings_obj = SafetySettings(
        enable_automatic_rollbacks=True,
        default_metrics=copy.deepcopy(SAFETY_METRICS),
    )
    db.add(settings_obj)
    db.commit()
    print("    Created global safety settings.")


# ---------------------------------------------------------------------------
# Bulk insert helper
# ---------------------------------------------------------------------------


def _bulk_insert(db, objects, batch_size=500):
    """Insert objects in batches to avoid memory issues."""
    for i in range(0, len(objects), batch_size):
        batch = objects[i : i + batch_size]
        db.add_all(batch)
        db.flush()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    require_development_environment("demo")
    parser = argparse.ArgumentParser(
        description="Seed demo data for Controlvar platform"
    )
    parser.add_argument(
        "--api-url", default=None, help="API URL (unused, kept for AWS script compat)"
    )
    parser.parse_args()

    print("\n" + "=" * 60)
    print("  Controlvar Demo Data Seeder")
    print("=" * 60)

    # Ensure schema and tables exist
    with SessionLocal() as db:
        ensure_schema(db)

    ensure_tables()

    with SessionLocal() as db:
        print("\n[1/5] Users")
        users = seed_users(db)
        admin_user = users["admin@demo.com"]

        print("\n[2/5] Experiments")
        exp1 = seed_homepage_hero_experiment(db, admin_user)
        exp2 = seed_checkout_button_experiment(db, admin_user)
        exp3 = seed_recommendation_mab_experiment(db, admin_user)
        backfill_experiment_keys(db)

        print("\n[3/5] Feature Flags")
        flags = seed_feature_flags(db, admin_user)

        print("\n[4/5] Audit Logs")
        seed_audit_logs(db, users, flags, [exp1, exp2, exp3])

        print("\n[5/5] Safety Settings")
        seed_safety_settings(db)

    # Count final records
    with SessionLocal() as db:
        user_count = db.query(User).count()
        exp_count = db.query(Experiment).count()
        event_count = db.query(Event).count()
        assignment_count = db.query(Assignment).count()
        flag_count = db.query(FeatureFlag).count()

    print("\n" + "=" * 60)
    print("  Seeding Complete!")
    print("=" * 60)
    print(f"  Users:       {user_count}")
    print(f"  Experiments: {exp_count}")
    print(f"  Feature Flags:{flag_count}")
    print(f"  Assignments: {assignment_count:,}")
    print(f"  Events:      {event_count:,}")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
