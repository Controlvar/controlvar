"""Every API-key route is classified for linked ids (IA-S9, #449).

``KEY_ROUTES`` (and ``MODULE_KEY_ROUTES`` in the full profile) is the exact
set of routes that take an API key, pinned against the application by
``test_api_key_routes_shape.py``. Each is here in exactly one class:

* ``RESOLVES``: the route assigns or records for a user in an experiment and
  answers a linked id as its person (``AssignmentService.assign_user`` or the
  tracking routes' ``_holder_at_receipt``, and D5's assignments list);
* ``NOT_USER_SCOPED``: the route answers no user's assignment or event for an
  experiment (the ruleset, flag evaluation counts, client errors, the visual
  preview, the split URL routes table);
* ``FLAGS_NOT_ALIASED``: flag evaluation, which stays ``md5(user_id:flag_key)``
  of the id sent;
* ``LINKS``: the route that writes a link, ``POST /api/v1/tracking/alias``.

A new key route fails ``test_every_key_route_is_classified_once`` until it is
put in a class here.
"""

from __future__ import annotations

import pytest

from backend.tests.smoke.test_api_key_routes_shape import (
    KEY_ROUTES,
    MODULE_KEY_ROUTES,
    _full_profile,
)

pytestmark = [pytest.mark.smoke]

RESOLVES = {
    ("POST", "/api/v1/tracking/assign"),
    ("POST", "/api/v1/tracking/assign/batch"),
    ("POST", "/api/v1/tracking/track"),
    ("POST", "/api/v1/tracking/batch"),
    ("POST", "/api/v1/tracking/events"),
    ("GET", "/api/v1/tracking/assignments/{user_id}"),
    ("POST", "/api/v1/tracking/visual"),
}
MODULE_RESOLVES = {("POST", "/api/v1/tracking/split-url/assign")}

NOT_USER_SCOPED = {
    ("GET", "/api/v1/sdk/ruleset"),
    ("POST", "/api/v1/tracking/evaluations"),
    ("POST", "/api/v1/tracking/errors"),
    ("POST", "/api/v1/tracking/errors/batch"),
    ("POST", "/api/v1/tracking/visual/preview"),
}
MODULE_NOT_USER_SCOPED = {("GET", "/api/v1/tracking/split-url/routes")}

FLAGS_NOT_ALIASED = {
    ("GET", "/api/v1/feature-flags/evaluate/{flag_key}"),
    ("POST", "/api/v1/feature-flags/evaluate/{flag_key}"),
    ("GET", "/api/v1/feature-flags/user/{user_id}"),
}

LINKS = {("POST", "/api/v1/tracking/alias")}


def _classes():
    resolves, not_scoped = set(RESOLVES), set(NOT_USER_SCOPED)
    if _full_profile():
        resolves |= MODULE_RESOLVES
        not_scoped |= MODULE_NOT_USER_SCOPED
    return {
        "RESOLVES": resolves,
        "NOT_USER_SCOPED": not_scoped,
        "FLAGS_NOT_ALIASED": set(FLAGS_NOT_ALIASED),
        "LINKS": set(LINKS),
    }


def _key_routes():
    routes = set(KEY_ROUTES)
    if _full_profile():
        routes |= MODULE_KEY_ROUTES
    return routes


def unclassified(routes, classes):
    """Routes in no class, and routes in more than one."""
    seen = [route for members in classes.values() for route in members]
    twice = sorted({r for r in seen if seen.count(r) > 1})
    return sorted(routes - set(seen)), twice


def test_every_key_route_is_classified_once():
    missing, twice = unclassified(_key_routes(), _classes())
    assert missing == [], f"unclassified key routes for aliasing: {missing}"
    assert twice == [], f"key routes in two classes: {twice}"
    stale = sorted(set().union(*_classes().values()) - _key_routes())
    assert stale == [], f"classified routes that take no key: {stale}"


def test_the_classes_have_the_counts_of_this_profile():
    counts = {name: len(members) for name, members in _classes().items()}
    if _full_profile():
        assert counts == {
            "RESOLVES": 8,
            "NOT_USER_SCOPED": 6,
            "FLAGS_NOT_ALIASED": 3,
            "LINKS": 1,
        }
        assert len(_key_routes()) == 18
    else:
        assert counts == {
            "RESOLVES": 7,
            "NOT_USER_SCOPED": 5,
            "FLAGS_NOT_ALIASED": 3,
            "LINKS": 1,
        }
        assert len(_key_routes()) == 16


def test_the_check_names_a_route_left_out():
    """The checker is not blind: a route in no class is named."""
    planted = ("POST", "/api/v1/tracking/planted")
    missing, _ = unclassified(_key_routes() | {planted}, _classes())
    assert missing == [planted]
