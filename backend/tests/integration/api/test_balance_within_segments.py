"""An experiment that balances within saved segments (#448).

* Each new user is balanced in the first listed segment they belong to, an id
  list or a rules segment, else ``other``, through ``/tracking/assign`` and
  ``/tracking/assign/batch`` alike; on rows, per group, the seqs are exactly
  ``0 .. n - 1`` and every row is the block place of its seq. The same users
  with the segments listed the other way round are balanced in the other
  segment, so the order listed decides, not the ids.
* A user whose membership of a segment listed before the first one they
  belong to cannot be decided is balanced in ``unknown``; a segment that
  cannot be decided listed after it does not matter.
* A user who leaves their segment keeps their variant and their group.
* A segment that a draft, active or paused experiment balances within cannot
  be archived or made inactive: 409 ``segment_in_use`` naming the experiment.
  A completed experiment does not hold it, and neither does an attribute
  whose listed value happens to be the segment's id.
"""

from __future__ import annotations

import logging
import uuid
from typing import Dict, Iterator, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    Metric,
    MetricType,
    Variant,
)
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.segment import (
    Segment,
    SegmentKind,
    SegmentMember,
    SegmentStatus,
)
from backend.app.models.user import User, UserRole
from backend.app.services import balanced_allocation
from backend.tests.integration.balance_checks import balance_rows

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SCHEMA = "test_experimentation"
ASSIGN = "/api/v1/tracking/assign"
BATCH = "/api/v1/tracking/assign/batch"
SEGMENTS = "/api/v1/segments"

#: The rules segment's members: ``context.tier == "premium"``.
PREMIUM_RULES = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "tier", "operator": "equals", "value": "premium"}
            ],
        }
    ],
}
PREMIUM = {"tier": "premium"}

#: A rules segment on the dotted attribute ``user.tier``: targeting's alias
#: rule makes ``{"tier": ...}`` and ``{"user": {"tier": ...}}`` both match it.
DOTTED_PREMIUM_RULES = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "user.tier", "operator": "equals", "value": "premium"}
            ],
        }
    ],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_sign_in(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture(autouse=True)
def _no_active_holdout(db_session):
    """No global holdout for the test; restored after."""
    parked = (
        db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active.is_(True)).all()
    )
    for row in parked:
        row.is_active = False
    db_session.commit()
    yield
    db_session.rollback()
    for row in parked:
        db_session.merge(row).is_active = True
    db_session.commit()


@pytest.fixture
def session_factory(test_db):
    factory = sessionmaker(bind=test_db, autocommit=False, autoflush=False)

    def make():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return make


@pytest.fixture
def client(session_factory) -> Iterator[TestClient]:
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


def _owner(db_session) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"bseg_{suffix}",
        email=f"bseg_{suffix}@balance.test",
        full_name="Balanced within segments",
        hashed_password="unused: this user signs in by key only",
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def key(db_session) -> Dict[str, str]:
    """An ``sdk:ruleset`` key of a developer: valid on /assign and /assign/batch."""
    _row, plaintext = APIKey.create_for_user(
        db_session,
        _owner(db_session).id,
        f"bseg-{uuid.uuid4().hex[:8]}",
        scopes="sdk:ruleset",
    )
    return {"X-API-Key": plaintext}


@pytest.fixture
def made(db_session, admin_user):
    """Segments and experiments made for one test, deleted after it."""
    rows: Dict[str, List[uuid.UUID]] = {"segments": [], "experiments": []}

    def segment(
        *,
        members: Tuple[str, ...] = (),
        rules: Optional[dict] = None,
        status: SegmentStatus = SegmentStatus.ACTIVE,
    ) -> str:
        row = Segment(
            name=f"Balance segment {uuid.uuid4().hex[:8]}",
            kind=(SegmentKind.RULES if rules else SegmentKind.ID_LIST).value,
            rules=rules,
            status=status,
            owner_id=admin_user.id,
        )
        db_session.add(row)
        db_session.commit()
        for member in members:
            db_session.add(SegmentMember(segment_id=row.id, member_id=member))
        db_session.commit()
        rows["segments"].append(row.id)
        return str(row.id)

    def experiment(
        setting: Optional[dict], status: ExperimentStatus = ExperimentStatus.ACTIVE
    ) -> Experiment:
        suffix = uuid.uuid4().hex[:8]
        row = Experiment(
            name=f"Balanced by segment {suffix}",
            key=f"balanced-seg-{suffix}",
            status=status,
            owner_id=admin_user.id,
            balance_within=setting,
        )
        db_session.add(row)
        db_session.flush()
        for index in range(2):
            db_session.add(
                Variant(
                    experiment_id=row.id,
                    name="control" if index == 0 else "treatment1",
                    is_control=index == 0,
                    traffic_allocation=50,
                )
            )
        db_session.add(
            Metric(
                experiment_id=row.id,
                name="Purchase",
                event_name="purchase",
                metric_type=MetricType.CONVERSION,
                is_primary=True,
            )
        )
        db_session.commit()
        db_session.refresh(row)
        rows["experiments"].append(row.id)
        return row

    yield type("Made", (), {"segment": segment, "experiment": experiment})
    db_session.rollback()
    db_session.query(Experiment).filter(Experiment.id.in_(rows["experiments"])).delete(
        False
    )
    db_session.query(Segment).filter(Segment.id.in_(rows["segments"])).delete(False)
    db_session.commit()


def _assign(client, key, experiment, user_id, context=None):
    body = {"experiment_key": experiment.key, "user_id": user_id}
    if context is not None:
        body["context"] = context
    response = client.post(ASSIGN, json=body, headers=key)
    assert response.status_code == 200, response.text
    assert response.json()["assigned"] is True, response.text
    return response.json()


def _groups(session_factory, experiment_id) -> Dict[str, Optional[str]]:
    """user id -> stored group."""
    session = session_factory()
    try:
        return {
            row.user_id: row.balance_group
            for row in session.execute(
                text(
                    "SELECT user_id, balance_group FROM assignments "
                    "WHERE experiment_id = :e"
                ),
                {"e": str(experiment_id)},
            )
        }
    finally:
        session.close()


def _checked_rows(session_factory, experiment_id):
    session = session_factory()
    try:
        found = balance_rows(session, experiment_id)
    finally:
        session.close()
    assert found.problems == [], found.problems[:10]
    assert found.outside_blocks() == 0
    return found


# ---------------------------------------------------------------------------
# The first listed segment a user belongs to
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_each_user_is_balanced_in_the_first_listed_segment_they_belong_to(
    client, key, made, session_factory
):
    """Users of an id list (``A``), of a rules segment (``B``), of both, and
    of neither, assigned one at a time and in lists; then the same users in an
    experiment that lists the two the other way round."""
    users: Dict[str, Tuple[str, dict]] = {}
    for kind, count in (("a", 7), ("b", 6), ("both", 5), ("none", 4)):
        for i in range(count):
            users[f"seg-{kind}-{i}-{uuid.uuid4().hex[:8]}"] = (
                kind,
                PREMIUM if kind in ("b", "both") else {"tier": "basic"},
            )
    in_a = tuple(u for u, (kind, _c) in users.items() if kind in ("a", "both"))
    a = made.segment(members=in_a)
    b = made.segment(rules=PREMIUM_RULES)

    for listed, both_in in (((a, b), a), ((b, a), b)):
        experiment = made.experiment({"segments": list(listed)})
        ordered = list(users.items())
        for start in range(0, len(ordered), 6):
            chunk = ordered[start : start + 6]
            batch = client.post(
                BATCH,
                json={
                    "experiment_key": experiment.key,
                    "users": [{"user_id": u, "context": c} for u, (_k, c) in chunk[:3]],
                },
                headers=key,
            )
            assert batch.status_code == 200, batch.text
            assert batch.json()["counts"]["assigned"] == len(chunk[:3]), batch.text
            for user_id, (_kind, context) in chunk[3:]:
                _assign(client, key, experiment, user_id, context)

        want = {"a": a, "b": b, "both": both_in, "none": "other"}
        stored = _groups(session_factory, experiment.id)
        wrong = {
            u: (kind, stored.get(u))
            for u, (kind, _c) in users.items()
            if stored.get(u) != want[kind]
        }
        assert wrong == {}, f"listed {listed}: {len(wrong)} users in the wrong group"
        found = _checked_rows(session_factory, experiment.id)
        sizes: Dict[str, int] = {}
        for kind, _c in users.values():
            sizes[want[kind]] = sizes.get(want[kind], 0) + 1
        assert {g: rows.next_seq for g, rows in found.groups.items()} == sizes


@pytest.mark.regression
def test_a_user_whose_membership_cannot_be_decided_is_balanced_in_unknown(
    client, key, made, session_factory
):
    """``C`` is made inactive past the segment routes (which refuse it while
    an experiment balances within it), so its membership cannot be decided."""
    member = f"undecided-{uuid.uuid4().hex[:8]}"
    outsider = f"undecided-out-{uuid.uuid4().hex[:8]}"
    a = made.segment(members=(member,))
    c = made.segment(members=(member, outsider), status=SegmentStatus.INACTIVE)
    first_c = made.experiment({"segments": [c, a]})
    first_a = made.experiment({"segments": [a, c]})

    for experiment in (first_c, first_a):
        for user_id in (member, outsider):
            _assign(client, key, experiment, user_id, {})

    assert _groups(session_factory, first_c.id) == {
        member: "unknown",
        outsider: "unknown",
    }
    assert _groups(session_factory, first_a.id) == {
        member: a,
        outsider: "unknown",
    }
    for experiment in (first_c, first_a):
        _checked_rows(session_factory, experiment.id)


@pytest.mark.regression
def test_a_user_who_leaves_their_segment_keeps_their_variant_and_group(
    client, key, made, session_factory, db_session
):
    user_id = f"leaver-{uuid.uuid4().hex[:8]}"
    a = made.segment(members=(user_id,))
    experiment = made.experiment({"segments": [a]})
    first = _assign(client, key, experiment, user_id, {})
    db_session.query(SegmentMember).filter(
        SegmentMember.segment_id == uuid.UUID(a), SegmentMember.member_id == user_id
    ).delete(False)
    db_session.commit()

    again = _assign(client, key, experiment, user_id, {})

    assert again["variant_id"] == first["variant_id"]
    assert _groups(session_factory, experiment.id) == {user_id: a}
    assert _checked_rows(session_factory, experiment.id).groups[a].next_seq == 1


# ---------------------------------------------------------------------------
# A segment an experiment balances within is in use
# ---------------------------------------------------------------------------

BALANCED_IN_USE = (
    "This segment is used by 1 experiment. Remove it from the segments a draft "
    "balances within first; a started experiment that balances within it uses it "
    "until it is completed or archived."
)


@pytest.mark.regression
@pytest.mark.parametrize(
    "status", [ExperimentStatus.DRAFT, ExperimentStatus.ACTIVE, ExperimentStatus.PAUSED]
)
def test_a_segment_an_experiment_balances_within_cannot_be_archived(
    admin_client, made, db_session, status
):
    segment_id = made.segment()
    experiment = made.experiment({"segments": [made.segment(), segment_id]}, status)

    expected = {
        "detail": {
            "code": "segment_in_use",
            "message": BALANCED_IN_USE,
            "feature_flags": [],
            "experiments": [
                {
                    "id": str(experiment.id),
                    "key": experiment.key,
                    "name": experiment.name,
                    "status": status.value,
                }
            ],
        }
    }
    archive = admin_client.delete(f"{SEGMENTS}/{segment_id}")
    assert archive.status_code == 409, archive.text
    assert archive.json() == expected
    for new_status in ("inactive", "archived"):
        deactivate = admin_client.put(
            f"{SEGMENTS}/{segment_id}", json={"status": new_status}
        )
        assert deactivate.status_code == 409, deactivate.text
        assert deactivate.json() == expected
    db_session.rollback()
    stored = db_session.query(Segment).filter(Segment.id == segment_id).one()
    assert stored.status == SegmentStatus.ACTIVE


@pytest.mark.regression
def test_a_completed_experiment_or_an_attribute_value_does_not_hold_a_segment(
    admin_client, made, db_session
):
    segment_id = made.segment()
    made.experiment({"segments": [segment_id]}, ExperimentStatus.COMPLETED)
    made.experiment(
        {"attribute": "tier", "values": [segment_id]}, ExperimentStatus.ACTIVE
    )

    response = admin_client.delete(f"{SEGMENTS}/{segment_id}")

    assert response.status_code == 204, response.text
    db_session.rollback()
    stored = db_session.query(Segment).filter(Segment.id == segment_id).one()
    assert stored.status == SegmentStatus.ARCHIVED


@pytest.mark.regression
def test_the_listing_names_an_experiment_that_balances_within_the_segment(
    admin_client, made
):
    """The "used by" listing and archiving read the same stored setting, so
    the listing cannot say no one uses a segment that archiving refuses."""
    segment_id = made.segment()
    draft = made.experiment({"segments": [segment_id]}, ExperimentStatus.DRAFT)
    made.experiment({"attribute": "tier", "values": [segment_id]})

    listing = admin_client.get(f"{SEGMENTS}/{segment_id}/experiments")
    archive = admin_client.delete(f"{SEGMENTS}/{segment_id}")

    assert archive.status_code == 409, archive.text
    assert listing.status_code == 200, listing.text
    assert [e["id"] for e in listing.json()["experiments"]] == [str(draft.id)]
    assert [e["id"] for e in archive.json()["detail"]["experiments"]] == [str(draft.id)]


# ---------------------------------------------------------------------------
# Claims of the guide, the body and the commits, each pinned
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_membership_is_decided_as_it_is_for_targeting_rules(
    client, key, made, session_factory
):
    """A rules segment on ``user.tier`` has a user sent ``{"tier": ...}`` or
    ``{"user": {"tier": ...}}`` as a member: the context is expanded first."""
    segment_id = made.segment(rules=DOTTED_PREMIUM_RULES)
    experiment = made.experiment({"segments": [segment_id]})
    flat = f"alias-flat-{uuid.uuid4().hex[:8]}"
    nested = f"alias-nested-{uuid.uuid4().hex[:8]}"

    _assign(client, key, experiment, flat, {"tier": "premium"})
    _assign(client, key, experiment, nested, {"user": {"tier": "premium"}})

    assert _groups(session_factory, experiment.id) == {
        flat: segment_id,
        nested: segment_id,
    }


@pytest.mark.regression
def test_a_stored_setting_that_cannot_be_read_holds_the_segment(admin_client, made):
    """The archive check fails closed: segments stored as text, not a list."""
    segment_id = made.segment()
    made.experiment({"segments": f"{segment_id}"}, ExperimentStatus.DRAFT)

    response = admin_client.delete(f"{SEGMENTS}/{segment_id}")

    assert response.status_code == 409, response.text


@pytest.mark.regression
def test_an_undecided_membership_is_logged_once_per_experiment(
    client, key, made, session_factory, caplog
):
    inactive = made.segment(members=("x",), status=SegmentStatus.INACTIVE)
    experiment = made.experiment({"segments": [inactive]})
    balanced_allocation._undecided_notes.clear()

    with caplog.at_level(logging.WARNING, logger=balanced_allocation.__name__):
        for index in range(3):
            _assign(
                client, key, experiment, f"undec-{index}-{uuid.uuid4().hex[:6]}", {}
            )

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == balanced_allocation.__name__
        and "could not" in record.getMessage()
    ]
    assert len(lines) == 1, lines
    assert str(experiment.id) in lines[0] and inactive in lines[0]
