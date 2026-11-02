"""
An event sent again with the same ``event_id`` is stored once (#1543).

``POST /api/v1/tracking/track``, ``/batch`` and ``/events`` accept an optional
``event_id``. The server stores the first event it receives for a given user,
experiment (as resolved at receipt), flag (likewise), event name and
``event_id``; every later one with the same five values is answered as a
success and writes nothing. An event without an ``event_id`` is stored every
time, as before.

The experiment is ACTIVE, ten users are assigned to each of its two arms, and
it has three metrics on the ``purchase`` event: a conversion (the primary), a
revenue and a count metric. Every ``/results`` read passes
``use_cache=false``.

The ids R1-R15 and U3 are SPEC #1543 v1.2's gates. Each test says which. R1 is
the regression: on the code before #1543 the control revenue mean is 4.0 and
the count mean 0.2, because the event sent again is stored again.

Nothing here is imported from the change itself at module level, so the file
also runs against the code before it (which is how R1's failure there is
shown).

Rows created here are deleted in teardown: the shared test database is not
truncated between tests.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.models.analysis_snapshot import AnalysisSnapshot
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import (
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

USERS = 10
CONVERSION, REVENUE, COUNT = "Buyers", "Order value", "Purchases"
#: The loggers a failed write logs to; U3 holds that a duplicate or a race
#: logs neither message.
SERVICE_LOGGER = "backend.app.services.event_service"
ROUTE_LOGGER = "backend.app.api.v1.endpoints.tracking"
FAILURE_MESSAGES = ("Error tracking event", "Tracking batch item failed")


@pytest.fixture
def experiment(db_session, make_experiment):
    """An ACTIVE A/B experiment with ``USERS`` users assigned to each arm."""
    suffix = uuid.uuid4().hex[:8]
    exp = make_experiment(
        name=f"Duplicate events {suffix}",
        key=f"dup-events-{suffix}",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        start_date=datetime.now(timezone.utc) - timedelta(days=3),
    )
    control = Variant(
        experiment_id=exp.id, name="control", is_control=True, traffic_allocation=50
    )
    treatment = Variant(
        experiment_id=exp.id, name="treatment", is_control=False, traffic_allocation=50
    )
    db_session.add_all(
        [
            control,
            treatment,
            Metric(
                experiment_id=exp.id,
                name=CONVERSION,
                event_name="purchase",
                metric_type=MetricType.CONVERSION,
                is_primary=True,
            ),
            Metric(
                experiment_id=exp.id,
                name=REVENUE,
                event_name="purchase",
                metric_type=MetricType.REVENUE,
            ),
            Metric(
                experiment_id=exp.id,
                name=COUNT,
                event_name="purchase",
                metric_type=MetricType.COUNT,
            ),
        ]
    )
    db_session.commit()
    db_session.add_all(
        Assignment(
            experiment_id=exp.id, variant_id=variant.id, user_id=_user(exp, arm, i)
        )
        for arm, variant in (("control", control), ("treatment", treatment))
        for i in range(USERS)
    )
    db_session.commit()
    db_session.refresh(exp)
    yield exp
    db_session.rollback()
    prefix = f"dup-{exp.key}-%"
    db_session.query(Event).filter(Event.user_id.like(prefix)).delete(
        synchronize_session=False
    )
    for model in (AnalysisSnapshot, Event, Assignment):
        db_session.query(model).filter(model.experiment_id == exp.id).delete(
            synchronize_session=False
        )
    db_session.commit()
    _delete_keys(db_session, prefix)


def _delete_keys(db_session, prefix: str) -> None:
    """Remove this test's key rows; a database without the table has none."""
    with db_session.get_bind().begin() as conn:
        conn.execute(text("SET search_path TO test_experimentation"))
        if conn.execute(text("SELECT to_regclass('event_dedup_keys')")).scalar():
            conn.execute(
                text("DELETE FROM event_dedup_keys WHERE user_id LIKE :p"),
                {"p": prefix},
            )


def _user(exp, arm: str, i: int) -> str:
    return f"dup-{exp.key}-{arm}-{i}"


def _other(exp, tag: str) -> str:
    """A user of this test with no assignment."""
    return f"dup-{exp.key}-x-{tag}"


def _variant_id(exp, arm: str) -> str:
    return str(next(v.id for v in exp.variants if v.name == arm))


def _event_id() -> str:
    return f"evt-{uuid.uuid4()}"


def _purchase(exp, user_id: str, event_id: Any, **extra) -> Dict[str, Any]:
    """A ``/track`` or ``/batch`` body: a purchase of 20.0 for ``exp``."""
    body: Dict[str, Any] = {
        "event_type": "purchase",
        "user_id": user_id,
        "experiment_key": exp.key,
        "value": 20.0,
        **extra,
    }
    if event_id is not ...:
        body["event_id"] = event_id
    return body


def _track(client, body: Dict[str, Any]) -> Dict[str, Any]:
    response = client.post("/api/v1/tracking/track", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _batch(client, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    response = client.post("/api/v1/tracking/batch", json={"events": items})
    assert response.status_code == 200, response.text
    return response.json()


def _results(client, exp) -> dict:
    response = client.get(f"/api/v1/results/{exp.id}", params={"use_cache": "false"})
    assert response.status_code == 200, response.text
    return response.json()


def _arms(results: dict) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """``{metric name: {variant name: variant result}}``."""
    return {
        metric["metric_name"]: {v["variant_name"]: v for v in metric["variants"]}
        for metric in results["metrics"]
    }


def _rows(db_session, user_id: str) -> List[Event]:
    """Every stored event of ``user_id``, in any experiment or none."""
    db_session.expire_all()
    return db_session.query(Event).filter(Event.user_id == user_id).all()


def _keys(db_session, user_id: str) -> List[str]:
    """The ``dedup_key`` of every key row of ``user_id``, committed only."""
    with db_session.get_bind().connect() as conn:
        conn.execute(text("SET search_path TO test_experimentation"))
        return [
            row[0]
            for row in conn.execute(
                text("SELECT dedup_key FROM event_dedup_keys WHERE user_id = :u"),
                {"u": user_id},
            )
        ]


@pytest.fixture
def failure_logs(caplog):
    """The records of both loggers at every level, for U3."""
    caplog.set_level(logging.DEBUG, logger=SERVICE_LOGGER)
    caplog.set_level(logging.DEBUG, logger=ROUTE_LOGGER)

    def failures() -> List[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if any(m in record.getMessage() for m in FAILURE_MESSAGES)
        ]

    return failures


# ---------------------------------------------------------------------------
# R1: the regression
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_r1_a_purchase_sent_again_with_its_event_id_is_stored_and_counted_once(
    admin_client, db_session, experiment
):
    """R1. The same purchase sent twice through ``/track`` with one
    ``event_id``: the control revenue mean is 2.0 (20 over ten users) and the
    count mean 0.1; one converting user; one event row and one key row; the
    second answer is 200 with ``duplicate: true`` and the first answer's id."""
    exp = experiment
    buyer = _user(exp, "control", 0)
    body = _purchase(exp, buyer, _event_id())

    first = _track(admin_client, body)
    second = _track(admin_client, body)

    arms = _arms(_results(admin_client, exp))
    assert (arms[REVENUE]["control"]["mean"], arms[COUNT]["control"]["mean"]) == (
        2.0,
        0.1,
    )
    assert arms[CONVERSION]["control"]["conversions"] == 1
    assert len(_rows(db_session, buyer)) == 1
    assert (first["duplicate"], second["duplicate"]) == (False, True)
    assert second["id"] == first["id"]
    assert first["event_id"] == second["event_id"] == body["event_id"]
    assert len(_keys(db_session, buyer)) == 1


# ---------------------------------------------------------------------------
# R2, R3: /batch
# ---------------------------------------------------------------------------


def test_r2_the_same_item_twice_in_one_batch_is_stored_once(
    admin_client, db_session, experiment, failure_logs
):
    """R2 (and U3). The later item counts as a success and a duplicate; items
    commit one at a time, so the earlier one is already stored."""
    exp = experiment
    buyer = _user(exp, "control", 1)
    item = _purchase(exp, buyer, _event_id())

    answer = _batch(admin_client, [item, item])

    assert (answer["success_count"], answer["duplicate_count"]) == (2, 1)
    assert answer["failure_count"] == 0
    assert answer["errors"] is None
    assert len(_rows(db_session, buyer)) == 1
    assert failure_logs() == []


def test_r3_a_full_batch_sent_twice_is_stored_once(
    admin_client, db_session, experiment, failure_logs
):
    """R3 (and U3). 100 distinct events, then the same request again: the
    second answer has 100 successes, all duplicates, and 100 rows remain."""
    exp = experiment
    buyer = _user(exp, "treatment", 1)
    items = [_purchase(exp, buyer, f"r3-{i}-{uuid.uuid4()}") for i in range(100)]

    first = _batch(admin_client, items)
    second = _batch(admin_client, items)

    assert len(_rows(db_session, buyer)) == 100
    assert (first["success_count"], first["duplicate_count"]) == (100, 0)
    assert (second["success_count"], second["duplicate_count"]) == (100, 100)
    assert second["failure_count"] == 0
    assert failure_logs() == []


# ---------------------------------------------------------------------------
# R4: one key for all three routes
# ---------------------------------------------------------------------------


def test_r4_one_event_through_track_batch_and_events_is_one_row(
    admin_client, db_session, experiment
):
    """R4. One logical event sent through ``/track``, then ``/batch``, then
    ``/events`` (which names the experiment by id and the event by name): one
    row, one key, and the key is ``dedup_key`` of the experiment, no flag, the
    event name and the ``event_id``."""
    from backend.app.services.event_dedup import dedup_key

    exp = experiment
    buyer = _user(exp, "control", 2)
    event_id = _event_id()

    tracked = _track(admin_client, _purchase(exp, buyer, event_id))
    batched = _batch(admin_client, [_purchase(exp, buyer, event_id)])
    response = admin_client.post(
        "/api/v1/tracking/events",
        json={
            "event_name": "purchase",
            "user_id": buyer,
            "experiment_id": str(exp.id),
            "value": 20.0,
            "event_id": event_id,
        },
    )
    assert response.status_code == 200, response.text
    by_ids = response.json()

    assert len(_rows(db_session, buyer)) == 1
    assert tracked["duplicate"] is False
    assert (batched["success_count"], batched["duplicate_count"]) == (1, 1)
    assert by_ids["duplicate"] is True
    assert by_ids["id"] == tracked["id"]
    assert _keys(db_session, buyer) == [dedup_key(exp.id, None, "purchase", event_id)]


# ---------------------------------------------------------------------------
# R5, R6, R14: what is a different event
# ---------------------------------------------------------------------------


def test_r5_the_same_event_id_from_two_users_is_two_events(
    admin_client, db_session, experiment
):
    """R5."""
    exp = experiment
    event_id = _event_id()
    users = (_user(exp, "control", 3), _user(exp, "treatment", 3))

    answers = [_track(admin_client, _purchase(exp, u, event_id)) for u in users]

    assert [len(_rows(db_session, u)) for u in users] == [1, 1]
    assert [a["duplicate"] for a in answers] == [False, False]


def test_r6_one_event_id_fanned_out_to_three_experiments_and_a_flag(
    admin_client, db_session, experiment, make_experiment, make_feature_flag
):
    """R6. An SDK sends one body per target, each with the event's id: four
    rows, one in each experiment and one for the flag. The same batch sent
    again stores nothing."""
    exp = experiment
    others = [
        make_experiment(
            name=f"Fan-out {i} {exp.key}",
            key=f"{exp.key}-fan-{i}",
            status=ExperimentStatus.ACTIVE,
            experiment_type=ExperimentType.A_B,
        )
        for i in range(2)
    ]
    flag = make_feature_flag()
    buyer = _user(exp, "control", 4)
    event_id = _event_id()
    items = [_purchase(e, buyer, event_id) for e in (exp, *others)] + [
        {
            "event_type": "purchase",
            "user_id": buyer,
            "feature_flag_key": flag.key,
            "value": 20.0,
            "event_id": event_id,
        }
    ]
    try:
        first = _batch(admin_client, items)
        second = _batch(admin_client, items)

        rows = _rows(db_session, buyer)
        assert len(rows) == 4
        per_target = sorted(str(r.experiment_id or r.feature_flag_id) for r in rows)
        assert per_target == sorted(str(t.id) for t in (exp, *others, flag))
        assert (first["success_count"], first["duplicate_count"]) == (4, 0)
        assert (second["success_count"], second["duplicate_count"]) == (4, 4)
        assert len(_keys(db_session, buyer)) == 4
    finally:
        db_session.rollback()
        db_session.query(Event).filter(
            Event.experiment_id.in_([o.id for o in others])
            | (Event.feature_flag_id == flag.id)
        ).delete(synchronize_session=False)
        db_session.commit()


def test_r14_two_event_names_with_one_event_id_are_two_events(
    admin_client, db_session, experiment
):
    """R14. ``checkout_completed`` and ``purchase`` keyed by one order id."""
    exp = experiment
    buyer = _user(exp, "treatment", 4)
    order_id = f"order-{uuid.uuid4()}"

    answers = [
        _track(admin_client, _purchase(exp, buyer, order_id, event_type=name))
        for name in ("checkout_completed", "purchase")
    ]

    assert sorted(r.event_name for r in _rows(db_session, buyer)) == [
        "checkout_completed",
        "purchase",
    ]
    assert [a["duplicate"] for a in answers] == [False, False]


# ---------------------------------------------------------------------------
# R7, R8, R10: what a duplicate does and does not do
# ---------------------------------------------------------------------------


def test_r7_the_first_write_wins_and_the_answer_echoes_this_request(
    admin_client, db_session, experiment
):
    """R7 (C10). The same five key values with another value, metadata and
    timestamp: the stored total stays 20.0, the answer is ``duplicate: true``
    with the stored event's id, and every other field is this request's or
    this request's time, field by field."""
    exp = experiment
    buyer = _user(exp, "control", 5)
    event_id = _event_id()
    first = _track(
        admin_client,
        _purchase(
            exp,
            buyer,
            event_id,
            metadata={"try": 1},
            timestamp="2026-10-01T10:00:00+00:00",
        ),
    )
    before = datetime.now(timezone.utc).replace(tzinfo=None)

    second = _track(
        admin_client,
        _purchase(
            exp,
            buyer,
            event_id,
            value=40.0,
            metadata={"try": 2},
            timestamp="2026-10-02T11:30:00+00:00",
        ),
    )

    [row] = _rows(db_session, buyer)
    assert sum(r.value for r in _rows(db_session, buyer)) == 20.0
    assert _arms(_results(admin_client, exp))[REVENUE]["control"]["mean"] == 2.0
    assert second["duplicate"] is True
    assert second["id"] == first["id"] == str(row.id)
    assert second["event_id"] == event_id
    assert second["value"] == 40.0
    assert second["properties"] == {"try": 2}
    assert second["variant_id"] == _variant_id(exp, "control")
    assert first["timestamp"] == first["created_at"] == "2026-10-01T10:00:00Z"
    assert second["timestamp"] == second["created_at"] == "2026-10-02T11:30:00Z"
    updated = datetime.fromisoformat(second["updated_at"])
    if updated.tzinfo is not None:
        updated = updated.astimezone(timezone.utc).replace(tzinfo=None)
    assert updated >= before > row.updated_at
    assert (second["event_name"], second["user_id"]) == ("purchase", buyer)
    assert second["experiment_id"] == str(exp.id)


def test_r8_without_an_event_id_two_identical_purchases_are_both_stored(
    admin_client, db_session, experiment
):
    """R8: nothing compares content."""
    exp = experiment
    buyer = _user(exp, "control", 6)
    body = _purchase(exp, buyer, ..., metadata={"order_id": "order-1001"})

    answers = [_track(admin_client, body) for _ in range(2)]

    assert sum(r.value for r in _rows(db_session, buyer)) == 40.0
    assert answers[0]["id"] != answers[1]["id"]
    assert [a["duplicate"] for a in answers] == [False, False]
    assert [a["event_id"] for a in answers] == [None, None]
    assert _keys(db_session, buyer) == []


@pytest.mark.regression
def test_m4_rows_stored_without_a_key_are_read_as_before(
    admin_client, db_session, experiment
):
    """M4. Two copies of one purchase stored as the previous release stored
    every event -- through ``EventService.track_event``, with no key row --
    are both read: nothing removes an existing duplicate, and no reader
    requires a key."""
    from backend.app.services.event_service import EventService

    exp = experiment
    buyer = _user(exp, "control", 0)
    copy = {
        "user_id": buyer,
        "event_type": "purchase",
        "event_name": "purchase",
        "experiment_id": str(exp.id),
        "variant_id": _variant_id(exp, "control"),
        "value": 20.0,
    }
    service = EventService(db_session)
    for _ in range(2):
        service.track_event(copy)

    arms = _arms(_results(admin_client, exp))
    assert (arms[REVENUE]["control"]["mean"], arms[COUNT]["control"]["mean"]) == (
        4.0,
        0.2,
    )
    assert len(_rows(db_session, buyer)) == 2
    assert _keys(db_session, buyer) == []


def test_r10_a_duplicate_leaves_the_stored_row_and_cuped_history_alone(
    admin_client, db_session, experiment
):
    """R10. History sent with an ``event_id`` before the user is assigned,
    then sent again after: the stored row keeps its value, metadata and
    received-at time, and CUPED still reads it as the user's history."""
    from backend.app.services.event_matching import covariate_user_ids

    exp = experiment
    user = _other(exp, "history")
    event_id = _event_id()
    history = {
        "event_type": "purchase",
        "user_id": user,
        "value": 5.0,
        "metadata": {"seen": "before"},
        "timestamp": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "event_id": event_id,
    }
    _track(admin_client, history)
    [stored] = _rows(db_session, user)
    kept = (stored.value, stored.event_metadata, stored.updated_at, stored.created_at)
    assigned_at = {user: datetime.now(timezone.utc).replace(tzinfo=None)}
    assert covariate_user_ids(db_session, assigned_at, "purchase", 14) == ({user}, 0)

    again = _track(
        admin_client, {**history, "value": 99.0, "metadata": {"seen": "after"}}
    )

    [row] = _rows(db_session, user)
    assert again["duplicate"] is True
    assert (row.value, row.event_metadata, row.updated_at, row.created_at) == kept
    assert covariate_user_ids(db_session, assigned_at, "purchase", 14) == ({user}, 0)


def test_a_key_outlives_its_event_and_is_remembered_while_it_is_kept(
    admin_client, db_session, experiment
):
    """The retention sentences (EM-GT-7): deleting an event by hand leaves its
    key, which holds the user's id; the same event sent again is still a
    duplicate and stores nothing. Once the key is deleted too, it is stored."""
    exp = experiment
    buyer = _user(exp, "treatment", 7)
    body = _purchase(exp, buyer, _event_id())
    first = _track(admin_client, body)
    db_session.query(Event).filter(Event.user_id == buyer).delete(
        synchronize_session=False
    )
    db_session.commit()

    kept = _keys(db_session, buyer)
    again = _track(admin_client, body)

    assert len(kept) == 1
    assert (again["duplicate"], again["id"]) == (True, first["id"])
    assert _rows(db_session, buyer) == []

    _delete_keys(db_session, buyer)
    stored = _track(admin_client, body)
    assert stored["duplicate"] is False
    assert [str(r.id) for r in _rows(db_session, buyer)] == [stored["id"]]


# ---------------------------------------------------------------------------
# R9, R13, R15: keys with no target, keys across a status change, the bound
# ---------------------------------------------------------------------------


def test_r9_a_flag_only_event_and_an_untagged_event_are_each_stored_once(
    admin_client, db_session, experiment, make_feature_flag
):
    """R9. The key expresses an empty experiment, and an empty flag."""
    exp = experiment
    flag = make_feature_flag()
    flag_user, history_user = _other(exp, "flag-only"), _other(exp, "untagged")
    flag_only = {
        "event_type": "purchase",
        "user_id": flag_user,
        "feature_flag_key": flag.key,
        "event_id": _event_id(),
    }
    untagged = {
        "event_type": "purchase",
        "user_id": history_user,
        "event_id": _event_id(),
    }
    try:
        answers = [_track(admin_client, body) for body in (flag_only, flag_only)]
        answers += [_track(admin_client, body) for body in (untagged, untagged)]

        assert [len(_rows(db_session, u)) for u in (flag_user, history_user)] == [1, 1]
        assert [a["duplicate"] for a in answers] == [False, True, False, True]
        [untagged_row] = _rows(db_session, history_user)
        assert (untagged_row.experiment_id, untagged_row.feature_flag_id) == (
            None,
            None,
        )
    finally:
        db_session.rollback()
        db_session.query(Event).filter(Event.feature_flag_id == flag.id).delete(
            synchronize_session=False
        )
        db_session.commit()


def test_r13_an_event_first_stored_while_paused_stays_untagged_when_sent_again(
    admin_client, db_session, experiment
):
    """R13. Stored with no variant while the experiment is paused; sent again
    with the same ``event_id`` after it is started again, it is a duplicate:
    the key holds neither the status nor the variant, and the row stays
    untagged. The answer's variant is the one resolved now."""
    exp = experiment
    buyer = _user(exp, "treatment", 6)
    body = _purchase(exp, buyer, _event_id())
    assert admin_client.post(f"/api/v1/experiments/{exp.id}/pause").status_code == 200
    first = _track(admin_client, body)
    assert first["variant_id"] is None
    started = admin_client.post(f"/api/v1/experiments/{exp.id}/start")
    assert started.status_code == 200, started.text

    again = _track(admin_client, body)

    rows = _rows(db_session, buyer)
    assert len(rows) == 1
    assert rows[0].variant_id is None
    assert again["duplicate"] is True
    assert again["id"] == first["id"]
    assert again["variant_id"] == _variant_id(exp, "treatment")
    assert _arms(_results(admin_client, exp))[REVENUE]["treatment"]["mean"] == 0.0


def test_r15_the_longest_user_id_and_event_id_in_four_byte_characters(
    admin_client, db_session, experiment, make_feature_flag
):
    """R15. The widest key row the routes accept: 255 four-byte characters in
    the user id (1,020 bytes of the primary key) and in the event id, a
    100-character four-byte event name, and both an experiment and a flag.
    The fixed-length key keeps the row within PostgreSQL's index row limit,
    and inserts."""
    exp = experiment
    flag = make_feature_flag()
    user = f"dup-{exp.key}-x-" + "\U0001f600" * (255 - len(f"dup-{exp.key}-x-"))
    assert len(user) == 255
    body = _purchase(
        exp,
        user,
        "\U0001f600" * 255,
        event_type="\U0001f600" * 100,
        feature_flag_key=flag.key,
    )

    answers = [_track(admin_client, body) for _ in range(2)]

    assert [a["duplicate"] for a in answers] == [False, True]
    assert len(_rows(db_session, user)) == 1
    assert len(_keys(db_session, user)) == 1


# ---------------------------------------------------------------------------
# R12: the field's contract
# ---------------------------------------------------------------------------


def _status_and_errors(response) -> tuple:
    """The status, and each validation error's (type, loc, msg); any other
    answer's ``detail`` (or the whole body) as it is, so a 200 or a 500 shows
    as itself in the comparison rather than failing to parse."""
    body = response.json()
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, list):
        return response.status_code, [(e["type"], e["loc"], e["msg"]) for e in detail]
    return response.status_code, detail


@pytest.mark.parametrize(
    "event_id, error",
    [
        (123, ("string_type", "Input should be a valid string")),
        (
            "x" * 256,
            ("string_too_long", "String should have at most 255 characters"),
        ),
        (
            "order\x00-1",
            (
                "value_error",
                "Value error, event_id must not contain NUL or unpaired "
                "surrogate characters",
            ),
        ),
    ],
    ids=["number", "256-characters", "nul"],
)
def test_r12_an_invalid_event_id_is_refused_and_a_batch_with_one_stores_nothing(
    admin_client, db_session, experiment, event_id, error
):
    """R12 (C9). 422 with the exact error on each route; in ``/batch`` one
    invalid item refuses the whole request and nothing is stored."""
    exp = experiment
    buyer = _user(exp, "control", 7)
    track = admin_client.post(
        "/api/v1/tracking/track", json=_purchase(exp, buyer, event_id)
    )
    batch = admin_client.post(
        "/api/v1/tracking/batch",
        json={
            "events": [
                _purchase(exp, buyer, _event_id()),
                _purchase(exp, buyer, event_id),
            ]
        },
    )
    by_ids = admin_client.post(
        "/api/v1/tracking/events",
        json={
            "event_name": "purchase",
            "user_id": buyer,
            "experiment_id": str(exp.id),
            "event_id": event_id,
        },
    )

    found = {
        name: _status_and_errors(response)
        for name, response in (("track", track), ("batch", batch), ("events", by_ids))
    }
    kind, message = error
    assert found == {
        "track": (422, [(kind, ["body", "event_id"], message)]),
        "batch": (422, [(kind, ["body", "events", 1, "event_id"], message)]),
        "events": (422, [(kind, ["body", "event_id"], message)]),
    }
    assert _rows(db_session, buyer) == []


@pytest.mark.parametrize("event_id", ["", None], ids=["empty", "null"])
def test_r12_an_empty_or_null_event_id_means_none(
    admin_client, db_session, experiment, event_id
):
    """R12 (C9). ``""`` and ``null`` store every time, like no ``event_id``."""
    exp = experiment
    buyer = _user(exp, "control", 8)
    body = _purchase(exp, buyer, event_id)

    answers = [_track(admin_client, body) for _ in range(2)]
    batch = _batch(admin_client, [body, body])

    assert len(_rows(db_session, buyer)) == 4
    assert [(a["duplicate"], a["event_id"]) for a in answers] == [(False, None)] * 2
    assert (batch["success_count"], batch["duplicate_count"]) == (2, 0)
    assert _keys(db_session, buyer) == []


# ---------------------------------------------------------------------------
# R11: the race
# ---------------------------------------------------------------------------


def _wait_for_a_lock_wait(engine, deadline: float = 10.0) -> bool:
    """True once some backend waits on a lock while claiming a key."""
    end = time.monotonic() + deadline
    with engine.connect() as conn:
        while time.monotonic() < end:
            waiting = conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' "
                    "AND query ILIKE '%event_dedup_keys%'"
                )
            ).scalar()
            if waiting:
                return True
            time.sleep(0.05)
    return False


@pytest.mark.parametrize("holder", ["commits", "rolls back"])
def test_r11_two_connections_claiming_one_key_at_once(
    db_session, experiment, failure_logs, holder
):
    """R11 (and U3). The first claim's transaction stays open until the second
    is seen waiting on the key. When the first commits, the second is a
    duplicate naming the first's event; when it rolls back, the second
    stores. Neither raises, and nothing is logged as a failure."""
    from backend.app.schemas.tracking import EventCreate
    from backend.app.services.event_service import EventService

    exp = experiment
    engine = db_session.get_bind()
    make_session = sessionmaker(bind=engine, expire_on_commit=False)
    buyer = _user(exp, "control", 9)
    event_id = _event_id()
    data = EventCreate(
        event_name="purchase",
        event_type="purchase",
        user_id=buyer,
        experiment_id=str(exp.id),
        value=20.0,
        event_id=event_id,
    )
    first_session, second_session = make_session(), make_session()
    let_go = threading.Event()
    results: Dict[str, Any] = {}

    real_commit = first_session.commit

    def held_commit() -> None:
        let_go.wait(15)
        if holder == "commits":
            real_commit()
        else:
            raise RuntimeError("the first request fails after claiming")

    first_session.commit = held_commit

    def run(name: str, session) -> None:
        try:
            results[name] = EventService(session).track_event_once(
                data, event_id, allow_untagged=False
            )
        except Exception as exc:  # recorded, asserted below
            results[name] = exc
        finally:
            session.close()

    first = threading.Thread(target=run, args=("first", first_session))
    first.start()
    # The first holds its claim, uncommitted, inside held_commit.
    deadline = time.monotonic() + 10
    while not _keys_claimed_uncommitted(engine) and time.monotonic() < deadline:
        time.sleep(0.05)
    second = threading.Thread(target=run, args=("second", second_session))
    second.start()
    try:
        assert _wait_for_a_lock_wait(engine), "the second claim never waited"
    finally:
        let_go.set()
        first.join(20)
        second.join(20)

    rows = _rows(db_session, buyer)
    if holder == "commits":
        stored_id, created, event = results["first"]
        assert created is True and event is not None
        assert results["second"] == (stored_id, False, None)
        assert [r.id for r in rows] == [stored_id]
    else:
        assert isinstance(results["first"], RuntimeError)
        second_id, created, event = results["second"]
        assert created is True
        assert [r.id for r in rows] == [second_id]
    assert len(_keys(db_session, buyer)) == 1
    logged = failure_logs()
    expected = ["Error tracking event: RuntimeError"] if holder != "commits" else []
    assert logged == expected


def _keys_claimed_uncommitted(engine) -> bool:
    """True once a transaction holds an uncommitted insert into the key table."""
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_locks l "
                    "JOIN pg_class c ON c.oid = l.relation "
                    "WHERE c.relname = 'event_dedup_keys' "
                    "AND l.mode = 'RowExclusiveLock' AND l.granted"
                )
            ).scalar()
        )
