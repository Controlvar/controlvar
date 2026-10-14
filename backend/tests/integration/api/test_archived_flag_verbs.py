"""An archived flag stays archived until it is unarchived (#631, Rule S).

Every route that changes a flag's status, from each of the three statuses,
through the real routes and a real database:

* a request that would turn an ARCHIVED flag on (activate, enable, toggle,
  PUT ``is_active: true``, bulk ``enable``) answers 400 with one fixed sentence
  and leaves the flag ARCHIVED;
* a request that turns a flag off (deactivate, disable, PUT
  ``is_active: false``, bulk ``disable``) succeeds on an ARCHIVED flag and
  changes nothing;
* ``POST /feature-flags/{id}/unarchive`` (beta) is the way out: ARCHIVED
  becomes INACTIVE, and anything else is returned unchanged;
* bulk ``enable`` refuses archived flags one by one and processes the rest.

Each case re-reads the row; a response alone proves nothing about the database.
The cases that were red on the code before #631 carry the regression marker
(see ``_red_on_main``).
"""

from __future__ import annotations

import uuid

import pytest

from backend.app.models.audit_log import AuditLog
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)
from backend.app.services.safety_service import SafetyService
from backend.tests.integration.conftest import make_client_for_user

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

COLLECTION = "/api/v1/feature-flags"

#: Typed out rather than imported, so a change to the constant fails here.
ARCHIVED_DETAIL = "This flag is archived. Unarchive it before turning it on."

ACTIVE = FeatureFlagStatus.ACTIVE
INACTIVE = FeatureFlagStatus.INACTIVE
ARCHIVED = FeatureFlagStatus.ARCHIVED

# verb -> (method, path suffix or "bulk", body)
VERBS = {
    "activate": ("POST", "/activate", None),
    "deactivate": ("POST", "/deactivate", None),
    "enable": ("POST", "/enable", {"reason": "631"}),
    "disable": ("POST", "/disable", {"reason": "631"}),
    "toggle": ("POST", "/toggle", {"reason": "631"}),
    "put_on": ("PUT", "", {"is_active": True}),
    "put_off": ("PUT", "", {"is_active": False}),
    "bulk_enable": ("POST", "bulk", {"action": "enable"}),
    "bulk_disable": ("POST", "bulk", {"action": "disable"}),
    "bulk_archive": ("POST", "bulk", {"action": "archive"}),
    "unarchive": ("POST", "/unarchive", None),
}

REFUSED = "refused"  # 400 (or a bulk item with success: false), flag unchanged

# verb -> {start status: the status it ends in, or REFUSED}
EXPECTED = {
    "activate": {ACTIVE: ACTIVE, INACTIVE: ACTIVE, ARCHIVED: REFUSED},
    "deactivate": {ACTIVE: INACTIVE, INACTIVE: INACTIVE, ARCHIVED: ARCHIVED},
    "enable": {ACTIVE: ACTIVE, INACTIVE: ACTIVE, ARCHIVED: REFUSED},
    "disable": {ACTIVE: INACTIVE, INACTIVE: INACTIVE, ARCHIVED: ARCHIVED},
    "toggle": {ACTIVE: INACTIVE, INACTIVE: ACTIVE, ARCHIVED: REFUSED},
    "put_on": {ACTIVE: ACTIVE, INACTIVE: ACTIVE, ARCHIVED: REFUSED},
    "put_off": {ACTIVE: INACTIVE, INACTIVE: INACTIVE, ARCHIVED: ARCHIVED},
    "bulk_enable": {ACTIVE: ACTIVE, INACTIVE: ACTIVE, ARCHIVED: REFUSED},
    "bulk_disable": {ACTIVE: INACTIVE, INACTIVE: INACTIVE, ARCHIVED: ARCHIVED},
    "bulk_archive": {ACTIVE: ARCHIVED, INACTIVE: ARCHIVED, ARCHIVED: ARCHIVED},
    "unarchive": {ACTIVE: ACTIVE, INACTIVE: INACTIVE, ARCHIVED: INACTIVE},
}


def _red_on_main(verb: str, start: FeatureFlagStatus) -> bool:
    """Measured on the code before #631 (b16cd837).

    Bulk archive of an archived flag was already a no-op, and PUT
    ``is_active: false`` already kept it archived (#706); every other verb
    moved an archived flag out of ARCHIVED.
    """
    if verb == "unarchive":
        return True  # the route did not exist
    return start is ARCHIVED and verb not in ("bulk_archive", "put_off")


CASES = [
    pytest.param(
        verb,
        start,
        id=f"{verb}-{start.value.lower()}",
        marks=[pytest.mark.regression] if _red_on_main(verb, start) else [],
    )
    for verb in VERBS
    for start in (ACTIVE, INACTIVE, ARCHIVED)
]
assert len(CASES) == 11 * 3


def _row(db_session, flag_id) -> FeatureFlag:
    db_session.expire_all()
    return db_session.get(FeatureFlag, uuid.UUID(str(flag_id)))


def _send(client, verb: str, flag_ids: list):
    method, suffix, body = VERBS[verb]
    if suffix == "bulk":
        return client.post(
            f"{COLLECTION}/bulk-toggle",
            json={**body, "flag_ids": [str(i) for i in flag_ids]},
        )
    (flag_id,) = flag_ids
    return client.request(method, f"{COLLECTION}/{flag_id}{suffix}", json=body)


@pytest.mark.parametrize("verb, start", CASES)
def test_every_status_verb_from_every_status(
    developer_client, db_session, make_feature_flag, verb, start
):
    """A non-superuser DEVELOPER, so no superuser shortcut is in play."""
    flag = make_feature_flag(status=start)
    before = _row(db_session, flag.id).updated_at
    want = EXPECTED[verb][start]

    response = _send(developer_client, verb, [flag.id])

    stored = _row(db_session, flag.id)
    bulk = VERBS[verb][1] == "bulk"
    if want is REFUSED:
        if bulk:
            assert response.status_code == 200, response.text
            (result,) = response.json()["results"]
            assert result["success"] is False, result
            assert result["error"] == ARCHIVED_DETAIL
        else:
            assert response.status_code == 400, response.text
            assert response.json()["detail"] == ARCHIVED_DETAIL
        assert stored.status is ARCHIVED
        assert stored.updated_at == before, "a refused request wrote the row"
        return

    assert response.status_code == 200, response.text
    assert stored.status is want, f"{verb} from {start}: stored {stored.status}"
    if bulk:
        (result,) = response.json()["results"]
        assert result["success"] is True, result
        assert result["new_status"].upper() == want.value
    else:
        assert response.json()["status"].upper() == want.value, response.json()
    if start is ARCHIVED and want is ARCHIVED:
        # An off-verb on an archived flag succeeds and changes nothing.
        assert stored.updated_at == before, f"{verb} rewrote an archived flag"


# --- bulk: a mixed batch ------------------------------------------------------


@pytest.mark.regression
def test_bulk_enable_refuses_only_the_archived_flag(
    developer_client, db_session, make_feature_flag
):
    """[ACTIVE, ARCHIVED, INACTIVE] with enable: 200, item 2 refused alone."""
    flags = [make_feature_flag(status=s) for s in (ACTIVE, ARCHIVED, INACTIVE)]

    response = _send(developer_client, "bulk_enable", [f.id for f in flags])

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["total"], body["succeeded"], body["failed"]) == (3, 2, 1)
    assert len(body["audit_log_ids"]) == 2
    first, second, third = body["results"]
    assert first["success"] is True and third["success"] is True
    assert second == {
        "flag_id": str(flags[1].id),
        "flag_key": flags[1].key,
        "success": False,
        "old_status": None,
        "new_status": None,
        "error": ARCHIVED_DETAIL,
    }
    assert [_row(db_session, f.id).status for f in flags] == [
        ACTIVE,
        ARCHIVED,
        ACTIVE,
    ]


# --- unarchive ----------------------------------------------------------------


@pytest.mark.regression
def test_unarchive_is_the_way_back(developer_client, db_session, make_feature_flag):
    """ARCHIVED -> INACTIVE (never ACTIVE), audited; then it can be turned on."""
    flag = make_feature_flag(status=ARCHIVED)

    response = developer_client.post(f"{COLLECTION}/{flag.id}/unarchive")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "inactive"
    assert response.json()["is_active"] is False
    assert _row(db_session, flag.id).status is INACTIVE

    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.entity_id == flag.id)
        .order_by(AuditLog.timestamp.desc())
        .first()
    )
    assert audit is not None, "unarchive wrote no audit entry"
    assert (audit.old_value, audit.new_value) == ("ARCHIVED", "INACTIVE")
    assert audit.action_type == "feature_flag_update"

    on = developer_client.post(f"{COLLECTION}/{flag.id}/activate")
    assert on.status_code == 200, on.text
    assert _row(db_session, flag.id).status is ACTIVE


@pytest.mark.parametrize("status", [ACTIVE, INACTIVE], ids=["active", "inactive"])
def test_unarchive_returns_a_flag_that_is_not_archived_unchanged(
    developer_client, db_session, make_feature_flag, status
):
    flag = make_feature_flag(status=status, rollout_percentage=40)

    response = developer_client.post(f"{COLLECTION}/{flag.id}/unarchive")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == status.value.lower()
    stored = _row(db_session, flag.id)
    assert (stored.status, stored.rollout_percentage) == (status, 40)


@pytest.mark.regression
@pytest.mark.parametrize(
    "fixture, code",
    [
        ("developer_user", 200),
        ("analyst_user", 403),
        ("viewer_user", 403),
    ],
)
def test_unarchive_is_permission_checked(
    request, db_session, make_feature_flag, fixture, code
):
    """By role, through can_act_on_feature_flag; a refused call changes nothing."""
    user = request.getfixturevalue(fixture)
    assert not user.is_superuser
    flag = make_feature_flag(status=ARCHIVED, owner_id=user.id)

    response = make_client_for_user(db_session, user).post(
        f"{COLLECTION}/{flag.id}/unarchive"
    )

    assert response.status_code == code, response.text
    assert _row(db_session, flag.id).status is (INACTIVE if code == 200 else ARCHIVED)


def test_unarchive_an_unknown_flag_is_404(developer_client):
    response = developer_client.post(f"{COLLECTION}/{uuid.uuid4()}/unarchive")
    assert response.status_code == 404, response.text


# --- the percentage writers on an archived flag --------------------------------
#
# Rule S is about status. A safety rollback never writes a flag that is not
# ACTIVE (#629): an archived flag already serves no one, so the rollback
# changes nothing and says why. The rollout stage advance is unchanged on an
# archived flag (#720); the model guard must not refuse it.


def test_a_safety_rollback_of_an_archived_flag_changes_nothing(
    db_session, make_feature_flag
):
    flag = make_feature_flag(status=ARCHIVED, rollout_percentage=50)

    result = SafetyService(db_session).execute_rollback(
        db_session, flag.id, reason="631", target_percentage=0
    )

    assert result.success is False, result
    assert "is archived; nothing to roll back" in result.message
    stored = _row(db_session, flag.id)
    assert (stored.status, stored.rollout_percentage) == (ARCHIVED, 50)


def test_a_stage_advance_on_an_archived_flag_is_not_refused(
    admin_client, db_session, make_feature_flag
):
    flag = make_feature_flag(status=ARCHIVED, rollout_percentage=10)
    schedule = RolloutSchedule(
        name=f"b631-{uuid.uuid4().hex[:6]}",
        feature_flag_id=flag.id,
        status=RolloutScheduleStatus.ACTIVE,
        max_percentage=100,
        min_stage_duration=0,
    )
    db_session.add(schedule)
    db_session.commit()
    stage = RolloutStage(
        rollout_schedule_id=schedule.id,
        name="stage 1",
        stage_order=1,
        target_percentage=80,
        trigger_type=TriggerType.MANUAL,
        status=RolloutStageStatus.PENDING,
    )
    db_session.add(stage)
    db_session.commit()

    response = admin_client.post(f"/api/v1/rollout-schedules/stages/{stage.id}/advance")

    assert response.status_code == 200, response.text
    stored = _row(db_session, flag.id)
    assert (stored.status, stored.rollout_percentage) == (ARCHIVED, 80)
