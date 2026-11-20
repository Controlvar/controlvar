"""A web page asks which visual changes and goals its visitor gets (#1545, PR 2).

``POST /api/v1/tracking/visual`` on Postgres, through real routes: experiments
made and started through the experiment routes by a DEVELOPER, API keys made
through ``POST /api/v1/api-keys``, and every answer read back from the rows it
wrote. The rows of PLAN-v3.1's VERIFICATION held here, each with the defect
that fails it:

* V23  (server) the exposure matches what is answered: an assigned visitor
       gets exactly one exposure per matched page view, with the cleaned
       context; a visitor outside the audience, or a page no rule matches,
       gets none. Plant: no exposure (0 != 1).
* V24  results reuse the normal view: the same visitors and conversions give
       the same counts for a visual and a code experiment.
* V31  only running experiments are answered: a draft, a paused and a
       completed visual experiment on the same page are absent, and so are
       their page rules. Plant: no status filter.
* V45a a rules segment on a website detail matches exactly.
* V56  (server) no visitor IP address is stored: after a page answer, a goal
       and an error report, no string or JSON column holds the client's
       address. Plant: the route adds ``request.client.host`` to the context.
* V57  (server) no raw referrer, landing query or click id is stored.
* V96  (S3) a key with any scope is refused with 403, an unscoped one served.
* V106 the cost of one answer is pinned: exact statement counts at 1 and at
       10 matching experiments, each targeting a segment; 10 experiments x 3
       page views store exactly 30 exposures.
* R4   ``/tracking/assign`` stores the context it is sent, ``web_*`` keys
       included: the cleaning is this route's alone.
* R10  a key with ``sdk:ruleset`` still works on ``/tracking/assign``,
       ``/tracking/batch`` and flag evaluation.

Also: at most 10 experiments, the one that started first first; a format
newer than the script's, or stored values that fail their schema, leave that
experiment out with nobody assigned, a 200 when it is the only match (plant:
counting it as tried, 500); one experiment that cannot be answered leaves only
itself out; a pool timeout or a lost database stops the answer, no later
experiment tried: a 503 at the first experiment (plant: trying the next one,
500), a 200 with exactly the first at the second (plant: always a 503);
``Cache-Control: no-store``; 401 and 422.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import event, text
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.page_key import SCOPED_KEY_IN_PAGE_DETAIL
from backend.app.api.v1.endpoints import tracking as tracking_endpoints
from backend.app.api.v1.endpoints import tracking_visual
from backend.app.core.config import settings
from backend.app.db import request_sessions
from backend.app.main import app
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.user import UserRole
from backend.app.services.assignment_service import AssignmentService
from backend.tests.integration.api.route_helpers import (
    api_client,
    api_key,
    auth,
    make_user,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

EXPERIMENTS = "/api/v1/experiments"
VISUAL = "/api/v1/tracking/visual"
PREFIX = "visual1545pr2-"
VISITOR = "v1545pr2-"
SCHEMA = "test_experimentation"
CHANGES = {
    "version": 1,
    "changes": [{"op": "set_text", "selector": "#headline", "text": "Hello"}],
}
METRIC_EVENT = "click_buy"


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture(autouse=True)
def _no_holdout(db_session):
    """No active global holdout, so an eligible visitor is assigned."""
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
        session.execute(
            text("DELETE FROM segments WHERE name LIKE :p"), {"p": f"{PREFIX}%"}
        )
        session.commit()
    finally:
        session.close()


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def registering_client(client, db_session):
    """``client`` with an async ``get_db`` that registers its session as
    ``deps.get_db`` does, so a balanced experiment's assignment ends that
    session's transaction as in production (the shared override registers
    nothing). ``client`` removes the override when it is torn down, after
    this fixture."""
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False)

    async def registering_get_db():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        request_sessions.register(session)
        try:
            yield session
        finally:
            try:
                session.close()
            finally:
                request_sessions.unregister(session)

    app.dependency_overrides[deps.get_db] = registering_get_db
    yield client


@pytest.fixture
def developer(db_session):
    return make_user(db_session, UserRole.DEVELOPER, tag="vis2")


@pytest.fixture
def key(client, developer):
    return api_key(client, developer)


def _host() -> str:
    return f"shop-{uuid.uuid4().hex[:10]}.example.com"


def _visitor() -> str:
    return f"{VISITOR}{uuid.uuid4().hex[:12]}"


def _config(host: str, match: str = "prefix", path: str = "/") -> Dict[str, Any]:
    page = {"match": match, "url": f"https://{host}{path}"}
    return {
        "version": 1,
        "editor_url": f"https://{host}{path}",
        "pages": [page],
        "goals": [
            {
                "event_name": METRIC_EVENT,
                "kind": "click",
                "selector": "#buy",
                "page": page,
            }
        ],
    }


def _create(
    client,
    user,
    host: str,
    *,
    control: int = 0,
    targeting: Optional[Dict[str, Any]] = None,
    match: str = "prefix",
    path: str = "/",
    balance_within: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "name": f"{PREFIX}{uuid.uuid4().hex[:8]}",
        "experiment_type": "a_b",
        "visual_config": _config(host, match, path),
        "variants": [
            {"name": "Control", "is_control": True, "traffic_allocation": control},
            {
                "name": "B",
                "is_control": False,
                "traffic_allocation": 100 - control,
                "visual_changes": CHANGES,
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
    }
    if targeting is not None:
        body["targeting_rules"] = targeting
    if balance_within is not None:
        body["balance_within"] = balance_within
    response = client.post(f"{EXPERIMENTS}/", json=body, headers=auth(user))
    assert response.status_code == 201, response.text
    return response.json()


def _start(client, user, experiment: Dict[str, Any]) -> Dict[str, Any]:
    read = client.get(f"{EXPERIMENTS}/{experiment['id']}", headers=auth(user))
    assert read.status_code == 200, read.text
    started = client.post(
        f"{EXPERIMENTS}/{experiment['id']}/start",
        json={"design_etag": read.json()["design_etag"]},
        headers=auth(user),
    )
    assert started.status_code == 200, started.text
    return started.json()


def _running(client, user, host: str, **kwargs) -> Dict[str, Any]:
    return _start(client, user, _create(client, user, host, **kwargs))


def _ask(
    client,
    key,
    host: str,
    *,
    user_id: Optional[str] = None,
    path: str = "/",
    context: Optional[Dict[str, Any]] = None,
    format_version: int = 1,
    status: int = 200,
):
    body: Dict[str, Any] = {
        "user_id": user_id or _visitor(),
        "host": host,
        "path": path,
        "format_version": format_version,
    }
    if context is not None:
        body["context"] = context
    response = client.post(VISUAL, json=body, headers=key)
    assert response.status_code == status, response.text
    return response


def _exposures(db_session, experiment_id: str, user_id: str) -> List[Event]:
    session = _session(db_session)
    try:
        return (
            session.query(Event)
            .filter(
                Event.experiment_id == uuid.UUID(experiment_id),
                Event.user_id == user_id,
                Event.event_type == "exposure",
            )
            .all()
        )
    finally:
        session.close()


def _assignments(db_session, experiment_id: str) -> int:
    session = _session(db_session)
    try:
        return (
            session.query(Assignment)
            .filter(Assignment.experiment_id == uuid.UUID(experiment_id))
            .count()
        )
    finally:
        session.close()


def _phone_only() -> Dict[str, Any]:
    return {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "web_device", "operator": "equals", "value": "phone"}
                ],
            }
        ],
    }


# --- V23: what is answered, and what is recorded --------------------------------


@pytest.mark.regression
def test_an_assigned_visitor_gets_the_changes_the_goals_and_one_exposure_per_view(
    client, db_session, developer, key
):
    host = _host()
    experiment = _running(client, developer, host)
    visitor = _visitor()
    context = {"plan": "pro", "web_device": "phone", "web_gclid": "XYZ"}

    answer = _ask(client, key, host, user_id=visitor, path="/shoes", context=context)
    assert answer.headers["cache-control"] == "no-store"
    body = answer.json()
    assert [e["experiment_key"] for e in body["experiments"]] == [experiment["key"]]
    (entry,) = body["experiments"]
    assert entry["assigned"] is True and entry["reason"] == "assigned"
    assert entry["variant_name"] == "B"
    assert entry["changes"] == CHANGES
    assert entry["goals"] == _config(host)["goals"]

    exposures = _exposures(db_session, experiment["id"], visitor)
    assert len(exposures) == 1
    assert exposures[0].event_metadata == {"plan": "pro", "web_device": "phone"}
    assert _assignments(db_session, experiment["id"]) == 1

    # Every matched page view is an exposure; the assignment is sticky.
    again = _ask(client, key, host, user_id=visitor, context=context).json()
    assert again["experiments"][0]["variant_id"] == entry["variant_id"]
    assert len(_exposures(db_session, experiment["id"], visitor)) == 2
    assert _assignments(db_session, experiment["id"]) == 1


@pytest.mark.regression
def test_a_visitor_outside_the_audience_gets_no_changes_and_nothing_is_recorded(
    client, db_session, developer, key
):
    host = _host()
    experiment = _running(client, developer, host, targeting=_phone_only())
    visitor = _visitor()
    body = _ask(
        client, key, host, user_id=visitor, context={"web_device": "desktop"}
    ).json()
    (entry,) = body["experiments"]
    assert entry["assigned"] is False and entry["reason"] == "targeting"
    assert entry["changes"] is None and entry["goals"] is None
    assert entry["variant_name"] == "Control"
    assert _exposures(db_session, experiment["id"], visitor) == []
    assert _assignments(db_session, experiment["id"]) == 0


def test_the_control_gets_the_goals_and_no_changes(client, developer, key):
    host = _host()
    _running(client, developer, host, control=100)
    (entry,) = _ask(client, key, host).json()["experiments"]
    assert entry["assigned"] is True and entry["variant_name"] == "Control"
    assert entry["changes"] is None
    assert entry["goals"] == _config(host)["goals"]


def test_a_page_no_rule_matches_gets_nothing_and_records_nothing(
    client, db_session, developer, key
):
    host = _host()
    experiment = _running(client, developer, host, match="exact", path="/landing")
    visitor = _visitor()
    body = _ask(client, key, host, user_id=visitor, path="/other").json()
    assert body["experiments"] == []
    assert body["pages"] == {
        "host": host,
        "rules": [{"match": "exact", "path": "/landing"}],
        "max_age": 60,
    }
    assert _exposures(db_session, experiment["id"], visitor) == []
    # Another host sees none of these rules.
    other = _ask(client, key, _host()).json()
    assert other == {
        "experiments": [],
        "pages": {"host": other["pages"]["host"], "rules": [], "max_age": 60},
    }


def test_the_host_is_matched_in_lower_case_without_its_port(client, developer, key):
    host = _host()
    experiment = _running(client, developer, host)
    body = _ask(client, key, host.upper() + ":8443", path="/a?b=c").json()
    assert [e["experiment_key"] for e in body["experiments"]] == [experiment["key"]]
    assert body["pages"]["host"] == host


@pytest.mark.regression
def test_a_balanced_visual_experiment_gives_each_new_visitor_a_place(
    registering_client, db_session, developer, key, monkeypatch
):
    """An experiment that balances within groups (#448), answered from the
    page answer's private event loop: ``/tracking/assign``'s function ends the
    transaction of the request's one session, then takes its worker trip on
    that private loop. Each new visitor of the group takes the next place in
    its blocks and gets their variant's changes."""
    client = registering_client
    host = _host()
    experiment = _running(
        client,
        developer,
        host,
        control=50,
        balance_within={"attribute": "tier", "values": ["gold", "silver"]},
    )
    released: List[List[bool]] = []
    trip_loops: List[asyncio.AbstractEventLoop] = []
    real_release = tracking_endpoints.release_request_sessions
    real_trip = tracking_endpoints.run_in_threadpool

    def release():
        sessions = request_sessions.current()
        real_release()
        released.append([session.in_transaction() for session in sessions])

    async def trip(func, *args, **kwargs):
        trip_loops.append(asyncio.get_running_loop())
        return await real_trip(func, *args, **kwargs)

    monkeypatch.setattr(tracking_endpoints, "release_request_sessions", release)
    monkeypatch.setattr(tracking_endpoints, "run_in_threadpool", trip)

    names = []
    for _ in range(4):
        body = _ask(client, key, host, user_id=_visitor(), context={"tier": "gold"})
        (entry,) = body.json()["experiments"]
        assert entry["assigned"] is True, entry
        expected = CHANGES if entry["variant_name"] == "B" else None
        assert entry["changes"] == expected, entry
        names.append(entry["variant_name"])

    # Two blocks of a 50/50 split: two visitors in each variant.
    assert sorted(names) == ["B", "B", "Control", "Control"], names
    # One session each time, its transaction ended before the trip.
    assert released == [[False]] * 4, released
    # Each trip ran on a private loop, closed once its answer was given.
    assert len(trip_loops) == 4 and all(loop.is_closed() for loop in trip_loops)
    session = _session(db_session)
    try:
        rows = (
            session.query(Assignment.balance_group, Assignment.balance_seq)
            .filter(Assignment.experiment_id == uuid.UUID(experiment["id"]))
            .all()
        )
    finally:
        session.close()
    assert sorted(rows, key=lambda row: row[1]) == [("gold", seq) for seq in range(4)]


# --- V31: running experiments only ----------------------------------------------


@pytest.mark.regression
def test_only_running_experiments_are_answered(client, developer, key):
    host = _host()
    running = _running(client, developer, host)
    draft = _create(client, developer, host, match="exact", path="/draft")
    paused = _running(client, developer, host, match="exact", path="/paused")
    assert (
        client.post(
            f"{EXPERIMENTS}/{paused['id']}/pause", headers=auth(developer)
        ).status_code
        == 200
    )
    completed = _running(client, developer, host, match="exact", path="/completed")
    assert (
        client.post(
            f"{EXPERIMENTS}/{completed['id']}/complete", headers=auth(developer)
        ).status_code
        == 200
    )
    for path in ("/", "/draft", "/paused", "/completed"):
        body = _ask(client, key, host, path=path).json()
        assert [e["experiment_key"] for e in body["experiments"]] == [running["key"]]
        assert body["pages"]["rules"] == [{"match": "prefix", "path": "/"}]
    for gone in (draft, paused, completed):
        assert gone["key"] not in _ask(client, key, host).text


# --- at most ten, oldest first; formats and stored values -----------------------


def _set_start(db_session, experiment_id: str, when: datetime) -> None:
    session = _session(db_session)
    try:
        session.execute(
            text("UPDATE experiments SET start_date = :when WHERE id = :id"),
            {"when": when, "id": uuid.UUID(experiment_id)},
        )
        session.commit()
    finally:
        session.close()


def test_at_most_ten_experiments_the_one_that_started_first_first(
    client, db_session, developer, key
):
    host = _host()
    base = datetime.now(timezone.utc) - timedelta(days=30)
    made = []
    for index in range(11):
        experiment = _running(client, developer, host)
        # Started in the reverse of the order they were made.
        _set_start(db_session, experiment["id"], base - timedelta(hours=index))
        made.append(experiment)
    body = _ask(client, key, host).json()
    oldest_first = [e["key"] for e in reversed(made)]
    assert [e["experiment_key"] for e in body["experiments"]] == oldest_first[:10]
    # The eleventh assigns nobody.
    assert _assignments(db_session, made[0]["id"]) == 0


def _write_row(db_session, sql: str, **params) -> None:
    session = _session(db_session)
    try:
        session.execute(text(sql), params)
        session.commit()
    finally:
        session.close()


def _break_changes(db_session, experiment_id: str) -> None:
    """Store page changes that fail their schema, as only a row written
    outside the API can hold."""
    _write_row(
        db_session,
        'UPDATE variants SET visual_changes = \'{"version": 1, "changes": '
        '[{"op": "set_html", "selector": "p"}]}\' '
        "WHERE experiment_id = :id AND NOT is_control",
        id=uuid.UUID(experiment_id),
    )


def test_a_newer_format_or_a_stored_value_that_fails_its_schema_is_left_out(
    client, db_session, developer, key
):
    host = _host()
    good = _running(client, developer, host)
    newer = _running(client, developer, host)
    broken = _running(client, developer, host)
    _write_row(
        db_session,
        "UPDATE experiments SET visual_config = jsonb_set(visual_config, "
        "'{version}', '2') WHERE id = :id",
        id=uuid.UUID(newer["id"]),
    )
    _break_changes(db_session, broken["id"])
    body = _ask(client, key, host).json()
    assert [e["experiment_key"] for e in body["experiments"]] == [good["key"]]
    assert _assignments(db_session, newer["id"]) == 0
    assert _assignments(db_session, broken["id"]) == 0


@pytest.mark.regression
def test_when_the_only_match_fails_its_schema_the_page_gets_200_without_it(
    client, db_session, developer, key
):
    """Left out means left out: not tried, so not a 500 on every page view."""
    host = _host()
    broken = _running(client, developer, host)
    _break_changes(db_session, broken["id"])
    for _view in range(2):
        assert _ask(client, key, host).json()["experiments"] == []
    assert _assignments(db_session, broken["id"]) == 0


def test_one_experiment_that_cannot_be_answered_leaves_only_itself_out(
    client, developer, key, monkeypatch
):
    host = _host()
    first = _running(client, developer, host)
    second = _running(client, developer, host)
    real = tracking_endpoints.assign_user_to_experiment

    async def fails_for_the_first(request, db, api_key_info):
        if request.experiment_key == first["key"]:
            raise RuntimeError("planted")
        return await real(request=request, db=db, api_key_info=api_key_info)

    monkeypatch.setattr(
        tracking_endpoints, "assign_user_to_experiment", fails_for_the_first
    )
    body = _ask(client, key, host).json()
    assert [e["experiment_key"] for e in body["experiments"]] == [second["key"]]

    async def always_fails(request, db, api_key_info):
        raise RuntimeError("planted")

    monkeypatch.setattr(tracking_endpoints, "assign_user_to_experiment", always_fails)
    failed = _ask(client, key, host, status=500)
    assert failed.json()["detail"].startswith(tracking_visual.PAGE_FAILURE_SENTENCE)


@pytest.mark.regression
@pytest.mark.parametrize("fails_at", [1, 2], ids=["first fails", "second fails"])
@pytest.mark.parametrize("where", ["pool timeout in the assignment", "lost database"])
def test_a_database_failure_stops_the_answer(
    client, db_session, developer, key, monkeypatch, where, fails_at
):
    """A database failure ends the answer: no later experiment is tried, so
    a failing database costs one wait, not ten. When it is the first, the
    answer is a 503 and nothing is recorded; when it is the second, the
    answer is a 200 with exactly the first, which is recorded, so it is
    shown (a 503 would leave its exposure for a page that showed nothing)."""
    host = _host()
    base = datetime.now(timezone.utc) - timedelta(days=30)
    made = []
    for index in range(3):
        experiment = _running(client, developer, host)
        _set_start(db_session, experiment["id"], base + timedelta(hours=index))
        made.append(experiment)
    visitor = _visitor()
    tried: List[str] = []
    if where == "pool timeout in the assignment":
        # Inside AssignmentService: /tracking/assign's function answers its
        # own 500 from inside ``except``, with the timeout underneath.
        field = "id"
        real_assign_user = AssignmentService.assign_user

        def times_out(self, *args, **kwargs):
            tried.append(kwargs["experiment_id"])
            if len(tried) == fails_at:
                raise sa_exc.TimeoutError("planted: the pool timed out")
            return real_assign_user(self, *args, **kwargs)

        monkeypatch.setattr(AssignmentService, "assign_user", times_out)
    else:
        field = "key"
        real_assign = tracking_endpoints.assign_user_to_experiment

        async def database_gone(request, db, api_key_info):
            tried.append(request.experiment_key)
            if len(tried) == fails_at:
                raise sa_exc.OperationalError(
                    "SELECT 1", {}, Exception("planted: the server closed")
                )
            return await real_assign(request=request, db=db, api_key_info=api_key_info)

        monkeypatch.setattr(
            tracking_endpoints, "assign_user_to_experiment", database_gone
        )

    if fails_at == 1:
        failed = _ask(client, key, host, user_id=visitor, status=503)
        assert failed.json()["detail"].startswith(
            tracking_visual.PAGE_UNAVAILABLE_SENTENCE
        )
    else:
        body = _ask(client, key, host, user_id=visitor).json()
        first = made[0]
        assert [e["experiment_key"] for e in body["experiments"]] == [first["key"]]
        assert body["experiments"][0]["assigned"] is True
        assert body["experiments"][0]["changes"] == CHANGES
        assert _assignments(db_session, first["id"]) == 1
        assert len(_exposures(db_session, first["id"], visitor)) == 1
    # Exactly the experiments up to the failing one were tried.
    assert tried == [made[i][field] for i in range(fails_at)], tried
    for experiment in made[fails_at - 1 :]:
        assert _assignments(db_session, experiment["id"]) == 0
        assert _exposures(db_session, experiment["id"], visitor) == []


# --- the request ------------------------------------------------------------------


def test_the_request_is_refused_when_it_is_not_in_the_documented_form(client, key):
    host = _host()
    good = {"user_id": _visitor(), "host": host, "path": "/", "format_version": 1}
    for change in (
        {"host": ""},
        {"host": "a b.example.com"},
        {"host": "a" * 262},
        {"path": "/a b"},
        {"path": "/a\nb"},
        {"path": "/" + "a" * 2048},
        {"format_version": 0},
        {"format_version": "1"},
        {"user_id": ""},
        {"user_id": "u" * 256},
        {"query": "q=1"},
        {"context": {"plan": "a\u0000b"}},
    ):
        response = client.post(VISUAL, json={**good, **change}, headers=key)
        assert response.status_code == 422, (change, response.text)
    del good["format_version"]
    assert client.post(VISUAL, json=good, headers=key).status_code == 422


def test_a_context_over_the_limits_is_refused(client, key):
    body = {
        "user_id": _visitor(),
        "host": _host(),
        "path": "/",
        "format_version": 1,
        "context": {"site": "x" * 9000},
    }
    response = client.post(VISUAL, json=body, headers=key)
    assert response.status_code == 422
    assert response.json()["detail"] == "Targeting context larger than 8192 bytes"


def test_no_key_or_an_unknown_key_is_refused(client):
    body = {"user_id": _visitor(), "host": _host(), "path": "/", "format_version": 1}
    assert client.post(VISUAL, json=body).status_code == 401
    unknown = client.post(VISUAL, json=body, headers={"X-API-Key": "not-a-key"})
    assert unknown.status_code == 401


# --- V96 (S3) and R10: keys ---------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "scopes",
    [
        ["sdk:ruleset"],
        ["mcp:read"],
        ["sdk:ruleset", "mcp:read"],
        ["future:x"],
        ["read"],
    ],
)
def test_a_key_with_any_scope_is_refused(client, developer, scopes):
    scoped = api_key(client, developer, scopes=scopes)
    body = {"user_id": _visitor(), "host": _host(), "path": "/", "format_version": 1}
    response = client.post(VISUAL, json=body, headers=scoped)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == SCOPED_KEY_IN_PAGE_DETAIL


def test_a_key_with_no_scopes_is_served(client, developer):
    unscoped = api_key(client, developer, scopes=[])
    _ask(client, unscoped, _host())


@pytest.mark.regression
def test_a_server_key_still_works_on_the_other_sdk_routes(
    client, db_session, developer
):
    """R10: the scope refusal is the page routes' alone."""
    server_key = api_key(client, developer, scopes=["sdk:ruleset"])
    experiment = _running(client, developer, _host())
    visitor = _visitor()
    assign = client.post(
        "/api/v1/tracking/assign",
        json={"experiment_key": experiment["key"], "user_id": visitor},
        headers=server_key,
    )
    assert assign.status_code == 200, assign.text
    batch = client.post(
        "/api/v1/tracking/batch",
        json={
            "events": [
                {
                    "event_type": "conversion",
                    "event_name": METRIC_EVENT,
                    "user_id": visitor,
                    "experiment_key": experiment["key"],
                }
            ]
        },
        headers=server_key,
    )
    assert batch.status_code in (200, 201), batch.text
    flag_key = f"{PREFIX}{uuid.uuid4().hex[:8]}"
    flag = client.post(
        "/api/v1/feature-flags/",
        json={
            "key": flag_key,
            "name": flag_key,
            "is_active": True,
            "rollout_percentage": 100,
        },
        headers=auth(developer),
    )
    assert flag.status_code == 201, flag.text
    try:
        evaluate = client.get(
            f"/api/v1/feature-flags/evaluate/{flag_key}",
            params={"user_id": visitor},
            headers=server_key,
        )
        assert evaluate.status_code == 200, evaluate.text
    finally:
        _write_row(db_session, "DELETE FROM feature_flags WHERE key = :k", k=flag_key)


# --- R4: the cleaning is this route's alone ----------------------------------------


@pytest.mark.regression
def test_tracking_assign_stores_the_context_it_is_sent(
    client, db_session, developer, key
):
    experiment = _running(client, developer, _host())
    visitor = _visitor()
    context = {
        "plan": "pro",
        "email": "someone@example.test",
        "web_device": "mobile",
        "web_country": "FR",
        "web_utm_campaign": "a@b.c",
    }
    response = client.post(
        "/api/v1/tracking/assign",
        json={
            "experiment_key": experiment["key"],
            "user_id": visitor,
            "context": context,
        },
        headers=key,
    )
    assert response.status_code == 200, response.text
    (exposure,) = _exposures(db_session, experiment["id"], visitor)
    assert exposure.event_metadata == context


# --- V57 (server): no raw referrer or query ----------------------------------------


@pytest.mark.regression
def test_no_raw_referrer_landing_query_or_click_id_is_stored(
    client, db_session, developer, key
):
    host = _host()
    experiment = _running(client, developer, host)
    visitor = _visitor()
    _ask(
        client,
        key,
        host,
        user_id=visitor,
        context={
            "web_referrer": "https://www.google.com/search?q=cv-secret-7731",
            "web_landing_path": "/a/b/page?email=a%40b.c&gclid=XYZ",
            "web_gclid": "XYZ",
            "web_utm_source": "news",
            "web_utm_medium": "email",
            "web_utm_campaign": "a@b.c",
        },
    )
    (exposure,) = _exposures(db_session, experiment["id"], visitor)
    assert exposure.event_metadata == {
        "web_utm_source": "news",
        "web_utm_medium": "email",
    }
    stored = str(exposure.event_metadata)
    for canary in ("cv-secret-7731", "gclid", "a%40b.c", "a@b.c", host):
        assert canary not in stored, canary


# --- V45a: a rules segment on a website detail ---------------------------------------


@pytest.mark.regression
def test_a_rules_segment_on_a_website_detail_matches_exactly(
    client, db_session, developer, key
):
    segment = client.post(
        "/api/v1/segments",
        json={"name": f"{PREFIX}phones-{uuid.uuid4().hex[:6]}", "rules": _phone_only()},
        headers=auth(developer),
    )
    assert segment.status_code == 201, segment.text
    in_segment = {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {
                        "attribute": "segment",
                        "operator": "in_segment",
                        "value": segment.json()["id"],
                    }
                ],
            }
        ],
    }
    host = _host()
    experiment = _running(client, developer, host, targeting=in_segment)
    answers = {}
    for device in ("phone", "tablet", "desktop", None, "Phone"):
        visitor = _visitor()
        context = {} if device is None else {"web_device": device}
        (entry,) = _ask(client, key, host, user_id=visitor, context=context).json()[
            "experiments"
        ]
        answers[device] = (entry["assigned"], entry["reason"])
    assert answers == {
        "phone": (True, "assigned"),
        "tablet": (False, "targeting"),
        "desktop": (False, "targeting"),
        None: (False, "targeting"),
        "Phone": (False, "targeting"),
    }
    assert _assignments(db_session, experiment["id"]) == 1


# --- V56 (server): no visitor IP address is stored -----------------------------------


def _client_address_count(db_session, address: str) -> int:
    """How many values of every text and JSON column of the schema hold
    *address*."""
    session = _session(db_session)
    try:
        columns = session.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = :schema AND data_type IN "
                "('text', 'character varying', 'json', 'jsonb')"
            ),
            {"schema": SCHEMA},
        ).all()
        assert len(columns) > 50, "the scan sees the schema"
        total = 0
        for table, column in columns:
            total += session.execute(
                text(
                    f'SELECT count(*) FROM "{SCHEMA}"."{table}" '
                    f'WHERE "{column}"::text LIKE :needle'
                ),
                {"needle": f"%{address}%"},
            ).scalar_one()
        return total
    finally:
        session.close()


@pytest.mark.regression
def test_no_visitor_ip_address_is_stored(client, db_session, developer, key):
    """TestClient's client address is ``testclient``: no column gains it."""
    host = _host()
    experiment = _running(client, developer, host)
    address = "testclient"
    before = _client_address_count(db_session, address)
    visitor = _visitor()
    _ask(client, key, host, user_id=visitor, context={"web_device": "phone"})
    goal = client.post(
        "/api/v1/tracking/batch",
        json={
            "events": [
                {
                    "event_type": "conversion",
                    "event_name": METRIC_EVENT,
                    "user_id": visitor,
                    "experiment_key": experiment["key"],
                    "properties": {"web_device": "phone"},
                }
            ]
        },
        headers=key,
    )
    assert goal.status_code in (200, 201), goal.text
    report = client.post(
        "/api/v1/tracking/errors",
        json={
            "experiment_key": experiment["key"],
            "error_type": "visual_change_not_applied",
            "user_id": visitor,
            "message": "not found",
        },
        headers=key,
    )
    assert report.status_code in (200, 201), report.text
    assert len(_exposures(db_session, experiment["id"], visitor)) == 1
    assert _client_address_count(db_session, address) == before


# --- V24: results reuse the normal view ----------------------------------------------


def _counts(client, user, experiment_id: str) -> Dict[str, Any]:
    response = client.get(f"{EXPERIMENTS}/{experiment_id}/results", headers=auth(user))
    assert response.status_code == 200, response.text
    (metric,) = response.json()["metrics"]
    return {
        v["variant_name"]: (v["sample_size"], v["conversions"])
        for v in metric["variants"]
    }


@pytest.mark.regression
def test_results_reuse_the_normal_view(client, db_session, developer, key):
    host = _host()
    visual = _running(client, developer, host, control=50)
    code = client.post(
        f"{EXPERIMENTS}/",
        json={
            "name": f"{PREFIX}code-{uuid.uuid4().hex[:6]}",
            "experiment_type": "a_b",
            "variants": [
                {"name": "Control", "is_control": True, "traffic_allocation": 50},
                {"name": "B", "is_control": False, "traffic_allocation": 50},
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
        headers=auth(developer),
    )
    assert code.status_code == 201, code.text
    code = _start(client, developer, code.json())
    visitors = [_visitor() for _ in range(12)]
    for index, visitor in enumerate(visitors):
        _ask(client, key, host, user_id=visitor)
        assigned = client.post(
            "/api/v1/tracking/assign",
            json={"experiment_key": code["key"], "user_id": visitor},
            headers=key,
        )
        assert assigned.status_code == 200, assigned.text
        if index % 3 == 0:
            for experiment in (visual, code):
                tracked = client.post(
                    "/api/v1/tracking/track",
                    json={
                        "event_type": "conversion",
                        "event_name": METRIC_EVENT,
                        "user_id": visitor,
                        "experiment_key": experiment["key"],
                    },
                    headers=key,
                )
                assert tracked.status_code in (200, 201), tracked.text
    visual_counts = _counts(client, developer, visual["id"])
    code_counts = _counts(client, developer, code["id"])
    assert sum(n for n, _ in visual_counts.values()) == 12
    assert sum(c for _, c in visual_counts.values()) == 4
    assert sum(n for n, _ in code_counts.values()) == 12
    assert sum(c for _, c in code_counts.values()) == 4


# --- V106: the cost of one answer ------------------------------------------------------


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


def _segment_running(client, developer, host: str) -> Dict[str, Any]:
    segment = client.post(
        "/api/v1/segments",
        json={"name": f"{PREFIX}seg-{uuid.uuid4().hex[:8]}", "rules": _phone_only()},
        headers=auth(developer),
    )
    assert segment.status_code == 201, segment.text
    rules = {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {
                        "attribute": "segment",
                        "operator": "in_segment",
                        "value": segment.json()["id"],
                    }
                ],
            }
        ],
    }
    return _running(client, developer, host, targeting=rules)


#: Statements for one page answer to a new visitor, at 1 and at 10 matching
#: experiments, each targeting a rules segment, measured on PostgreSQL: each
#: further experiment costs 13 more (136 = 19 + 9 x 13), and an extra
#: statement per experiment moves both.
STATEMENTS_AT_1 = 19
STATEMENTS_AT_10 = 136


@pytest.mark.regression
def test_the_cost_of_one_answer_is_pinned(client, db_session, developer, key):
    engine = db_session.get_bind()
    counts = {}
    for size in (1, 10):
        host = _host()
        made = [_segment_running(client, developer, host) for _ in range(size)]
        _ask(client, key, _host())  # the key's first use writes its last-used time
        with _Statements(engine) as statements:
            body = _ask(client, key, host, context={"web_device": "phone"}).json()
        assert len(body["experiments"]) == size
        assert all(e["assigned"] for e in body["experiments"])
        counts[size] = statements.count
        if size == 10:
            for _view in range(2):
                visitor = _visitor()
                for _ in range(3):
                    _ask(
                        client,
                        key,
                        host,
                        user_id=visitor,
                        context={"web_device": "phone"},
                    )
                exposures = sum(
                    len(_exposures(db_session, e["id"], visitor)) for e in made
                )
                assert exposures == 30
    assert counts == {1: STATEMENTS_AT_1, 10: STATEMENTS_AT_10}, counts
