"""The published Python local evaluators read what ``GET /api/v1/sdk/ruleset``
serves from a real database (#1497, G2).

``tests/sdk-contract/test_published_ruleset.py`` (G1) feeds every published
release with a local evaluator the generator's in-memory build of the ruleset.
This test feeds the published ``controlvar`` wheels (0.1.1 and 0.2.0, pinned in
``tests/sdk-contract/published-sdks.json``) the route's own response bytes, for
flags a non-superuser DEVELOPER created through the API in every shape the
ruleset serves today: a plain rollout, dashboard rules, native rules with a
``default_rule``, the legacy list, a pattern (regex) rule and a segment rule
(the last three answered by the server), an inactive flag and an archived one.
The API has refused the legacy list on write since #743, so that one flag is
created through the API and its rules written as a row from before #743 was.

* The route answers 200 with exactly one entry per stored flag.
* Each wheel runs in its own ``python -I -S`` process with only its verified
  extraction on ``sys.path`` (PF-8); no published ``controlvar`` is imported into
  this process. Identity is checked there: the digest before unpacking, and
  every ``controlvar`` module it loaded inside the extraction.
* Its ``LocalEvaluation`` loads each response in turn. Every answer it gives
  locally is exactly ``/evaluate``'s, the server-evaluated flags defer, and both
  kill switches reach it: ``POST /feature-flags/{id}/deactivate``, and a safety
  rollback to 0% (which turns the flag off).
* In server mode its ``get_feature_flag``, ``is_feature_enabled`` and
  ``get_all_flags`` read the same answers as ``/evaluate`` and the bulk route,
  and every bulk value is a boolean.

It needs the network (the wheels come from PyPI); there is no skip.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.segment import Segment, SegmentKind, SegmentStatus
from backend.app.models.user import User, UserRole

REPO_ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location(
    "published_sdks_for_g2", REPO_ROOT / "tests" / "sdk-contract" / "published_sdks.py"
)
ps = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ps  # dataclasses look their module up here
_spec.loader.exec_module(ps)

pytestmark = [pytest.mark.integration, pytest.mark.regression]
log = logging.getLogger("published-sdk-gate")

RULESET = "/api/v1/sdk/ruleset"
FLAGS = "/api/v1/feature-flags"
PYTHON_ARTEFACTS = [a for a in ps.load_pins() if a.language == "python"]
USERS = [f"user-{n}" for n in range(1, 7)]
CONTEXTS = [None, {"plan": "pro"}, {"plan": "free", "email": "abc@example.com"}]

#: The shapes, as the API stores them, and what the ruleset makes of each:
#: "local" and "remote" are active entries; None is an inactive entry.
SHAPES = {
    "rollout": ({"is_active": True, "rollout_percentage": 100}, "local"),
    "dashboard": (
        {
            "is_active": True,
            "rollout_percentage": 0,
            "targeting_rules": {
                "logical_operator": "AND",
                "groups": [
                    {
                        "logical_operator": "AND",
                        "conditions": [
                            {"attribute": "plan", "operator": "equals", "value": "pro"}
                        ],
                    }
                ],
            },
        },
        "local",
    ),
    "native": (
        {
            "is_active": True,
            "rollout_percentage": 0,
            "targeting_rules": {
                "rules": [
                    {
                        "id": "pro",
                        "rule": {
                            "operator": "and",
                            "conditions": [
                                {"attribute": "plan", "operator": "eq", "value": "pro"}
                            ],
                            "groups": [],
                        },
                        "rollout_percentage": 100,
                        "priority": 0,
                    }
                ],
                "default_rule": {
                    "id": "fallback",
                    "rule": {"operator": "and", "conditions": [], "groups": []},
                    "rollout_percentage": 40,
                    "priority": 0,
                },
            },
        },
        "local",
    ),
    "legacy": (
        {
            "is_active": True,
            "rollout_percentage": 30,
            "targeting_rules": [{"type": "user_id", "user_ids": ["user-1"]}],
        },
        "remote",
    ),
    "pattern": (
        {
            "is_active": True,
            "rollout_percentage": 100,
            "targeting_rules": {
                "logical_operator": "AND",
                "rollout_percentage": 0,
                "groups": [
                    {
                        "logical_operator": "AND",
                        "conditions": [
                            {"attribute": "email", "operator": "regex", "value": "^ab"}
                        ],
                    }
                ],
            },
        },
        "remote",
    ),
    "segment": (
        {
            "is_active": True,
            "rollout_percentage": 0,
            "targeting_rules": {
                "logical_operator": "AND",
                "groups": [
                    {
                        "logical_operator": "AND",
                        "conditions": [
                            {
                                "attribute": "segment",
                                "operator": "in_segment",
                                "value": "{segment}",
                            }
                        ],
                    }
                ],
            },
        },
        "remote",
    ),
    "inactive": ({"is_active": False, "rollout_percentage": 100}, None),
    "archived": ({"is_active": True, "rollout_percentage": 100}, None),
}
INACTIVE = {"enabled": False, "reason": "inactive"}


@pytest.fixture(autouse=True)
def _local_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


def _make_user(db_session, role, is_superuser=False):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"g2_{role.value}_{suffix}",
        email=f"g2_{role.value}_{suffix}@published-sdk.test",
        full_name="Published SDK gate",
        hashed_password=get_password_hash("Demo1234!"),
        is_active=True,
        is_superuser=is_superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def client(db_session):
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        session = factory()
        session.execute(text("SET search_path TO test_experimentation"))
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


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


@pytest.fixture(scope="module")
def artefact_cache(tmp_path_factory):
    return tmp_path_factory.mktemp("published-wheels")


@pytest.fixture
def stored(client, db_session):
    """The flags, created through the API by a non-superuser DEVELOPER."""
    developer = _make_user(db_session, UserRole.DEVELOPER)
    assert not developer.is_superuser
    segment = Segment(
        id=uuid.uuid4(),
        name=f"G2 segment {uuid.uuid4().hex[:6]}",
        kind=SegmentKind.ID_LIST.value,
        status=SegmentStatus.ACTIVE,
        owner_id=developer.id,
    )
    db_session.add(segment)
    db_session.commit()
    suffix = uuid.uuid4().hex[:8]
    flags = {}
    for shape, (body, _) in SHAPES.items():
        body = json.loads(json.dumps(body).replace("{segment}", str(segment.id)))
        key = f"g2-{shape}-{suffix}"
        legacy = body.pop("targeting_rules") if shape == "legacy" else None
        if legacy is not None:
            # The API has refused the legacy list on write since #743; rows
            # stored before that are still served (as remote entries). The flag
            # is created through the API and its rules set the way such a row
            # was stored.
            refused = client.post(
                f"{FLAGS}/",
                json={"key": key, "name": "G2 legacy", "targeting_rules": legacy},
                headers=_auth(developer),
            )
            assert refused.status_code == 422, refused.text
        response = client.post(
            f"{FLAGS}/",
            json={"key": key, "name": f"G2 {shape}", **body},
            headers=_auth(developer),
        )
        assert response.status_code == 201, (shape, response.text)
        flags[shape] = response.json()
        if legacy is not None:
            row = db_session.get(FeatureFlag, uuid.UUID(flags[shape]["id"]))
            row.targeting_rules = legacy
            db_session.commit()
    archived = client.post(
        f"{FLAGS}/bulk-toggle",
        json={"action": "archive", "flag_ids": [flags["archived"]["id"]]},
        headers=_auth(developer),
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["results"][0]["success"] is True, archived.text
    created = client.post(
        "/api/v1/api-keys",
        json={"name": f"g2-{suffix}", "scopes": ["sdk:ruleset"]},
        headers=_auth(developer),
    )
    assert created.status_code == 201, created.text
    yield {
        "developer": developer,
        "flags": flags,
        "keys": {shape: flag["key"] for shape, flag in flags.items()},
        "api_key": created.json()["key"],
    }
    db_session.rollback()
    ids = [uuid.UUID(flag["id"]) for flag in flags.values()]
    db_session.query(FeatureFlag).filter(FeatureFlag.id.in_(ids)).delete(
        synchronize_session=False
    )
    db_session.query(Segment).filter(Segment.id == segment.id).delete(
        synchronize_session=False
    )
    db_session.commit()


def _ruleset(client, api_key, etag=None):
    headers = {"X-API-Key": api_key}
    if etag:
        headers["If-None-Match"] = etag
    response = client.get(RULESET, headers=headers)
    assert response.status_code == 200, (response.status_code, response.text)
    return response


def _evaluate(client, api_key, key, user_id, context):
    params = {"user_id": user_id}
    if context is not None:
        params["context"] = json.dumps(context)
    response = client.get(
        f"{FLAGS}/evaluate/{quote(key, safe='')}",
        params=params,
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200, (key, response.text)
    return {k: response.json()[k] for k in ("enabled", "reason")}


def _probes(keys):
    return [
        {"flag": key, "user_id": user, "context": context}
        for key in keys
        for user in USERS
        for context in CONTEXTS
    ]


def _write(path, payload):
    path.write_bytes(payload)
    return str(path)


@pytest.mark.parametrize("artefact", PYTHON_ARTEFACTS, ids=lambda a: a.version)
def test_the_published_wheel_reads_the_route_and_obeys_both_kill_switches(
    client, db_session, stored, artefact, artefact_cache, tmp_path
):
    keys, api_key = stored["keys"], stored["api_key"]
    superuser = _make_user(db_session, UserRole.ADMIN, is_superuser=True)

    # 1. Every flag, as the route serves it.
    stored_count = db_session.query(FeatureFlag).count()
    first = _ruleset(client, api_key)
    body = first.json()
    assert len(body["flags"]) == stored_count
    entries = {entry["key"]: entry for entry in body["flags"]}
    ours = {shape: entries[key] for shape, key in keys.items()}
    assert len(ours) == len(SHAPES) == 8
    for shape, (_, evaluation) in SHAPES.items():
        assert ours[shape]["active"] is (evaluation is not None), (shape, ours[shape])
        assert ours[shape].get("evaluation") == evaluation, (shape, ours[shape])
    assert ours["native"]["default_rule"]["rollout_percentage"] == 40
    probes_first = _probes(keys.values())
    expected_first = [
        _evaluate(client, api_key, p["flag"], p["user_id"], p["context"])
        for p in probes_first
    ]

    # 2. Kill switch one: the DEVELOPER deactivates the rollout flag.
    deactivated = client.post(
        f"{FLAGS}/{stored['flags']['rollout']['id']}/deactivate",
        headers=_auth(stored["developer"]),
    )
    assert deactivated.status_code == 200, deactivated.text
    second = _ruleset(client, api_key, first.headers["etag"])
    assert second.headers["etag"] != first.headers["etag"]
    probes_second = _probes([keys["rollout"]])
    expected_second = [
        _evaluate(client, api_key, p["flag"], p["user_id"], p["context"])
        for p in probes_second
    ]
    assert all(answer == INACTIVE for answer in expected_second)

    # 3. Kill switch two: a safety rollback to 0% of the native-rules flag.
    rollback = client.post(
        f"/api/v1/safety/feature-flags/{stored['flags']['native']['id']}/rollback",
        params={"percentage": 0, "reason": "G2 kill switch"},
        headers=_auth(superuser),
    )
    assert rollback.status_code == 200, rollback.text
    third = _ruleset(client, api_key, second.headers["etag"])
    assert third.headers["etag"] != second.headers["etag"]
    probes_third = _probes([keys["native"], keys["rollout"]])
    expected_third = [
        _evaluate(client, api_key, p["flag"], p["user_id"], p["context"])
        for p in probes_third
    ]
    assert all(answer == INACTIVE for answer in expected_third)

    # Server mode, at the final state: /evaluate and the bulk route as captured.
    replies = {}
    calls = []
    for key in keys.values():
        response = client.get(
            f"{FLAGS}/evaluate/{quote(key, safe='')}",
            params={"user_id": "user-1"},
            headers={"X-API-Key": api_key},
        )
        assert response.status_code == 200, response.text
        replies[f"GET {FLAGS}/evaluate/{quote(key, safe='')}?user_id=user-1"] = {
            "status": 200,
            "body_file": _write(tmp_path / f"evaluate-{key}.json", response.content),
            "answer": {k: response.json()[k] for k in ("enabled", "reason")},
        }
        calls += [
            {"op": "get_feature_flag", "flag": key, "user_id": "user-1"},
            {"op": "is_feature_enabled", "flag": key, "user_id": "user-1"},
        ]
    bulk = client.get(f"{FLAGS}/user/user-1", headers={"X-API-Key": api_key})
    assert bulk.status_code == 200, bulk.text
    bulk_body = bulk.json()
    assert all(type(value) is bool for value in bulk_body.values()), bulk_body
    assert keys["rollout"] not in bulk_body and keys["archived"] not in bulk_body
    replies[f"GET {FLAGS}/user/user-1"] = {
        "status": 200,
        "body_file": _write(tmp_path / "bulk.json", bulk.content),
    }
    calls.append({"op": "get_all_flags", "user_id": "user-1"})

    # The published wheel, in its own process.
    extraction = tmp_path / "artefact"
    root = ps.extract(artefact, ps.fetch(artefact, artefact_cache), extraction)
    plan = {
        "steps": [
            {
                "body_file": _write(tmp_path / f"ruleset-{n}.json", response.content),
                "etag": response.headers["etag"],
                "probes": probes,
            }
            for n, (response, probes) in enumerate(
                [
                    (first, probes_first),
                    (second, probes_second),
                    (third, probes_third),
                ]
            )
        ],
        "server": {
            "api_url": "http://published-sdk-gate.test",
            "api_key": "g2-replayed",
            "replies": {
                target: {"status": r["status"], "body_file": r["body_file"]}
                for target, r in replies.items()
            },
            "calls": calls,
        },
    }
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    report = ps.run_harness(
        artefact, root, tmp_path / "cwd", {"plan": str(plan_file)}, mode="sequence"
    )
    module = ps.check_identity(artefact, extraction, report)
    log.info("%s: identity %s... at %s", artefact.label, artefact.digest[:19], module)

    steps = report["steps"]
    assert [s["version"] for s in steps] == [
        r.json()["version"] for r in (first, second, third)
    ], [s["error"] for s in steps]
    assert [s["if_none_match"] for s in steps] == [
        None,
        first.headers["etag"],
        second.headers["etag"],
    ]
    remote = sorted(keys[s] for s, (_, e) in SHAPES.items() if e == "remote")
    assert set(remote) <= set(steps[0]["server_evaluated_flags"])
    assert not {keys["rollout"], keys["dashboard"], keys["native"]} & set(
        steps[0]["server_evaluated_flags"]
    )

    # Every probe: a server-evaluated flag defers; every other answer is local
    # and exactly /evaluate's at that state.
    shape_of = {key: shape for shape, key in keys.items()}
    problems, local, deferred = [], 0, 0
    expected_steps = (expected_first, expected_second, expected_third)
    for step, planned, expected in zip(steps, plan["steps"], expected_steps):
        assert len(step["probes"]) == len(planned["probes"]) == len(expected)
        for probe, answer, server in zip(planned["probes"], step["probes"], expected):
            shape = shape_of[probe["flag"]]
            if SHAPES[shape][1] == "remote":
                deferred += 1
                if answer != "DEFER":
                    problems.append(f"{shape} answered locally: {probe} -> {answer}")
            elif answer == "DEFER":
                problems.append(f"{shape} deferred: {probe}")
            elif answer != server:
                problems.append(
                    f"{shape}: {probe} -> {answer}; the server says {server}"
                )
            else:
                local += 1
    assert not problems, problems[:10]
    total = len(probes_first) + len(probes_second) + len(probes_third)
    assert deferred == 3 * len(USERS) * len(CONTEXTS)
    assert local == total - deferred
    log.info("%s: %d probes answered locally, each the server's", artefact.label, local)

    # Both kill switches reached it.
    assert steps[1]["probes"] and all(a == INACTIVE for a in steps[1]["probes"])
    assert steps[2]["probes"] and all(a == INACTIVE for a in steps[2]["probes"])

    # Server mode reads what /evaluate and the bulk route send.
    server = report["server"]
    assert server["unmatched"] == [], server["requests"]
    results = iter(server["results"])
    for key in keys.values():
        target = f"GET {FLAGS}/evaluate/{quote(key, safe='')}?user_id=user-1"
        answer = replies[target]["answer"]
        assert next(results) == answer, key
        enabled = next(results)
        assert type(enabled) is bool and enabled == answer["enabled"], key
    assert next(results) == bulk_body
