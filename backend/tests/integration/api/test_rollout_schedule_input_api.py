"""What a rollout schedule's create and update accept (#962, #1086, #1529, #1534).

* A ``PUT`` whose end date is not after the start answered 500 when one date
  was stored (the database's ``check_rollout_schedule_dates`` refused it) and
  is now 422 with create's message; a pair in the body is refused by the
  request model, the same 422 (#962).
* ``min_stage_duration`` is a whole number of hours from 0 to 8760, or null, on
  create and update: a negative value was stored and 2**31 or more answered 500
  (#962, #1086). A value stored before the bound still reads back.
* ``stages`` on ``PUT /{schedule_id}`` is refused whatever its value: it
  answered 200, applied the rest of the body and ignored the stages (#1529).
* A create mixing a date with a zone and one without answered 500 (a
  ``TypeError`` comparing them); an unzoned date is now taken as UTC (#1534).
* Any other constraint the database refuses keeps today's 500 and log line.

The order of the answers is 401, the request model's 422, 403/404, the rule
409s of #1460, and last the database's date refusal. Every request is a real
one (a local JWT; ``deps.get_db`` is the only override). The request models'
own properties are pinned by
``backend/tests/unit/schemas/test_rollout_schedule_input.py``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.rollout_schedule import RolloutSchedule
from backend.app.models.user import User, UserRole
from backend.app.schemas.rollout_schedule import (
    MAX_MIN_STAGE_DURATION_HOURS,
    STAGES_ON_UPDATE_REFUSED,
)
from backend.tests.conftest import DEFAULT_TEST_DB_URL
from backend.tests.integration.api.rollout_rules import (
    SCHEDULES,
    SCHEMA,
    client,
    create,
    developer,
    fresh,
    local_auth,
    new_flag,
    ok,
    rollout_invariants,
    send,
    session_factory,
    stage,
    stage_rows,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_db,
    pytest.mark.usefixtures("local_auth", "rollout_invariants"),
]

#: The one item of create's 422 for an end that is not after the start.
DATE_REFUSAL = {
    "type": "value_error",
    "loc": ["body"],
    "msg": "Value error, End date must be after start date",
}

START = "2027-01-08T00:00:00Z"
START_UTC = datetime(2027, 1, 8, tzinfo=timezone.utc)
END = "2027-02-08T00:00:00Z"

ROUTES_LOGGER = "backend.app.api.v1.endpoints.rollout_schedules"
#: Today's 500 bodies, byte for byte (captured on main at ac8c0159).
UPDATE_FAILED = '{"detail":"An error occurred while updating the rollout schedule"}'
CREATE_FAILED = '{"detail":"An error occurred while creating the rollout schedule"}'


def _items(response, status: int = 422) -> list:
    """The 422's items, each cut to ``type``, ``loc`` and ``msg``."""
    assert response.status_code == status, response.text
    return [
        {key: item[key] for key in ("type", "loc", "msg")}
        for item in response.json()["detail"]
    ]


def _row(fresh, schedule_id) -> dict:
    """The stored schedule's name, dates and min_stage_duration."""
    return fresh(
        lambda session: dict(
            session.execute(
                text(
                    "SELECT name, start_date, end_date, min_stage_duration, "
                    "max_percentage FROM rollout_schedules WHERE id = :id"
                ),
                {"id": str(schedule_id)},
            )
            .mappings()
            .one()
        )
    )


def _schedules_of(fresh, flag_id) -> int:
    return fresh(
        lambda session: session.execute(
            text("SELECT count(*) FROM rollout_schedules WHERE feature_flag_id = :id"),
            {"id": str(flag_id)},
        ).scalar()
    )


def _put(client, user, schedule_id, body):
    return send(client, "PUT", f"{SCHEDULES}/{schedule_id}", user, body)


def _post(client, user, flag_id, **extra):
    body = {
        "name": f"input {uuid.uuid4().hex[:8]}",
        "feature_flag_id": str(flag_id),
        "stages": [stage(1, 10), stage(2, 50)],
    }
    body.update(extra)
    return send(client, "POST", f"{SCHEDULES}/", user, body)


@pytest.fixture
def make_user(fresh):
    def make(role: UserRole) -> User:
        suffix = uuid.uuid4().hex[:8]
        user = User(
            username=f"input_{role.value}_{suffix}",
            email=f"input_{role.value}_{suffix}@input.test",
            full_name=f"Schedule input {role.value}",
            hashed_password="unused: this user signs in by token only",
            is_active=True,
            is_superuser=False,
            role=role,
        )

        def work(session):
            session.add(user)
            return user

        return fresh(work)

    return make


@pytest.fixture
def dated(client, developer, new_flag) -> dict:
    """A draft schedule stored with START and no end date."""
    return create(
        client, developer, new_flag(), [stage(1, 10), stage(2, 50)], start_date=START
    )


@pytest.fixture
def routes_log(monkeypatch):
    """The ERROR records of the routes' logger, captured on the logger itself."""
    import backend.app.api.v1.endpoints.rollout_schedules as routes

    logger = logging.getLogger(ROUTES_LOGGER)
    monkeypatch.setattr(routes, "logger", logger)
    records: list = []

    class _Records(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Records(level=logging.ERROR)
    disabled = logger.disabled
    logger.disabled = False
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.disabled = disabled


# ---------------------------------------------------------------------------
# Dates (#962)
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_put_end_before_stored_start_is_422(client, developer, dated, fresh):
    # Before: 500 {"detail":"An error occurred while updating the rollout
    # schedule"}, from the database's CheckViolation.
    before = _row(fresh, dated["id"])
    response = _put(
        client,
        developer,
        dated["id"],
        {"name": "renamed by the refused put", "end_date": "2027-01-01T00:00:00Z"},
    )
    assert (response.status_code, response.json()) == (
        422,
        {"detail": [DATE_REFUSAL]},
    ), response.text
    assert _row(fresh, dated["id"]) == before


@pytest.mark.regression
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"end_date": "2027-01-07T00:00:00Z"}, id="end-z"),
        pytest.param({"end_date": "2027-01-07T00:00:00"}, id="end-naive"),
        pytest.param({"end_date": "2027-01-07T15:00:00-08:00"}, id="end-offset"),
        # 2027-01-08T00:00:00Z exactly: equal is refused, as the constraint's > is.
        pytest.param({"end_date": "2027-01-07T16:00:00-08:00"}, id="end-equal"),
        pytest.param(
            {"start_date": "2027-03-01T00:00:00Z", "end_date": "2027-02-01T00:00:00Z"},
            id="inverted-pair",
        ),
    ],
)
def test_a_put_whose_end_is_not_after_the_start_is_422(
    client, developer, dated, fresh, body
):
    # Before: 500 for each, the inverted pair included: the update model had
    # no date check, so the database's constraint refused every one.
    before = _row(fresh, dated["id"])
    response = _put(client, developer, dated["id"], {"name": "not applied", **body})
    assert _items(response) == [DATE_REFUSAL]
    assert _row(fresh, dated["id"]) == before


@pytest.mark.regression
def test_a_put_start_after_the_stored_end_is_422(client, developer, new_flag, fresh):
    created = create(
        client, developer, new_flag(), [stage(1, 10)], start_date=START, end_date=END
    )
    before = _row(fresh, created["id"])
    response = _put(
        client, developer, created["id"], {"start_date": "2027-03-01T00:00:00"}
    )
    assert (response.status_code, response.json()) == (
        422,
        {"detail": [DATE_REFUSAL]},
    ), response.text
    assert _row(fresh, created["id"]) == before


@pytest.mark.parametrize(
    "end", ["2027-01-09T00:00:00Z", "2027-01-09T00:00:00", "2027-01-08T00:00:01-00:00"]
)
def test_a_put_end_after_the_stored_start_is_applied(
    client, developer, dated, fresh, end
):
    ok(_put(client, developer, dated["id"], {"end_date": end}))
    stored = _row(fresh, dated["id"])["end_date"]
    assert stored > START_UTC


# ---------------------------------------------------------------------------
# min_stage_duration (#962, #1086)
# ---------------------------------------------------------------------------

OUT_OF_RANGE = [
    pytest.param(-1, "greater_than_equal", id="-1"),
    pytest.param(-5, "greater_than_equal", id="-5"),
    pytest.param(MAX_MIN_STAGE_DURATION_HOURS + 1, "less_than_equal", id="8761"),
    pytest.param(2**31 - 1, "less_than_equal", id="2^31-1"),
    pytest.param(2**31, "less_than_equal", id="2^31"),
]


def _bound_refusal(kind: str) -> dict:
    return {
        "type": kind,
        "loc": ["body", "min_stage_duration"],
        "msg": (
            "Input should be greater than or equal to 0"
            if kind == "greater_than_equal"
            else "Input should be less than or equal to 8760"
        ),
    }


@pytest.mark.regression
@pytest.mark.parametrize("value, kind", OUT_OF_RANGE)
def test_create_min_stage_duration_out_of_range_is_422(
    client, developer, new_flag, fresh, value, kind
):
    # Before: -1 and -5 were stored (201); 2**31 answered 500
    # (NumericValueOutOfRange).
    flag_id = new_flag()
    response = _post(client, developer, flag_id, min_stage_duration=value)
    assert _items(response) == [_bound_refusal(kind)]
    assert _schedules_of(fresh, flag_id) == 0


@pytest.mark.regression
def test_put_negative_min_stage_duration_is_422(client, developer, dated, fresh):
    # Before: 200, and -5 was stored.
    before = _row(fresh, dated["id"])
    response = _put(client, developer, dated["id"], {"min_stage_duration": -5})
    assert _items(response) == [_bound_refusal("greater_than_equal")]
    assert _row(fresh, dated["id"]) == before


@pytest.mark.regression
@pytest.mark.parametrize("value, kind", OUT_OF_RANGE)
def test_put_min_stage_duration_out_of_range_is_422(
    client, developer, dated, fresh, value, kind
):
    before = _row(fresh, dated["id"])
    response = _put(client, developer, dated["id"], {"min_stage_duration": value})
    assert _items(response) == [_bound_refusal(kind)]
    assert _row(fresh, dated["id"]) == before


@pytest.mark.parametrize("value", [0, 1, 24, 48, MAX_MIN_STAGE_DURATION_HOURS, None])
def test_min_stage_duration_in_range_is_accepted_on_create_and_put(
    client, developer, new_flag, fresh, value
):
    created = ok(_post(client, developer, new_flag(), min_stage_duration=value), 201)
    assert created["min_stage_duration"] == value
    assert _row(fresh, created["id"])["min_stage_duration"] == value
    updated = ok(_put(client, developer, created["id"], {"min_stage_duration": value}))
    assert updated["min_stage_duration"] == value


def test_put_null_still_clears_min_stage_duration(client, developer, new_flag, fresh):
    created = ok(_post(client, developer, new_flag(), min_stage_duration=24), 201)
    ok(_put(client, developer, created["id"], {"min_stage_duration": None}))
    assert _row(fresh, created["id"])["min_stage_duration"] is None


@pytest.mark.parametrize("stored", [-5, 100000])
def test_a_stored_min_stage_duration_outside_the_range_still_reads_back(
    client, developer, new_flag, fresh, stored
):
    # Gate 1e: the bound is on the request models only. On the response's
    # base model it made GET one and GET list answer 500 for such a row.
    flag_id = new_flag()
    created = create(client, developer, flag_id, [stage(1, 10)])
    fresh(
        lambda session: session.execute(
            text("UPDATE rollout_schedules SET min_stage_duration = :v WHERE id = :id"),
            {"v": stored, "id": created["id"]},
        )
    )
    one = ok(send(client, "GET", f"{SCHEDULES}/{created['id']}", developer))
    assert one["min_stage_duration"] == stored
    listed = ok(
        send(client, "GET", f"{SCHEDULES}/?feature_flag_id={flag_id}", developer)
    )
    assert [item["min_stage_duration"] for item in listed["items"]] == [stored]
    # Sending it back is refused until it is corrected.
    response = _put(client, developer, created["id"], {"min_stage_duration": stored})
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# stages on PUT (#1529)
# ---------------------------------------------------------------------------

STAGES_REFUSAL = {
    "type": "value_error",
    "loc": ["body", "stages"],
    "msg": STAGES_ON_UPDATE_REFUSED,
}


@pytest.mark.regression
def test_put_nonempty_stages_is_422_and_changes_nothing(
    client, developer, new_flag, fresh
):
    # Before: 200; the schedule was renamed "renamed-by-put" and the stages
    # were not applied.
    created = create(client, developer, new_flag(), [stage(1, 10), stage(2, 50)])
    before = (_row(fresh, created["id"]), stage_rows(fresh, created["id"]))
    response = _put(
        client,
        developer,
        created["id"],
        {
            "name": "renamed-by-put",
            "stages": [{"name": "replaced", "stage_order": 1, "target_percentage": 20}],
        },
    )
    assert (response.status_code, response.json()) == (
        422,
        {"detail": [STAGES_REFUSAL]},
    ), response.text
    assert (_row(fresh, created["id"]), stage_rows(fresh, created["id"])) == before


@pytest.mark.regression
@pytest.mark.parametrize(
    "stages",
    [
        pytest.param(None, id="null"),
        pytest.param([], id="empty"),
        pytest.param([{"name": "x"}], id="non-empty"),
    ],
)
def test_put_with_stages_is_422_whatever_its_value(
    client, developer, new_flag, fresh, stages
):
    # Deliberately wider than #1529's "non-empty": a client could read [] or
    # null as "remove every stage". Before: 200 for all three.
    created = create(client, developer, new_flag(), [stage(1, 10)])
    before = _row(fresh, created["id"])
    response = _put(
        client, developer, created["id"], {"name": "not applied", "stages": stages}
    )
    assert _items(response) == [STAGES_REFUSAL]
    assert _row(fresh, created["id"]) == before


def test_put_without_stages_is_applied(client, developer, new_flag, fresh):
    created = create(client, developer, new_flag(), [stage(1, 10)])
    ok(_put(client, developer, created["id"], {"name": "renamed"}))
    assert _row(fresh, created["id"])["name"] == "renamed"


def test_put_an_item_that_fails_its_own_validation_answers_that_first(
    client, developer, new_flag
):
    created = create(client, developer, new_flag(), [stage(1, 10)])
    response = _put(client, developer, created["id"], {"stages": [{"stage_order": -1}]})
    assert [item["loc"] for item in _items(response)] == [
        ["body", "stages", 0, "stage_order"]
    ]


# ---------------------------------------------------------------------------
# Mixed zones (#1534)
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "start, end, status",
    [
        pytest.param(
            "2027-01-08T00:00:00Z", "2027-01-09T00:00:00", 201, id="aware-naive"
        ),
        pytest.param(
            "2027-01-08T00:00:00", "2027-01-09T00:00:00Z", 201, id="naive-aware"
        ),
        pytest.param(
            "2027-01-09T00:00:00Z",
            "2027-01-08T00:00:00",
            422,
            id="aware-naive-inverted",
        ),
        pytest.param(
            "2027-01-09T00:00:00",
            "2027-01-08T00:00:00Z",
            422,
            id="naive-aware-inverted",
        ),
    ],
)
def test_post_mixed_zone_dates_never_500(
    client, developer, new_flag, fresh, start, end, status
):
    # Before: 500 for all four (a TypeError comparing an aware and a naive
    # date in the request model), and nothing was stored.
    flag_id = new_flag()
    response = _post(client, developer, flag_id, start_date=start, end_date=end)
    if status == 422:
        assert _items(response) == [DATE_REFUSAL]
        assert _schedules_of(fresh, flag_id) == 0
        return
    created = ok(response, 201)
    stored = _row(fresh, created["id"])
    assert (stored["start_date"], stored["end_date"]) == (
        datetime(2027, 1, 8, tzinfo=timezone.utc),
        datetime(2027, 1, 9, tzinfo=timezone.utc),
    )


@pytest.fixture
def la_client(client, test_db):
    """``client`` whose request sessions are in America/Los_Angeles."""
    engine = create_engine(DEFAULT_TEST_DB_URL, poolclass=NullPool)

    @event.listens_for(engine, "connect")
    def _los_angeles(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("SET TIME ZONE 'America/Los_Angeles'")
        cursor.execute(f"SET search_path TO {SCHEMA}")
        cursor.close()
        dbapi_conn.commit()

    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    probe = factory()
    try:
        zone = probe.execute(text("SHOW TIME ZONE")).scalar()
    finally:
        probe.close()
    assert zone == "America/Los_Angeles"
    try:
        yield client
    finally:
        engine.dispose()


def test_an_unzoned_schedule_date_is_stored_as_utc_whatever_the_session_zone(
    la_client, developer, new_flag, fresh
):
    # The route's session is in Los Angeles; without the UTC rule an unzoned
    # date would be stored as Los Angeles time, eight hours later.
    created = ok(
        _post(
            la_client,
            developer,
            new_flag(),
            start_date="2027-01-08T00:00:00",
            end_date="2027-01-09T00:00:00Z",
        ),
        201,
    )
    stored = _row(fresh, created["id"])
    assert (stored["start_date"], stored["end_date"]) == (
        datetime(2027, 1, 8, tzinfo=timezone.utc),
        datetime(2027, 1, 9, tzinfo=timezone.utc),
    )
    ok(_put(la_client, developer, created["id"], {"end_date": "2027-01-10T00:00:00"}))
    assert _row(fresh, created["id"])["end_date"] == datetime(
        2027, 1, 10, tzinfo=timezone.utc
    )


# ---------------------------------------------------------------------------
# Other constraints keep today's answer (gate 1k)
# ---------------------------------------------------------------------------


@pytest.fixture
def refuse_max_percentage():
    """Make the database refuse the next write of the given schedule ids on
    ``check_max_percentage`` (a real CheckViolation from psycopg2)."""
    ids: set = set()

    def force(mapper, connection, target):
        if target.id in ids or target.name in ids:
            target.max_percentage = 150

    event.listen(RolloutSchedule, "before_update", force)
    event.listen(RolloutSchedule, "before_insert", force)
    try:
        yield ids
    finally:
        event.remove(RolloutSchedule, "before_update", force)
        event.remove(RolloutSchedule, "before_insert", force)


@pytest.mark.parametrize("violation", ["not-null", "check_max_percentage"])
def test_another_constraint_on_update_keeps_todays_500_and_log_line(
    client, developer, new_flag, fresh, routes_log, refuse_max_percentage, violation
):
    created = create(client, developer, new_flag(), [stage(1, 10)], start_date=START)
    before = _row(fresh, created["id"])
    if violation == "not-null":
        body = {"name": None}
        cause = "NotNullViolation"
    else:
        refuse_max_percentage.add(uuid.UUID(created["id"]))
        body = {"name": "renamed"}
        cause = "check_max_percentage"
    response = _put(client, developer, created["id"], body)
    assert (response.status_code, response.text) == (500, UPDATE_FAILED)
    messages = [record.getMessage() for record in routes_log]
    assert len(messages) == 1, messages
    assert messages[0].startswith("Error updating rollout schedule: "), messages
    assert cause in messages[0], messages
    assert _row(fresh, created["id"]) == before


def test_another_constraint_on_create_keeps_todays_500_and_log_line(
    client, developer, new_flag, fresh, routes_log, refuse_max_percentage
):
    flag_id = new_flag()
    name = f"refused {uuid.uuid4().hex[:8]}"
    refuse_max_percentage.add(name)
    response = _post(client, developer, flag_id, name=name)
    assert (response.status_code, response.text) == (500, CREATE_FAILED)
    messages = [record.getMessage() for record in routes_log]
    assert len(messages) == 1, messages
    assert messages[0].startswith("Error creating rollout schedule: "), messages
    assert "check_max_percentage" in messages[0], messages
    assert _schedules_of(fresh, flag_id) == 0


def test_the_date_constraint_on_create_answers_the_date_422(
    client, developer, new_flag, fresh, routes_log
):
    # The request model refuses every pair a create can send, so the route's
    # mapping is reached here by moving the stored end before the start.
    flag_id = new_flag()
    name = f"moved {uuid.uuid4().hex[:8]}"

    def move_end(mapper, connection, target):
        if target.name == name:
            target.end_date = datetime(2026, 1, 1, tzinfo=timezone.utc)

    event.listen(RolloutSchedule, "before_insert", move_end)
    try:
        response = _post(client, developer, flag_id, name=name, start_date=START)
    finally:
        event.remove(RolloutSchedule, "before_insert", move_end)
    assert (response.status_code, response.json()) == (422, {"detail": [DATE_REFUSAL]})
    assert _schedules_of(fresh, flag_id) == 0
    assert routes_log == []


# ---------------------------------------------------------------------------
# The order of the answers (gates 1h, 1o)
# ---------------------------------------------------------------------------


def test_an_unauthenticated_invalid_body_answers_401(client, dated):
    response = client.put(
        f"{SCHEDULES}/{dated['id']}", json={"min_stage_duration": -5, "stages": []}
    )
    assert response.status_code == 401, response.text


@pytest.mark.parametrize("role", [UserRole.ANALYST, UserRole.VIEWER])
def test_a_forbidden_caller_gets_403_before_the_date_check(
    client, make_user, dated, fresh, role
):
    # The body passes the request model; its one date is on the wrong side of
    # the stored start, which only the database can judge, after the route's
    # permission check.
    before = _row(fresh, dated["id"])
    response = _put(
        client, make_user(role), dated["id"], {"end_date": "2027-01-01T00:00:00Z"}
    )
    assert response.status_code == 403, response.text
    assert response.json() == {
        "detail": "Not enough permissions to change this feature flag's rollout"
    }
    assert _row(fresh, dated["id"]) == before


@pytest.mark.parametrize("role", [UserRole.ANALYST, UserRole.VIEWER])
def test_a_forbidden_caller_with_an_invalid_body_gets_the_schema_422(
    client, make_user, dated, role
):
    response = _put(client, make_user(role), dated["id"], {"min_stage_duration": -5})
    assert _items(response) == [_bound_refusal("greater_than_equal")]


def test_an_invalid_body_for_a_missing_schedule_is_the_schema_422(client, developer):
    response = _put(client, developer, uuid.uuid4(), {"stages": []})
    assert _items(response) == [STAGES_REFUSAL]


def test_a_rule_conflict_answers_409_before_the_date_refusal(
    client, developer, dated, fresh
):
    # Lowering max_percentage below a stage's target is #1460's 409; the
    # end before the stored start is the database's refusal, which comes last.
    before = _row(fresh, dated["id"])
    response = _put(
        client,
        developer,
        dated["id"],
        {"max_percentage": 20, "end_date": "2027-01-01T00:00:00Z"},
    )
    assert response.status_code == 409, response.text
    assert response.json() == {
        "detail": "max_percentage cannot be below a stage's target: "
        "stage_order 2 targets 50%."
    }
    assert _row(fresh, dated["id"]) == before
