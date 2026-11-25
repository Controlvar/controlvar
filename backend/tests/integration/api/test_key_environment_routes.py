"""Which flag environment each API-key route answers for (#2011).

The routes are ``test_api_key_routes_shape.py``'s own set (``_expected()``: the
key routes, the MCP read routes and, when the modules import, the split URL
routes), imported, so a new key route is unclassified here until it is added.
Each belongs to exactly one class, and the number in each class is pinned:

* ENV_SCOPED: answers the presented key's environment;
* PRODUCTION_ONLY: 403 with one fixed detail for a staging or development key,
  and nothing written, in any table;
* STORED_NOT_COUNTED: the error reports, stored with the environment and
  counted in neither of the flag's safety numbers;
* ACCEPTED_ZERO: the local evaluation counts, 201 with nothing recorded and
  every flag listed with code ``environment``;
* ENV_AGNOSTIC: the ``mcp:read`` routes, which read production with any key.

One flag is on at 100% in production and off in staging; a second one is the
other way round. Every route is called with keys of both environments, so each
test sees that the target changed and the other environment did not. Keys are
real rows and ``deps.get_api_key`` is not replaced: a test that replaces it
answers production.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, NamedTuple, Optional, Tuple

import pytest
from sqlalchemy import text

from backend.app.api import deps, sdk_scope
from backend.app.main import app
from backend.app.models.base import Base
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog
from backend.app.models.user import User, UserRole
from backend.app.services.safety_service import SafetyService
from backend.tests.integration.api.route_helpers import (
    SCHEMA,
    api_client,
    local_sign_in,
    make_user,
)
from backend.tests.integration.identity_alias_world import World, no_active_holdout
from backend.tests.integration.key_environments import configure, environment_key
from backend.tests.smoke import test_api_key_routes_shape as shape

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

Route = Tuple[str, str]

ENV_SCOPED = {
    ("GET", "/api/v1/feature-flags/evaluate/{flag_key}"),
    ("POST", "/api/v1/feature-flags/evaluate/{flag_key}"),
    ("GET", "/api/v1/feature-flags/user/{user_id}"),
    ("GET", "/api/v1/sdk/ruleset"),
}
PRODUCTION_ONLY = {
    ("POST", "/api/v1/tracking/assign"),
    ("POST", "/api/v1/tracking/assign/batch"),
    ("POST", "/api/v1/tracking/alias"),
    ("POST", "/api/v1/tracking/track"),
    ("POST", "/api/v1/tracking/events"),
    ("POST", "/api/v1/tracking/batch"),
    ("GET", "/api/v1/tracking/assignments/{user_id}"),
    ("POST", "/api/v1/tracking/visual"),
    ("POST", "/api/v1/tracking/visual/preview"),
}
#: The full profile's, production only too.
PRODUCTION_ONLY_SPLIT_URL = {
    ("GET", "/api/v1/tracking/split-url/routes"),
    ("POST", "/api/v1/tracking/split-url/assign"),
}
STORED_NOT_COUNTED = {
    ("POST", "/api/v1/tracking/errors"),
    ("POST", "/api/v1/tracking/errors/batch"),
}
ACCEPTED_ZERO = {("POST", "/api/v1/tracking/evaluations")}
ENV_AGNOSTIC = {
    ("GET", "/api/v1/feature-flags/"),
    ("GET", "/api/v1/feature-flags"),
    ("GET", "/api/v1/experiments/"),
    ("GET", "/api/v1/experiments/{experiment_id}"),
    ("GET", "/api/v1/experiments/{experiment_id}/results"),
}

FULL = bool(shape._expected() & PRODUCTION_ONLY_SPLIT_URL)
if FULL:
    PRODUCTION_ONLY = PRODUCTION_ONLY | PRODUCTION_ONLY_SPLIT_URL

CLASSES = {
    "ENV_SCOPED": ENV_SCOPED,
    "PRODUCTION_ONLY": PRODUCTION_ONLY,
    "STORED_NOT_COUNTED": STORED_NOT_COUNTED,
    "ACCEPTED_ZERO": ACCEPTED_ZERO,
    "ENV_AGNOSTIC": ENV_AGNOSTIC,
}
COUNTS = {
    "ENV_SCOPED": 4,
    "PRODUCTION_ONLY": 11 if FULL else 9,
    "STORED_NOT_COUNTED": 2,
    "ACCEPTED_ZERO": 1,
    "ENV_AGNOSTIC": 5,
}

#: The 403, word for word.
DETAIL = (
    "This API key belongs to the {environment} environment. Experiments and event "
    "tracking run in production only; use a production key."
)

#: The ``sdk:ruleset`` routes; the rest take a key with no scopes. The two
#: visual routes refuse a key with any scope.
SERVER_KEY_ROUTES = {
    ("GET", "/api/v1/sdk/ruleset"),
    ("POST", "/api/v1/tracking/alias"),
    ("POST", "/api/v1/tracking/assign/batch"),
    ("POST", "/api/v1/tracking/evaluations"),
}

PURCHASE = "purchase"
CHANGES = {
    "version": 1,
    "changes": [{"op": "set_text", "selector": "#headline", "text": "Hello"}],
}


# ---------------------------------------------------------------------------
# The classes
# ---------------------------------------------------------------------------


def test_every_key_route_is_in_exactly_one_class_and_the_counts_are_exact():
    classified = [route for routes in CLASSES.values() for route in routes]
    unclassified = sorted(shape._expected() - set(classified))
    assert not unclassified, f"unclassified key routes: {unclassified}"
    gone = sorted(set(classified) - shape._expected())
    assert not gone, f"classified but not key routes: {gone}"
    assert len(classified) == len(set(classified)), "a route is in two classes"
    assert {name: len(routes) for name, routes in CLASSES.items()} == COUNTS


def test_the_walk_agrees_with_the_shape_tests_set():
    """Guard the guard: what is classified is what the application serves."""
    assert set(shape._key_routes(app)) == shape._expected()
    assert len(shape._expected()) == (23 if FULL else 21)


# ---------------------------------------------------------------------------
# One world: two flags, an experiment, and a key of each environment
# ---------------------------------------------------------------------------


class Made(NamedTuple):
    client: Any
    db: Any
    world: World
    owner: User
    on_in_production: FeatureFlag
    on_in_staging: FeatureFlag
    experiment: Any
    host: str
    keys: Dict[Tuple[str, Optional[str]], Dict[str, str]]

    def key(self, environment: str, route: Route) -> Dict[str, str]:
        scopes = "sdk:ruleset" if route in SERVER_KEY_ROUTES else None
        return self.keys[(environment, scopes)]


@pytest.fixture(autouse=True)
def _local(monkeypatch):
    local_sign_in(monkeypatch)
    assert deps.get_api_key not in app.dependency_overrides


@pytest.fixture
def made(db_session, make_experiment, make_feature_flag):
    owner = make_user(db_session, UserRole.DEVELOPER, tag="envkeys")
    on = make_feature_flag(
        key=f"env-prod-{uuid.uuid4().hex[:8]}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
    )
    off = make_feature_flag(
        key=f"env-stg-{uuid.uuid4().hex[:8]}",
        status=FeatureFlagStatus.INACTIVE,
        rollout_percentage=0,
    )
    configure(db_session, on, "staging", is_active=False, rollout_percentage=0)
    configure(db_session, off, "staging", is_active=True, rollout_percentage=100)
    keys = {
        (environment, scopes): environment_key(db_session, owner, environment, scopes)
        for environment in ("production", "staging", "development")
        for scopes in (None, "sdk:ruleset", "mcp:read")
    }
    world = World(db_session, make_experiment)
    host = f"shop-{uuid.uuid4().hex[:10]}.example.com"
    with no_active_holdout(db_session), api_client(db_session) as client:
        try:
            experiment = world.experiment("Key environments")
            page = {"match": "prefix", "url": f"https://{host}/"}
            experiment.visual_config = {
                "version": 1,
                "editor_url": f"https://{host}/",
                "pages": [page],
                "goals": [
                    {
                        "event_name": PURCHASE,
                        "kind": "click",
                        "selector": "#buy",
                        "page": page,
                    }
                ],
            }
            for variant in experiment.variants:
                if not variant.is_control:
                    variant.visual_changes = CHANGES
            db_session.commit()
            yield Made(
                client, db_session, world, owner, on, off, experiment, host, keys
            )
        finally:
            world.close()


# ---------------------------------------------------------------------------
# One request per route
# ---------------------------------------------------------------------------


def _windows() -> Dict[str, str]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "window_start": (now - timedelta(seconds=30)).isoformat(),
        "window_end": now.isoformat(),
    }


def _user(m: Made, prefix: str = "env") -> str:
    return m.world.id(prefix)


def _event(m: Made) -> Dict[str, Any]:
    return {
        "event_type": PURCHASE,
        "event_name": PURCHASE,
        "user_id": _user(m),
        "experiment_key": m.experiment.key,
    }


def _report(m: Made) -> Dict[str, Any]:
    return {
        "feature_flag_key": m.on_in_production.key,
        "error_type": "crash",
        "message": "raised while the flag was on",
    }


#: route -> (path, query, body) of a request a production key is served.
REQUESTS: Dict[Route, Callable[[Made], Tuple[str, Dict[str, Any], Any]]] = {
    ("GET", "/api/v1/feature-flags/evaluate/{flag_key}"): lambda m: (
        f"/api/v1/feature-flags/evaluate/{m.on_in_production.key}",
        {"user_id": "u-1"},
        None,
    ),
    ("POST", "/api/v1/feature-flags/evaluate/{flag_key}"): lambda m: (
        f"/api/v1/feature-flags/evaluate/{m.on_in_production.key}",
        {},
        {"user_id": "u-1", "context": {}},
    ),
    ("GET", "/api/v1/feature-flags/user/{user_id}"): lambda m: (
        "/api/v1/feature-flags/user/u-1",
        {},
        None,
    ),
    ("GET", "/api/v1/sdk/ruleset"): lambda m: ("/api/v1/sdk/ruleset", {}, None),
    ("POST", "/api/v1/tracking/assign"): lambda m: (
        "/api/v1/tracking/assign",
        {},
        {"experiment_key": m.experiment.key, "user_id": _user(m)},
    ),
    ("POST", "/api/v1/tracking/assign/batch"): lambda m: (
        "/api/v1/tracking/assign/batch",
        {},
        {"experiment_key": m.experiment.key, "users": [{"user_id": _user(m)}]},
    ),
    ("POST", "/api/v1/tracking/alias"): lambda m: (
        "/api/v1/tracking/alias",
        {},
        {"anonymous_id": _user(m, "anon"), "user_id": _user(m, "known")},
    ),
    ("POST", "/api/v1/tracking/track"): lambda m: (
        "/api/v1/tracking/track",
        {},
        _event(m),
    ),
    ("POST", "/api/v1/tracking/events"): lambda m: (
        "/api/v1/tracking/events",
        {},
        {
            "event_type": PURCHASE,
            "event_name": PURCHASE,
            "user_id": _user(m),
            "experiment_id": str(m.experiment.id),
        },
    ),
    ("POST", "/api/v1/tracking/batch"): lambda m: (
        "/api/v1/tracking/batch",
        {},
        {"events": [_event(m)]},
    ),
    ("GET", "/api/v1/tracking/assignments/{user_id}"): lambda m: (
        f"/api/v1/tracking/assignments/{_user(m)}",
        {},
        None,
    ),
    ("POST", "/api/v1/tracking/visual"): lambda m: (
        "/api/v1/tracking/visual",
        {},
        {"host": m.host, "path": "/", "user_id": _user(m), "format_version": 1},
    ),
    ("POST", "/api/v1/tracking/visual/preview"): lambda m: (
        "/api/v1/tracking/visual/preview",
        {},
        {"host": m.host, "path": "/", "token": "a" * 43},
    ),
    ("GET", "/api/v1/tracking/split-url/routes"): lambda m: (
        "/api/v1/tracking/split-url/routes",
        {},
        None,
    ),
    ("POST", "/api/v1/tracking/split-url/assign"): lambda m: (
        "/api/v1/tracking/split-url/assign",
        {},
        {
            "experiment_key": m.experiment.key,
            "user_id": _user(m),
            "host": m.host,
            "path": "/",
        },
    ),
    ("POST", "/api/v1/tracking/errors"): lambda m: (
        "/api/v1/tracking/errors",
        {},
        _report(m),
    ),
    ("POST", "/api/v1/tracking/errors/batch"): lambda m: (
        "/api/v1/tracking/errors/batch",
        {},
        {"errors": [_report(m)]},
    ),
    ("POST", "/api/v1/tracking/evaluations"): lambda m: (
        "/api/v1/tracking/evaluations",
        {},
        {
            "evaluations": [
                {
                    "flag_key": m.on_in_production.key,
                    "count": 40,
                    "enabled_count": 40,
                    **_windows(),
                },
                {
                    "flag_key": m.on_in_production.key,
                    "count": 2,
                    "enabled_count": 0,
                    **_windows(),
                },
                {
                    "flag_key": m.on_in_staging.key,
                    "count": 5,
                    "enabled_count": 5,
                    **_windows(),
                },
            ]
        },
    ),
    ("GET", "/api/v1/feature-flags/"): lambda m: (
        "/api/v1/feature-flags/",
        {"search": m.on_in_production.key},
        None,
    ),
    ("GET", "/api/v1/feature-flags"): lambda m: (
        "/api/v1/feature-flags",
        {"search": m.on_in_production.key},
        None,
    ),
    ("GET", "/api/v1/experiments/"): lambda m: (
        "/api/v1/experiments/",
        {"search": m.experiment.name},
        None,
    ),
    ("GET", "/api/v1/experiments/{experiment_id}"): lambda m: (
        f"/api/v1/experiments/{m.experiment.id}",
        {},
        None,
    ),
    ("GET", "/api/v1/experiments/{experiment_id}/results"): lambda m: (
        f"/api/v1/experiments/{m.experiment.id}/results",
        {},
        None,
    ),
}


def test_there_is_a_request_for_every_classified_route():
    expected = shape._expected() | PRODUCTION_ONLY_SPLIT_URL
    assert set(REQUESTS) == expected


def _send(m: Made, route: Route, headers: Dict[str, str]):
    path, query, body = REQUESTS[route](m)
    return m.client.request(
        route[0], path, params=query, json=body, headers=dict(headers)
    )


def _rows(m: Made) -> Dict[str, int]:
    """How many rows every table of the schema holds."""
    m.db.rollback()
    tables = sorted(table.name for table in Base.metadata.tables.values())
    assert len(tables) > 30, tables
    union = " UNION ALL ".join(
        f"SELECT '{name}', count(*) FROM {SCHEMA}.\"{name}\"" for name in tables
    )
    counts = dict(m.db.execute(text(union)).tuples().all())
    m.db.rollback()
    return counts


def _added(before: Dict[str, int], after: Dict[str, int]) -> Dict[str, int]:
    return {
        name: after[name] - before[name]
        for name in after
        if after[name] != before[name]
    }


def _label(route: Route) -> str:
    return f"{route[0]} {route[1]}"


# ---------------------------------------------------------------------------
# ENV_SCOPED
# ---------------------------------------------------------------------------


def _answer(m: Made, route: Route, headers: Dict[str, str], flag: FeatureFlag) -> list:
    """What *route* says of *flag*: ``[enabled, reason]``, as the evaluate
    routes answer; the ruleset and the bulk route in the same words."""
    method, template = route
    if template.endswith("/evaluate/{flag_key}"):
        path = f"/api/v1/feature-flags/evaluate/{flag.key}"
        if method == "GET":
            response = m.client.get(path, params={"user_id": "u-1"}, headers=headers)
        else:
            response = m.client.post(path, json={"user_id": "u-1"}, headers=headers)
        assert response.status_code == 200, response.text
        return [response.json()["enabled"], response.json()["reason"]]
    response = _send(m, route, headers)
    assert response.status_code == 200, response.text
    if template == "/api/v1/sdk/ruleset":
        entries = {entry["key"]: entry for entry in response.json()["flags"]}
        entry = entries[flag.key]
        if not entry["active"]:
            return [False, "inactive"]
        assert entry["rollout_percentage"] == 100, entry
        return [True, "rollout"]
    flags = response.json()
    if flag.key not in flags:
        return [False, "inactive"]
    return [flags[flag.key], "rollout"]


ON, OFF = [True, "rollout"], [False, "inactive"]


@pytest.mark.regression
@pytest.mark.parametrize("route", sorted(ENV_SCOPED), ids=_label)
def test_a_scoped_route_answers_the_presented_keys_environment(made, route):
    answers = {
        environment: {
            "on_in_production": _answer(
                made, route, made.key(environment, route), made.on_in_production
            ),
            "on_in_staging": _answer(
                made, route, made.key(environment, route), made.on_in_staging
            ),
        }
        for environment in ("production", "staging", "development")
    }
    expected = {
        "production": {"on_in_production": ON, "on_in_staging": OFF},
        "staging": {"on_in_production": OFF, "on_in_staging": ON},
        # Nothing configures either flag in development.
        "development": {"on_in_production": OFF, "on_in_staging": OFF},
    }
    crossed = [
        f"{_label(route)}: {environment} key answered production's "
        f"{answers[environment][flag]}"
        for environment in ("staging", "development")
        for flag in expected[environment]
        if answers[environment][flag] != expected[environment][flag]
        and answers[environment][flag] == answers["production"][flag]
    ]
    assert not crossed, crossed
    assert answers == expected


@pytest.mark.parametrize("route", sorted(ENV_SCOPED), ids=_label)
def test_a_scoped_route_records_nothing_for_a_key_outside_production(made, route):
    """No ``raw_metrics`` row, nor any other; production's evaluation records
    its own, so the count is not vacuous."""
    for environment in ("staging", "development"):
        before = _rows(made)
        _answer(made, route, made.key(environment, route), made.on_in_production)
        assert _added(before, _rows(made)) == {}, (_label(route), environment)
    before = _rows(made)
    _answer(made, route, made.key("production", route), made.on_in_production)
    added = _added(before, _rows(made))
    if route == ("GET", "/api/v1/sdk/ruleset"):
        assert added == {}
    else:
        assert set(added) == {"raw_metrics"}, added


# ---------------------------------------------------------------------------
# PRODUCTION_ONLY
# ---------------------------------------------------------------------------

#: What a production key's request answers, and whether it writes. The split
#: URL route answers 400 here: the experiment is not a split URL one (the
#: modules' own test drives a real one).
PRODUCTION_STATUS = {
    ("POST", "/api/v1/tracking/visual/preview"): (404, False),
    ("GET", "/api/v1/tracking/assignments/{user_id}"): (200, False),
    ("GET", "/api/v1/tracking/split-url/routes"): (200, False),
    ("POST", "/api/v1/tracking/split-url/assign"): (400, False),
}


@pytest.mark.regression
@pytest.mark.parametrize("environment", ["staging", "development"])
@pytest.mark.parametrize("route", sorted(PRODUCTION_ONLY), ids=_label)
def test_a_production_only_route_refuses_another_key_and_writes_nothing(
    made, route, environment
):
    before = _rows(made)
    refused = _send(made, route, made.key(environment, route))
    added = _added(before, _rows(made))
    assert not added, f"{environment} key wrote {added} on {_label(route)}"
    assert refused.status_code == 403, (
        f"{_label(route)}: {environment} key got {refused.status_code}: {refused.text}"
    )
    assert refused.json() == {"detail": DETAIL.format(environment=environment)}

    # The same request with a production key is served, and writes when the
    # route writes: the refusal above is the environment's, not the request's.
    served = _send(made, route, made.key("production", route))
    added = _added(before, _rows(made))
    status, writes = PRODUCTION_STATUS.get(route, (200, True))
    assert served.status_code == status, (_label(route), served.text)
    assert bool(added) is writes, (_label(route), added)


def test_the_two_visual_routes_refuse_a_scoped_key_for_its_scope_first(made):
    """A key with a scope in a page is refused for the scope, whatever its
    environment, as before."""
    for route in (
        ("POST", "/api/v1/tracking/visual"),
        ("POST", "/api/v1/tracking/visual/preview"),
    ):
        refused = _send(made, route, made.keys[("staging", "sdk:ruleset")])
        assert refused.status_code == 403
        assert refused.json()["detail"].startswith("This API key has scopes")


@pytest.mark.parametrize(
    "route",
    [("POST", "/api/v1/tracking/alias"), ("POST", "/api/v1/tracking/assign/batch")],
    ids=_label,
)
def test_the_server_key_routes_refuse_for_the_scope_before_the_environment(made, route):
    """A staging key without ``sdk:ruleset`` gets the scope's 403, the same
    order as ``require_sdk_ruleset_key``'s own refusals."""
    refused = _send(made, route, made.keys[("staging", None)])
    assert refused.status_code == 403
    assert refused.json()["detail"].startswith("This API key does not have the")


def test_batch_assignment_refuses_the_environment_before_the_owners_experiments_rule(
    made, monkeypatch
):
    """With an owner's rule that refuses everyone, a staging key still gets the
    environment's 403 and the rule is not asked; a production key is asked it,
    and gets its text."""
    asked = []

    def refuses(db, api_key):
        asked.append(api_key.environment)
        return sdk_scope.OWNER_EXPERIMENT_DETAIL

    monkeypatch.setattr(sdk_scope, "batch_assign_refusal", refuses)
    route = ("POST", "/api/v1/tracking/assign/batch")
    refused = _send(made, route, made.key("staging", route))
    assert refused.status_code == 403
    assert refused.json() == {"detail": DETAIL.format(environment="staging")}
    assert asked == []

    refused = _send(made, route, made.key("production", route))
    assert refused.status_code == 403
    assert refused.json() == {"detail": sdk_scope.OWNER_EXPERIMENT_DETAIL}
    assert asked == ["production"]


# ---------------------------------------------------------------------------
# STORED_NOT_COUNTED
# ---------------------------------------------------------------------------


def _errors(m: Made) -> Tuple[int, int]:
    m.db.expire_all()
    metrics = SafetyService(m.db).get_error_metrics(m.db, m.on_in_production.id)
    return metrics["error_count"], metrics["uncounted_error_count"]


def _environments_stored(m: Made) -> list:
    m.db.expire_all()
    rows = (
        m.db.query(ErrorLog)
        .filter(ErrorLog.feature_flag_id == m.on_in_production.id)
        .order_by(ErrorLog.timestamp)
        .all()
    )
    return [
        (row.request_data["source"], row.request_data.get("environment"))
        for row in rows
    ]


@pytest.mark.regression
@pytest.mark.parametrize("environment", ["staging", "development"])
@pytest.mark.parametrize("scopes", [None, "sdk:ruleset"], ids=["client", "server"])
@pytest.mark.parametrize("route", sorted(STORED_NOT_COUNTED), ids=_label)
def test_an_error_report_of_another_environment_is_stored_and_counted_in_neither_set(
    made, route, scopes, environment
):
    source = "client" if scopes is None else "server"
    accepted = 201 if route[1].endswith("/errors") else 200
    before = _rows(made)
    sent = _send(made, route, made.keys[(environment, scopes)])
    assert sent.status_code == accepted, sent.text
    assert _added(before, _rows(made)) == {"error_logs": 1}
    assert _errors(made) == (0, 0)
    assert _environments_stored(made) == [(source, environment)]

    sent = _send(made, route, made.keys[("production", scopes)])
    assert sent.status_code == accepted, sent.text
    assert _errors(made) == ((1, 0) if source == "server" else (0, 1))
    assert _environments_stored(made) == [(source, environment), (source, None)]


# ---------------------------------------------------------------------------
# ACCEPTED_ZERO
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("environment", ["staging", "development"])
def test_local_evaluation_counts_of_another_environment_are_not_recorded(
    made, environment
):
    (route,) = ACCEPTED_ZERO
    before = _rows(made)
    answer = _send(made, route, made.key(environment, route))
    assert answer.status_code == 201, answer.text
    assert _added(before, _rows(made)) == {}
    message = (
        "Evaluation counts are recorded in production only; this API key belongs "
        f"to the {environment} environment"
    )
    # One entry per flag, the two for the same flag merged, sorted by key.
    assert answer.json() == {
        "accepted": 0,
        "errors": [
            {
                "flag_key": key,
                "code": "environment",
                "message": message,
                "requested": requested,
                "accepted": 0,
            }
            for key, requested in sorted(
                [(made.on_in_production.key, 42), (made.on_in_staging.key, 5)]
            )
        ],
    }

    served = _send(made, route, made.key("production", route))
    assert served.status_code == 201, served.text
    assert served.json() == {"accepted": 47, "errors": []}
    assert _added(before, _rows(made)) == {
        "raw_metrics": 2,
        "sdk_evaluation_flag_counts": 2,
        "sdk_evaluation_key_counts": 2,
    }


# ---------------------------------------------------------------------------
# ENV_AGNOSTIC
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", sorted(ENV_AGNOSTIC), ids=_label)
def test_an_mcp_read_route_reads_production_with_a_key_of_any_environment(made, route):
    """The MCP read routes are not scoped to an environment: a staging key
    with ``mcp:read`` reads what production is, which is why no request
    can make one."""
    answers = {}
    for environment in ("production", "staging", "development"):
        response = _send(made, route, made.keys[(environment, "mcp:read")])
        assert response.status_code == 200, (_label(route), response.text)
        answers[environment] = response.json()
    if route[1] == "/api/v1/experiments/{experiment_id}/results":
        # When it was worked out, not what it says.
        for answer in answers.values():
            for field in ("generated_at", "last_updated", "computed_at"):
                answer.pop(field, None)
    assert answers["staging"] == answers["production"]
    assert answers["development"] == answers["production"]
    if route[1].rstrip("/") == "/api/v1/feature-flags":
        (item,) = answers["staging"]["items"]
        assert (item["key"], item["rollout_percentage"]) == (
            made.on_in_production.key,
            100,
        )
        assert item["status"] == "active"
