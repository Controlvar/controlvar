"""The fixed parts of ``POST /api/v1/tracking/alias`` (beta, #449).

* The request model refuses, in the standard list-shaped 422, two equal ids
  (at the body), either field exactly ``anonymous`` or ``anon`` (at the
  field: the comparison is exact), and a missing, empty, over-255-character
  or unstorable field. Its messages name the fields, never the ids.
* Every reason ``identity_aliases.link`` can refuse a link for has one 409
  text, word for word; no text holds a placeholder for an id.
* The ids are in the body only: the path has no parameter (IA-K4), and the
  route is in the SDK rate-limit bucket.
* The route's key is ``require_sdk_ruleset_key`` itself, not batch
  assignment's ``require_batch_assign_key``.
* The handler answers a link's refusal with its 409 and any other failure
  with a fixed 500, neither carrying the exception's text; ``link`` runs once,
  with the ids as sent.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.api import sdk_scope
from backend.app.api.v1.endpoints import tracking
from backend.app.main import app as real_app
from backend.app.middleware.rate_limiter import (
    DEFAULT_SDK_RATE_LIMIT_PER_MINUTE,
    SDK_BUCKET,
    RateLimitMiddleware,
    SlidingWindowRateLimiter,
    resolve_rate_limit_bucket,
)
from backend.app.schemas.tracking import AliasRequest, AliasResponse
from backend.app.services import identity_aliases

pytestmark = [pytest.mark.unit, pytest.mark.regression]

PATH = "/api/v1/tracking/alias"

#: ruling OQ-5's texts, as literal strings: the route's own constants are
#: compared with a copy made here, not with themselves.
CONFLICT_TEXTS = {
    "anonymous_id_linked_to_another_user_id": (
        "That anonymous_id is already linked to a different user_id. An "
        "anonymous_id can be linked to one user_id."
    ),
    "anonymous_id_has_links": (
        "That anonymous_id already has other ids linked to it, so it cannot itself "
        "be linked. Links are one level deep; check that the two ids are not "
        "swapped."
    ),
    "user_id_is_linked_as_anonymous_id": (
        "That user_id is itself linked to another user_id, so nothing can be "
        "linked to it. Links are one level deep; link to the user_id it is linked "
        "to, or check that the two ids are not swapped."
    ),
    "user_id_at_cap": (
        "That user_id already has 100 anonymous_ids linked to it, the most "
        "allowed, so this anonymous_id was not linked."
    ),
}
EQUAL_IDS = "anonymous_id and user_id must differ."
PLACEHOLDER = (
    "must identify one visitor or customer: 'anonymous' and 'anon' are "
    "placeholders shared by many and cannot be linked"
)

A, U = "anon-7f3a", "known-19c2"


def _errors(body) -> list:
    with pytest.raises(ValidationError) as caught:
        AliasRequest(**body)
    return [(e["loc"], e["msg"]) for e in caught.value.errors()]


# ---------------------------------------------------------------------------
# The request model
# ---------------------------------------------------------------------------


def test_two_different_ids_are_accepted_as_sent():
    request = AliasRequest(anonymous_id=A, user_id=U)
    assert (request.anonymous_id, request.user_id) == (A, U)


def test_two_equal_ids_are_refused_at_the_body():
    assert _errors({"anonymous_id": A, "user_id": A}) == [((), EQUAL_IDS)]


def test_ids_are_compared_exactly():
    """Ids that differ only in case or white space are two ids."""
    assert AliasRequest(anonymous_id="Visitor-1", user_id="visitor-1")
    assert AliasRequest(anonymous_id="visitor-1 ", user_id="visitor-1")


@pytest.mark.parametrize("field", ["anonymous_id", "user_id"])
@pytest.mark.parametrize("placeholder", ["anonymous", "anon"])
def test_each_placeholder_is_refused_in_each_field(field, placeholder):
    body = {"anonymous_id": A, "user_id": U, field: placeholder}
    assert _errors(body) == [((field,), PLACEHOLDER)]


def test_both_fields_a_placeholder_is_two_errors_and_no_equal_ids_error():
    assert _errors({"anonymous_id": "anon", "user_id": "anon"}) == [
        (("anonymous_id",), PLACEHOLDER),
        (("user_id",), PLACEHOLDER),
    ]


@pytest.mark.parametrize("near", ["Anonymous", "ANON", "anon ", " anon", "anon1"])
def test_only_the_exact_placeholders_are_refused(near):
    assert AliasRequest(anonymous_id=near, user_id=U).anonymous_id == near
    assert AliasRequest(anonymous_id=A, user_id=near).user_id == near


@pytest.mark.parametrize("field", ["anonymous_id", "user_id"])
def test_a_missing_empty_or_too_long_field_is_refused_at_the_field(field):
    base = {"anonymous_id": A, "user_id": U}
    missing = {k: v for k, v in base.items() if k != field}
    for body in (missing, {**base, field: ""}, {**base, field: "x" * 256}):
        assert [loc for loc, _ in _errors(body)] == [(field,)], body
    assert len(getattr(AliasRequest(**{**base, field: "x" * 255}), field)) == 255


@pytest.mark.parametrize("field", ["anonymous_id", "user_id"])
@pytest.mark.parametrize("bad", ["\u0000", "\ud800"], ids=["nul", "lone-surrogate"])
def test_unstorable_text_is_refused_with_the_fields_name_only(field, bad):
    body = {"anonymous_id": A, "user_id": U, field: f"secret-{bad}"}
    errors = _errors(body)
    assert errors == [
        (
            (field,),
            f"Value error, {field} must not contain NUL or unpaired surrogate "
            "characters",
        )
    ]
    assert "secret" not in repr(errors)


def test_no_message_repeats_an_id():
    """The placeholder message names the two placeholders, never the other id."""
    cases = [
        ({"anonymous_id": "same-id-449", "user_id": "same-id-449"}, "same-id-449"),
        ({"anonymous_id": "anon", "user_id": "known-id-449"}, "known-id-449"),
        ({"anonymous_id": "anon-id-449", "user_id": "x" * 256}, "anon-id-449"),
    ]
    for body, sent in cases:
        for loc, msg in _errors(body):
            assert sent not in msg and sent not in repr(loc), (body, msg)


def test_the_answer_is_the_two_ids_and_created():
    assert set(AliasResponse.model_fields) == {"anonymous_id", "user_id", "created"}
    assert set(AliasRequest.model_fields) == {"anonymous_id", "user_id"}


# ---------------------------------------------------------------------------
# The 409 texts
# ---------------------------------------------------------------------------


def test_every_reason_a_link_is_refused_has_its_text():
    assert set(identity_aliases.CONFLICT_REASONS) == set(CONFLICT_TEXTS)
    assert tracking.ALIAS_CONFLICT_DETAILS == CONFLICT_TEXTS


def test_no_conflict_text_has_room_for_an_id():
    for text in tracking.ALIAS_CONFLICT_DETAILS.values():
        assert "{" not in text and "%" not in text, text


# ---------------------------------------------------------------------------
# The route: where it is, its key and its limit (IA-K4)
# ---------------------------------------------------------------------------


@pytest.fixture
def route() -> APIRoute:
    """The alias route, found by its handler's name, not by its path."""
    found = [
        ctx
        for r in real_app.routes
        for ctx in (
            r.effective_route_contexts()
            if callable(getattr(r, "effective_route_contexts", None))
            else getattr(r, "effective_route_contexts", None) or [r]
        )
        if getattr(ctx, "name", None) == "link_anonymous_id"
    ]
    assert len(found) == 1, found
    return found[0]


def test_the_route_is_where_the_limit_is():
    assert real_app.url_path_for("link_anonymous_id") == PATH


def test_the_ids_are_in_the_body_only(route):
    assert route.methods == {"POST"}
    params = [p.name for p in route.dependant.path_params]
    assert params == [], f"path parameters {params}"
    assert [p.name for p in route.dependant.query_params] == []
    assert [p.name for p in route.dependant.body_params] == ["request"]
    assert route.path == PATH


def test_the_route_is_in_the_sdk_rate_limit_bucket():
    assert resolve_rate_limit_bucket(PATH) == (
        SDK_BUCKET,
        (DEFAULT_SDK_RATE_LIMIT_PER_MINUTE, 60),
    )
    assert resolve_rate_limit_bucket(PATH, sdk_limit_per_minute=100_000) == (
        SDK_BUCKET,
        (100_000, 60),
    )


def test_over_the_sdk_limit_the_route_answers_429_before_it_runs():
    """The real middleware, at an SDK limit of 3 a minute (the application
    turns it off under test, so it is driven with ``enabled=True`` here, as
    ``test_assign_batch_contract.py`` does)."""
    calls = []
    routes = FastAPI()

    @routes.post(PATH)
    async def alias():
        calls.append(1)
        return {}

    middleware = RateLimitMiddleware(routes, enabled=True)
    middleware._limiter = SlidingWindowRateLimiter()
    middleware._sdk_limit = 3
    client = TestClient(middleware)
    answers = [client.post(PATH) for _ in range(4)]
    assert [a.status_code for a in answers] == [200, 200, 200, 429]
    assert answers[-1].json() == {
        "detail": "Too Many Requests. Please slow down and retry after a moment."
    }
    assert answers[-1].headers["Retry-After"] == "60"
    assert len(calls) == 3


def test_the_key_is_the_server_side_key_not_batch_assignments(route):
    calls = []

    def walk(dependant):
        for sub in dependant.dependencies:
            calls.append(sub.call)
            walk(sub)

    walk(route.dependant)
    assert sdk_scope.require_sdk_ruleset_key in calls
    assert sdk_scope.require_batch_assign_key not in calls
    direct = [sub.call for sub in route.dependant.dependencies]
    assert sdk_scope.require_sdk_ruleset_key in direct


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------


def _call(monkeypatch, outcome):
    """Run the handler with ``link`` answering *outcome* (a value, or an
    exception to raise); returns (result or HTTPException, link's calls)."""
    calls = []

    def fake_link(db, anonymous_id, user_id):
        calls.append((anonymous_id, user_id))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(tracking, "link", fake_link)
    request = AliasRequest(anonymous_id=A, user_id=U)
    try:
        result = asyncio.run(
            tracking.link_anonymous_id(request=request, db=MagicMock(), api_key=None)
        )
    except HTTPException as refused:
        result = refused
    return result, calls


@pytest.mark.parametrize("created", [True, False])
def test_a_link_answers_the_ids_as_sent(monkeypatch, created):
    result, calls = _call(monkeypatch, created)
    assert result == AliasResponse(anonymous_id=A, user_id=U, created=created)
    assert calls == [(A, U)]


@pytest.mark.parametrize("reason", sorted(CONFLICT_TEXTS))
def test_each_refusal_is_its_409(monkeypatch, reason):
    result, calls = _call(monkeypatch, identity_aliases.AliasConflict(reason))
    assert isinstance(result, HTTPException)
    assert (result.status_code, result.detail) == (409, CONFLICT_TEXTS[reason])
    assert calls == [(A, U)]


def test_any_other_failure_is_a_fixed_500(monkeypatch):
    failure = RuntimeError(f"(psycopg2) [parameters: {{'a': '{A}', 'u': '{U}'}}]")
    result, calls = _call(monkeypatch, failure)
    assert isinstance(result, HTTPException)
    assert result.status_code == 500
    assert result.detail.startswith("Could not link the two ids")
    assert A not in result.detail and U not in result.detail
    assert calls == [(A, U)]
