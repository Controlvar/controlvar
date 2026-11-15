"""Who may read an experiment's results, its exports and its live stream.

The results routes (``/api/v1/results/*``, post-stratification and FDR
correction), the single-experiment report export, the bandit status route, the
real-time counters and the live results stream admit a caller by the rule
``GET /api/v1/experiments/{id}`` uses: experiment READ. The experiment and
variant exports need experiment LIST and the flag export feature-flag LIST, as
their list pages do, and the overview needs both. The interaction routes admit
the ANALYST, DEVELOPER and ADMIN roles.

Every cell is a real request: real ``User`` rows, a real local JWT from
``create_local_access_token``, and no dependency override but ``deps.get_db``
(and, for the counters, the counter service, which is a fake). The caller with
no base role has ``users.role`` set to NULL by raw SQL, and the stored NULL is
asserted: the ORM writes VIEWER for a role of None. Every cell gets its own
experiment, created through the API and then given its state by SQL.

``FOUR_ROLES`` pins what a superuser and a non-superuser of each shipped role
get on each route, as a literal table of the real statuses. Every shipped role
holds READ and LIST on experiments and on feature flags, so none of those
answers moves.
"""

from __future__ import annotations

import importlib.util
import json
import os
import uuid
from unittest.mock import patch

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.websocket_results import WS_AUTH_SUBPROTOCOL
from backend.app.core.config import settings
from backend.app.core.permissions import ROLE_PERMISSIONS, Action, ResourceType
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.user import User, UserRole
from backend.tests.smoke.test_experiment_routes_classified import (
    _http_routes,
    _websocket_paths,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

SCHEMA = "test_experimentation"
CACHE_DB = 15
SNAPSHOT = "backend.app.services.results_streaming_service.ResultsStreamingService.get_live_snapshot"

CANNOT_VIEW = "You don't have permission to view experiments"
CANNOT_LIST_EXPERIMENTS = "You don't have permission to list experiments"
CANNOT_LIST_FLAGS = "You don't have permission to list feature flags"
INTERACTIONS_ROLES = "Interaction analysis needs the ANALYST, DEVELOPER or ADMIN role."
NOT_SUPERUSER = "Not enough permissions"
WS_UNAUTHORIZED = 4401
WS_FORBIDDEN = 4403

# --- callers ------------------------------------------------------------------

#: column -> (role, superuser). ``no_role`` is stored with users.role NULL.
CALLERS = {
    "superuser": (UserRole.ADMIN, True),
    "admin": (UserRole.ADMIN, False),
    "developer": (UserRole.DEVELOPER, False),
    "analyst": (UserRole.ANALYST, False),
    "viewer": (UserRole.VIEWER, False),
    "no_role": (None, False),
}
SHIPPED = ["superuser", "admin", "developer", "analyst", "viewer"]

# --- routes -------------------------------------------------------------------

R = "/api/v1/results/{experiment_id}"
POST_STRAT_BODY = {"stratum_cols": ["country"], "metric_col": "metric_value"}
FDR_BODY = {"p_values": {"conversion": 0.01, "revenue": 0.2}}

#: name -> (method, path as the application declares it, body)
ROUTES = {
    "results": ("GET", R, None),
    "daily": ("GET", f"{R}/daily", None),
    "sample_size": ("GET", f"{R}/sample-size", None),
    "sequential": ("GET", f"{R}/sequential", None),
    "cuped": ("GET", f"{R}/cuped", None),
    "bayesian": ("GET", f"{R}/bayesian", None),
    "balanced_groups": ("GET", f"{R}/balanced-groups", None),
    "post_stratification": ("POST", f"{R}/post-stratification", POST_STRAT_BODY),
    "fdr_correction": ("POST", f"{R}/fdr-correction", FDR_BODY),
    "export_experiments": ("GET", "/api/v1/export/experiments", None),
    "export_variants": ("GET", "/api/v1/export/variants", None),
    "export_feature_flags": ("GET", "/api/v1/export/feature-flags", None),
    "export_overview": ("GET", "/api/v1/export/reports/overview", None),
    "export_report": (
        "GET",
        "/api/v1/export/reports/experiments/{experiment_id}",
        None,
    ),
    "bandit": ("GET", "/api/v1/bandit/{experiment_id}", None),
    "interaction_scan": ("GET", "/api/v1/interactions/scan", None),
    "interaction_pair": ("GET", "/api/v1/interactions/{exp_a_id}/{exp_b_id}", None),
    "stream": ("WS", "/api/v1/ws/experiments/{experiment_id}/results", None),
}
#: Added by the full profile.
MODULE_ROUTES = {"counters": ("GET", "/api/v1/counters/{experiment_id}", None)}

RESULTS_ROUTES = [
    "results",
    "daily",
    "sample_size",
    "sequential",
    "cuped",
    "bayesian",
    "balanced_groups",
    "post_stratification",
    "fdr_correction",
]
#: The 9 results routes, and the results route with a breakdown.
BREAKDOWN = "results_breakdown"
NO_ROLE_RESULTS_CELLS = [*RESULTS_ROUTES, BREAKDOWN]

LIST_EXPORTS = [
    "export_experiments",
    "export_variants",
    "export_feature_flags",
    "export_overview",
]
INTERACTIONS = ["interaction_scan", "interaction_pair"]

# --- what the shipped roles get -----------------------------------------------

#: Post-stratification answers 501 to everyone it admits: it is not available.
POST_STRAT_UNAVAILABLE = 501
#: The balanced-groups route answers 409 to everyone it admits: the
#: experiment every cell reads balances nothing.
NOT_BALANCED_ANSWER = 409


def _everyone(answer):
    return dict.fromkeys(SHIPPED, answer)


#: route -> caller -> the real answer: an HTTP status, or for the stream
#: "snapshot" (the first frame is a results snapshot) or a close code.
FOUR_ROLES = {
    "results": _everyone(200),
    "daily": _everyone(200),
    "sample_size": _everyone(200),
    "sequential": _everyone(200),
    "cuped": _everyone(200),
    "bayesian": _everyone(200),
    "balanced_groups": _everyone(NOT_BALANCED_ANSWER),
    "post_stratification": _everyone(POST_STRAT_UNAVAILABLE),
    "fdr_correction": _everyone(200),
    "export_experiments": _everyone(200),
    "export_variants": _everyone(200),
    "export_feature_flags": _everyone(200),
    "export_overview": _everyone(200),
    "export_report": _everyone(200),
    "bandit": _everyone(200),
    "interaction_scan": {**_everyone(200), "viewer": 403},
    "interaction_pair": {**_everyone(200), "viewer": 403},
    "stream": _everyone("snapshot"),
}
MODULE_FOUR_ROLES = {"counters": _everyone(200)}

#: route -> (answer, detail) for the caller with no base role.
NO_ROLE = {
    **dict.fromkeys(NO_ROLE_RESULTS_CELLS, (403, CANNOT_VIEW)),
    "export_experiments": (403, CANNOT_LIST_EXPERIMENTS),
    "export_variants": (403, CANNOT_LIST_EXPERIMENTS),
    "export_feature_flags": (403, CANNOT_LIST_FLAGS),
    "export_overview": (403, CANNOT_LIST_EXPERIMENTS),
    "export_report": (403, CANNOT_VIEW),
    "bandit": (403, CANNOT_VIEW),
    "interaction_scan": (403, INTERACTIONS_ROLES),
    "interaction_pair": (403, INTERACTIONS_ROLES),
    "stream": (WS_FORBIDDEN, None),
    "counters": (403, CANNOT_VIEW),
}


def _full_profile() -> bool:
    return importlib.util.find_spec("modules") is not None


def _routes():
    routes = dict(ROUTES)
    if _full_profile():
        routes.update(MODULE_ROUTES)
    return routes


def _four_roles():
    table = dict(FOUR_ROLES)
    if _full_profile():
        table.update(MODULE_FOUR_ROLES)
    return table


FOUR_ROLE_CELLS = [
    (route, caller) for route in [*FOUR_ROLES, *MODULE_FOUR_ROLES] for caller in SHIPPED
]


def _cell_marks(route):
    return [pytest.mark.modules] if route in MODULE_ROUTES else []


def _params(cells):
    return [
        pytest.param(route, caller, id=f"{route}-{caller}", marks=_cell_marks(route))
        for route, caller in cells
    ]


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_sign_in(monkeypatch):
    """The local JWT path with the development sign-in off, and no cache
    except where a test turns it on."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


def _session(engine):
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.execute(text(f"SET search_path TO {SCHEMA}"))
    return session


@pytest.fixture(scope="module")
def people(test_db):
    """One real user per caller, plus the DEVELOPER who creates every
    experiment through the API."""
    session = _session(test_db)
    suffix = uuid.uuid4().hex[:8]

    def make(name, role, is_superuser):
        user = User(
            username=f"readrule_{name}_{suffix}",
            email=f"readrule_{name}_{suffix}@read-rule.test",
            full_name="Experiment Read Rule",
            hashed_password="unused: these users sign in by token only",
            is_active=True,
            is_superuser=is_superuser,
            role=role or UserRole.VIEWER,
        )
        session.add(user)
        return user

    users = {name: make(name, role, su) for name, (role, su) in CALLERS.items()}
    users["creator"] = make("creator", UserRole.DEVELOPER, False)
    session.commit()

    # No base role: written by raw SQL, since the ORM stores VIEWER for None.
    session.execute(
        text("UPDATE users SET role = NULL WHERE id = :id"),
        {"id": users["no_role"].id},
    )
    session.commit()
    stored = session.execute(
        text("SELECT role FROM users WHERE id = :id"), {"id": users["no_role"].id}
    ).scalar_one()
    assert stored is None, f"the caller with no base role is stored as {stored!r}"
    session.refresh(users["no_role"])
    assert users["no_role"].role is None
    try:
        yield users
    finally:
        session.close()


def _db_override(db_session):
    engine = db_session.get_bind()

    def override_get_db():
        session = _session(engine)
        try:
            yield session
        finally:
            session.close()

    return override_get_db


@pytest.fixture
def client(db_session):
    app.dependency_overrides[deps.get_db] = _db_override(db_session)
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


class FakeCounters:
    """The counter service, counting what it is asked. It answers with the
    route's own response model."""

    def __init__(self, model):
        self.model = model
        self.calls = []

    def get_experiment_counters(self, experiment_id):
        self.calls.append(experiment_id)
        return self.model(
            experiment_id=experiment_id,
            total_assignments=3,
            total_events=2,
            total_conversions=1,
            variants=[],
        )


def _served_route(method, path):
    """The route the application serves at *path*, as FastAPI resolved it."""
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            continue
        for ctx in contexts() if callable(contexts) else contexts:
            if ctx.path == path and method in (getattr(ctx, "methods", None) or ()):
                return ctx
    raise LookupError(f"{method} {path} is not served")


@pytest.fixture
def counters():
    """The fake counter service in the full profile; None in the core one.

    The service is found on the served counters route (its ``svc``
    dependency), so this core file names no module."""
    if not _full_profile():
        yield None
        return
    route = _served_route(*MODULE_ROUTES["counters"][:2])
    (provider,) = [d.call for d in route.dependant.dependencies if d.name == "svc"]
    fake = FakeCounters(route.response_model)
    app.dependency_overrides[provider] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(provider, None)


@pytest.fixture
def redis_handle(monkeypatch):
    """The Redis the results cache uses, database 15, emptied."""
    monkeypatch.setattr(settings, "REDIS_DB", CACHE_DB)
    monkeypatch.setattr(settings, "REDIS_PASSWORD", None)
    monkeypatch.setattr(settings, "REDIS_SSL", False)
    handle = redis.Redis(
        host=str(settings.REDIS_HOST),
        port=int(settings.REDIS_PORT),
        db=CACHE_DB,
        decode_responses=True,
        socket_connect_timeout=2,
    )
    try:
        handle.ping()
    except redis.RedisError as exc:
        message = f"no Redis at {settings.REDIS_HOST}:{settings.REDIS_PORT}: {exc}"
        if os.environ.get("EXPERIMENTLY_REQUIRE_REDIS") == "1":
            pytest.fail(message + " but EXPERIMENTLY_REQUIRE_REDIS=1")
        pytest.skip(message)
    handle.flushdb()
    yield handle
    handle.flushdb()
    handle.close()


# --- helpers ------------------------------------------------------------------


def _token(user):
    return create_local_access_token(user)


def _auth(user):
    return {"Authorization": f"Bearer {_token(user)}"}


def _payload():
    return {
        "name": f"readrule {uuid.uuid4().hex[:8]}",
        "description": "One experiment per cell",
        "hypothesis": "The route admits exactly the callers it should",
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


def _experiment(client, db_session, people):
    """Created through the API by the creator, then given by SQL a state in
    which every route answers: completed, sequential testing on and a bandit
    allocation, so /sequential and the bandit route do not answer 404."""
    response = client.post(
        "/api/v1/experiments/", json=_payload(), headers=_auth(people["creator"])
    )
    assert response.status_code == 201, response.text
    experiment_id = response.json()["id"]
    row = db_session.get(Experiment, uuid.UUID(experiment_id))
    row.status = ExperimentStatus.COMPLETED
    row.sequential_testing_enabled = True
    row.sequential_testing_method = "msprt"
    row.optimization_type = "thompson_sampling"
    db_session.commit()
    return experiment_id


def _url(route, experiment_id, other_id=None):
    path = _routes()[route if route != BREAKDOWN else "results"][1]
    path = path.replace("{experiment_id}", experiment_id)
    path = path.replace("{exp_a_id}", experiment_id)
    path = path.replace("{exp_b_id}", other_id or str(uuid.uuid4()))
    if route == BREAKDOWN:
        path += "?breakdown=country"
    return path


def _stream(client, url, user):
    """The first message of the stream: "snapshot", or the close code."""
    protocols = [WS_AUTH_SUBPROTOCOL, _token(user)] if user is not None else None
    with patch(SNAPSHOT, return_value={"event": "results_update", "variants": []}):
        with client.websocket_connect(url, subprotocols=protocols) as ws:
            message = ws.receive()
    if message["type"] == "websocket.close":
        return message["code"]
    assert json.loads(message["text"])["event"] == "results_update", message
    return "snapshot"


def _ask(client, db_session, people, route, user, experiment_id=None):
    """(answer, detail) when *user* asks (None: no token)."""
    if experiment_id is None:
        experiment_id = _experiment(client, db_session, people)
    other_id = None
    if route == "interaction_pair":
        other_id = _experiment(client, db_session, people)
    url = _url(route, experiment_id, other_id)
    if route == "stream":
        return _stream(client, url, user), None
    method = _routes()[route if route != BREAKDOWN else "results"][0]
    body = _routes()[route if route != BREAKDOWN else "results"][2]
    headers = _auth(user) if user is not None else {}
    response = client.request(method, url, json=body, headers=headers)
    detail = None
    if response.status_code >= 400:
        detail = response.json().get("detail")
    return response.status_code, detail


def _live_read_routes():
    """The routes this file covers, as the running application serves them."""
    http = set(_http_routes(app))
    covered = {
        (m, p)
        for m, p in http
        if p.startswith(
            ("/api/v1/results/", "/api/v1/export/", "/api/v1/interactions/")
        )
    }
    covered.discard(("POST", f"{R}/invalidate-cache"))
    covered |= http & {
        ("GET", "/api/v1/bandit/{experiment_id}"),
        ("GET", "/api/v1/counters/{experiment_id}"),
    }
    covered |= {
        ("WS", p)
        for p in _websocket_paths(app.router)
        if p.startswith("/api/v1/ws/experiments/")
    }
    return covered


# --- the table is the application's -------------------------------------------


def test_the_development_sign_in_is_off():
    """Every cell below goes through the real sign-in: with the development
    sign-in on, every caller would be admitted and the refusals would fail."""
    assert settings.DEV_AUTH_BYPASS is False
    assert deps.dev_auth_bypass_active() is False


def test_the_table_is_the_live_application_routes():
    """18 routes in the core profile and 19 in the full one, by 5 callers:
    90 and 95 answers. A route added under these prefixes, or one of them
    mounted somewhere else, fails here until it is in the table."""
    declared = {(method, path) for method, path, _ in _routes().values()}
    assert _live_read_routes() == declared
    expected_routes = 19 if _full_profile() else 18
    assert len(_four_roles()) == len(_routes()) == expected_routes
    answers = sum(len(row) for row in _four_roles().values())
    assert answers == expected_routes * len(SHIPPED)
    assert all(list(row) == SHIPPED for row in _four_roles().values())
    assert set(NO_ROLE) == set(_routes()) | {BREAKDOWN} | set(MODULE_ROUTES)


# --- the four shipped roles keep their answers ----------------------------------


@pytest.mark.parametrize(("route", "caller"), _params(FOUR_ROLE_CELLS))
def test_every_shipped_role_keeps_its_answer_on_each_route(
    client, db_session, people, counters, route, caller
):
    answer, detail = _ask(client, db_session, people, route, people[caller])
    assert answer == _four_roles()[route][caller], detail
    if answer == 403:
        assert detail == INTERACTIONS_ROLES


# --- no base role ---------------------------------------------------------------


@pytest.mark.parametrize("route", NO_ROLE_RESULTS_CELLS)
def test_no_base_role_is_refused_on_results_routes(client, db_session, people, route):
    answer, detail = _ask(client, db_session, people, route, people["no_role"])
    assert (answer, detail) == NO_ROLE[route]


@pytest.mark.parametrize(
    "route",
    [
        "export_report",
        "bandit",
        pytest.param("counters", marks=pytest.mark.modules),
    ],
)
def test_no_base_role_is_refused_on_the_route_level_reads(
    client, db_session, people, counters, route
):
    answer, detail = _ask(client, db_session, people, route, people["no_role"])
    assert (answer, detail) == NO_ROLE[route]


@pytest.mark.modules
def test_the_counters_are_not_read_for_a_refused_caller(
    client, db_session, people, counters
):
    """The check runs before the counter service is asked: zero calls when
    refused, one when admitted."""
    experiment_id = _experiment(client, db_session, people)
    url = _url("counters", experiment_id)

    refused = client.get(url, headers=_auth(people["no_role"]))
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == CANNOT_VIEW
    assert counters.calls == []

    admitted = client.get(url, headers=_auth(people["viewer"]))
    assert admitted.status_code == 200, admitted.text
    assert counters.calls == [experiment_id]


#: case -> (method, path, body); "{id}" is an experiment that exists.
PRECEDENCE = {
    "unknown_experiment": ("GET", f"/api/v1/results/{uuid.UUID(int=7)}", None),
    "unknown_experiment_fdr": (
        "POST",
        f"/api/v1/results/{uuid.UUID(int=7)}/fdr-correction",
        FDR_BODY,
    ),
    "empty_body": ("POST", "/api/v1/results/{id}/fdr-correction", {}),
    "malformed_id": ("GET", "/api/v1/results/not-a-uuid/daily", None),
    "unknown_bandit": ("GET", f"/api/v1/bandit/{uuid.UUID(int=7)}", None),
    "unknown_report": (
        "GET",
        f"/api/v1/export/reports/experiments/{uuid.UUID(int=7)}",
        None,
    ),
}


@pytest.mark.parametrize("case", list(PRECEDENCE))
def test_refusal_precedes_not_found_and_validation(client, db_session, people, case):
    """The refusal comes before the 404 of an unknown experiment and the 422
    of a malformed id or body."""
    method, path, body = PRECEDENCE[case]
    if "{id}" in path:
        path = path.replace("{id}", _experiment(client, db_session, people))
    response = client.request(method, path, json=body, headers=_auth(people["no_role"]))
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == CANNOT_VIEW


def test_no_token_is_still_answered_401_first(client, db_session, people):
    experiment_id = _experiment(client, db_session, people)
    response = client.get(_url("results", experiment_id))
    assert response.status_code == 401, response.text


@pytest.mark.parametrize("route", ["results", BREAKDOWN], ids=["plain", "breakdown"])
def test_refusal_when_the_answer_is_already_cached(
    redis_handle, db_session, people, monkeypatch, route
):
    """The superuser reads first, so the answer is in Redis before the caller
    with no base role asks; the caller is still refused."""
    monkeypatch.setattr(settings, "CACHE_ENABLED", True)
    app.dependency_overrides[deps.get_db] = _db_override(db_session)
    try:
        with TestClient(app, raise_server_exceptions=False) as cached:
            experiment_id = _experiment(cached, db_session, people)
            url = _url(route, experiment_id)
            primed = cached.get(url, headers=_auth(people["superuser"]))
            assert primed.status_code == 200, primed.text
            keys = list(redis_handle.scan_iter(match=f"results:{experiment_id}:*"))
            assert keys, (
                "the first read wrote nothing to Redis: the cache is not on, and "
                "this cell would pass whatever the order of the check and the "
                "cache read"
            )
            if route == BREAKDOWN:
                assert any(key.endswith(":country") for key in keys), keys

            response = cached.get(url, headers=_auth(people["no_role"]))
    finally:
        app.dependency_overrides.pop(deps.get_db, None)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == CANNOT_VIEW


@pytest.mark.parametrize("caller", [*SHIPPED, "no_role"])
def test_invalidate_cache_stays_with_superusers(client, db_session, people, caller):
    """A control: only a superuser clears the cache. The caller with no base
    role is refused by the read rule first, the shipped roles by the
    superuser rule."""
    experiment_id = _experiment(client, db_session, people)
    response = client.post(
        f"/api/v1/results/{experiment_id}/invalidate-cache",
        headers=_auth(people[caller]),
    )
    if caller == "superuser":
        assert response.status_code == 200, response.text
        return
    assert response.status_code == 403, response.text
    expected = CANNOT_VIEW if caller == "no_role" else NOT_SUPERUSER
    assert response.json()["detail"] == expected


# --- exports follow the list pages --------------------------------------------

EXPORTS = [*LIST_EXPORTS, "export_report"]

#: caller -> (the ANALYST role's experiment and feature-flag actions,
#: route -> answer)
LIST_CALLERS = {
    "experiment_list_only": (
        [Action.LIST],
        [],
        {
            "export_experiments": (200, None),
            "export_variants": (200, None),
            "export_feature_flags": (403, CANNOT_LIST_FLAGS),
            "export_overview": (403, CANNOT_LIST_FLAGS),
            "export_report": (403, CANNOT_VIEW),
        },
    ),
    "feature_flag_list_only": (
        [],
        [Action.LIST],
        {
            "export_experiments": (403, CANNOT_LIST_EXPERIMENTS),
            "export_variants": (403, CANNOT_LIST_EXPERIMENTS),
            "export_feature_flags": (200, None),
            "export_overview": (403, CANNOT_LIST_EXPERIMENTS),
            "export_report": (403, CANNOT_VIEW),
        },
    ),
}
LIST_CELLS = [(caller, route) for caller in LIST_CALLERS for route in EXPORTS]


@pytest.mark.parametrize(
    ("caller", "route"), LIST_CELLS, ids=[f"{c}-{r}" for c, r in LIST_CELLS]
)
def test_export_routes_follow_the_list_page_rule(
    client, db_session, people, monkeypatch, caller, route
):
    """An ANALYST whose role holds LIST on one resource only: each export
    asks for the permission its list page asks for."""
    experiment_actions, flag_actions, expected = LIST_CALLERS[caller]
    monkeypatch.setitem(
        ROLE_PERMISSIONS[UserRole.ANALYST], ResourceType.EXPERIMENT, experiment_actions
    )
    monkeypatch.setitem(
        ROLE_PERMISSIONS[UserRole.ANALYST], ResourceType.FEATURE_FLAG, flag_actions
    )
    answer = _ask(client, db_session, people, route, people["analyst"])
    assert answer == expected[route]


@pytest.mark.parametrize("route", LIST_EXPORTS)
def test_no_base_role_is_refused_the_list_exports(client, db_session, people, route):
    answer = _ask(client, db_session, people, route, people["no_role"])
    assert answer == NO_ROLE[route]


# --- interactions name their roles --------------------------------------------

INTERACTION_CELLS = [
    (route, caller) for route in INTERACTIONS for caller in ("no_role", "viewer")
]


@pytest.mark.parametrize(
    ("route", "caller"),
    INTERACTION_CELLS,
    ids=[f"{r}-{c}" for r, c in INTERACTION_CELLS],
)
def test_interaction_routes_admit_only_named_roles(
    client, db_session, people, route, caller
):
    """The ANALYST, DEVELOPER and ADMIN roles and superusers are admitted (the
    four-role table); a VIEWER and a caller with no base role are not."""
    answer = _ask(client, db_session, people, route, people[caller])
    assert answer == (403, INTERACTIONS_ROLES)


# --- the live results stream ----------------------------------------------------


def test_stream_closes_with_forbidden_code_for_no_base_role(client, db_session, people):
    """Closed with 4403 after the handshake, with nothing registered; the
    superuser on the same experiment gets a snapshot."""
    from backend.app.api.v1.endpoints.websocket_results import manager

    assert deps.dev_auth_bypass_active() is False
    experiment_id = _experiment(client, db_session, people)
    url = _url("stream", experiment_id)

    assert _stream(client, url, people["no_role"]) == WS_FORBIDDEN
    assert manager.subscriber_count(experiment_id) == 0
    assert _stream(client, url, people["superuser"]) == "snapshot"


def test_stream_closes_4401_without_a_token(client, db_session, people):
    assert deps.dev_auth_bypass_active() is False
    experiment_id = _experiment(client, db_session, people)
    assert _stream(client, _url("stream", experiment_id), None) == WS_UNAUTHORIZED


def test_stream_closes_when_the_read_check_itself_fails(client, db_session, people):
    """An error that is not a refusal, raised by the read check, closes the
    socket 4401 and sends no snapshot: the check fails closed."""
    assert deps.dev_auth_bypass_active() is False
    experiment_id = _experiment(client, db_session, people)
    url = _url("stream", experiment_id)
    protocols = [WS_AUTH_SUBPROTOCOL, _token(people["superuser"])]

    with (
        patch.object(
            deps, "get_experiment_read_access", side_effect=RuntimeError("boom")
        ),
        patch(SNAPSHOT, return_value={"event": "results_update"}) as snapshot,
    ):
        with client.websocket_connect(url, subprotocols=protocols) as ws:
            message = ws.receive()

    assert message["type"] == "websocket.close"
    assert message["code"] == WS_UNAUTHORIZED
    snapshot.assert_not_called()


def test_stream_snapshot_for_a_reader(client, db_session, people):
    """A VIEWER, the narrowest shipped role, still gets the first snapshot."""
    assert deps.dev_auth_bypass_active() is False
    experiment_id = _experiment(client, db_session, people)
    assert (
        _stream(client, _url("stream", experiment_id), people["viewer"]) == "snapshot"
    )


# --- READ is what decides -------------------------------------------------------

READ_ROUTES = [*RESULTS_ROUTES, "export_report", "bandit", "stream"]
WITHOUT_READ = {
    **dict.fromkeys(READ_ROUTES, 403),
    "stream": WS_FORBIDDEN,
    **dict.fromkeys([*LIST_EXPORTS, *INTERACTIONS], 200),
}
WITHOUT_READ_CELLS = [*WITHOUT_READ, "counters"]


@pytest.mark.parametrize(
    "route",
    [pytest.param(route, marks=_cell_marks(route)) for route in WITHOUT_READ_CELLS],
)
def test_without_read_the_read_routes_refuse(
    client, db_session, people, counters, monkeypatch, route
):
    """An ANALYST whose role keeps LIST but has had READ on experiments taken
    out. Every shipped role holds READ, so this is the column that tells a
    READ check apart from a LIST check or from none: refused on every READ
    route, admitted on the list exports and the interaction routes."""
    monkeypatch.setitem(
        ROLE_PERMISSIONS[UserRole.ANALYST], ResourceType.EXPERIMENT, [Action.LIST]
    )
    answer, detail = _ask(client, db_session, people, route, people["analyst"])
    expected = WITHOUT_READ.get(route, 403)
    assert answer == expected, detail
    if answer == 403:
        assert detail == CANNOT_VIEW


# --- a grant of experiment (read) -----------------------------------------------

GRANT_ROUTES = [*RESULTS_ROUTES, "export_report", "bandit", "stream", "counters"]


def _grant_experiment_read(db_session, user):
    """An activated custom role holding experiment (read), assigned to
    *user*, written as the modules' own RBAC tests write one."""
    role_id = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO custom_roles (id, name, description, is_system_role, "
            "permissions, created_at, updated_at) VALUES (:id, :name, NULL, false, "
            "CAST(:perms AS jsonb), now(), now())"
        ),
        {
            "id": role_id,
            "name": f"readrule-{uuid.uuid4().hex[:10]}",
            "perms": json.dumps([{"resource": "experiment", "actions": ["read"]}]),
        },
    )
    db_session.execute(
        text(
            "INSERT INTO user_custom_roles (id, user_id, role_id, assigned_at, "
            "activated_at, created_at, updated_at) VALUES (:id, :user, :role, now(), "
            "now(), now(), now())"
        ),
        {"id": uuid.uuid4(), "user": user.id, "role": role_id},
    )
    db_session.commit()


@pytest.fixture
def granted(test_db):
    """A second caller with no base role, who holds experiment (read) through
    a custom role; the rows are removed after."""
    session = _session(test_db)
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"readrule_granted_{suffix}",
        email=f"readrule_granted_{suffix}@read-rule.test",
        full_name="Experiment Read Rule",
        hashed_password="unused: these users sign in by token only",
        is_active=True,
        is_superuser=False,
        role=UserRole.VIEWER,
    )
    session.add(user)
    session.commit()
    session.execute(
        text("UPDATE users SET role = NULL WHERE id = :id"), {"id": user.id}
    )
    session.commit()
    session.refresh(user)
    assert user.role is None
    _grant_experiment_read(session, user)
    try:
        yield user
    finally:
        session.execute(
            text("DELETE FROM user_custom_roles WHERE user_id = :id"), {"id": user.id}
        )
        session.execute(text("DELETE FROM custom_roles WHERE name LIKE 'readrule-%'"))
        session.commit()
        session.close()


@pytest.mark.modules
@pytest.mark.parametrize("route", GRANT_ROUTES)
def test_a_grant_of_experiment_read_admits_no_base_role(
    client, db_session, people, counters, granted, route
):
    """A positive control, in the full profile (the core profile has no
    grants): the rule is experiment READ, wherever it comes from."""
    answer, detail = _ask(client, db_session, people, route, granted)
    assert answer == _four_roles()[route]["analyst"], detail
