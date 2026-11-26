"""A visual experiment's goal events count for their own metric, in each arm (#1545).

The browser script (sdk/visual) records a goal as one event on
``POST /api/v1/tracking/batch``: ``event_type`` is the goal's event name (the
``event_name`` of one of the experiment's metrics), with ``user_id``,
``experiment_key``, ``event_id`` (``<page-view id>:<event name>``) and the page
view's website details as ``metadata``, nothing else. Here two goals with
distinct event names are sent exactly so for visitors of both arms, assigned
through ``POST /api/v1/tracking/visual``, and the experiment's results count
each goal only for its own metric (through ``conversion_event_filter``), in
the visitor's own arm; every stored row carries its sender's variant. A call
sent again, as the script's outbox sends one the API did not answer, stores
nothing more.

Experiments are made and started through the experiment routes by a
DEVELOPER, and the key through ``POST /api/v1/api-keys``.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.core.config import settings
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.user import UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    api_key,
    auth,
    local_sign_in,
    make_user,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

EXPERIMENTS = "/api/v1/experiments"
VISUAL = "/api/v1/tracking/visual"
BATCH = "/api/v1/tracking/batch"
PREFIX = "visual1545goals-"
VISITOR = "v1545goals-"
SCHEMA = "test_experimentation"
CLICK = "click_buy_button"
THANKS = "page_thanks"
#: The website details the script sends with a page view's goal events.
DETAILS = {"web_device": "desktop", "web_browser": "chrome", "web_language": "en"}
VISITORS = 13
#: Every visitor clicks; these six also reach the thank-you page.
THANKED = {0, 2, 4, 6, 8, 10}


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    local_sign_in(monkeypatch)
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture(autouse=True)
def _no_holdout(db_session):
    """No active global holdout, so every visitor is assigned."""
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


def _session(db_session):
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False)
    session = factory()
    session.execute(text(f"SET search_path TO {SCHEMA}"))
    return session


@pytest.fixture(autouse=True)
def _remove_what_the_tests_made(db_session):
    yield
    session = _session(db_session)
    try:
        session.execute(
            text("DELETE FROM events WHERE user_id LIKE :p"), {"p": f"{VISITOR}%"}
        )
        session.execute(
            text("DELETE FROM experiments WHERE name LIKE :p"), {"p": f"{PREFIX}%"}
        )
        session.commit()
    finally:
        session.close()


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def developer(db_session):
    return make_user(db_session, UserRole.DEVELOPER, tag="vis3b")


@pytest.fixture
def key(client, developer):
    return api_key(client, developer)


def _running(client, user, host: str) -> Dict[str, Any]:
    shop = {"match": "prefix", "url": f"https://{host}/shop"}
    thanks = {"match": "exact", "url": f"https://{host}/thanks"}
    created = client.post(
        f"{EXPERIMENTS}/",
        json={
            "name": f"{PREFIX}{uuid.uuid4().hex[:8]}",
            "experiment_type": "a_b",
            "visual_config": {
                "version": 1,
                "editor_url": shop["url"],
                "pages": [shop],
                "goals": [
                    {
                        "event_name": CLICK,
                        "kind": "click",
                        "selector": "#buy",
                        "page": shop,
                    },
                    {"event_name": THANKS, "kind": "page_view", "page": thanks},
                ],
            },
            "variants": [
                {"name": "Control", "is_control": True, "traffic_allocation": 50},
                {
                    "name": "B",
                    "is_control": False,
                    "traffic_allocation": 50,
                    "visual_changes": {
                        "version": 1,
                        "changes": [
                            {"op": "set_text", "selector": "#headline", "text": "Hi"}
                        ],
                    },
                },
            ],
            "metrics": [
                {
                    "name": "Clicked buy",
                    "event_name": CLICK,
                    "metric_type": "conversion",
                    "is_primary": True,
                },
                {
                    "name": "Reached thanks",
                    "event_name": THANKS,
                    "metric_type": "conversion",
                    "is_primary": False,
                },
            ],
        },
        headers=auth(user),
    )
    assert created.status_code == 201, created.text
    experiment = created.json()
    read = client.get(f"{EXPERIMENTS}/{experiment['id']}", headers=auth(user))
    assert read.status_code == 200, read.text
    started = client.post(
        f"{EXPERIMENTS}/{experiment['id']}/start",
        json={"design_etag": read.json()["design_etag"]},
        headers=auth(user),
    )
    assert started.status_code == 200, started.text
    return started.json()


def _page_view_id() -> str:
    return str(uuid.uuid4())


def _goal_event(
    goal: Dict[str, Any], visitor: str, experiment_key: str
) -> Dict[str, Any]:
    """One goal event, exactly as the script sends it."""
    name = goal["event_name"]
    return {
        "event_type": name,
        "user_id": visitor,
        "experiment_key": experiment_key,
        "event_id": f"{_page_view_id()}:{name}",
        "metadata": dict(DETAILS),
    }


def _results(client, user, experiment_id: str) -> Dict[str, Dict[str, int]]:
    """Each metric's conversions per variant name, as the results route counts them."""
    response = client.get(f"{EXPERIMENTS}/{experiment_id}/results", headers=auth(user))
    assert response.status_code == 200, response.text
    return {
        metric["metric_name"]: {
            v["variant_name"]: v["conversions"] for v in metric["variants"]
        }
        for metric in response.json()["metrics"]
    }


def _stored(db_session, experiment_id: str) -> List[Event]:
    session = _session(db_session)
    try:
        return (
            session.query(Event)
            .filter(
                Event.experiment_id == uuid.UUID(experiment_id),
                Event.event_type.in_([CLICK, THANKS]),
            )
            .all()
        )
    finally:
        session.close()


def _variants_of(db_session, experiment_id: str) -> Dict[str, str]:
    session = _session(db_session)
    try:
        rows = (
            session.query(Assignment.user_id, Assignment.variant_id)
            .filter(Assignment.experiment_id == uuid.UUID(experiment_id))
            .all()
        )
        return {user_id: str(variant_id) for user_id, variant_id in rows}
    finally:
        session.close()


@pytest.mark.regression
def test_each_goal_counts_only_for_its_own_metric_in_the_visitors_own_arm(
    client, db_session, developer, key
):
    host = f"shop-{uuid.uuid4().hex[:10]}.example.com"
    experiment = _running(client, developer, host)
    visitors = [f"{VISITOR}{uuid.uuid4().hex[:12]}" for _ in range(VISITORS)]
    arms: Dict[str, str] = {}
    events: List[Dict[str, Any]] = []
    for index, visitor in enumerate(visitors):
        answer = client.post(
            VISUAL,
            json={
                "user_id": visitor,
                "host": host,
                "path": "/shop",
                "format_version": 1,
            },
            headers=key,
        )
        assert answer.status_code == 200, answer.text
        (mine,) = answer.json()["experiments"]
        assert mine["assigned"] is True
        arms[visitor] = mine["variant_name"]
        goals = {goal["event_name"]: goal for goal in mine["goals"]}
        assert sorted(goals) == sorted([CLICK, THANKS])
        events.append(_goal_event(goals[CLICK], visitor, experiment["key"]))
        if index in THANKED:
            events.append(_goal_event(goals[THANKS], visitor, experiment["key"]))
    assert set(arms.values()) == {"Control", "B"}, arms

    sent = client.post(BATCH, json={"events": events}, headers=key)
    assert sent.status_code == 200, sent.text
    assert sent.json()["success_count"] == len(events), sent.text
    # The same call again, as the outbox sends one whose answer it never got.
    again = client.post(BATCH, json={"events": events}, headers=key)
    assert again.status_code == 200, again.text
    assert again.json()["duplicate_count"] == len(events), again.text

    clicked = dict.fromkeys(("Control", "B"), 0)
    thanked = dict.fromkeys(("Control", "B"), 0)
    for index, visitor in enumerate(visitors):
        clicked[arms[visitor]] += 1
        if index in THANKED:
            thanked[arms[visitor]] += 1
    results = _results(client, developer, experiment["id"])
    assert (
        f"goal B: {sum(results['Reached thanks'].values())}"
        == f"goal B: {len(THANKED)}"
    )
    assert f"goal A: {sum(results['Clicked buy'].values())}" == f"goal A: {VISITORS}"
    assert results == {"Clicked buy": clicked, "Reached thanks": thanked}

    stored = _stored(db_session, experiment["id"])
    assert len(stored) == len(events)
    variants = _variants_of(db_session, experiment["id"])
    assert sorted((row.user_id, str(row.variant_id)) for row in stored) == sorted(
        (event["user_id"], variants[event["user_id"]]) for event in events
    )
    assert {row.event_metadata == DETAILS for row in stored} == {True}
