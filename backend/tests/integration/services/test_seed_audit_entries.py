"""The demo seeds write audit entries only in the form the platform writes them (#221).

* Every action a seed names is in ``WRITTEN_ACTION_TYPES``, and none is a
  safety rollback (StreamPulse's rollout story produces a real one).
* No seed builds an ``AuditLog`` itself: every entry goes through
  ``seed_audit.seeded_entry``, which calls the platform's own writer, and
  ``seeded_entry`` refuses an action the platform does not write.
* Run against Postgres, each seed writes entries whose values have the shape
  the routes write, and a second run adds none.
"""

from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from backend.app.models.audit_log import ActionType, AuditLog, EntityType
from backend.app.models.experiment import ExperimentStatus
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.models.user import User, UserRole
from backend.app.services.audit_service import (
    AUDIT_VALUE_FIELDS,
    WRITTEN_ACTION_TYPES,
)
from backend.scripts import seed_audit, seed_demo_data, seed_streampulse
from backend.tests.integration.conftest import HASHED_PASSWORD

pytestmark = [pytest.mark.integration]

SCRIPTS = Path(seed_audit.__file__).resolve().parent
SEEDS = ("seed_demo_data.py", "seed_streampulse.py", "seed_audit.py")


def _action_names(source: str):
    """Every ``ActionType.<NAME>`` a module names."""
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "ActionType"
        ):
            yield node.attr


def seed_problems(sources):
    """What is wrong with the seeds' audit writes; [] when nothing is."""
    found = []
    for name, source in sources.items():
        for attr in _action_names(source):
            action = getattr(ActionType, attr, None)
            if action is None:
                found.append(f"{name}: ActionType.{attr} does not exist")
            elif action not in WRITTEN_ACTION_TYPES and name != "seed_audit.py":
                found.append(f"{name}: seeds {action.value}, which nothing writes")
            elif action is ActionType.SAFETY_ROLLBACK and name != "seed_audit.py":
                found.append(f"{name}: seeds a safety rollback")
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AuditLog"
            ):
                found.append(f"{name}:{node.lineno}: builds an AuditLog itself")
    return found


def test_the_seeds_name_only_written_actions_and_use_the_writer():
    sources = {name: (SCRIPTS / name).read_text(encoding="utf-8") for name in SEEDS}
    assert seed_problems(sources) == []


def test_the_check_names_an_unwritten_action_and_a_hand_built_entry():
    planted = {
        "seed_x.py": (
            "a = ActionType.USER_LOGOUT\n"
            "b = ActionType.SAFETY_ROLLBACK\n"
            "c = AuditLog(user_email='x')\n"
        )
    }
    assert seed_problems(planted) == [
        "seed_x.py: seeds user_logout, which nothing writes",
        "seed_x.py: seeds a safety rollback",
        "seed_x.py:3: builds an AuditLog itself",
    ]


@pytest.mark.parametrize(
    "action",
    [ActionType.USER_LOGOUT, ActionType.PERMISSION_GRANT, ActionType.SAFETY_ROLLBACK],
)
def test_seeded_entry_refuses_what_the_platform_does_not_write(
    db_session, admin_user, action
):
    with pytest.raises(seed_audit.UnwrittenActionError):
        seed_audit.seeded_entry(
            db_session,
            at=seed_demo_data.days_ago(1),
            actor=admin_user,
            action=action,
            entity_type=EntityType.USER,
            entity_id=admin_user.id,
            entity_name="x",
        )
    db_session.rollback()


def _user(db_session: Session, role: UserRole) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"seed_{role.name.lower()}_{suffix}",
        email=f"seed_{suffix}@int.test",
        hashed_password=HASHED_PASSWORD,
        is_active=True,
        is_superuser=False,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _entries(db_session, user_ids, entity_ids):
    db_session.expire_all()
    return (
        db_session.query(AuditLog)
        .filter((AuditLog.user_id.in_(user_ids)) | (AuditLog.entity_id.in_(entity_ids)))
        .all()
    )


def test_the_demo_seed_writes_the_platform_shapes_once(
    db_session, make_feature_flag, make_experiment
):
    admin = _user(db_session, UserRole.ADMIN)
    dev = _user(db_session, UserRole.DEVELOPER)
    on = make_feature_flag(status=FeatureFlagStatus.ACTIVE, name="Seed on")
    off = make_feature_flag(name="Seed off")
    exp = make_experiment(
        name=f"Seed exp {uuid.uuid4().hex[:6]}", bayesian_enabled=True
    )
    users = {"admin@demo.com": admin, "dev@demo.com": dev}
    flags = {on.key: on, off.key: off}

    seed_demo_data.seed_audit_logs(db_session, users, flags, [exp])
    ids = ([admin.id, dev.id], [on.id, off.id, exp.id])
    rows = _entries(db_session, *ids)

    assert {r.action_type for r in rows} <= {a.value for a in WRITTEN_ACTION_TYPES}
    by_action = {}
    for r in rows:
        by_action.setdefault(r.action_type, []).append(r)
    assert sorted(by_action) == sorted(
        a.value
        for a in (
            ActionType.USER_LOGIN,
            ActionType.EXPERIMENT_CREATE,
            ActionType.EXPERIMENT_UPDATE,
            ActionType.EXPERIMENT_START,
            ActionType.EXPERIMENT_PAUSE,
            ActionType.EXPERIMENT_COMPLETE,
            ActionType.FEATURE_FLAG_CREATE,
            ActionType.TOGGLE_ENABLE,
            ActionType.ROLE_ASSIGN,
        )
    )
    # The shapes the routes write.
    login = by_action["user_login"][0]
    assert json.loads(login.new_value) == {"provider": "local"}
    assert login.entity_name == login_user_name(login, admin, dev)
    (role,) = by_action["role_assign"]
    assert json.loads(role.old_value) == {"role": "VIEWER", "is_superuser": False}
    assert json.loads(role.new_value) == {"role": "DEVELOPER", "is_superuser": False}
    (created,) = by_action["experiment_create"]
    assert set(json.loads(created.new_value)) == set(
        AUDIT_VALUE_FIELDS[EntityType.EXPERIMENT]
    )
    # #848: the experiment's own switches, as values (Bayesian is on).
    created_value = json.loads(created.new_value)
    assert {
        field: created_value[field]
        for field in ("sequential_testing_enabled", "bayesian_enabled")
    } == {"sequential_testing_enabled": False, "bayesian_enabled": True}
    (started,) = by_action["experiment_start"]
    assert json.loads(started.old_value) == {"status": ExperimentStatus.DRAFT.value}
    assert json.loads(started.new_value) == {"status": ExperimentStatus.ACTIVE.value}
    (updated,) = by_action["experiment_update"]
    assert updated.old_value is None
    assert json.loads(updated.new_value) == {"changed_fields": ["description"]}
    (toggled,) = by_action["toggle_enable"]
    assert toggled.entity_id == on.id
    assert (toggled.old_value, toggled.new_value) == ("INACTIVE", "ACTIVE")
    assert len(by_action["feature_flag_create"]) == 2
    # Every entry carries its actor's own email.
    assert all(r.user_email in (admin.email, dev.email) for r in rows)

    seed_demo_data.seed_audit_logs(db_session, users, flags, [exp])
    assert len(_entries(db_session, *ids)) == len(rows)


def login_user_name(row, *users):
    for user in users:
        if user.id == row.entity_id:
            return user.username
    return None


def test_the_streampulse_seed_writes_the_platform_shapes_once(
    db_session, admin_user, make_experiment
):
    exp = make_experiment(name=f"Upsell {uuid.uuid4().hex[:6]}")
    assert seed_streampulse.seed_audit_logs(db_session, admin_user, exp) == 3
    rows = _entries(db_session, [], [exp.id])
    assert sorted(r.action_type for r in rows) == [
        "experiment_create",
        "experiment_start",
        "experiment_update",
    ]
    assert all(r.user_id == admin_user.id for r in rows)
    assert seed_streampulse.seed_audit_logs(db_session, admin_user, exp) == 0
    assert len(_entries(db_session, [], [exp.id])) == 3
