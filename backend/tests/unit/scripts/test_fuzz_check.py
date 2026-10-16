"""scripts/fuzz_check.py decides an API fuzzing pass; these pin how.

The reach check keeps a fuzz run honest: Schemathesis exits 0 on a run whose
every request was refused (a bad token, a filter that selected the wrong
operations), so a pass is red unless every operation it selects was reached or
is listed, with a reason, in tests/fuzz/unreached.toml. The known-5xx list
keeps it useful: a pass is red on a 5xx that tests/fuzz/known-5xx.toml does not
list for it, keyed on the route that ANSWERED (the application's own router),
and on a listed entry that no longer fires. Fixture reports below are ndjson in
the shape Schemathesis 4.29 writes (``ScenarioFinished.recorder.interactions``),
cut down to the fields read.

Also pinned: the exclusion list (tests/fuzz/exclusions.toml) operation by
operation, the SDK pass's selection against the rate limiter's SDK routes, the
counts line and step summary as the only public output, the AWS variables of
the workflow and of `make fuzz` as one set, the install of the pinned closure,
and `make fuzz`'s refusals that need neither git nor Docker.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = REPO_ROOT / "scripts"
FULL_SNAPSHOT = REPO_ROOT / "docs" / "api" / "openapi-v1.full.json"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
FUZZ_DIR = REPO_ROOT / "tests" / "fuzz"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fuzz_check

DOCUMENT = {
    "openapi": "3.1.0",
    "paths": {
        "/api/v1/items": {
            "get": {"security": [{"OAuth2PasswordBearer": []}]},
            "post": {"security": [{"OAuth2PasswordBearer": []}]},
        },
        "/api/v1/items/{item_id}": {
            "delete": {"security": [{"OAuth2PasswordBearer": []}]},
        },
        "/api/v1/open": {"get": {}},
        "/api/v1/tracking/track": {"post": {"security": [{"APIKeyHeader": []}]}},
        "/api/v1/maybe": {"get": {"security": [{"OAuth2PasswordBearer": []}, {}]}},
    },
}
ALL = {
    "GET /api/v1/items",
    "POST /api/v1/items",
    "DELETE /api/v1/items/{item_id}",
    "GET /api/v1/open",
    "POST /api/v1/tracking/track",
    "GET /api/v1/maybe",
}

EXCLUSIONS = """
[[exclude]]
operation = "DELETE /api/v1/items/{item_id}"
passes = ["superuser", "viewer", "sdk"]
reason = "Deletes the item the pass needs."
"""

#: label -> [(method, status) or (method, status, path as sent)]
Interactions = Dict[str, List[tuple]]


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _report(
    tmp_path: Path, interactions: Interactions, selected: Optional[int] = 5
) -> Path:
    """An ndjson report in Schemathesis 4.29's shape, in its own directory."""
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    events: List[dict] = [{"Initialize": {"schemathesis_version": "4.29.3"}}]
    if selected is not None:
        events.append(
            {
                "LoadingFinished": {
                    "statistic": {"operations": {"total": 6, "selected": selected}}
                }
            }
        )
    for label, items in interactions.items():
        recorded = {}
        for index, item in enumerate(items):
            method, status = item[0], item[1]
            path = item[2] if len(item) > 2 else label.split(" ", 1)[1]
            recorded[f"{label}-{index}"] = {
                "request": {
                    "method": method,
                    "uri": f"http://127.0.0.1:8000{path}?q=1",
                },
                "response": None if status is None else {"status_code": status},
            }
        events.append(
            {
                "ScenarioFinished": {
                    "phase": "coverage",
                    "status": "success",
                    "recorder": {"label": label, "interactions": recorded},
                }
            }
        )
    events.append({"EngineFinished": {}})
    _write(
        report_dir / "ndjson-20261006T000000Z.ndjson",
        "".join(json.dumps(e) + "\n" for e in events),
    )
    return report_dir


#: Every selected operation of the superuser pass reached once.
REACHED_ALL: Interactions = {
    "GET /api/v1/items": [("GET", 200)],
    "POST /api/v1/items": [("POST", 201)],
    "GET /api/v1/open": [("GET", 200)],
    "POST /api/v1/tracking/track": [("POST", 202)],
    "GET /api/v1/maybe": [("GET", 200)],
}

#: Each request answered by the route of its own label: what the router
#: resolves for a request sent to an operation's own path with its own method.
OWN_ROUTES = object()


def _own_routes(report: fuzz_check.Report) -> Dict[Tuple[str, str], Optional[str]]:
    return {
        (item.method, item.path): f"{item.method} {item.path}"
        for items in report.interactions.values()
        for item in items
        if f"{item.method} {item.path}" in ALL
    }


def _verdict(
    tmp_path: Path,
    interactions: Interactions,
    unreached: str = "",
    pass_name: str = "superuser",
    selected: Optional[int] = 5,
    known: str = "",
    routes: Any = OWN_ROUTES,
) -> fuzz_check.Verdict:
    operations = fuzz_check.load_operations(DOCUMENT)
    excluded = fuzz_check.load_exclusions(
        _write(tmp_path / "exclusions.toml", EXCLUSIONS), operations
    )
    listed = fuzz_check.load_unreached(
        _write(tmp_path / "unreached.toml", unreached),
        pass_name,
        operations,
        excluded,
    )
    known_set = fuzz_check.load_known(
        _write(tmp_path / "known.toml", known), pass_name, operations
    )
    chosen = fuzz_check.selection(pass_name, operations, excluded)
    report = fuzz_check.read_report(_report(tmp_path, interactions, selected))
    if routes is OWN_ROUTES:
        routes = _own_routes(report)
    return fuzz_check.decide(report, operations, chosen, listed, known_set, routes)


# ---------------------------------------------------------------------------
# The reach check
# ---------------------------------------------------------------------------


def test_not_reached_is_exactly_the_refusals():
    """Dropping a status from the set would let an operation that was only ever
    refused count as reached; adding one would hide a reached operation."""
    assert fuzz_check.NOT_REACHED == frozenset({400, 401, 403, 404, 405, 422, 429})


def test_every_operation_reached_is_green(tmp_path):
    verdict = _verdict(tmp_path, REACHED_ALL)
    assert verdict.green
    assert verdict.reached == ALL - {"DELETE /api/v1/items/{item_id}"}
    assert not verdict.unreached


@pytest.mark.parametrize("status", sorted(fuzz_check.NOT_REACHED))
def test_each_refusal_alone_leaves_an_operation_unreached(tmp_path, status):
    interactions = dict(REACHED_ALL)
    interactions["POST /api/v1/items"] = [("POST", status)]
    verdict = _verdict(tmp_path, interactions)
    assert verdict.unlisted == {"POST /api/v1/items"}
    assert not verdict.green


def test_an_operation_that_only_answered_refusals_is_unreached_and_red(tmp_path):
    """The planted UNREACHED: every answer was a refusal of some kind."""
    interactions = dict(REACHED_ALL)
    interactions["POST /api/v1/items"] = [
        ("POST", status) for status in sorted(fuzz_check.NOT_REACHED)
    ]
    verdict = _verdict(tmp_path, interactions)
    assert verdict.unreached == {"POST /api/v1/items"}
    assert verdict.unlisted == {"POST /api/v1/items"}
    assert not verdict.green


def test_a_listed_unreached_operation_is_green(tmp_path):
    interactions = dict(REACHED_ALL)
    interactions["POST /api/v1/items"] = [("POST", 422), ("POST", 404)]
    listed = """
[[unreached]]
pass = "superuser"
operation = "POST /api/v1/items"
reason = "Every generated body fails validation."
"""
    verdict = _verdict(tmp_path, interactions, unreached=listed)
    assert verdict.unreached == {"POST /api/v1/items"}
    assert not verdict.unlisted
    assert verdict.green


def test_a_listed_operation_that_was_reached_is_stale_and_red(tmp_path):
    listed = """
[[unreached]]
pass = "superuser"
operation = "GET /api/v1/open"
reason = "Listed as unreached, but it answers 200 now."
"""
    verdict = _verdict(tmp_path, REACHED_ALL, unreached=listed)
    assert verdict.stale == {"GET /api/v1/open"}
    assert not verdict.green


def test_a_listed_entry_counts_only_for_its_own_pass(tmp_path):
    interactions = dict(REACHED_ALL)
    interactions["POST /api/v1/items"] = [("POST", 403)]
    listed = """
[[unreached]]
pass = "viewer"
operation = "POST /api/v1/items"
reason = "A VIEWER may not create items."
"""
    verdict = _verdict(tmp_path, interactions, unreached=listed)
    assert verdict.unlisted == {"POST /api/v1/items"}
    assert not verdict.green


def test_a_method_probe_does_not_count(tmp_path):
    """The coverage phase sends other methods to the same path; their answers
    (405, or whatever another route on that path answers) say nothing about
    this operation."""
    interactions = dict(REACHED_ALL)
    interactions["GET /api/v1/items"] = [
        ("GET", 404),
        ("TRACE", 405),
        ("PUT", 405),
        ("POST", 201),
    ]
    verdict = _verdict(tmp_path, interactions)
    assert "GET /api/v1/items" in verdict.unreached
    assert not verdict.green


def test_an_operation_missing_from_the_report_is_unreached(tmp_path):
    """One operation filtered out of the command: no interaction at all."""
    interactions = dict(REACHED_ALL)
    del interactions["GET /api/v1/maybe"]
    verdict = _verdict(tmp_path, interactions, selected=4)
    assert verdict.unlisted == {"GET /api/v1/maybe"}
    assert verdict.selection_mismatch
    assert not verdict.green


def test_a_request_with_no_answer_does_not_reach(tmp_path):
    interactions = dict(REACHED_ALL)
    interactions["GET /api/v1/open"] = [("GET", None)]
    verdict = _verdict(tmp_path, interactions)
    assert "GET /api/v1/open" in verdict.unreached
    assert verdict.no_answer == {"GET /api/v1/open"}


def test_an_excluded_operation_in_the_report_is_red(tmp_path):
    interactions = dict(REACHED_ALL)
    interactions["DELETE /api/v1/items/{item_id}"] = [("DELETE", 204)]
    verdict = _verdict(tmp_path, interactions, selected=6)
    assert verdict.outside == {"DELETE /api/v1/items/{item_id}"}
    assert not verdict.green


def test_schemathesis_selecting_a_different_number_is_red(tmp_path):
    verdict = _verdict(tmp_path, REACHED_ALL, selected=6)
    assert verdict.selection_mismatch
    assert not verdict.green


def test_a_bogus_token_leaves_reached_only_what_declares_no_auth(tmp_path):
    """Tamper (i) in miniature: every operation with an auth scheme answers
    401, and only the ones that declare none are reached."""
    interactions = {
        "GET /api/v1/items": [("GET", 401)],
        "POST /api/v1/items": [("POST", 401)],
        "GET /api/v1/open": [("GET", 200)],
        "POST /api/v1/tracking/track": [("POST", 401)],
        "GET /api/v1/maybe": [("GET", 401)],
    }
    verdict = _verdict(tmp_path, interactions)
    assert verdict.reached == {"GET /api/v1/open"}
    assert verdict.auth_declared & verdict.reached == set()
    assert verdict.unreached == verdict.auth_declared
    assert not verdict.green


def test_an_empty_report_fails(tmp_path):
    with pytest.raises(fuzz_check.ReportError, match="no interactions"):
        fuzz_check.read_report(_report(tmp_path, {"GET /api/v1/items": []}))


def test_a_missing_or_second_report_fails(tmp_path):
    (tmp_path / "none").mkdir()
    with pytest.raises(fuzz_check.ReportError, match="found 0"):
        fuzz_check.read_report(tmp_path / "none")
    report_dir = _report(tmp_path, REACHED_ALL)
    _write(report_dir / "ndjson-second.ndjson", "")
    with pytest.raises(fuzz_check.ReportError, match="found 2"):
        fuzz_check.read_report(report_dir)


def test_a_report_line_that_is_not_json_fails(tmp_path):
    report_dir = _report(tmp_path, REACHED_ALL)
    path = next(report_dir.glob("*.ndjson"))
    path.write_text(path.read_text() + "{not json\n")
    with pytest.raises(fuzz_check.ReportError, match="not JSON"):
        fuzz_check.read_report(report_dir)


def test_the_report_keeps_each_requests_path_as_sent(tmp_path):
    report = fuzz_check.read_report(
        _report(tmp_path, {"GET /api/v1/items": [("GET", 500, "/api/v1/it%20ems")]})
    )
    assert report.interactions["GET /api/v1/items"] == [
        fuzz_check.Interaction("GET", 500, "/api/v1/it%20ems")
    ]


# ---------------------------------------------------------------------------
# The known 5xx answers
# ---------------------------------------------------------------------------

KNOWN_ITEMS_500 = """
[[known]]
pass = "superuser"
operation = "GET /api/v1/items"
status = 500
issue = 955
reason = "An unknown filter value reaches the database."
"""


def test_a_listed_5xx_is_green(tmp_path):
    interactions = dict(REACHED_ALL)
    interactions["GET /api/v1/items"] = [("GET", 200), ("GET", 500)]
    verdict = _verdict(tmp_path, interactions, known=KNOWN_ITEMS_500)
    assert verdict.errors == {("GET /api/v1/items", 500)}
    assert not verdict.errors_unlisted and not verdict.known_quiet
    assert verdict.green


def test_an_unlisted_5xx_is_red(tmp_path):
    """The planted unlisted 5xx: the list holds nothing for it."""
    interactions = dict(REACHED_ALL)
    interactions["GET /api/v1/items"] = [("GET", 200), ("GET", 500)]
    verdict = _verdict(tmp_path, interactions)
    assert verdict.errors_unlisted == {("GET /api/v1/items", 500)}
    assert "GET /api/v1/items" in verdict.reached
    assert not verdict.green


def test_a_listed_entry_that_answered_no_5xx_is_stale_and_red(tmp_path):
    """The planted stale entry: the route no longer fails, so the entry must
    go (the bug was fixed, or the list was wrong)."""
    verdict = _verdict(tmp_path, REACHED_ALL, known=KNOWN_ITEMS_500)
    assert not verdict.errors
    assert verdict.known_quiet == {("GET /api/v1/items", 500)}
    assert not verdict.green


def test_a_known_entry_counts_only_for_its_own_pass(tmp_path):
    interactions = dict(REACHED_ALL)
    interactions["GET /api/v1/items"] = [("GET", 500)]
    viewer_only = KNOWN_ITEMS_500.replace('"superuser"', '"viewer"')
    verdict = _verdict(tmp_path, interactions, known=viewer_only)
    assert verdict.errors_unlisted == {("GET /api/v1/items", 500)}
    assert not verdict.known_quiet  # the viewer's entry is the viewer pass's
    assert not verdict.green


def test_the_status_is_part_of_the_entry(tmp_path):
    """A 503 where the list says 500 is a new answer, and the 500 is stale."""
    interactions = dict(REACHED_ALL)
    interactions["GET /api/v1/items"] = [("GET", 503)]
    verdict = _verdict(tmp_path, interactions, known=KNOWN_ITEMS_500)
    assert verdict.errors_unlisted == {("GET /api/v1/items", 503)}
    assert verdict.known_quiet == {("GET /api/v1/items", 500)}
    assert not verdict.green


def test_a_5xx_no_route_answered_is_never_listed(tmp_path):
    interactions = dict(REACHED_ALL)
    interactions["GET /api/v1/items"] = [("GET", 200), ("GET", 500, "/nowhere")]
    verdict = _verdict(tmp_path, interactions)
    assert verdict.errors_unlisted == {(f"{fuzz_check.NO_ROUTE} GET /nowhere", 500)}
    assert not verdict.green


def test_without_the_resolved_routes_a_5xx_is_red(tmp_path):
    interactions = dict(REACHED_ALL)
    interactions["GET /api/v1/items"] = [("GET", 500)]
    verdict = _verdict(tmp_path, interactions, known=KNOWN_ITEMS_500, routes=None)
    assert verdict.report_problem
    assert not verdict.green


#: The probe case from the first runs: the coverage phase sends DELETE to the
#: path of GET /api/v1/users/me, which the router hands to
#: DELETE /api/v1/users/{user_id} with user_id "me".
USERS_DOCUMENT = {
    "openapi": "3.1.0",
    "paths": {
        "/api/v1/users/me": {"get": {}},
        "/api/v1/users/{user_id}": {"delete": {}, "get": {}},
    },
}


def _users_app():
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/api/v1/users/me")
    def me():  # pragma: no cover - never called
        return {}

    @app.get("/api/v1/users/{user_id}")
    def read(user_id: str):  # pragma: no cover - never called
        return {}

    @app.delete("/api/v1/users/{user_id}")
    def delete(user_id: str):  # pragma: no cover - never called
        return {}

    return app


def test_the_router_names_the_route_that_answered():
    app = _users_app()
    resolved = fuzz_check.resolve_routes(
        app,
        [
            ("DELETE", "/api/v1/users/me"),
            ("GET", "/api/v1/users/me"),
            ("GET", "/api/v1/users/%5BuY"),
            ("PUT", "/api/v1/users/me"),
            ("GET", "/api/v1/users/me/"),
            ("GET", "/elsewhere"),
        ],
    )
    assert resolved == {
        ("DELETE", "/api/v1/users/me"): "DELETE /api/v1/users/{user_id}",
        ("GET", "/api/v1/users/me"): "GET /api/v1/users/me",
        ("GET", "/api/v1/users/%5BuY"): "GET /api/v1/users/{user_id}",
        # a method no route on the path takes: the router answers 405 itself
        ("PUT", "/api/v1/users/me"): None,
        # the router answers with a redirect to the path without the slash
        ("GET", "/api/v1/users/me/"): None,
        ("GET", "/elsewhere"): None,
    }


@pytest.mark.regression
def test_a_probe_counts_against_the_route_that_answered_not_its_label(tmp_path):
    """The first runs counted the 500 of a DELETE probe sent to
    /api/v1/users/me under the label GET /api/v1/users/me, whose GET always
    answered 200. Keyed on the label, a known-5xx entry would have encoded the
    probe, and a fix to DELETE /users/{user_id} would have left it standing."""
    operations = fuzz_check.load_operations(USERS_DOCUMENT)
    report = fuzz_check.read_report(
        _report(
            tmp_path,
            {
                "GET /api/v1/users/me": [
                    ("GET", 200, "/api/v1/users/me"),
                    ("DELETE", 500, "/api/v1/users/me"),
                    ("PUT", 405, "/api/v1/users/me"),
                ]
            },
            selected=1,
        )
    )
    routes = fuzz_check.resolve_routes(
        _users_app(),
        {(i.method, i.path) for i in report.interactions["GET /api/v1/users/me"]},
    )
    selected = {"GET /api/v1/users/me"}

    def known(text: str):
        return fuzz_check.load_known(
            _write(tmp_path / "known.toml", text), "viewer", operations
        )

    by_route = known(
        '[[known]]\npass = "viewer"\noperation = "DELETE /api/v1/users/{user_id}"\n'
        'status = 500\nissue = 959\nreason = "A non-UUID id reaches the query."\n'
    )
    verdict = fuzz_check.decide(report, operations, selected, set(), by_route, routes)
    assert verdict.errors == {("DELETE /api/v1/users/{user_id}", 500)}
    assert verdict.answered == [
        fuzz_check.Answered(
            "DELETE",
            "/api/v1/users/me",
            500,
            "GET /api/v1/users/me",
            "DELETE /api/v1/users/{user_id}",
        )
    ]
    assert verdict.green
    detail = fuzz_check.details(verdict)["server_errors"]
    assert detail == [
        {
            "method": "DELETE",
            "path": "/api/v1/users/me",
            "status": 500,
            "route": "DELETE /api/v1/users/{user_id}",
            "label": "GET /api/v1/users/me",
        }
    ]

    by_label = known(
        '[[known]]\npass = "viewer"\noperation = "GET /api/v1/users/me"\n'
        'status = 500\nissue = 959\nreason = "Listed under the label, not the route."\n'
    )
    verdict = fuzz_check.decide(report, operations, selected, set(), by_label, routes)
    assert verdict.errors_unlisted == {("DELETE /api/v1/users/{user_id}", 500)}
    assert verdict.known_quiet == {("GET /api/v1/users/me", 500)}
    assert not verdict.green


def test_the_routes_are_resolved_only_by_the_app_that_served_the_document(
    tmp_path,
):
    requests = [fuzz_check.ServerError("DELETE", "/api/v1/users/me", 500, "x")]
    other = {"openapi": "3.1.0", "paths": {"/api/v1/other": {"get": {}}}}
    with pytest.raises(fuzz_check.ListError, match="does not serve the document"):
        fuzz_check.write_routes(_users_app(), other, requests, tmp_path / "r.json")
    fuzz_check.write_routes(_users_app(), USERS_DOCUMENT, requests, tmp_path / "r.json")
    assert fuzz_check.read_routes(tmp_path / "r.json") == {
        ("DELETE", "/api/v1/users/me"): "DELETE /api/v1/users/{user_id}"
    }


def test_the_requests_file_holds_each_distinct_5xx_request(tmp_path):
    report = fuzz_check.read_report(
        _report(
            tmp_path,
            {
                "GET /api/v1/items": [
                    ("GET", 500, "/api/v1/items"),
                    ("GET", 500, "/api/v1/items"),
                    ("GET", 200, "/api/v1/items"),
                ],
                "GET /api/v1/open": [("DELETE", 503, "/api/v1/open")],
            },
        )
    )
    out = tmp_path / "requests.json"
    assert fuzz_check.write_requests(report, out) == 2
    assert fuzz_check.read_requests(out) == [
        fuzz_check.ServerError("DELETE", "/api/v1/open", 503, "GET /api/v1/open"),
        fuzz_check.ServerError("GET", "/api/v1/items", 500, "GET /api/v1/items"),
    ]


#: The application's own modules: a tree without them has no application to
#: route with, and only then do the tests below skip.
APP_MODULES = ("backend", "backend.app", "backend.app.main")


def _real_app():
    """The application, or a skip when this tree does not hold it. Any other
    import error fails: these tests are what catches a FastAPI bump that
    breaks the route resolution, and a skip would hide it."""
    try:
        from backend.app.main import app
    except ModuleNotFoundError as error:
        if error.name in APP_MODULES:
            pytest.skip(f"{error.name} is not installed in this tree")
        raise
    return app


def test_the_real_router_answers_the_users_me_probe_with_delete_user():
    """FastAPI's included routers keep their own routes; the dispatch goes
    through them and names each route by its full path."""
    resolved = fuzz_check.resolve_routes(
        _real_app(),
        [
            ("DELETE", "/api/v1/users/me"),
            ("GET", "/api/v1/users/me"),
            ("GET", "/api/v1/feature-flags/"),
            ("GET", "/health"),
            ("TRACE", "/api/v1/users/me"),
            ("GET", "/api/v1/nowhere"),
        ],
    )
    assert resolved == {
        ("DELETE", "/api/v1/users/me"): "DELETE /api/v1/users/{user_id}",
        ("GET", "/api/v1/users/me"): "GET /api/v1/users/me",
        ("GET", "/api/v1/feature-flags/"): "GET /api/v1/feature-flags/",
        ("GET", "/health"): "GET /health",
        ("TRACE", "/api/v1/users/me"): None,
        ("GET", "/api/v1/nowhere"): None,
    }


def test_resolving_runs_no_handler_and_restores_the_routes(monkeypatch):
    """A request for each operation of the served document, at the
    operation's own method and path, is answered by that operation: each was
    stopped by the recording handler (a handler that ran instead would leave
    its request unresolved), and the route classes get their handlers back."""
    from fastapi import routing as fastapi_routing
    from starlette import routing as starlette_routing

    app = _real_app()
    before = (
        starlette_routing.Route.__dict__["handle"],
        fastapi_routing.APIRoute.__dict__["handle"],
    )
    labels = sorted(fuzz_check.app_operations(app))
    requests = [(label.split(" ", 1)[0], label.split(" ", 1)[1]) for label in labels]
    resolved = fuzz_check.resolve_routes(app, requests)
    assert (
        starlette_routing.Route.__dict__["handle"],
        fastapi_routing.APIRoute.__dict__["handle"],
    ) == before
    # Each operation's own path and method resolve to itself.
    assert {f"{m} {p}": route for (m, p), route in resolved.items()} == {
        label: label for label in labels
    }


def test_the_real_router_answers_the_counters_bulk_probe_with_the_counter_read():
    app = _real_app()
    if "GET /api/v1/counters/{experiment_id}" not in fuzz_check.app_operations(app):
        pytest.skip("the counters module is not installed in this profile")
    resolved = fuzz_check.resolve_routes(app, [("GET", "/api/v1/counters/bulk")])
    assert resolved == {
        ("GET", "/api/v1/counters/bulk"): "GET /api/v1/counters/{experiment_id}"
    }


# ---------------------------------------------------------------------------
# The lists
# ---------------------------------------------------------------------------

OPERATIONS = fuzz_check.load_operations(DOCUMENT)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            '[[exclude]]\noperation = "GET /api/v1/nope"\npasses = ["sdk"]\n'
            'reason = "No such operation here."\n',
            "not an operation",
        ),
        (
            '[[exclude]]\noperation = "GET /api/v1/open"\npasses = ["admin"]\n'
            'reason = "Not a pass of this suite."\n',
            "passes must be",
        ),
        (
            '[[exclude]]\noperation = "GET /api/v1/open"\npasses = []\n'
            'reason = "No pass at all is named."\n',
            "passes must be",
        ),
        (
            '[[exclude]]\noperation = "GET /api/v1/open"\npasses = ["sdk"]\n',
            "reason",
        ),
        (
            '[[exclude]]\noperation = "GET /api/v1/open"\npasses = ["sdk"]\n'
            'reason = "First time it is listed."\n'
            '[[exclude]]\noperation = "GET /api/v1/open"\npasses = ["viewer"]\n'
            'reason = "Second time it is listed."\n',
            "listed twice",
        ),
        (
            '[[exclude]]\noperation = "/api/v1/open"\npasses = ["sdk"]\n'
            'reason = "No method in the operation."\n',
            "METHOD /path",
        ),
        (
            '[[exclude]]\noperation = "GET /api/v1/open"\npasses = ["sdk"]\n'
            'reason = "An extra key is refused."\nmethod = "GET"\n',
            "unknown keys",
        ),
        ("[[excluded]]\n", "unknown top-level"),
        ("[[exclude]\n", "cannot be read"),
    ],
)
def test_the_exclusion_list_refuses(tmp_path, text, message):
    with pytest.raises(fuzz_check.ListError, match=message):
        fuzz_check.load_exclusions(_write(tmp_path / "x.toml", text), OPERATIONS)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            '[[unreached]]\npass = "superuser"\noperation = "GET /api/v1/nope"\n'
            'reason = "No such operation here."\n',
            "not an operation",
        ),
        (
            '[[unreached]]\npass = "nobody"\noperation = "GET /api/v1/open"\n'
            'reason = "Not a pass of this suite."\n',
            "pass must be",
        ),
        (
            '[[unreached]]\npass = "superuser"\noperation = "GET /api/v1/open"\n'
            'reason = "short"\n',
            "reason",
        ),
        (
            '[[unreached]]\npass = "superuser"\n'
            'operation = "DELETE /api/v1/items/{item_id}"\n'
            'reason = "Excluded, so it cannot be unreached."\n',
            "does not select",
        ),
        (
            '[[unreached]]\npass = "sdk"\noperation = "GET /api/v1/open"\n'
            'reason = "Not an SDK route at all."\n',
            "does not select",
        ),
        (
            '[[unreached]]\npass = "viewer"\noperation = "GET /api/v1/open"\n'
            'reason = "First time it is listed."\n'
            '[[unreached]]\npass = "viewer"\noperation = "GET /api/v1/open"\n'
            'reason = "Second time it is listed."\n',
            "listed twice",
        ),
    ],
)
def test_the_unreached_list_refuses_for_every_pass(tmp_path, text, message):
    """A broken entry fails all three passes, not only its own."""
    excluded = fuzz_check.load_exclusions(
        _write(tmp_path / "exclusions.toml", EXCLUSIONS), OPERATIONS
    )
    path = _write(tmp_path / "unreached.toml", text)
    for pass_name in fuzz_check.PASSES:
        with pytest.raises(fuzz_check.ListError, match=message):
            fuzz_check.load_unreached(path, pass_name, OPERATIONS, excluded)


def _known(**fields: Any) -> str:
    entry = {
        "pass": "viewer",
        "operation": "GET /api/v1/items",
        "status": 500,
        "issue": 955,
        "reason": "An unknown filter value reaches the database.",
    }
    entry.update(fields)
    lines = ["[[known]]"]
    for key, value in entry.items():
        if value is not None:
            lines.append(f"{key} = {json.dumps(value)}")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (_known(operation="GET /api/v1/nope"), "not an operation"),
        (_known(operation="/api/v1/items"), "METHOD /path"),
        (_known(**{"pass": "admin"}), "pass must be"),
        (_known(status=404), "5xx status"),
        (_known(status=600), "5xx status"),
        (_known(status="500"), "5xx status"),
        (_known(status=True), "5xx status"),
        (_known(issue=None), "either the issue or the cause"),
        (_known(cause="aws"), "either the issue or the cause"),
        (_known(issue=None, cause="network"), "cause must be"),
        (_known(issue=None, cause="config", status=500), "needs an issue"),
        (_known(issue=None, cause="config", status=502), "needs an issue"),
        (_known(issue=None, cause="aws", status=503), "needs an issue"),
        (_known(issue=None, cause="aws", status=504), "needs an issue"),
        (_known(issue=0), "public issue number"),
        (_known(issue="#955"), "public issue number"),
        (_known(reason="short"), "reason"),
        (_known(reason="two\nlines of reason here"), "one line"),
        (_known(label="GET /api/v1/items"), "unknown keys"),
        (_known() + _known(issue=956), "listed twice"),
        ("[[known5xx]]\n", "unknown top-level"),
    ],
)
def test_the_known_list_refuses_for_every_pass(tmp_path, text, message):
    path = _write(tmp_path / "known.toml", text)
    for pass_name in fuzz_check.PASSES:
        with pytest.raises(fuzz_check.ListError, match=message):
            fuzz_check.load_known(path, pass_name, OPERATIONS)


@pytest.mark.parametrize(("cause", "status"), [("aws", 500), ("config", 503)])
def test_a_cause_lists_its_own_status(tmp_path, cause, status):
    """An aws call to the closed port fails as 500, and a missing setting is
    answered 503; those are the only statuses a cause may list."""
    text = _known(issue=None, cause=cause, status=status)
    assert fuzz_check.load_known(
        _write(tmp_path / "k.toml", text), "viewer", OPERATIONS
    ) == {("GET /api/v1/items", status)}


def test_a_known_entry_may_name_a_route_its_pass_does_not_select(tmp_path):
    """A probe can be answered by a route the pass excludes or, for sdk, by
    one outside the SDK routes; the entry names the route that answered."""
    text = _known(**{"pass": "sdk"}, operation="DELETE /api/v1/items/{item_id}")
    assert fuzz_check.load_known(_write(tmp_path / "k.toml", text), "sdk", OPERATIONS)


def test_the_sdk_pass_selects_only_the_sdk_routes(tmp_path):
    excluded = fuzz_check.load_exclusions(
        _write(tmp_path / "exclusions.toml", EXCLUSIONS), OPERATIONS
    )
    assert fuzz_check.selection("sdk", OPERATIONS, excluded) == {
        "POST /api/v1/tracking/track"
    }
    args = fuzz_check.filter_args("sdk", excluded)
    assert args[:2] == ["--include-path-regex", fuzz_check.SDK_PATH_REGEX]
    assert args[2:] == ["--exclude-name", "DELETE /api/v1/items/{item_id}"]
    assert fuzz_check.filter_args("viewer", excluded) == args[2:]


def test_auth_is_read_from_the_operation_or_the_document():
    document = {
        "security": [{"OAuth2PasswordBearer": []}],
        "paths": {"/a": {"get": {}, "post": {"security": []}}},
    }
    operations = fuzz_check.load_operations(document)
    assert operations["GET /a"].has_auth
    assert not operations["POST /a"].has_auth
    assert OPERATIONS["GET /api/v1/maybe"].has_auth  # optional, but it names one
    assert not OPERATIONS["GET /api/v1/open"].has_auth


# ---------------------------------------------------------------------------
# The real lists, against the full profile's API document
# ---------------------------------------------------------------------------

#: The exclusion list, pinned: (operation, passes). Change it with
#: tests/fuzz/exclusions.toml.
ALL_PASSES = ("superuser", "viewer", "sdk")
EXCLUDED = {
    # signing in and out, and the caller's own credentials
    "POST /api/v1/auth/login": ALL_PASSES,
    "POST /api/v1/auth/token": ALL_PASSES,
    "POST /api/v1/auth/logout": ALL_PASSES,
    "POST /api/v1/users/me/password": ALL_PASSES,
    "DELETE /api/v1/api-keys/{key_id}": ALL_PASSES,
    "DELETE /api/v1/users/{user_id}": ("viewer",),
    # /api/v1/export/*: one counter of 10 a minute
    "GET /api/v1/export/experiments": ALL_PASSES,
    "GET /api/v1/export/feature-flags": ALL_PASSES,
    "GET /api/v1/export/reports/experiments/{experiment_id}": ALL_PASSES,
    "GET /api/v1/export/reports/overview": ALL_PASSES,
    "GET /api/v1/export/variants": ALL_PASSES,
    # outbound connections
    "POST /api/v1/warehouse/analysis/connections/test": ALL_PASSES,
    "POST /api/v1/warehouse/analysis/connections/{connection_id}/test": ALL_PASSES,
    "POST /api/v1/warehouse/analysis/sources/{source_id}/validate": ALL_PASSES,
    "POST /api/v1/warehouse/analysis/sources/{source_id}/preview": ALL_PASSES,
    "POST /api/v1/warehouse/analysis/experiments/{experiment_id}/runs": ALL_PASSES,
    "POST /api/v1/notifications/test": ALL_PASSES,
    "POST /api/v1/scheduler/notify/test": ALL_PASSES,
    "POST /api/v1/llm-experiments/{experiment_id}/complete": ALL_PASSES,
    "POST /api/v1/llm-experiments/{experiment_id}/judge": ALL_PASSES,
    "POST /api/v1/ai/design": ALL_PASSES,
    "POST /api/v1/ai/interpret/{experiment_id}": ALL_PASSES,
    "POST /api/v1/auth/sso/configs": ALL_PASSES,
    "PUT /api/v1/auth/sso/configs/{config_id}": ALL_PASSES,
    "GET /api/v1/auth/sso/oidc/{provider}/callback": ALL_PASSES,
}


@pytest.fixture(scope="module")
def full_operations():
    if not FULL_SNAPSHOT.is_file():
        pytest.skip("this tree has no docs/api/openapi-v1.full.json")
    return fuzz_check.load_operations(json.loads(FULL_SNAPSHOT.read_text()))


def test_the_exclusion_list_is_exactly_the_pinned_one(full_operations):
    excluded = fuzz_check.load_exclusions(fuzz_check.EXCLUSIONS, full_operations)
    actual = {
        label: tuple(p for p in ALL_PASSES if label in excluded[p])
        for label in set().union(*excluded.values())
    }
    assert actual == EXCLUDED


def test_both_warehouse_connection_tests_are_excluded_in_every_pass(
    full_operations,
):
    excluded = fuzz_check.load_exclusions(fuzz_check.EXCLUSIONS, full_operations)
    for label in (
        "POST /api/v1/warehouse/analysis/connections/test",
        "POST /api/v1/warehouse/analysis/connections/{connection_id}/test",
    ):
        assert all(label in excluded[p] for p in fuzz_check.PASSES), label


def test_the_unreached_list_holds_for_every_pass(full_operations):
    excluded = fuzz_check.load_exclusions(fuzz_check.EXCLUSIONS, full_operations)
    for pass_name in fuzz_check.PASSES:
        fuzz_check.load_unreached(
            fuzz_check.UNREACHED, pass_name, full_operations, excluded
        )


def test_the_known_list_holds_for_every_pass(full_operations):
    for pass_name in fuzz_check.PASSES:
        fuzz_check.load_known(fuzz_check.KNOWN_5XX, pass_name, full_operations)


def test_every_known_entry_names_an_issue_or_a_cause_and_nothing_else():
    """Public text: the operation, the status, the issue or cause, one line."""
    import tomllib

    data = tomllib.loads(fuzz_check.KNOWN_5XX.read_text(encoding="utf-8"))
    for entry in data.get("known", []):
        assert set(entry) - {"issue", "cause"} == {
            "pass",
            "operation",
            "status",
            "reason",
        }, entry
        assert len(entry["reason"]) <= 160, entry


def test_the_sdk_prefixes_are_the_rate_limiters():
    from backend.app.middleware.rate_limiter import SDK_PATH_PREFIXES

    assert fuzz_check.SDK_PATH_PREFIXES == tuple(SDK_PATH_PREFIXES)


def test_the_sdk_regex_selects_what_the_evaluation_selects(full_operations):
    """Schemathesis selects by --include-path-regex, the evaluation by prefix;
    over the whole document they must agree."""
    pattern = re.compile(fuzz_check.SDK_PATH_REGEX)
    by_regex = {
        label for label, op in full_operations.items() if pattern.search(op.path)
    }
    by_prefix = fuzz_check.selection(
        "sdk", full_operations, {p: set() for p in fuzz_check.PASSES}
    )
    assert by_regex == by_prefix
    assert len(by_prefix) >= 10


# ---------------------------------------------------------------------------
# What a run prints and posts
# ---------------------------------------------------------------------------

COUNTS_LINE = re.compile(
    r"fuzz pass (superuser|viewer|sdk), \d{4}-\d{2}-\d{2}, commit [0-9a-f]{7,40},"
    r" seed \d{1,10} \((the lists' seed|not the lists' seed)\): (GREEN|RED)"
    r" \| operations selected \d+ \(Schemathesis (\d+|none)\), fuzzed \d+,"
    r" outside the selection \d+"
    r" \| 5xx operations \d+ \(not listed \d+, known entries \d+,"
    r" of them with no 5xx \d+\)"
    r" \| reached \d+, unreached \d+ \(not listed \d+, listed but reached \d+\)"
    r" \| declaring an auth scheme \d+, of them reached \d+"
    r" \| with a request that got no answer \d+"
)


def _run_main(
    tmp_path, interactions, *extra, unreached="", known="", routes="own", seed=None
):
    schema = _write(tmp_path / "openapi.json", json.dumps(DOCUMENT))
    exclusions = _write(tmp_path / "exclusions.toml", EXCLUSIONS)
    listed = _write(tmp_path / "unreached.toml", unreached)
    known_file = _write(tmp_path / "known.toml", known)
    report_dir = (
        _report(tmp_path, interactions)
        if interactions is not None
        else tmp_path / "empty"
    )
    report_dir.mkdir(exist_ok=True)
    routes_file = tmp_path / "routes.json"
    if routes == "own" and interactions is not None:
        own = _own_routes(fuzz_check.read_report(report_dir))
        _write(
            routes_file,
            json.dumps({"routes": [[m, p, r] for (m, p), r in own.items()]}),
        )
    return fuzz_check.main(
        [
            "evaluate",
            "--pass",
            "superuser",
            "--schema",
            str(schema),
            "--exclusions",
            str(exclusions),
            "--unreached",
            str(listed),
            "--known",
            str(known_file),
            "--report-dir",
            str(report_dir),
            "--routes",
            str(routes_file),
            "--seed",
            seed or str(fuzz_check.LISTS_SEED),
            "--sha",
            "0123456789abcdef0123456789abcdef01234567",
            "--date",
            "2026-10-06",
            *extra,
        ]
    )


def test_the_only_printed_line_is_counts(tmp_path, capsys):
    interactions = dict(REACHED_ALL)
    interactions["GET /api/v1/items"] = [("GET", 500, "/api/v1/items/x%2Fy")]
    assert _run_main(tmp_path, interactions) == 1
    out = capsys.readouterr()
    assert out.err == ""
    lines = out.out.splitlines()
    assert len(lines) == 1 and COUNTS_LINE.fullmatch(lines[0]), lines
    assert "/api/" not in out.out


def test_a_missing_report_is_red_and_says_only_that(tmp_path, capsys):
    assert _run_main(tmp_path, None) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "fuzz_check: expected one ndjson report, found 0"
    assert COUNTS_LINE.fullmatch(lines[1]), lines


def test_a_missing_routes_file_is_red(tmp_path, capsys):
    assert _run_main(tmp_path, REACHED_ALL, routes=None) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("fuzz_check: the routes file cannot be read")
    assert COUNTS_LINE.fullmatch(lines[1]), lines


def test_a_refused_list_exits_2(tmp_path, capsys):
    bad = '[[unreached]]\npass = "superuser"\noperation = "GET /api/v1/nope"\n'
    bad += 'reason = "No such operation here."\n'
    assert _run_main(tmp_path, REACHED_ALL, unreached=bad) == 2
    assert "refused" in capsys.readouterr().err


def test_the_counts_line_says_whether_the_seed_is_the_lists(tmp_path, capsys):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert _run_main(tmp_path / "a", REACHED_ALL) == 0
    assert "(the lists' seed): GREEN" in capsys.readouterr().out
    assert _run_main(tmp_path / "b", REACHED_ALL, seed="7") == 0
    assert "seed 7 (not the lists' seed): GREEN" in capsys.readouterr().out


@pytest.fixture
def qa_render():
    if not (SCRIPTS / "qa_render.py").is_file():
        pytest.skip("this tree has no scripts/qa_render.py")


@pytest.mark.usefixtures("qa_render")
def test_the_green_summary_is_the_green_template(tmp_path, capsys):
    summary = tmp_path / "summary.md"
    interactions = dict(REACHED_ALL)
    interactions["GET /api/v1/items"] = [("GET", 500)]
    assert (
        _run_main(
            tmp_path, interactions, "--summary", str(summary), known=KNOWN_ITEMS_500
        )
        == 0
    )
    text = summary.read_text()
    assert text.startswith(
        "API fuzzing (superuser pass), 2026-10-06, commit "
        "0123456789abcdef0123456789abcdef01234567: GREEN"
    )
    assert "Operations fuzzed: 5 of the 5 this pass selects." in text
    assert "Operations that answered 5xx: 1, every one in the known list." in text
    assert f"make fuzz FUZZ_SEED={fuzz_check.LISTS_SEED}" in text


@pytest.mark.usefixtures("qa_render")
def test_the_red_summary_is_the_red_template(tmp_path):
    summary = tmp_path / "summary.md"
    values = tmp_path / "values.json"
    interactions = dict(REACHED_ALL)
    interactions["GET /api/v1/items"] = [("GET", 503)]
    interactions["POST /api/v1/items"] = [("POST", 403)]
    link = "https://github.com/Controlvar/controlvar/actions/runs/123"
    assert (
        _run_main(
            tmp_path,
            interactions,
            "--summary",
            str(summary),
            "--values",
            str(values),
            "--run-link",
            link,
            known=KNOWN_ITEMS_500,
        )
        == 1
    )
    text = summary.read_text()
    assert text.splitlines()[0].endswith(": RED")
    assert "Operations that answered 5xx: 1, of which 1 are not in" in text
    assert "Known entries with no 5xx in this run: 1." in text
    assert "1 unreached and not listed; 0 listed as unreached" in text
    assert f"Run: {link}" in text
    handed = json.loads(values.read_text())
    assert handed["template"] == "fuzz-red.tmpl"
    assert handed["values"]["count_known_quiet"] == "1"
    assert handed["values"]["run_link"] == link


@pytest.mark.usefixtures("qa_render")
def test_a_red_summary_without_a_run_link_is_refused(tmp_path):
    interactions = dict(REACHED_ALL)
    interactions["GET /api/v1/items"] = [("GET", 500)]
    summary = tmp_path / "summary.md"
    assert _run_main(tmp_path, interactions, "--summary", str(summary)) == 2
    assert not summary.exists()


# ---------------------------------------------------------------------------
# The workflow, the arm and `make fuzz` run the same command
# ---------------------------------------------------------------------------

#: The Schemathesis flags of a fuzz run (plan v1.2 section F [4]).
FLAGS = (
    "--checks not_a_server_error --phases examples,coverage,fuzzing",
    "--max-examples 5 --workers 1 --rate-limit 4/s",
    '--seed "$FUZZ_SEED" --generation-deterministic',
    "--report ndjson --report-dir",
)
#: The fuzzer's install: exactly the two files' pins, then checked.
INSTALL = (
    "install --quiet --disable-pip-version-check --no-deps"
    " -r tests/fuzz/requirements.txt -r tests/fuzz/constraints.txt"
)


def _arm() -> str:
    return (WORKFLOWS / "_platform.yml").read_text()


def _local() -> str:
    return (SCRIPTS / "fuzz_local.sh").read_text()


def test_the_arm_and_make_fuzz_pass_the_same_flags():
    arm, local = _arm(), _local()
    for flags in FLAGS:
        assert flags in arm, flags
        assert flags.replace("$FUZZ_SEED", "$fuzz_seed") in local, flags
    for text in (arm, local):
        assert "stateful" not in text


def test_the_arm_and_make_fuzz_install_the_pinned_closure_and_check_it():
    # One copy of the commands, scripts/check_fuzz_install.sh; the arm, `make
    # fuzz` and the pull request check all call it (test_check_fuzz_install.py
    # pins the third, and runs the script).
    script = (SCRIPTS / "check_fuzz_install.sh").read_text()
    assert INSTALL in script
    assert 'pip" check --disable-pip-version-check' in script
    for text in (_arm(), _local()):
        assert "scripts/check_fuzz_install.sh" in text
        assert INSTALL not in text


def test_the_arm_and_make_fuzz_resolve_routes_and_hand_them_to_the_evaluation():
    for text in (_arm(), _local()):
        assert "scripts/fuzz_check.py requests --report-dir" in text
        assert "fuzz_check.py routes" in text
        assert '--routes "$work/routes.json"' in text


def test_the_sdk_pass_uses_the_seeds_ruleset_key_in_both():
    assert "tests/sdk-contract/live/.api_key_local" in _arm()
    assert "/data/demo/sdk-contract/.api_key_local" in _local()


def test_requirements_name_the_constraints_and_every_pin_is_exact():
    requirements = (FUZZ_DIR / "requirements.txt").read_text().splitlines()
    assert "-c constraints.txt" in requirements
    for path in (FUZZ_DIR / "requirements.txt", FUZZ_DIR / "constraints.txt"):
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-c "):
                continue
            assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*==[0-9][^\s]*", line), (
                path.name,
                line,
            )


def test_the_lock_check_holds_the_fuzz_pins_to_the_venvs():
    import check_requirements_lock as lock

    pairs = {
        (s.relative_to(REPO_ROOT).as_posix(), t.relative_to(REPO_ROOT).as_posix())
        for s, t, _r, _e in lock.SUPERSETS
    }
    assert ("tests/fuzz/requirements.txt", "backend/requirements.txt") in pairs
    assert ("tests/fuzz/constraints.txt", "backend/requirements.txt") in pairs


# ---------------------------------------------------------------------------
# The AWS variables: one set, in the workflow and in `make fuzz`
# ---------------------------------------------------------------------------

#: What the set must include, whatever else it holds.
AWS_REQUIRED = {
    "AWS_ENDPOINT_URL": "http://127.0.0.1:9",
    "AWS_MAX_ATTEMPTS": "1",
}
REGION_VARIABLES = ("AWS_REGION", "AWS_DEFAULT_REGION")


def _api_env(text: str) -> Dict[str, str]:
    return dict(line.split("=", 1) for line in text.split())


def aws_problems(
    fuzz: Dict[str, Any],
    platform: Dict[str, Any],
    compose: Dict[str, Any],
    override: Dict[str, Any],
) -> List[str]:
    """Where the workflow's AWS variables and `make fuzz`'s differ."""
    found: List[str] = []
    sets = {}
    for job_id, job in fuzz["jobs"].items():
        if "uses" in job:
            sets[job_id] = {
                k: v
                for k, v in _api_env(job["with"].get("api_env", "")).items()
                if k.startswith("AWS_")
            }
    if len(sets) != 3:
        found.append(f"fuzz.yml has {len(sets)} passes with an api_env")
    if len({tuple(sorted(s.items())) for s in sets.values()}) > 1:
        found.append("the passes' AWS variables differ")
    for name, value in (
        platform.get("jobs", {}).get("platform", {}).get("env") or {}
    ).items():
        if name.startswith("AWS_"):
            found.append(
                f"_platform.yml's job env sets {name}, which the CI API inherits"
            )
    base = compose["services"]["api"].get("environment") or {}
    extra = override["services"]["api"].get("environment") or {}
    container = {
        k: str(v) for k, v in {**base, **extra}.items() if k.startswith("AWS_")
    }
    for job_id, ci in sets.items():
        if ci != container:
            differ = sorted(set(ci.items()) ^ set(container.items()))
            found.append(
                f"{job_id}: the CI API and the make fuzz container differ in"
                f" {sorted({k for k, _ in differ})}"
            )
        for name, value in AWS_REQUIRED.items():
            if ci.get(name) != value:
                found.append(f"{job_id}: {name} is not {value}")
        if not all(ci.get(name) for name in REGION_VARIABLES):
            found.append(f"{job_id}: no region in {REGION_VARIABLES}")
    return found


def _aws_docs():
    return (
        yaml.safe_load((WORKFLOWS / "fuzz.yml").read_text()),
        yaml.safe_load((WORKFLOWS / "_platform.yml").read_text()),
        yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text()),
        yaml.safe_load((FUZZ_DIR / "compose.fuzz.yml").read_text()),
    )


def test_the_workflow_and_make_fuzz_give_the_api_one_set_of_aws_variables():
    fuzz, platform, compose, override = _aws_docs()
    assert aws_problems(fuzz, platform, compose, override) == []


def _set_override(name, value):
    def plant(fuzz, platform, compose, override):
        override["services"]["api"]["environment"][name] = value

    return plant


def _drop_override(name):
    def plant(fuzz, platform, compose, override):
        del override["services"]["api"]["environment"][name]

    return plant


def _set_pass(job_id, name, value):
    def plant(fuzz, platform, compose, override):
        env = _api_env(fuzz["jobs"][job_id]["with"]["api_env"])
        if value is None:
            env.pop(name)
        else:
            env[name] = value
        fuzz["jobs"][job_id]["with"]["api_env"] = "\n".join(
            f"{k}={v}" for k, v in env.items()
        )

    return plant


def _set_base(name, value):
    def plant(fuzz, platform, compose, override):
        compose["services"]["api"]["environment"][name] = value

    return plant


def _set_platform(name, value):
    def plant(fuzz, platform, compose, override):
        platform["jobs"]["platform"]["env"][name] = value

    return plant


AWS_PLANTS = [
    ("compose-region-differs", _set_override("AWS_REGION", "us-west-2"), "differ in"),
    (
        "compose-default-region-dropped",
        _drop_override("AWS_DEFAULT_REGION"),
        "differ in",
    ),
    ("compose-retries", _drop_override("AWS_MAX_ATTEMPTS"), "differ in"),
    ("one-pass-retries", _set_pass("viewer", "AWS_MAX_ATTEMPTS", None), "passes'"),
    ("every-pass-no-region", None, "no region"),
    ("base-adds-one", _set_base("AWS_PROFILE", "dev"), "differ in"),
    ("platform-env", _set_platform("AWS_REGION", "us-east-1"), "job env"),
]


def _every_pass_no_region(fuzz, platform, compose, override):
    for job_id in ("superuser", "viewer", "sdk"):
        for name in REGION_VARIABLES:
            _set_pass(job_id, name, None)(fuzz, platform, compose, override)
    for name in REGION_VARIABLES:
        override["services"]["api"]["environment"].pop(name)
        compose["services"]["api"]["environment"].pop(name)


@pytest.mark.parametrize(
    "plant, fragment", [p[1:] for p in AWS_PLANTS], ids=[p[0] for p in AWS_PLANTS]
)
def test_each_aws_difference_is_caught(plant, fragment):
    docs = _aws_docs()
    (plant or _every_pass_no_region)(*docs)
    found = aws_problems(*docs)
    assert any(fragment in problem for problem in found), found


def test_make_fuzz_runs_the_workflows_database_and_redis_images():
    platform = yaml.safe_load((WORKFLOWS / "_platform.yml").read_text())
    override = yaml.safe_load((FUZZ_DIR / "compose.fuzz.yml").read_text())
    services = platform["jobs"]["platform"]["services"]
    assert override["services"]["postgres"]["image"] == services["postgres"]["image"]
    assert override["services"]["redis"]["image"] == services["redis"]["image"]


# ---------------------------------------------------------------------------
# The seed
# ---------------------------------------------------------------------------


def test_the_schedule_and_a_plain_dispatch_run_the_lists_seed():
    text = (WORKFLOWS / "fuzz.yml").read_text()
    assert f'seed="${{FUZZ_SEED:-{fuzz_check.LISTS_SEED}}}"' in text
    for name in ("unreached.toml", "known-5xx.toml"):
        assert str(fuzz_check.LISTS_SEED) in (FUZZ_DIR / name).read_text(), name


# ---------------------------------------------------------------------------
# `make fuzz`
# ---------------------------------------------------------------------------

FUZZ_LOCAL = SCRIPTS / "fuzz_local.sh"


def test_make_fuzz_cleans_up_before_and_after():
    text = _local()
    preclean = text.index('down -v --remove-orphans > "$work/compose-preclean.log"')
    assert preclean < text.index("up -d --build --wait api")
    cleanup = text[text.index("cleanup() {") : text.index("trap cleanup EXIT")]
    assert "down -v --remove-orphans" in cleanup
    assert 'docker image rm "$image"' in cleanup
    assert "image=experimently-api:fuzz" in text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["1", "abc1234", ""], "PASS must be"),
        (["1", "abc1234", "admin"], "PASS must be"),
        (["", "abc1234", "viewer"], "FUZZ_SEED must be"),
        (["01", "abc1234", "viewer"], "FUZZ_SEED must be"),
        (["x", "abc1234", "sdk"], "FUZZ_SEED must be"),
        (["7", "", "sdk"], "SHA must name"),
    ],
)
def test_make_fuzz_refuses_bad_arguments_before_touching_anything(
    tmp_path, args, message
):
    """These refusals come before git and Docker, so they hold anywhere; the
    SHA and clean-tree refusals need a git checkout and are shown on the pull
    request that added them."""
    result = subprocess.run(
        ["bash", str(FUZZ_LOCAL), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, result
    assert message in result.stderr
    assert "usage: make fuzz" in result.stderr
