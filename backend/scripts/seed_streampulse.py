#!/usr/bin/env python3
"""
StreamPulse demo seeder.

Creates the catalogue the StreamPulse phone-frame app (``demo/streampulse``)
and its device simulator (``demo/streampulse/simulator``) rely on:

- the demo users (same as ``seed_demo_data.py``; ``admin@demo.com`` owns everything)
- 4 ACTIVE feature flags with dashboard-shape targeting rules:
  ``streampulse_recs_v2`` (kill switch, safety config), ``streampulse_player_v2``
  (5% + ``employee`` rule, 4-stage rollout schedule, safety config that rolls
  back to 5%), ``streampulse_ai_search`` (iOS 17+/US/premium only) and
  ``streampulse_offline_mode``
- 5 ACTIVE A/B experiments with stable ``key`` values, variants and Metric rows
  (push frequency with sequential testing, Wrapped + profile badges inside the
  ``streampulse-profile`` mutual exclusion group, Bayesian onboarding steps and
  the upsell modal with an audit trail)
- the ``streampulse-profile`` mutual exclusion group and a 2% global holdout
  (``streampulse-holdout``; an already-active holdout is reused instead)
- automatic rollbacks switched on in the global safety settings
- the ``streampulse-app`` API key, written to ``demo/streampulse/.api_key`` and
  ``demo/streampulse/.env.local`` (both gitignored; ``STREAMPULSE_DEMO_DIR``
  redirects the folder). The app ships it to the browser, so it has no scope.
- the ``streampulse-server`` API key (scope ``sdk:ruleset``), written only to
  ``demo/streampulse/.api_key_server`` (gitignored), never to ``.env.local``.
  The device simulator sends crash reports with it, the way the StreamPulse
  server would forward them: only reports sent with a server-side key count
  toward automatic rollback.
- 7 days of history (assignments, exposure and conversion events) per
  experiment using the same true rates as the live simulator

This script is IDEMPOTENT: a second run is a no-op.  ``--reset`` removes
everything it created (the global safety-settings row is shared with the rest
of the platform and is left as it is).

Usage:
    source venv/bin/activate
    python backend/scripts/seed_streampulse.py [--no-history] [--history-users N]
    python backend/scripts/seed_streampulse.py --reset
"""

import argparse
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
from backend.app.core.security import hash_api_key
from backend.app.db.session import SessionLocal
from backend.app.models.api_key import APIKey, generate_api_key
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
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.metrics.metric import (
    AggregatedMetric,
    ErrorLog,
    RawMetric,
)
from backend.app.models.mutual_exclusion_group import (
    MutualExclusionGroup,
    MutualExclusionGroupStatus,
)
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)
from backend.app.models.safety import (
    FeatureFlagSafetyConfig,
    SafetyRollbackRecord,
    SafetySettings,
)
from backend.app.schemas.bayesian import BayesianConfig
from backend.app.schemas.experiment import SequentialTestingConfigInput
from backend.app.services.global_holdout_service import GlobalHoldoutService
from backend.scripts import seed_audit
from backend.scripts.seed_audit import seeded_entry
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
API_KEY_NAME = "streampulse-app"
ENV_KEY_NAME = "NEXT_PUBLIC_CONTROLVAR_API_KEY"
#: The line an .env.local written before the rename holds; replaced, not kept.
FORMER_ENV_KEY_NAME = "NEXT_PUBLIC_EXPERIMENTLY_API_KEY"
#: The server-side key the simulator reports crashes with. It carries
#: ``sdk:ruleset`` (its reports count toward automatic rollback, and it can
#: download every flag's targeting rules), so it is kept out of ``.env.local``
#: and the browser bundle.
SERVER_KEY_NAME = "streampulse-server"
SERVER_KEY_SCOPES = "sdk:ruleset"

# STREAMPULSE_DEMO_DIR lets CI / verification runs redirect the generated key
# files away from the checked-in demo app directory.
STREAMPULSE_DIR = Path(
    os.environ.get("STREAMPULSE_DEMO_DIR") or PROJECT_ROOT / "demo" / "streampulse"
)
API_KEY_FILE = STREAMPULSE_DIR / ".api_key"
SERVER_KEY_FILE = STREAMPULSE_DIR / ".api_key_server"
ENV_LOCAL_FILE = STREAMPULSE_DIR / ".env.local"

HISTORY_DAYS = 7
DEFAULT_HISTORY_USERS = 3000
HISTORY_USER_PREFIX = "streampulse_hist"

MEG_NAME = "streampulse-profile"
HOLDOUT_NAME = "streampulse-holdout"
#: The demo holdout's salt, fixed so the device ids the demo documents stay
#: where they are across re-seeds: ``sp-holdout-15`` is held out (bucket 1,
#: the same bucket it has under the legacy salt) and every preset in
#: ``demo/streampulse/src/lib/devices.ts`` is not.  Chosen by search for that
#: property; ``test_seed_streampulse_holdout.py`` pins it.
DEMO_HOLDOUT_SALT = "holdout:streampulse-demo-22"
HOLDOUT_PERCENTAGE = 2

SUBSCRIPTION_PRICE = 9.99

# Public keys (the app and the simulator use exactly these).
RECS_FLAG = "streampulse_recs_v2"
PLAYER_FLAG = "streampulse_player_v2"
AI_SEARCH_FLAG = "streampulse_ai_search"
OFFLINE_FLAG = "streampulse_offline_mode"
FLAG_KEYS = [RECS_FLAG, PLAYER_FLAG, AI_SEARCH_FLAG, OFFLINE_FLAG]

PUSH_KEY = "streampulse_push_frequency"
WRAPPED_KEY = "streampulse_wrapped"
BADGES_KEY = "streampulse_profile_badges"
ONBOARDING_KEY = "streampulse_onboarding_steps"
UPSELL_KEY = "streampulse_upsell_modal"

# True rates (spec §2). The live simulator uses the same numbers.
RATES: Dict[str, Dict[str, Any]] = {
    PUSH_KEY: {
        "notification_open": {"daily": 0.22, "three_weekly": 0.27},
        "app_uninstall": {"daily": 0.018, "three_weekly": 0.011},
    },
    WRAPPED_KEY: {
        "wrapped_view": {"control": 0.0, "wrapped_2026": 0.41},
        # share of *viewers*
        "share": {"control": 0.06, "wrapped_2026": 0.12},
    },
    BADGES_KEY: {
        "badge_tap": {"control": 0.03, "badges": 0.14},
    },
    ONBOARDING_KEY: {
        "onboarding_complete": {"five_step": 0.58, "three_step": 0.71},
        # marginal rate per user (first_play only happens after completion)
        "first_play": {"five_step": 0.45, "three_step": 0.56},
    },
    UPSELL_KEY: {
        "subscribe": {"control": 0.024, "value_modal": 0.031},
    },
}

# Device mix (spec §2) used for the history assignment context.
OS_MIX = (("iOS", 0.55), ("Android", 0.45))
IOS_VERSIONS = (("16.7.0", 0.20), ("17.4.0", 0.60), ("18.1.0", 0.20))
ANDROID_VERSIONS = (("12.0.0", 0.25), ("13.0.0", 0.35), ("14.0.0", 0.40))
REGION_MIX = (("US", 0.45), ("GB", 0.15), ("DE", 0.12), ("IN", 0.18), ("BR", 0.10))
APP_VERSION_MIX = (("3.1.0", 0.30), ("3.2.0", 0.50), ("3.2.1", 0.20))
PREMIUM_SHARE = 0.30
EMPLOYEE_SHARE = 0.01
IOS_MODELS = ("iPhone 13", "iPhone 14", "iPhone 15", "iPhone 15 Pro", "iPhone SE")
ANDROID_MODELS = ("Pixel 7", "Pixel 8", "Galaxy S10", "Galaxy S23", "OnePlus 11")

SAFETY_METRICS = {
    "error_rate": {
        "warning_threshold": 0.02,
        "critical_threshold": 0.05,
        "comparison_type": "greater_than",
    }
}

# ---------------------------------------------------------------------------
# Catalogue (spec §2)
# ---------------------------------------------------------------------------

# A metric's expected_effect is a RELATIVE lift: 0.25 means 12% -> 15%. The
# primary metric's is the effect the results page plans the sample for, so
# backend/tests/unit/scripts/test_seed_planned_effects.py pins each plan.
EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "key": PUSH_KEY,
        "name": "StreamPulse: Push notification frequency",
        "description": "Daily push digest vs three pushes a week on the Notifications screen.",
        "hypothesis": "Fewer, better-timed pushes raise open rate and lower the uninstall guardrail.",
        "sequential": True,
        "bayesian": False,
        "meg": False,
        "variants": [
            {
                "name": "daily",
                "is_control": True,
                "traffic_allocation": 50,
                "description": "One push per day",
                "configuration": {"frequency": "daily"},
            },
            {
                "name": "three_weekly",
                "is_control": False,
                "traffic_allocation": 50,
                "description": "Three pushes per week",
                "configuration": {"frequency": "3x_week"},
            },
        ],
        "metrics": [
            {
                "name": "Notification open",
                "event_name": "notification_open",
                "metric_type": MetricType.CONVERSION,
                "is_primary": True,
                "minimum_sample_size": 2000,
                "expected_effect": 0.23,
            },
            {
                "name": "App uninstall (guardrail)",
                "event_name": "app_uninstall",
                "metric_type": MetricType.CONVERSION,
                "is_primary": False,
                "minimum_sample_size": 2000,
                "expected_effect": None,
                "lower_is_better": True,
            },
        ],
    },
    {
        "key": WRAPPED_KEY,
        "name": "StreamPulse: Your 2026 Wrapped",
        "description": "Personal year-in-review card on the Profile screen (mutually exclusive with profile badges).",
        "hypothesis": "A Wrapped card drives views and social shares from the Profile screen.",
        "sequential": False,
        "bayesian": False,
        "meg": True,
        "variants": [
            {
                "name": "control",
                "is_control": True,
                "traffic_allocation": 50,
                "description": "No Wrapped card",
                "configuration": {"wrapped": False},
            },
            {
                "name": "wrapped_2026",
                "is_control": False,
                "traffic_allocation": 50,
                "description": "Your 2026 Wrapped card",
                "configuration": {"wrapped": True},
            },
        ],
        "metrics": [
            {
                "name": "Wrapped view",
                "event_name": "wrapped_view",
                "metric_type": MetricType.CONVERSION,
                "is_primary": True,
                "minimum_sample_size": 1000,
                "expected_effect": 0.10,
            },
            {
                "name": "Share",
                "event_name": "share",
                "metric_type": MetricType.CONVERSION,
                "is_primary": False,
                "minimum_sample_size": 1000,
                "expected_effect": 0.33,
            },
        ],
    },
    {
        "key": BADGES_KEY,
        "name": "StreamPulse: Profile badges",
        "description": "Listening-milestone badges on the Profile screen (mutually exclusive with Wrapped).",
        "hypothesis": "Badges give users a reason to tap around their profile.",
        "sequential": False,
        "bayesian": False,
        "meg": True,
        "variants": [
            {
                "name": "control",
                "is_control": True,
                "traffic_allocation": 50,
                "description": "No badge row",
                "configuration": {"badges": False},
            },
            {
                "name": "badges",
                "is_control": False,
                "traffic_allocation": 50,
                "description": "Badge row under the avatar",
                "configuration": {"badges": True},
            },
        ],
        "metrics": [
            {
                "name": "Badge tap",
                "event_name": "badge_tap",
                "metric_type": MetricType.CONVERSION,
                "is_primary": True,
                "minimum_sample_size": 1000,
                "expected_effect": 0.50,
            },
        ],
    },
    {
        "key": ONBOARDING_KEY,
        "name": "StreamPulse: Onboarding — 5 steps vs 3 steps",
        "description": "Shorter onboarding stepper; Bayesian decisioning stops early on a clear winner.",
        "hypothesis": "A 3-step onboarding completes more often and gets users to a first play sooner.",
        "sequential": False,
        "bayesian": True,
        "meg": False,
        "variants": [
            {
                "name": "five_step",
                "is_control": True,
                "traffic_allocation": 50,
                "description": "5-step onboarding",
                "configuration": {"steps": 5},
            },
            {
                "name": "three_step",
                "is_control": False,
                "traffic_allocation": 50,
                "description": "3-step onboarding",
                "configuration": {"steps": 3},
            },
        ],
        "metrics": [
            {
                "name": "Onboarding complete",
                "event_name": "onboarding_complete",
                "metric_type": MetricType.CONVERSION,
                "is_primary": True,
                "minimum_sample_size": 1500,
                "expected_effect": 0.09,
            },
            {
                "name": "First play",
                "event_name": "first_play",
                "metric_type": MetricType.CONVERSION,
                "is_primary": False,
                "minimum_sample_size": 1500,
                "expected_effect": 0.11,
            },
        ],
    },
    {
        "key": UPSELL_KEY,
        "name": "StreamPulse: Premium upsell modal",
        "description": "Classic upsell modal vs a value-led modal on the Payments screen (full audit trail).",
        "hypothesis": "Leading with value (offline, no ads, hi-fi) converts more free users to premium.",
        "sequential": False,
        "bayesian": False,
        "meg": False,
        "variants": [
            {
                "name": "control",
                "is_control": True,
                "traffic_allocation": 50,
                "description": "Classic upsell modal",
                "configuration": {"modal": "classic"},
            },
            {
                "name": "value_modal",
                "is_control": False,
                "traffic_allocation": 50,
                "description": "Value-led upsell modal",
                "configuration": {"modal": "value"},
            },
        ],
        "metrics": [
            {
                "name": "Subscribe",
                "event_name": "subscribe",
                "metric_type": MetricType.CONVERSION,
                "is_primary": True,
                "minimum_sample_size": 5000,
                "expected_effect": 0.25,
            },
        ],
    },
]

EXPERIMENT_KEYS = [spec["key"] for spec in EXPERIMENTS]

# Dashboard-shape targeting rules (what the platform's targeting editor writes),
# so the rules round-trip through the UI.  Attribute names are the device
# attributes the app/simulator send as ``context``.
PLAYER_V2_RULES: Dict[str, Any] = {
    "logical_operator": "OR",
    "groups": [
        {
            "id": "grp-employees",
            "logical_operator": "AND",
            "conditions": [
                {
                    "id": "cond-employee",
                    "attribute": "employee",
                    "operator": "equals",
                    "value": True,
                }
            ],
        }
    ],
}

AI_SEARCH_RULES: Dict[str, Any] = {
    "logical_operator": "AND",
    "groups": [
        {
            "id": "grp-ios17-us-premium",
            "logical_operator": "AND",
            "conditions": [
                {
                    "id": "cond-os",
                    "attribute": "os",
                    "operator": "equals",
                    "value": "iOS",
                },
                {
                    "id": "cond-os-version",
                    "attribute": "os_version",
                    "operator": "semver_gte",
                    "value": "17.0.0",
                },
                {
                    "id": "cond-region",
                    "attribute": "region",
                    "operator": "equals",
                    "value": "US",
                },
                {
                    "id": "cond-tier",
                    "attribute": "tier",
                    "operator": "equals",
                    "value": "premium",
                },
            ],
        }
    ],
}

FLAGS: List[Dict[str, Any]] = [
    {
        "key": RECS_FLAG,
        "name": "StreamPulse: Recommendations v2",
        "description": "Home feed: new recommendation algorithm. Kill switch — flip it off and every device falls back to the classic chronological feed.",
        "rollout_percentage": 100,
        "targeting_rules": {},
        "tags": ["home", "kill-switch", "streampulse"],
        "safety": {"rollback_percentage": 0},
        "schedule": False,
    },
    {
        "key": PLAYER_FLAG,
        "name": "StreamPulse: Player v2",
        "description": "Redesigned player. Employees always get it; everyone else follows the staged rollout (5% → 25% → 50% → 100%). Safety monitoring rolls back to 5% on an error-rate spike.",
        "rollout_percentage": 5,
        "targeting_rules": PLAYER_V2_RULES,
        "tags": ["player", "rollout", "safety", "streampulse"],
        "safety": {"rollback_percentage": 5},
        "schedule": True,
    },
    {
        "key": AI_SEARCH_FLAG,
        "name": "StreamPulse: AI search",
        "description": "GPU-backed AI search. Targeted only: iOS 17+, US, premium tier.",
        "rollout_percentage": 0,
        "targeting_rules": AI_SEARCH_RULES,
        "tags": ["search", "targeting"],
        "safety": None,
        "schedule": False,
    },
    {
        "key": OFFLINE_FLAG,
        "name": "StreamPulse: Offline downloads",
        "description": "Offline downloads. Demonstrates SDK caching: the app keeps serving the cached value when the platform is unreachable.",
        "rollout_percentage": 100,
        "targeting_rules": {},
        "tags": ["kill-switch"],
        "safety": None,
        "schedule": False,
    },
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _short(key: str) -> str:
    return key[len("streampulse_") :] if key.startswith("streampulse_") else key


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


def _weighted(rng: random.Random, table) -> Any:
    return rng.choices([v for v, _ in table], weights=[w for _, w in table], k=1)[0]


def _random_device(rng: random.Random, device_id: str) -> Dict[str, Any]:
    """Device attributes following the spec §2 mix (mirrors the simulator's population)."""
    os_name = _weighted(rng, OS_MIX)
    if os_name == "iOS":
        os_version = _weighted(rng, IOS_VERSIONS)
        model = rng.choice(IOS_MODELS)
    else:
        os_version = _weighted(rng, ANDROID_VERSIONS)
        model = rng.choice(ANDROID_MODELS)
    return {
        "device_id": device_id,
        "os": os_name,
        "os_version": os_version,
        "app_version": _weighted(rng, APP_VERSION_MIX),
        "region": _weighted(rng, REGION_MIX),
        "tier": "premium" if rng.random() < PREMIUM_SHARE else "free",
        "employee": rng.random() < EMPLOYEE_SHARE,
        "device_model": model,
    }


# ---------------------------------------------------------------------------
# Mutual exclusion group + global holdout
# ---------------------------------------------------------------------------


def seed_mutual_exclusion_group(db, admin_user) -> MutualExclusionGroup:
    """Create the ``streampulse-profile`` group (idempotent by name)."""
    print("  Seeding mutual exclusion group...")
    existing = (
        db.query(MutualExclusionGroup)
        .filter(MutualExclusionGroup.name == MEG_NAME)
        .first()
    )
    if existing:
        print(f"    '{MEG_NAME}' already exists (id={existing.id}), skipping.")
        return existing

    group = MutualExclusionGroup(
        name=MEG_NAME,
        description="Profile screen: a device sees either Wrapped or profile badges, never both.",
        traffic_allocation=1.0,
        status=MutualExclusionGroupStatus.ACTIVE,
        owner_id=admin_user.id,
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    print(f"    Created '{MEG_NAME}' (traffic_allocation 1.0, active).")
    return group


def seed_global_holdout(db, admin_user) -> GlobalHoldout:
    """
    Ensure an active global holdout exists, through ``GlobalHoldoutService``.

    Only one holdout may be active at a time, so an already-active holdout
    (whatever its name) is reused and reported instead of creating a second one.
    For each state of ``streampulse-holdout`` (#445):

    * active but not measurable (active since before membership was
      recorded): reused, with a note that ``--reset`` gives a measurable one;
    * ended (deactivated): it cannot restart, so nothing is activated and the
      seed carries on without an active holdout;
    * inactive and never ended: activated through the service;
    * absent: created and activated through the service.

    The percentage of a holdout that has been active is never changed, and no
    ``holdout_population`` row is written: membership is recorded only from
    real ``/tracking/assign`` calls, so results need a warm-up.
    """
    print("  Seeding global holdout...")
    service = GlobalHoldoutService(db)
    active = service.get_active_holdout()
    if active is not None:
        if active.name == HOLDOUT_NAME:
            print(
                f"    '{HOLDOUT_NAME}' already active at {active.holdout_percentage}%, skipping."
            )
        else:
            print(
                f"    REUSING the existing active holdout '{active.name}' "
                f"({active.holdout_percentage}%); '{HOLDOUT_NAME}' was not created "
                "because only one holdout can be active."
            )
        if not active.is_measurable:
            print(
                f"    NOTE: '{active.name}' has been active since before holdout "
                "membership was recorded, so it cannot be measured. "
                "Run with --reset for a holdout that can."
            )
        return active

    holdout = db.query(GlobalHoldout).filter(GlobalHoldout.name == HOLDOUT_NAME).first()
    if holdout is not None and holdout.deactivated_at is not None:
        print(
            f"    '{HOLDOUT_NAME}' has ended and cannot restart; no holdout is "
            "active. Run with --reset to create a new one."
        )
        return holdout
    if holdout is not None:
        never_active = holdout.activated_at is None
        holdout = service.update_holdout(
            holdout.id,
            holdout_percentage=HOLDOUT_PERCENTAGE if never_active else None,
            is_active=True,
        )
        print(f"    Activated '{HOLDOUT_NAME}' at {holdout.holdout_percentage}%.")
        return holdout

    holdout = service.create_holdout(
        name=HOLDOUT_NAME,
        description="2% of devices never enter any StreamPulse experiment (clean baseline).",
        holdout_percentage=HOLDOUT_PERCENTAGE,
        is_active=True,
        owner_id=admin_user.id,
        hash_salt=DEMO_HOLDOUT_SALT,
    )
    print(f"    Created '{HOLDOUT_NAME}' at {HOLDOUT_PERCENTAGE}% (active).")
    return holdout


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------


def seed_experiments(
    db, admin_user, group: MutualExclusionGroup
) -> Dict[str, Experiment]:
    """Create the five StreamPulse experiments (idempotent by ``key``)."""
    print("  Seeding StreamPulse experiments...")
    experiments: Dict[str, Experiment] = {}

    for spec in EXPERIMENTS:
        existing = db.query(Experiment).filter(Experiment.key == spec["key"]).first()
        if existing:
            if spec["meg"] and existing.mutual_exclusion_group_id != group.id:
                existing.mutual_exclusion_group_id = group.id
                db.commit()
                print(
                    f"    '{spec['key']}' already exists; attached it to '{MEG_NAME}'."
                )
            else:
                print(
                    f"    '{spec['key']}' already exists (id={existing.id}), skipping."
                )
            experiments[spec["key"]] = existing
            continue

        exp = Experiment(
            key=spec["key"],
            name=spec["name"],
            description=spec["description"],
            hypothesis=spec["hypothesis"],
            status=ExperimentStatus.ACTIVE,
            experiment_type=ExperimentType.A_B,
            optimization_type="fixed",
            owner_id=admin_user.id,
            start_date=days_ago(HISTORY_DAYS),
            end_date=days_ago(-30),
            targeting_rules={},
            metrics=_legacy_metrics_json(spec),
            tags=["streampulse", "demo", "mobile"],
            mutual_exclusion_group_id=group.id if spec["meg"] else None,
        )
        if spec["sequential"]:
            exp.sequential_testing_enabled = True
            exp.sequential_testing_method = "msprt"
            exp.sequential_testing_config = SequentialTestingConfigInput().model_dump()
        if spec["bayesian"]:
            exp.bayesian_enabled = True
            exp.bayesian_config = BayesianConfig(rope=[-0.01, 0.01]).model_dump(
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
        for m in spec["metrics"]:
            db.add(Metric(experiment_id=exp.id, **m))

        db.commit()
        db.refresh(exp)
        experiments[spec["key"]] = exp
        extras = []
        if spec["sequential"]:
            extras.append("sequential/msprt")
        if spec["bayesian"]:
            extras.append("bayesian")
        if spec["meg"]:
            extras.append(f"MEG {MEG_NAME}")
        print(
            f"    Created '{spec['key']}' (A/B, {len(spec['variants'])} variants"
            + (", " + ", ".join(extras) if extras else "")
            + ")."
        )

    return experiments


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------


def _seed_player_schedule(db, flag: FeatureFlag, admin_user) -> None:
    """The four-stage "Player v2 rollout" schedule (stage 1 in progress since 2 days ago)."""
    schedule = RolloutSchedule(
        feature_flag_id=flag.id,
        name="Player v2 rollout",
        description="Internal + 5% → 25% → 50% → 100%",
        status=RolloutScheduleStatus.ACTIVE,
        start_date=days_ago(2),
        end_date=days_ago(-30),
        max_percentage=100,
        min_stage_duration=1,
        owner_id=admin_user.id,
    )
    db.add(schedule)
    db.flush()

    stage_specs = [
        (
            "Internal + 5%",
            "Employees via targeting rule plus 5% of everyone",
            1,
            5,
            days_ago(2),
            RolloutStageStatus.IN_PROGRESS,
        ),
        (
            "25%",
            "A quarter of all devices",
            2,
            25,
            days_ago(-1),
            RolloutStageStatus.PENDING,
        ),
        ("50%", "Half of all devices", 3, 50, days_ago(-3), RolloutStageStatus.PENDING),
        ("100%", "Everyone", 4, 100, days_ago(-7), RolloutStageStatus.PENDING),
    ]
    for name, description, order, pct, start, status in stage_specs:
        stage = RolloutStage(
            rollout_schedule_id=schedule.id,
            name=name,
            description=description,
            stage_order=order,
            target_percentage=pct,
            trigger_type=TriggerType.TIME_BASED,
            start_date=start,
            status=status,
        )
        if status == RolloutStageStatus.IN_PROGRESS:
            # The stage went live two days ago (the scheduler reads updated_at
            # as the activation time).
            naive_start = start.replace(tzinfo=None)
            stage.created_at = naive_start
            stage.updated_at = naive_start
        db.add(stage)


def seed_feature_flags(db, admin_user) -> Dict[str, FeatureFlag]:
    """Create the four StreamPulse flags (idempotent by ``key``)."""
    print("  Seeding StreamPulse feature flags...")
    flags: Dict[str, FeatureFlag] = {}

    for spec in FLAGS:
        key = spec["key"]
        existing = db.query(FeatureFlag).filter(FeatureFlag.key == key).first()
        if existing:
            print(
                f"    '{key}' already exists (rollout {existing.rollout_percentage}%), skipping."
            )
            flags[key] = existing
            continue

        flag = FeatureFlag(
            key=key,
            name=spec["name"],
            description=spec["description"],
            status=FeatureFlagStatus.ACTIVE,
            owner_id=admin_user.id,
            rollout_percentage=spec["rollout_percentage"],
            targeting_rules=spec["targeting_rules"],
            tags=spec["tags"],
        )
        db.add(flag)
        db.flush()

        details = [f"{spec['rollout_percentage']}% rollout"]
        if spec["targeting_rules"]:
            details.append("dashboard targeting rules")
        if spec["schedule"]:
            _seed_player_schedule(db, flag, admin_user)
            details.append("4-stage schedule")
        if spec["safety"]:
            db.add(
                FeatureFlagSafetyConfig(
                    feature_flag_id=flag.id,
                    enabled=True,
                    metrics=SAFETY_METRICS,
                    rollback_percentage=spec["safety"]["rollback_percentage"],
                )
            )
            details.append(
                f"safety config (rollback to {spec['safety']['rollback_percentage']}%)"
            )
        db.commit()
        db.refresh(flag)
        flags[key] = flag
        print(f"    Created '{key}' ({', '.join(details)}).")

    return flags


def seed_safety_settings(db) -> SafetySettings:
    """Switch automatic rollbacks on globally so the safety scheduler can act."""
    print("  Seeding global safety settings...")
    settings_row = db.query(SafetySettings).first()
    if settings_row is None:
        settings_row = SafetySettings(
            enable_automatic_rollbacks=True,
            default_metrics=SAFETY_METRICS,
        )
        db.add(settings_row)
        db.commit()
        print("    Created safety settings with automatic rollbacks ENABLED.")
    elif not settings_row.enable_automatic_rollbacks:
        settings_row.enable_automatic_rollbacks = True
        db.commit()
        print("    Automatic rollbacks were off; ENABLED them.")
    else:
        print("    Automatic rollbacks already enabled, skipping.")
    return settings_row


# ---------------------------------------------------------------------------
# Audit trail (upsell modal experiment)
# ---------------------------------------------------------------------------


def seed_audit_logs(db, admin_user, experiment: Experiment) -> int:
    """Create, change and start entries for the upsell experiment (the compliance story).

    Written in the form the platform writes them (``seed_audit.seeded_entry``),
    dated before the seed ran. Safety rollbacks are never seeded: the rollout
    story produces a real one.
    """
    print("  Seeding audit trail for the upsell experiment...")
    existing = (
        db.query(func.count(AuditLog.id))
        .filter(
            AuditLog.entity_type == EntityType.EXPERIMENT.value,
            AuditLog.entity_id == experiment.id,
        )
        .scalar()
        or 0
    )
    if existing:
        print(
            f"    {existing} audit rows already exist for '{experiment.key}', skipping."
        )
        return 0

    E = EntityType.EXPERIMENT
    created_at = days_ago(HISTORY_DAYS + 1)
    entry = {
        "actor": admin_user,
        "entity_type": E,
        "entity_id": experiment.id,
        "entity_name": experiment.name,
    }
    changed_before, changed_after = seed_audit.changes(
        E, experiment, {"description": "(draft)"}
    )
    started_before, started_after = seed_audit.changes(
        E,
        experiment,
        {"status": ExperimentStatus.DRAFT},
        {"status": ExperimentStatus.ACTIVE},
    )
    rows = [
        seeded_entry(
            db,
            at=created_at,
            action=ActionType.EXPERIMENT_CREATE,
            after=seed_audit.identity(E, experiment, status=ExperimentStatus.DRAFT),
            **entry,
        ),
        seeded_entry(
            db,
            at=created_at + timedelta(hours=6),
            action=ActionType.EXPERIMENT_UPDATE,
            before=changed_before,
            after=changed_after,
            **entry,
        ),
        seeded_entry(
            db,
            at=days_ago(HISTORY_DAYS),
            action=ActionType.EXPERIMENT_START,
            before=started_before,
            after=started_after,
            **entry,
        ),
    ]
    db.flush()
    db.commit()
    print(f"    Created {len(rows)} audit rows (create / update / start).")
    return len(rows)


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------


def _ensure_key(
    db,
    admin_user,
    *,
    name: str,
    path: Path,
    scopes: str,
    description: str,
    plaintext: Optional[str] = None,
) -> tuple:
    """Ensure the key ``name`` exists and its plaintext is in ``path`` (0600).

    Keeps the existing row when ``path`` still holds the matching plaintext;
    otherwise the stale row is replaced by a fresh key (``plaintext`` when
    given). Returns ``(plaintext, created)``.
    """
    existing = (
        db.query(APIKey)
        .filter(APIKey.name == name, APIKey.user_id == admin_user.id)
        .first()
    )

    if existing is not None and path.exists():
        kept = path.read_text().strip()
        if kept and hash_api_key(kept) == existing.key:
            print(f"    Existing '{name}' key kept ({_display(path)}).")
            return kept, False
        print(f"    {_display(path)} does not match the stored hash; rotating.")

    if existing is not None:
        db.delete(existing)
        db.commit()
        print(f"    Removed stale '{name}' key row.")

    plaintext = plaintext or generate_api_key()
    db.add(
        APIKey(
            user_id=admin_user.id,
            key=hash_api_key(plaintext),
            name=name,
            description=description,
            scopes=scopes,
            is_active=True,
        )
    )
    db.commit()

    STREAMPULSE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(plaintext + "\n")
    os.chmod(path, 0o600)
    print(f"    Created '{name}' key, written to {_display(path)}.")
    return plaintext, True


def seed_api_key(db, admin_user) -> str:
    """
    Ensure the ``streampulse-app`` API key exists and is written to disk.

    The app's key: written to ``demo/streampulse/.api_key`` and, as
    ``NEXT_PUBLIC_CONTROLVAR_API_KEY``, to ``.env.local``, from where it goes
    into the browser bundle. It carries no scope. Keeps the existing row when
    ``.api_key`` still holds the matching plaintext; otherwise the stale row is
    replaced by a fresh key. Returns the plaintext key.
    """
    print("  Seeding StreamPulse API key...")
    plaintext, _created = _ensure_key(
        db,
        admin_user,
        name=API_KEY_NAME,
        path=API_KEY_FILE,
        scopes="read,write",
        description="StreamPulse demo app + device simulator",
        plaintext=os.environ.get("STREAMPULSE_API_KEY"),
    )
    _write_env_line(ENV_LOCAL_FILE, ENV_KEY_NAME, plaintext)
    return plaintext


def seed_server_api_key(db, admin_user) -> str:
    """
    Ensure the ``streampulse-server`` key exists and is written to disk.

    It carries ``sdk:ruleset`` and is owned by the demo administrator, so the
    error reports the simulator sends with it count toward automatic rollback.
    It is written only to ``demo/streampulse/.api_key_server`` (0600), never
    to ``.env.local``: a key with this scope can download every flag's
    targeting rules, so it stays out of the browser bundle. Returns the
    plaintext key.
    """
    print("  Seeding StreamPulse server key...")
    plaintext, _created = _ensure_key(
        db,
        admin_user,
        name=SERVER_KEY_NAME,
        path=SERVER_KEY_FILE,
        scopes=SERVER_KEY_SCOPES,
        description="StreamPulse server: forwards the devices' crash reports",
    )
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
    variant_config: Dict[str, Any],
    device: Dict[str, Any],
    experiment_id,
    variant_id,
    exposure_at: datetime,
    rng: random.Random,
) -> List[Event]:
    """Conversion events for one seeded device, following the §2 rates."""
    events: List[Event] = []
    clock = {"t": exposure_at}
    user_id = device["device_id"]

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
    if key == PUSH_KEY:
        frequency = variant_config.get("frequency", "daily")
        if rng.random() < rates["notification_open"][variant_name]:
            events.append(
                event(
                    "notification_open",
                    later(600, 86400),
                    metadata={"frequency": frequency},
                )
            )
        if rng.random() < rates["app_uninstall"][variant_name]:
            events.append(event("app_uninstall", later(3600, 172800)))
    elif key == WRAPPED_KEY:
        if rng.random() < rates["wrapped_view"][variant_name]:
            events.append(event("wrapped_view", later(5, 120)))
            if rng.random() < rates["share"][variant_name]:
                events.append(
                    event("share", later(5, 60), metadata={"surface": "wrapped"})
                )
    elif key == BADGES_KEY:
        if rng.random() < rates["badge_tap"][variant_name]:
            events.append(event("badge_tap", later(5, 120)))
    elif key == ONBOARDING_KEY:
        steps = variant_config.get("steps", 5)
        complete_rate = rates["onboarding_complete"][variant_name]
        if rng.random() < complete_rate:
            events.append(
                event("onboarding_complete", later(30, 300), metadata={"steps": steps})
            )
            # first_play is a marginal rate per user; it only happens after completion
            if rng.random() < rates["first_play"][variant_name] / complete_rate:
                events.append(event("first_play", later(10, 600)))
    elif key == UPSELL_KEY:
        if rng.random() < rates["subscribe"][variant_name]:
            events.append(
                event(
                    "subscribe",
                    later(20, 900),
                    value=SUBSCRIPTION_PRICE,
                    metadata={
                        "plan": "premium" if device["tier"] == "free" else "family",
                        "modal": variant_config.get("modal", "classic"),
                    },
                )
            )
    return events


def seed_history(
    db, experiments: Dict[str, Experiment], users_per_experiment: int
) -> Dict[str, Dict[str, int]]:
    """Seed ``HISTORY_DAYS`` of assignments + events per experiment (skips experiments with data)."""
    print(
        f"  Seeding {HISTORY_DAYS}-day history (~{users_per_experiment:,} devices per experiment)..."
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
        rng = random.Random(f"streampulse-history:{key}")
        assignments: List[Assignment] = []
        events: List[Event] = []

        for i in range(users_per_experiment):
            device = _random_device(rng, f"{HISTORY_USER_PREFIX}_{_short(key)}_{i:05d}")
            variant = _pick_variant(rng, variants)
            exposure_at = now - timedelta(days=rng.uniform(0.05, HISTORY_DAYS))
            assignments.append(
                Assignment(
                    experiment_id=exp.id,
                    variant_id=variant.id,
                    user_id=device["device_id"],
                    created_at=exposure_at,
                    context={"source": "seed_streampulse", **device},
                )
            )
            events.append(
                Event(
                    event_type="experiment_exposure",
                    event_name="experiment_exposure",
                    user_id=device["device_id"],
                    experiment_id=exp.id,
                    variant_id=variant.id,
                    value=1.0,
                    created_at=exposure_at.isoformat(),
                )
            )
            events.extend(
                _funnel_events(
                    key,
                    variant.name,
                    variant.configuration or {},
                    device,
                    exp.id,
                    variant.id,
                    exposure_at,
                    rng,
                )
            )

        _bulk_insert(db, assignments, batch_size=1000)
        _bulk_insert(db, events, batch_size=1000)
        db.commit()
        summary[key] = {"assignments": len(assignments), "events": len(events)}
        print(f"    '{key}': {len(assignments):,} assignments, {len(events):,} events.")

    return summary


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def reset_streampulse(db) -> Dict[str, int]:
    """Delete everything this script creates. Returns per-table delete counts."""
    counts: Dict[str, int] = {
        "experiments": 0,
        "assignments": 0,
        "events": 0,
        "audit_logs": 0,
        "feature_flags": 0,
        "rollback_records": 0,
        "mutual_exclusion_groups": 0,
        "global_holdouts": 0,
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
        counts["audit_logs"] += (
            db.query(AuditLog)
            .filter(
                AuditLog.entity_type == EntityType.EXPERIMENT.value,
                AuditLog.entity_id == exp.id,
            )
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
        counts["rollback_records"] += (
            db.query(SafetyRollbackRecord)
            .filter(SafetyRollbackRecord.feature_flag_id == flag.id)
            .delete(synchronize_session=False)
        )
        db.commit()
        db.expire_all()
        # ORM delete cascades to rollout schedules/stages, safety config, overrides
        db.delete(db.query(FeatureFlag).filter(FeatureFlag.id == flag.id).one())
        db.commit()
        counts["feature_flags"] += 1
        print(f"    Deleted feature flag '{flag.key}'.")

    group = (
        db.query(MutualExclusionGroup)
        .filter(MutualExclusionGroup.name == MEG_NAME)
        .first()
    )
    if group is not None:
        # Any experiment still pointing at the group (not ours) is detached first.
        db.query(Experiment).filter(
            Experiment.mutual_exclusion_group_id == group.id
        ).update(
            {Experiment.mutual_exclusion_group_id: None}, synchronize_session=False
        )
        db.delete(group)
        db.commit()
        counts["mutual_exclusion_groups"] = 1
        print(f"    Deleted mutual exclusion group '{MEG_NAME}'.")

    counts["global_holdouts"] = (
        db.query(GlobalHoldout)
        .filter(GlobalHoldout.name == HOLDOUT_NAME)
        .delete(synchronize_session=False)
    )
    if counts["global_holdouts"]:
        print(f"    Deleted global holdout '{HOLDOUT_NAME}'.")

    counts["api_keys"] = (
        db.query(APIKey)
        .filter(APIKey.name.in_((API_KEY_NAME, SERVER_KEY_NAME)))
        .delete(synchronize_session=False)
    )
    db.commit()

    for key_file in (API_KEY_FILE, SERVER_KEY_FILE):
        if key_file.exists():
            key_file.unlink()
            print(f"    Removed {_display(key_file)}.")
    if ENV_LOCAL_FILE.exists():
        _write_env_line(ENV_LOCAL_FILE, ENV_KEY_NAME, None)
        print(f"    Removed {ENV_KEY_NAME} from {_display(ENV_LOCAL_FILE)}.")

    return counts


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def print_summary(
    db,
    experiments: Dict[str, Experiment],
    flags: Dict[str, FeatureFlag],
    holdout: GlobalHoldout,
) -> None:
    print("\n" + "=" * 72)
    print("  StreamPulse Seeding Complete!")
    print("=" * 72)
    print(f"  {'experiment':<30}{'variants':>9}{'assign.':>10}{'events':>9}  extras")
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
        extras = []
        if exp.sequential_testing_enabled:
            extras.append("sequential")
        if exp.bayesian_enabled:
            extras.append("bayesian")
        if exp.mutual_exclusion_group_id:
            extras.append(f"MEG {MEG_NAME}")
        print(
            f"  {key:<30}{len(exp.variants):>9}{n_assign:>10,}{n_events:>9,}  {', '.join(extras)}"
        )
    print()
    for key, flag in flags.items():
        rules = "dashboard rules" if flag.targeting_rules else "no rules"
        schedule = ", 4-stage schedule" if flag.rollout_schedules else ""
        safety = (
            f", safety → {flag.safety_config.rollback_percentage}%"
            if flag.safety_config
            else ""
        )
        print(
            f"  flag {key:<28} {flag.status.value:<7} rollout {flag.rollout_percentage:>3}%  "
            f"{rules}{schedule}{safety}"
        )
    print()
    state = "active" if holdout.is_active else "ended"
    print(f"  Holdout:       {holdout.name} at {holdout.holdout_percentage}% ({state})")
    print(f"  API key file:  {_display(API_KEY_FILE)}  (X-API-Key)")
    print(f"  Env file:      {_display(ENV_LOCAL_FILE)}  ({ENV_KEY_NAME})")
    print(
        f"  Server key:    {_display(SERVER_KEY_FILE)}  "
        f"({SERVER_KEY_SCOPES}; crash reports; never in the app)"
    )
    print(f"  Owner:         {ADMIN_EMAIL} / Demo1234!")
    print("=" * 72 + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    require_development_environment("streampulse")
    parser = argparse.ArgumentParser(description="Seed the StreamPulse demo catalogue")
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Skip the 7-day assignment/event history",
    )
    parser.add_argument(
        "--history-users",
        type=int,
        default=DEFAULT_HISTORY_USERS,
        metavar="N",
        help=f"Devices per experiment for the history (default {DEFAULT_HISTORY_USERS})",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete every StreamPulse experiment/flag/group/holdout/event/API key this script created and exit",
    )
    args = parser.parse_args(argv)

    print("\n" + "=" * 72)
    print("  StreamPulse Demo Seeder")
    print("=" * 72)

    with SessionLocal() as db:
        ensure_schema(db)  # the schema must exist before create_all
    ensure_tables()

    if args.reset:
        print("\nResetting StreamPulse data...")
        with SessionLocal() as db:
            counts = reset_streampulse(db)
        print("\n  Removed: " + ", ".join(f"{k}={v:,}" for k, v in counts.items()))
        print()
        return 0

    with SessionLocal() as db:
        print("\n[1/8] Users")
        users = seed_users(db)
        admin_user = users[ADMIN_EMAIL]

        print("\n[2/8] Mutual exclusion group + global holdout")
        group = seed_mutual_exclusion_group(db, admin_user)
        holdout = seed_global_holdout(db, admin_user)

        print("\n[3/8] Experiments")
        experiments = seed_experiments(db, admin_user, group)

        print("\n[4/8] Feature flags")
        flags = seed_feature_flags(db, admin_user)

        print("\n[5/8] Safety settings")
        seed_safety_settings(db)

        print("\n[6/8] Audit trail")
        seed_audit_logs(db, admin_user, experiments[UPSELL_KEY])

        print("\n[7/8] API keys")
        seed_api_key(db, admin_user)
        seed_server_api_key(db, admin_user)

        print("\n[8/8] History")
        if args.no_history:
            print("  --no-history given, skipping.")
        else:
            seed_history(db, experiments, max(args.history_users, 0))

        print_summary(db, experiments, flags, holdout)

    return 0


if __name__ == "__main__":
    sys.exit(main())
