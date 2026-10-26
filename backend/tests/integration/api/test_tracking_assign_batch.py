"""
Integration tests for ``POST /api/v1/tracking/assign/batch`` (#441, beta).

Real database and real API-key authentication: only ``deps.get_db`` is
overridden, with a session per request that expires its objects on commit as
production's ``SessionLocal`` does, and keys are created through
``POST /api/v1/api-keys``, so the ``sdk:ruleset`` check runs on the key
presented.

Covered:
* parity: each user gets what N single ``POST /api/v1/tracking/assign`` calls
  give, over holdout, mutual exclusion (hashed and sibling-held), targeting,
  bandit, sticky and plain users; the same Assignment rows; no view events;
  and for an experiment targeted at a segment (#440);
* the order of the list changes nobody's answer; a resend adds no rows;
* the bandit weights are read once per request, and the statements per user
  are exact;
* refusals: 401, 403, 404, 422 and the trailing slash assign nobody; 422
  bodies never repeat a submitted id;
* mid-batch: the experiment paused part-way answers 409 and earlier users
  stay assigned; any other failure answers 500; a resend then completes.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints import tracking
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.main import app
from backend.app.models.assignment import Assignment
from backend.app.models.bandit_state import BanditState
from backend.app.models.event import Event, EventType
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Variant,
)
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.holdout_population import HoldoutPopulation
from backend.app.models.mutual_exclusion_group import (
    MutualExclusionGroup,
    MutualExclusionGroupStatus,
)
from backend.app.models.segment import (
    Segment,
    SegmentKind,
    SegmentMember,
    SegmentStatus,
)
from backend.app.models.user import User, UserRole
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.global_holdout_service import (
    GlobalHoldoutService,
    holdout_bucket,
)

pytestmark = pytest.mark.integration

URL = "/api/v1/tracking/assign/batch"
SINGLE_URL = "/api/v1/tracking/assign"
KEYS_URL = "/api/v1/api-keys"
SCHEMA = "test_experimentation"
HOLDOUT_PERCENTAGE = 20

#: The 403 texts, literally (``test_assign_batch_contract.py`` pins the constants).
MISSING_SCOPE = (
    "This API key does not have the 'sdk:ruleset' scope. Create a key with the "
    "'sdk:ruleset' scope for server-side SDK use."
)
OWNER_ROLE = (
    "This key's owner can no longer change feature flags or experiments, so the "
    "key is refused for server-side SDK use."
)
CHANGED = (
    "The experiment changed during the request. Users earlier in the list may "
    "already be assigned; resending the same request is safe."
)

NATIVE_US_ONLY_RULES = {
    "version": "1.0",
    "rules": [
        {
            "id": "us_only",
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


@pytest.fixture
def session_factory(db_session):
    # expire_on_commit stays True, as in production's SessionLocal: a route
    # that keeps an ORM object across a commit reloads it, and the statement
    # and bandit gates below see that.
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def make():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return make


@pytest.fixture
def client(session_factory):
    def override_get_db():
        session = session_factory()
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


def _make_user(db_session, role=UserRole.DEVELOPER) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"batch_{suffix}",
        email=f"batch_{suffix}@batch.test",
        full_name="Batch Assign User",
        hashed_password=get_password_hash("Demo1234!"),
        is_active=True,
        is_superuser=False,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def developer(db_session):
    return _make_user(db_session)


def _create_key(client, user, scopes=None) -> str:
    body = {"name": f"key-{uuid.uuid4().hex[:6]}"}
    if scopes is not None:
        body["scopes"] = scopes
    resp = client.post(
        KEYS_URL,
        json=body,
        headers={"Authorization": f"Bearer {create_local_access_token(user)}"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["key"]


@pytest.fixture
def headers(client, developer) -> Dict[str, str]:
    return {"X-API-Key": _create_key(client, developer, scopes=["sdk:ruleset"])}


def _cleanup(db_session, experiment_ids) -> None:
    db_session.rollback()
    for experiment_id in experiment_ids:
        db_session.query(BanditState).filter(
            BanditState.experiment_id == experiment_id
        ).delete()
        db_session.query(Event).filter(Event.experiment_id == experiment_id).delete()
        db_session.query(Assignment).filter(
            Assignment.experiment_id == experiment_id
        ).delete()
    db_session.commit()


def _experiment(db_session, make_experiment, label, arms=2, **kwargs) -> Experiment:
    suffix = uuid.uuid4().hex[:8]
    experiment = make_experiment(
        name=f"Batch {label} {suffix}",
        key=f"batch-{label}-{suffix}",
        status=ExperimentStatus.ACTIVE,
        **kwargs,
    )
    for i in range(arms):
        name = "control" if i == 0 else f"treatment-{i}"
        db_session.add(
            Variant(
                experiment_id=experiment.id,
                name=name,
                description=f"{name} variant",
                is_control=i == 0,
                traffic_allocation=100 / arms,
                configuration={"arm": name},
            )
        )
    db_session.commit()
    db_session.refresh(experiment)
    return experiment


@contextmanager
def _parked_holdouts(db_session):
    """No active global holdout for the duration, restored afterwards."""
    parked = (
        db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active.is_(True)).all()
    )
    for row in parked:
        row.is_active = False
    db_session.commit()
    try:
        yield
    finally:
        db_session.rollback()
        for row in parked:
            db_session.merge(row).is_active = True
        db_session.commit()


@pytest.fixture
def no_holdout(db_session):
    with _parked_holdouts(db_session):
        yield


@pytest.fixture
def plain_experiment(db_session, make_experiment, no_holdout):
    """Fixed allocation, no holdout, group or targeting."""
    experiment = _experiment(db_session, make_experiment, "plain")
    yield experiment
    _cleanup(db_session, [experiment.id])


@pytest.fixture
def everything(db_session, make_experiment):
    """One experiment E with every eligibility path, and its sibling S.

    E: a 3-arm Thompson bandit (weights 0.1/0.6/0.3) with US-only targeting,
    in an ACTIVE mutual exclusion group (traffic 1.0) with S, under an active
    20% global holdout. Ten users already hold an assignment in E (sticky),
    ten in S (sibling-held).
    """
    with _parked_holdouts(db_session):
        holdout = GlobalHoldout(
            name=f"batch-holdout-{uuid.uuid4().hex[:8]}",
            description="Batch parity holdout",
            holdout_percentage=HOLDOUT_PERCENTAGE,
            is_active=True,
        )
        db_session.add(holdout)
        group = MutualExclusionGroup(
            name=f"batch-meg-{uuid.uuid4().hex[:8]}",
            description="Batch parity group",
            traffic_allocation=1.0,
            status=MutualExclusionGroupStatus.ACTIVE,
        )
        db_session.add(group)
        db_session.commit()

        exp = _experiment(
            db_session,
            make_experiment,
            "everything",
            arms=3,
            experiment_type=ExperimentType.BANDIT,
            optimization_type="thompson_sampling",
            targeting_rules=NATIVE_US_ONLY_RULES,
            mutual_exclusion_group_id=group.id,
        )
        sibling = _experiment(
            db_session, make_experiment, "sibling", mutual_exclusion_group_id=group.id
        )
        arms = sorted(exp.variants, key=lambda v: v.name)
        db_session.add(
            BanditState(
                experiment_id=exp.id,
                algorithm="thompson_sampling",
                variant_weights={
                    str(arms[0].id): {"weight": 0.1},
                    str(arms[1].id): {"weight": 0.6},
                    str(arms[2].id): {"weight": 0.3},
                },
                total_pulls=0,
            )
        )
        sticky = [f"sticky-{uuid.uuid4().hex[:10]}" for _ in range(10)]
        # Outside the holdout, which is checked first: these users meet the
        # mutual exclusion check.  The holdout buckets with its own salt.
        held = []
        while len(held) < 10:
            user_id = f"held-{uuid.uuid4().hex[:10]}"
            bucket = holdout_bucket(user_id, holdout.hash_salt)
            if bucket >= HOLDOUT_PERCENTAGE:
                held.append(user_id)
        for i, user_id in enumerate(sticky):
            db_session.add(
                Assignment(
                    user_id=user_id,
                    experiment_id=exp.id,
                    variant_id=arms[i % 3].id,
                )
            )
        for user_id in held:
            db_session.add(
                Assignment(
                    user_id=user_id,
                    experiment_id=sibling.id,
                    variant_id=sibling.variants[0].id,
                )
            )
        db_session.commit()
        try:
            yield {"experiment": exp, "sticky": sticky, "held": held}
        finally:
            _cleanup(db_session, [exp.id, sibling.id])
            for experiment in (exp, sibling):
                experiment.mutual_exclusion_group_id = None
            db_session.commit()
            db_session.query(MutualExclusionGroup).filter(
                MutualExclusionGroup.id == group.id
            ).delete()
            db_session.query(GlobalHoldout).filter(
                GlobalHoldout.id == holdout.id
            ).delete()
            db_session.commit()


@pytest.fixture
def segment_experiment(db_session, make_experiment, admin_user, no_holdout):
    """An experiment targeted at an id-list segment of five members (#440)."""
    members = _users(5, "member")
    segment = Segment(
        name=f"batch-segment-{uuid.uuid4().hex[:8]}",
        kind=SegmentKind.ID_LIST.value,
        status=SegmentStatus.ACTIVE,
        owner_id=admin_user.id,
    )
    db_session.add(segment)
    db_session.commit()
    for member in members:
        db_session.add(SegmentMember(segment_id=segment.id, member_id=member))
    db_session.commit()
    experiment = _experiment(
        db_session,
        make_experiment,
        "segment",
        targeting_rules={
            "groups": [
                {
                    "conditions": [
                        {
                            "attribute": "segment",
                            "operator": "in_segment",
                            "value": str(segment.id),
                        }
                    ]
                }
            ]
        },
    )
    yield {"experiment": experiment, "members": members, "segment": str(segment.id)}
    _cleanup(db_session, [experiment.id])
    db_session.query(Segment).filter(Segment.id == segment.id).delete()
    db_session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _users(n: int, prefix: str = "u") -> List[str]:
    return [f"{prefix}-{uuid.uuid4().hex[:12]}" for _ in range(n)]


def _body(experiment, user_ids, contexts=None) -> dict:
    users = []
    for user_id in user_ids:
        entry = {"user_id": user_id}
        if contexts is not None:
            entry["context"] = contexts[user_id]
        users.append(entry)
    return {"experiment_key": experiment.key, "users": users}


def _assignment_rows(session_factory, experiment) -> Dict[str, str]:
    session = session_factory()
    try:
        rows = (
            session.query(Assignment.user_id, Assignment.variant_id)
            .filter(Assignment.experiment_id == experiment.id)
            .all()
        )
        return {user_id: str(variant_id) for user_id, variant_id in rows}
    finally:
        session.close()


def _event_rows(session_factory, experiment) -> List[Tuple[str, str, str]]:
    session = session_factory()
    try:
        rows = (
            session.query(Event.user_id, Event.event_type, Event.variant_id)
            .filter(Event.experiment_id == experiment.id)
            .all()
        )
        return sorted((u, t, str(v)) for u, t, v in rows)
    finally:
        session.close()


def _reset_to(session_factory, experiment, keep_user_ids) -> None:
    """Remove the rows a run wrote: every event of the experiment, and every
    assignment except those of *keep_user_ids* (the pre-seeded sticky users)."""
    session = session_factory()
    try:
        session.query(Event).filter(Event.experiment_id == experiment.id).delete()
        session.query(Assignment).filter(
            Assignment.experiment_id == experiment.id,
            Assignment.user_id.notin_(list(keep_user_ids)),
        ).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def _answers(resp) -> Dict[str, Tuple[str, bool, str]]:
    assert resp.status_code == 200, resp.text
    return {
        a["user_id"]: (a["variant_id"], a["assigned"], a["reason"])
        for a in resp.json()["assignments"]
    }


@contextmanager
def _statements(engine):
    """Record every SQL statement sent on *engine* inside the block."""
    seen: List[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


# ---------------------------------------------------------------------------
# Parity with N single calls
# ---------------------------------------------------------------------------


class TestParity:
    def _population(self, everything):
        """New users split across US/DE, plus the sticky and sibling-held ones."""
        new = _users(160)
        user_ids = new + everything["sticky"] + everything["held"]
        contexts = {
            user_id: {"country": "US" if i % 3 else "DE"}
            for i, user_id in enumerate(user_ids)
        }
        return user_ids, contexts

    def test_each_user_gets_what_a_single_call_gives(
        self, client, headers, everything, session_factory
    ):
        exp = everything["experiment"]
        user_ids, contexts = self._population(everything)
        before = _assignment_rows(session_factory, exp)
        assert set(before) == set(everything["sticky"])

        single = {}
        for user_id in user_ids:
            resp = client.post(
                SINGLE_URL,
                json={
                    "experiment_key": exp.key,
                    "user_id": user_id,
                    "context": contexts[user_id],
                },
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            single[user_id] = (data["variant_id"], data["assigned"], data["reason"])
        single_rows = _assignment_rows(session_factory, exp)
        single_events = _event_rows(session_factory, exp)

        _reset_to(session_factory, exp, everything["sticky"])
        assert _assignment_rows(session_factory, exp) == before
        assert _event_rows(session_factory, exp) == []

        batch_resp = client.post(
            URL, json=_body(exp, user_ids, contexts), headers=headers
        )
        batch = _answers(batch_resp)
        assert [a["user_id"] for a in batch_resp.json()["assignments"]] == user_ids

        # Not vacuous: every path is in the population.
        reasons = {reason for _v, _a, reason in single.values()}
        assert reasons == {"assigned", "holdout", "mutual_exclusion", "targeting"}
        assert all(single[u] == (before[u], True, "assigned") for u in before)
        assert all(single[u][2] == "mutual_exclusion" for u in everything["held"])
        assigned_variants = {v for v, a, _r in single.values() if a}
        assert len(assigned_variants) == 3, "the bandit routed to fewer than 3 arms"

        assert batch == single
        assert _assignment_rows(session_factory, exp) == single_rows
        # The single route records a view event for each enrolled user; the
        # batch route records none. Nothing else differs.
        assert {t for _u, t, _v in single_events} == {EventType.EXPOSURE.value}
        assert len(single_events) == sum(1 for _v, a, _r in single.values() if a)
        assert _event_rows(session_factory, exp) == [
            e for e in single_events if e[1] != EventType.EXPOSURE.value
        ]

        body = batch_resp.json()
        variants = {str(v.id): v for v in exp.variants}
        assert set(body["variants"]) == set(variants)
        for variant_id, entry in body["variants"].items():
            assert entry == {
                "name": variants[variant_id].name,
                "is_control": bool(variants[variant_id].is_control),
                "configuration": variants[variant_id].configuration,
            }
        expected_counts = {"assigned": 0, "holdout": 0, "mutual_exclusion": 0}
        expected_counts["targeting"] = 0
        for _v, _a, reason in single.values():
            expected_counts[reason] += 1
        assert body["counts"] == expected_counts

    def test_segment_targeted_users(
        self, client, headers, segment_experiment, session_factory
    ):
        """#440: the batch gives a segment-targeted experiment's users exactly
        what single calls give, and both give the right answer: an id-list
        member is assigned, a non-member is refused, and a non-member whose
        context carries a member's ``user_id`` or a ``$segments`` list is
        refused too."""
        exp, members = segment_experiment["experiment"], segment_experiment["members"]
        outsiders = _users(6, "out")
        user_ids = members + outsiders
        contexts = {user_id: {"country": "US"} for user_id in user_ids}
        contexts[outsiders[0]] = {"user_id": members[0]}
        contexts[outsiders[1]] = {"user": {"user_id": members[0]}}
        contexts[outsiders[2]] = {"$segments": [segment_experiment["segment"]]}

        single = {}
        for user_id in user_ids:
            resp = client.post(
                SINGLE_URL,
                json={
                    "experiment_key": exp.key,
                    "user_id": user_id,
                    "context": contexts[user_id],
                },
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            single[user_id] = (data["variant_id"], data["assigned"], data["reason"])
        single_rows = _assignment_rows(session_factory, exp)

        _reset_to(session_factory, exp, [])
        batch = _answers(
            client.post(URL, json=_body(exp, user_ids, contexts), headers=headers)
        )

        assert {u: single[u][1:] for u in members} == dict.fromkeys(
            members, (True, "assigned")
        )
        assert {u: single[u][1:] for u in outsiders} == dict.fromkeys(
            outsiders, (False, "targeting")
        )
        assert batch == single
        assert _assignment_rows(session_factory, exp) == single_rows

    def test_the_order_of_the_list_changes_no_answer(
        self, client, headers, everything, session_factory
    ):
        exp = everything["experiment"]
        user_ids, contexts = self._population(everything)
        forward = _answers(
            client.post(URL, json=_body(exp, user_ids, contexts), headers=headers)
        )
        rows = _assignment_rows(session_factory, exp)
        _reset_to(session_factory, exp, everything["sticky"])
        backward = _answers(
            client.post(URL, json=_body(exp, user_ids[::-1], contexts), headers=headers)
        )
        assert backward == forward
        assert _assignment_rows(session_factory, exp) == rows


class TestCounts:
    def test_counts_has_all_four_keys_when_three_of_them_are_zero(
        self, client, headers, plain_experiment
    ):
        """No holdout, no group and no targeting: every user is assigned, and
        the three other counts are present at 0, not left out."""
        resp = client.post(
            URL, json=_body(plain_experiment, _users(3)), headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["counts"] == {
            "assigned": 3,
            "holdout": 0,
            "mutual_exclusion": 0,
            "targeting": 0,
        }


class TestResend:
    def test_a_resend_returns_the_same_and_adds_no_rows(
        self, client, headers, plain_experiment, session_factory
    ):
        user_ids = _users(25)
        first = _answers(
            client.post(URL, json=_body(plain_experiment, user_ids), headers=headers)
        )
        rows = _assignment_rows(session_factory, plain_experiment)
        assert len(rows) == 25
        assert _event_rows(session_factory, plain_experiment) == []

        second = _answers(
            client.post(URL, json=_body(plain_experiment, user_ids), headers=headers)
        )
        assert second == first
        assert _assignment_rows(session_factory, plain_experiment) == rows
        assert _event_rows(session_factory, plain_experiment) == []

    def test_a_later_single_call_returns_the_same_variant_and_records_the_view(
        self, client, headers, plain_experiment, session_factory
    ):
        user_id = _users(1)[0]
        batch = _answers(
            client.post(URL, json=_body(plain_experiment, [user_id]), headers=headers)
        )
        resp = client.post(
            SINGLE_URL,
            json={"experiment_key": plain_experiment.key, "user_id": user_id},
            headers=headers,
        )
        assert resp.json()["variant_id"] == batch[user_id][0]
        events = _event_rows(session_factory, plain_experiment)
        assert events == [(user_id, EventType.EXPOSURE.value, batch[user_id][0])]


# ---------------------------------------------------------------------------
# Database work per request and per user
# ---------------------------------------------------------------------------


class TestDatabaseWork:
    def test_the_bandit_weights_are_read_once_per_request(
        self, client, headers, everything, db_session
    ):
        exp = everything["experiment"]
        user_ids = _users(50)
        contexts = {u: {"country": "US"} for u in user_ids}
        with _statements(db_session.get_bind()) as seen:
            resp = client.post(
                URL, json=_body(exp, user_ids, contexts), headers=headers
            )
        assert resp.status_code == 200, resp.text
        assert sum(1 for s in resp.json()["assignments"] if s["assigned"]) > 0
        reads = [s for s in seen if "bandit_states" in s]
        assert len(reads) == 1, len(reads)

    def test_statements_per_user_are_exact(
        self, client, headers, plain_experiment, db_session
    ):
        """Fixed allocation, no holdout active, no group, no targeting.

        Per new user, 7: the experiment (with its variants), the sticky
        lookup, the holdout lookup, the INSERT, the refresh, and the two reads
        of the stored assignment and its variant. Per sticky user, 4: the
        experiment, the sticky lookup and those two reads. The request's own
        work (the key, the experiment by key) is the same for any N.
        """
        engine = db_session.get_bind()

        def count(user_ids):
            with _statements(engine) as seen:
                resp = client.post(
                    URL, json=_body(plain_experiment, user_ids), headers=headers
                )
            assert resp.status_code == 200, resp.text
            return len(seen)

        count(_users(1))  # warm-up: first use of the key
        one, eleven = _users(1), _users(11)
        new_one, new_eleven = count(one), count(eleven)
        sticky_one, sticky_eleven = count(one), count(eleven)
        assert (new_eleven - new_one) == 10 * 7, (new_one, new_eleven)
        assert (sticky_eleven - sticky_one) == 10 * 4, (sticky_one, sticky_eleven)


# ---------------------------------------------------------------------------
# Holdout population (#445): the batch records what the single route records
# ---------------------------------------------------------------------------


def _population(session_factory, holdout_id) -> Dict[str, bool]:
    session = session_factory()
    try:
        rows = (
            session.query(HoldoutPopulation.user_id, HoldoutPopulation.in_holdout)
            .filter(HoldoutPopulation.holdout_id == holdout_id)
            .all()
        )
        return dict(rows)
    finally:
        session.close()


class TestHoldoutPopulation:
    def test_a_batch_records_the_population_the_single_route_records(
        self, client, headers, plain_experiment, db_session, session_factory
    ):
        """With an active, measurable holdout, a batch writes the same
        ``holdout_population`` rows as one single call per user."""
        holdout = GlobalHoldoutService(db_session).create_holdout(
            name=f"batch-population-{uuid.uuid4().hex[:8]}",
            holdout_percentage=HOLDOUT_PERCENTAGE,
            is_active=True,
        )
        assert holdout.is_measurable
        holdout_id = holdout.id
        try:
            user_ids = _users(80)
            single = {}
            for user_id in user_ids:
                resp = client.post(
                    SINGLE_URL,
                    json={"experiment_key": plain_experiment.key, "user_id": user_id},
                    headers=headers,
                )
                assert resp.status_code == 200, resp.text
                single[user_id] = resp.json()["reason"]
            single_population = _population(session_factory, holdout_id)

            _reset_to(session_factory, plain_experiment, [])
            session = session_factory()
            try:
                session.query(HoldoutPopulation).filter(
                    HoldoutPopulation.holdout_id == holdout_id
                ).delete()
                session.commit()
            finally:
                session.close()

            batch = _answers(
                client.post(
                    URL, json=_body(plain_experiment, user_ids), headers=headers
                )
            )
            batch_population = _population(session_factory, holdout_id)

            assert set(single_population) == set(user_ids)
            held = {u for u, reason in single.items() if reason == "holdout"}
            assert 0 < len(held) < len(user_ids)
            assert {u for u, inside in single_population.items() if inside} == held
            assert batch_population == single_population
            assert {u for u, (_v, _a, r) in batch.items() if r == "holdout"} == held
        finally:
            db_session.rollback()
            # holdout_population rows go with it (ON DELETE CASCADE).
            db_session.query(GlobalHoldout).filter(
                GlobalHoldout.id == holdout_id
            ).delete()
            db_session.commit()


# ---------------------------------------------------------------------------
# Refusals: nobody is assigned
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_1000_users_are_accepted(
        self, client, headers, plain_experiment, session_factory
    ):
        user_ids = _users(1000)
        resp = client.post(URL, json=_body(plain_experiment, user_ids), headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["counts"]["assigned"] == 1000
        assert len(_assignment_rows(session_factory, plain_experiment)) == 1000

    @pytest.mark.parametrize(
        "case",
        [
            "empty",
            "too_many",
            "duplicate",
            "id_too_long",
            "id_empty",
            "nul_id",
            "nul_context",
            "unknown_field",
            "unknown_user_field",
        ],
    )
    def test_a_422_assigns_nobody_and_repeats_no_id(
        self, client, headers, plain_experiment, session_factory, case
    ):
        ids = _users(4, prefix="secret")
        users = [{"user_id": u} for u in ids]
        body = {"experiment_key": plain_experiment.key, "users": users}
        expect_loc, expect_msg = None, None
        if case == "empty":
            body["users"] = []
            expect_loc = ["body", "users"]
            expect_msg = "List should have at least 1 item after validation, not 0"
        elif case == "too_many":
            body["users"] = [{"user_id": u} for u in _users(1001, prefix="secret")]
            expect_loc = ["body", "users"]
            expect_msg = "at most 1,000 users per request; split the list"
        elif case == "duplicate":
            users.append({"user_id": ids[1]})
            expect_loc = ["body", "users", 4, "user_id"]
            expect_msg = (
                "duplicate user_id; each user may appear once per request "
                "(first seen at users[1])"
            )
        elif case == "id_too_long":
            users[2]["user_id"] = "secret-" + "x" * 249
            expect_loc = ["body", "users", 2, "user_id"]
        elif case == "id_empty":
            users[2]["user_id"] = ""
            expect_loc = ["body", "users", 2, "user_id"]
        elif case == "nul_id":
            users[2]["user_id"] = "secret-\x00"
        elif case == "nul_context":
            users[2]["context"] = {"country": "U\x00S"}
        elif case == "unknown_field":
            body["track_view"] = True
            expect_loc = ["body", "track_view"]
        elif case == "unknown_user_field":
            users[2]["contxt"] = {"country": "US"}
            expect_loc = ["body", "users", 2, "contxt"]

        resp = client.post(URL, json=body, headers=headers)
        assert resp.status_code == 422, resp.text
        errors = resp.json()["detail"]
        if expect_loc is not None:
            assert errors[0]["loc"] == expect_loc, errors
        if expect_msg is not None:
            assert errors[0]["msg"] == expect_msg, errors
        assert "secret-" not in resp.text, resp.text
        assert _assignment_rows(session_factory, plain_experiment) == {}

    def test_255_characters_is_accepted(self, client, headers, plain_experiment):
        user_id = "u" * 255
        resp = client.post(
            URL, json=_body(plain_experiment, [user_id]), headers=headers
        )
        assert resp.status_code == 200, resp.text

    def test_unknown_or_inactive_experiment_is_404(
        self, client, headers, db_session, make_experiment, session_factory
    ):
        missing = f"missing-{uuid.uuid4().hex[:8]}"
        resp = client.post(
            URL,
            json={"experiment_key": missing, "users": [{"user_id": "u1"}]},
            headers=headers,
        )
        assert resp.status_code == 404
        assert (
            resp.json()["detail"] == f"Active experiment with key '{missing}' not found"
        )

        draft = _experiment(db_session, make_experiment, "draft")
        draft.status = ExperimentStatus.DRAFT
        db_session.commit()
        try:
            resp = client.post(URL, json=_body(draft, _users(3)), headers=headers)
            assert resp.status_code == 404
            assert _assignment_rows(session_factory, draft) == {}
        finally:
            _cleanup(db_session, [draft.id])

    def test_every_status_but_active_is_still_404_with_the_same_detail(
        self, client, headers, db_session, make_experiment, session_factory
    ):
        """The batch route keeps its 404 for an experiment that is not ACTIVE,
        and its detail, though ``POST /api/v1/tracking/assign`` now answers
        such an experiment with its control."""
        experiment = _experiment(db_session, make_experiment, "not-running")
        try:
            for status in (
                ExperimentStatus.DRAFT,
                ExperimentStatus.PAUSED,
                ExperimentStatus.COMPLETED,
                ExperimentStatus.ARCHIVED,
            ):
                experiment.status = status
                db_session.commit()
                resp = client.post(
                    URL, json=_body(experiment, _users(3)), headers=headers
                )
                assert (resp.status_code, resp.json()) == (
                    404,
                    {
                        "detail": f"Active experiment with key '{experiment.key}' "
                        "not found"
                    },
                ), status
                assert _assignment_rows(session_factory, experiment) == {}
        finally:
            _cleanup(db_session, [experiment.id])

    def test_no_key_is_401(self, client, plain_experiment, session_factory):
        resp = client.post(URL, json=_body(plain_experiment, _users(2)))
        assert resp.status_code == 401
        assert resp.json()["detail"] == "API key missing"
        assert _assignment_rows(session_factory, plain_experiment) == {}

    def test_a_key_without_the_scope_is_403(
        self, client, developer, plain_experiment, session_factory
    ):
        key = _create_key(client, developer)
        resp = client.post(
            URL, json=_body(plain_experiment, _users(2)), headers={"X-API-Key": key}
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == MISSING_SCOPE
        assert _assignment_rows(session_factory, plain_experiment) == {}

    @pytest.mark.parametrize("role", [UserRole.ANALYST, UserRole.VIEWER])
    def test_a_scoped_key_whose_owner_lost_the_role_is_403(
        self, client, db_session, plain_experiment, session_factory, role
    ):
        owner = _make_user(db_session)
        key = _create_key(client, owner, scopes=["sdk:ruleset"])
        owner.role = role
        db_session.commit()
        resp = client.post(
            URL, json=_body(plain_experiment, _users(2)), headers={"X-API-Key": key}
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == OWNER_ROLE
        assert _assignment_rows(session_factory, plain_experiment) == {}

    def test_a_trailing_slash_is_redirected_and_assigns_nobody(
        self, client, headers, plain_experiment, session_factory
    ):
        resp = client.post(
            URL + "/",
            json=_body(plain_experiment, _users(2)),
            headers=headers,
            follow_redirects=False,
        )
        assert resp.status_code == 307, resp.text
        assert resp.headers["location"].endswith(URL)
        assert _assignment_rows(session_factory, plain_experiment) == {}


# ---------------------------------------------------------------------------
# Failure part-way through a batch
# ---------------------------------------------------------------------------


def _fail_at(monkeypatch, index, action):
    """Run *action()* before assign_user call number *index* (from 0).

    Returns a function that puts the real assign_user back."""
    real = AssignmentService.assign_user
    calls = {"n": 0}

    def wrapped(self, *args, **kwargs):
        if calls["n"] == index:
            action()
        calls["n"] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(tracking.AssignmentService, "assign_user", wrapped)
    return lambda: monkeypatch.setattr(tracking.AssignmentService, "assign_user", real)


class TestMidBatch:
    def test_experiment_paused_part_way_is_409_and_a_resend_completes(
        self, client, headers, plain_experiment, session_factory, monkeypatch
    ):
        user_ids = _users(6)

        def pause():
            session = session_factory()
            try:
                session.query(Experiment).filter(
                    Experiment.id == plain_experiment.id
                ).update({"status": ExperimentStatus.PAUSED})
                session.commit()
            finally:
                session.close()

        restore = _fail_at(monkeypatch, 3, pause)
        resp = client.post(URL, json=_body(plain_experiment, user_ids), headers=headers)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == CHANGED
        assert set(_assignment_rows(session_factory, plain_experiment)) == set(
            user_ids[:3]
        )

        restore()
        session = session_factory()
        session.query(Experiment).filter(Experiment.id == plain_experiment.id).update(
            {"status": ExperimentStatus.ACTIVE}
        )
        session.commit()
        session.close()
        before = _assignment_rows(session_factory, plain_experiment)
        again = _answers(
            client.post(URL, json=_body(plain_experiment, user_ids), headers=headers)
        )
        assert set(again) == set(user_ids)
        assert all(again[u][0] == before[u] for u in user_ids[:3])
        assert set(_assignment_rows(session_factory, plain_experiment)) == set(user_ids)

    def test_any_value_error_part_way_is_the_same_409(
        self, client, headers, plain_experiment, session_factory, monkeypatch
    ):
        user_ids = _users(5, prefix="secret")

        def boom():
            raise ValueError(f"Override variant not valid for {user_ids[2]}")

        _fail_at(monkeypatch, 2, boom)
        resp = client.post(URL, json=_body(plain_experiment, user_ids), headers=headers)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == CHANGED
        assert "secret-" not in resp.text
        assert set(_assignment_rows(session_factory, plain_experiment)) == set(
            user_ids[:2]
        )

    def test_any_other_failure_part_way_is_500_and_a_resend_completes(
        self, client, headers, plain_experiment, session_factory, monkeypatch
    ):
        user_ids = _users(5, prefix="secret")

        def boom():
            raise RuntimeError(f"[parameters: {{'user_id': '{user_ids[2]}'}}]")

        restore = _fail_at(monkeypatch, 2, boom)
        resp = client.post(URL, json=_body(plain_experiment, user_ids), headers=headers)
        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"].startswith(
            "Could not assign the users to the experiment"
        )
        assert "secret-" not in resp.text
        assert set(_assignment_rows(session_factory, plain_experiment)) == set(
            user_ids[:2]
        )

        restore()
        again = _answers(
            client.post(URL, json=_body(plain_experiment, user_ids), headers=headers)
        )
        assert set(again) == set(user_ids)
        assert set(_assignment_rows(session_factory, plain_experiment)) == set(user_ids)
