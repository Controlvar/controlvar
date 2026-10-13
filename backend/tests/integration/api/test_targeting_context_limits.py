"""A targeting context over the limits answers 422, and nothing is evaluated,
assigned or recorded for the request.

The limits are ``backend/app/core/context_limits.py``'s: more than
``MAX_CONTEXT_DEPTH`` levels of objects and arrays, or more than
``MAX_CONTEXT_BYTES`` bytes of compact UTF-8 JSON. Each route that takes a
context checks it in its handler, before any evaluation or assignment:

* ``GET`` and ``POST /api/v1/feature-flags/evaluate/{flag_key}``
* ``GET /api/v1/feature-flags/user/{user_id}``
* ``POST /api/v1/tracking/assign``
* ``POST /api/v1/tracking/assign/batch``: every user, before the first is
  assigned

Real database and real API-key authentication: keys are made through
``POST /api/v1/api-keys`` (as ``test_tracking_assign_batch.py`` does), and only
``deps.get_db`` is overridden. The flag and the experiment carry a
dashboard-shape targeting rule, which is matched through ``expand_context``:
the spies below see every call made where each service looks the name up, so a
context within the limits is seen to reach the rules, and one over them is
seen not to. The last test starts uvicorn itself (h11, as the image runs it).
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from contextlib import contextmanager
from typing import Any, Dict, List

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core import context_limits
from backend.app.core.config import settings
from backend.app.core.context_limits import (
    CONTEXT_TOO_DEEP,
    CONTEXT_TOO_LARGE,
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_DEPTH,
)
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.core.targeting_adapter import normalise_targeting_rules
from backend.app.main import app
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.metrics.metric import ErrorLog, RawMetric
from backend.app.models.user import User, UserRole
from backend.app.services import assignment_service, feature_flag_service
from backend.tests.unit.core.test_context_limits import (
    array_chain,
    object_chain,
    shoplab_contexts,
    sized,
    streampulse_contexts,
)
from backend.tests.unit.docs.test_targeting_context_limit_docs import (
    docs_example_contexts,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

SCHEMA = "test_experimentation"
KEYS_URL = "/api/v1/api-keys"
REPO = pathlib.Path(__file__).resolve().parents[4]

#: The one rule of the flag and of the experiment: the dashboard shape, which
#: the rules engine matches on ``expand_context(context)``. A flag with no
#: rule, or a legacy-list rule, never expands the context, and these tests
#: could not tell a refused context from an ignored one.
DASHBOARD_RULE = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "tier", "operator": "equals", "value": "premium"}
            ],
        }
    ],
}
#: Matches the rule: the flag is on and the user is enrolled.
MATCHING = {"tier": "premium"}

ROUTES = ["get-evaluate", "post-evaluate", "get-user", "post-assign", "post-batch"]
FLAG_ROUTES = ROUTES[:3]
#: The batch's over-limit user, after two that are within the limits.
BATCH_POSITION = 2

#: Each over the limits, and each matching the rule, so that served it would
#: be evaluated, enrolled and recorded.
OVER_LIMITS = {
    "size": (lambda: sized(MAX_CONTEXT_BYTES + 1, **MATCHING), CONTEXT_TOO_LARGE),
    "size-multibyte": (
        lambda: sized(MAX_CONTEXT_BYTES + 1, "é", **MATCHING),
        CONTEXT_TOO_LARGE,
    ),
    "depth-objects": (
        lambda: object_chain(MAX_CONTEXT_DEPTH + 1, **MATCHING),
        CONTEXT_TOO_DEEP,
    ),
    "depth-arrays": (
        lambda: array_chain(MAX_CONTEXT_DEPTH + 1, **MATCHING),
        CONTEXT_TOO_DEEP,
    ),
    # About 1.9 KB: nested arrays this deep make json.dumps raise
    # RecursionError when it starts deep in the stack.
    "deep-array-930": (lambda: array_chain(930, **MATCHING), CONTEXT_TOO_DEEP),
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


@pytest.fixture
def client(db_session):
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )

    def override_get_db():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


@pytest.fixture
def headers(client, db_session) -> Dict[str, str]:
    """A key with the ``sdk:ruleset`` scope, which every route here accepts."""
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"ctxlim_{suffix}",
        email=f"ctxlim_{suffix}@ctxlim.test",
        full_name="Context Limits",
        hashed_password=get_password_hash("Demo1234!"),
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    resp = client.post(
        KEYS_URL,
        json={"name": f"ctxlim-{suffix}", "scopes": ["sdk:ruleset"]},
        headers={"Authorization": f"Bearer {create_local_access_token(user)}"},
    )
    assert resp.status_code in (200, 201), resp.text
    return {"X-API-Key": resp.json()["key"]}


@contextmanager
def _parked_holdouts(db_session):
    """No active global holdout for the duration, restored afterwards: a user
    in a holdout is never matched against the experiment's rule."""
    parked = (
        db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active.is_(True)).all()
    )
    for row in parked:
        row.is_active = False
    db_session.commit()
    try:
        yield
    finally:
        db_session.rollback()
        for row in parked:
            db_session.merge(row).is_active = True
        db_session.commit()


@pytest.fixture
def flag(db_session, make_feature_flag):
    """ACTIVE, at 0%: only the rule turns it on."""
    flag = make_feature_flag(
        key=f"ctxlim-{uuid.uuid4().hex[:8]}",
        name="Context limits",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=0,
        targeting_rules=DASHBOARD_RULE,
    )
    yield flag
    db_session.rollback()
    db_session.query(RawMetric).filter(RawMetric.feature_flag_id == flag.id).delete()
    db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id == flag.id).delete()
    db_session.query(FeatureFlag).filter(FeatureFlag.id == flag.id).delete()
    db_session.commit()


@pytest.fixture
def experiment(db_session, make_experiment):
    """ACTIVE, two arms, the same rule; no group, no holdout."""
    with _parked_holdouts(db_session):
        suffix = uuid.uuid4().hex[:8]
        experiment = make_experiment(
            name=f"Context limits {suffix}",
            key=f"ctxlim-{suffix}",
            status=ExperimentStatus.ACTIVE,
            targeting_rules=DASHBOARD_RULE,
        )
        for name in ("control", "treatment"):
            db_session.add(
                Variant(
                    experiment_id=experiment.id,
                    name=name,
                    description=name,
                    is_control=name == "control",
                    traffic_allocation=50,
                    configuration={"arm": name},
                )
            )
        db_session.commit()
        db_session.refresh(experiment)
        yield experiment
        db_session.rollback()
        db_session.query(Event).filter(Event.experiment_id == experiment.id).delete()
        db_session.query(Assignment).filter(
            Assignment.experiment_id == experiment.id
        ).delete()
        db_session.query(Experiment).filter(Experiment.id == experiment.id).update(
            {"status": ExperimentStatus.COMPLETED}
        )
        db_session.commit()


@pytest.fixture
def spies(monkeypatch) -> Dict[str, List[Any]]:
    """Every ``expand_context`` call, where each service looks the name up."""
    calls: Dict[str, List[Any]] = {"flags": [], "assignments": []}
    for name, module in (
        ("flags", feature_flag_service),
        ("assignments", assignment_service),
    ):
        real = module.expand_context

        def spy(context, _real=real, _calls=calls[name]):
            _calls.append(context)
            return _real(context)

        monkeypatch.setattr(module, "expand_context", spy)
    return calls


class Raw(str):
    """A context already written as JSON text: ``json.dumps`` recurses, and
    cannot write the chains deeper than the parser goes."""


def object_chain_text(levels: int) -> Raw:
    """``object_chain(levels)`` as compact JSON, without ``json.dumps``."""
    return Raw('{"a":' * levels + "1" + "}" * levels)


def array_chain_text(levels: int) -> Raw:
    """``array_chain(levels)`` as compact JSON, without ``json.dumps``."""
    return Raw('{"a":' + "[" * (levels - 1) + "1" + "]" * (levels - 1) + "}")


def _compact(context: Any) -> str:
    if isinstance(context, Raw):
        return str(context)
    return json.dumps(context, separators=(",", ":"), ensure_ascii=False)


def _body(**fields: Any) -> Dict[str, Any]:
    """A JSON request body whose ``context`` values may be :class:`Raw`.

    Only the body's own fields and the batch's user list are written here;
    every other value goes to ``json.dumps`` whole.
    """

    def write(value: Any) -> str:
        if isinstance(value, list):
            return "[" + ",".join(write(v) for v in value) + "]"
        if isinstance(value, dict) and any(
            isinstance(v, (Raw, list)) for v in value.values()
        ):
            return (
                "{"
                + ",".join(f"{json.dumps(k)}:{write(v)}" for k, v in value.items())
                + "}"
            )
        return _compact(value)

    return {
        "content": write(fields).encode("utf-8"),
        "headers": {"content-type": "application/json"},
    }


def _send(route, client, headers, flag, experiment, context, user="ctx-user"):
    if route == "get-evaluate":
        return client.get(
            f"/api/v1/feature-flags/evaluate/{flag.key}",
            params={"user_id": user, "context": _compact(context)},
            headers=headers,
        )
    if route == "get-user":
        return client.get(
            f"/api/v1/feature-flags/user/{user}",
            params={"context": _compact(context)},
            headers=headers,
        )
    if route == "post-evaluate":
        url = f"/api/v1/feature-flags/evaluate/{flag.key}"
        body = _body(user_id=user, context=context)
    elif route == "post-assign":
        url = "/api/v1/tracking/assign"
        body = _body(experiment_key=experiment.key, user_id=user, context=context)
    elif route == "post-batch":
        url = "/api/v1/tracking/assign/batch"
        users = [
            {"user_id": f"{user}-{i}", "context": MATCHING}
            for i in range(BATCH_POSITION)
        ]
        users.append({"user_id": f"{user}-{BATCH_POSITION}", "context": context})
        body = _body(experiment_key=experiment.key, users=users)
    else:
        raise AssertionError(route)
    return client.post(
        url, content=body["content"], headers={**headers, **body["headers"]}
    )


def _rows(db_session, flag, experiment) -> Dict[str, int]:
    db_session.expire_all()
    return {
        "metric": db_session.query(RawMetric)
        .filter(RawMetric.feature_flag_id == flag.id)
        .count(),
        "error": db_session.query(ErrorLog)
        .filter(ErrorLog.feature_flag_id == flag.id)
        .count(),
        "assignment": db_session.query(Assignment)
        .filter(Assignment.experiment_id == experiment.id)
        .count(),
        "event": db_session.query(Event)
        .filter(Event.experiment_id == experiment.id)
        .count(),
    }


NOTHING = {"metric": 0, "error": 0, "assignment": 0, "event": 0}


def _answer(response) -> tuple:
    """``(status, body)``: the JSON body, or the start of a body that is not
    JSON (a 500's plain text)."""
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text[:200]


# ---------------------------------------------------------------------------
# The fixtures are not vacuous
# ---------------------------------------------------------------------------


def test_the_rule_is_the_dashboard_shape_that_expands_the_context(flag, experiment):
    assert flag.targeting_rules == DASHBOARD_RULE
    assert experiment.targeting_rules == DASHBOARD_RULE
    assert normalise_targeting_rules(DASHBOARD_RULE, owner="test") is not None


@pytest.mark.parametrize("route", ROUTES)
def test_a_context_within_the_limits_reaches_the_rule_and_is_recorded(
    route, client, headers, flag, experiment, spies, db_session
):
    """The control for every refusal below: the same route, flag and
    experiment, a context within the limits, served and recorded."""
    response = _send(route, client, headers, flag, experiment, MATCHING)
    assert response.status_code == 200, response.text
    rows = _rows(db_session, flag, experiment)
    if route in FLAG_ROUTES:
        assert spies["flags"], "expand_context was not called"
        assert rows["metric"] > 0, rows
        if route == "get-user":
            assert response.json()[flag.key] is True
        else:
            assert response.json()["enabled"] is True
            assert response.json()["reason"] == "targeting_rule"
    else:
        assert spies["assignments"], "expand_context was not called"
        assert rows["assignment"] == (
            BATCH_POSITION + 1 if route == "post-batch" else 1
        )


# ---------------------------------------------------------------------------
# Over the limits: 422, nothing evaluated, assigned or recorded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", sorted(OVER_LIMITS))
@pytest.mark.parametrize("route", ROUTES)
def test_a_context_over_the_limits_answers_422_and_nothing_is_recorded(
    route, case, client, headers, flag, experiment, spies, db_session
):
    make, detail = OVER_LIMITS[case]
    response = _send(route, client, headers, flag, experiment, make())
    if route == "post-batch":
        detail = f"users[{BATCH_POSITION}]: {detail}"
    assert _answer(response) == (422, {"detail": detail})
    assert _rows(db_session, flag, experiment) == NOTHING
    assert spies == {"flags": [], "assignments": []}


#: Deeper than the JSON parser goes, and short: 1,000 objects with
#: one-character keys (6,001 bytes) and 1,500 levels of arrays (3,005 bytes).
TOO_DEEP_TO_PARSE = {
    "objects-1000": (object_chain_text(1000), 6001),
    "arrays-1500": (array_chain_text(1500), 3005),
}


@pytest.mark.parametrize("shape", sorted(TOO_DEEP_TO_PARSE))
@pytest.mark.parametrize("route", ["get-evaluate", "get-user"])
def test_a_get_context_too_deep_to_parse_answers_422(
    route, shape, client, headers, flag, experiment, spies, db_session
):
    """A parameter over the size limit is refused as sent, before it is
    parsed, so the depth detail is for one within it."""
    context, size = TOO_DEEP_TO_PARSE[shape]
    assert len(context) == size
    detail = CONTEXT_TOO_DEEP if size <= MAX_CONTEXT_BYTES else CONTEXT_TOO_LARGE
    response = _send(route, client, headers, flag, experiment, context)
    assert _answer(response) == (422, {"detail": detail})
    assert _rows(db_session, flag, experiment) == NOTHING
    assert spies == {"flags": [], "assignments": []}


@pytest.mark.parametrize("route", ["post-evaluate", "post-assign", "post-batch"])
def test_a_post_body_too_deep_to_parse_still_answers_400(
    route, client, headers, flag, experiment, spies, db_session
):
    response = _send(route, client, headers, flag, experiment, array_chain_text(5000))
    assert _answer(response) == (
        400,
        {"detail": "There was an error parsing the body"},
    )
    assert _rows(db_session, flag, experiment) == NOTHING


#: Depths around the JSON parser's own limit, the interpreter's recursion
#: limit. Near it, a context the parser accepts can still be too deep for
#: ``json.dumps`` called a few frames further down the stack, and where that
#: band falls depends on the stack. Every depth is sent, so the band is
#: crossed wherever it is.
SWEEP = range(sys.getrecursionlimit() - 300, sys.getrecursionlimit() + 21)


def _deep_arrays(depth: int) -> Raw:
    """``{"a": [[...[1]...]]}`` holding ``depth`` nested arrays."""
    return Raw('{"a":' + "[" * depth + "1" + "]" * depth + "}")


@pytest.mark.parametrize("route", ROUTES)
def test_contexts_around_the_parsers_own_depth_limit_are_refused_cleanly(
    route, client, headers, flag, experiment, spies, db_session, monkeypatch
):
    """Each depth answers exactly the depth 422 (or, on a POST, the body's
    400 where the body is too deep to parse at all), never 500 or 200. The
    walk ran for some depths and not for others: the sweep straddles the
    depth where the parser itself gives up."""
    walked = set()
    sending = {"depth": None}
    real = context_limits.nested_deeper_than

    def counting(value, limit):
        walked.add(sending["depth"])
        return real(value, limit)

    monkeypatch.setattr(context_limits, "nested_deeper_than", counting)
    refused = (
        422,
        {
            "detail": f"users[{BATCH_POSITION}]: {CONTEXT_TOO_DEEP}"
            if route == "post-batch"
            else CONTEXT_TOO_DEEP
        },
    )
    unparsed = (400, {"detail": "There was an error parsing the body"})
    allowed = [refused] if route.startswith("get") else [refused, unparsed]
    answers = {}
    for depth in SWEEP:
        sending["depth"] = depth
        answers[depth] = _answer(
            _send(route, client, headers, flag, experiment, _deep_arrays(depth))
        )
    ran = sorted(walked & set(SWEEP))
    print(
        f"{route}: depths {SWEEP.start}-{SWEEP.stop - 1}; the walk ran for "
        f"{ran[0] if ran else None}-{ran[-1] if ran else None} ({len(ran)}); "
        f"answers {sorted({a[0] for a in answers.values()})}"
    )
    wrong = {d: a for d, a in answers.items() if a not in allowed}
    assert not wrong, wrong
    assert walked & set(SWEEP), "the walk never ran for any depth"
    assert set(SWEEP) - walked, (
        "the walk ran for every depth: no depth reached the parser's limit"
    )
    assert _rows(db_session, flag, experiment) == NOTHING
    assert spies == {"flags": [], "assignments": []}


def _over_both():
    context = object_chain(MAX_CONTEXT_DEPTH + 1, **MATCHING)
    context["pad"] = "x" * (MAX_CONTEXT_BYTES + 1)
    return context


@pytest.mark.parametrize("route", ["post-evaluate", "post-assign", "post-batch"])
def test_a_context_over_both_limits_answers_the_depth_detail(
    route, client, headers, flag, experiment, spies, db_session
):
    detail = CONTEXT_TOO_DEEP
    if route == "post-batch":
        detail = f"users[{BATCH_POSITION}]: {detail}"
    response = _send(route, client, headers, flag, experiment, _over_both())
    assert _answer(response) == (422, {"detail": detail})
    assert _rows(db_session, flag, experiment) == NOTHING


def test_the_batch_is_refused_whole_before_the_first_user_is_assigned(
    client, headers, flag, experiment, spies, db_session
):
    """The over-limit user is the last of three; the first two would be
    enrolled, and are not."""
    response = _send(
        "post-batch",
        client,
        headers,
        flag,
        experiment,
        sized(MAX_CONTEXT_BYTES + 1, **MATCHING),
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith(f"users[{BATCH_POSITION}]: ")
    db_session.expire_all()
    assert (
        db_session.query(Assignment)
        .filter(Assignment.experiment_id == experiment.id)
        .count()
        == 0
    )
    assert spies["assignments"] == []


# ---------------------------------------------------------------------------
# The boundaries, on the routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("char", ["x", "é"], ids=["ascii", "multibyte"])
@pytest.mark.parametrize("route", ["get-evaluate", "post-evaluate"])
def test_exactly_the_size_limit_is_served_and_one_byte_more_is_refused(
    route, char, client, headers, flag, experiment, spies, db_session
):
    at_limit = sized(MAX_CONTEXT_BYTES, char, **MATCHING)
    over = sized(MAX_CONTEXT_BYTES + 1, char, **MATCHING)
    served = _send(route, client, headers, flag, experiment, at_limit, user="at")
    assert served.status_code == 200, served.text
    assert served.json()["reason"] == "targeting_rule"
    refused = _send(route, client, headers, flag, experiment, over, user="over")
    assert _answer(refused) == (
        422,
        {"detail": CONTEXT_TOO_LARGE},
    )
    assert len(spies["flags"]) == 1


@pytest.mark.parametrize("route", ["get-evaluate", "get-user"])
def test_a_get_context_is_held_to_the_size_limit_as_sent(
    route, client, headers, flag, experiment, spies, db_session
):
    """Spaces count on a GET: the parameter's text as sent, before it is
    parsed, though the same context written compactly is within the limit."""
    compact = sized(MAX_CONTEXT_BYTES - 10, **MATCHING)
    spaced = Raw(json.dumps(compact, indent=40, ensure_ascii=False))
    assert len(_compact(compact).encode()) <= MAX_CONTEXT_BYTES < len(spaced.encode())
    response = _send(route, client, headers, flag, experiment, spaced)
    assert _answer(response) == (422, {"detail": CONTEXT_TOO_LARGE})
    assert _rows(db_session, flag, experiment) == NOTHING
    assert spies == {"flags": [], "assignments": []}


@pytest.mark.parametrize("route", ["get-evaluate", "get-user"])
def test_a_get_context_within_the_limit_as_sent_and_over_it_compact_is_refused(
    route, client, headers, flag, experiment, spies, db_session
):
    """The parsed context is held to the limit too: ``1e9`` is three bytes as
    sent and twelve (``1000000000.0``) as compact JSON."""
    sent = Raw('{"tier":"premium","n":[' + ",".join(["1e9"] * 1000) + "]}")
    assert len(sent.encode()) <= MAX_CONTEXT_BYTES < len(_compact(json.loads(sent)))
    response = _send(route, client, headers, flag, experiment, sent)
    assert _answer(response) == (422, {"detail": CONTEXT_TOO_LARGE})
    assert _rows(db_session, flag, experiment) == NOTHING
    assert spies == {"flags": [], "assignments": []}


@pytest.mark.parametrize(
    "chain", [object_chain, array_chain], ids=["objects", "arrays"]
)
@pytest.mark.parametrize("route", ["get-evaluate", "post-evaluate", "post-assign"])
def test_exactly_the_depth_limit_is_served_and_one_level_more_is_refused(
    route, chain, client, headers, flag, experiment, spies, db_session
):
    served = _send(
        route, client, headers, flag, experiment, chain(MAX_CONTEXT_DEPTH, **MATCHING)
    )
    assert served.status_code == 200, served.text
    refused = _send(
        route,
        client,
        headers,
        flag,
        experiment,
        chain(MAX_CONTEXT_DEPTH + 1, **MATCHING),
        user="deeper",
    )
    assert _answer(refused) == (
        422,
        {"detail": CONTEXT_TOO_DEEP},
    )


# ---------------------------------------------------------------------------
# What is sent today is still served
# ---------------------------------------------------------------------------


def test_every_example_context_in_the_docs_is_served(client, headers, flag, experiment):
    found, _ = docs_example_contexts()
    assert len(found) >= 15
    for where, context in found:
        response = _send("post-evaluate", client, headers, flag, experiment, context)
        assert response.status_code == 200, (where, response.text)


def test_the_demo_apps_contexts_are_served_as_the_demo_apps_send_them(
    client, headers, flag, experiment
):
    """StreamPulse evaluates flags with GET; ShopLab assigns with POST."""
    devices = streampulse_contexts()
    for context in devices[:20] + devices[-5:]:
        response = _send(
            "get-evaluate", client, headers, flag, experiment, context, user="sp"
        )
        assert response.status_code == 200, response.text
    shoppers = shoplab_contexts()
    for n in range(0, len(shoppers), 50):
        response = _send(
            "post-assign",
            client,
            headers,
            flag,
            experiment,
            shoppers[n],
            user=f"sl-{n}",
        )
        assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# A real server
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_real_server_answers_the_clean_422(tmp_path, headers, flag, experiment):
    """uvicorn with h11, as the image runs it: the refusal is the route's 422
    with its detail, not a 400, a 500 or a dropped connection."""
    port = _free_port()
    log = (tmp_path / "uvicorn.log").open("w")
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.app.main:app",
            "--http",
            "h11",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO,
        env=dict(os.environ),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health/live", timeout=1
                )
                break
            except OSError:
                if server.poll() is not None or time.monotonic() > deadline:
                    pytest.fail(
                        "uvicorn did not start:\n"
                        + (tmp_path / "uvicorn.log").read_text()[-3000:]
                    )
                time.sleep(0.3)

        evaluate = f"/api/v1/feature-flags/evaluate/{flag.key}"
        over = sized(MAX_CONTEXT_BYTES + 1, **MATCHING)
        deep = array_chain(930, **MATCHING)
        requests = {
            "get size": ("GET", evaluate, {"params": {"context": _compact(over)}}),
            "get deep": ("GET", evaluate, {"params": {"context": _compact(deep)}}),
            "get too deep to parse": (
                "GET",
                evaluate,
                {"params": {"context": TOO_DEEP_TO_PARSE["arrays-1500"][0]}},
            ),
            "post size": ("POST", evaluate, {"json": {"context": over}}),
            "post deep": ("POST", evaluate, {"json": {"context": deep}}),
            "assign deep": (
                "POST",
                "/api/v1/tracking/assign",
                {"json": {"experiment_key": experiment.key, "context": deep}},
            ),
        }
        got = {}
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=30
        ) as http:
            served = http.post(evaluate, json={"user_id": "u", "context": MATCHING})
            assert served.status_code == 200, served.text
            for name, (method, url, kwargs) in requests.items():
                fields = kwargs["params"] if method == "GET" else kwargs["json"]
                fields["user_id"] = "u"
                try:
                    # One connection each: a server that fails a request may
                    # close the connection the next one would reuse.
                    got[name] = _answer(
                        http.request(
                            method, url, headers={"connection": "close"}, **kwargs
                        )
                    )
                except httpx.HTTPError as exc:
                    got[name] = ("no answer", type(exc).__name__)
    finally:
        server.terminate()
        server.wait(timeout=20)
        log.close()

    expected = {
        "get size": (422, {"detail": CONTEXT_TOO_LARGE}),
        "get deep": (422, {"detail": CONTEXT_TOO_DEEP}),
        "get too deep to parse": (422, {"detail": CONTEXT_TOO_DEEP}),
        "post size": (422, {"detail": CONTEXT_TOO_LARGE}),
        "post deep": (422, {"detail": CONTEXT_TOO_DEEP}),
        "assign deep": (422, {"detail": CONTEXT_TOO_DEEP}),
    }
    assert got == expected, got
