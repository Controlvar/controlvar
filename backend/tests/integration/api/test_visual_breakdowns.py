"""Website-detail breakdowns of a visual experiment, under #1776's rules (#1545, SEC-15).

``POST /api/v1/tracking/visual`` stores the ``web_*`` details on exposures,
which is where a breakdown reads a visitor's value. These rows hold #1776's
breakdown to the website details, on both breakdown routes
(``GET /api/v1/results/{id}?breakdown=`` and
``GET /api/v1/experiments/{id}/segmented-results/``), each with the defect
that fails it:

* V105 this file imports #1776's ``MAX_LISTED_SEGMENTS`` and asserts 20: before
       #1776 is on the branch every test here fails, naming #1776, so PR 2
       cannot merge first. The import's failure is caught and each test fails
       on it, never the collection: a collection error would stop every other
       test of the session, and a skip would turn this red into green.
* V104 a ``web_*`` breakdown reads in a fixed number of statements: 21 and
       10,000 distinct ``web_referrer`` values take the same number, and list
       exactly 20 values plus ``other`` and ``unknown``. Plant: ``web_*``
       skipping the fold.
* V107 the first exposure decides a visitor's detail: ``google`` received
       first and ``bing`` second, with ``created_at`` the other way round, puts
       the visitor under ``google``. Plant: ordering by ``created_at``.
* V108 a landing path longer than 255 characters is not stored, so it is never
       cut and never counted under ``other``; one of 255 is stored and listed.
       Plant: the route cutting paths to 255 (two visitors under one cut path).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List

import pytest
from sqlalchemy import event, insert, text
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
    make_user,
)

try:  # V105
    from backend.app.services.segment_breakdown import (
        MAX_LISTED_SEGMENTS,
        OTHER,
        UNKNOWN,
    )
except ImportError as missing:
    _WITHOUT_1776: str | None = (
        "#1776's breakdown change is not on this branch "
        f"({type(missing).__name__}: {missing}); these tests hold the visual "
        "editor's website details to its rules, so #1545's delivery routes "
        "merge after it"
    )
    # Nothing a test could pass on: each fails first, in _needs_1776.
    MAX_LISTED_SEGMENTS = OTHER = UNKNOWN = None  # type: ignore[assignment]
else:
    _WITHOUT_1776 = None

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

EXPERIMENTS = "/api/v1/experiments"
VISUAL = "/api/v1/tracking/visual"
PREFIX = "visual1545bk-"
VISITOR = "v1545bk-"
SCHEMA = "test_experimentation"
METRIC_EVENT = "click_buy"
#: Naive UTC, as ``events.updated_at`` stores it.
BASE = datetime(2026, 11, 1, 12, 0, 0)


@pytest.fixture(autouse=True)
def _needs_1776():
    """V105: every test here fails, before it touches the database, until
    #1776 is on the branch. Never a skip."""
    if _WITHOUT_1776 is not None:
        pytest.fail(_WITHOUT_1776, pytrace=False)


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture(autouse=True)
def _no_holdout(db_session):
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
    return make_user(db_session, UserRole.DEVELOPER, tag="vbk1545")


@pytest.fixture
def viewer(db_session):
    return make_user(db_session, UserRole.VIEWER, tag="vbk1545")


@pytest.fixture
def key(client, developer):
    return api_key(client, developer)


def _running(client, user, host: str) -> Dict[str, Any]:
    page = {"match": "prefix", "url": f"https://{host}/"}
    created = client.post(
        f"{EXPERIMENTS}/",
        json={
            "name": f"{PREFIX}{uuid.uuid4().hex[:8]}",
            "experiment_type": "a_b",
            "visual_config": {
                "version": 1,
                "editor_url": f"https://{host}/",
                "pages": [page],
                "goals": [],
            },
            "variants": [
                {"name": "Control", "is_control": True, "traffic_allocation": 50},
                {
                    "name": "B",
                    "is_control": False,
                    "traffic_allocation": 50,
                    "visual_changes": {
                        "version": 1,
                        "changes": [{"op": "hide", "selector": "#promo"}],
                    },
                },
            ],
            "metrics": [
                {
                    "name": "Buy clicks",
                    "event_name": METRIC_EVENT,
                    "metric_type": "conversion",
                    "is_primary": True,
                }
            ],
        },
        headers=auth(user),
    )
    assert created.status_code == 201, created.text
    experiment = created.json()
    etag = client.get(f"{EXPERIMENTS}/{experiment['id']}", headers=auth(user)).json()[
        "design_etag"
    ]
    started = client.post(
        f"{EXPERIMENTS}/{experiment['id']}/start",
        json={"design_etag": etag},
        headers=auth(user),
    )
    assert started.status_code == 200, started.text
    return started.json()


def _host() -> str:
    return f"shop-{uuid.uuid4().hex[:10]}.example.com"


def _results_rows(client, user, experiment_id: str, dimension: str) -> Dict[str, int]:
    response = client.get(
        f"/api/v1/results/{experiment_id}",
        params={"breakdown": dimension, "use_cache": "false"},
        headers=auth(user),
    )
    assert response.status_code == 200, response.text
    return {
        s["segment_value"]: sum(v["sample_size"] for v in s["variants"])
        for s in response.json()["breakdown"]["segments"]
    }


def _segmented_rows(client, user, experiment_id: str, dimension: str) -> Dict[str, int]:
    response = client.get(
        f"{EXPERIMENTS}/{experiment_id}/segmented-results/{dimension}",
        params={"skip_cache": "true"},
        headers=auth(user),
    )
    assert response.status_code == 200, response.text
    rows = {}
    for segment in response.json()["segments"]:
        metric = segment["metrics"][0]
        rows[segment["segment_value"]] = sum(
            v["sample_size"] for v in metric["variants"]
        )
    return rows


def _both(
    client, user, experiment_id: str, dimension: str
) -> Dict[str, Dict[str, int]]:
    return {
        "/results": _results_rows(client, user, experiment_id, dimension),
        "/segmented-results": _segmented_rows(client, user, experiment_id, dimension),
    }


# --- V105 -------------------------------------------------------------------------


@pytest.mark.regression
def test_the_breakdown_lists_twenty_values():
    assert MAX_LISTED_SEGMENTS == 20


# --- V104: the cost does not grow with distinct web_* values -------------------------


def _seed_referrers(db_session, experiment: Dict[str, Any], values: int) -> None:
    """``values`` assigned visitors, each with one exposure carrying its own
    ``web_referrer``, and one assigned visitor with none (``unknown``)."""
    variant_ids = [v["id"] for v in experiment["variants"]]
    assignments, events = [], []
    for index in range(values + 1):
        user_id = f"{VISITOR}{experiment['key'][-8:]}-{index}"
        variant_id = uuid.UUID(variant_ids[index % 2])
        assignments.append(
            {
                "id": uuid.uuid4(),
                "experiment_id": uuid.UUID(experiment["id"]),
                "variant_id": variant_id,
                "user_id": user_id,
            }
        )
        if index < values:
            events.append(
                {
                    "id": uuid.uuid4(),
                    "experiment_id": uuid.UUID(experiment["id"]),
                    "variant_id": variant_id,
                    "user_id": user_id,
                    "event_type": "exposure",
                    "event_name": "variant_exposure",
                    "event_metadata": {"web_referrer": f"r{index:05d}.example.com"},
                    "created_at": BASE.isoformat() + "+00:00",
                    "updated_at": BASE,
                }
            )
    session = _session(db_session)
    try:
        session.execute(insert(Assignment), assignments)
        session.execute(insert(Event), events)
        session.commit()
    finally:
        session.close()


class _Statements:
    def __init__(self, engine):
        self.engine = engine
        self.count = 0

    def _record(self, *_args):
        self.count += 1

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *_exc):
        event.remove(self.engine, "before_cursor_execute", self._record)


@pytest.mark.regression
def test_a_web_detail_breakdown_takes_the_same_statements_at_any_cardinality(
    client, db_session, developer, viewer
):
    engine = db_session.get_bind()
    counts: Dict[str, Dict[int, int]] = {"/results": {}, "/segmented-results": {}}
    for values in (21, 10_000):
        experiment = _running(client, developer, _host())
        _seed_referrers(db_session, experiment, values)
        for route, read in (
            ("/results", _results_rows),
            ("/segmented-results", _segmented_rows),
        ):
            read(client, viewer, experiment["id"], "web_referrer")
            with _Statements(engine) as statements:
                rows = read(client, viewer, experiment["id"], "web_referrer")
            counts[route][values] = statements.count
            assert len(rows) == MAX_LISTED_SEGMENTS + 2, (route, values)
            assert rows[UNKNOWN] == 1, (route, values)
            assert rows[OTHER] == values - MAX_LISTED_SEGMENTS, (route, values)
            assert sum(rows.values()) == values + 1, (route, values)
    for route, by_values in counts.items():
        assert by_values[21] == by_values[10_000], f"{route}: {by_values}"


# --- V107: the first exposure decides --------------------------------------------------


def _exposure_ids(db_session, experiment_id: str, user_id: str) -> Dict[str, uuid.UUID]:
    session = _session(db_session)
    try:
        rows = (
            session.query(Event)
            .filter(
                Event.experiment_id == uuid.UUID(experiment_id),
                Event.user_id == user_id,
                Event.event_type == "exposure",
            )
            .all()
        )
        return {row.event_metadata["web_referrer"]: row.id for row in rows}
    finally:
        session.close()


@pytest.mark.regression
def test_the_first_exposure_received_decides_a_visitors_detail(
    client, db_session, developer, viewer, key
):
    host = _host()
    experiment = _running(client, developer, host)
    visitor = f"{VISITOR}{uuid.uuid4().hex[:10]}"
    for referrer in ("www.google.com", "www.bing.com"):
        response = client.post(
            VISUAL,
            json={
                "user_id": visitor,
                "host": host,
                "path": "/",
                "format_version": 1,
                "context": {"web_referrer": referrer},
            },
            headers=key,
        )
        assert response.status_code == 200, response.text
    ids = _exposure_ids(db_session, experiment["id"], visitor)
    assert set(ids) == {"www.google.com", "www.bing.com"}
    session = _session(db_session)
    try:
        # google received first, bing second; the client's times the other way.
        for referrer, received, created in (
            ("www.google.com", BASE, BASE + timedelta(hours=1)),
            ("www.bing.com", BASE + timedelta(minutes=1), BASE - timedelta(hours=1)),
        ):
            session.execute(
                text(
                    "UPDATE events SET updated_at = :received, created_at = :created "
                    "WHERE id = :id"
                ),
                {"received": received, "created": created, "id": ids[referrer]},
            )
        session.commit()
    finally:
        session.close()
    for route, rows in _both(client, viewer, experiment["id"], "web_referrer").items():
        assert rows == {"www.google.com": 1}, route


# --- V108: long landing paths are dropped, never cut -----------------------------------


@pytest.mark.regression
def test_a_long_landing_path_is_not_stored_and_a_255_character_one_is_listed(
    client, db_session, developer, viewer, key
):
    host = _host()
    experiment = _running(client, developer, host)
    kept = "/" + "b" * 254
    long_prefix = "/" + "a" * 254
    sent = {
        "kept": kept,
        "256": long_prefix + "x",
        "300": long_prefix + "y" * 45,
    }
    assert {len(path) for path in sent.values()} == {255, 256, 300}
    visitors = {}
    for name, path in sent.items():
        visitors[name] = f"{VISITOR}{name}-{uuid.uuid4().hex[:8]}"
        response = client.post(
            VISUAL,
            json={
                "user_id": visitors[name],
                "host": host,
                "path": "/",
                "format_version": 1,
                "context": {"web_landing_path": path},
            },
            headers=key,
        )
        assert response.status_code == 200, response.text
    session = _session(db_session)
    try:
        stored: List[Any] = [
            row[0]
            for row in session.execute(
                text(
                    "SELECT event_metadata FROM events WHERE experiment_id = :e "
                    "AND event_type = 'exposure'"
                ),
                {"e": uuid.UUID(experiment["id"])},
            ).all()
        ]
    finally:
        session.close()
    assert sorted(stored, key=lambda m: len(m)) == [{}, {}, {"web_landing_path": kept}]
    for route, rows in _both(
        client, viewer, experiment["id"], "web_landing_path"
    ).items():
        assert rows == {kept: 1, UNKNOWN: 2}, route
        assert OTHER not in rows, route
