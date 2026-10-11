#!/usr/bin/env python3
"""
Seed the fixtures used by the live SDK contract tests.

Creates (idempotently):
- the demo users (admin@demo.com owns everything),
- an ACTIVE A/B experiment with public key ``sdk_contract_ab`` (variants
  ``control`` / ``treatment`` at 50/50, primary metric ``purchase``),
- an ACTIVE feature flag ``sdk_contract_flag`` at 100% rollout,
- an API key named ``sdk-contract-smoke``, with no ``sdk:ruleset`` scope,
  whose plaintext is written to ``tests/sdk-contract/live/.api_key``;
- a second key of the same user, ``sdk-contract-local``, with the
  ``sdk:ruleset`` scope (server-side local evaluation), written to
  ``tests/sdk-contract/live/.api_key_local``.

Both files are 0600 and gitignored. Two keys, so the live contract can show
the ruleset endpoint refusing the first and serving the second.

Then run ``python tests/sdk-contract/live/run_live_contract.py`` against a
backend pointed at the same database.

Usage:
    source venv/bin/activate
    python backend/scripts/seed_sdk_contract.py [--reset]

Development and test only, like the demo seeds: with any other ENVIRONMENT
(staging, production) it stops before touching the database
(backend/scripts/seed_guard.py).

Environment: the same POSTGRES_* variables as the other seed scripts.
``SDK_CONTRACT_API_KEY`` and ``SDK_CONTRACT_LOCAL_API_KEY`` fix the plaintext
keys (otherwise they are generated); ``SDK_CONTRACT_KEY_DIR`` redirects the key
files (containers).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("POSTGRES_SERVER", "localhost")
os.environ.setdefault("POSTGRES_USER", "postgres")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres")
os.environ.setdefault("POSTGRES_DB", "experimentation")
os.environ.setdefault("POSTGRES_SCHEMA", "experimentation")

from backend.app.core.permissions import Action, ResourceType, check_permission
from backend.app.core.security import hash_api_key
from backend.app.db.session import SessionLocal
from backend.app.models.api_key import APIKey, generate_api_key
from backend.app.models.assignment import Assignment
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
from backend.scripts.seed_demo_data import (
    ensure_schema,
    ensure_tables,
    now_utc,
    seed_users,
)
from backend.scripts.seed_guard import require_development_environment

EXPERIMENT_KEY = "sdk_contract_ab"
FLAG_KEY = "sdk_contract_flag"
API_KEY_NAME = "sdk-contract-smoke"
LOCAL_API_KEY_NAME = "sdk-contract-local"
LOCAL_API_KEY_SCOPES = "sdk:ruleset"
# SDK_CONTRACT_KEY_DIR lets containers (docker-compose SEED=...,sdk-contract)
# redirect the generated keys away from the read-only source tree.
KEY_DIR = (
    Path(os.environ["SDK_CONTRACT_KEY_DIR"])
    if os.environ.get("SDK_CONTRACT_KEY_DIR")
    else PROJECT_ROOT / "tests" / "sdk-contract" / "live"
)
KEY_FILE = KEY_DIR / ".api_key"
LOCAL_KEY_FILE = KEY_DIR / ".api_key_local"


def seed_experiment(db, admin_user) -> Experiment:
    existing = db.query(Experiment).filter(Experiment.key == EXPERIMENT_KEY).first()
    if existing:
        print(f"  Experiment '{EXPERIMENT_KEY}' already exists (id={existing.id}).")
        return existing

    exp = Experiment(
        key=EXPERIMENT_KEY,
        name="SDK contract A/B",
        description="Fixture for the live SDK contract tests.",
        hypothesis="Every SDK reaches the same sticky assignment through /tracking/assign.",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        owner_id=admin_user.id,
        start_date=now_utc() - timedelta(days=1),
        end_date=now_utc() + timedelta(days=365),
        targeting_rules={},
        metrics={"primary_metric": "purchase", "metric_type": "conversion"},
        tags=["sdk-contract"],
    )
    db.add(exp)
    db.flush()
    db.add_all(
        [
            Variant(
                experiment_id=exp.id,
                name="control",
                description="Control",
                is_control=True,
                traffic_allocation=50,
                configuration={"color": "blue"},
            ),
            Variant(
                experiment_id=exp.id,
                name="treatment",
                description="Treatment",
                is_control=False,
                traffic_allocation=50,
                configuration={"color": "green"},
            ),
            Metric(
                experiment_id=exp.id,
                name="Purchase",
                event_name="purchase",
                metric_type=MetricType.CONVERSION,
                is_primary=True,
                minimum_sample_size=100,
            ),
        ]
    )
    db.commit()
    db.refresh(exp)
    print(f"  Created experiment '{EXPERIMENT_KEY}' (id={exp.id}).")
    return exp


def seed_flag(db, admin_user) -> FeatureFlag:
    existing = db.query(FeatureFlag).filter(FeatureFlag.key == FLAG_KEY).first()
    if existing:
        print(f"  Flag '{FLAG_KEY}' already exists (id={existing.id}).")
        return existing

    flag = FeatureFlag(
        key=FLAG_KEY,
        name="SDK contract flag",
        description="Always-on fixture for the live SDK contract tests.",
        status=FeatureFlagStatus.ACTIVE,
        owner_id=admin_user.id,
        rollout_percentage=100,
        targeting_rules={},
        tags=["sdk-contract"],
    )
    db.add(flag)
    db.commit()
    db.refresh(flag)
    print(f"  Created flag '{FLAG_KEY}' at 100% (id={flag.id}).")
    return flag


def seed_api_key(
    db,
    admin_user,
    name: str = API_KEY_NAME,
    key_file: Path = KEY_FILE,
    scopes: Optional[str] = "read,write",
    env_var: str = "SDK_CONTRACT_API_KEY",
) -> str:
    """Create (or keep) the named key and write its plaintext to *key_file*.

    An existing key is kept only when the file still holds its plaintext and
    its scopes are the ones asked for; otherwise it is replaced.
    """
    existing = (
        db.query(APIKey)
        .filter(APIKey.name == name, APIKey.user_id == admin_user.id)
        .first()
    )
    if existing is not None and key_file.exists():
        plaintext = key_file.read_text().strip()
        if (
            plaintext
            and hash_api_key(plaintext) == existing.key
            and existing.scopes == scopes
        ):
            print(f"  API key '{name}' kept ({_display(key_file)}).")
            return plaintext
        print(f"  API key '{name}' does not match its file or scopes; rotating.")
    if existing is not None:
        db.delete(existing)
        db.commit()

    plaintext = os.environ.get(env_var) or generate_api_key()
    db.add(
        APIKey(
            user_id=admin_user.id,
            key=hash_api_key(plaintext),
            name=name,
            description="Live SDK contract tests",
            scopes=scopes,
            is_active=True,
        )
    )
    db.commit()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(plaintext + "\n")
    os.chmod(key_file, 0o600)
    print(f"  Created API key '{name}', written to {_display(key_file)}.")
    return plaintext


def seed_local_api_key(db, admin_user) -> str:
    """The second key, scoped for the local-evaluation ruleset.

    The scope works only while the key's owner can change feature flags, so
    the seed refuses an owner who cannot rather than mint a key that 403s.
    """
    if not check_permission(admin_user, ResourceType.FEATURE_FLAG, Action.UPDATE):
        raise SystemExit(
            f"{admin_user.email} cannot change feature flags, so an sdk:ruleset "
            "key it owns would be refused; seed it as an ADMIN or DEVELOPER."
        )
    return seed_api_key(
        db,
        admin_user,
        name=LOCAL_API_KEY_NAME,
        key_file=LOCAL_KEY_FILE,
        scopes=LOCAL_API_KEY_SCOPES,
        env_var="SDK_CONTRACT_LOCAL_API_KEY",
    )


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def reset(db) -> None:
    exp = db.query(Experiment).filter(Experiment.key == EXPERIMENT_KEY).first()
    if exp:
        db.query(Event).filter(Event.experiment_id == exp.id).delete()
        db.query(Assignment).filter(Assignment.experiment_id == exp.id).delete()
        db.query(Metric).filter(Metric.experiment_id == exp.id).delete()
        db.query(Variant).filter(Variant.experiment_id == exp.id).delete()
        db.delete(exp)
    flag = db.query(FeatureFlag).filter(FeatureFlag.key == FLAG_KEY).first()
    if flag:
        db.query(Event).filter(Event.feature_flag_id == flag.id).delete()
        db.delete(flag)
    db.query(APIKey).filter(APIKey.name.in_([API_KEY_NAME, LOCAL_API_KEY_NAME])).delete(
        synchronize_session=False
    )
    db.commit()
    for key_file in (KEY_FILE, LOCAL_KEY_FILE):
        if key_file.exists():
            key_file.unlink()
    print("  Removed the SDK contract experiment, flag, API keys and key files.")


def main(argv: Optional[list[str]] = None) -> int:
    require_development_environment("sdk-contract")
    parser = argparse.ArgumentParser(description="Seed the live SDK contract fixtures.")
    parser.add_argument(
        "--reset", action="store_true", help="remove everything this script created"
    )
    args = parser.parse_args(argv)

    print("SDK contract seed")
    with SessionLocal() as db:
        ensure_schema(db)  # the schema must exist before create_all
    ensure_tables()
    with SessionLocal() as db:
        if args.reset:
            reset(db)
            return 0
        users = seed_users(db)
        admin = users["admin@demo.com"]
        seed_experiment(db, admin)
        seed_flag(db, admin)
        seed_api_key(db, admin)
        seed_local_api_key(db, admin)
    print("Done. Run: python tests/sdk-contract/live/run_live_contract.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
