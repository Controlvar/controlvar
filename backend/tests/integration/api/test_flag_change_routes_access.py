"""Who may change a feature flag through bulk-toggle and rollout schedules.

The same rule as the per-flag routes (test_feature_flag_ownership.py): ADMIN and
DEVELOPER may change any flag, ANALYST and VIEWER may change none -- not even a
flag they own -- and ownership never changes the answer.

Bulk-toggle is partial-success by design: it answers 200 and reports each flag
separately, so these tests assert each flag's ``success`` and re-read the row,
never just the status code.  A rollout schedule moves its flag's rollout
percentage, so every change to a schedule or a stage needs UPDATE on the flag.
"""

import uuid

import pytest

from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)
from backend.app.models.user import User, UserRole
from backend.tests.integration.conftest import HASHED_PASSWORD, make_client_for_user

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_db,
    pytest.mark.regression,
]

ACTORS = {
    "superuser": (UserRole.ADMIN, True),
    "admin": (UserRole.ADMIN, False),
    "developer": (UserRole.DEVELOPER, False),
    "analyst": (UserRole.ANALYST, False),
    "viewer": (UserRole.VIEWER, False),
}
READ_ONLY = ("analyst", "viewer")
OWNERSHIP = ["own", "other", "null"]


@pytest.fixture
def cleanup_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _remove_what_the_test_created(db_session):
    """Leave the shared test database as it was: the list tests page at 100."""
    yield
    db_session.rollback()
    flags = db_session.query(FeatureFlag.id).filter(FeatureFlag.key.like("fc93-%"))
    flag_ids = [row.id for row in flags]
    if flag_ids:
        schedules = db_session.query(RolloutSchedule.id).filter(
            RolloutSchedule.feature_flag_id.in_(flag_ids)
        )
        schedule_ids = [row.id for row in schedules]
        if schedule_ids:
            db_session.query(RolloutStage).filter(
                RolloutStage.rollout_schedule_id.in_(schedule_ids)
            ).delete(synchronize_session=False)
            db_session.query(RolloutSchedule).filter(
                RolloutSchedule.id.in_(schedule_ids)
            ).delete(synchronize_session=False)
        db_session.query(FeatureFlag).filter(FeatureFlag.id.in_(flag_ids)).delete(
            synchronize_session=False
        )
    db_session.commit()


def _user(db_session, role: UserRole, is_superuser: bool) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"fc93_{role.value}_{suffix}",
        email=f"fc93_{suffix}@int.test",
        full_name="fc93",
        hashed_password=HASHED_PASSWORD,
        is_active=True,
        is_superuser=is_superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _actor_and_flag(db_session, actor, ownership, status=FeatureFlagStatus.INACTIVE):
    role, su = ACTORS[actor]
    user = _user(db_session, role, su)
    other = _user(db_session, UserRole.DEVELOPER, False)
    owner_id = {"own": user.id, "other": other.id, "null": None}[ownership]
    flag = FeatureFlag(
        key=f"fc93-{uuid.uuid4().hex[:10]}",
        name="fc93",
        status=status,
        owner_id=owner_id,
        rollout_percentage=0,
    )
    db_session.add(flag)
    db_session.commit()
    db_session.refresh(flag)
    return user, flag


# ---------------------------------------------------------------------------
# bulk-toggle: 5 actors x 3 ownerships x 3 actions = 45
# ---------------------------------------------------------------------------

BULK_ACTIONS = {
    # action -> (status the flag starts in, status it must end in if allowed)
    "enable": (FeatureFlagStatus.INACTIVE, FeatureFlagStatus.ACTIVE),
    "disable": (FeatureFlagStatus.ACTIVE, FeatureFlagStatus.INACTIVE),
    "archive": (FeatureFlagStatus.INACTIVE, FeatureFlagStatus.ARCHIVED),
}
BULK_CASES = [
    pytest.param(a, o, act, id=f"{a}-{o}-{act}")
    for a in ACTORS
    for o in OWNERSHIP
    for act in BULK_ACTIONS
]
assert len(BULK_CASES) == 5 * 3 * 3 == 45


@pytest.mark.parametrize("actor,ownership,action", BULK_CASES)
def test_bulk_toggle_refuses_flags_the_user_cannot_update(
    db_session, cleanup_overrides, actor, ownership, action
):
    start, end = BULK_ACTIONS[action]
    user, flag = _actor_and_flag(db_session, actor, ownership, start)

    response = make_client_for_user(db_session, user).post(
        "/api/v1/feature-flags/bulk-toggle",
        json={"flag_ids": [str(flag.id)], "action": action},
    )
    assert response.status_code == 200, response.text
    (result,) = response.json()["results"]

    db_session.expire_all()
    stored = db_session.query(FeatureFlag).filter(FeatureFlag.id == flag.id).one()
    if actor in READ_ONLY:
        assert result["success"] is False, result
        assert stored.status == start, (
            f"{actor} was refused but the flag changed from {start} to "
            f"{stored.status} (the final commit covers refused flags too)"
        )
    else:
        assert result["success"] is True, result
        assert stored.status == end


# ---------------------------------------------------------------------------
# rollout schedules: every route that changes a schedule or a stage
# ---------------------------------------------------------------------------


def _schedule(db_session, flag, status=RolloutScheduleStatus.DRAFT):
    schedule = RolloutSchedule(
        name=f"fc93-{uuid.uuid4().hex[:6]}",
        feature_flag_id=flag.id,
        status=status,
        max_percentage=100,
        min_stage_duration=0,
    )
    db_session.add(schedule)
    db_session.commit()
    db_session.refresh(schedule)
    stage = RolloutStage(
        rollout_schedule_id=schedule.id,
        name="s1",
        stage_order=1,
        target_percentage=50,
        trigger_type=TriggerType.MANUAL,
        status=RolloutStageStatus.PENDING,
    )
    db_session.add(stage)
    db_session.commit()
    db_session.refresh(stage)
    return schedule, stage


_STAGE_BODY = {
    "name": "s2",
    "stage_order": 2,
    "target_percentage": 100,
    "trigger_type": "manual",
}

# route -> (schedule status to set up, method, path template, body, success code)
ROLLOUT_ROUTES = {
    "create": (None, "POST", "/", "create", 201),
    "update": (RolloutScheduleStatus.DRAFT, "PUT", "/{schedule}", {"name": "r"}, 200),
    "delete": (RolloutScheduleStatus.DRAFT, "DELETE", "/{schedule}", None, 204),
    "activate": (
        RolloutScheduleStatus.DRAFT,
        "POST",
        "/{schedule}/activate",
        None,
        200,
    ),
    "pause": (RolloutScheduleStatus.ACTIVE, "POST", "/{schedule}/pause", None, 200),
    "cancel": (RolloutScheduleStatus.DRAFT, "POST", "/{schedule}/cancel", None, 200),
    "add_stage": (
        RolloutScheduleStatus.DRAFT,
        "POST",
        "/{schedule}/stages",
        _STAGE_BODY,
        201,
    ),
    "update_stage": (
        RolloutScheduleStatus.DRAFT,
        "PUT",
        "/stages/{stage}",
        {"name": "t"},
        200,
    ),
    "delete_stage": (
        RolloutScheduleStatus.DRAFT,
        "DELETE",
        "/stages/{stage}",
        None,
        204,
    ),
    "advance_stage": (
        RolloutScheduleStatus.ACTIVE,
        "POST",
        "/stages/{stage}/advance",
        None,
        200,
    ),
}
assert len(ROLLOUT_ROUTES) == 10

ROLLOUT_CASES = [
    pytest.param(a, o, r, id=f"{a}-{o}-{r}")
    for a in ACTORS
    for o in OWNERSHIP
    for r in ROLLOUT_ROUTES
]
assert len(ROLLOUT_CASES) == 5 * 3 * 10 == 150


def _rollout_request(db_session, user, flag, route):
    setup, method, template, body, _ = ROLLOUT_ROUTES[route]
    client = make_client_for_user(db_session, user)
    if route == "create":
        body = {
            "name": "fc93 create",
            "feature_flag_id": str(flag.id),
            "max_percentage": 100,
            "stages": [dict(_STAGE_BODY, stage_order=1)],
        }
        return client.request(method, "/api/v1/rollout-schedules/", json=body), None
    schedule, stage = _schedule(db_session, flag, setup)
    path = template.format(schedule=schedule.id, stage=stage.id)
    return client.request(method, f"/api/v1/rollout-schedules{path}", json=body), (
        schedule,
        stage,
    )


@pytest.mark.parametrize("actor,ownership,route", ROLLOUT_CASES)
def test_rollout_schedule_changes_require_update_on_the_flag(
    db_session, cleanup_overrides, actor, ownership, route
):
    user, flag = _actor_and_flag(db_session, actor, ownership)
    response, _ = _rollout_request(db_session, user, flag, route)

    want = 403 if actor in READ_ONLY else ROLLOUT_ROUTES[route][4]
    assert response.status_code == want, (
        f"{actor} {route} on a flag owned by {ownership}: want {want}, "
        f"got {response.status_code}: {response.text}"
    )


@pytest.mark.parametrize("route", [r for r in ROLLOUT_ROUTES if r != "create"], ids=str)
def test_rollout_change_to_a_missing_schedule_or_stage_is_404(
    db_session, cleanup_overrides, route
):
    """A missing id answered 500 before: each route's except Exception
    swallowed its own 404."""
    user = _user(db_session, UserRole.DEVELOPER, False)
    _, method, template, body, _ = ROLLOUT_ROUTES[route]
    path = template.format(schedule=uuid.uuid4(), stage=uuid.uuid4())
    response = make_client_for_user(db_session, user).request(
        method, f"/api/v1/rollout-schedules{path}", json=body
    )
    assert response.status_code == 404, response.text


def test_rollout_schedule_for_a_missing_flag_is_404(db_session, cleanup_overrides):
    user = _user(db_session, UserRole.DEVELOPER, False)
    response = make_client_for_user(db_session, user).post(
        "/api/v1/rollout-schedules/",
        json={
            "name": "fc93 missing flag",
            "feature_flag_id": str(uuid.uuid4()),
            "max_percentage": 100,
            "stages": [dict(_STAGE_BODY, stage_order=1)],
        },
    )
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# A grant of feature_flag (update) covers any flag (#891, check #32, D11)
# ---------------------------------------------------------------------------
# In the full profile a VIEWER holding a direct grant of feature_flag (update)
# may change any flag -- their own, another user's, one with no owner -- on
# every route above, exactly as a DEVELOPER may; the same VIEWER whose grant has
# no activation time (recorded before custom roles took effect) may change none.
# The grants are planted before the request and never change during it, so
# make_client_for_user is acceptable here (and in this file only).

GRANT_ACTORS = {"viewer+grant": True, "viewer+inactive grant": False}


def _granted_viewer_and_flag(db_session, activated, ownership, status):
    from sqlalchemy import text

    user = _user(db_session, UserRole.VIEWER, False)
    db_session.execute(
        text(
            "INSERT INTO direct_permission_grants (id, user_id, resource, actions, "
            "is_active, activated_at, created_at, updated_at) VALUES (:id, :user, "
            "'feature_flag', CAST('[\"update\"]' AS jsonb), true, "
            + ("now()" if activated else "NULL")
            + ", now(), now())"
        ),
        {"id": uuid.uuid4(), "user": user.id},
    )
    other = _user(db_session, UserRole.DEVELOPER, False)
    owner_id = {"own": user.id, "other": other.id, "null": None}[ownership]
    flag = FeatureFlag(
        key=f"fc93-{uuid.uuid4().hex[:10]}",
        name="fc93",
        status=status,
        owner_id=owner_id,
        rollout_percentage=0,
    )
    db_session.add(flag)
    db_session.commit()
    db_session.refresh(flag)
    return user, flag


@pytest.fixture
def _remove_grants(db_session):
    yield
    from sqlalchemy import text

    db_session.rollback()
    db_session.execute(
        text(
            "DELETE FROM direct_permission_grants WHERE user_id IN "
            "(SELECT id FROM users WHERE username LIKE 'fc93_%')"
        )
    )
    db_session.commit()


GRANT_BULK_CASES = [
    pytest.param(a, o, act, id=f"{a}-{o}-{act}")
    for a in GRANT_ACTORS
    for o in OWNERSHIP
    for act in BULK_ACTIONS
]


@pytest.mark.modules
@pytest.mark.parametrize("actor,ownership,action", GRANT_BULK_CASES)
def test_bulk_toggle_follows_a_flag_update_grant(
    db_session, cleanup_overrides, _remove_grants, actor, ownership, action
):
    start, end = BULK_ACTIONS[action]
    user, flag = _granted_viewer_and_flag(
        db_session, GRANT_ACTORS[actor], ownership, start
    )
    response = make_client_for_user(db_session, user).post(
        "/api/v1/feature-flags/bulk-toggle",
        json={"flag_ids": [str(flag.id)], "action": action},
    )
    assert response.status_code == 200, response.text
    (result,) = response.json()["results"]
    db_session.expire_all()
    stored = db_session.query(FeatureFlag).filter(FeatureFlag.id == flag.id).one()
    if GRANT_ACTORS[actor]:
        assert result["success"] is True, f"{actor}, {ownership} owner: {result}"
        assert stored.status == end
    else:
        assert result["success"] is False, result
        assert stored.status == start


GRANT_ROLLOUT_CASES = [
    pytest.param(a, o, r, id=f"{a}-{o}-{r}")
    for a in GRANT_ACTORS
    for o in OWNERSHIP
    for r in ROLLOUT_ROUTES
]


@pytest.mark.modules
@pytest.mark.parametrize("actor,ownership,route", GRANT_ROLLOUT_CASES)
def test_rollout_schedule_changes_follow_a_flag_update_grant(
    db_session, cleanup_overrides, _remove_grants, actor, ownership, route
):
    user, flag = _granted_viewer_and_flag(
        db_session, GRANT_ACTORS[actor], ownership, FeatureFlagStatus.INACTIVE
    )
    response, _ = _rollout_request(db_session, user, flag, route)
    want = ROLLOUT_ROUTES[route][4] if GRANT_ACTORS[actor] else 403
    assert response.status_code == want, (
        f"{actor} {route} on a flag owned by {ownership}: want {want}, "
        f"got {response.status_code}: {response.text}"
    )
