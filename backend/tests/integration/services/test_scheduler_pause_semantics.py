"""A paused experiment stays paused until it is started or a resume is scheduled (#436).

Before the fix the scheduler activated every PAUSED experiment whose
``start_date`` had passed -- which is every experiment that ever ran -- so a
pause lasted until the next tick, and the scheduler ticks once at startup. A
resume scheduled with ``PUT /schedule`` was stored by overwriting
``start_date``, and a later manual pause was undone by that same stale date.

Now a resume on a PAUSED experiment is its own column, ``resume_at``:

* P1  a paused experiment with a past ``start_date`` and no resume stays paused;
* P2  ``PUT /schedule`` on a PAUSED experiment records ``resume_at`` (in the
      response and on a later GET), leaves ``start_date`` alone, and the tick
      resumes it, clears ``resume_at`` and says "resumed automatically";
* P3  a resume in the future is not fired early;
* P4  a manual pause after a resume leaves nothing pending, whichever writer
      pauses it (the sequence pause, schedule, start, pause, tick);
* P5  every other status change clears a scheduled resume, through every
      writer that has a route, the two service-only methods, and an instance
      expired by a commit;
* P6  a due DRAFT still activates, with "started automatically";
* P7  ``PUT /schedule`` on a DRAFT experiment sets no ``resume_at``, and a
      field the request omits is left unchanged (#482);
* P8  one tick starts and completes only what is due: a DRAFT starting
      tomorrow stays a DRAFT, and an ACTIVE experiment ending tomorrow keeps
      running, beside a due start and a due end;

plus the refusals ``PUT /schedule`` on PAUSED makes from the values the
experiment would end up with (400, never a constraint error), what happens to
each field the request omits (on DRAFT too, #482), how ``time_zone`` reads a
date without an offset and refuses a name that is not an IANA zone (#483),
the 400 text for an ACTIVE experiment, and the
hazard the rollback note describes: without the listener, ``POST /start`` on a
paused experiment with a pending resume is a 500.

Every request is a real one: real ``User`` rows, a real local JWT from
``create_local_access_token``, and no dependency override except
``deps.get_db``. ``make_client_for_user`` is deliberately not used (#470).
The scheduler runs against the same database with its ``SessionLocal``
patched, as ``test_experiment_bayesian_config_api.py`` does. The database is
shared with other tests, so the tick's own counts are not asserted; which
experiments it activated is read from the notifications it sends.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text, update
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps

# The function the local login route signs its tokens with.
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.models import experiment as experiment_models
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.user import User, UserRole
from backend.app.schemas.experiment import TIME_ZONE_ERROR
from backend.app.services.experiment_service import ExperimentService
from backend.app.services.notification_service import NotificationService

pytestmark = [pytest.mark.integration, pytest.mark.regression]

BASE = "/api/v1/experiments"


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


def _factory(db_session, **kwargs):
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, **kwargs)

    def session():
        s = factory()
        s.execute(text("SET search_path TO test_experimentation"))
        return s

    return session


@pytest.fixture
def people(db_session):
    """A DEVELOPER (not a superuser) for the everyday routes, and a superuser
    for the status PUT, which only a superuser may make on a non-DRAFT."""
    suffix = uuid.uuid4().hex[:8]

    def make(name, role, is_superuser):
        user = User(
            username=f"pause436_{name}_{suffix}",
            email=f"pause436_{name}_{suffix}@pause.test",
            full_name="Pause Semantics User",
            hashed_password="unused: these users sign in by token only",
            is_active=True,
            is_superuser=is_superuser,
            role=role,
        )
        db_session.add(user)
        return user

    users = {
        "developer": make("developer", UserRole.DEVELOPER, False),
        "superuser": make("superuser", UserRole.ADMIN, True),
    }
    db_session.commit()
    return users


@pytest.fixture
def client(db_session):
    session = _factory(db_session, autocommit=False, autoflush=False)

    def override_get_db():
        s = session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


# --- helpers ------------------------------------------------------------------


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _now():
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _payload():
    return {
        "name": f"pause436 {uuid.uuid4().hex[:8]}",
        "description": "A paused experiment stays paused",
        "hypothesis": "A pause lasts until someone ends it",
        "experiment_type": "a_b",
        "variants": [
            {"name": "Control", "is_control": True, "traffic_allocation": 50},
            {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
        ],
        "metrics": [
            {
                "name": "Conversion Rate",
                "event_name": "purchase",
                "metric_type": "conversion",
                "is_primary": True,
            }
        ],
    }


def _ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def _create(client, people):
    return _ok(
        client.post(f"{BASE}/", json=_payload(), headers=_auth(people["developer"])),
        201,
    )["id"]


def _post(client, people, experiment_id, action, who="developer"):
    return client.post(f"{BASE}/{experiment_id}/{action}", headers=_auth(people[who]))


def _schedule(client, people, experiment_id, body):
    return client.put(
        f"{BASE}/{experiment_id}/schedule",
        json=body,
        headers=_auth(people["developer"]),
    )


def _set_status_in_db(db_session, experiment_id, status):
    """Write a status straight to the row. Since #542 no route sets a status
    except the lifecycle endpoints, so a state those cannot reach (a DRAFT
    paused before it started, as older data may hold) is set up here."""
    session = _factory(db_session)()
    try:
        row = session.get(Experiment, uuid.UUID(experiment_id))
        row.status = status
        session.commit()
    finally:
        session.close()


def _get(client, people, experiment_id):
    return _ok(
        client.get(f"{BASE}/{experiment_id}", headers=_auth(people["developer"]))
    )


def _row(db_session, experiment_id):
    session = _factory(db_session)()
    try:
        row = session.get(Experiment, uuid.UUID(experiment_id))
        return row.status, row.resume_at, row.start_date, row.end_date
    finally:
        session.close()


def _paused(client, people):
    """Created, started and paused through the API: its start_date is now in
    the past, which is exactly the row the old scheduler resumed."""
    experiment_id = _create(client, people)
    _ok(_post(client, people, experiment_id, "start"))
    assert _ok(_post(client, people, experiment_id, "pause"))["status"] == "paused"
    return experiment_id


def _paused_with_resume(client, people, delta):
    experiment_id = _paused(client, people)
    resume = _now() + delta
    _ok(_schedule(client, people, experiment_id, {"start_date": _iso(resume)}))
    return experiment_id


def _tick(db_session):
    """One scheduler tick against the test database. Returns the
    notifications it sent, as {experiment_id: message}."""
    from backend.app.core.scheduler import ExperimentScheduler

    sent = {}
    notifications = NotificationService()
    notifications._slack = MagicMock()
    notifications._email = MagicMock()

    def capture(event_):
        assert event_.event_type == "experiment_started"
        sent[event_.experiment_id] = event_.message
        return True

    notifications._send = capture

    scheduler = ExperimentScheduler()
    scheduler._notification_service = notifications
    with patch("backend.app.core.scheduler.SessionLocal", _factory(db_session)):
        asyncio.run(scheduler.process_scheduled_experiments())
    return sent


# --- P1: no resume, no activation ----------------------------------------------


def test_p1_paused_experiment_with_past_start_stays_paused(client, db_session, people):
    experiment_id = _paused(client, people)
    status, resume_at, start_date, _ = _row(db_session, experiment_id)
    assert status == ExperimentStatus.PAUSED
    assert resume_at is None
    assert start_date is not None and start_date.replace(tzinfo=timezone.utc) <= _now()

    sent = _tick(db_session)

    assert experiment_id not in sent
    assert _row(db_session, experiment_id)[0] == ExperimentStatus.PAUSED
    assert _get(client, people, experiment_id)["status"] == "paused"


# --- P2: a scheduled resume ------------------------------------------------------


def test_p2_scheduled_resume_is_recorded_and_fires(client, db_session, people):
    experiment_id = _paused(client, people)
    started = _get(client, people, experiment_id)["start_date"]
    resume = _now() - timedelta(minutes=1)

    body = _ok(_schedule(client, people, experiment_id, {"start_date": _iso(resume)}))
    assert body["status"] == "paused"
    assert _parse(body["resume_at"]) == resume
    assert body["start_date"] == started

    fetched = _get(client, people, experiment_id)
    assert _parse(fetched["resume_at"]) == resume
    assert fetched["start_date"] == started

    sent = _tick(db_session)

    assert sent[experiment_id] == (
        f"Experiment '{fetched['name']}' has been resumed automatically."
    )
    after = _get(client, people, experiment_id)
    assert after["status"] == "active"
    assert after["resume_at"] is None
    assert after["start_date"] == started


# --- P3: not early ---------------------------------------------------------------


def test_p3_future_resume_is_not_fired(client, db_session, people):
    experiment_id = _paused_with_resume(client, people, timedelta(hours=1))

    sent = _tick(db_session)

    assert experiment_id not in sent
    status, resume_at, _, _ = _row(db_session, experiment_id)
    assert status == ExperimentStatus.PAUSED
    assert resume_at is not None and resume_at > _now()


# --- P4: a later manual pause leaves nothing pending ------------------------------


def _pause_by_route(client, people, experiment_id, db_session):
    _ok(_post(client, people, experiment_id, "pause"))


def _pause_by_service(client, people, experiment_id, db_session):
    session = _factory(db_session)()
    try:
        row = session.get(Experiment, uuid.UUID(experiment_id))
        ExperimentService(session).pause_experiment(row)
    finally:
        session.close()


PAUSERS = {
    "post_pause": _pause_by_route,
    "service_pause_experiment": _pause_by_service,
}


@pytest.mark.parametrize("pauser", PAUSERS)
def test_p4_pause_after_a_resume_sticks(client, db_session, people, pauser):
    """Pause, schedule a resume, start, pause again: the second pause holds.
    The old code had moved start_date to the resume time, so the tick undid
    the second pause."""
    experiment_id = _paused_with_resume(client, people, -timedelta(minutes=1))
    _ok(_post(client, people, experiment_id, "start"))
    assert _row(db_session, experiment_id)[:2] == (ExperimentStatus.ACTIVE, None)

    PAUSERS[pauser](client, people, experiment_id, db_session)
    assert _row(db_session, experiment_id)[:2] == (ExperimentStatus.PAUSED, None)

    sent = _tick(db_session)

    assert experiment_id not in sent
    assert _row(db_session, experiment_id)[:2] == (ExperimentStatus.PAUSED, None)


# --- P5: every status change clears a scheduled resume ---------------------------


def _start(client, people, experiment_id, db_session):
    return _post(client, people, experiment_id, "start")


def _complete(client, people, experiment_id, db_session):
    return _post(client, people, experiment_id, "complete")


def _archive(client, people, experiment_id, db_session):
    return _post(client, people, experiment_id, "archive")


def _service_complete(client, people, experiment_id, db_session):
    session = _factory(db_session)()
    try:
        row = session.get(Experiment, uuid.UUID(experiment_id))
        ExperimentService(session).complete_experiment(row)
    finally:
        session.close()


#: writer -> (how it changes the status, the status afterwards)
CLEARERS = {
    "post_start": (_start, ExperimentStatus.ACTIVE),
    "post_complete": (_complete, ExperimentStatus.COMPLETED),
    "post_archive": (_archive, ExperimentStatus.ARCHIVED),
    "service_complete_experiment": (_service_complete, ExperimentStatus.COMPLETED),
}


@pytest.mark.parametrize("writer", CLEARERS)
def test_p5_status_change_clears_the_resume(client, db_session, people, writer):
    experiment_id = _paused_with_resume(client, people, timedelta(hours=1))
    assert _row(db_session, experiment_id)[1] is not None

    do, expected = CLEARERS[writer]
    response = do(client, people, experiment_id, db_session)
    if response is not None:
        body = _ok(response)
        assert body["status"] == expected.value
        assert body["resume_at"] is None

    assert _row(db_session, experiment_id)[:2] == (expected, None)


def test_p5_scheduler_resume_clears_the_resume(client, db_session, people):
    experiment_id = _paused_with_resume(client, people, -timedelta(minutes=1))

    assert experiment_id in _tick(db_session)

    assert _row(db_session, experiment_id)[:2] == (ExperimentStatus.ACTIVE, None)


def test_p5_expired_instance(client, db_session, people):
    """A status set on an instance a commit has expired: the old value is
    loaded (active_history), so re-setting PAUSED keeps the resume and a real
    change clears it."""
    experiment_id = _paused_with_resume(client, people, timedelta(hours=1))
    session = _factory(db_session, expire_on_commit=True)()
    try:
        row = session.get(Experiment, uuid.UUID(experiment_id))
        session.commit()  # expires row
        assert "status" not in row.__dict__
        row.status = ExperimentStatus.PAUSED
        session.commit()
        assert _row(db_session, experiment_id)[0] == ExperimentStatus.PAUSED
        assert _row(db_session, experiment_id)[1] is not None

        assert "status" not in row.__dict__
        row.status = ExperimentStatus.ACTIVE
        session.commit()
    finally:
        session.close()

    assert _row(db_session, experiment_id)[:2] == (ExperimentStatus.ACTIVE, None)


# --- the hazard the rollback note describes --------------------------------------


def test_without_the_listener_start_on_a_pending_resume_is_a_500(
    client, db_session, people, caplog
):
    """Rolling back only the code (keeping resume_at and its CHECK) leaves
    nothing to clear resume_at: POST /start then violates
    ck_experiments_resume_only_when_paused. Hence the rollback runs
    ``UPDATE experiments SET resume_at = NULL`` first.

    The constraint is named in the server log; the response carries only the
    fixed message."""
    experiment_id = _paused_with_resume(client, people, timedelta(hours=1))
    listener = experiment_models._clear_resume_on_status_change
    event.remove(Experiment.status, "set", listener)
    caplog.set_level(logging.ERROR)
    try:
        response = _post(client, people, experiment_id, "start")
    finally:
        event.listen(Experiment.status, "set", listener, active_history=True)

    assert response.status_code == 500, response.text
    assert response.json()["detail"].startswith("Could not start the experiment")
    assert "ck_experiments_resume_only_when_paused" not in response.text
    assert any(
        r.exc_info and "ck_experiments_resume_only_when_paused" in str(r.exc_info[1])
        for r in caplog.records
    ), "the constraint must be named in the server log"
    status, resume_at, _, _ = _row(db_session, experiment_id)
    assert status == ExperimentStatus.PAUSED
    assert resume_at is not None


# --- P6 / P7: DRAFT is unchanged --------------------------------------------------


def test_p6_due_draft_activates(client, db_session, people):
    experiment_id = _create(client, people)
    start = _now() - timedelta(minutes=1)
    body = _ok(
        _schedule(
            client,
            people,
            experiment_id,
            {"start_date": _iso(start), "end_date": _iso(start + timedelta(days=7))},
        )
    )
    assert body["resume_at"] is None

    sent = _tick(db_session)

    assert sent[experiment_id] == (
        f"Experiment '{body['name']}' has been started automatically."
    )
    after = _get(client, people, experiment_id)
    assert after["status"] == "active"
    assert after["resume_at"] is None
    assert _parse(after["start_date"]) == start


def test_p7_draft_schedule_writes_start_date_and_keeps_what_is_omitted(
    client, db_session, people
):
    """#482: on a DRAFT a field the request omits is left unchanged, as on
    PAUSED. Before the fix the omitted end_date was written as null."""
    experiment_id = _create(client, people)
    start = _now() + timedelta(days=1)
    end = start + timedelta(days=7)
    _ok(
        _schedule(
            client,
            people,
            experiment_id,
            {"start_date": _iso(start), "end_date": _iso(end)},
        )
    )

    later = start + timedelta(days=1)
    body = _ok(_schedule(client, people, experiment_id, {"start_date": _iso(later)}))

    assert body["status"] == "draft"
    assert body["resume_at"] is None
    assert _parse(body["start_date"]) == later
    assert _parse(body["end_date"]) == end  # omitted on DRAFT: kept (#482)
    status, resume_at, _, end_date = _row(db_session, experiment_id)
    assert (status, resume_at) == (ExperimentStatus.DRAFT, None)
    assert end_date.replace(tzinfo=timezone.utc) == end


# --- P8: one tick, what is due and what is not ----------------------------------------


def _set_dates_in_db(db_session, experiment_id, start_date, end_date):
    """Write a running experiment's dates straight to the row: no route sets
    them once it runs (``PUT /schedule`` answers 400 on ACTIVE), and an end
    already passed needs a start before it (``check_experiment_dates``)."""
    session = _factory(db_session)()
    try:
        row = session.get(Experiment, uuid.UUID(experiment_id))
        row.start_date = start_date
        row.end_date = end_date
        session.commit()
    finally:
        session.close()


def _tick_announcing(db_session):
    """One scheduler tick, as ``_tick``. Returns what it announced as
    ``{"experiment_started": {id: message}, "experiment_ended": {id: message}}``."""
    from backend.app.core.scheduler import ExperimentScheduler

    sent = {"experiment_started": {}, "experiment_ended": {}}
    notifications = NotificationService()
    notifications._slack = MagicMock()
    notifications._email = MagicMock()

    def capture(event_):
        sent[event_.event_type][event_.experiment_id] = event_.message
        return True

    notifications._send = capture

    scheduler = ExperimentScheduler()
    scheduler._notification_service = notifications
    with patch("backend.app.core.scheduler.SessionLocal", _factory(db_session)):
        asyncio.run(scheduler.process_scheduled_experiments())
    return sent


def test_p8_one_tick_starts_and_ends_only_what_is_due(client, db_session, people):
    """In one tick: a DRAFT whose start has passed starts and one starting
    tomorrow stays a DRAFT; an ACTIVE experiment whose end has passed completes
    and one ending tomorrow keeps running. Neither of the two not yet due is
    announced or changed."""
    now = _now()
    due_start = now - timedelta(minutes=1)
    later_start = now + timedelta(days=1)
    due_draft = _create(client, people)
    _ok(
        _schedule(
            client,
            people,
            due_draft,
            {"start_date": _iso(due_start), "end_date": _iso(now + timedelta(days=7))},
        )
    )
    later_draft = _create(client, people)
    _ok(
        _schedule(
            client,
            people,
            later_draft,
            {
                "start_date": _iso(later_start),
                "end_date": _iso(later_start + timedelta(days=7)),
            },
        )
    )
    due_end = _create(client, people)
    later_end = _create(client, people)
    for experiment_id in (due_end, later_end):
        assert _ok(_post(client, people, experiment_id, "start"))["status"] == "active"
    started_days_ago = now - timedelta(days=2)
    later_end_date = now + timedelta(days=1)
    _set_dates_in_db(db_session, due_end, started_days_ago, now - timedelta(minutes=1))
    _set_dates_in_db(db_session, later_end, started_days_ago, later_end_date)

    sent = _tick_announcing(db_session)
    started, ended = sent["experiment_started"], sent["experiment_ended"]

    assert due_draft in started, started
    assert _row(db_session, due_draft)[0] == ExperimentStatus.ACTIVE
    assert later_draft not in started, started
    status, _, start_date, _ = _row(db_session, later_draft)
    assert status == ExperimentStatus.DRAFT
    assert start_date.replace(tzinfo=timezone.utc) == later_start

    assert due_end in ended, ended
    assert _row(db_session, due_end)[0] == ExperimentStatus.COMPLETED
    assert later_end not in ended, ended
    status, _, _, end_date = _row(db_session, later_end)
    assert status == ExperimentStatus.ACTIVE
    assert end_date.replace(tzinfo=timezone.utc) == later_end_date


# --- PUT /schedule on DRAFT: each field (#482) -------------------------------------


def _draft_with_schedule(client, people):
    experiment_id = _create(client, people)
    start = _now() + timedelta(days=1)
    end = start + timedelta(days=7)
    _ok(
        _schedule(
            client,
            people,
            experiment_id,
            {"start_date": _iso(start), "end_date": _iso(end)},
        )
    )
    return experiment_id, start, end


def test_draft_omitted_start_date_is_kept(client, db_session, people):
    experiment_id, start, _ = _draft_with_schedule(client, people)
    end = start + timedelta(days=3)

    body = _ok(_schedule(client, people, experiment_id, {"end_date": _iso(end)}))

    assert _parse(body["start_date"]) == start
    assert _parse(body["end_date"]) == end


def test_draft_time_zone_alone_changes_no_date(client, db_session, people):
    experiment_id, start, end = _draft_with_schedule(client, people)

    body = _ok(_schedule(client, people, experiment_id, {"time_zone": "Asia/Tokyo"}))

    assert _parse(body["start_date"]) == start
    assert _parse(body["end_date"]) == end


@pytest.mark.parametrize("field", ["start_date", "end_date"])
def test_draft_explicit_null_clears_only_that_field(client, db_session, people, field):
    experiment_id, start, end = _draft_with_schedule(client, people)
    other = "end_date" if field == "start_date" else "start_date"
    kept = end if other == "end_date" else start

    body = _ok(_schedule(client, people, experiment_id, {field: None}))

    assert body[field] is None
    assert _parse(body[other]) == kept


def test_draft_refusal_from_the_stored_start_date_writes_nothing(
    client, db_session, people
):
    """The request omits start_date; its end_date is less than an hour after
    the stored one. The refusal is checked before anything is written."""
    experiment_id, start, end = _draft_with_schedule(client, people)

    response = _schedule(
        client,
        people,
        experiment_id,
        {"end_date": _iso(start + timedelta(minutes=30))},
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Experiment must run for at least 1:00:00"
    _, _, start_date, end_date = _row(db_session, experiment_id)
    assert start_date.replace(tzinfo=timezone.utc) == start
    assert end_date.replace(tzinfo=timezone.utc) == end


def test_draft_end_before_the_stored_start_is_400(client, db_session, people):
    experiment_id, start, end = _draft_with_schedule(client, people)

    response = _schedule(
        client,
        people,
        experiment_id,
        {"end_date": _iso(start - timedelta(hours=2))},
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "End date must be after start date"
    assert _row(db_session, experiment_id)[3].replace(tzinfo=timezone.utc) == end


# --- PUT /schedule: time_zone (#483) ----------------------------------------------


def _wall_clock(days):
    """A whole-minute wall-clock time *days* ahead, with no offset."""
    return (_now() + timedelta(days=days)).replace(tzinfo=None, second=0, microsecond=0)


@pytest.mark.parametrize("zone", ["America/Los_Angeles", "Asia/Kolkata", "UTC"])
def test_draft_date_without_offset_is_read_in_the_time_zone(
    client, db_session, people, zone
):
    """Before the fix any zone but UTC was a 500 ('MetaData' object does not
    support item assignment), and a date without an offset was a 500 with
    any zone."""
    experiment_id = _create(client, people)
    start, end = _wall_clock(2), _wall_clock(9)

    body = _ok(
        _schedule(
            client,
            people,
            experiment_id,
            {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "time_zone": zone,
            },
        )
    )

    tz = ZoneInfo(zone)
    want_start = start.replace(tzinfo=tz).astimezone(timezone.utc)
    want_end = end.replace(tzinfo=tz).astimezone(timezone.utc)
    assert _parse(body["start_date"]) == want_start
    assert _parse(body["end_date"]) == want_end
    _, _, start_date, end_date = _row(db_session, experiment_id)
    assert start_date.replace(tzinfo=timezone.utc) == want_start
    assert end_date.replace(tzinfo=timezone.utc) == want_end


def test_date_with_an_offset_keeps_it_whatever_the_time_zone(
    client, db_session, people
):
    experiment_id = _create(client, people)
    start = _now() + timedelta(days=2)
    end = start + timedelta(days=7)

    body = _ok(
        _schedule(
            client,
            people,
            experiment_id,
            {
                "start_date": _iso(start),
                "end_date": _iso(end),
                "time_zone": "Europe/London",
            },
        )
    )

    assert _parse(body["start_date"]) == start
    assert _parse(body["end_date"]) == end


def test_paused_resume_without_offset_is_read_in_the_time_zone(
    client, db_session, people
):
    experiment_id = _paused(client, people)
    resume = _wall_clock(1)

    body = _ok(
        _schedule(
            client,
            people,
            experiment_id,
            {"start_date": resume.isoformat(), "time_zone": "America/Los_Angeles"},
        )
    )

    want = resume.replace(tzinfo=ZoneInfo("America/Los_Angeles")).astimezone(
        timezone.utc
    )
    assert _parse(body["resume_at"]) == want
    assert _row(db_session, experiment_id)[1] == want


def test_past_date_without_offset_is_still_refused(client, db_session, people):
    experiment_id = _create(client, people)

    response = _schedule(
        client,
        people,
        experiment_id,
        {"start_date": _wall_clock(-1).isoformat(), "time_zone": "Asia/Tokyo"},
    )

    assert response.status_code == 422, response.text
    assert "Start date must be in the future" in response.text


@pytest.mark.parametrize(
    "zone",
    ["Not/AZone", "+05:30", "America", "utc", "../zone", "", "Z" * 65],
)
@pytest.mark.parametrize("paused", [False, True])
def test_unknown_time_zone_is_422_naming_the_field(
    client, db_session, people, zone, paused
):
    """A name that is not an IANA zone answers 422 with a fixed message that
    names time_zone and does not repeat the value, and writes nothing.

    ``utc`` is refused although ``ZoneInfo("utc")`` loads on a case-insensitive
    file system: the check is membership, so it answers the same everywhere."""
    experiment_id = _paused(client, people) if paused else _create(client, people)
    before = _row(db_session, experiment_id)

    response = _schedule(
        client,
        people,
        experiment_id,
        {
            "start_date": _iso(_now() + timedelta(days=2)),
            "end_date": _iso(_now() + timedelta(days=9)),
            "time_zone": zone,
        },
    )

    assert response.status_code == 422, response.text
    errors = response.json()["detail"]
    assert [e["loc"] for e in errors] == [["body", "time_zone"]]
    if len(zone) <= 64:
        assert errors[0]["msg"] == f"Value error, {TIME_ZONE_ERROR}"
    if zone and zone not in TIME_ZONE_ERROR:  # "America" is in the example
        assert zone not in response.text
    assert _row(db_session, experiment_id) == before


# --- PUT /schedule on PAUSED: each field -----------------------------------------


def test_paused_start_date_null_cancels_the_resume(client, db_session, people):
    experiment_id = _paused_with_resume(client, people, timedelta(hours=1))
    before = _get(client, people, experiment_id)

    body = _ok(_schedule(client, people, experiment_id, {"start_date": None}))

    assert body["resume_at"] is None
    assert body["start_date"] == before["start_date"]
    assert body["end_date"] == before["end_date"]
    assert experiment_id not in _tick(db_session)


def test_paused_omitted_start_date_keeps_the_resume(client, db_session, people):
    experiment_id = _paused_with_resume(client, people, timedelta(hours=1))
    before = _get(client, people, experiment_id)
    end = _now() + timedelta(days=5)

    body = _ok(_schedule(client, people, experiment_id, {"end_date": _iso(end)}))

    assert body["resume_at"] == before["resume_at"]
    assert body["start_date"] == before["start_date"]
    assert _parse(body["end_date"]) == end


def test_paused_omitted_end_date_is_kept(client, db_session, people):
    experiment_id = _paused(client, people)
    end = _now() + timedelta(days=5)
    _ok(_schedule(client, people, experiment_id, {"end_date": _iso(end)}))

    resume = _now() + timedelta(hours=2)
    body = _ok(_schedule(client, people, experiment_id, {"start_date": _iso(resume)}))

    assert _parse(body["resume_at"]) == resume
    assert _parse(body["end_date"]) == end


def test_paused_end_date_null_clears_it(client, db_session, people):
    experiment_id = _paused(client, people)
    _ok(
        _schedule(
            client,
            people,
            experiment_id,
            {"end_date": _iso(_now() + timedelta(days=5))},
        )
    )

    body = _ok(_schedule(client, people, experiment_id, {"end_date": None}))

    assert body["end_date"] is None


# --- PUT /schedule on PAUSED: refusals from the effective values (400) -----------


def test_paused_resume_after_the_stored_end_date_is_400(client, db_session, people):
    """The request omits end_date; the stored one is sooner than resume + 1 h."""
    experiment_id = _paused(client, people)
    stored_end = _now() + timedelta(minutes=30)
    session = _factory(db_session)()
    try:
        session.execute(
            update(Experiment)
            .where(Experiment.id == uuid.UUID(experiment_id))
            .values(end_date=stored_end.replace(tzinfo=None))
        )
        session.commit()
    finally:
        session.close()

    response = _schedule(
        client,
        people,
        experiment_id,
        {"start_date": _iso(_now() + timedelta(minutes=10))},
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == (
        "End date must be at least 1:00:00 after the resume time"
    )
    assert _row(db_session, experiment_id)[1] is None


def test_paused_end_date_before_the_stored_start_date_is_400(
    client, db_session, people
):
    """A DRAFT scheduled for the future and then paused (older data; no route
    can do this since #542): an end_date
    alone that is earlier than its start_date would break check_experiment_dates."""
    experiment_id = _create(client, people)
    start = _now() + timedelta(days=2)
    _ok(
        _schedule(
            client,
            people,
            experiment_id,
            {"start_date": _iso(start), "end_date": _iso(start + timedelta(days=8))},
        )
    )
    _set_status_in_db(db_session, experiment_id, ExperimentStatus.PAUSED)

    response = _schedule(
        client,
        people,
        experiment_id,
        {"end_date": _iso(_now() + timedelta(days=1))},
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == (
        "End date must be after the experiment's start date"
    )
    _, _, _, end_date = _row(db_session, experiment_id)
    assert end_date.replace(tzinfo=timezone.utc) == start + timedelta(days=8)


def test_paused_valid_resume_and_end_date(client, db_session, people):
    experiment_id = _paused(client, people)
    before = _get(client, people, experiment_id)
    resume = _now() + timedelta(hours=1)
    end = resume + timedelta(days=3)

    body = _ok(
        _schedule(
            client,
            people,
            experiment_id,
            {"start_date": _iso(resume), "end_date": _iso(end)},
        )
    )

    assert _parse(body["resume_at"]) == resume
    assert _parse(body["end_date"]) == end
    assert body["start_date"] == before["start_date"]


def _full_row(db_session, experiment_id):
    session = _factory(db_session)()
    try:
        row = session.get(Experiment, uuid.UUID(experiment_id))
        return (
            row.name,
            row.description,
            row.start_date,
            row.end_date,
            row.updated_at,
        )
    finally:
        session.close()


def test_experiment_update_refuses_schedule_whatever_its_zone(
    client, db_session, people
):
    """``ScheduleConfig`` is also the declared ``schedule`` field of
    ``PUT /experiments/{id}``, which never applied it. Since #595 any
    ``schedule`` there is a 422 at ``["body", "schedule"]`` pointing at
    ``/schedule``, before its zone is looked at, so an unknown zone and a known
    one get the same refusal, the zone is not repeated and the row is not
    touched. (Before #595 an unknown zone was a 422 at
    ``["body", "schedule", "time_zone"]`` (#483) and a known one a 200.)"""
    experiment_id = _create(client, people)
    before = _full_row(db_session, experiment_id)

    for zone in ("Not/AZone", "UTC", "America/Los_Angeles"):
        response = client.put(
            f"{BASE}/{experiment_id}",
            json={"schedule": {"time_zone": zone}},
            headers=_auth(people["developer"]),
        )

        assert response.status_code == 422, (zone, response.text)
        locs = [e["loc"] for e in response.json()["detail"]]
        assert locs == [["body", "schedule"]], (zone, locs)
        assert zone not in response.text
        assert _full_row(db_session, experiment_id) == before


# --- the 400 for an ACTIVE experiment ----------------------------------------------


def test_schedule_on_active_names_the_status_by_value(client, db_session, people):
    experiment_id = _create(client, people)
    _ok(_post(client, people, experiment_id, "start"))

    response = _schedule(
        client,
        people,
        experiment_id,
        {"start_date": _iso(_now() + timedelta(days=1))},
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == (
        "Cannot schedule experiment with status active. "
        "Experiment must be in DRAFT or PAUSED status."
    )
