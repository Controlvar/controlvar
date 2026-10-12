"""Evaluating a flag with a context value out of range for a comparison.

A context value that is out of range for a rule's comparison (an integer too
large for a float, ``Infinity`` or a timestamp no date can hold, for a date)
makes the condition unevaluable: the flag answers ``enabled: false`` with
reason ``error``, as before, handled like a pattern condition that cannot be
evaluated. A value in range, and every value for a comparison it is not out of
range for, answers with its usual reason. Covered through ``GET`` (the ``context`` query
parameter, which may carry a literal ``Infinity``) and ``POST``
``/api/v1/feature-flags/evaluate/{flag_key}``, with a key without any scope,
for the eleven operators in dashboard-shape and native rules, and one
legacy-list rule. Keys are created through ``POST /api/v1/api-keys`` and
``deps.get_api_key`` is not replaced.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog

pytestmark = [pytest.mark.integration, pytest.mark.regression]

SCHEMA = "test_experimentation"
BIG = "1" + "0" * 399  # a 400-digit integer, as JSON text
HUGE = "1" + "0" * 3999  # a 4000-digit integer


def _dashboard(operator, value):
    return {
        "logical_operator": "or",
        "groups": [
            {
                "logical_operator": "and",
                "conditions": [
                    {"attribute": "v", "operator": operator, "value": value}
                ],
            }
        ],
    }


def _native(operator, value, additional=None):
    condition = {"attribute": "v", "operator": operator, "value": value}
    if additional is not None:
        condition["additional_value"] = additional
    return {
        "version": "1.0",
        "rules": [
            {
                "id": f"rule-{operator}",
                "rule": {"operator": "and", "conditions": [condition]},
                "rollout_percentage": 100,
            }
        ],
    }


#: Indexes into CONTEXTS: the three integers too large for a float, and every
#: value no date can hold (the integers, both infinities, 1e20, and 1e17, whose
#: year the C library cannot hold).
NUMBERS = frozenset({0, 1, 2})
DATES = frozenset({0, 1, 2, 3, 4, 5, 7})

#: name -> (stored rules, the CONTEXTS that are out of range for them)
RULES = {
    "equals": (_dashboard("equals", "18"), NUMBERS),
    "not_equals": (_dashboard("not_equals", "1234"), NUMBERS),
    "in": (_dashboard("in", ["18", "pro"]), NUMBERS),
    "not_in": (_dashboard("not_in", ["18", "pro"]), NUMBERS),
    "greater_than": (_dashboard("greater_than", "18"), NUMBERS),
    "greater_than_or_equal": (_dashboard("greater_than_or_equal", "18"), NUMBERS),
    "less_than": (_dashboard("less_than", "18"), NUMBERS),
    "less_than_or_equal": (_dashboard("less_than_or_equal", "18"), NUMBERS),
    "before": (_native("before", "2026-01-01T00:00:00"), DATES),
    "after": (_native("after", "2026-01-01T00:00:00"), DATES),
    "between-numbers": (_native("between", "1", "100"), NUMBERS),
    "between-dates": (
        _native("between", "2020-01-01T00:00:00", "2030-01-01T00:00:00"),
        DATES,
    ),
    # Legacy gt/lt against a stored value that is not a number: no match.
    "legacy-list": (
        [
            {
                "type": "context",
                "conditions": [
                    {"attribute": "v", "operator": "gt", "value": "18"},
                    {"attribute": "v", "operator": "lt", "value": "abc"},
                ],
            }
        ],
        frozenset(),
    ),
}

#: Raw JSON contexts, so that ``Infinity`` is sent literally.
CONTEXTS = [
    '{"v": ' + BIG + "}",
    '{"v": -' + BIG + "}",
    '{"v": ' + HUGE + "}",
    '{"v": Infinity}',
    '{"v": -Infinity}',
    '{"v": 1e20}',
    '{"v": 30}',
    '{"v": 1e17}',
]


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


@pytest.fixture
def client(db_session):
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

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
def plain_key(client, viewer_user) -> str:
    resp = client.post(
        "/api/v1/api-keys",
        json={"name": f"k-{uuid.uuid4().hex[:6]}"},
        headers={"Authorization": f"Bearer {create_local_access_token(viewer_user)}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


@pytest.mark.parametrize("name", list(RULES))
def test_an_out_of_range_value_answers_off_with_reason_error(
    client, db_session, make_feature_flag, plain_key, name
):
    rules, out_of_range = RULES[name]
    flag = make_feature_flag(
        key=f"range-{name}-{uuid.uuid4().hex[:6]}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
        targeting_rules=rules,
    )
    headers = {"X-API-Key": plain_key}
    wrong = []
    for index, raw in zip(range(len(CONTEXTS)), CONTEXTS):
        got = client.get(
            f"/api/v1/feature-flags/evaluate/{flag.key}",
            params={"user_id": "u1", "context": raw},
            headers=headers,
        )
        posted = client.post(
            f"/api/v1/feature-flags/evaluate/{flag.key}",
            content='{"user_id": "u1", "context": ' + raw + "}",
            headers={**headers, "content-type": "application/json"},
        )
        for method, resp in (("GET", got), ("POST", posted)):
            assert resp.status_code == 200, (method, raw[:20], resp.text[:200])
            body = resp.json()
            if index in out_of_range:
                ok = body["enabled"] is False and body["reason"] == "error"
            else:
                ok = body["reason"] != "error"
            if not ok:
                wrong.append((method, raw[:12], body["enabled"], body["reason"]))
    assert wrong == [], wrong

    db_session.expire_all()
    rows = (
        db_session.query(func.count(ErrorLog.id))
        .filter(
            ErrorLog.feature_flag_id == flag.id,
            ErrorLog.error_type == "flag_evaluation_error",
        )
        .scalar()
    )
    assert rows == 0


def test_the_contexts_reach_the_rules_as_numbers():
    """Guard the probe: the raw contexts parse to the out-of-range numbers."""
    parsed = [json.loads(raw)["v"] for raw in CONTEXTS]
    assert parsed[0] == 10**399 and parsed[2] == 10**3999
    assert parsed[3] == float("inf") and parsed[4] == float("-inf")
