"""
Which stored events count in an experiment's results, through the tracking
routes and ``GET /api/v1/results/{id}`` on a real database.

``docs/guides/how-events-count.md`` states these rules, and its registry under
``docs/_claims/`` names each test here. Each test holds today's behaviour and
none sends an ``event_id``, so each stays true once an event can carry one
(#1543): an event sent again without one is stored again.

The experiment is ACTIVE, ten users are assigned to each of its two arms, and
it has three metrics on the ``purchase`` event: a conversion (the primary), a
revenue and a count metric. Every ``/results`` read passes
``use_cache=false``.

Rows created here are deleted in teardown: the shared test database is not
truncated between tests.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

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
#: The metric names, by type.
CONVERSION, REVENUE, COUNT = "Buyers", "Order value", "Purchases"
#: Keys whose value is the time of the request, not a result.
VOLATILE_KEYS = frozenset({"computed_at", "timestamp", "generated_at"})
#: A crawler's User-Agent, as Google documents it.
CRAWLER = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


@pytest.fixture
def experiment(db_session, make_experiment):
    """An ACTIVE A/B experiment that started at midnight UTC three days ago,
    with ``USERS`` users assigned to each arm and no event yet."""
    suffix = uuid.uuid4().hex[:8]
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=3)
    exp = make_experiment(
        name=f"How events count {suffix}",
        key=f"how-events-count-{suffix}",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        start_date=start,
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
    for model in (AnalysisSnapshot, Event, Assignment):
        db_session.query(model).filter(model.experiment_id == exp.id).delete(
            synchronize_session=False
        )
    db_session.commit()


def _user(exp, arm: str, i: int) -> str:
    return f"hec-{exp.key}-{arm}-{i}"


def _variant_id(exp, arm: str) -> str:
    return str(next(v.id for v in exp.variants if v.name == arm))


def _purchase(client, exp, user_id: str, *, value: float = 20.0, **extra) -> dict:
    """One ``purchase`` for ``exp`` through ``/track``; its answer."""
    response = client.post(
        "/api/v1/tracking/track",
        json={
            "event_type": "purchase",
            "user_id": user_id,
            "experiment_key": exp.key,
            "value": value,
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _results(client, exp, **params) -> dict:
    response = client.get(
        f"/api/v1/results/{exp.id}", params={"use_cache": "false", **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _arms(results: dict) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """``{metric name: {variant name: variant result}}``."""
    return {
        metric["metric_name"]: {v["variant_name"]: v for v in metric["variants"]}
        for metric in results["metrics"]
    }


def _rows(db_session, exp, user_id: str):
    db_session.expire_all()
    return (
        db_session.query(Event)
        .filter(Event.experiment_id == exp.id, Event.user_id == user_id)
        .all()
    )


def _canonical(value):
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        items = [_canonical(v) for v in value]
        if items and all(isinstance(i, dict) and "segment_value" in i for i in items):
            items.sort(key=lambda i: str(i["segment_value"]))
        return items
    return value


def test_an_event_sent_again_is_stored_and_counted_again(
    admin_client, db_session, experiment
):
    """The same purchase sent twice, through ``/track`` and through
    ``/batch``, carrying an order id in its metadata: two rows each, each
    answered as stored. The revenue and count metrics count both copies; the
    conversion metric counts each user once."""
    exp = experiment
    first, second = _user(exp, "control", 0), _user(exp, "control", 1)
    body = {"value": 20.0, "metadata": {"order_id": "order-1001"}}

    answers = [_purchase(admin_client, exp, first, **body) for _ in range(2)]
    assert answers[0]["id"] != answers[1]["id"]
    assert [a["variant_id"] for a in answers] == [_variant_id(exp, "control")] * 2

    item = {
        "event_type": "purchase",
        "user_id": second,
        "experiment_key": exp.key,
        **body,
    }
    for _ in range(2):
        response = admin_client.post("/api/v1/tracking/batch", json={"events": [item]})
        assert response.status_code == 200, response.text
        assert response.json() == {
            "success_count": 1,
            "failure_count": 0,
            "errors": None,
        }

    for user_id in (first, second):
        rows = _rows(db_session, exp, user_id)
        assert len(rows) == 2
        assert {r.event_metadata["order_id"] for r in rows} == {"order-1001"}

    arms = _arms(_results(admin_client, exp))
    # Two buyers of 2 x 20 among ten users: (40 + 40) / 10.
    assert arms[REVENUE]["control"]["mean"] == pytest.approx(8.0)
    # Four purchases among ten users.
    assert arms[COUNT]["control"]["mean"] == pytest.approx(0.4)
    # Two converting users, each counted once.
    assert arms[CONVERSION]["control"]["conversions"] == 2
    assert arms[CONVERSION]["control"]["mean"] == pytest.approx(0.2)


def test_an_event_received_before_the_assignment_counts_in_no_variant(
    admin_client, db_session, experiment
):
    """A purchase from a user not yet assigned is stored with the experiment
    and no variant. It counts in no variant once the user is assigned, and the
    summary's Total Events counts it."""
    exp = experiment
    early = f"hec-{exp.key}-early"
    before = _results(admin_client, exp)

    answer = _purchase(admin_client, exp, early, value=50.0)
    assert answer["experiment_id"] == str(exp.id)
    assert answer["variant_id"] is None
    after_event = _results(admin_client, exp)
    assert (
        after_event["summary"]["total_events"] == before["summary"]["total_events"] + 1
    )

    assigned = admin_client.post(
        "/api/v1/tracking/assign",
        json={"experiment_key": exp.key, "user_id": early},
    )
    assert assigned.status_code == 200, assigned.text
    arm = assigned.json()["variant_name"]

    [purchase] = [
        r for r in _rows(db_session, exp, early) if r.event_name == "purchase"
    ]
    assert purchase.variant_id is None
    arms = _arms(_results(admin_client, exp))
    assert arms[CONVERSION][arm]["sample_size"] == USERS + 1
    assert arms[CONVERSION][arm]["conversions"] == 0
    assert arms[REVENUE][arm]["mean"] == 0.0
    assert arms[COUNT][arm]["mean"] == 0.0

    # The same user's purchase, received after the assignment, counts.
    answer = _purchase(admin_client, exp, early, value=50.0)
    assert answer["variant_id"] == assigned.json()["variant_id"]
    arms = _arms(_results(admin_client, exp))
    assert arms[CONVERSION][arm]["conversions"] == 1
    assert arms[REVENUE][arm]["mean"] == pytest.approx(50.0 / (USERS + 1))


def test_an_event_received_while_paused_counts_in_no_variant_after_the_start(
    admin_client, db_session, experiment
):
    """A purchase received while the experiment is paused is stored with no
    variant and still counts in no variant after the experiment is started
    again."""
    exp = experiment
    buyer = _user(exp, "treatment", 2)
    response = admin_client.post(f"/api/v1/experiments/{exp.id}/pause")
    assert response.status_code == 200, response.text

    answer = _purchase(admin_client, exp, buyer)
    assert answer["experiment_id"] == str(exp.id)
    assert answer["variant_id"] is None

    response = admin_client.post(f"/api/v1/experiments/{exp.id}/start")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == ExperimentStatus.ACTIVE.value

    [row] = _rows(db_session, exp, buyer)
    assert row.variant_id is None
    arms = _arms(_results(admin_client, exp))
    assert arms[CONVERSION]["treatment"]["conversions"] == 0
    assert arms[REVENUE]["treatment"]["mean"] == 0.0
    assert arms[COUNT]["treatment"]["mean"] == 0.0


def test_the_variant_is_the_one_at_receipt_whatever_the_timestamp(
    admin_client, db_session, experiment
):
    """Purchases dated long before the experiment and far after it, sent by
    assigned users: each is stored with the timestamp it was sent, tagged with
    the user's variant, and counted."""
    exp = experiment
    sent = {
        _user(exp, "control", 3): "0001-01-01T00:00:00Z",
        _user(exp, "control", 4): "1999-12-31T23:59:59Z",
        _user(exp, "control", 5): "9999-12-31T23:59:59Z",
    }
    for user_id, timestamp in sent.items():
        answer = _purchase(admin_client, exp, user_id, timestamp=timestamp)
        assert answer["variant_id"] == _variant_id(exp, "control")
        [row] = _rows(db_session, exp, user_id)
        assert row.created_at == timestamp.replace("Z", "+00:00")
        assert str(row.variant_id) == _variant_id(exp, "control")

    arms = _arms(_results(admin_client, exp))
    assert arms[CONVERSION]["control"]["conversions"] == 3
    assert arms[REVENUE]["control"]["mean"] == pytest.approx(3 * 20.0 / USERS)


def test_an_event_without_a_timestamp_is_dated_when_it_is_received(
    admin_client, db_session, experiment
):
    exp = experiment
    user_id = _user(exp, "treatment", 3)
    before = datetime.now(timezone.utc)
    _purchase(admin_client, exp, user_id)
    after = datetime.now(timezone.utc)
    [row] = _rows(db_session, exp, user_id)
    assert before <= datetime.fromisoformat(row.created_at) <= after


def test_the_results_do_not_read_the_timestamp(admin_client, db_session, experiment):
    """``/results``, with a breakdown, is the same after every event of the
    experiment is moved to another date."""
    exp = experiment
    for arm, i, value, country in (
        ("control", 0, 20.0, "US"),
        ("control", 1, 35.0, "DE"),
        ("treatment", 0, 12.5, "US"),
        ("treatment", 0, 40.0, "US"),
        ("treatment", 1, 60.0, "FR"),
    ):
        _purchase(
            admin_client,
            exp,
            _user(exp, arm, i),
            value=value,
            metadata={"country": country},
        )
    before = _results(admin_client, exp, breakdown="country")
    assert before["metrics"][0]["variants"][0]["sample_size"] == USERS

    for moved_to in ("1970-01-01T00:00:00+00:00", "2999-06-30T12:00:00+00:00"):
        db_session.query(Event).filter(Event.experiment_id == exp.id).update(
            {"created_at": moved_to}, synchronize_session=False
        )
        db_session.commit()
        after = _results(admin_client, exp, breakdown="country")
        assert json.dumps(_canonical(after), sort_keys=True) == json.dumps(
            _canonical(before), sort_keys=True
        )


def test_the_daily_series_dates_a_first_conversion_by_the_utc_day_of_its_timestamp(
    admin_client, experiment
):
    """A purchase sent at 23:30 at UTC-5 on the experiment's second day is on
    its third day in UTC, and the daily series counts it there."""
    exp = experiment
    start = exp.start_date
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    assert (start.hour, start.minute) == (0, 0)
    local = (start + timedelta(days=1)).replace(tzinfo=None) + timedelta(
        hours=23, minutes=30
    )
    _purchase(
        admin_client,
        exp,
        _user(exp, "control", 6),
        timestamp=local.isoformat() + "-05:00",
    )
    [metric] = [m for m in exp.metric_definitions if m.name == CONVERSION]
    response = admin_client.get(
        f"/api/v1/results/{exp.id}/daily", params={"metric_id": str(metric.id)}
    )
    assert response.status_code == 200, response.text
    by_name = {s["variant_name"]: s for s in response.json()["series"]}
    per_day = {p["date"]: p["conversions"] for p in by_name["control"]["values"]}
    utc_day = (start + timedelta(days=2)).strftime("%Y-%m-%d")
    assert per_day[utc_day] == 1
    assert sum(per_day.values()) == 1


def test_events_by_ids_count_only_for_a_user_assigned_to_that_variant(
    admin_client, db_session, experiment
):
    """``/tracking/events`` stores the variant it is sent. A purchase tagged
    with the treatment from a control user, or from a user with no assignment,
    counts in no variant; one tagged with the user's own variant counts. The
    summary's Total Events counts all three."""
    exp = experiment
    control_id, treatment_id = (
        _variant_id(exp, "control"),
        _variant_id(exp, "treatment"),
    )
    before = _results(admin_client, exp)["summary"]["total_events"]
    sent = [
        (_user(exp, "control", 7), treatment_id),
        (f"hec-{exp.key}-unassigned", treatment_id),
        (_user(exp, "control", 8), control_id),
    ]
    for user_id, variant_id in sent:
        response = admin_client.post(
            "/api/v1/tracking/events",
            json={
                "event_name": "purchase",
                "user_id": user_id,
                "experiment_id": str(exp.id),
                "variant_id": variant_id,
                "value": 20.0,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["variant_id"] == variant_id

    results = _results(admin_client, exp)
    arms = _arms(results)
    assert arms[CONVERSION]["control"]["conversions"] == 1
    assert arms[CONVERSION]["treatment"]["conversions"] == 0
    assert arms[REVENUE]["control"]["mean"] == pytest.approx(20.0 / USERS)
    assert arms[REVENUE]["treatment"]["mean"] == 0.0
    assert results["summary"]["total_events"] == before + 3


def test_an_event_from_a_crawler_is_stored_and_counted(
    admin_client, db_session, experiment
):
    """Nothing reads the sender's User-Agent: a crawler's purchase is stored,
    tagged and counted like any other."""
    exp = experiment
    user_id = _user(exp, "treatment", 4)
    response = admin_client.post(
        "/api/v1/tracking/track",
        json={
            "event_type": "purchase",
            "user_id": user_id,
            "experiment_key": exp.key,
            "value": 20.0,
        },
        headers={"User-Agent": CRAWLER},
    )
    assert response.status_code == 200, response.text
    assert response.json()["variant_id"] == _variant_id(exp, "treatment")
    assert len(_rows(db_session, exp, user_id)) == 1
    arms = _arms(_results(admin_client, exp))
    assert arms[CONVERSION]["treatment"]["conversions"] == 1
    assert arms[COUNT]["treatment"]["mean"] == pytest.approx(1 / USERS)


def test_one_user_can_send_any_number_of_events(admin_client, db_session, experiment):
    """Three full batches from one user: all 300 are stored and counted."""
    exp = experiment
    user_id = _user(exp, "treatment", 5)
    item = {
        "event_type": "purchase",
        "user_id": user_id,
        "experiment_key": exp.key,
        "value": 1.0,
    }
    for _ in range(3):
        response = admin_client.post(
            "/api/v1/tracking/batch", json={"events": [item] * 100}
        )
        assert response.status_code == 200, response.text
        assert response.json()["success_count"] == 100
    assert len(_rows(db_session, exp, user_id)) == 300
    arms = _arms(_results(admin_client, exp))
    assert arms[COUNT]["treatment"]["mean"] == pytest.approx(300 / USERS)
