"""In demo mode, what a visitor does writes only an enumerated set of tables.

The demo data is seeded (``backend/scripts/seed_demo_data.py``'s users,
experiments with history, flags, audit entries and safety settings) and an
SDK key is made for the administrator, as the demo apps' keys are. Every table
in the schema is then fingerprinted: an md5 of its rows, each row as JSON with
the columns in :data:`VOLATILE_COLUMNS` removed. Then, with ``DEMO_MODE=true``:

* the demo viewer signs in and out through ``/auth/login`` and ``/auth/logout``;
* every GET route is called with the seeded ids, the viewer's token and the
  SDK key (each experiment for an ``{experiment_id}``);
* every refused route is called with a body: its OpenAPI example when it has
  one, else ``{}``; each must answer the demo refusal;
* the five SDK POSTs are called with valid bodies, and a flag is evaluated.

Afterwards the set of tables whose fingerprint changed must equal
:data:`GET_WRITE_ALLOWED` exactly, each with its reason. The test also holds
two positive controls: a results GET and the notification-preferences GET
answered 200, so the sweep reached the handlers that write.

``backend/tests/no_outbound_network.py`` guards this directory: a connection
to anything but loopback fails the test.
"""

from __future__ import annotations

import copy
import re
import uuid
from typing import Any, Dict, Iterator, List, Set, Tuple

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.demo_mode import REFUSAL_BODY, allowed_requests
from backend.app.core.security import hash_api_key
from backend.app.main import app
from backend.app.models.api_key import APIKey, generate_api_key
from backend.app.services.local_auth_service import login_attempt_tracker
from backend.scripts import seed_demo_data as demo_seed

pytestmark = pytest.mark.integration

SCHEMA = "test_experimentation"
PLACEHOLDER = "00000000-0000-4000-8000-000000000000"
VISITOR = "demo-visitor-p6"
VIEWER = ("viewer@demo.com", "Demo1234!")
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
REFUSED = (403, REFUSAL_BODY)

#: The tables a visitor's requests may change, and why.
GET_WRITE_ALLOWED: Dict[str, str] = {
    "analysis_snapshots": "a results GET keeps one snapshot per experiment, kind and day",
    "notification_preferences": "the preferences GET creates the reader's default row",
    "raw_metrics": "a flag evaluation records its outcome and latency",
    "audit_logs": "a sign-in records a user_login entry",
    "assignments": "POST /tracking/assign records the visitor's variant",
    "events": "POST /tracking/track and /tracking/batch record the visitor's events",
    "error_logs": "POST /tracking/errors and /tracking/errors/batch record client errors",
}

#: Columns left out of a table's fingerprint: they change on reads, by design.
#: Any other change to these tables -- a row added, removed or edited -- counts.
VOLATILE_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "api_keys": ("last_used_at",),  # stamped by every request an SDK key makes
}


def _fingerprints(db_session) -> Dict[str, str]:
    tables = db_session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = :schema AND table_type = 'BASE TABLE'"
        ),
        {"schema": SCHEMA},
    ).scalars()
    found = {}
    for table in sorted(tables):
        dropped = list(VOLATILE_COLUMNS.get(table, ()))
        found[table] = db_session.execute(
            text(
                f"SELECT md5(coalesce(string_agg(r, '' ORDER BY r), '')) FROM "
                f"(SELECT (to_jsonb(t) - CAST(:dropped AS text[]))::text AS r "
                f'FROM "{SCHEMA}"."{table}" t) s'
            ),
            {"dropped": dropped},
        ).scalar_one()
    db_session.commit()
    return found


def _routes() -> Set[Tuple[str, str]]:
    found: Set[Tuple[str, str]] = set()
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            for method in getattr(ctx, "methods", None) or ():
                found.add((method, ctx.path))
    return found


def _resolve(schema: Dict[str, Any], document: Dict[str, Any]) -> Dict[str, Any]:
    ref = schema.get("$ref")
    if ref:
        node: Any = document
        for part in ref.lstrip("#/").split("/"):
            node = node[part]
        return node
    return schema


def _example_body(method: str, path: str, document: Dict[str, Any]) -> Any:
    """The request body's OpenAPI example, or ``{}``."""
    operation = document.get("paths", {}).get(path, {}).get(method.lower(), {})
    content = operation.get("requestBody", {}).get("content", {})
    media = content.get("application/json")
    if not media:
        return {}
    if "example" in media:
        return media["example"]
    schema = _resolve(media.get("schema", {}), document)
    if "example" in schema:
        return schema["example"]
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:
        return examples[0]
    return {}


@pytest.fixture(autouse=True)
def _demo_install(monkeypatch):
    # No AWS region or profile, as on a demo install: the full profile's ETL
    # status GET builds a Glue client, which then fails without a connection
    # (a handled 500). Anything that does connect fails the test (the guard).
    for name in ("AWS_DEFAULT_REGION", "AWS_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    # The demo account published, as on a demo install.
    monkeypatch.setattr(settings, "DEMO_ACCOUNT_EMAIL", VIEWER[0])
    monkeypatch.setattr(settings, "DEMO_ACCOUNT_PASSWORD", VIEWER[1])
    login_attempt_tracker.clear()
    yield
    login_attempt_tracker.clear()


@pytest.fixture
def client(db_session) -> Iterator[TestClient]:
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    assert app.dependency_overrides == {}
    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


@pytest.fixture
def seeded(db_session) -> Dict[str, Any]:
    """The demo seed, and an SDK key owned by the administrator."""
    users = demo_seed.seed_users(db_session)
    admin = users["admin@demo.com"]
    experiments = [
        demo_seed.seed_homepage_hero_experiment(db_session, admin),
        demo_seed.seed_checkout_button_experiment(db_session, admin),
        demo_seed.seed_recommendation_mab_experiment(db_session, admin),
    ]
    demo_seed.backfill_experiment_keys(db_session)
    flags = demo_seed.seed_feature_flags(db_session, admin)
    demo_seed.seed_audit_logs(db_session, users, flags, experiments)
    demo_seed.seed_safety_settings(db_session)
    plaintext = generate_api_key()
    db_session.add(
        APIKey(
            user_id=admin.id,
            key=hash_api_key(plaintext),
            name=f"demo-storefront-{uuid.uuid4().hex[:8]}",
            scopes="read,write",
            is_active=True,
        )
    )
    db_session.commit()
    flag = next(iter(flags.values()))
    return {
        "viewer": users[VIEWER[0]],
        "experiments": experiments,
        "flag": flag,
        "api_key": plaintext,
    }


def _path_values(seeded: Dict[str, Any]) -> Dict[str, List[str]]:
    experiments = [str(e.id) for e in seeded["experiments"]]
    return {
        "experiment_id": experiments,
        "exp_a_id": experiments[:1],
        "exp_b_id": experiments[1:2],
        "entity_type": ["experiment"],
        "entity_id": experiments[:1],
        "user_id": [str(seeded["viewer"].id)],
        "flag_id": [str(seeded["flag"].id)],
        "feature_flag_id": [str(seeded["flag"].id)],
        "flag_key": [seeded["flag"].key],
        "segment_by": ["country"],
        "name": ["experiment_scheduler"],
        "standard": ["soc2"],
        "role_name": ["viewer"],
    }


def _urls(path: str, values: Dict[str, List[str]]) -> List[str]:
    names = re.findall(r"\{([^}]+)\}", path)
    urls = [path]
    for name in names:
        choices = values.get(name, [PLACEHOLDER])
        urls = [u.replace("{" + name + "}", c, 1) for u in urls for c in choices]
    return urls


def _answer(response) -> Tuple[int, Any]:
    try:
        return response.status_code, response.json() if response.content else None
    except ValueError:
        return response.status_code, response.text[:200]


def test_a_visitor_writes_only_the_enumerated_tables(client, db_session, seeded):
    before = _fingerprints(db_session)
    routes = _routes()
    allowed = allowed_requests(settings.API_V1_STR)
    document = copy.deepcopy(app.openapi())
    values = _path_values(seeded)

    login = client.post(
        "/api/v1/auth/login", json={"email": VIEWER[0], "password": VIEWER[1]}
    )
    assert login.status_code == 200, login.text
    headers = {
        "Authorization": f"Bearer {login.json()['access_token']}",
        "X-API-Key": seeded["api_key"],
    }

    answered: Dict[str, int] = {}
    for method, path in sorted(routes):
        if method != "GET":
            continue
        for url in _urls(path, values):
            response = client.get(url, params={"user_id": VISITOR}, headers=headers)
            answered[url] = response.status_code

    not_refused = {}
    for method, path in sorted(routes):
        if method not in MUTATING or (method, path) in allowed:
            continue
        for url in _urls(path, values)[:1]:
            body = _example_body(method, path, document)
            answer = _answer(client.request(method, url, json=body, headers=headers))
            if answer != REFUSED:
                not_refused[f"{method} {path}"] = answer

    experiment = seeded["experiments"][1]
    flag_key = seeded["flag"].key
    sdk = {"X-API-Key": seeded["api_key"]}
    event = {
        "event_type": "purchase",
        "user_id": VISITOR,
        "experiment_key": experiment.key,
        "value": 12.5,
    }
    error = {"error_type": "crash", "message": "demo", "feature_flag_key": flag_key}
    sdk_calls = [
        (
            "/api/v1/tracking/assign",
            {"experiment_key": experiment.key, "user_id": VISITOR},
        ),
        ("/api/v1/tracking/track", event),
        ("/api/v1/tracking/batch", {"events": [dict(event, event_type="page_view")]}),
        ("/api/v1/tracking/errors", error),
        ("/api/v1/tracking/errors/batch", {"errors": [error]}),
    ]
    sdk_answers = {
        url: client.post(url, json=body, headers=sdk).status_code
        for url, body in sdk_calls
    }
    evaluated = client.get(
        f"/api/v1/feature-flags/evaluate/{flag_key}",
        params={"user_id": VISITOR},
        headers=sdk,
    ).status_code
    logout = client.post("/api/v1/auth/logout", headers=headers).status_code

    after = _fingerprints(db_session)
    changed = sorted(t for t in after if after[t] != before.get(t))
    print("changed tables:", changed)
    print("sdk answers:", sdk_answers, "evaluate:", evaluated, "logout:", logout)
    print("GET answers by status:", sorted(set(answered.values())))

    assert not not_refused, f"refused routes answered otherwise: {not_refused}"
    assert all(200 <= s < 300 for s in sdk_answers.values()), sdk_answers
    assert evaluated == 200 and logout == 204
    first = str(seeded["experiments"][0].id)
    assert answered[f"/api/v1/results/{first}"] == 200
    assert answered["/api/v1/notifications/preferences"] == 200
    unexpected = sorted(set(changed) - set(GET_WRITE_ALLOWED))
    unchanged = sorted(set(GET_WRITE_ALLOWED) - set(changed))
    assert not unexpected, f"changed tables not allowed: {unexpected}"
    assert not unchanged, f"allowed tables no longer written: {unchanged}"
