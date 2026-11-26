"""Setting a flag's split and its rules' own values over the real routes (#2012).

A DEVELOPER (not a superuser) writes through every route that can:

* ``POST /feature-flags/`` and ``PUT /feature-flags/{id}`` (production);
* ``PUT /feature-flags/{id}/environments/{environment}`` (beta), in production
  and in staging;
* ``POST /feature-flags/{id}/environments/{target}/promote`` (beta).

Each refuses what ``flag_values.serving_errors`` refuses, with the same 422 and
nothing stored; each stores what it accepts exactly, including a change only
JSON sees (``true`` for ``1``, ``1.0`` for ``1``), and names ``split`` in its
audit entry without what it holds. What is stored is served: ``/evaluate``
answers each user the value the ground-truth copy of the split's hash picks,
and a rule's own value to the users it matches. A save that changes one
weight keeps the order, and moves exactly the users the hash says. ANALYST and
VIEWER cannot set a split.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient
from pydantic_core import PydanticCustomError
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.flag_values import (
    MAX_SERVED_VALUE_BYTES,
    SERVED_VALUES_TOO_LARGE,
    served_value_bytes,
)
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.feature_flag import FeatureFlag, FlagEnvironmentSetting
from backend.app.models.user import UserRole
from backend.app.services import feature_flag_service
from backend.app.services.feature_flag_service import FlagValuesRefused
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from backend.tests.integration.key_environments import configure, environment_key

#: The ground-truth copy of the hash (standard library only).
_LIBRARY_ROOT = Path(__file__).resolve().parents[4] / "tests" / "ground_truth"
if str(_LIBRARY_ROOT) not in sys.path:
    sys.path.insert(0, str(_LIBRARY_ROOT))

from ground_truth import hashing

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SCHEMA = "test_experimentation"
FLAGS = "/api/v1/feature-flags"
USERS = [f"user-{n}" for n in range(300)]
PRO = {"plan": "pro"}
RULE = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}],
        }
    ],
}
SPLIT = [
    {"value": "red", "weight": 34},
    {"value": "green", "weight": 33},
    {"value": "blue", "weight": 33},
]
#: Weights that add up to 99: refused on every path.
SHORT = [{**SPLIT[0], "weight": 33}, *SPLIT[1:]]
SHORT_MESSAGE = "Weights must add up to 100; they add up to 99."


@pytest.fixture(autouse=True)
def _local(monkeypatch):
    local_sign_in(monkeypatch)
    assert deps.get_api_key not in app.dependency_overrides


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def developer(db_session):
    return make_user(db_session, UserRole.DEVELOPER, tag="splitw")


@pytest.fixture
def keys(db_session, developer) -> Dict[str, Dict[str, str]]:
    return {
        environment: environment_key(db_session, developer, environment)
        for environment in ("production", "staging")
    }


@pytest.fixture
def fresh(test_db):
    """A session of the test's own, opened per read, never the request's."""
    factory = sessionmaker(bind=test_db, expire_on_commit=False)

    def open_session():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return open_session


def _create(client, user, **fields) -> Dict[str, Any]:
    body = {
        "key": f"copy-{uuid.uuid4().hex[:10]}",
        "name": "Checkout copy",
        "value_type": "string",
        "on_value": "on",
        "off_value": "off",
        "is_active": True,
        "rollout_percentage": 100,
        **fields,
    }
    response = client.post(f"{FLAGS}/", json=body, headers=auth(user))
    assert response.status_code == 201, response.text
    return response.json()


def _stored(fresh, flag_id) -> Dict[str, Any]:
    """``{environment: (split, targeting_rules)}`` as the database holds them."""
    session = fresh()
    try:
        flag = session.get(FeatureFlag, flag_id)
        found = {"production": (flag.split, flag.targeting_rules)}
        rows = session.query(FlagEnvironmentSetting).filter(
            FlagEnvironmentSetting.feature_flag_id == flag_id
        )
        found.update({r.environment: (r.split, r.targeting_rules) for r in rows})
        return found
    finally:
        session.close()


def _stored_text(fresh, flag_id) -> Dict[str, Any]:
    """The split's JSON text as the database holds it, by environment: what
    Python's ``==`` cannot tell apart, the text can."""
    session = fresh()
    try:
        rows = session.execute(
            text(
                "SELECT 'production', split::text FROM feature_flags WHERE id = :id "
                "UNION ALL SELECT environment, split::text "
                "FROM flag_environment_settings WHERE feature_flag_id = :id"
            ),
            {"id": str(flag_id)},
        ).all()
        return dict(rows)
    finally:
        session.close()


def _entries(fresh, flag_id) -> List[AuditLog]:
    session = fresh()
    try:
        return (
            session.query(AuditLog)
            .filter(AuditLog.entity_id == flag_id)
            .order_by(AuditLog.timestamp)
            .all()
        )
    finally:
        session.close()


def _changed(entry: AuditLog) -> List[str]:
    return (json.loads(entry.new_value or "{}") or {}).get("changed_fields", [])


def _evaluate(client, key_headers, flag_key, user, context=None) -> Dict[str, Any]:
    params = {"user_id": user}
    if context is not None:
        params["context"] = json.dumps(context)
    answer = client.get(
        f"{FLAGS}/evaluate/{flag_key}", params=params, headers=key_headers
    )
    assert answer.status_code == 200, answer.text
    return answer.json()


def _served(client, key_headers, flag_key, context=None) -> Dict[str, Any]:
    return {
        u: _evaluate(client, key_headers, flag_key, u, context)["value"] for u in USERS
    }


def _put(client, user, flag_id, body):
    return client.put(f"{FLAGS}/{flag_id}", json=body, headers=auth(user))


def _put_in(client, user, flag_id, environment, body):
    return client.put(
        f"{FLAGS}/{flag_id}/environments/{environment}", json=body, headers=auth(user)
    )


def _promote(client, user, flag_id, source, target):
    return client.post(
        f"{FLAGS}/{flag_id}/environments/{target}/promote",
        json={"from": source},
        headers=auth(user),
    )


def _refusals(response) -> List[Any]:
    assert response.status_code == 422, response.text
    return [(e["type"], e["loc"], e["msg"]) for e in response.json()["detail"]]


# ---------------------------------------------------------------------------
# One check on every write path
# ---------------------------------------------------------------------------

WRITE_PATHS = [
    "create",
    "put",
    "environment-put-staging",
    "environment-put-production",
    "promote",
]


def _write(path, client, user, db_session, split):
    """Write *split* by *path*; ``(response, flag_id or None)``."""
    if path == "create":
        body = {
            "key": f"copy-{uuid.uuid4().hex[:10]}",
            "name": "Checkout copy",
            "value_type": "string",
            "on_value": "on",
            "off_value": "off",
            "split": split,
        }
        return client.post(f"{FLAGS}/", json=body, headers=auth(user)), None
    flag = _create(client, user)
    if path == "put":
        return _put(client, user, flag["id"], {"split": split}), flag["id"]
    if path == "environment-put-staging":
        return _put_in(client, user, flag["id"], "staging", {"split": split}), flag[
            "id"
        ]
    if path == "environment-put-production":
        return _put_in(client, user, flag["id"], "production", {"split": split}), flag[
            "id"
        ]
    # A promotion copies what staging stores; only a direct write could store
    # a split no route accepts.
    row = db_session.get(FeatureFlag, uuid.UUID(flag["id"]))
    configure(db_session, row, "staging", split=split)
    return _promote(client, user, flag["id"], "staging", "production"), flag["id"]


@pytest.mark.regression
@pytest.mark.parametrize("path", WRITE_PATHS)
def test_every_write_path_refuses_the_same_split_with_the_same_answer(
    client, db_session, fresh, developer, path
):
    response, flag_id = _write(path, client, developer, db_session, SHORT)
    assert _refusals(response) == [("split_total", ["body", "split"], SHORT_MESSAGE)]
    if flag_id is not None:
        stored = _stored(fresh, uuid.UUID(flag_id))
        assert stored["production"] == (None, None)
        if path != "promote":
            assert stored.get("staging", (None, None)) == (None, None)


@pytest.mark.parametrize("path", WRITE_PATHS)
def test_every_write_path_stores_what_it_accepts_as_sent(
    client, db_session, fresh, developer, path
):
    response, flag_id = _write(path, client, developer, db_session, SPLIT)
    assert response.status_code in (200, 201), response.text
    flag_id = flag_id or response.json()["id"]
    target = "staging" if path == "environment-put-staging" else "production"
    other = "production" if target == "staging" else "staging"
    stored = _stored(fresh, uuid.UUID(flag_id))
    assert stored[target][0] == SPLIT
    if path != "promote":
        assert stored.get(other, (None, None))[0] is None
    assert response.json()["split"] == SPLIT
    # Audited by name on every path but the create, which records the flag's
    # identifying fields; never the split's values.
    entries = _entries(fresh, uuid.UUID(flag_id))
    named = [e.action_type for e in entries if "split" in _changed(e)]
    assert named if path != "create" else not named, (path, named)
    for entry in entries:
        assert "green" not in (entry.new_value or "") + (entry.old_value or ""), path


def test_the_responses_carry_the_split_in_every_environment(client, developer):
    flag = _create(client, developer, split=SPLIT)
    assert flag["split"] == SPLIT
    assert (
        client.get(f"{FLAGS}/{flag['id']}", headers=auth(developer)).json()["split"]
        == SPLIT
    )
    listed = client.get(
        f"{FLAGS}/", params={"search": flag["key"]}, headers=auth(developer)
    ).json()["items"]
    assert [i["split"] for i in listed if i["id"] == flag["id"]] == [SPLIT]
    staging = [
        {**SPLIT[0], "weight": 50},
        {**SPLIT[1], "weight": 50},
        {**SPLIT[2], "weight": 0},
    ]
    assert (
        _put_in(client, developer, flag["id"], "staging", {"split": staging}).json()[
            "split"
        ]
        == staging
    )
    every = client.get(f"{FLAGS}/{flag['id']}/environments", headers=auth(developer))
    assert {e["environment"]: e["split"] for e in every.json()["environments"]} == {
        "production": SPLIT,
        "staging": staging,
        "development": None,
    }
    many = client.get(
        "/api/v1/environments/staging/feature-flag-settings",
        params={"flag_id": flag["id"]},
        headers=auth(developer),
    )
    assert many.json()["settings"][flag["id"]]["split"] == staging


def test_a_get_body_sent_back_changes_nothing(client, fresh, developer):
    flag = _create(
        client, developer, split=SPLIT, targeting_rules={**RULE, "value": "pro"}
    )
    before = _stored(fresh, uuid.UUID(flag["id"]))
    body = client.get(f"{FLAGS}/{flag['id']}", headers=auth(developer)).json()
    assert _put(client, developer, flag["id"], body).status_code == 200
    assert _stored(fresh, uuid.UUID(flag["id"])) == before
    boolean = client.post(
        f"{FLAGS}/",
        json={"key": f"b-{uuid.uuid4().hex[:8]}", "name": "B"},
        headers=auth(developer),
    ).json()
    assert boolean["split"] is None
    assert _put(client, developer, boolean["id"], boolean).status_code == 200


def test_null_clears_the_split_and_leaving_it_out_keeps_it(client, fresh, developer):
    flag = _create(client, developer, split=SPLIT)
    assert (
        _put(client, developer, flag["id"], {"rollout_percentage": 50}).json()["split"]
        == SPLIT
    )
    assert _put(client, developer, flag["id"], {"split": None}).json()["split"] is None
    assert _stored(fresh, uuid.UUID(flag["id"]))["production"][0] is None


def test_a_put_counts_the_stored_split_with_the_rules_values_it_sends(
    client, fresh, developer
):
    largest = [{"value": f"{n}".ljust(8190, "x"), "weight": 10} for n in range(10)]
    flag = _create(client, developer, split=largest)
    response = _put(
        client, developer, flag["id"], {"targeting_rules": {**RULE, "value": "a"}}
    )
    assert _refusals(response) == [
        (
            "served_values_too_large",
            ["body", "targeting_rules"],
            SERVED_VALUES_TOO_LARGE,
        )
    ]
    assert _stored(fresh, uuid.UUID(flag["id"]))["production"][1] is None


#: Ten values of 8000 characters: with a rule value of 5000 they take more than
#: the 81920 bytes a split and the rules' own values may take together.
LARGE_SPLIT = [{"value": f"{n}".ljust(8000, "x"), "weight": 10} for n in range(10)]
LARGE_RULE = {**RULE, "value": "r" * 5000}


ENVIRONMENTS = ("production", "staging", "development")


def _set_in(client, user, flag_id, environment, body):
    if environment == "production":
        response = _put(client, user, flag_id, body)
    else:
        response = _put_in(client, user, flag_id, environment, body)
    assert response.status_code == 200, response.text


def _fits(response, fresh, flag_id, environment):
    """*response* answered 200, and *environment* stores no split and the
    large rule's value."""
    assert response.status_code == 200, response.text
    assert response.json()["split"] is None
    assert response.json()["targeting_rules"]["value"] == LARGE_RULE["value"]
    split, rules = _stored(fresh, uuid.UUID(flag_id))[environment]
    assert (split, rules["value"]) == (None, LARGE_RULE["value"])


@pytest.mark.regression
@pytest.mark.parametrize(
    "route", ["put", "environment-put-production", "environment-put-staging"]
)
def test_a_change_counts_the_split_it_writes_with_the_rules(
    client, fresh, developer, route
):
    """Clearing a large split while sending rules with a large value of their
    own fits on every route. The environment PUT to production hands the split
    to the flag update beside the request; the update counted the rules with
    the split it was replacing, refused what the route had accepted, and
    answered 500."""
    environment = "staging" if route.endswith("staging") else "production"
    flag = _create(client, developer)
    _set_in(client, developer, flag["id"], environment, {"split": LARGE_SPLIT})
    body = {"split": None, "targeting_rules": LARGE_RULE}
    if route == "put":
        response = _put(client, developer, flag["id"], body)
    else:
        response = _put_in(client, developer, flag["id"], environment, body)
    _fits(response, fresh, flag["id"], environment)


@pytest.mark.regression
@pytest.mark.parametrize(
    "source,target",
    [(s, t) for s in ENVIRONMENTS for t in ENVIRONMENTS if s != t],
)
def test_a_promotion_counts_the_split_it_copies_with_the_rules(
    client, fresh, developer, source, target
):
    """A promotion from an environment with no split and rules with a large
    value of their own, into one with a large split, fits, from and into every
    environment. Into production it answered 500, as the environment PUT did."""
    flag = _create(client, developer)
    _set_in(client, developer, flag["id"], target, {"split": LARGE_SPLIT})
    _set_in(client, developer, flag["id"], source, {"targeting_rules": LARGE_RULE})
    response = _promote(client, developer, flag["id"], source, target)
    _fits(response, fresh, flag["id"], target)


@pytest.mark.regression
@pytest.mark.parametrize("route", ["environment-put", "promote"])
def test_a_refusal_by_the_update_is_the_422_of_a_flag_put_on_production(
    client, fresh, developer, monkeypatch, route
):
    """Should the update refuse a change to production that the route let
    through, the answer is the 422 ``PUT /feature-flags/{id}`` gives, and
    nothing is stored; it was a 500."""
    flag = _create(client, developer)
    rules = {**RULE, "value": "pro"}
    if route == "promote":
        staged = _put_in(
            client, developer, flag["id"], "staging", {"targeting_rules": rules}
        )
        assert staged.status_code == 200, staged.text
    refusal = FlagValuesRefused(
        [
            (
                "targeting_rules",
                PydanticCustomError("served_values_too_large", SERVED_VALUES_TOO_LARGE),
            )
        ]
    )

    def refuse(*args, **kwargs):
        raise refusal

    monkeypatch.setattr(feature_flag_service, "_check_sent_values", refuse)
    stable = _put(client, developer, flag["id"], {"targeting_rules": rules})
    if route == "environment-put":
        response = _put_in(
            client, developer, flag["id"], "production", {"targeting_rules": rules}
        )
    else:
        response = _promote(client, developer, flag["id"], "staging", "production")
    assert _refusals(stable) == [
        (
            "served_values_too_large",
            ["body", "targeting_rules"],
            SERVED_VALUES_TOO_LARGE,
        )
    ]
    assert (response.status_code, response.json()) == (422, stable.json())
    assert _stored(fresh, uuid.UUID(flag["id"]))["production"] == (None, None)


def _until_it_waits_for_a_lock_or_ends(engine, request, seconds=30.0):
    """Block until some backend of this database waits on a lock, or the
    *request* thread has ended, so the order is decided by PostgreSQL."""
    deadline = time.monotonic() + seconds
    with engine.connect() as probe:
        while time.monotonic() < deadline and request.is_alive():
            waiting = probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar()
            probe.commit()
            if waiting:
                return
            time.sleep(0.05)
    assert not request.is_alive(), "the PUT neither waited nor ended"


@pytest.mark.regression
def test_a_split_and_rules_sent_at_once_never_store_more_than_the_limit(
    client, db_session, fresh, developer, monkeypatch
):
    """Two PUTs at once, one sending a large split and one rules with a large
    value of their own: the second waits for the first, is counted with the
    split it stored, and is refused; the flag never holds more than the two may
    take together."""
    flag = _create(client, developer)
    checked, release = threading.Event(), threading.Event()
    real_check = feature_flag_service._check_sent_values

    def check_then_hold(flag_data, row, *split):
        real_check(flag_data, row, *split)
        if "split" in flag_data.model_fields_set:
            checked.set()
            assert release.wait(60)

    monkeypatch.setattr(feature_flag_service, "_check_sent_values", check_then_hold)
    answers: Dict[str, Any] = {}

    def put(name, body):
        # A client of its own per request: each runs on its own event loop.
        answers[name] = TestClient(app, raise_server_exceptions=False).put(
            f"{FLAGS}/{flag['id']}", json=body, headers=auth(developer)
        )

    first = threading.Thread(target=put, args=("split", {"split": LARGE_SPLIT}))
    second = threading.Thread(
        target=put, args=("rules", {"targeting_rules": LARGE_RULE})
    )
    first.start()
    try:
        assert checked.wait(60), "the split was never checked"
        second.start()
        _until_it_waits_for_a_lock_or_ends(db_session.get_bind(), second)
    finally:
        release.set()
        first.join(60)
        if second.ident is not None:
            second.join(60)
    assert answers["split"].status_code == 200, answers["split"].text
    assert _refusals(answers["rules"]) == [
        (
            "served_values_too_large",
            ["body", "targeting_rules"],
            SERVED_VALUES_TOO_LARGE,
        )
    ]
    split, rules = _stored(fresh, uuid.UUID(flag["id"]))["production"]
    assert (split, rules) == (LARGE_SPLIT, None)
    assert served_value_bytes(split, rules) <= MAX_SERVED_VALUE_BYTES


# ---------------------------------------------------------------------------
# What is stored is served
# ---------------------------------------------------------------------------


def test_a_split_set_through_the_api_is_served_by_the_hash(client, developer, keys):
    """Each user gets the value the ground-truth copy of the split's hash
    picks; the flag at 100% gives every value to someone."""
    flag = _create(client, developer, split=SPLIT)
    served = _served(client, keys["production"], flag["key"])
    expected = {u: hashing.split_value(u, flag["key"], SPLIT) for u in USERS}
    assert served == expected
    assert set(served.values()) == {"red", "green", "blue"}


def test_a_rule_set_through_the_api_serves_its_own_value(client, developer, keys):
    flag = _create(
        client,
        developer,
        split=SPLIT,
        targeting_rules={**RULE, "value": "pro-copy", "rollout_percentage": 100},
    )
    assert set(_served(client, keys["production"], flag["key"], PRO).values()) == {
        "pro-copy"
    }
    free = _served(client, keys["production"], flag["key"], {"plan": "free"})
    assert free == {u: hashing.split_value(u, flag["key"], SPLIT) for u in USERS}
    native = {
        "rules": [
            {
                "id": "pro",
                "rule": {
                    "operator": "and",
                    "conditions": [
                        {"attribute": "plan", "operator": "eq", "value": "pro"}
                    ],
                },
                "value": "native-pro",
            }
        ]
    }
    assert (
        _put(client, developer, flag["id"], {"targeting_rules": native}).status_code
        == 200
    )
    assert set(_served(client, keys["production"], flag["key"], PRO).values()) == {
        "native-pro"
    }


def test_a_staging_split_set_through_the_api_is_served_by_staging_alone(
    client, developer, keys
):
    flag = _create(client, developer, split=SPLIT)
    staging = [SPLIT[2], SPLIT[0], SPLIT[1]]
    body = {"split": staging, "is_active": True, "rollout_percentage": 100}
    assert _put_in(client, developer, flag["id"], "staging", body).status_code == 200
    assert _served(client, keys["staging"], flag["key"]) == {
        u: hashing.split_value(u, flag["key"], staging) for u in USERS
    }
    assert _served(client, keys["production"], flag["key"]) == {
        u: hashing.split_value(u, flag["key"], SPLIT) for u in USERS
    }


def test_a_staging_rule_value_set_through_the_api_is_served_by_staging_alone(
    client, developer, keys
):
    """The values a flag's rules serve are each environment's: the same rule
    serves its staging value in staging and its production value in
    production, and a user it does not match gets the on value in both."""
    flag = _create(
        client,
        developer,
        targeting_rules={**RULE, "value": "production-pro", "rollout_percentage": 100},
    )
    body = {
        "is_active": True,
        "rollout_percentage": 100,
        "targeting_rules": {**RULE, "value": "staging-pro", "rollout_percentage": 100},
    }
    assert _put_in(client, developer, flag["id"], "staging", body).status_code == 200
    served = {
        environment: {
            plan: {
                _evaluate(client, keys[environment], flag["key"], u, context)["value"]
                for u in USERS[:40]
            }
            for plan, context in (("pro", PRO), ("free", {"plan": "free"}))
        }
        for environment in ("staging", "production")
    }
    assert served == {
        "staging": {"pro": {"staging-pro"}, "free": {"on"}},
        "production": {"pro": {"production-pro"}, "free": {"on"}},
    }


@pytest.mark.regression
def test_a_save_that_changes_one_weight_keeps_the_order_and_moves_only_neighbours(
    client, developer, keys
):
    """34/33/33 to 44/23/33 moves the boundary between red and green alone:
    exactly the users the hash puts in buckets 34 to 43 go from green to red,
    and nobody else moves."""
    flag = _create(client, developer, split=SPLIT)
    before = _served(client, keys["production"], flag["key"])
    changed = [{**SPLIT[0], "weight": 44}, {**SPLIT[1], "weight": 23}, SPLIT[2]]
    answer = _put(client, developer, flag["id"], {"split": changed})
    assert answer.json()["split"] == changed
    after = _served(client, keys["production"], flag["key"])
    moved = {u: (before[u], after[u]) for u in USERS if before[u] != after[u]}
    expected = {
        u: ("green", "red")
        for u in USERS
        if 34 <= hashing.split_bucket(u, flag["key"]) < 44
    }
    assert moved == expected
    assert len(expected) > 0


# ---------------------------------------------------------------------------
# A change only JSON sees is stored and named
# ---------------------------------------------------------------------------

JSON_SPLIT = [
    {"value": {"color": "red", "bold": True}, "weight": 34},
    {"value": {"color": "green"}, "weight": 33},
    {"value": {"color": "blue", "size": 1}, "weight": 33},
]
#: Equal to JSON_SPLIT for Python's ``==``, different JSON: ``1`` for ``true``
#: and ``1.0`` for ``1``.
JSON_TWIN = [
    {"value": {"color": "red", "bold": 1}, "weight": 34},
    JSON_SPLIT[1],
    {"value": {"color": "blue", "size": 1.0}, "weight": 33},
]


def _json_flag(client, user):
    return _create(
        client,
        user,
        value_type="json",
        on_value={"on": True},
        off_value={"off": True},
        split=JSON_SPLIT,
    )


@pytest.mark.regression
@pytest.mark.parametrize("route", ["put", "environment-put"])
def test_a_split_that_differs_only_as_json_is_stored_and_named_by_a_put(
    client, fresh, developer, route
):
    assert JSON_TWIN == JSON_SPLIT
    flag = _json_flag(client, developer)
    flag_id = uuid.UUID(flag["id"])
    before = _stored_text(fresh, flag_id)["production"]
    if route == "put":
        response = _put(client, developer, flag["id"], {"split": JSON_TWIN})
    else:
        response = _put_in(
            client, developer, flag["id"], "production", {"split": JSON_TWIN}
        )
    assert response.status_code == 200, response.text
    after = _stored_text(fresh, flag_id)["production"]
    assert after != before
    assert '"bold": 1' in after and '"size": 1.0' in after
    latest = _entries(fresh, flag_id)[-1]
    assert latest.action_type == "feature_flag_update"
    assert _changed(latest) == ["split"]
    assert "blue" not in (latest.new_value or "") + (latest.old_value or "")


@pytest.mark.regression
@pytest.mark.parametrize("target", ["production", "staging"])
def test_a_split_that_differs_only_as_json_is_stored_and_named_by_a_promotion(
    client, db_session, fresh, developer, target
):
    source = "development"
    flag = _json_flag(client, developer)
    row = db_session.get(FeatureFlag, uuid.UUID(flag["id"]))
    configure(db_session, row, "staging", split=JSON_SPLIT)
    configure(db_session, row, source, split=JSON_TWIN)
    flag_id = uuid.UUID(flag["id"])
    response = _promote(client, developer, flag["id"], source, target)
    assert response.status_code == 200, response.text
    stored = _stored_text(fresh, flag_id)
    assert stored[target] == stored[source]
    assert '"bold": 1' in stored[target]
    promoted = [
        e for e in _entries(fresh, flag_id) if e.action_type == "feature_flag_promote"
    ]
    assert len(promoted) == 1
    assert "split" in _changed(promoted[0])
    assert "blue" not in (promoted[0].new_value or "") + (promoted[0].old_value or "")


@pytest.mark.regression
def test_rules_that_differ_only_as_json_are_stored_and_named_by_a_put(
    client, fresh, developer
):
    """The same holds for every JSON column a PUT writes: before, rules whose
    one change was ``1`` for ``true`` answered 200, were not stored, and the
    entry said they changed."""
    rules = {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "beta", "operator": "equals", "value": True}
                ],
            }
        ],
    }
    twin = json.loads(json.dumps(rules).replace("true", "1"))
    assert twin == rules
    flag = _create(client, developer, targeting_rules=rules)
    response = _put(client, developer, flag["id"], {"targeting_rules": twin})
    assert response.status_code == 200, response.text
    session = fresh()
    try:
        stored = session.execute(
            text("SELECT targeting_rules::text FROM feature_flags WHERE id = :id"),
            {"id": flag["id"]},
        ).scalar_one()
    finally:
        session.close()
    assert '"value": 1' in stored, stored
    latest = _entries(fresh, uuid.UUID(flag["id"]))[-1]
    assert _changed(latest) == ["targeting_rules"]


# ---------------------------------------------------------------------------
# Audit and permissions
# ---------------------------------------------------------------------------


def test_a_create_records_the_flag_and_never_the_splits_values(
    client, fresh, developer
):
    flag = _create(
        client, developer, split=SPLIT, targeting_rules={**RULE, "value": "pro"}
    )
    (entry,) = [
        e
        for e in _entries(fresh, uuid.UUID(flag["id"]))
        if e.action_type == "feature_flag_create"
    ]
    assert "green" not in (entry.new_value or "") and "pro" not in json.loads(
        entry.new_value
    ).get("targeting_rules", "")


@pytest.mark.parametrize("role", [UserRole.ANALYST, UserRole.VIEWER])
def test_analysts_and_viewers_cannot_set_a_split(
    client, db_session, fresh, developer, role
):
    flag = _create(client, developer)
    reader = make_user(db_session, role, tag="splitr")
    assert _put(client, reader, flag["id"], {"split": SPLIT}).status_code == 403
    for environment in ("production", "staging"):
        response = _put_in(client, reader, flag["id"], environment, {"split": SPLIT})
        assert response.status_code == 403, response.text
    assert _stored(fresh, uuid.UUID(flag["id"])) == {"production": (None, None)}


# ---------------------------------------------------------------------------
# A rule's own value holding a character the database cannot store
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("route", ["create", "put", "environment-put"])
def test_a_rule_value_holding_nul_answers_422_naming_the_field(
    client, fresh, developer, route
):
    rules = {**RULE, "value": "pro\u0000copy"}
    if route == "create":
        response = client.post(
            f"{FLAGS}/",
            json={
                "key": f"nul-{uuid.uuid4().hex[:8]}",
                "name": "N",
                "value_type": "string",
                "on_value": "on",
                "off_value": "off",
                "targeting_rules": rules,
            },
            headers=auth(developer),
        )
    else:
        flag = _create(client, developer)
        if route == "put":
            response = _put(client, developer, flag["id"], {"targeting_rules": rules})
        else:
            response = _put_in(
                client, developer, flag["id"], "staging", {"targeting_rules": rules}
            )
        assert _stored(fresh, uuid.UUID(flag["id"])) == {"production": (None, None)}
    refusals = _refusals(response)
    assert [loc for _, loc, _ in refusals] == [["body", "targeting_rules"]]
    assert "NUL" in refusals[0][2] and "copy" not in refusals[0][2]
