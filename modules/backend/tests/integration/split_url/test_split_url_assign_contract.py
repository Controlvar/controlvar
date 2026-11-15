"""``POST /api/v1/tracking/split-url/assign``'s contract (#393).

Locations as exact strings (``vectors.json``), the order of the answers and
their texts, the reasons, what the body refuses, the API key, and that the
route resolves every dependency ``/tracking/assign`` declares (it answers
through that route's own function).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Dict, Set

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment, ExperimentStatus, ExperimentType
from backend.app.schemas.tracking import VariantAssignmentResponse
from modules.backend.app.api.v1.endpoints.experiments_split_url import (
    NO_SPLIT_URL_CONFIG_DETAIL,
    NOT_SPLIT_URL_DETAIL,
    SPLIT_URL_CONFIG_NOT_VALID_DETAIL,
)
from modules.backend.app.api.v1.endpoints.split_url_routing import REASONS

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

ASSIGN = "/api/v1/tracking/split-url/assign"
ROUTES = "/api/v1/tracking/split-url/routes"
VECTORS = json.loads((Path(__file__).with_name("vectors.json")).read_text())
EVERYONE_TO_THE_VARIANT = {"Control": 0.0, "Treatment": 1.0}


def request_body(key: str, **overrides) -> Dict:
    return {
        "experiment_key": key,
        "user_id": f"visitor-{uuid.uuid4().hex[:10]}",
        "host": "e.com",
        "path": "/checkout",
        "query": "utm_source=x",
        **overrides,
    }


def counts(db_session, experiment) -> tuple:
    db_session.expire_all()
    return tuple(
        db_session.query(model).filter(model.experiment_id == experiment.id).count()
        for model in (Assignment, Event)
    )


@pytest.mark.parametrize("vector", VECTORS["location"], ids=lambda v: v["id"])
def test_the_location_is_exact(admin_client, split_experiment, db_session, vector):
    """V9: hosts, query strings and paths, as exact answers; a request that is
    not the entry is served and records nothing."""
    experiment = split_experiment(
        "split-vectors",
        [
            ("Control", True, 50, vector["control"]),
            ("Treatment", False, 50, vector["variant"]),
        ],
        optimization_type="thompson_sampling",
        bandit_weights=EVERYONE_TO_THE_VARIANT,
    )
    response = admin_client.post(
        ASSIGN,
        json=request_body(
            "split-vectors",
            host=vector["host"],
            path=vector["path"],
            query=vector["query"],
        ),
    )
    assert response.status_code == 200, response.text
    answer = response.json()
    if vector["expect"] == "not_entry":
        assert (answer["action"], answer["reason"], answer["location"]) == (
            "serve",
            "not_entry",
            None,
        ), vector["id"]
        assert (answer["variant_id"], answer["variant_name"]) == (None, None)
        assert counts(db_session, experiment) == (0, 0)
    else:
        assert (answer["action"], answer["location"]) == (
            "redirect",
            vector["expect"],
        ), vector["id"]


def test_an_unknown_key_answers_as_tracking_assign_does(admin_client):
    """V30: the 404's detail is /tracking/assign's, compared literally."""
    key = f"no-such-split-{uuid.uuid4().hex[:8]}"
    ours = admin_client.post(ASSIGN, json=request_body(key))
    theirs = admin_client.post(
        "/api/v1/tracking/assign", json={"experiment_key": key, "user_id": "u"}
    )
    assert (ours.status_code, theirs.status_code) == (404, 404)
    assert ours.json()["detail"] == theirs.json()["detail"]
    assert ours.json()["detail"] == f"No experiment has the key '{key}'."


def test_another_type_answers_the_preview_s_400(admin_client, split_experiment):
    split_experiment("split-ab", experiment_type=ExperimentType.A_B)
    response = admin_client.post(ASSIGN, json=request_body("split-ab"))
    assert (response.status_code, response.json()["detail"]) == (
        400,
        NOT_SPLIT_URL_DETAIL,
    )
    assert NOT_SPLIT_URL_DETAIL == "Experiment is not a split_url type"


def test_no_configuration_answers_the_preview_s_400(admin_client, split_experiment):
    split_experiment("split-no-config", split_url_config={})
    response = admin_client.post(ASSIGN, json=request_body("split-no-config"))
    assert (response.status_code, response.json()["detail"]) == (
        400,
        NO_SPLIT_URL_CONFIG_DETAIL,
    )
    assert (
        NO_SPLIT_URL_CONFIG_DETAIL == "split_url_config is not set for this experiment"
    )


LEGACY_CONFIGS = {
    # Names that are not the experiment's variants.
    "names": {
        "variants": [
            {"name": "A", "url": "https://e.com/checkout", "traffic_allocation": 50},
            {"name": "B", "url": "https://e.com/checkout-v2", "traffic_allocation": 50},
        ]
    },
    # A URL no rule accepts any longer.
    "url": {
        "variants": [
            {
                "name": "Control",
                "url": "ftp://e.com/checkout",
                "traffic_allocation": 50,
            },
            {
                "name": "Treatment",
                "url": "https://e.com/checkout-v2",
                "traffic_allocation": 50,
            },
        ]
    },
}


@pytest.mark.parametrize("legacy", sorted(LEGACY_CONFIGS))
def test_a_stored_configuration_that_breaks_the_rules_answers_409(
    admin_client, split_experiment, db_session, legacy
):
    """V7: a legacy stored row answers 409 from the route and the preview,
    before the entry is looked at, and records nothing."""
    experiment = split_experiment("split-legacy")
    db_session.execute(
        update(Experiment)
        .where(Experiment.id == experiment.id)
        .values(split_url_config=LEGACY_CONFIGS[legacy])
    )
    db_session.commit()
    for body in (request_body("split-legacy"), request_body("split-legacy", path="/x")):
        response = admin_client.post(ASSIGN, json=body)
        assert (response.status_code, response.json()["detail"]) == (
            409,
            SPLIT_URL_CONFIG_NOT_VALID_DETAIL,
        )
    preview = admin_client.get(
        f"/api/v1/experiments/{experiment.id}/split-url/preview",
        params={"user_id": "u"},
    )
    assert (preview.status_code, preview.json()["detail"]) == (
        409,
        SPLIT_URL_CONFIG_NOT_VALID_DETAIL,
    )
    assert counts(db_session, experiment) == (0, 0)


def test_not_entry_comes_before_the_status_and_the_bot(
    admin_client, split_experiment, db_session
):
    """V30: the order. Off the entry, a paused experiment and a crawler are
    both answered not_entry; on it, the status, then the crawler."""
    experiment = split_experiment("split-order", status=ExperimentStatus.PAUSED)
    off = admin_client.post(
        ASSIGN, json=request_body("split-order", path="/x", user_agent="Googlebot")
    ).json()
    assert off["reason"] == "not_entry"
    on = admin_client.post(
        ASSIGN, json=request_body("split-order", user_agent="Googlebot")
    ).json()
    assert (on["action"], on["reason"]) == ("serve", "paused")
    db_session.execute(
        update(Experiment)
        .where(Experiment.id == experiment.id)
        .values(status=ExperimentStatus.ACTIVE)
    )
    db_session.commit()
    on = admin_client.post(
        ASSIGN, json=request_body("split-order", user_agent="Googlebot")
    ).json()
    assert on["reason"] == "bot"


def test_the_reasons_are_tracking_assigns_and_two_more():
    """V31: the set the route answers from: every reason /tracking/assign
    describes, and not_entry and bot."""
    described = VariantAssignmentResponse.model_fields["reason"].description
    theirs = {"assigned", "holdout", "mutual_exclusion", "targeting"} | {
        status.value for status in ExperimentStatus if status != ExperimentStatus.ACTIVE
    }
    assert all(f" {reason}" in f" {described}" for reason in theirs)
    assert set(REASONS) == theirs | {"not_entry", "bot"}
    assert len(REASONS) == len(set(REASONS)) == 10


@pytest.mark.parametrize(
    "overrides, loc",
    [
        ({"user_id": "a\u0000b"}, ["body", "user_id"]),
        ({"user_id": "u" * 256}, ["body", "user_id"]),
        ({"user_id": ""}, ["body", "user_id"]),
        ({"host": None}, ["body", "host"]),
        ({"host": "e.com evil"}, ["body", "host"]),
        ({"host": "h" * 262}, ["body", "host"]),
        ({"host": "e.com\u0000"}, ["body", "host"]),
        ({"path": None}, ["body", "path"]),
        ({"path": "checkout"}, ["body", "path"]),
        ({"path": "/" + "p" * 2048}, ["body", "path"]),
        ({"query": "a=1#frag"}, ["body", "query"]),
        ({"query": "a=1 b"}, ["body", "query"]),
        ({"query": "q" * 8193}, ["body", "query"]),
        ({"user_agent": "u" * 1025}, ["body", "user_agent"]),
        ({"experiment_key": "k" * 101}, ["body", "experiment_key"]),
    ],
    ids=[
        "nul-user-id",
        "long-user-id",
        "empty-user-id",
        "missing-host",
        "space-in-host",
        "long-host",
        "nul-in-host",
        "missing-path",
        "relative-path",
        "long-path",
        "fragment-in-query",
        "space-in-query",
        "long-query",
        "long-user-agent",
        "long-key",
    ],
)
def test_the_body_refuses_what_tracking_assign_refuses(
    admin_client, split_experiment, db_session, overrides, loc
):
    """V40: a 422 at the field, with no row and no exposure."""
    experiment = split_experiment("split-body")
    body = request_body("split-body", **overrides)
    body = {k: v for k, v in body.items() if v is not None}
    response = admin_client.post(ASSIGN, json=body)
    assert response.status_code == 422, response.text
    assert loc in [error["loc"] for error in response.json()["detail"]]
    assert counts(db_session, experiment) == (0, 0)


@pytest.mark.parametrize(
    "method, path", [("GET", ROUTES), ("POST", ASSIGN)], ids=["routes", "assign"]
)
@pytest.mark.parametrize(
    "headers", [{}, {"X-API-Key": "not-a-key"}], ids=["missing", "unknown"]
)
def test_both_routes_need_an_api_key(admin_client, method, path, headers):
    """401 without a valid API key, before the body is looked at: the test
    database, and the real key check."""
    app.dependency_overrides.pop(deps.get_api_key)
    response = TestClient(app).request(
        method, path, json=request_body("split-key"), headers=headers
    )
    assert response.status_code == 401, response.text


def _dependency_calls(context) -> Set:
    found, stack = set(), list(context.dependant.dependencies)
    while stack:
        dependency = stack.pop()
        if dependency.call is not None:
            found.add(dependency.call)
        stack.extend(dependency.dependencies)
    return found


def _context(path: str):
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            continue
        contexts = contexts() if callable(contexts) else contexts
        for context in contexts:
            if context.path == path and "POST" in (context.methods or ()):
                return context
    raise AssertionError(f"no POST {path}")


def test_the_route_resolves_every_dependency_tracking_assign_declares():
    """It calls /tracking/assign's function with its own db and key, so it
    must resolve the same dependencies: any /tracking/assign declares and
    this route does not is named here."""
    theirs = _dependency_calls(_context("/api/v1/tracking/assign"))
    ours = _dependency_calls(_context(ASSIGN))
    assert theirs
    missing = sorted(
        getattr(call, "__qualname__", repr(call)) for call in theirs - ours
    )
    assert missing == []
