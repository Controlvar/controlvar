#!/usr/bin/env python3
"""
Seed the fixtures used by the live SDK contract tests.

Creates (idempotently):
- the demo users (admin@demo.com owns everything),
- an ACTIVE A/B experiment with public key ``sdk_contract_ab`` (variants
  ``control`` / ``treatment`` at 50/50, primary metric ``purchase``),
- an ACTIVE feature flag ``sdk_contract_flag`` at 100% rollout,
- one ACTIVE flag per value type at 100% rollout (#1497):
  ``sdk_contract_value_string``, ``sdk_contract_value_number`` and
  ``sdk_contract_value_json``, each with plain-word values; the live contract
  turns them off and on again through the API to show a published SDK's
  local evaluator receives the change (an existing one is turned back on),
- an ACTIVE flag ``sdk_contract_targeted`` at 0% rollout whose one rule,
  ``plan`` equals ``pro``, is at 100% (#1520): the smokes of the SDKs in the
  runner's ``ATTRIBUTE_SDKS`` evaluate it with ``{"plan": "pro"}`` (on, reason
  ``targeting_rule``) and ``{"plan": "free"}`` (off), so it is on only when
  the SDK sends the attributes (an existing one is put back to that state),
- two ACTIVE string flags with the string flag's values (#2012; an existing
  one is put back to that state): ``sdk_contract_multiway``, on at 100% with
  a split of three values, ``red``, ``green`` and ``blue``, weighted 34/33/33,
  and ``sdk_contract_rule_value``, at 0% with the targeted flag's rule
  (``plan`` equals ``pro``) serving a value of its own, ``pro-copy``.
  ``multiway_users()`` picks three users, one per value of the split, with
  the server's own split function; the runner gives them to the smokes of the
  SDKs in its ``MULTIWAY_SDKS``,
- an API key named ``sdk-contract-smoke``, with no ``sdk:ruleset`` scope,
  whose plaintext is written to ``tests/sdk-contract/live/.api_key``;
- a second key of the same user, ``sdk-contract-local``, with the
  ``sdk:ruleset`` scope (server-side local evaluation), written to
  ``tests/sdk-contract/live/.api_key_local``;
- the same two keys for the staging environment (#2011),
  ``sdk-contract-smoke-staging`` and ``sdk-contract-local-staging``, written to
  ``tests/sdk-contract/live/.api_key_staging`` and ``.api_key_local_staging``,
  and staging's settings for two of the flags, each the other way round from
  production: ``sdk_contract_flag`` off, ``sdk_contract_targeted`` on at 100%
  with no rule.

The files are 0600 and gitignored. Two keys of each environment, so the live
contract can show the ruleset endpoint refusing the first and serving the
second. Every SDK smoke uses the first two, which are production keys.

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
keys, ``SDK_CONTRACT_STAGING_API_KEY`` and
``SDK_CONTRACT_LOCAL_STAGING_API_KEY`` the staging ones (otherwise they are
generated); ``SDK_CONTRACT_KEY_DIR`` redirects the key files (containers).
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

from backend.app.core.flag_environments import PRODUCTION, STAGING
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
from backend.app.models.feature_flag import (
    FeatureFlag,
    FeatureFlagStatus,
    FlagEnvironmentSetting,
)
from backend.scripts.seed_demo_data import (
    ensure_schema,
    ensure_tables,
    now_utc,
    seed_users,
)
from backend.scripts.seed_guard import require_development_environment

EXPERIMENT_KEY = "sdk_contract_ab"
FLAG_KEY = "sdk_contract_flag"
#: One flag per value type (#1497): its key, and its on and off values (plain
#: words, nothing the leak scan reads as a credential).
VALUE_FLAGS = {
    "string": ("sdk_contract_value_string", "on-copy", "off-copy"),
    "number": ("sdk_contract_value_number", 10, 2.5),
    "json": ("sdk_contract_value_json", {"color": "blue"}, ["fallback"]),
}
#: Off for everyone (0%) except through its one rule, plan equals pro, at 100%
#: (#1520): only an SDK that sends the attributes on flag evaluation gets it on.
TARGETED_FLAG_KEY = "sdk_contract_targeted"
#: Dashboard shape (what the targeting editor writes).
TARGETED_FLAG_RULES = {
    "logical_operator": "AND",
    "rollout_percentage": 100,
    "groups": [
        {
            "id": "grp-plan-pro",
            "logical_operator": "AND",
            "conditions": [
                {
                    "id": "cond-plan",
                    "attribute": "plan",
                    "operator": "equals",
                    "value": "pro",
                }
            ],
        }
    ],
}
#: A string flag with a split (#2012): on at 100%, each user served one of
#: three values by weight. :func:`multiway_users` picks the users the smokes
#: read it for, one per value, with the server's own split function.
MULTIWAY_FLAG_KEY = "sdk_contract_multiway"
MULTIWAY_SPLIT = [
    {"value": "red", "weight": 34},
    {"value": "green", "weight": 33},
    {"value": "blue", "weight": 33},
]
#: A string flag off for everyone (0%) except through its one rule, plan
#: equals pro at 100%, which serves a value of its own (#2012).
RULE_VALUE_FLAG_KEY = "sdk_contract_rule_value"
RULE_VALUE = "pro-copy"
RULE_VALUE_FLAG_RULES = {**TARGETED_FLAG_RULES, "value": RULE_VALUE}
#: What an existing one of the two is put back to.
MULTIWAY_FIELDS = (
    "status",
    "value_type",
    "on_value",
    "off_value",
    "rollout_percentage",
    "targeting_rules",
    "split",
)
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
#: The staging environment's two keys (#2011), and what it serves for the two
#: flags it configures: ``(is_active, rollout_percentage)``, each the other way
#: round from production, so a key that answers production's is seen.
STAGING_API_KEY_NAME = "sdk-contract-smoke-staging"
STAGING_LOCAL_API_KEY_NAME = "sdk-contract-local-staging"
STAGING_KEY_FILE = KEY_DIR / ".api_key_staging"
STAGING_LOCAL_KEY_FILE = KEY_DIR / ".api_key_local_staging"
STAGING_SETTINGS = {FLAG_KEY: (False, 0), TARGETED_FLAG_KEY: (True, 100)}


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


def seed_value_flags(db, admin_user) -> list[FeatureFlag]:
    """One ACTIVE flag per value type, at 100%.

    An existing one is kept and turned back on at 100% (the live contract
    turns them off and on again; a run stopped in between leaves one off).
    """
    flags = []
    for value_type, (key, on_value, off_value) in VALUE_FLAGS.items():
        flag = db.query(FeatureFlag).filter(FeatureFlag.key == key).first()
        if flag is not None:
            flag.status = FeatureFlagStatus.ACTIVE
            flag.rollout_percentage = 100
            db.commit()
            print(f"  Flag '{key}' already exists (id={flag.id}); on at 100%.")
            flags.append(flag)
            continue
        flag = FeatureFlag(
            key=key,
            name=f"SDK contract {value_type} flag",
            description=f"A {value_type} flag for the live SDK contract tests.",
            status=FeatureFlagStatus.ACTIVE,
            owner_id=admin_user.id,
            rollout_percentage=100,
            targeting_rules={},
            tags=["sdk-contract"],
            value_type=value_type,
            on_value=on_value,
            off_value=off_value,
        )
        db.add(flag)
        db.commit()
        db.refresh(flag)
        print(f"  Created {value_type} flag '{key}' at 100% (id={flag.id}).")
        flags.append(flag)
    return flags


def multiway_users() -> dict[str, str]:
    """Three users of the multi-way flag, each served a different value:
    ``{user_id: value}``, picked by the server's own split function
    (``flag_values.split_bucket`` and ``split_value``), the first of
    ``mw-user-0``, ``mw-user-1``, ... to get each value."""
    from backend.app.core.flag_values import split_bucket, split_value

    picked: dict[str, str] = {}
    for n in range(1000):
        user = f"mw-user-{n}"
        value = split_value(MULTIWAY_SPLIT, split_bucket(user, MULTIWAY_FLAG_KEY))
        if value not in picked.values():
            picked[user] = value
        if len(picked) == len(MULTIWAY_SPLIT):
            return picked
    raise RuntimeError("no three users get the three values of the split")


def seed_multiway_flags(db, admin_user) -> list[FeatureFlag]:
    """The flag with a split and the flag whose rule serves a value of its own,
    both string flags with the seed's string values (#2012), written through
    the model as the other flags are. An existing one is put back to exactly
    that."""
    _, on_value, off_value = VALUE_FLAGS["string"]
    common = {
        "description": "A multi-way flag for the live SDK contract tests.",
        "status": FeatureFlagStatus.ACTIVE,
        "owner_id": admin_user.id,
        "tags": ["sdk-contract"],
        "value_type": "string",
        "on_value": on_value,
        "off_value": off_value,
    }
    wanted = [
        FeatureFlag(
            key=MULTIWAY_FLAG_KEY,
            name="SDK contract multi-way flag",
            rollout_percentage=100,
            targeting_rules={},
            split=MULTIWAY_SPLIT,
            **common,
        ),
        FeatureFlag(
            key=RULE_VALUE_FLAG_KEY,
            name="SDK contract rule value flag",
            rollout_percentage=0,
            targeting_rules=RULE_VALUE_FLAG_RULES,
            split=None,
            **common,
        ),
    ]
    flags = []
    for new in wanted:
        flag = db.query(FeatureFlag).filter(FeatureFlag.key == new.key).first()
        if flag is None:
            db.add(new)
            flag = new
        else:
            for field in MULTIWAY_FIELDS:
                setattr(flag, field, getattr(new, field))
        db.commit()
        db.refresh(flag)
        print(
            f"  Flag '{flag.key}' active at {flag.rollout_percentage}% (id={flag.id})."
        )
        flags.append(flag)
    return flags


def seed_targeted_flag(db, admin_user) -> FeatureFlag:
    """The flag only a rule on ``plan`` turns on: ACTIVE, 0%, plan=pro at 100%.

    An existing one is put back to exactly that, so a run judges the SDKs and
    not an edit made to the fixture since.
    """
    flag = db.query(FeatureFlag).filter(FeatureFlag.key == TARGETED_FLAG_KEY).first()
    if flag is not None:
        flag.status = FeatureFlagStatus.ACTIVE
        flag.rollout_percentage = 0
        flag.targeting_rules = TARGETED_FLAG_RULES
        db.commit()
        print(
            f"  Flag '{TARGETED_FLAG_KEY}' already exists (id={flag.id}); "
            "0%, rule plan=pro at 100%."
        )
        return flag
    flag = FeatureFlag(
        key=TARGETED_FLAG_KEY,
        name="SDK contract targeted flag",
        description="Off except for plan=pro: the live SDK contract checks attributes reach flag evaluation.",
        status=FeatureFlagStatus.ACTIVE,
        owner_id=admin_user.id,
        rollout_percentage=0,
        targeting_rules=TARGETED_FLAG_RULES,
        tags=["sdk-contract"],
    )
    db.add(flag)
    db.commit()
    db.refresh(flag)
    print(
        f"  Created flag '{TARGETED_FLAG_KEY}' at 0%, rule plan=pro at 100% (id={flag.id})."
    )
    return flag


def seed_staging_settings(db) -> None:
    """Staging's settings for the flags in :data:`STAGING_SETTINGS` (#2011).

    Existing ones are put back to exactly that. The rows are written here
    through the model, as the flags are.
    """
    for key, (is_active, percentage) in STAGING_SETTINGS.items():
        flag = db.query(FeatureFlag).filter(FeatureFlag.key == key).one()
        setting = (
            db.query(FlagEnvironmentSetting)
            .filter(
                FlagEnvironmentSetting.feature_flag_id == flag.id,
                FlagEnvironmentSetting.environment == STAGING,
            )
            .first()
        )
        if setting is None:
            setting = FlagEnvironmentSetting(
                feature_flag_id=flag.id, environment=STAGING
            )
            db.add(setting)
        setting.is_active = is_active
        setting.rollout_percentage = percentage
        setting.targeting_rules = None
        state = f"on at {percentage}%" if is_active else "off"
        print(f"  Flag '{key}' in staging: {state}.")
    db.commit()


def seed_api_key(
    db,
    admin_user,
    name: str = API_KEY_NAME,
    key_file: Path = KEY_FILE,
    scopes: Optional[str] = "read,write",
    env_var: str = "SDK_CONTRACT_API_KEY",
    environment: str = PRODUCTION,
) -> str:
    """Create (or keep) the named key and write its plaintext to *key_file*.

    An existing key is kept only when the file still holds its plaintext and
    its scopes and environment are the ones asked for; otherwise it is
    replaced.
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
            and existing.environment == environment
        ):
            print(f"  API key '{name}' kept ({_display(key_file)}).")
            return plaintext
        print(
            f"  API key '{name}' does not match its file, scopes or environment; "
            "rotating."
        )
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
            environment=environment,
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


def seed_staging_api_keys(db, admin_user) -> None:
    """The staging environment's two keys, with the two keys' own scopes."""
    seed_api_key(
        db,
        admin_user,
        name=STAGING_API_KEY_NAME,
        key_file=STAGING_KEY_FILE,
        env_var="SDK_CONTRACT_STAGING_API_KEY",
        environment=STAGING,
    )
    seed_api_key(
        db,
        admin_user,
        name=STAGING_LOCAL_API_KEY_NAME,
        key_file=STAGING_LOCAL_KEY_FILE,
        scopes=LOCAL_API_KEY_SCOPES,
        env_var="SDK_CONTRACT_LOCAL_STAGING_API_KEY",
        environment=STAGING,
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
    for key in [
        FLAG_KEY,
        *(key for key, _, _ in VALUE_FLAGS.values()),
        TARGETED_FLAG_KEY,
        MULTIWAY_FLAG_KEY,
        RULE_VALUE_FLAG_KEY,
    ]:
        flag = db.query(FeatureFlag).filter(FeatureFlag.key == key).first()
        if flag:
            db.query(Event).filter(Event.feature_flag_id == flag.id).delete()
            db.delete(flag)
    db.query(APIKey).filter(
        APIKey.name.in_(
            [
                API_KEY_NAME,
                LOCAL_API_KEY_NAME,
                STAGING_API_KEY_NAME,
                STAGING_LOCAL_API_KEY_NAME,
            ]
        )
    ).delete(synchronize_session=False)
    db.commit()
    for key_file in (
        KEY_FILE,
        LOCAL_KEY_FILE,
        STAGING_KEY_FILE,
        STAGING_LOCAL_KEY_FILE,
    ):
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
        seed_value_flags(db, admin)
        seed_targeted_flag(db, admin)
        seed_multiway_flags(db, admin)
        for user, value in multiway_users().items():
            print(f"  '{MULTIWAY_FLAG_KEY}' serves {user} {value!r}.")
        seed_staging_settings(db)
        seed_api_key(db, admin)
        seed_local_api_key(db, admin)
        seed_staging_api_keys(db, admin)
    print("Done. Run: python tests/sdk-contract/live/run_live_contract.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
