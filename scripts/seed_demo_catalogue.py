#!/usr/bin/env python3
"""Give any Controlvar install the ShopLab and StreamPulse demo catalogue, through its API.

    export CONTROLVAR_ADMIN_PASSWORD     # or leave it unset: the password is asked for
    python3 scripts/seed_demo_catalogue.py --api-url https://controlvar.example.com \\
        --email admin@example.com

The demo apps (``demo/shoplab``, ``demo/streampulse``) and their simulators need
a catalogue of experiments and flags with the exact keys they use, and an API
key each. ``backend/scripts/seed_shoplab.py`` and ``seed_streampulse.py`` write
that catalogue straight into a development database; this command creates the
same catalogue in any install, through the public API only, signed in once as
the administrator. The AWS installer's demo step runs it too.

What it creates, each item matched by its key or name and never changed when it
is already there, so a second run changes nothing:

* ShopLab: 4 active experiments, and 2 flags, one of them with its rollout
  schedule and safety configuration;
* StreamPulse: the exclusion group ``streampulse-profile``; the 2% global
  holdout ``streampulse-holdout``, skipped when another holdout is active (the
  server picks its salt: the API takes none); 5 active experiments; 4 flags,
  with the player's rollout schedule and the safety configurations; and the
  global safety settings with automatic rollbacks on;
* no users;
* three API keys: ``shoplab-storefront`` and ``streampulse-app`` (read,write),
  and ``streampulse-server`` (``sdk:ruleset``).

The safety settings and the holdout apply to the whole install. An item that is
already there but not in the state the demo apps need (an experiment that is
not active, say) is reported, not changed; when the apps cannot work with it
the run fails, before any key is written.

The files it writes, each mode 0600, are where the apps and the simulators
already look (the ``SHOPLAB_DEMO_DIR`` and ``STREAMPULSE_DEMO_DIR`` environment
variables move them, as they move the database seeders' files):

* ``demo/shoplab/.api_key`` and ``demo/shoplab/.env.local``;
* ``demo/streampulse/.api_key`` and ``demo/streampulse/.env.local``;
* ``demo/streampulse/.api_key_server``: the StreamPulse server key goes there
  and nowhere else, so it never reaches the browser or the screen.

Each ``.env.local`` gets ``NEXT_PUBLIC_CONTROLVAR_API_URL``,
``NEXT_PUBLIC_CONTROLVAR_API_KEY`` and ``NEXT_PUBLIC_CONTROLVAR_DASHBOARD_URL``
for the install, and keeps its other lines. A key file is written only after
its key has answered 200 once. On a later run a key whose file is missing, or
whose key is refused, is revoked and replaced: a key's plaintext exists only
when it is created.

It prints no key and no password. The password comes from
``CONTROLVAR_ADMIN_PASSWORD`` or from a prompt, never from the command line.

Exit codes: 0 done; 1 failed (run it again); 2 refused, nothing was changed;
130 interrupted.

Standard library only, and outside the backend's import graph: it runs from a
checkout with nothing installed.
"""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TextIO, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Where the administrator's password is read from. Never the command line.
PASSWORD_ENV = "CONTROLVAR_ADMIN_PASSWORD"

#: The variables that move each app's key files (the database seeders read
#: the same two).
SHOPLAB_DIR_ENV = "SHOPLAB_DEMO_DIR"
STREAMPULSE_DIR_ENV = "STREAMPULSE_DEMO_DIR"

#: The files the apps and the simulators read.
API_KEY_FILE_NAME = ".api_key"
SERVER_KEY_FILE_NAME = ".api_key_server"
ENV_LOCAL_FILE_NAME = ".env.local"
ENV_API_URL = "NEXT_PUBLIC_CONTROLVAR_API_URL"
ENV_API_KEY = "NEXT_PUBLIC_CONTROLVAR_API_KEY"
ENV_DASHBOARD_URL = "NEXT_PUBLIC_CONTROLVAR_DASHBOARD_URL"

SHOPLAB_KEY_NAME = "shoplab-storefront"
STREAMPULSE_KEY_NAME = "streampulse-app"
SERVER_KEY_NAME = "streampulse-server"
APP_KEY_SCOPES: Tuple[str, ...] = ("read", "write")
SERVER_KEY_SCOPES: Tuple[str, ...] = ("sdk:ruleset",)

MEG_NAME = "streampulse-profile"
HOLDOUT_NAME = "streampulse-holdout"
HOLDOUT_PERCENTAGE = 2

#: The user id a key's check evaluates a flag for.
CHECK_USER_ID = "controlvar-demo-seed"
#: The flags each app's key is checked against (neither has a safety
#: configuration, so the check cannot count toward a rollback).
SHOPLAB_CHECK_FLAG = "shoplab_free_shipping_banner"
STREAMPULSE_CHECK_FLAG = "streampulse_offline_mode"

#: Seconds before a request counts as no answer.
TIMEOUT = 30.0
#: The most of an answer that is read.
MAX_ANSWER_BYTES = 32_000_000
#: Tries for a request the API answers 429, and the longest wait between them.
RATE_LIMIT_TRIES = 4
RATE_LIMIT_MAX_WAIT = 20.0
USER_AGENT = "controlvar-demo-seed/1"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_INTERRUPTED = 130

SAFETY_METRICS: Dict[str, Dict[str, Any]] = {
    "error_rate": {
        "warning_threshold": 0.02,
        "critical_threshold": 0.05,
        "comparison_type": "greater_than",
    }
}

# ---------------------------------------------------------------------------
# The catalogue: the database seeders' (backend/scripts/seed_shoplab.py and
# seed_streampulse.py), as API request bodies. The keys are the ones
# demo/*/src/lib/env.ts and the simulators use;
# backend/tests/unit/scripts/test_seed_demo_catalogue.py holds every key, and
# every field the database seeders set, to theirs.
# ---------------------------------------------------------------------------

SHOPLAB_EXPERIMENTS: Tuple[Dict[str, Any], ...] = (
    {
        "key": "shoplab_hero_banner",
        "name": "ShopLab: Hero banner — image vs video",
        "description": "Home page hero: static seasonal image vs autoplaying product video.",
        "hypothesis": "A video hero lifts hero CTA click-through by 20%+ without hurting purchases.",
        "experiment_type": "a_b",
        "optimization_type": "fixed",
        "tags": ["shoplab", "demo"],
        "sequential_testing_enabled": True,
        "sequential_testing_config": {"method": "msprt"},
        "bayesian_enabled": True,
        "bayesian_config": {"rope": [-0.005, 0.005]},
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
                "metric_type": "conversion",
                "is_primary": True,
                "minimum_sample_size": 2000,
                "expected_effect": 0.25,
            },
            {
                "name": "Purchase",
                "event_name": "purchase",
                "metric_type": "conversion",
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
        "experiment_type": "bandit",
        "optimization_type": "thompson_sampling",
        "tags": ["shoplab", "demo"],
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
                "metric_type": "conversion",
                "is_primary": True,
                "minimum_sample_size": 1000,
                "expected_effect": 0.14,
            },
            {
                "name": "Add to cart",
                "event_name": "add_to_cart",
                "metric_type": "conversion",
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
        "experiment_type": "mv",
        "optimization_type": "fixed",
        "tags": ["shoplab", "demo"],
        "sequential_testing_enabled": True,
        "sequential_testing_config": {"method": "msprt"},
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
                "metric_type": "conversion",
                "is_primary": True,
                "minimum_sample_size": 4000,
                "expected_effect": 0.25,
            },
            {
                "name": "Purchase",
                "event_name": "purchase",
                "metric_type": "conversion",
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
        "experiment_type": "a_b",
        "optimization_type": "fixed",
        "tags": ["shoplab", "demo"],
        "bayesian_enabled": True,
        "bayesian_config": {"rope": [-0.005, 0.005]},
        # The database seed names the primary metric as the covariate. A create
        # cannot name a metric that does not exist yet; without one, each
        # metric's own event is the covariate, and both metrics here count
        # `purchase`, so the adjustment is the same.
        "variance_reduction_config": {"method": "cuped", "covariate_lookback_days": 7},
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
                "metric_type": "conversion",
                "is_primary": True,
                "minimum_sample_size": 2000,
                "expected_effect": 0.08,
            },
            {
                "name": "Order value",
                "event_name": "purchase",
                "metric_type": "revenue",
                "is_primary": False,
                "aggregation_method": "sum",
                "event_value_path": "value",
                "minimum_sample_size": 2000,
                "expected_effect": None,
            },
        ],
    },
)

#: A flag: its create body, and optionally its rollout schedule (dates as days
#: from now) and its safety configuration.
SHOPLAB_FLAGS: Tuple[Dict[str, Any], ...] = (
    {
        "flag": {
            "key": "shoplab_new_search",
            "name": "ShopLab: New search engine",
            "description": "Gradual rollout of the new search engine on the ShopLab storefront.",
            "is_active": True,
            "rollout_percentage": 10,
            "targeting_rules": {},
            "tags": ["search", "rollout"],
        },
        "schedule": {
            "name": "New search engine rollout",
            "description": "10% → 50% → 100% over two weeks",
            "start_in_days": -2,
            "end_in_days": 30,
            "max_percentage": 100,
            "min_stage_duration": 24,
            "stages": [
                {
                    "name": "Canary 10%",
                    "description": "First 10% of visitors",
                    "stage_order": 1,
                    "target_percentage": 10,
                    "trigger_type": "time_based",
                    "start_in_days": -2,
                },
                {
                    "name": "Expand to 50%",
                    "description": "Half of all visitors",
                    "stage_order": 2,
                    "target_percentage": 50,
                    "trigger_type": "time_based",
                    "start_in_days": 5,
                },
                {
                    "name": "Full rollout",
                    "description": "Everyone",
                    "stage_order": 3,
                    "target_percentage": 100,
                    "trigger_type": "time_based",
                    "start_in_days": 12,
                },
            ],
        },
        "safety": {
            "enabled": True,
            "metrics": SAFETY_METRICS,
            "rollback_percentage": 0,
        },
    },
    {
        "flag": {
            "key": "shoplab_free_shipping_banner",
            "name": "ShopLab: Free shipping banner",
            "description": "Kill switch for the free-shipping promo banner in the header.",
            "is_active": True,
            "rollout_percentage": 100,
            "targeting_rules": {},
            "tags": ["promo", "kill-switch"],
        },
    },
)

STREAMPULSE_EXPERIMENTS: Tuple[Dict[str, Any], ...] = (
    {
        "key": "streampulse_push_frequency",
        "name": "StreamPulse: Push notification frequency",
        "description": "Daily push digest vs three pushes a week on the Notifications screen.",
        "hypothesis": "Fewer, better-timed pushes raise open rate and lower the uninstall guardrail.",
        "experiment_type": "a_b",
        "optimization_type": "fixed",
        "tags": ["streampulse", "demo", "mobile"],
        "sequential_testing_enabled": True,
        "sequential_testing_config": {"method": "msprt"},
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
                "metric_type": "conversion",
                "is_primary": True,
                "minimum_sample_size": 2000,
                "expected_effect": 0.23,
            },
            {
                "name": "App uninstall (guardrail)",
                "event_name": "app_uninstall",
                "metric_type": "conversion",
                "is_primary": False,
                "minimum_sample_size": 2000,
                "expected_effect": None,
                "lower_is_better": True,
            },
        ],
    },
    {
        "key": "streampulse_wrapped",
        "name": "StreamPulse: Your 2026 Wrapped",
        "description": "Personal year-in-review card on the Profile screen (mutually exclusive with profile badges).",
        "hypothesis": "A Wrapped card drives views and social shares from the Profile screen.",
        "experiment_type": "a_b",
        "optimization_type": "fixed",
        "tags": ["streampulse", "demo", "mobile"],
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
                "metric_type": "conversion",
                "is_primary": True,
                "minimum_sample_size": 1000,
                "expected_effect": 0.10,
            },
            {
                "name": "Share",
                "event_name": "share",
                "metric_type": "conversion",
                "is_primary": False,
                "minimum_sample_size": 1000,
                "expected_effect": 0.33,
            },
        ],
    },
    {
        "key": "streampulse_profile_badges",
        "name": "StreamPulse: Profile badges",
        "description": "Listening-milestone badges on the Profile screen (mutually exclusive with Wrapped).",
        "hypothesis": "Badges give users a reason to tap around their profile.",
        "experiment_type": "a_b",
        "optimization_type": "fixed",
        "tags": ["streampulse", "demo", "mobile"],
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
                "metric_type": "conversion",
                "is_primary": True,
                "minimum_sample_size": 1000,
                "expected_effect": 0.50,
            },
        ],
    },
    {
        "key": "streampulse_onboarding_steps",
        "name": "StreamPulse: Onboarding — 5 steps vs 3 steps",
        "description": "Shorter onboarding stepper; Bayesian decisioning stops early on a clear winner.",
        "hypothesis": "A 3-step onboarding completes more often and gets users to a first play sooner.",
        "experiment_type": "a_b",
        "optimization_type": "fixed",
        "tags": ["streampulse", "demo", "mobile"],
        "bayesian_enabled": True,
        "bayesian_config": {"rope": [-0.01, 0.01]},
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
                "metric_type": "conversion",
                "is_primary": True,
                "minimum_sample_size": 1500,
                "expected_effect": 0.09,
            },
            {
                "name": "First play",
                "event_name": "first_play",
                "metric_type": "conversion",
                "is_primary": False,
                "minimum_sample_size": 1500,
                "expected_effect": 0.11,
            },
        ],
    },
    {
        "key": "streampulse_upsell_modal",
        "name": "StreamPulse: Premium upsell modal",
        "description": "Classic upsell modal vs a value-led modal on the Payments screen (full audit trail).",
        "hypothesis": "Leading with value (offline, no ads, hi-fi) converts more free users to premium.",
        "experiment_type": "a_b",
        "optimization_type": "fixed",
        "tags": ["streampulse", "demo", "mobile"],
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
                "metric_type": "conversion",
                "is_primary": True,
                "minimum_sample_size": 5000,
                "expected_effect": 0.25,
            },
        ],
    },
)

#: The two Profile experiments: a device sees one of them, never both.
STREAMPULSE_GROUPED: Tuple[str, ...] = (
    "streampulse_wrapped",
    "streampulse_profile_badges",
)

STREAMPULSE_GROUP: Dict[str, Any] = {
    "name": MEG_NAME,
    "description": "Profile screen: a device sees either Wrapped or profile badges, never both.",
    "traffic_allocation": 1.0,
}

STREAMPULSE_HOLDOUT: Dict[str, Any] = {
    "name": HOLDOUT_NAME,
    "description": "2% of devices never enter any StreamPulse experiment (clean baseline).",
    "holdout_percentage": HOLDOUT_PERCENTAGE,
    "is_active": True,
}

# Dashboard-shape targeting rules, as the database seed writes them.
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

STREAMPULSE_FLAGS: Tuple[Dict[str, Any], ...] = (
    {
        "flag": {
            "key": "streampulse_recs_v2",
            "name": "StreamPulse: Recommendations v2",
            "description": "Home feed: new recommendation algorithm. Kill switch — flip it off and every device falls back to the classic chronological feed.",
            "is_active": True,
            "rollout_percentage": 100,
            "targeting_rules": {},
            "tags": ["home", "kill-switch", "streampulse"],
        },
        "safety": {
            "enabled": True,
            "metrics": SAFETY_METRICS,
            "rollback_percentage": 0,
        },
    },
    {
        "flag": {
            "key": "streampulse_player_v2",
            "name": "StreamPulse: Player v2",
            "description": "Redesigned player. Employees always get it; everyone else follows the staged rollout (5% → 25% → 50% → 100%). Safety monitoring rolls back to 5% on an error-rate spike.",
            "is_active": True,
            "rollout_percentage": 5,
            "targeting_rules": PLAYER_V2_RULES,
            "tags": ["player", "rollout", "safety", "streampulse"],
        },
        "schedule": {
            "name": "Player v2 rollout",
            "description": "Internal + 5% → 25% → 50% → 100%",
            "start_in_days": -2,
            "end_in_days": 30,
            "max_percentage": 100,
            "min_stage_duration": 1,
            "stages": [
                {
                    "name": "Internal + 5%",
                    "description": "Employees via targeting rule plus 5% of everyone",
                    "stage_order": 1,
                    "target_percentage": 5,
                    "trigger_type": "time_based",
                    "start_in_days": -2,
                },
                {
                    "name": "25%",
                    "description": "A quarter of all devices",
                    "stage_order": 2,
                    "target_percentage": 25,
                    "trigger_type": "time_based",
                    "start_in_days": 1,
                },
                {
                    "name": "50%",
                    "description": "Half of all devices",
                    "stage_order": 3,
                    "target_percentage": 50,
                    "trigger_type": "time_based",
                    "start_in_days": 3,
                },
                {
                    "name": "100%",
                    "description": "Everyone",
                    "stage_order": 4,
                    "target_percentage": 100,
                    "trigger_type": "time_based",
                    "start_in_days": 7,
                },
            ],
        },
        "safety": {
            "enabled": True,
            "metrics": SAFETY_METRICS,
            "rollback_percentage": 5,
        },
    },
    {
        "flag": {
            "key": "streampulse_ai_search",
            "name": "StreamPulse: AI search",
            "description": "GPU-backed AI search. Targeted only: iOS 17+, US, premium tier.",
            "is_active": True,
            "rollout_percentage": 0,
            "targeting_rules": AI_SEARCH_RULES,
            "tags": ["search", "targeting"],
        },
    },
    {
        "flag": {
            "key": "streampulse_offline_mode",
            "name": "StreamPulse: Offline downloads",
            "description": "Offline downloads. Demonstrates SDK caching: the app keeps serving the cached value when the platform is unreachable.",
            "is_active": True,
            "rollout_percentage": 100,
            "targeting_rules": {},
            "tags": ["kill-switch"],
        },
    },
)

#: The global safety settings the StreamPulse rollout story needs. Default
#: metrics are set only when the install has none.
SAFETY_SETTINGS: Dict[str, Any] = {
    "enable_automatic_rollbacks": True,
    "default_metrics": SAFETY_METRICS,
}

SHOPLAB_KEY_DESCRIPTION = "ShopLab demo storefront + traffic simulator"
STREAMPULSE_KEY_DESCRIPTION = "StreamPulse demo app + device simulator"
SERVER_KEY_DESCRIPTION = "StreamPulse server: forwards the devices' crash reports"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SeedError(Exception):
    """The run failed; a message for the operator, holding no secret."""


class Refused(SeedError):
    """Refused before anything was changed (exit 2)."""


class ApiError(SeedError):
    """An answer the run cannot go on from. ``status`` 0 means no answer."""

    def __init__(self, status: int, method: str, path: str, detail: str = "") -> None:
        self.status = status
        self.method = method
        self.path = path
        self.detail = detail
        answered = "no answer" if status == 0 else f"HTTP {status}"
        super().__init__(
            f"{method} {path}: {answered}" + (f" ({detail})" if detail else "")
        )


# ---------------------------------------------------------------------------
# The HTTP client
# ---------------------------------------------------------------------------


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is answered as an error: a sign-in is never re-sent elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _short(text: str, limit: int = 300) -> str:
    """One line of printable text, at most ``limit`` characters."""
    flat = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _detail(payload: bytes) -> str:
    """The API's own reason for a refusal: ``detail`` only, never the request."""
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return ""
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, str):
        return _short(detail)
    if isinstance(detail, list):
        reasons = []
        for item in detail[:3]:
            if not isinstance(item, dict):
                continue
            where = ".".join(str(p) for p in item.get("loc", []) if p != "body")
            what = str(item.get("msg", ""))
            reasons.append(f"{where}: {what}" if where else what)
        return _short("; ".join(reasons))
    return ""


class Api:
    """The install's API, signed in as the administrator once :meth:`sign_in` succeeds."""

    def __init__(
        self,
        base_url: str,
        timeout: float = TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.sleep = sleep
        self.token: Optional[str] = None
        self._opener = urllib.request.build_opener(_RefuseRedirects)

    def _send(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Tuple[int, bytes, Mapping[str, str]]:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None if body is None else json.dumps(body).encode("utf-8")
        sent = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            **(headers or {}),
        }
        if data is not None:
            sent["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=sent)
        for attempt in range(1, RATE_LIMIT_TRIES + 1):
            try:
                with self._opener.open(request, timeout=self.timeout) as answer:
                    return answer.status, answer.read(MAX_ANSWER_BYTES), answer.headers
            except urllib.error.HTTPError as error:
                payload = error.read(MAX_ANSWER_BYTES) if error.fp is not None else b""
                if error.code == 429 and attempt < RATE_LIMIT_TRIES:
                    self.sleep(_retry_after(error.headers))
                    continue
                return error.code, payload, error.headers
            except (
                urllib.error.URLError,
                OSError,
                http.client.HTTPException,
                ValueError,
            ) as error:
                reason = getattr(error, "reason", None) or error
                raise ApiError(0, method, path, _short(str(reason))) from None
        raise AssertionError("unreachable")  # pragma: no cover

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        params: Optional[Mapping[str, Any]] = None,
        *,
        ok: Sequence[int] = (200, 201),
    ) -> Any:
        """Send one signed-in request; the JSON answer, or :class:`ApiError`."""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        status, payload, answer_headers = self._send(
            method, path, body=body, params=params, headers=headers
        )
        if status not in ok:
            raise ApiError(
                status,
                method,
                path,
                _redirect_note(status, answer_headers) or _detail(payload),
            )
        if not payload:
            return None
        try:
            return json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(status, method, path, "the answer is not JSON") from None

    def health(self) -> None:
        """``GET /health`` answers 200 with JSON: this is a Controlvar API."""
        status, payload, headers = self._send("GET", "/health")
        if status != 200:
            raise ApiError(
                status,
                "GET",
                "/health",
                _redirect_note(status, headers) or "the install is not healthy",
            )
        try:
            json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(
                status,
                "GET",
                "/health",
                "the answer is not JSON: is this a Controlvar install?",
            ) from None

    def sign_in(self, email: str, password: str) -> Dict[str, Any]:
        """Sign in once; the signed-in user. Nothing from the answer is printed but its status."""
        status, payload, headers = self._send(
            "POST", "/api/v1/auth/login", body={"email": email, "password": password}
        )
        if status == 401:
            raise Refused(
                f"the install refused the sign-in for {email}: check the address and the password"
            )
        if status == 423:
            raise Refused(
                f"{email} is locked after repeated failed sign-ins: wait, then run this again"
            )
        if status == 404:
            raise Refused(
                "this install has no password sign-in (POST /api/v1/auth/login answered 404)"
            )
        if status != 200:
            raise ApiError(
                status, "POST", "/api/v1/auth/login", _redirect_note(status, headers)
            )
        try:
            answer = json.loads(payload)
            token = answer["access_token"]
            user = answer.get("user") or {}
        except (ValueError, UnicodeDecodeError, KeyError, TypeError, AttributeError):
            raise ApiError(
                status,
                "POST",
                "/api/v1/auth/login",
                "the answer carries no access token",
            ) from None
        if not isinstance(token, str) or not token:
            raise ApiError(
                status,
                "POST",
                "/api/v1/auth/login",
                "the answer carries no access token",
            )
        self.token = token
        return user if isinstance(user, dict) else {}

    def check_key(
        self, path: str, key: str, params: Optional[Mapping[str, Any]] = None
    ) -> int:
        """The status one request with ``X-API-Key: key`` gets. Its answer is not kept."""
        status, _payload, _headers = self._send(
            "GET", path, params=params, headers={"X-API-Key": key}
        )
        return status


def _retry_after(headers: Mapping[str, str]) -> float:
    try:
        wait = float(headers.get("Retry-After", "2"))
    except (TypeError, ValueError):
        wait = 2.0
    return min(max(wait, 1.0), RATE_LIMIT_MAX_WAIT)


def _redirect_note(status: int, headers: Mapping[str, str]) -> str:
    if 300 <= status < 400:
        target = _short(str(headers.get("Location", "")), 120)
        return (
            f"a redirect to {target}: pass that address as --api-url"
            if target
            else "a redirect"
        )
    return ""


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def merged_env_local(
    existing: str, values: Mapping[str, str], forbidden: Sequence[str] = ()
) -> str:
    """``existing`` with each name in ``values`` set once, the other lines kept.

    A line holding any of ``forbidden`` (the StreamPulse server key) is dropped,
    so the file never carries it.
    """
    lines: List[str] = []
    seen = set()
    for line in existing.splitlines():
        if any(secret and secret in line for secret in forbidden):
            continue
        match = _ENV_LINE.match(line)
        name = match.group(1) if match else None
        if name in values:
            if name not in seen:
                lines.append(f"{name}={values[name]}")
                seen.add(name)
            continue
        lines.append(line)
    lines.extend(
        f"{name}={value}" for name, value in values.items() if name not in seen
    )
    return "\n".join(lines) + "\n"


def write_private(path: Path, text: str) -> bool:
    """Make ``path`` hold ``text`` with mode 0600; whether it changed.

    The new content goes to a temporary file in the same folder (created 0600,
    ignored by git as ``*.tmp``), which then replaces ``path`` in one step.
    """
    try:
        if (
            path.read_text(encoding="utf-8") == text
            and (path.stat().st_mode & 0o777) == 0o600
        ):
            return False
    except (FileNotFoundError, UnicodeDecodeError, NotADirectoryError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return True


def read_key_file(path: Path) -> Optional[str]:
    try:
        key = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return key or None


def display(path: Path) -> str:
    """A repository-relative path when it is under the checkout."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


@dataclass(frozen=True)
class DemoFolders:
    shoplab: Path
    streampulse: Path

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "DemoFolders":
        return cls(
            shoplab=Path(
                environ.get(SHOPLAB_DIR_ENV) or REPO_ROOT / "demo" / "shoplab"
            ),
            streampulse=Path(
                environ.get(STREAMPULSE_DIR_ENV) or REPO_ROOT / "demo" / "streampulse"
            ),
        )


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _items(page: Any) -> List[Dict[str, Any]]:
    if isinstance(page, list):
        return [item for item in page if isinstance(item, dict)]
    if isinstance(page, dict) and isinstance(page.get("items"), list):
        return [item for item in page["items"] if isinstance(item, dict)]
    return []


@dataclass
class KeySpec:
    name: str
    description: str
    scopes: Tuple[str, ...]
    file: Path
    check_path: str
    check_params: Optional[Dict[str, str]] = None


@dataclass
class Seeder:
    api: Any
    folders: DemoFolders
    out: TextIO = sys.stdout
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    problems: List[str] = field(default_factory=list)
    created: int = 0

    # --- output -------------------------------------------------------------
    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def item(self, subject: str, outcome: str) -> None:
        self.say(f"  {subject:<46} {outcome}")

    # --- lookups ------------------------------------------------------------
    def experiments_by_key(self) -> Dict[str, Dict[str, Any]]:
        found: Dict[str, Dict[str, Any]] = {}
        skip = 0
        while True:
            page = self.api.call(
                "GET", "/api/v1/experiments/", params={"skip": skip, "limit": 500}
            )
            items = _items(page)
            for item in items:
                if item.get("key"):
                    found.setdefault(str(item["key"]), item)
            skip += len(items)
            total = page.get("total") if isinstance(page, dict) else None
            if not items or not isinstance(total, int) or skip >= total:
                return found

    def flag_by_key(self, key: str) -> Optional[Dict[str, Any]]:
        skip = 0
        while True:
            page = self.api.call(
                "GET",
                "/api/v1/feature-flags/",
                params={"search": key, "skip": skip, "limit": 100},
            )
            items = _items(page)
            for item in items:
                if item.get("key") == key:
                    return item
            skip += len(items)
            total = page.get("total") if isinstance(page, dict) else None
            if not items or not isinstance(total, int) or skip >= total:
                return None

    def _at(self, days: int) -> str:
        return (self.now() + timedelta(days=days)).isoformat()

    # --- catalogue ----------------------------------------------------------
    def seed_experiments(
        self,
        specs: Sequence[Dict[str, Any]],
        existing: Dict[str, Dict[str, Any]],
        group: Optional[Dict[str, Any]] = None,
        grouped: Sequence[str] = (),
    ) -> None:
        for spec in specs:
            key = spec["key"]
            subject = f"experiment {key}"
            found = existing.get(key)
            if found is not None:
                status = str(found.get("status", "")).lower()
                if status != "active":
                    self.item(
                        subject,
                        f"already there but {status or 'not active'}: not changed",
                    )
                    self.problems.append(
                        f"the experiment {key} is {status or 'not active'}, and the demo apps need it active: "
                        "start it in the dashboard, or delete it and run this again"
                    )
                    continue
                note = ""
                if (
                    key in grouped
                    and group is not None
                    and found.get("mutual_exclusion_group_id") != group.get("id")
                ):
                    note = f" (outside {MEG_NAME}; not changed)"
                self.item(subject, "already there, not changed" + note)
                continue
            created = self.api.call("POST", "/api/v1/experiments/", spec)
            experiment_id = created["id"]
            if key in grouped and group is not None:
                self.api.call(
                    "POST",
                    f"/api/v1/mutual-exclusion-groups/{group['id']}/experiments",
                    {"experiment_id": experiment_id},
                )
            self.api.call("POST", f"/api/v1/experiments/{experiment_id}/start")
            self.created += 1
            extra = f", in {MEG_NAME}" if key in grouped and group is not None else ""
            self.item(subject, f"created and started{extra}")

    def seed_flags(self, specs: Sequence[Dict[str, Any]]) -> None:
        for spec in specs:
            body = spec["flag"]
            key = body["key"]
            subject = f"flag {key}"
            found = self.flag_by_key(key)
            if found is not None:
                status = str(found.get("status", "")).lower()
                if status != "active":
                    self.item(
                        subject,
                        f"already there but {status or 'off'}: not changed (the apps see it off)",
                    )
                else:
                    self.item(subject, "already there, not changed")
                continue
            flag = self.api.call("POST", "/api/v1/feature-flags/", body)
            parts = []
            schedule = spec.get("schedule")
            if schedule:
                created = self.api.call(
                    "POST",
                    "/api/v1/rollout-schedules/",
                    self._schedule_body(schedule, flag["id"]),
                )
                self.api.call(
                    "POST", f"/api/v1/rollout-schedules/{created['id']}/activate"
                )
                parts.append("rollout schedule")
            safety = spec.get("safety")
            if safety:
                self.api.call(
                    "POST", f"/api/v1/safety/feature-flags/{flag['id']}/config", safety
                )
                parts.append("safety configuration")
            self.created += 1
            self.item(
                subject,
                "created" + (f" with its {' and '.join(parts)}" if parts else ""),
            )

    def _schedule_body(self, schedule: Dict[str, Any], flag_id: str) -> Dict[str, Any]:
        return {
            "name": schedule["name"],
            "description": schedule["description"],
            "feature_flag_id": flag_id,
            "start_date": self._at(schedule["start_in_days"]),
            "end_date": self._at(schedule["end_in_days"]),
            "max_percentage": schedule["max_percentage"],
            "min_stage_duration": schedule["min_stage_duration"],
            "stages": [
                {
                    "name": stage["name"],
                    "description": stage["description"],
                    "stage_order": stage["stage_order"],
                    "target_percentage": stage["target_percentage"],
                    "trigger_type": stage["trigger_type"],
                    "start_date": self._at(stage["start_in_days"]),
                }
                for stage in schedule["stages"]
            ],
        }

    def seed_group(self) -> Dict[str, Any]:
        subject = f"exclusion group {MEG_NAME}"
        page = self.api.call(
            "GET", "/api/v1/mutual-exclusion-groups", params={"limit": 1000}
        )
        for group in _items(page):
            if group.get("name") == MEG_NAME:
                self.item(subject, "already there, not changed")
                return group
        group = self.api.call(
            "POST", "/api/v1/mutual-exclusion-groups", STREAMPULSE_GROUP
        )
        self.created += 1
        self.item(subject, "created")
        return group

    def seed_holdout(self) -> None:
        subject = f"global holdout {HOLDOUT_NAME}"
        active = self.api.call("GET", "/api/v1/holdout")
        if isinstance(active, dict) and active:
            if active.get("name") == HOLDOUT_NAME:
                self.item(subject, "already there and active, not changed")
            else:
                self.item(
                    subject,
                    f"skipped: the holdout '{_short(str(active.get('name')), 80)}' is active, and only one can be",
                )
            return
        page = self.api.call("GET", "/api/v1/holdout/all", params={"limit": 1000})
        if any(holdout.get("name") == HOLDOUT_NAME for holdout in _items(page)):
            self.item(subject, "already there but not active: not changed")
            return
        self.api.call("POST", "/api/v1/holdout", STREAMPULSE_HOLDOUT)
        self.created += 1
        self.item(
            subject,
            f"created: {HOLDOUT_PERCENTAGE}% of the whole install's users, active",
        )

    def seed_safety_settings(self) -> None:
        subject = "safety settings"
        current = self.api.call("GET", "/api/v1/safety/settings") or {}
        if current.get("enable_automatic_rollbacks"):
            self.item(subject, "automatic rollbacks already on, not changed")
            return
        body: Dict[str, Any] = {"enable_automatic_rollbacks": True}
        if not current.get("default_metrics"):
            body["default_metrics"] = SAFETY_SETTINGS["default_metrics"]
        self.api.call("POST", "/api/v1/safety/settings", body)
        self.created += 1
        self.item(subject, "automatic rollbacks switched on, for the whole install")

    def seed_catalogue(self) -> None:
        existing = self.experiments_by_key()
        self.say("ShopLab")
        self.seed_experiments(SHOPLAB_EXPERIMENTS, existing)
        self.seed_flags(SHOPLAB_FLAGS)
        self.say("StreamPulse")
        group = self.seed_group()
        self.seed_holdout()
        self.seed_experiments(
            STREAMPULSE_EXPERIMENTS, existing, group, STREAMPULSE_GROUPED
        )
        self.seed_flags(STREAMPULSE_FLAGS)
        self.seed_safety_settings()

    # --- keys ---------------------------------------------------------------
    def key_specs(self) -> List[KeySpec]:
        evaluate = "/api/v1/feature-flags/evaluate/{}"
        return [
            KeySpec(
                SHOPLAB_KEY_NAME,
                SHOPLAB_KEY_DESCRIPTION,
                APP_KEY_SCOPES,
                self.folders.shoplab / API_KEY_FILE_NAME,
                evaluate.format(SHOPLAB_CHECK_FLAG),
                {"user_id": CHECK_USER_ID},
            ),
            KeySpec(
                STREAMPULSE_KEY_NAME,
                STREAMPULSE_KEY_DESCRIPTION,
                APP_KEY_SCOPES,
                self.folders.streampulse / API_KEY_FILE_NAME,
                evaluate.format(STREAMPULSE_CHECK_FLAG),
                {"user_id": CHECK_USER_ID},
            ),
            KeySpec(
                SERVER_KEY_NAME,
                SERVER_KEY_DESCRIPTION,
                SERVER_KEY_SCOPES,
                self.folders.streampulse / SERVER_KEY_FILE_NAME,
                "/api/v1/sdk/ruleset",
            ),
        ]

    def ensure_key(self, spec: KeySpec, mine: List[Dict[str, Any]]) -> Tuple[str, str]:
        """The key's plaintext, kept from its file or new; and what was done."""
        kept = read_key_file(spec.file)
        if kept is not None:
            status = self.api.check_key(spec.check_path, kept, spec.check_params)
            if status == 200:
                write_private(spec.file, kept + "\n")
                return (
                    kept,
                    f"kept: the key in {display(spec.file)} works on this install",
                )
            if status not in (401, 403):
                raise ApiError(
                    status, "GET", spec.check_path, "checking the key in its file"
                )
            reason = f"the key in {display(spec.file)} is refused here ({status})"
        else:
            reason = f"{display(spec.file)} holds no key"
        created = self.api.call(
            "POST",
            "/api/v1/api-keys",
            {
                "name": spec.name,
                "description": spec.description,
                "scopes": list(spec.scopes),
            },
        )
        plaintext = created.get("key") if isinstance(created, dict) else None
        if not isinstance(plaintext, str) or not plaintext:
            raise ApiError(201, "POST", "/api/v1/api-keys", "the answer carries no key")
        status = self.api.check_key(spec.check_path, plaintext, spec.check_params)
        if status != 200:
            self.api.call("DELETE", f"/api/v1/api-keys/{created['id']}", ok=(204, 404))
            raise ApiError(
                status,
                "GET",
                spec.check_path,
                f"the new {spec.name} key did not work; it was revoked",
            )
        write_private(spec.file, plaintext + "\n")
        revoked = 0
        for old in mine:
            if old.get("name") == spec.name and old.get("id") != created.get("id"):
                self.api.call("DELETE", f"/api/v1/api-keys/{old['id']}", ok=(204, 404))
                revoked += 1
        self.created += 1
        done = f"created ({reason})"
        if revoked:
            done += f"; the old {spec.name} key{'s' if revoked > 1 else ''} revoked"
        return plaintext, done

    def seed_keys(self, api_url: str, dashboard_url: str) -> None:
        self.say("API keys")
        mine = _items(self.api.call("GET", "/api/v1/api-keys"))
        plaintexts: Dict[str, str] = {}
        for spec in self.key_specs():
            plaintexts[spec.name], done = self.ensure_key(spec, mine)
            self.item(spec.name, done)
        server_key = plaintexts[SERVER_KEY_NAME]
        for folder, name in (
            (self.folders.shoplab, SHOPLAB_KEY_NAME),
            (self.folders.streampulse, STREAMPULSE_KEY_NAME),
        ):
            env_local = folder / ENV_LOCAL_FILE_NAME
            try:
                existing = env_local.read_text(encoding="utf-8")
            except FileNotFoundError:
                existing = ""
            values = {
                ENV_API_URL: api_url,
                ENV_API_KEY: plaintexts[name],
                ENV_DASHBOARD_URL: dashboard_url,
            }
            changed = write_private(
                env_local, merged_env_local(existing, values, forbidden=(server_key,))
            )
            self.item(
                display(env_local), "written" if changed else "already up to date"
            )
        self.say(
            f"  The StreamPulse server key is in {display(self.folders.streampulse / SERVER_KEY_FILE_NAME)} only: "
            "it never reaches the browser or the screen."
        )


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seed_demo_catalogue.py",
        description=(
            "Create the ShopLab and StreamPulse demo catalogue in a Controlvar install, through its API, "
            "and write the demo apps' key files. A second run changes nothing."
        ),
        epilog=(
            f"The password is read from {PASSWORD_ENV}, or asked for when this runs in a terminal; "
            "never from the command line. The key files, each mode 0600: demo/shoplab/.api_key and "
            ".env.local, demo/streampulse/.api_key and .env.local, and demo/streampulse/.api_key_server, "
            "the StreamPulse server key's only place: it never reaches the browser or the screen. "
            "Nothing printed holds a key or the password. "
            "Exit codes: 0 done; 1 failed (run it again); 2 refused, nothing changed; 130 interrupted."
        ),
    )
    parser.add_argument(
        "--api-url",
        required=True,
        metavar="URL",
        help="the install's address, such as https://controlvar.example.com (the API is under /api/v1)",
    )
    parser.add_argument(
        "--email", required=True, help="the administrator to sign in as; a superuser"
    )
    parser.add_argument(
        "--dashboard-url",
        metavar="URL",
        help="the dashboard the demo apps link to (default: --api-url, where an install serves its dashboard)",
    )
    return parser


def install_address(value: str, option: str) -> str:
    """An http(s) address with a host, no credentials, query or fragment, and no trailing slash."""
    text = value.strip().rstrip("/")
    refused = Refused(
        f"{option} must be an http:// or https:// address, such as https://controlvar.example.com"
    )
    try:
        parts = urllib.parse.urlsplit(text)
        host = parts.hostname
        _port = parts.port  # raises ValueError for a port that is not a number
    except ValueError:
        raise refused from None
    if (
        parts.scheme not in ("http", "https")
        or not host
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        raise refused
    if parts.path.endswith("/api/v1"):
        raise Refused(f"{option} is the install's address, without /api/v1")
    return text


def _is_loopback(address: str) -> bool:
    host = (urllib.parse.urlsplit(address).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def admin_password(
    environ: Mapping[str, str], stdin: Any, prompt: Callable[[str], str], email: str
) -> Optional[str]:
    """The password: the environment variable, else a prompt in a terminal, else none."""
    given = environ.get(PASSWORD_ENV)
    if given:
        return given
    isatty = getattr(stdin, "isatty", None)
    if callable(isatty) and isatty():
        return prompt(f"Password for {email}: ") or None
    return None


def next_steps(api_url: str) -> List[str]:
    """The commands to start each app and its simulator, which stops after 60 minutes."""
    lines = []
    for app in ("shoplab", "streampulse"):
        lines.append(f"  cd demo/{app} && npm install && npm run dev")
        lines.append(
            f"  python3 demo/{app}/simulator/traffic.py --api-url {api_url} --duration 3600"
        )
    return lines


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    api_factory: Callable[[str], Any] = Api,
    environ: Optional[Mapping[str, str]] = None,
    stdin: Any = None,
    prompt: Callable[[str], str] = getpass.getpass,
    out: Optional[TextIO] = None,
    err: Optional[TextIO] = None,
) -> int:
    environ = os.environ if environ is None else environ
    stdin = sys.stdin if stdin is None else stdin
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = build_parser().parse_args(argv)
    try:
        api_url = install_address(args.api_url, "--api-url")
        dashboard_url = (
            install_address(args.dashboard_url, "--dashboard-url")
            if args.dashboard_url
            else api_url
        )
        password = admin_password(environ, stdin, prompt, args.email)
        if not password:
            raise Refused(
                f"no password: set {PASSWORD_ENV}, or run this in a terminal to be asked for it"
            )
        folders = DemoFolders.from_environ(environ)
        seeder = Seeder(api_factory(api_url), folders, out=out)
        print(f"Demo catalogue -> {api_url}", file=out)
        if not _is_loopback(api_url) and api_url.startswith("http://"):
            print(
                f"warning: {api_url} is not https: the password and the keys cross the network unencrypted",
                file=err,
            )
        seeder.api.health()
        user = seeder.api.sign_in(args.email, password)
        if not user.get("is_superuser"):
            raise Refused(
                f"{args.email} is not a superuser, and the safety settings need one: sign in as the install's first administrator"
            )
        print(f"Signed in as {args.email}.", file=out)
    except Refused as error:
        print(f"error: {error}", file=err)
        return EXIT_REFUSED
    except ApiError as error:
        print(f"error: {error}", file=err)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\ninterrupted: nothing was changed", file=err)
        return EXIT_INTERRUPTED

    try:
        seeder.seed_catalogue()
        if seeder.problems:
            for problem in seeder.problems:
                print(f"error: {problem}", file=err)
            print(
                "error: no key file was written; run this again once that is fixed",
                file=err,
            )
            return EXIT_FAILED
        seeder.seed_keys(api_url, dashboard_url)
    except SeedError as error:
        print(f"error: {error}", file=err)
        print(
            "error: the run stopped there; run this again: what it already made is kept",
            file=err,
        )
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\ninterrupted: run this again to finish", file=err)
        return EXIT_INTERRUPTED

    print("", file=out)
    print(
        "Done: "
        + (f"{seeder.created} created" if seeder.created else "nothing to change")
        + ".",
        file=out,
    )
    print(
        "Results start empty: there is no history until the simulators send traffic.",
        file=out,
    )
    print(
        "Start each app and its simulator (the simulator stops after 60 minutes):",
        file=out,
    )
    for line in next_steps(api_url):
        print(line, file=out)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
