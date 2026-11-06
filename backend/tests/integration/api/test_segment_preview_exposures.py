"""The segment preview estimates from recent exposure contexts (#830).

``POST /api/v1/segments/{id}/preview`` reads the exposures ``/tracking/assign``
writes (the attributes sent as ``context`` are stored on each), newest first
from the last 30 days, keeps each user's latest one, and evaluates the rules
against up to ``sample_size`` users with attributes. Through the routes,
against a real database, every population is written through the public API
(``/tracking/assign``, or ``/tracking/track`` and ``/tracking/batch`` where a
row needs a timestamp or a body ``/assign`` cannot send), never by writing
events or ``Assignment.context`` directly.

The preview reads every exposure in the database, and the test database is
shared by the session and not truncated, so each test starts from no exposure
rows (``no_exposures``) and with no active holdout (a held-out user gets no
exposure).
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, List, Tuple

import pytest
import sqlalchemy as sa
from sqlalchemy import delete, update
from sqlalchemy import event as sa_event

from backend.app.api.v1.endpoints.segments import PREVIEW_TIMED_OUT
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import ExperimentStatus, Variant
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.user import User, UserRole
from backend.app.services import audience_service
from backend.app.services.event_matching import EXPOSURE_EVENT_TYPES
from backend.tests.integration.conftest import HASHED_PASSWORD, make_client_for_user

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

COLLECTION = "/api/v1/segments"
ASSIGN = "/api/v1/tracking/assign"
TRACK = "/api/v1/tracking/track"
BATCH = "/api/v1/tracking/batch"

#: The answer's fields, exactly (the SPEC).
FIELDS = {
    "estimated_percentage",
    "sample_size",
    "matched",
    "source",
    "window_days",
    "sampled_since",
    "ended_by",
    "users_without_context",
    "reason",
}


def _cond(attribute: str, operator: str, value):
    return {"attribute": attribute, "operator": operator, "value": value}


def _rules(*conditions):
    return {
        "logical_operator": "AND",
        "groups": [{"logical_operator": "AND", "conditions": list(conditions)}],
    }


US = _rules(_cond("country", "equals", "US"))


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def no_exposures(db_session):
    """Start from no exposure rows, with every active holdout parked."""
    db_session.execute(delete(Event).where(Event.event_type.in_(EXPOSURE_EVENT_TYPES)))
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
    db_session.rollback()
    db_session.execute(
        update(GlobalHoldout).where(GlobalHoldout.id.in_(parked)).values(is_active=True)
    )
    db_session.commit()


def _active_experiment(db_session, make_experiment):
    suffix = uuid.uuid4().hex[:8]
    experiment = make_experiment(
        name=f"Preview {suffix}",
        key=f"preview-{suffix}",
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
    return experiment


@pytest.fixture
def experiments(db_session, make_experiment, no_exposures):
    """Three ACTIVE experiments; their rows are removed afterwards."""
    made = [_active_experiment(db_session, make_experiment) for _ in range(3)]
    yield made
    db_session.rollback()
    ids = [e.id for e in made]
    db_session.query(Event).filter(Event.experiment_id.in_(ids)).delete(
        synchronize_session=False
    )
    db_session.query(Assignment).filter(Assignment.experiment_id.in_(ids)).delete(
        synchronize_session=False
    )
    db_session.commit()


@pytest.fixture
def experiment(experiments):
    return experiments[0]


@pytest.fixture
def segment_id(admin_client) -> str:
    response = admin_client.post(
        COLLECTION, json={"name": f"Preview {uuid.uuid4().hex[:8]}", "rules": US}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _prefix() -> str:
    return f"pv-{uuid.uuid4().hex[:6]}"


def _assign(client, experiment, user_id: str, context=None) -> None:
    body = {"experiment_key": experiment.key, "user_id": user_id}
    if context is not None:
        body["context"] = context
    response = client.post(ASSIGN, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["assigned"] is True, response.text


def _batch(client, events: List[dict]) -> None:
    for start in range(0, len(events), 100):
        response = client.post(BATCH, json={"events": events[start : start + 100]})
        assert response.status_code == 200, response.text


def _exposure(user_id: str, at: datetime, metadata=None) -> dict:
    event = {"event_type": "exposure", "user_id": user_id, "timestamp": at.isoformat()}
    if metadata is not None:
        event["metadata"] = metadata
    return event


def _preview(client, segment_id: str, rules=US, sample_size: int = 1000, **params):
    response = client.post(
        f"{COLLECTION}/{segment_id}/preview",
        params={"sample_size": sample_size, **params},
        json={"name": "Preview", "rules": rules},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == FIELDS
    return body


def _stored_times(db_session, prefix: str) -> List[str]:
    """``created_at`` of every exposure of the users named ``prefix...``, newest first."""
    db_session.expire_all()
    return [
        row.created_at
        for row in db_session.query(Event)
        .filter(
            Event.event_type.in_(EXPOSURE_EVENT_TYPES),
            Event.user_id.like(f"{prefix}%"),
        )
        .order_by(Event.created_at.desc())
    ]


@contextmanager
def _statements(engine) -> Iterator[List[Tuple[str, object]]]:
    seen: List[Tuple[str, object]] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        seen.append((" ".join(statement.split()), parameters))

    sa_event.listen(engine, "before_cursor_execute", _before)
    try:
        yield seen
    finally:
        sa_event.remove(engine, "before_cursor_execute", _before)


def _preview_read(seen) -> Tuple[str, object]:
    [read] = [s for s in seen if "FROM test_experimentation.events" in s[0]]
    return read


# ---------------------------------------------------------------------------
# Row 1: a known population gives its known share
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_preview_counts_users_who_were_assigned_through_the_tracking_api(
    admin_client, db_session, experiment, segment_id
):
    """On main the preview read ``Assignment.context``, which /tracking/assign
    never writes: 1,000 assigned users previewed as 0 of 0."""
    prefix = _prefix()
    for i in range(1000):
        country = "US" if i % 10 < 3 else "DE"
        _assign(admin_client, experiment, f"{prefix}-{i:04d}", {"country": country})

    body = _preview(admin_client, segment_id, sample_size=1000)
    assert body == {
        "estimated_percentage": 30.0,
        "sample_size": 1000,
        "matched": 300,
        "source": "exposure_metadata",
        "window_days": 30,
        "sampled_since": _stored_times(db_session, prefix)[-1],
        "ended_by": "sample_size",
        "users_without_context": 0,
        "reason": None,
    }

    wider = _preview(admin_client, segment_id, sample_size=2000)
    assert (wider["matched"], wider["sample_size"]) == (300, 1000)
    assert wider["estimated_percentage"] == 30.0
    assert wider["ended_by"] == "window"
    assert wider["sampled_since"] == body["sampled_since"]


# ---------------------------------------------------------------------------
# Row 2: the newest users are read, through an ORDER BY
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_preview_uses_newest_not_oldest_rows(
    admin_client, db_session, experiment, segment_id
):
    prefix = _prefix()
    for i in range(1000):
        _assign(admin_client, experiment, f"{prefix}-old-{i:04d}", {"country": "US"})
    for i in range(1000):
        _assign(admin_client, experiment, f"{prefix}-new-{i:04d}", {"country": "DE"})

    with _statements(db_session.get_bind()) as seen:
        body = _preview(admin_client, segment_id, sample_size=1000)
    assert (body["estimated_percentage"], body["matched"], body["sample_size"]) == (
        0.0,
        0,
        1000,
    )
    assert body["ended_by"] == "sample_size"
    statement, _ = _preview_read(seen)
    assert "ORDER BY test_experimentation.events.created_at DESC" in statement


# ---------------------------------------------------------------------------
# Rows 3 and 4: one user is counted once, with their latest attributes
# ---------------------------------------------------------------------------


def test_each_user_is_counted_once_across_experiments_and_repeats(
    admin_client, experiments, segment_id
):
    prefix = _prefix()
    for i in range(100):
        for experiment in experiments:
            for _ in range(3):
                _assign(
                    admin_client, experiment, f"{prefix}-{i:03d}", {"country": "US"}
                )

    body = _preview(admin_client, segment_id, sample_size=1000)
    assert (body["sample_size"], body["matched"]) == (100, 100)
    assert body["ended_by"] == "window"


def test_a_users_latest_attributes_are_the_ones_evaluated(
    admin_client, experiment, segment_id
):
    prefix = _prefix()
    pro = _rules(_cond("plan", "equals", "pro"))
    _assign(admin_client, experiment, f"{prefix}-mover", {"plan": "free"})
    for i in range(29):
        _assign(admin_client, experiment, f"{prefix}-{i:02d}", {"plan": "basic"})
    _assign(admin_client, experiment, f"{prefix}-mover", {"plan": "pro"})

    body = _preview(admin_client, segment_id, rules=pro)
    assert (body["matched"], body["sample_size"]) == (1, 30)
    assert body["estimated_percentage"] == round(100 / 30, 2)


# ---------------------------------------------------------------------------
# Row 8: a fixed number of statements, whatever the number of users
# ---------------------------------------------------------------------------

#: The route's statements: the session's search path, the signed-in user, the
#: segment, the statement timeout, and the one read.
PREVIEW_STATEMENTS = [
    "SET search_path TO test_experimentation",
    "SELECT users",
    "SELECT segments",
    "SELECT set_config",
    "SELECT events",
]


def _brief(statement: str) -> str:
    if statement.startswith("SET "):
        return statement
    if "set_config(" in statement:
        return "SELECT set_config"
    table = statement.split(" FROM test_experimentation.", 1)[1].split()[0]
    return f"{statement.split()[0]} {table}"


def test_the_preview_runs_the_same_statements_for_10_and_1000_users(
    admin_client, db_session, experiment, segment_id
):
    prefix = _prefix()
    engine = db_session.get_bind()
    counts = []
    for size in (10, 1000):
        for i in range(len(_stored_times(db_session, prefix)), size):
            _assign(admin_client, experiment, f"{prefix}-{i:04d}", {"country": "US"})
        with _statements(engine) as seen:
            body = _preview(admin_client, segment_id, sample_size=1000)
        assert body["sample_size"] == size
        counts.append([_brief(statement) for statement, _ in seen])
    assert counts == [PREVIEW_STATEMENTS, PREVIEW_STATEMENTS]


# ---------------------------------------------------------------------------
# Row 9: the read is bounded by the cap and by sample_size
# ---------------------------------------------------------------------------


def test_the_read_is_capped_and_stops_at_sample_size(
    admin_client, db_session, no_exposures, segment_id
):
    prefix = _prefix()
    start = datetime.now(timezone.utc) - timedelta(hours=6)
    _batch(
        admin_client,
        [
            _exposure(
                f"{prefix}-{i:05d}", start + timedelta(seconds=i), {"country": "US"}
            )
            for i in range(10001)
        ],
    )
    times = _stored_times(db_session, prefix)
    assert len(times) == 10001

    with _statements(db_session.get_bind()) as seen:
        body = _preview(admin_client, segment_id, sample_size=10000)
    assert (body["sample_size"], body["matched"]) == (10000, 10000)
    assert body["ended_by"] == "sample_size"
    # The 10,000th newest user's row is the oldest read; the one before it is not.
    assert body["sampled_since"] == times[9999]
    _, parameters = _preview_read(seen)
    assert parameters["param_1"] == 50_000

    with _statements(db_session.get_bind()) as seen:
        small = _preview(admin_client, segment_id, sample_size=10)
    assert small["sample_size"] == 10
    _, parameters = _preview_read(seen)
    assert parameters["param_1"] == audience_service.preview_candidate_cap(10) == 50

    response = admin_client.post(
        f"{COLLECTION}/{segment_id}/preview?sample_size=10001",
        json={"name": "Preview", "rules": US},
    )
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Rows 10 and 13: the window is the server's 30 days
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_exposures_older_than_the_window_are_not_read(
    admin_client, no_exposures, segment_id
):
    prefix = _prefix()
    now = datetime.now(timezone.utc)
    _batch(
        admin_client,
        [
            _exposure(f"{prefix}-recent", now - timedelta(days=1), {"country": "US"}),
            _exposure(f"{prefix}-old", now - timedelta(days=100), {"country": "US"}),
        ],
    )
    body = _preview(admin_client, segment_id)
    assert (body["sample_size"], body["matched"]) == (1, 1)
    assert body["window_days"] == 30
    assert body["ended_by"] == "window"
    # A window_days query parameter is not part of the route: it is ignored.
    assert _preview(admin_client, segment_id, window_days=365) == body


# ---------------------------------------------------------------------------
# Row 11: every role that can read segments may preview
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role", [UserRole.ADMIN, UserRole.DEVELOPER, UserRole.ANALYST, UserRole.VIEWER]
)
def test_every_role_may_preview(db_session, admin_client, no_exposures, role):
    segment = admin_client.post(
        COLLECTION, json={"name": f"Roles {uuid.uuid4().hex[:8]}", "rules": US}
    ).json()
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"pv_{role.value}_{suffix}",
        email=f"pv_{role.value}_{suffix}@int.test",
        full_name="Preview role",
        hashed_password=HASHED_PASSWORD,
        is_active=True,
        is_superuser=False,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    client = make_client_for_user(db_session, user)
    body = _preview(client, segment["id"])
    assert body["reason"] == "no_exposures_with_context"


# ---------------------------------------------------------------------------
# Row 12: no stored attribute leaves the server
# ---------------------------------------------------------------------------

SECRET = "secret@x.test"


def test_no_stored_attribute_is_echoed_or_logged(
    admin_client, experiment, segment_id, caplog
):
    prefix = _prefix()
    for i in range(30):
        _assign(
            admin_client,
            experiment,
            f"{prefix}-{i:02d}",
            {"country": "US", "email": SECRET},
        )
    caplog.set_level(logging.DEBUG)
    response = admin_client.post(
        f"{COLLECTION}/{segment_id}/preview?sample_size=100",
        json={
            "name": "Preview",
            "rules": _rules(_cond("email", "regex", r"@y\.test$")),
        },
    )
    assert response.status_code == 200, response.text
    assert set(response.json()) == FIELDS
    assert response.json()["sample_size"] == 30
    refused = admin_client.post(
        f"{COLLECTION}/{segment_id}/preview?sample_size=5",
        json={"name": "Preview", "rules": US},
    )
    assert refused.status_code == 422
    for text in (response.text, refused.text, caplog.text):
        assert SECRET not in text


# ---------------------------------------------------------------------------
# Row 15: no attributes is not a percentage
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_preview_empty_sample_is_not_a_percentage(
    admin_client, db_session, experiment, segment_id
):
    """On main an empty sample answered ``estimated_percentage: 0.0``."""
    empty = _preview(admin_client, segment_id)
    assert empty == {
        "estimated_percentage": None,
        "sample_size": 0,
        "matched": 0,
        "source": "exposure_metadata",
        "window_days": 30,
        "sampled_since": None,
        "ended_by": "window",
        "users_without_context": 0,
        "reason": "no_exposures_with_context",
    }

    prefix = _prefix()
    _assign(admin_client, experiment, f"{prefix}-none-a")
    _assign(admin_client, experiment, f"{prefix}-none-b", {})
    response = admin_client.post(
        TRACK,
        json={"event_type": "exposure", "user_id": f"{prefix}-null"},
    )
    assert response.status_code == 200, response.text

    body = _preview(admin_client, segment_id)
    times = _stored_times(db_session, prefix)
    assert len(times) == 3
    assert body == {
        **empty,
        "users_without_context": 3,
        "sampled_since": times[-1],
    }


# ---------------------------------------------------------------------------
# Rows 17 and 18: the preview evaluates as membership does
# ---------------------------------------------------------------------------


def test_preview_matched_equals_membership_of_each_user(
    admin_client, experiment, segment_id
):
    prefix = _prefix()
    contexts = (
        [{"country": "US"}] * 12
        + [{"user": {"country": "US"}}] * 8
        + [{"country": "DE"}] * 20
    )
    for i, context in enumerate(contexts):
        _assign(admin_client, experiment, f"{prefix}-{i:02d}", context)

    members = 0
    for context in contexts:
        response = admin_client.post(
            f"{COLLECTION}/{segment_id}/evaluate", json={"user_context": context}
        )
        assert response.status_code == 200, response.text
        members += response.json()["is_member"]

    body = _preview(admin_client, segment_id)
    assert members == 20
    assert (body["matched"], body["sample_size"]) == (members, 40)


def test_unevaluable_and_missing_attributes_count_and_do_not_match(
    admin_client, experiment, segment_id
):
    prefix = _prefix()
    contexts = (
        [{"email": "a@x.test"}] * 10 + [{"email": 123}] * 10 + [{"country": "US"}] * 10
    )
    for i, context in enumerate(contexts):
        _assign(admin_client, experiment, f"{prefix}-{i:02d}", context)

    rules = _rules(_cond("email", "regex", r"@x\.test$"))
    body = _preview(admin_client, segment_id, rules=rules, sample_size=100)
    assert (body["matched"], body["sample_size"]) == (10, 30)


# ---------------------------------------------------------------------------
# Row 20: the floor of 30 users
# ---------------------------------------------------------------------------


def test_fewer_than_30_users_give_no_percentage(admin_client, experiment, segment_id):
    prefix = _prefix()
    for i in range(29):
        _assign(admin_client, experiment, f"{prefix}-{i:02d}", {"country": "US"})

    body = _preview(admin_client, segment_id)
    assert (body["estimated_percentage"], body["reason"]) == (None, "too_few_users")
    assert (body["matched"], body["sample_size"]) == (29, 29)

    _assign(admin_client, experiment, f"{prefix}-29", {"country": "DE"})
    body = _preview(admin_client, segment_id)
    assert (body["estimated_percentage"], body["reason"]) == (round(2900 / 30, 2), None)
    assert (body["matched"], body["sample_size"]) == (29, 30)


# ---------------------------------------------------------------------------
# Row 22: a statement timeout answers 503, and only that
# ---------------------------------------------------------------------------


def _slow_statement(since, until, cap):
    return sa.text(
        "SELECT 'u' AS user_id, NULL::jsonb AS event_metadata, "
        "'2026-01-01T00:00:00+00:00' AS created_at FROM pg_sleep(1)"
    )


def _failing_statement(since, until, cap):
    return sa.text(
        "SELECT 'u' AS user_id, NULL::jsonb AS event_metadata, "
        "(1 / 0)::text AS created_at"
    )


def test_a_read_past_the_statement_timeout_answers_503(
    admin_client, no_exposures, segment_id, monkeypatch
):
    monkeypatch.setattr(audience_service, "PREVIEW_STATEMENT_TIMEOUT_MS", 50)
    monkeypatch.setattr(audience_service, "recent_exposures_statement", _slow_statement)
    response = admin_client.post(
        f"{COLLECTION}/{segment_id}/preview", json={"name": "Preview", "rules": US}
    )
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": PREVIEW_TIMED_OUT}
    assert "window" not in response.text


def test_another_database_error_is_not_a_503(
    admin_client, no_exposures, segment_id, monkeypatch
):
    monkeypatch.setattr(
        audience_service, "recent_exposures_statement", _failing_statement
    )
    with pytest.raises(sa.exc.DataError):
        admin_client.post(
            f"{COLLECTION}/{segment_id}/preview",
            json={"name": "Preview", "rules": US},
        )


# ---------------------------------------------------------------------------
# Row 26: sampled_since and ended_by say how far the read went
# ---------------------------------------------------------------------------


def test_sampled_since_is_the_oldest_row_read_and_ended_by_says_why(
    admin_client, db_session, no_exposures, segment_id
):
    prefix = _prefix()
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    # Three users with 20 exposures each, interleaved, newest last.
    _batch(
        admin_client,
        [
            _exposure(
                f"{prefix}-{i % 3}", start + timedelta(seconds=i), {"country": "US"}
            )
            for i in range(60)
        ],
    )
    times = _stored_times(db_session, prefix)
    since = datetime.now(timezone.utc) - timedelta(days=30)

    # sample_size 10 reads at most 50 rows: the cap ends the read first.
    capped = _preview(admin_client, segment_id, sample_size=10)
    assert capped["ended_by"] == "cap"
    assert capped["sample_size"] == 3
    assert capped["sampled_since"] == times[49]
    assert datetime.fromisoformat(capped["sampled_since"]) > since

    # sample_size 100 reads at most 500: every row in the window is read.
    window = _preview(admin_client, segment_id, sample_size=100)
    assert window["ended_by"] == "window"
    assert window["sampled_since"] == times[-1]

    # Ten newer users: sample_size 10 is reached before the cap.
    _batch(
        admin_client,
        [
            _exposure(
                f"{prefix}-x{i}",
                datetime.now(timezone.utc) - timedelta(seconds=10 - i),
                {"country": "US"},
            )
            for i in range(10)
        ],
    )
    target = _preview(admin_client, segment_id, sample_size=10)
    assert target["ended_by"] == "sample_size"
    assert target["sample_size"] == 10


# ---------------------------------------------------------------------------
# Row 27: rows dated in the future, and oversize rows, are not read
# ---------------------------------------------------------------------------


def test_future_dated_and_oversize_exposures_are_not_sampled(
    admin_client, no_exposures, segment_id, monkeypatch
):
    prefix = _prefix()
    now = datetime.now(timezone.utc)
    _batch(
        admin_client,
        [
            _exposure(
                f"{prefix}-{i:02d}", now - timedelta(minutes=30), {"country": "DE"}
            )
            for i in range(30)
        ],
    )
    planted = {
        f"{prefix}-future": (now + timedelta(days=1), {"country": "US"}),
        f"{prefix}-oversize": (
            now - timedelta(minutes=1),
            {"country": "US", "pad": "x" * 9000},
        ),
        f"{prefix}-skewed": (now + timedelta(seconds=60), {"country": "US"}),
    }
    for user_id, (at, metadata) in planted.items():
        response = admin_client.post(
            TRACK,
            json={
                "event_type": "exposure",
                "user_id": user_id,
                "timestamp": at.isoformat(),
                "metadata": metadata,
            },
        )
        assert response.status_code == 200, response.text

    reached: List[str] = []
    read = audience_service._read_recent_exposures

    def recording(*args, **kwargs):
        for row in read(*args, **kwargs):
            reached.append(row[0])
            yield row

    monkeypatch.setattr(audience_service, "_read_recent_exposures", recording)
    body = _preview(admin_client, segment_id)
    # The 30 users and the one dated inside the skew; neither planted row.
    assert (body["sample_size"], body["matched"]) == (31, 1)
    assert f"{prefix}-skewed" in reached
    assert f"{prefix}-future" not in reached
    assert f"{prefix}-oversize" not in reached
