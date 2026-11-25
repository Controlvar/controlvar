"""Split URL routing takes a production key only (#2011).

Both routes refuse a staging or development key with the core's one 403 and
write nothing, for a running split URL experiment and every way the assign
route has of answering: a redirect, a page that is not the entry, and a
crawler. Keys are real rows and ``deps.get_api_key`` is not replaced, so the
core dependency the routes use (``key_environment.require_production_key``)
runs as it is served. The same requests with a production key are served, and
record the visitor.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict

import pytest

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.user import UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    local_sign_in,
    make_user,
)
from backend.tests.integration.key_environments import environment_key

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

ROUTES = "/api/v1/tracking/split-url/routes"
ASSIGN = "/api/v1/tracking/split-url/assign"
DETAIL = (
    "This API key belongs to the {environment} environment. Experiments and event "
    "tracking run in production only; use a production key."
)
BROWSER = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"
)
CRAWLER = "Mozilla/5.0 (compatible; Googlebot/2.1)"

#: What each visit asks: the reason a production key is answered, and whether
#: the visitor is recorded.
VISITS = {
    "entry": ({"path": "/checkout", "user_agent": BROWSER}, "assigned", True),
    "not-the-entry": ({"path": "/other", "user_agent": BROWSER}, "not_entry", False),
    "crawler": ({"path": "/checkout", "user_agent": CRAWLER}, "bot", False),
}


@pytest.fixture(autouse=True)
def _local(monkeypatch):
    local_sign_in(monkeypatch)
    assert deps.get_api_key not in app.dependency_overrides


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def keys(db_session) -> Dict[str, Dict[str, str]]:
    owner = make_user(db_session, UserRole.DEVELOPER, tag="spliturlenv")
    return {
        environment: environment_key(db_session, owner, environment)
        for environment in ("production", "staging", "development")
    }


def _recorded(db_session, experiment, user_id: str) -> tuple:
    db_session.expire_all()
    return tuple(
        db_session.query(model)
        .filter(model.experiment_id == experiment.id, model.user_id == user_id)
        .count()
        for model in (Assignment, Event)
    )


def _body(experiment, user_id: str, visit: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "experiment_key": experiment.key,
        "user_id": user_id,
        "host": "e.com",
        **visit,
    }


@pytest.mark.parametrize("environment", ["staging", "development"])
@pytest.mark.parametrize("visit", sorted(VISITS))
def test_the_assign_route_refuses_another_environments_key_and_records_nothing(
    client, db_session, split_experiment, keys, visit, environment
):
    asked, reason, recorded = VISITS[visit]
    experiment = split_experiment(f"split-env-{uuid.uuid4().hex[:8]}")
    visitor = f"split-env-{uuid.uuid4().hex[:10]}"

    body = _body(experiment, visitor, asked)
    refused = client.post(ASSIGN, json=body, headers=keys[environment])
    rows = _recorded(db_session, experiment, visitor)
    assert rows == (0, 0), (
        f"{environment} key wrote {rows} on POST {ASSIGN} ({refused.status_code})"
    )
    assert refused.status_code == 403, (
        f"{environment} key got {refused.status_code} on POST {ASSIGN}: {refused.text}"
    )
    assert refused.json() == {"detail": DETAIL.format(environment=environment)}

    served = client.post(ASSIGN, json=body, headers=keys["production"])
    assert served.status_code == 200, served.text
    assert served.json()["reason"] == reason
    assert (_recorded(db_session, experiment, visitor) != (0, 0)) is recorded


@pytest.mark.parametrize("environment", ["staging", "development"])
def test_the_routes_table_refuses_another_environments_key(
    client, split_experiment, keys, environment
):
    experiment = split_experiment(f"split-env-{uuid.uuid4().hex[:8]}")

    refused = client.get(ROUTES, headers=keys[environment])
    assert refused.status_code == 403, (
        f"{environment} key got {refused.status_code} on GET {ROUTES}: {refused.text}"
    )
    assert refused.json() == {"detail": DETAIL.format(environment=environment)}
    assert "cache-control" not in refused.headers or (
        "max-age" not in refused.headers["cache-control"]
    )

    served = client.get(ROUTES, headers=keys["production"])
    assert served.status_code == 200, served.text
    assert [r["experiment_key"] for r in served.json()["routes"]] == [experiment.key]
