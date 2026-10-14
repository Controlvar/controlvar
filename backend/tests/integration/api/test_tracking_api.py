"""
Integration tests for the SDK-facing tracking API (/api/v1/tracking/*).

These endpoints resolve experiments by their public ``key`` and persist
assignments and events through AssignmentService / EventService against the
real test database.  Every assertion is scoped to rows created by the test
(unique keys, user ids and experiment ids) because the shared test database is
not truncated between tests.
"""

import datetime as dt
import json
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import text, update
from sqlalchemy.orm import Session

from backend.app.models.assignment import Assignment
from backend.app.models.bandit_state import BanditState
from backend.app.models.event import Event
from backend.app.models.experiment import ExperimentStatus, ExperimentType, Variant
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.holdout_population import HoldoutPopulation
from backend.app.models.mutual_exclusion_group import (
    MutualExclusionGroup,
    MutualExclusionGroupStatus,
)
from backend.app.services.global_holdout_service import (
    GlobalHoldoutService,
    holdout_bucket,
)


def _cleanup_experiment_rows(db_session, experiment) -> None:
    """Remove rows an experiment produced so later tests see a clean slate."""
    db_session.rollback()
    db_session.query(BanditState).filter(
        BanditState.experiment_id == experiment.id
    ).delete()
    db_session.query(Event).filter(Event.experiment_id == experiment.id).delete()
    db_session.query(Assignment).filter(
        Assignment.experiment_id == experiment.id
    ).delete()
    db_session.commit()


@pytest.fixture
def active_experiment(db_session, make_experiment):
    """An ACTIVE experiment with a control and a treatment variant."""
    suffix = uuid.uuid4().hex[:8]
    experiment = make_experiment(
        name=f"Tracking API {suffix}",
        key=f"tracking-api-{suffix}",
        status=ExperimentStatus.ACTIVE,
    )
    for name, is_control in (("control", True), ("treatment", False)):
        db_session.add(
            Variant(
                experiment_id=experiment.id,
                name=name,
                description=f"{name} variant",
                is_control=is_control,
                traffic_allocation=50,
                configuration={"color": name},
            )
        )
    db_session.commit()
    db_session.refresh(experiment)
    yield experiment
    _cleanup_experiment_rows(db_session, experiment)


@pytest.fixture
def bandit_experiment(db_session, make_experiment):
    """An ACTIVE thompson_sampling experiment with variants ``arm_a``/``arm_b``."""
    suffix = uuid.uuid4().hex[:8]
    experiment = make_experiment(
        name=f"Tracking bandit {suffix}",
        key=f"tracking-bandit-{suffix}",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.BANDIT,
        optimization_type="thompson_sampling",
    )
    for name, is_control in (("arm_a", True), ("arm_b", False)):
        db_session.add(
            Variant(
                experiment_id=experiment.id,
                name=name,
                description=f"{name} arm",
                is_control=is_control,
                traffic_allocation=50,
                configuration={"arm": name},
            )
        )
    db_session.commit()
    db_session.refresh(experiment)
    yield experiment
    _cleanup_experiment_rows(db_session, experiment)


def _set_bandit_weights(db_session, experiment, weights: dict) -> None:
    """Upsert the BanditState row for ``experiment`` with ``weights``."""
    state = (
        db_session.query(BanditState)
        .filter(BanditState.experiment_id == experiment.id)
        .first()
    )
    if state is None:
        state = BanditState(
            experiment_id=experiment.id,
            algorithm=experiment.optimization_type or "thompson_sampling",
            variant_weights=weights,
            total_pulls=0,
        )
        db_session.add(state)
    else:
        state.variant_weights = weights
    db_session.commit()


def _variant_by_name(experiment, name):
    return next(v for v in experiment.variants if v.name == name)


def _user() -> str:
    return f"user-{uuid.uuid4().hex[:10]}"


class TestAssign:
    def test_assigns_user_to_a_variant_and_is_sticky(
        self, admin_client, active_experiment
    ):
        user_id = _user()
        body = {"experiment_key": active_experiment.key, "user_id": user_id}

        first = admin_client.post("/api/v1/tracking/assign", json=body)
        assert first.status_code == 200, first.text
        data = first.json()
        assert data["experiment_key"] == active_experiment.key
        assert data["user_id"] == user_id
        assert data["variant_name"] in {"control", "treatment"}
        assert data["configuration"] == {"color": data["variant_name"]}
        assert data["is_control"] == (data["variant_name"] == "control")

        second = admin_client.post("/api/v1/tracking/assign", json=body)
        assert second.status_code == 200, second.text
        assert second.json()["variant_id"] == data["variant_id"]

    def test_unknown_key_returns_404(self, admin_client):
        resp = admin_client.post(
            "/api/v1/tracking/assign",
            json={"experiment_key": f"missing-{uuid.uuid4().hex}", "user_id": _user()},
        )
        assert resp.status_code == 404

    def test_inactive_experiment_returns_404(self, admin_client, make_experiment):
        suffix = uuid.uuid4().hex[:8]
        draft = make_experiment(name=f"Draft {suffix}", key=f"draft-{suffix}")
        resp = admin_client.post(
            "/api/v1/tracking/assign",
            json={"experiment_key": draft.key, "user_id": _user()},
        )
        assert resp.status_code == 404


class TestBanditAssignment:
    """``/tracking/assign`` honours BanditState weights for MAB experiments."""

    def test_new_users_follow_bandit_weights(
        self, admin_client, bandit_experiment, db_session
    ):
        arm_a = _variant_by_name(bandit_experiment, "arm_a")
        arm_b = _variant_by_name(bandit_experiment, "arm_b")
        _set_bandit_weights(
            db_session, bandit_experiment, {str(arm_a.id): 1.0, str(arm_b.id): 0.0}
        )

        for _ in range(20):
            resp = admin_client.post(
                "/api/v1/tracking/assign",
                json={"experiment_key": bandit_experiment.key, "user_id": _user()},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["variant_id"] == str(arm_a.id)
            assert data["variant_name"] == "arm_a"
            assert data["is_control"] is True

    def test_scheduler_payload_shape_is_honoured(
        self, admin_client, bandit_experiment, db_session
    ):
        """The scheduler stores ``{"weight": ..., "pulls": ...}`` per variant."""
        arm_a = _variant_by_name(bandit_experiment, "arm_a")
        arm_b = _variant_by_name(bandit_experiment, "arm_b")
        _set_bandit_weights(
            db_session,
            bandit_experiment,
            {
                str(arm_a.id): {
                    "weight": 0.0,
                    "successes": 1,
                    "failures": 9,
                    "pulls": 10,
                },
                str(arm_b.id): {
                    "weight": 1.0,
                    "successes": 8,
                    "failures": 2,
                    "pulls": 10,
                },
            },
        )

        for _ in range(10):
            resp = admin_client.post(
                "/api/v1/tracking/assign",
                json={"experiment_key": bandit_experiment.key, "user_id": _user()},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["variant_id"] == str(arm_b.id)

    def test_existing_assignment_is_sticky_when_weights_change(
        self, admin_client, bandit_experiment, db_session
    ):
        arm_a = _variant_by_name(bandit_experiment, "arm_a")
        arm_b = _variant_by_name(bandit_experiment, "arm_b")
        _set_bandit_weights(
            db_session, bandit_experiment, {str(arm_a.id): 1.0, str(arm_b.id): 0.0}
        )

        user_id = _user()
        body = {"experiment_key": bandit_experiment.key, "user_id": user_id}
        first = admin_client.post("/api/v1/tracking/assign", json=body)
        assert first.status_code == 200, first.text
        assert first.json()["variant_id"] == str(arm_a.id)

        # The bandit now routes all new traffic to arm_b ...
        _set_bandit_weights(
            db_session, bandit_experiment, {str(arm_a.id): 0.0, str(arm_b.id): 1.0}
        )

        # ... but the already-assigned user keeps arm_a
        again = admin_client.post("/api/v1/tracking/assign", json=body)
        assert again.status_code == 200, again.text
        assert again.json()["variant_id"] == str(arm_a.id)

        stored = (
            db_session.query(Assignment)
            .filter(
                Assignment.experiment_id == bandit_experiment.id,
                Assignment.user_id == user_id,
            )
            .all()
        )
        assert len(stored) == 1 and str(stored[0].variant_id) == str(arm_a.id)

        # while a fresh user follows the new weights
        fresh = admin_client.post(
            "/api/v1/tracking/assign",
            json={"experiment_key": bandit_experiment.key, "user_id": _user()},
        )
        assert fresh.json()["variant_id"] == str(arm_b.id)

    def test_all_zero_weights_fall_back_to_default_hashing(
        self, admin_client, bandit_experiment, db_session
    ):
        arm_a = _variant_by_name(bandit_experiment, "arm_a")
        arm_b = _variant_by_name(bandit_experiment, "arm_b")
        _set_bandit_weights(
            db_session, bandit_experiment, {str(arm_a.id): 0.0, str(arm_b.id): 0.0}
        )

        seen = set()
        for _ in range(40):
            resp = admin_client.post(
                "/api/v1/tracking/assign",
                json={"experiment_key": bandit_experiment.key, "user_id": _user()},
            )
            assert resp.status_code == 200, resp.text
            seen.add(resp.json()["variant_name"])
        # 50/50 traffic allocation: 40 users landing on one arm is ~2^-39 likely
        assert seen == {"arm_a", "arm_b"}

    def test_fixed_allocation_ignores_bandit_state(
        self, admin_client, active_experiment, db_session
    ):
        """A BanditState row on a fixed-allocation experiment changes nothing."""
        control = _variant_by_name(active_experiment, "control")
        treatment = _variant_by_name(active_experiment, "treatment")
        assert (active_experiment.optimization_type or "fixed") == "fixed"
        _set_bandit_weights(
            db_session,
            active_experiment,
            {str(control.id): 0.0, str(treatment.id): 1.0},
        )

        seen = set()
        for _ in range(40):
            resp = admin_client.post(
                "/api/v1/tracking/assign",
                json={"experiment_key": active_experiment.key, "user_id": _user()},
            )
            assert resp.status_code == 200, resp.text
            seen.add(resp.json()["variant_name"])
        # Weights would have sent everyone to treatment; default hashing splits 50/50.
        assert seen == {"control", "treatment"}


class TestTrack:
    def test_tracks_event_with_assigned_variant(
        self, admin_client, active_experiment, db_session
    ):
        user_id = _user()
        assigned = admin_client.post(
            "/api/v1/tracking/assign",
            json={"experiment_key": active_experiment.key, "user_id": user_id},
        ).json()

        resp = admin_client.post(
            "/api/v1/tracking/track",
            json={
                "event_type": "purchase",
                "event_name": "checkout_complete",
                "user_id": user_id,
                "experiment_key": active_experiment.key,
                "value": 49.99,
                "metadata": {"currency": "USD"},
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["event_type"] == "purchase"
        assert data["event_name"] == "checkout_complete"
        assert data["experiment_id"] == str(active_experiment.id)
        assert data["variant_id"] == assigned["variant_id"]
        assert data["value"] == 49.99
        assert data["properties"] == {"currency": "USD"}

        row = db_session.query(Event).filter(Event.id == uuid.UUID(data["id"])).one()
        assert row.user_id == user_id
        assert row.event_metadata == {"currency": "USD"}
        assert str(row.variant_id) == assigned["variant_id"]

    def test_event_name_defaults_to_event_type(self, admin_client, active_experiment):
        resp = admin_client.post(
            "/api/v1/tracking/track",
            json={
                "event_type": "page_view",
                "user_id": _user(),
                "experiment_key": active_experiment.key,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["event_name"] == "page_view"
        assert resp.json()["variant_id"] is None  # no assignment for this user

    def test_unknown_keys_return_404(self, admin_client):
        resp = admin_client.post(
            "/api/v1/tracking/track",
            json={
                "event_type": "click",
                "user_id": _user(),
                "experiment_key": "nope-" + uuid.uuid4().hex,
            },
        )
        assert resp.status_code == 404

    def test_missing_keys_store_the_event_as_history(self, admin_client, db_session):
        # #217: an event with no key is stored with every id null (it used to
        # answer 422). test_tracking_untagged_events.py covers the rest.
        user_id = _user()
        resp = admin_client.post(
            "/api/v1/tracking/track", json={"event_type": "click", "user_id": user_id}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["experiment_id"] is None
        db_session.query(Event).filter(Event.user_id == user_id).delete()
        db_session.commit()


class TestBatch:
    def test_reports_success_and_failure_per_event(
        self, admin_client, active_experiment, db_session
    ):
        user_id = _user()
        missing_key = "missing-" + uuid.uuid4().hex
        resp = admin_client.post(
            "/api/v1/tracking/batch",
            json={
                "events": [
                    {
                        "event_type": "page_view",
                        "user_id": user_id,
                        "experiment_key": active_experiment.key,
                    },
                    {
                        "event_type": "click",
                        "user_id": user_id,
                        "experiment_key": missing_key,
                    },
                    {
                        "event_type": "add_to_cart",
                        "user_id": user_id,
                        "experiment_key": active_experiment.key,
                        "value": 2,
                    },
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["success_count"] == 2
        assert data["failure_count"] == 1
        assert data["errors"][0]["index"] == 1
        assert data["errors"][0]["error"] == (
            f"No experiment has the key '{missing_key}'. Leave experiment_key out "
            "to record the event without an experiment."
        )

        stored = (
            db_session.query(Event)
            .filter(
                Event.experiment_id == active_experiment.id, Event.user_id == user_id
            )
            .all()
        )
        assert sorted(e.event_type for e in stored) == ["add_to_cart", "page_view"]

    def test_more_than_100_events_is_422_and_stores_nothing(
        self, admin_client, active_experiment, db_session
    ):
        user_id = _user()
        event = {
            "event_type": "page_view",
            "user_id": user_id,
            "experiment_key": active_experiment.key,
        }
        resp = admin_client.post(
            "/api/v1/tracking/batch", json={"events": [event] * 101}
        )
        assert resp.status_code == 422, resp.text
        (error,) = resp.json()["detail"]
        assert error["loc"] == ["body", "events"]
        assert error["type"] == "too_long"
        assert (
            db_session.query(Event)
            .filter(
                Event.experiment_id == active_experiment.id, Event.user_id == user_id
            )
            .count()
            == 0
        )


class TestEventsByIds:
    def test_tracks_event_by_ids_and_parses_json_properties(
        self, admin_client, active_experiment, db_session
    ):
        variant = (
            db_session.query(Variant)
            .filter(Variant.experiment_id == active_experiment.id)
            .first()
        )
        user_id = _user()
        resp = admin_client.post(
            "/api/v1/tracking/events",
            json={
                "event_type": "track",
                "event_name": "signup",
                "user_id": user_id,
                "experiment_id": str(active_experiment.id),
                "variant_id": str(variant.id),
                "value": 1,
                "properties": json.dumps({"plan": "pro"}),
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["experiment_id"] == str(active_experiment.id)
        assert data["variant_id"] == str(variant.id)
        assert data["properties"] == {"plan": "pro"}

        row = db_session.query(Event).filter(Event.id == uuid.UUID(data["id"])).one()
        assert row.event_metadata == {"plan": "pro"}
        assert row.event_type == "track"

    def test_rejects_event_without_experiment_or_flag(self, admin_client):
        resp = admin_client.post(
            "/api/v1/tracking/events",
            json={"event_type": "track", "event_name": "x", "user_id": _user()},
        )
        assert resp.status_code == 422

    def test_rejects_invalid_experiment_id(self, admin_client):
        resp = admin_client.post(
            "/api/v1/tracking/events",
            json={
                "event_type": "track",
                "event_name": "x",
                "user_id": _user(),
                "experiment_id": "not-a-uuid",
            },
        )
        assert resp.status_code == 422


class TestUserAssignments:
    def test_lists_assignments_for_user(self, admin_client, active_experiment):
        user_id = _user()
        admin_client.post(
            "/api/v1/tracking/assign",
            json={"experiment_key": active_experiment.key, "user_id": user_id},
        )
        resp = admin_client.get(f"/api/v1/tracking/assignments/{user_id}")
        assert resp.status_code == 200, resp.text
        assert any(
            a.get("experiment_id") == str(active_experiment.id) for a in resp.json()
        )


# ---------------------------------------------------------------------------
# Eligibility on /tracking/assign: global holdout, mutual exclusion, targeting
# ---------------------------------------------------------------------------

HOLDOUT_PERCENTAGE = 20  # the model's CHECK constraint caps holdouts at 20%

NATIVE_US_ONLY_RULES = {
    "version": "1.0",
    "rules": [
        {
            "id": "us_only",
            "name": "US only",
            "rule": {
                "operator": "and",
                "conditions": [
                    {"attribute": "country", "operator": "eq", "value": "US"}
                ],
            },
            "rollout_percentage": 100,
            "priority": 1,
        }
    ],
}

DASHBOARD_US_ONLY_RULES = {
    "logical_operator": "AND",
    "groups": [
        {
            "id": "g1",
            "logical_operator": "AND",
            "conditions": [
                {
                    "id": "c1",
                    "attribute": "user.country",
                    "operator": "equals",
                    "value": "US",
                }
            ],
        }
    ],
}


def _users_by_holdout_membership(holdout, count_in: int, count_out: int):
    """Fresh user ids split by ``holdout``'s own bucket (its ``hash_salt``).

    Each holdout buckets with its own salt since #445, so the split is the
    holdout's, not the legacy constant's.
    """
    inside, outside = [], []
    while len(inside) < count_in or len(outside) < count_out:
        user_id = _user()
        bucket = holdout_bucket(user_id, holdout.hash_salt)
        if bucket < holdout.holdout_percentage:
            if len(inside) < count_in:
                inside.append(user_id)
        elif len(outside) < count_out:
            outside.append(user_id)
    return inside, outside


def _assignment_rows(db_session, experiment, user_id):
    return (
        db_session.query(Assignment)
        .filter(
            Assignment.experiment_id == experiment.id, Assignment.user_id == user_id
        )
        .all()
    )


def _event_rows(db_session, experiment, user_id):
    return (
        db_session.query(Event)
        .filter(Event.experiment_id == experiment.id, Event.user_id == user_id)
        .all()
    )


def _assert_unassigned(resp, experiment, reason):
    """An ineligible user gets HTTP 200 + the control variant, assigned=False."""
    assert resp.status_code == 200, resp.text
    data = resp.json()
    control = _variant_by_name(experiment, "control")
    assert data["assigned"] is False
    assert data["reason"] == reason
    assert data["variant_id"] == str(control.id)
    assert data["variant_name"] == "control"
    assert data["is_control"] is True
    assert data["configuration"] == {"color": "control"}
    return data


@pytest.fixture
def active_holdout(db_session):
    """An active GlobalHoldout at HOLDOUT_PERCENTAGE; any other active holdout is
    parked for the duration of the test and restored afterwards."""
    parked = (
        db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active == True).all()
    )
    for row in parked:
        row.is_active = False
    holdout = GlobalHoldout(
        name=f"tracking-holdout-{uuid.uuid4().hex[:8]}",
        description="Eligibility test holdout",
        holdout_percentage=HOLDOUT_PERCENTAGE,
        is_active=True,
    )
    db_session.add(holdout)
    db_session.commit()
    db_session.refresh(holdout)
    yield holdout
    db_session.rollback()
    db_session.query(GlobalHoldout).filter(GlobalHoldout.id == holdout.id).delete()
    for row in parked:
        db_session.merge(row).is_active = True
    db_session.commit()


def _make_active_experiment(db_session, make_experiment, label, **kwargs):
    suffix = uuid.uuid4().hex[:8]
    experiment = make_experiment(
        name=f"Tracking {label} {suffix}",
        key=f"tracking-{label}-{suffix}",
        status=ExperimentStatus.ACTIVE,
        **kwargs,
    )
    for name, is_control in (("control", True), ("treatment", False)):
        db_session.add(
            Variant(
                experiment_id=experiment.id,
                name=name,
                description=f"{name} variant",
                is_control=is_control,
                traffic_allocation=50,
                configuration={"color": name},
            )
        )
    db_session.commit()
    db_session.refresh(experiment)
    return experiment


@pytest.fixture
def meg_experiments(db_session, make_experiment):
    """Two ACTIVE experiments sharing one ACTIVE mutual exclusion group (traffic 1.0)."""
    group = MutualExclusionGroup(
        name=f"tracking-meg-{uuid.uuid4().hex[:8]}",
        description="Eligibility test group",
        traffic_allocation=1.0,
        status=MutualExclusionGroupStatus.ACTIVE,
    )
    db_session.add(group)
    db_session.commit()
    db_session.refresh(group)

    experiments = [
        _make_active_experiment(
            db_session, make_experiment, f"meg{i}", mutual_exclusion_group_id=group.id
        )
        for i in (1, 2)
    ]
    yield group, experiments
    for experiment in experiments:
        _cleanup_experiment_rows(db_session, experiment)
        experiment.mutual_exclusion_group_id = None
    db_session.commit()
    db_session.query(MutualExclusionGroup).filter(
        MutualExclusionGroup.id == group.id
    ).delete()
    db_session.commit()


@pytest.fixture
def targeted_experiment(db_session, make_experiment):
    """ACTIVE experiment whose native targeting rules admit ``country == "US"`` only."""
    experiment = _make_active_experiment(
        db_session, make_experiment, "targeting", targeting_rules=NATIVE_US_ONLY_RULES
    )
    yield experiment
    _cleanup_experiment_rows(db_session, experiment)


@pytest.fixture
def dashboard_targeted_experiment(db_session, make_experiment):
    """Same as ``targeted_experiment`` but rules in the dashboard editor shape."""
    experiment = _make_active_experiment(
        db_session,
        make_experiment,
        "dash-targeting",
        targeting_rules=DASHBOARD_US_ONLY_RULES,
    )
    yield experiment
    _cleanup_experiment_rows(db_session, experiment)


DASHBOARD_US_OR_PRO_RULES = {
    "logical_operator": "OR",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "country", "operator": "equals", "value": "US"}
            ],
        },
        {
            "logical_operator": "AND",
            "conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}],
        },
    ],
}


@pytest.fixture
def or_targeted_experiment(db_session, make_experiment):
    """ACTIVE experiment targeting ``country equals US OR plan equals pro``."""
    experiment = _make_active_experiment(
        db_session,
        make_experiment,
        "or-targeting",
        targeting_rules=DASHBOARD_US_OR_PRO_RULES,
    )
    yield experiment
    _cleanup_experiment_rows(db_session, experiment)


class TestAssignEligibility:
    """``/tracking/assign`` honours the global holdout, mutual exclusion groups
    and experiment targeting rules for *new* users; sticky users bypass all."""

    def test_holdout_users_get_control_and_are_not_recorded(
        self, admin_client, active_experiment, active_holdout, db_session
    ):
        inside, outside = _users_by_holdout_membership(active_holdout, 10, 10)

        for user_id in inside:
            resp = admin_client.post(
                "/api/v1/tracking/assign",
                json={"experiment_key": active_experiment.key, "user_id": user_id},
            )
            data = _assert_unassigned(resp, active_experiment, "holdout")
            assert data["experiment_key"] == active_experiment.key
            assert data["user_id"] == user_id
            assert _assignment_rows(db_session, active_experiment, user_id) == []
            assert _event_rows(db_session, active_experiment, user_id) == []

            # Still no row after a second call: the response is not sticky.
            again = admin_client.post(
                "/api/v1/tracking/assign",
                json={"experiment_key": active_experiment.key, "user_id": user_id},
            )
            assert again.json()["assigned"] is False
            assert _assignment_rows(db_session, active_experiment, user_id) == []

        for user_id in outside:
            resp = admin_client.post(
                "/api/v1/tracking/assign",
                json={"experiment_key": active_experiment.key, "user_id": user_id},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["assigned"] is True
            assert data["reason"] == "assigned"
            assert len(_assignment_rows(db_session, active_experiment, user_id)) == 1

    def test_every_holdout_bucket_user_is_unassigned(
        self, admin_client, active_experiment, active_holdout
    ):
        """Every user the holdout hashes inside the holdout is refused (not a sample)."""
        inside, _ = _users_by_holdout_membership(active_holdout, 40, 0)
        reasons = {
            admin_client.post(
                "/api/v1/tracking/assign",
                json={"experiment_key": active_experiment.key, "user_id": user_id},
            ).json()["reason"]
            for user_id in inside
        }
        assert reasons == {"holdout"}

    def test_mutual_exclusion_assigns_each_user_to_exactly_one_experiment(
        self, admin_client, meg_experiments, db_session
    ):
        group, (exp_a, exp_b) = meg_experiments
        assigned_counts = {exp_a.key: 0, exp_b.key: 0}

        for _ in range(200):
            user_id = _user()
            results = {}
            for experiment in (exp_a, exp_b):
                resp = admin_client.post(
                    "/api/v1/tracking/assign",
                    json={"experiment_key": experiment.key, "user_id": user_id},
                )
                assert resp.status_code == 200, resp.text
                results[experiment.key] = resp.json()

            assigned = [k for k, v in results.items() if v["assigned"]]
            excluded = [k for k, v in results.items() if not v["assigned"]]
            assert len(assigned) == 1 and len(excluded) == 1, results
            assigned_counts[assigned[0]] += 1

            assert results[assigned[0]]["reason"] == "assigned"
            assert results[excluded[0]]["reason"] == "mutual_exclusion"
            excluded_exp = exp_a if excluded[0] == exp_a.key else exp_b
            _assert_unassigned(
                admin_client.post(
                    "/api/v1/tracking/assign",
                    json={"experiment_key": excluded_exp.key, "user_id": user_id},
                ),
                excluded_exp,
                "mutual_exclusion",
            )
            assert _assignment_rows(db_session, excluded_exp, user_id) == []
            assert _event_rows(db_session, excluded_exp, user_id) == []

        # traffic_allocation 1.0 split between two experiments: both get users
        assert assigned_counts[exp_a.key] > 0 and assigned_counts[exp_b.key] > 0
        total_rows = (
            db_session.query(Assignment)
            .filter(Assignment.experiment_id.in_([exp_a.id, exp_b.id]))
            .count()
        )
        assert total_rows == 200

    def test_native_targeting_rules_gate_new_users(
        self, admin_client, targeted_experiment, db_session
    ):
        us_user, de_user, blank_user = _user(), _user(), _user()

        resp = admin_client.post(
            "/api/v1/tracking/assign",
            json={
                "experiment_key": targeted_experiment.key,
                "user_id": us_user,
                "context": {"country": "US"},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["assigned"] is True
        assert resp.json()["reason"] == "assigned"
        assert len(_assignment_rows(db_session, targeted_experiment, us_user)) == 1
        # exposure recorded with the request context
        events = _event_rows(db_session, targeted_experiment, us_user)
        assert len(events) == 1 and events[0].event_metadata == {"country": "US"}

        resp = admin_client.post(
            "/api/v1/tracking/assign",
            json={
                "experiment_key": targeted_experiment.key,
                "user_id": de_user,
                "context": {"country": "DE"},
            },
        )
        _assert_unassigned(resp, targeted_experiment, "targeting")
        assert _assignment_rows(db_session, targeted_experiment, de_user) == []
        assert _event_rows(db_session, targeted_experiment, de_user) == []

        resp = admin_client.post(
            "/api/v1/tracking/assign",
            json={"experiment_key": targeted_experiment.key, "user_id": blank_user},
        )
        _assert_unassigned(resp, targeted_experiment, "targeting")
        assert _assignment_rows(db_session, targeted_experiment, blank_user) == []

    def test_targeting_accepts_dotted_aliases_for_top_level_context(
        self, admin_client, targeted_experiment, db_session
    ):
        """A rule on ``country`` also matches a nested ``{"user": {"country"}}``
        context, and the dotted lookup works the other way round too."""
        user_id = _user()
        resp = admin_client.post(
            "/api/v1/tracking/assign",
            json={
                "experiment_key": targeted_experiment.key,
                "user_id": user_id,
                "context": {"user": {"country": "US"}, "country": "US"},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["assigned"] is True

    def test_dashboard_shaped_targeting_rules_gate_new_users(
        self, admin_client, dashboard_targeted_experiment, db_session
    ):
        """Rules saved by the dashboard editor (``user.country equals US``) are
        normalised through ``backend.app.core.targeting_adapter``."""
        us_user, de_user, blank_user = _user(), _user(), _user()

        resp = admin_client.post(
            "/api/v1/tracking/assign",
            json={
                "experiment_key": dashboard_targeted_experiment.key,
                "user_id": us_user,
                "context": {"country": "US"},  # answers ``user.country`` via alias
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["assigned"] is True
        assert (
            len(_assignment_rows(db_session, dashboard_targeted_experiment, us_user))
            == 1
        )

        resp = admin_client.post(
            "/api/v1/tracking/assign",
            json={
                "experiment_key": dashboard_targeted_experiment.key,
                "user_id": de_user,
                "context": {"country": "DE"},
            },
        )
        _assert_unassigned(resp, dashboard_targeted_experiment, "targeting")
        assert (
            _assignment_rows(db_session, dashboard_targeted_experiment, de_user) == []
        )

        resp = admin_client.post(
            "/api/v1/tracking/assign",
            json={
                "experiment_key": dashboard_targeted_experiment.key,
                "user_id": blank_user,
            },
        )
        _assert_unassigned(resp, dashboard_targeted_experiment, "targeting")

    @pytest.mark.regression
    def test_or_targeting_enrols_a_user_lacking_the_other_branch_attribute(
        self, admin_client, or_targeted_experiment, db_session
    ):
        """A missing attribute makes only its own condition false (#822): a
        user with ``plan`` but no ``country`` matches the ``plan`` branch."""
        pro_user, fr_user = _user(), _user()

        resp = admin_client.post(
            "/api/v1/tracking/assign",
            json={
                "experiment_key": or_targeted_experiment.key,
                "user_id": pro_user,
                "context": {"plan": "pro"},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["assigned"] is True
        assert resp.json()["reason"] == "assigned"
        assert len(_assignment_rows(db_session, or_targeted_experiment, pro_user)) == 1

        # Neither branch holds: still refused.
        resp = admin_client.post(
            "/api/v1/tracking/assign",
            json={
                "experiment_key": or_targeted_experiment.key,
                "user_id": fr_user,
                "context": {"country": "FR"},
            },
        )
        _assert_unassigned(resp, or_targeted_experiment, "targeting")
        assert _assignment_rows(db_session, or_targeted_experiment, fr_user) == []

    def test_sticky_assignment_bypasses_all_eligibility_checks(
        self,
        admin_client,
        targeted_experiment,
        active_holdout,
        meg_experiments,
        make_assignment,
        db_session,
    ):
        """A stored Assignment row wins even if the user is now in the holdout,
        excluded by the group and fails targeting."""
        group, (exp_a, exp_b) = meg_experiments
        (user_id,), _ = _users_by_holdout_membership(active_holdout, 1, 0)

        # Put the targeted experiment into the group as well so all three
        # gates apply to it; the link is removed in ``finally`` before the
        # meg_experiments fixture deletes the group.
        targeted_experiment.mutual_exclusion_group_id = group.id
        db_session.commit()
        try:
            treatment = _variant_by_name(targeted_experiment, "treatment")
            make_assignment(
                experiment=targeted_experiment, variant=treatment, user_id=user_id
            )

            # Sanity: a *new* user with the same profile is refused (holdout wins).
            (fresh,), _ = _users_by_holdout_membership(active_holdout, 1, 0)
            _assert_unassigned(
                admin_client.post(
                    "/api/v1/tracking/assign",
                    json={
                        "experiment_key": targeted_experiment.key,
                        "user_id": fresh,
                        "context": {"country": "DE"},
                    },
                ),
                targeted_experiment,
                "holdout",
            )

            resp = admin_client.post(
                "/api/v1/tracking/assign",
                json={
                    "experiment_key": targeted_experiment.key,
                    "user_id": user_id,
                    "context": {"country": "DE"},
                },
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["assigned"] is True
            assert data["reason"] == "assigned"
            assert data["variant_id"] == str(treatment.id)
            assert data["variant_name"] == "treatment"
            assert data["is_control"] is False
            assert len(_assignment_rows(db_session, targeted_experiment, user_id)) == 1
        finally:
            db_session.rollback()
            targeted_experiment.mutual_exclusion_group_id = None
            db_session.commit()

    def test_reason_order_is_holdout_then_mutual_exclusion_then_targeting(
        self, admin_client, meg_experiments, active_holdout, db_session
    ):
        group, (exp_a, exp_b) = meg_experiments
        exp_a.targeting_rules = NATIVE_US_ONLY_RULES
        db_session.commit()
        try:
            # In the holdout: reason is holdout regardless of group/targeting.
            (held,), _ = _users_by_holdout_membership(active_holdout, 1, 0)
            _assert_unassigned(
                admin_client.post(
                    "/api/v1/tracking/assign",
                    json={
                        "experiment_key": exp_a.key,
                        "user_id": held,
                        "context": {"country": "DE"},
                    },
                ),
                exp_a,
                "holdout",
            )

            # Outside the holdout, with a DE context (fails targeting on exp_a):
            # users routed to exp_b by the group report mutual_exclusion on
            # exp_a, users routed to exp_a report targeting.
            _, outside = _users_by_holdout_membership(active_holdout, 0, 40)
            seen = set()
            for user_id in outside:
                resp = admin_client.post(
                    "/api/v1/tracking/assign",
                    json={
                        "experiment_key": exp_a.key,
                        "user_id": user_id,
                        "context": {"country": "DE"},
                    },
                )
                assert resp.status_code == 200, resp.text
                reason = resp.json()["reason"]
                assert reason in {"mutual_exclusion", "targeting"}, resp.text
                _assert_unassigned(resp, exp_a, reason)
                seen.add(reason)
            # 40 users routed by the group hash: both outcomes show up (~2^-39 otherwise)
            assert seen == {"mutual_exclusion", "targeting"}
        finally:
            db_session.rollback()
            exp_a.targeting_rules = None
            db_session.commit()


# ---------------------------------------------------------------------------
# Holdout population: who a measurable holdout covered (#445)
# ---------------------------------------------------------------------------


@pytest.fixture
def measurable_holdout(db_session):
    """An active holdout created and activated through the service, so it has
    its own salt and ``activated_at``; any other active holdout is parked with
    a plain UPDATE (nothing stamps it ended) and restored afterwards."""
    parked = [
        row.id
        for row in db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active)
    ]
    db_session.execute(
        update(GlobalHoldout)
        .where(GlobalHoldout.id.in_(parked))
        .values(is_active=False)
    )
    db_session.commit()
    holdout = GlobalHoldoutService(db_session).create_holdout(
        name=f"population-holdout-{uuid.uuid4().hex[:8]}",
        holdout_percentage=HOLDOUT_PERCENTAGE,
        is_active=True,
    )
    assert holdout.is_measurable
    yield holdout
    db_session.rollback()
    # holdout_population rows go with it (ON DELETE CASCADE).
    db_session.query(GlobalHoldout).filter(GlobalHoldout.id == holdout.id).delete()
    db_session.execute(
        update(GlobalHoldout).where(GlobalHoldout.id.in_(parked)).values(is_active=True)
    )
    db_session.commit()


def _population(db_session, holdout) -> dict:
    """``{user_id: (in_holdout, first_seen_at)}``, read in a SEPARATE session,
    so only committed rows count."""
    with Session(bind=db_session.get_bind()) as fresh:
        fresh.execute(text("SET search_path TO test_experimentation"))
        rows = (
            fresh.query(HoldoutPopulation)
            .filter(HoldoutPopulation.holdout_id == holdout.id)
            .all()
        )
        return {r.user_id: (r.in_holdout, r.first_seen_at) for r in rows}


def _assign(client, experiment, user_id, context=None):
    body = {"experiment_key": experiment.key, "user_id": user_id}
    if context is not None:
        body["context"] = context
    resp = client.post("/api/v1/tracking/assign", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestHoldoutPopulation:
    @pytest.mark.regression
    def test_a2_holdout_rows_are_exactly_the_users_answered_holdout(
        self,
        admin_client,
        active_experiment,
        targeted_experiment,
        meg_experiments,
        measurable_holdout,
        db_session,
    ):
        """Every caller is recorded once, in their arm: held out, assigned,
        refused by targeting or by mutual exclusion, and repeat callers."""
        _, (exp_a, _) = meg_experiments
        users = [_user() for _ in range(120)]
        held = set()
        for user_id in users:
            for experiment in (active_experiment, targeted_experiment, exp_a):
                answer = _assign(admin_client, experiment, user_id, {"country": "DE"})
                if answer["reason"] == "holdout":
                    held.add(user_id)
        # Repeat callers add nothing.
        for user_id in users[:30]:
            _assign(admin_client, active_experiment, user_id, {"country": "DE"})

        population = _population(db_session, measurable_holdout)

        assert set(population) == set(users)
        assert {u for u, (inside, _) in population.items() if inside} == held
        assert 0 < len(held) < len(users)

    @pytest.mark.regression
    def test_c4_check_and_assign_agree_on_1000_ids(
        self, admin_client, active_experiment, measurable_holdout
    ):
        """``/holdout/check`` and ``/tracking/assign`` bucket with the same
        function and the holdout's own salt."""
        disagree = []
        held = 0
        for _ in range(1000):
            user_id = _user()
            check = admin_client.get(f"/api/v1/holdout/check/{user_id}")
            assert check.status_code == 200, check.text
            in_check = check.json()["is_in_holdout"]
            in_assign = _assign(admin_client, active_experiment, user_id)["reason"] == (
                "holdout"
            )
            held += in_assign
            if in_check != in_assign:
                disagree.append(user_id)
        assert disagree == []
        # About 20% of 1000; far outside this band means a wrong bucket.
        assert 130 < held < 270, held

    def test_a5_a_sticky_user_is_not_written(
        self,
        admin_client,
        active_experiment,
        measurable_holdout,
        make_assignment,
        db_session,
    ):
        """An assignment row made after activation with no population row --
        what an older task during a blue/green overlap leaves -- is answered
        from the row and writes nothing: only the new-user path records."""
        _, (user_id,) = _users_by_holdout_membership(measurable_holdout, 0, 1)
        make_assignment(
            experiment=active_experiment,
            variant=_variant_by_name(active_experiment, "treatment"),
            user_id=user_id,
        )

        answer = _assign(admin_client, active_experiment, user_id)

        assert answer["assigned"] is True
        assert user_id not in _population(db_session, measurable_holdout)

    def test_a3_an_earlier_assignment_predates_the_first_seen_time(
        self,
        admin_client,
        active_experiment,
        targeted_experiment,
        make_assignment,
        db_session,
        measurable_holdout,
    ):
        """The C8 fixture: a user assigned to one experiment before they are
        first seen is recorded when they reach another experiment's new-user
        path, with ``first_seen_at`` after that assignment.  The results
        query (#445 PR2) excludes such a user from both arms; here the rows
        it needs are pinned."""
        _, (user_id,) = _users_by_holdout_membership(measurable_holdout, 0, 1)
        earlier = make_assignment(
            experiment=active_experiment,
            variant=_variant_by_name(active_experiment, "control"),
            user_id=user_id,
        )

        _assign(admin_client, targeted_experiment, user_id, {"country": "US"})

        in_holdout, first_seen_at = _population(db_session, measurable_holdout)[user_id]
        assert in_holdout is False
        assert first_seen_at.endswith("+00:00")
        assert dt.datetime.fromisoformat(first_seen_at) > earlier.created_at.replace(
            tzinfo=dt.timezone.utc
        )

    def test_a_holdout_that_is_not_measurable_records_nobody(
        self, admin_client, active_experiment, active_holdout, db_session
    ):
        """``active_holdout`` is written straight through the ORM: its own
        salt, but no ``activated_at`` -- like the holdout active at the
        upgrade, it holds users out and records nobody."""
        assert not active_holdout.is_measurable
        inside, outside = _users_by_holdout_membership(active_holdout, 3, 3)
        for user_id in inside + outside:
            _assign(admin_client, active_experiment, user_id)

        assert _population(db_session, active_holdout) == {}


# ---------------------------------------------------------------------------
# A7 / PE C5: statements per /tracking/assign call, under expire_on_commit=True
# ---------------------------------------------------------------------------


@contextmanager
def _statements(engine):
    seen = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    sa_event.listen(engine, "before_cursor_execute", _before)
    try:
        yield seen
    finally:
        sa_event.remove(engine, "before_cursor_execute", _before)


def _population_inserts(statements) -> int:
    return sum(
        st.lstrip().upper().startswith("INSERT INTO") and "holdout_population" in st
        for st in statements
    )


class TestAssignStatementCounts:
    """Pinned through the HTTP route, whose per-request session is a plain
    ``sessionmaker`` (``expire_on_commit=True``, as ``SessionLocal``), so a
    commit in the middle of the path shows up as reloads.  COMMIT is not a
    cursor statement and is not counted."""

    def _count(self, client, db_session, experiment, user_id):
        with _statements(db_session.get_bind()) as seen:
            answer = _assign(client, experiment, user_id)
        return answer, seen

    @pytest.mark.regression
    def test_statement_counts_with_a_measurable_holdout(
        self, admin_client, active_experiment, measurable_holdout, db_session
    ):
        (held,), (fresh,) = _users_by_holdout_membership(measurable_holdout, 1, 1)

        held_answer, held_statements = self._count(
            admin_client, db_session, active_experiment, held
        )
        new_answer, new_statements = self._count(
            admin_client, db_session, active_experiment, fresh
        )
        sticky_answer, sticky_statements = self._count(
            admin_client, db_session, active_experiment, fresh
        )

        assert held_answer["reason"] == "holdout"
        assert new_answer["reason"] == "assigned"
        assert sticky_answer["reason"] == "assigned"
        counts = {
            "held out": len(held_statements),
            "new": len(new_statements),
            "sticky": len(sticky_statements),
        }
        inserts = {
            "held out": _population_inserts(held_statements),
            "new": _population_inserts(new_statements),
            "sticky": _population_inserts(sticky_statements),
        }
        print("counts", counts, "population inserts", inserts)
        assert inserts == {"held out": 1, "new": 1, "sticky": 0}
        assert counts == HOLDOUT_COUNTS, (counts, _brief(held_statements))

    @pytest.mark.regression
    def test_statement_counts_with_no_active_holdout(
        self, admin_client, active_experiment, db_session
    ):
        parked = [
            row.id
            for row in db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active)
        ]
        db_session.execute(
            update(GlobalHoldout)
            .where(GlobalHoldout.id.in_(parked))
            .values(is_active=False)
        )
        db_session.commit()
        try:
            fresh = _user()
            new_answer, new_statements = self._count(
                admin_client, db_session, active_experiment, fresh
            )
            sticky_answer, sticky_statements = self._count(
                admin_client, db_session, active_experiment, fresh
            )
        finally:
            db_session.execute(
                update(GlobalHoldout)
                .where(GlobalHoldout.id.in_(parked))
                .values(is_active=True)
            )
            db_session.commit()

        counts = {"new": len(new_statements), "sticky": len(sticky_statements)}
        print("counts", counts)
        assert new_answer["reason"] == sticky_answer["reason"] == "assigned"
        assert _population_inserts(new_statements + sticky_statements) == 0
        assert counts == NO_HOLDOUT_COUNTS, (counts, _brief(new_statements))


#: Measured through the route.  On the parent commit (no population): held
#: out 5, new 13, sticky 10, with or without a holdout.  Now, with a
#: measurable holdout: new +1 (the INSERT, committed with the assignment);
#: held out +3 (the INSERT, then the one commit at the end of the path
#: expires the experiment, and the route's read of ``experiment.variants``
#: reloads it and its variants -- the assigned path has always paid that);
#: sticky +0.  With no holdout, or one that is not measurable, nothing moves.
#: A commit moved before ``check_eligibility`` adds two more to "held out".
HOLDOUT_COUNTS = {"held out": 8, "new": 14, "sticky": 10}
NO_HOLDOUT_COUNTS = {"new": 13, "sticky": 10}


def _brief(statements) -> list:
    """Each statement's verb and table, for a readable failure."""
    out = []
    for st in statements:
        words = st.split()
        table = next(
            (w for w in words if w.startswith("test_experimentation.")), words[-1]
        )
        out.append(f"{words[0]} {table.split('.')[1] if '.' in table else table}")
    return out
