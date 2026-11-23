"""
Mutual exclusion holds when the group's set of active experiments changes (#446).

The group's consistent hash divides users among the experiments that are
ACTIVE at the moment of the request.  Before the fix, activating a second
experiment in a group moved about half of the first experiment's users into
the new experiment's slot, and they were enrolled in both.  A user who holds an
assignment in a sibling that is ACTIVE, PAUSED or DRAFT is now refused
(``reason: mutual_exclusion``, the same response shape as before); a COMPLETED
or ARCHIVED sibling, removal from the group and an archived group release them.

Real PostgreSQL throughout.  Every experiment, group and user id is fixed, so
the hash -- which is salted with the group id -- puts the same users in the
same slots on every run and the counts below are exact, not ranges.
"""

import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.assignment import Assignment
from backend.app.models.bandit_state import BanditState
from backend.app.models.event import Event
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Variant,
)
from backend.app.models.mutual_exclusion_group import (
    MutualExclusionGroup,
    MutualExclusionGroupStatus,
)
from backend.app.models.user import User, UserRole
from backend.app.schemas.tracking import VariantAssignmentResponse
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.mutual_exclusion_service import MutualExclusionService

pytestmark = [pytest.mark.integration, pytest.mark.regression]

GROUP_ID = uuid.UUID("44600000-0000-4000-8000-000000000001")
EXP_A = uuid.UUID("44600000-0000-4000-8000-00000000000a")
EXP_B = uuid.UUID("44600000-0000-4000-8000-00000000000b")
EXP_SOLO = uuid.UUID("44600000-0000-4000-8000-00000000000c")
ALL_EXPERIMENTS = (EXP_A, EXP_B, EXP_SOLO)
GROUP_NAME = "me-446 sticky group"
KEYS = {EXP_A: "me-446-sticky-a", EXP_B: "me-446-sticky-b", EXP_SOLO: "me-446-solo"}

# Users who first meet the group while only A is live.
ENROLLED = [f"me446-enrolled-{i:04d}" for i in range(1000)]
# Users who first meet the group after B is live.
NEWCOMERS = [f"me446-new-{i:04d}" for i in range(300)]

# The hash splits NEWCOMERS between A and B (both ACTIVE, sorted by id) like
# this.  A change to the hash, the salt or the slot arithmetic moves these
# numbers; that is a change to who sees which experiment and must be noticed.
NEWCOMERS_IN_B_SLOT = 145
ENROLLED_IN_A_SLOT = 492

REFUSING_RULES = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "country", "operator": "equals", "value": "zz"}
            ],
        }
    ],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _purge(db_session) -> None:
    db_session.rollback()
    db_session.query(Event).filter(Event.experiment_id.in_(ALL_EXPERIMENTS)).delete(
        synchronize_session=False
    )
    db_session.query(Assignment).filter(
        Assignment.experiment_id.in_(ALL_EXPERIMENTS)
    ).delete(synchronize_session=False)
    db_session.query(BanditState).filter(
        BanditState.experiment_id.in_(ALL_EXPERIMENTS)
    ).delete(synchronize_session=False)
    db_session.query(Variant).filter(Variant.experiment_id.in_(ALL_EXPERIMENTS)).delete(
        synchronize_session=False
    )
    db_session.query(Experiment).filter(Experiment.id.in_(ALL_EXPERIMENTS)).delete(
        synchronize_session=False
    )
    db_session.query(MutualExclusionGroup).filter(
        (MutualExclusionGroup.id == GROUP_ID)
        | (MutualExclusionGroup.name == GROUP_NAME)
    ).delete(synchronize_session=False)
    db_session.commit()
    db_session.expire_all()


@pytest.fixture
def owner(db_session):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"me446_{suffix}",
        email=f"me446_{suffix}@sticky.test",
        full_name="Mutual exclusion sticky",
        hashed_password="not-a-real-hash",
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def group_setup(db_session, owner):
    """Group G with A (ACTIVE) and B (DRAFT), plus an ungrouped ACTIVE experiment."""
    _purge(db_session)
    db_session.add(
        MutualExclusionGroup(
            id=GROUP_ID,
            name=GROUP_NAME,
            traffic_allocation=1.0,
            status=MutualExclusionGroupStatus.ACTIVE,
            owner_id=owner.id,
        )
    )
    db_session.flush()
    for exp_id, status, group in (
        (EXP_A, ExperimentStatus.ACTIVE, GROUP_ID),
        (EXP_B, ExperimentStatus.DRAFT, GROUP_ID),
        (EXP_SOLO, ExperimentStatus.ACTIVE, None),
    ):
        db_session.add(
            Experiment(
                id=exp_id,
                name=f"#446 {KEYS[exp_id]}",
                key=KEYS[exp_id],
                description="mutual exclusion sticky test",
                hypothesis="a user stays in one experiment of the group",
                status=status,
                experiment_type=ExperimentType.A_B,
                owner_id=owner.id,
                mutual_exclusion_group_id=group,
            )
        )
        db_session.flush()
        for name, is_control in (("control", True), ("treatment", False)):
            db_session.add(
                Variant(
                    experiment_id=exp_id,
                    name=name,
                    description=name,
                    is_control=is_control,
                    traffic_allocation=50,
                    configuration={"arm": name},
                )
            )
    db_session.commit()
    yield
    _purge(db_session)


@pytest.fixture
def client(db_session):
    """Real API-key authentication: only ``get_db`` is overridden."""
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        session = factory()
        session.execute(text("SET search_path TO test_experimentation"))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


@pytest.fixture
def api_key(db_session, owner):
    """A real key: the route authenticates it through ``deps.get_api_key``."""
    _, plaintext = APIKey.create_for_user(
        db_session, owner.id, name=f"me446-{uuid.uuid4().hex[:6]}"
    )
    return plaintext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set(db_session, exp_id, **fields) -> None:
    experiment = db_session.get(Experiment, exp_id)
    for name, value in fields.items():
        setattr(experiment, name, value)
    db_session.commit()


def _assign(db_session, users, exp_id, context=None):
    """Assign through the service (the route's own call); returns the results."""
    service = AssignmentService(db_session)
    return [
        service.assign_user(u, exp_id, track_exposure=False, context=context)
        for u in users
    ]


def _users_in(db_session, exp_id) -> set:
    return {
        row.user_id
        for row in db_session.query(Assignment.user_id).filter(
            Assignment.experiment_id == exp_id
        )
    }


def _rows(db_session, exp_id) -> int:
    return (
        db_session.query(Assignment).filter(Assignment.experiment_id == exp_id).count()
    )


def _events(db_session, exp_id) -> int:
    return db_session.query(Event).filter(Event.experiment_id == exp_id).count()


def _enrol_in_a_then_activate_b(db_session, users) -> None:
    results = _assign(db_session, users, EXP_A)
    assert sum(r["assigned"] for r in results) == len(users)
    _set(db_session, EXP_B, status=ExperimentStatus.ACTIVE)


# ---------------------------------------------------------------------------
# M1 / M1b: activating a sibling does not double-enrol A's users
# ---------------------------------------------------------------------------


class TestActivatingASibling:
    def test_m1b_everyone_lands_in_a_while_b_is_draft(self, db_session, group_setup):
        _assign(db_session, ENROLLED, EXP_A)
        assert len(_users_in(db_session, EXP_A)) == 1000
        assert _rows(db_session, EXP_B) == 0

    def test_m1_users_of_a_are_not_enrolled_in_b_after_b_is_activated(
        self, db_session, group_setup
    ):
        _enrol_in_a_then_activate_b(db_session, ENROLLED)

        results = _assign(db_session, ENROLLED, EXP_B)

        in_a = _users_in(db_session, EXP_A)
        in_b = _users_in(db_session, EXP_B)
        assert len(in_a) == 1000
        assert len(in_b) == 0
        assert len(in_a & in_b) == 0
        assert [r["reason"] for r in results] == ["mutual_exclusion"] * 1000
        assert all(r["assigned"] is False for r in results)
        # A keeps every one of them (sticky), with no new rows.
        again = _assign(db_session, ENROLLED, EXP_A)
        assert all(r["assigned"] for r in again)
        assert _rows(db_session, EXP_A) == 1000

    def test_m2_new_users_still_reach_b(self, db_session, group_setup):
        _enrol_in_a_then_activate_b(db_session, ENROLLED[:10])

        results = _assign(db_session, NEWCOMERS, EXP_B)

        assert sum(r["assigned"] for r in results) == NEWCOMERS_IN_B_SLOT
        assert _rows(db_session, EXP_B) == NEWCOMERS_IN_B_SLOT


# ---------------------------------------------------------------------------
# M3 / M3b: which sibling statuses hold their users
# ---------------------------------------------------------------------------


class TestSiblingStatus:
    @pytest.mark.parametrize(
        "a_status", [ExperimentStatus.PAUSED, ExperimentStatus.DRAFT]
    )
    def test_m3_paused_or_draft_sibling_holds_its_users(
        self, db_session, group_setup, a_status
    ):
        users = ENROLLED[:200]
        _assign(db_session, users, EXP_A)
        _set(db_session, EXP_A, status=a_status)
        _set(db_session, EXP_B, status=ExperimentStatus.ACTIVE)

        # B is the only ACTIVE experiment, so the hash picks B for everyone.
        held = _assign(db_session, users, EXP_B)
        fresh = _assign(db_session, NEWCOMERS[:200], EXP_B)

        assert sum(r["assigned"] for r in held) == 0
        assert {r["reason"] for r in held} == {"mutual_exclusion"}
        assert sum(r["assigned"] for r in fresh) == 200
        assert _rows(db_session, EXP_B) == 200
        assert not (_users_in(db_session, EXP_A) & _users_in(db_session, EXP_B))

    @pytest.mark.parametrize(
        "a_status", [ExperimentStatus.COMPLETED, ExperimentStatus.ARCHIVED]
    )
    def test_m3b_completed_or_archived_sibling_releases_its_users(
        self, db_session, group_setup, a_status
    ):
        users = ENROLLED[:200]
        _assign(db_session, users, EXP_A)
        _set(db_session, EXP_A, status=a_status)
        _set(db_session, EXP_B, status=ExperimentStatus.ACTIVE)

        released = _assign(db_session, users, EXP_B)

        assert sum(r["assigned"] for r in released) == 200
        assert _rows(db_session, EXP_B) == 200


# ---------------------------------------------------------------------------
# M4 / M5: the public route
# ---------------------------------------------------------------------------


def _post_assign(client, key, experiment_key, user_id):
    return client.post(
        "/api/v1/tracking/assign",
        json={"experiment_key": experiment_key, "user_id": user_id},
        headers={"X-API-Key": key},
    )


class TestPublicAssignRoute:
    def test_m4_tracking_assign_refuses_with_no_row_and_no_event(
        self, client, api_key, db_session, group_setup
    ):
        # Authentication is real: an unknown key is refused.
        refused = _post_assign(client, "eptk_" + "0" * 32, KEYS[EXP_A], "me446-x")
        assert refused.status_code == 401
        users = ENROLLED[:100]
        for u in users:
            resp = _post_assign(client, api_key, KEYS[EXP_A], u)
            assert resp.status_code == 200, resp.text
            assert resp.json()["assigned"] is True
        events_in_a = _events(db_session, EXP_A)
        assert events_in_a == 100
        _set(db_session, EXP_B, status=ExperimentStatus.ACTIVE)

        bodies = []
        for u in users:
            resp = _post_assign(client, api_key, KEYS[EXP_B], u)
            assert resp.status_code == 200, resp.text
            bodies.append(resp.json())

        db_session.expire_all()
        assert _rows(db_session, EXP_B) == 0
        assert _events(db_session, EXP_B) == 0
        assert _events(db_session, EXP_A) == events_in_a
        for body in bodies:
            assert set(body) == set(VariantAssignmentResponse.model_fields)
            assert body["assigned"] is False
            assert body["reason"] == "mutual_exclusion"
            assert body["is_control"] is True
            assert body["variant_name"] == "control"

    def test_m5_bandit_sibling_is_refused_too(
        self, client, api_key, db_session, group_setup
    ):
        users = ENROLLED[:200]
        _assign(db_session, users, EXP_A)
        _set(
            db_session,
            EXP_B,
            status=ExperimentStatus.ACTIVE,
            experiment_type=ExperimentType.BANDIT,
            optimization_type="thompson_sampling",
        )
        b_variants = db_session.query(Variant).filter(Variant.experiment_id == EXP_B)
        db_session.add(
            BanditState(
                experiment_id=EXP_B,
                algorithm="thompson_sampling",
                variant_weights={str(v.id): 0.5 for v in b_variants},
                total_pulls=0,
            )
        )
        db_session.commit()

        held = [_post_assign(client, api_key, KEYS[EXP_B], u).json() for u in users]
        fresh = [
            _post_assign(client, api_key, KEYS[EXP_B], u).json() for u in NEWCOMERS
        ]

        db_session.expire_all()
        assert sum(b["assigned"] for b in held) == 0
        assert {b["reason"] for b in held} == {"mutual_exclusion"}
        assert sum(b["assigned"] for b in fresh) == NEWCOMERS_IN_B_SLOT
        assert _rows(db_session, EXP_B) == NEWCOMERS_IN_B_SLOT
        assert not (_users_in(db_session, EXP_A) & _users_in(db_session, EXP_B))


# ---------------------------------------------------------------------------
# M6 / M7: what does not hold a user
# ---------------------------------------------------------------------------


class TestWhatIsNotAnEnrolment:
    def test_m6_a_user_refused_by_targeting_may_join_b(self, db_session, group_setup):
        _set(db_session, EXP_A, targeting_rules=REFUSING_RULES)
        refused = _assign(db_session, NEWCOMERS, EXP_A, context={"country": "us"})
        assert {r["reason"] for r in refused} == {"targeting"}
        assert _rows(db_session, EXP_A) == 0
        _set(db_session, EXP_B, status=ExperimentStatus.ACTIVE)

        results = _assign(db_session, NEWCOMERS, EXP_B)

        # Exactly as many as a group with no history gives B (M2).
        assert sum(r["assigned"] for r in results) == NEWCOMERS_IN_B_SLOT

    def test_m7_existing_double_enrolment_is_kept(self, db_session, group_setup):
        """Overlaps made before the fix stay: sticky assignments always win."""
        _set(db_session, EXP_B, status=ExperimentStatus.ACTIVE)
        users = ENROLLED[:300]
        variants = {
            exp_id: db_session.query(Variant)
            .filter(Variant.experiment_id == exp_id, Variant.is_control.is_(True))
            .one()
            .id
            for exp_id in (EXP_A, EXP_B)
        }
        for u in users:
            for exp_id in (EXP_A, EXP_B):
                db_session.add(
                    Assignment(
                        user_id=u, experiment_id=exp_id, variant_id=variants[exp_id]
                    )
                )
        db_session.commit()

        in_a = _assign(db_session, users, EXP_A)
        in_b = _assign(db_session, users, EXP_B)

        assert sum(r["assigned"] for r in in_a) == 300
        assert sum(r["assigned"] for r in in_b) == 300
        assert _rows(db_session, EXP_A) == 300
        assert _rows(db_session, EXP_B) == 300


# ---------------------------------------------------------------------------
# M8: cost -- one extra statement, and only on the grouped path
# ---------------------------------------------------------------------------


@contextmanager
def _count_statements(db_session):
    engine = db_session.get_bind()
    statements = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa_event.listen(engine, "before_cursor_execute", _before)
    try:
        yield statements
    finally:
        sa_event.remove(engine, "before_cursor_execute", _before)


class TestStatementCount:
    def test_m8_grouped_new_user_costs_one_more_statement_and_solo_none(
        self, db_session, group_setup
    ):
        _set(db_session, EXP_B, status=ExperimentStatus.ACTIVE)
        # A newcomer the hash puts in B, so the eligible grouped path runs.
        service = MutualExclusionService(db_session)
        user = next(
            u
            for u in NEWCOMERS
            if service.select_experiment_for_user(u, GROUP_ID) == EXP_B
        )
        db_session.expire_all()

        with _count_statements(db_session) as grouped:
            result = AssignmentService(db_session).assign_user(user, EXP_B)
        assert result["assigned"] is True
        db_session.expire_all()

        with _count_statements(db_session) as solo:
            result = AssignmentService(db_session).assign_user(user, EXP_SOLO)
        assert result["assigned"] is True

        # Measured on the parent commit with exposure tracking on (what the
        # route does): grouped 13, solo 9.  The sibling lookup adds exactly one
        # statement to the grouped path and none to an ungrouped experiment.
        # #449's read of the person the id belongs to adds one to both.
        assert len(grouped) == 15, grouped
        assert len(solo) == 10, solo
        assert sum("NOT IN" in st for st in grouped) == 1
        assert sum("NOT IN" in st for st in solo) == 0


# ---------------------------------------------------------------------------
# M9: the group's own constraint, and the releases the docs promise
# ---------------------------------------------------------------------------


class TestGroupRules:
    def test_m9_archived_group_no_longer_constrains(self, db_session, group_setup):
        users = ENROLLED[:200]
        _assign(db_session, users, EXP_A)
        _set(db_session, EXP_B, status=ExperimentStatus.ACTIVE)
        group = db_session.get(MutualExclusionGroup, GROUP_ID)
        group.status = MutualExclusionGroupStatus.ARCHIVED
        db_session.commit()

        results = _assign(db_session, users, EXP_B)

        assert sum(r["assigned"] for r in results) == 200

    def test_m9_removing_a_from_the_group_releases_its_users(
        self, db_session, group_setup
    ):
        users = ENROLLED[:200]
        _assign(db_session, users, EXP_A)
        MutualExclusionService(db_session).remove_experiment_from_group(GROUP_ID, EXP_A)
        _set(db_session, EXP_B, status=ExperimentStatus.ACTIVE)

        results = _assign(db_session, users, EXP_B)

        assert sum(r["assigned"] for r in results) == 200

    def test_m9_with_both_live_from_the_start_each_user_is_in_exactly_one(
        self, db_session, group_setup
    ):
        _set(db_session, EXP_B, status=ExperimentStatus.ACTIVE)

        _assign(db_session, ENROLLED, EXP_A)
        _assign(db_session, ENROLLED, EXP_B)

        in_a = _users_in(db_session, EXP_A)
        in_b = _users_in(db_session, EXP_B)
        assert len(in_a) == ENROLLED_IN_A_SLOT
        assert len(in_b) == 1000 - ENROLLED_IN_A_SLOT
        assert len(in_a & in_b) == 0
