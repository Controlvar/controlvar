"""The segment preview changes nothing on /tracking/assign or flag evaluation (#830).

The preview reads the attributes users sent as ``context`` from the exposure
rows that ``/tracking/assign`` already writes. So the two request paths an
SDK calls on every page view must run exactly the statements they ran before
#830: no new write, no new read, no new column. Each list below was recorded
through the routes on the commit before the change and is compared for
equality, statement by statement (not a count, not ``<=``).

A statement is pinned as its verb and table (to read a failure) and the first
16 hex digits of the SHA-256 of its text with whitespace collapsed (so a new
column, a new filter or a reordered clause fails too). COMMIT is not a cursor
statement and is not seen. A change to these paths that is meant must
re-record the lists, and say why in its pull request.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from typing import Iterator, List, Tuple

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import update

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import ExperimentStatus, Variant
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.models.global_holdout import GlobalHoldout

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

#: A new user's ``/tracking/assign``, with a context, and no active holdout.
ASSIGN_NEW_USER = [
    ("SET search_path TO test_experimentation", "ff5c96ca3f3a2d4d"),
    ("SELECT experiments", "deb03a6ac648343e"),
    ("SELECT experiments+variants", "92e5c9fa28954735"),
    ("SELECT assignments", "9f4f07bfbdd34878"),
    ("SELECT global_holdouts", "0b4c48f8051086b3"),
    ("INSERT assignments", "22aea8689339e06a"),
    ("SELECT assignments", "670c054a379fec6a"),
    ("INSERT events", "19bd997a94b8dc62"),
    ("SELECT events", "dc44e28bf432a997"),
    ("SELECT assignments", "9f4f07bfbdd34878"),
    ("SELECT variants", "17dfe07abcf55f97"),
    ("SELECT experiments", "dc0a2d5b8215565b"),
    ("SELECT variants", "ef2cccb95e119ad1"),
]

#: The same user and context again: the stored variant, and one exposure.
ASSIGN_STICKY_REPEAT = [
    ("SET search_path TO test_experimentation", "ff5c96ca3f3a2d4d"),
    ("SELECT experiments", "deb03a6ac648343e"),
    ("SELECT experiments+variants", "92e5c9fa28954735"),
    ("SELECT assignments", "9f4f07bfbdd34878"),
    ("SELECT assignments", "9f4f07bfbdd34878"),
    ("SELECT variants", "17dfe07abcf55f97"),
    ("INSERT events", "19bd997a94b8dc62"),
    ("SELECT events", "dc44e28bf432a997"),
    ("SELECT experiments", "dc0a2d5b8215565b"),
    ("SELECT variants", "ef2cccb95e119ad1"),
]

#: ``GET /feature-flags/evaluate/{key}`` with a context, a flag at 100%.
FLAG_EVALUATE = [
    ("SET search_path TO test_experimentation", "ff5c96ca3f3a2d4d"),
    ("SELECT feature_flags", "3ef8f6e666d27638"),
    ("INSERT raw_metrics", "fd0a8005b25de354"),
    ("SELECT raw_metrics", "b0caed01a9fe2077"),
    ("INSERT raw_metrics", "fd0a8005b25de354"),
    ("SELECT raw_metrics", "b0caed01a9fe2077"),
    ("SELECT feature_flags", "0ac4844d985ed346"),
]


def _brief(statement: str) -> str:
    words = statement.split()
    verb = words[0].upper()
    if verb == "SET":
        return statement
    if verb == "INSERT":
        return f"INSERT {words[2].split('.')[-1]}"
    if verb == "UPDATE":
        return f"UPDATE {words[1].split('.')[-1]}"
    upper = [w.upper() for w in words]
    tables = []
    for i, w in enumerate(upper):
        if w in ("FROM", "JOIN") and i + 1 < len(words):
            name = words[i + 1].strip("(),")
            if name.startswith("test_experimentation."):
                tables.append(name.split(".")[-1])
    return f"{verb} {'+'.join(dict.fromkeys(tables)) or '?'}"


def _pinned(statements: List[str]) -> List[Tuple[str, str]]:
    return [
        (_brief(s), hashlib.sha256(s.encode()).hexdigest()[:16]) for s in statements
    ]


@contextmanager
def _statements(engine) -> Iterator[List[str]]:
    seen: List[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()))

    sa_event.listen(engine, "before_cursor_execute", _before)
    try:
        yield seen
    finally:
        sa_event.remove(engine, "before_cursor_execute", _before)


@pytest.fixture
def no_active_holdout(db_session):
    """Park every active holdout for the test, and put them back after."""
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
    yield
    db_session.execute(
        update(GlobalHoldout).where(GlobalHoldout.id.in_(parked)).values(is_active=True)
    )
    db_session.commit()


@pytest.fixture
def active_experiment(db_session, make_experiment):
    suffix = uuid.uuid4().hex[:8]
    experiment = make_experiment(
        name=f"Hot path {suffix}",
        key=f"hot-path-{suffix}",
        status=ExperimentStatus.ACTIVE,
    )
    for name, is_control in (("control", True), ("treatment", False)):
        db_session.add(
            Variant(
                experiment_id=experiment.id,
                name=name,
                description=f"{name} variant",
                is_control=is_control,
                traffic_allocation=50.0,
            )
        )
    db_session.commit()
    db_session.refresh(experiment)
    yield experiment
    db_session.rollback()
    db_session.query(Event).filter(Event.experiment_id == experiment.id).delete()
    db_session.query(Assignment).filter(
        Assignment.experiment_id == experiment.id
    ).delete()
    db_session.commit()


@pytest.mark.regression
def test_assign_runs_the_statements_it_ran_before(
    admin_client, db_session, active_experiment, no_active_holdout
):
    user_id = f"hot-{uuid.uuid4().hex[:8]}"
    body = {
        "experiment_key": active_experiment.key,
        "user_id": user_id,
        "context": {"country": "US", "plan": "pro"},
    }
    engine = db_session.get_bind()
    with _statements(engine) as new_user:
        response = admin_client.post("/api/v1/tracking/assign", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["reason"] == "assigned"
    with _statements(engine) as sticky:
        response = admin_client.post("/api/v1/tracking/assign", json=body)
    assert response.status_code == 200, response.text

    assert _pinned(new_user) == ASSIGN_NEW_USER
    assert _pinned(sticky) == ASSIGN_STICKY_REPEAT


@pytest.mark.regression
def test_flag_evaluation_runs_the_statements_it_ran_before(
    admin_client, db_session, make_feature_flag
):
    flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=100)
    context = json.dumps({"country": "US"})
    with _statements(db_session.get_bind()) as seen:
        response = admin_client.get(
            f"/api/v1/feature-flags/evaluate/{flag.key}",
            params={"user_id": f"hot-{uuid.uuid4().hex[:8]}", "context": context},
        )
    assert response.status_code == 200, response.text

    assert _pinned(seen) == FLAG_EVALUATE
