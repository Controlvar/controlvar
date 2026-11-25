"""What a key of each flag environment is answered for a flag (#2011).

Over the real routes and a real database, with real keys and
``deps.get_api_key`` not replaced:

* a flag nothing configures in an environment is off there, ``inactive``, with
  its off value; an unknown key is 404 in every environment; an archived flag
  row is off in every environment, whatever its side rows say; the answer has
  the fields it had;
* bucketing has no environment in it: the same users fall inside the same
  percentage in every environment;
* a staging evaluation that fails is stored with its environment, and neither
  it nor any staging evaluation is counted, by the metrics or by Prometheus;
* the environment never enters the targeting context;
* the ruleset's version, and so its ETag, is the environment's own;
* one evaluation issues exactly the statements 0.48.0 issues for a production
  key, and the stated ones for a key of another environment;
* a staging key is answered what the contract vectors expect of a production
  one, for all of them, when the vectors' flags are staging's.

Each test checks the environment it changes and another one it does not.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List
from urllib.parse import quote

import pytest
from sqlalchemy import event

from backend.app.api import deps
from backend.app.core.metrics import feature_flag_evaluations_total
from backend.app.main import app
from backend.app.models.feature_flag import (
    FeatureFlag,
    FeatureFlagStatus,
    FlagEnvironmentSetting,
)
from backend.app.models.metrics.metric import ErrorLog, RawMetric
from backend.app.models.segment import (
    Segment,
    SegmentKind,
    SegmentMember,
    SegmentStatus,
)
from backend.app.models.user import UserRole
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.app.services.safety_service import SafetyService
from backend.scripts.generate_ruleset_vectors import (
    VECTOR_SEGMENT,
    VECTOR_SEGMENT_MEMBERS,
    VECTORS_PATH,
)
from backend.tests.integration.api.route_helpers import (
    api_client,
    local_sign_in,
    make_user,
)
from backend.tests.integration.key_environments import configure, environment_key

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

FLAGS = "/api/v1/feature-flags"
RULESET = "/api/v1/sdk/ruleset"
ENVIRONMENTS = ("production", "staging", "development")
US = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "country", "operator": "equals", "value": "US"}
            ],
        }
    ],
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
def owner(db_session):
    return make_user(db_session, UserRole.DEVELOPER, tag="enveval")


@pytest.fixture
def keys(db_session, owner) -> Dict[str, Dict[str, str]]:
    return {
        environment: environment_key(db_session, owner, environment, "sdk:ruleset")
        for environment in ENVIRONMENTS
    }


@pytest.fixture
def make_flag(db_session, owner):
    """A flag on at 100% in production, unless told otherwise."""

    def make(**overrides) -> FeatureFlag:
        fields: Dict[str, Any] = {
            "key": f"env-{uuid.uuid4().hex[:10]}",
            "name": "Environment flag",
            "status": FeatureFlagStatus.ACTIVE,
            "owner_id": owner.id,
            "rollout_percentage": 100,
        }
        fields.update(overrides)
        flag = FeatureFlag(**fields)
        db_session.add(flag)
        db_session.commit()
        return flag

    return make


def _get(client, headers, flag_key: str, user: str = "u-1", context=None) -> Any:
    params = {"user_id": user}
    if context is not None:
        params["context"] = json.dumps(context)
    return client.get(
        f"{FLAGS}/evaluate/{quote(flag_key, safe='')}", params=params, headers=headers
    )


def _evaluate(client, headers, flag_key: str, user: str = "u-1", context=None) -> Dict:
    answer = _get(client, headers, flag_key, user, context)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _short(answer: Dict[str, Any]) -> list:
    return [answer["enabled"], answer["reason"]]


# ---------------------------------------------------------------------------
# What each environment serves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("environment", ["staging", "development"])
def test_a_flag_nothing_configures_there_is_off_with_its_off_value(
    client, keys, make_flag, environment
):
    flag = make_flag(value_type="string", on_value="new", off_value="old")
    there = _evaluate(client, keys[environment], flag.key)
    assert there == {
        "key": flag.key,
        "enabled": False,
        "value": "old",
        "value_type": "string",
        "config": None,
        "reason": "inactive",
    }
    production = _evaluate(client, keys["production"], flag.key)
    assert (production["enabled"], production["value"]) == (True, "new")
    # No field is added in either environment.
    assert set(production) == set(there)


@pytest.mark.parametrize("environment", ["staging", "development"])
def test_an_environments_own_rules_and_percentage_decide(
    client, db_session, keys, make_flag, environment
):
    flag = make_flag(value_type="number", on_value=10, off_value=2.5)
    configure(db_session, flag, environment, rollout_percentage=0, targeting_rules=US)
    in_us = _evaluate(client, keys[environment], flag.key, context={"country": "US"})
    elsewhere = _evaluate(
        client, keys[environment], flag.key, context={"country": "CA"}
    )
    assert _short(in_us) == [True, "targeting_rule"] and in_us["value"] == 10
    assert _short(elsewhere) == [False, "rollout"] and elsewhere["value"] == 2.5
    # Production has no rules and is at 100%; the other one is untouched.
    for country in ("US", "CA"):
        context = {"country": country}
        answer = _evaluate(client, keys["production"], flag.key, context=context)
        assert _short(answer) == [True, "rollout"]
    (other,) = {"staging", "development"} - {environment}
    answer = _evaluate(client, keys[other], flag.key, context={"country": "US"})
    assert _short(answer) == [False, "inactive"]


def test_an_archived_flag_is_off_in_every_environment(
    client, db_session, keys, make_flag
):
    flag = make_flag(status=FeatureFlagStatus.ARCHIVED)
    live = make_flag()
    for environment in ("staging", "development"):
        for each in (flag, live):
            configure(db_session, each, environment, rollout_percentage=100)
    for environment in ENVIRONMENTS:
        assert _short(_evaluate(client, keys[environment], flag.key)) == [
            False,
            "inactive",
        ]
        bulk = client.get(f"{FLAGS}/user/u-1", headers=keys[environment]).json()
        assert flag.key not in bulk and bulk[live.key] is True, environment
        entries = client.get(RULESET, headers=keys[environment]).json()["flags"]
        by_key = {entry["key"]: entry for entry in entries}
        assert by_key[flag.key]["active"] is False, environment
        assert by_key[live.key]["active"] is True, environment


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_an_unknown_flag_is_404_in_every_environment(client, keys, environment):
    missing = f"missing-{uuid.uuid4().hex[:8]}"
    answer = _get(client, keys[environment], missing)
    assert answer.status_code == 404
    assert answer.json() == {"detail": f"Feature flag with key '{missing}' not found"}


def test_the_same_users_are_inside_the_same_percentage_in_every_environment(
    client, db_session, keys, make_flag
):
    """No environment salt: 25% is the same 25% of users everywhere."""
    flag = make_flag(rollout_percentage=25)
    configure(db_session, flag, "staging", rollout_percentage=25)
    configure(db_session, flag, "development", rollout_percentage=60)
    users = [f"user-{n}" for n in range(60)]
    inside = {
        environment: {
            user
            for user in users
            if _evaluate(client, keys[environment], flag.key, user)["enabled"]
        }
        for environment in ENVIRONMENTS
    }
    assert 0 < len(inside["production"]) < len(users)
    assert inside["staging"] == inside["production"]
    assert inside["production"] < inside["development"]


def test_a_key_stored_with_no_environment_named_is_a_production_key(
    client, db_session, owner, keys, make_flag
):
    flag = make_flag()
    configure(db_session, flag, "staging", is_active=False)
    unnamed = environment_key(db_session, owner)
    assert _short(_evaluate(client, unnamed, flag.key)) == [True, "rollout"]
    assert _short(_evaluate(client, keys["staging"], flag.key)) == [False, "inactive"]


# ---------------------------------------------------------------------------
# The environment is not in the context
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_a_rule_on_an_environment_attribute_sees_only_what_the_client_sent(
    client, db_session, keys, make_flag, environment
):
    """The server never puts the key's environment in the targeting context:
    a rule on an attribute named ``environment`` matches what the client sent
    and nothing else, with a key of any environment."""

    def rules(value: str) -> Dict[str, Any]:
        condition = {"attribute": "environment", "operator": "equals", "value": value}
        return {
            "logical_operator": "AND",
            "groups": [{"logical_operator": "AND", "conditions": [condition]}],
        }

    flag = make_flag(rollout_percentage=0, targeting_rules=rules(environment))
    for other in ("staging", "development"):
        configure(
            db_session,
            flag,
            other,
            rollout_percentage=0,
            targeting_rules=rules(environment),
        )
    headers = keys[environment]
    sent = next(e for e in ENVIRONMENTS if e != environment)
    not_sent = _evaluate(client, headers, flag.key)
    another = _evaluate(client, headers, flag.key, context={"environment": sent})
    for answer in (not_sent, another):
        assert _short(answer) == [False, "rollout"], (
            "rule on context attribute 'environment' matched by the key's environment"
        )
    same = _evaluate(client, headers, flag.key, context={"environment": environment})
    assert _short(same) == [True, "targeting_rule"]


# ---------------------------------------------------------------------------
# What is recorded
# ---------------------------------------------------------------------------


def _counted(flag_key: str) -> float:
    return sum(
        feature_flag_evaluations_total.labels(
            flag_key=flag_key, result=result
        )._value.get()
        for result in ("enabled", "disabled")
    )


def _metric_rows(db_session, flag) -> int:
    db_session.expire_all()
    return (
        db_session.query(RawMetric).filter(RawMetric.feature_flag_id == flag.id).count()
    )


def test_evaluations_outside_production_are_not_counted_anywhere(
    client, db_session, keys, make_flag
):
    flag = make_flag()
    configure(db_session, flag, "staging")
    for environment in ("staging", "development"):
        _evaluate(client, keys[environment], flag.key)
        client.post(
            f"{FLAGS}/evaluate/{flag.key}",
            json={"user_id": "u-1"},
            headers=keys[environment],
        )
        client.get(f"{FLAGS}/user/u-1", headers=keys[environment])
    assert (_metric_rows(db_session, flag), _counted(flag.key)) == (0, 0)

    _evaluate(client, keys["production"], flag.key)
    # One flag_evaluation and one latency row, and one Prometheus count.
    assert (_metric_rows(db_session, flag), _counted(flag.key)) == (2, 1)


@pytest.mark.regression
@pytest.mark.parametrize("environment", ["staging", "development"])
def test_a_failed_evaluation_outside_production_names_its_environment(
    client, db_session, keys, make_flag, monkeypatch, environment
):
    flag = make_flag()
    configure(db_session, flag, environment)

    def fails(self, *args, **kwargs):
        raise RuntimeError("planted evaluation failure")

    monkeypatch.setattr(FeatureFlagService, "_match_targeting_rule", fails)
    context = {"environment": "production"}
    for sent_with in (environment, "production"):
        answer = _evaluate(client, keys[sent_with], flag.key, context=context)
        assert _short(answer) == [False, "error"]
    monkeypatch.undo()

    db_session.expire_all()
    rows = (
        db_session.query(ErrorLog)
        .filter(ErrorLog.feature_flag_id == flag.id)
        .order_by(ErrorLog.timestamp)
        .all()
    )
    assert [row.request_data for row in rows] == [
        {"context": context, "environment": environment},
        {"context": context},
    ]
    metrics = SafetyService(db_session).get_error_metrics(db_session, flag.id)
    assert (metrics["error_count"], metrics["uncounted_error_count"]) == (1, 0)
    assert metrics["total_evaluations"] == 1


# ---------------------------------------------------------------------------
# The ruleset's version
# ---------------------------------------------------------------------------


def _poll(client, headers, etag: str) -> int:
    answer = client.get(RULESET, headers={**headers, "If-None-Match": etag})
    assert answer.status_code in (200, 304), answer.text
    return answer.status_code


def _etag(client, headers) -> str:
    answer = client.get(RULESET, headers=headers)
    assert answer.status_code == 200, answer.text
    assert answer.headers["etag"] == f'"{answer.json()["version"]}"'
    return answer.headers["etag"]


@pytest.mark.regression
def test_each_environments_ruleset_has_its_own_version(
    client, db_session, keys, make_flag
):
    flag = make_flag()
    etags = {e: _etag(client, keys[e]) for e in ENVIRONMENTS}
    for environment in ENVIRONMENTS:
        assert _poll(client, keys[environment], etags[environment]) == 304

    # A staging change moves staging's version and nobody else's.
    configure(db_session, flag, "staging", rollout_percentage=30)
    assert _poll(client, keys["staging"], etags["staging"]) == 200, (
        "staging poll answered 304 after a staging change"
    )
    for environment in ("production", "development"):
        assert _poll(client, keys[environment], etags[environment]) == 304
    etags["staging"] = _etag(client, keys["staging"])

    # A production change moves production's, and not staging's.
    flag.rollout_percentage = 40
    db_session.commit()
    assert _poll(client, keys["production"], etags["production"]) == 200
    for environment in ("staging", "development"):
        assert _poll(client, keys[environment], etags[environment]) == 304

    # A version of one environment is not a match for another.
    production = _etag(client, keys["production"])
    assert _poll(client, keys["staging"], production) == 200


# ---------------------------------------------------------------------------
# The statements one evaluation issues
# ---------------------------------------------------------------------------

#: One evaluation's statements after the key's first use of the minute, as
#: 0.48.0 issues them with a production key: the key and its owner, the flag,
#: the two metric rows, and the rows their commits expired, read back. The
#: numbers are what one request of 0.48.0 issues, measured there.
PRODUCTION_STATEMENTS = {
    "GET evaluate": [
        "SELECT api_keys",
        "SELECT users",
        "SELECT feature_flags",
        "INSERT raw_metrics",
        "SELECT raw_metrics",
        "INSERT raw_metrics",
        "SELECT raw_metrics",
        "SELECT feature_flags",
    ],
}
#: With a key of another environment: one more statement to read the flag's
#: side row (k = 1), and none of the five the metrics cost.
OTHER_STATEMENTS = {
    "GET evaluate": [
        "SELECT api_keys",
        "SELECT users",
        "SELECT feature_flags",
        "SELECT flag_environment_settings",
    ],
}

_SHAPE = re.compile(
    r"^\s*(?:(SELECT)\b.*?\bFROM|(INSERT) INTO|(UPDATE)|(DELETE) FROM)\s+"
    r"(?:\w+\.)?\"?(\w+)\"?",
    re.DOTALL,
)


def _shapes(statements: List[str]) -> List[str]:
    found = []
    for statement in statements:
        if statement.startswith("SET search_path"):
            continue  # the test session's own
        match = _SHAPE.match(statement)
        assert match, statement
        verb = next(group for group in match.groups()[:4] if group)
        found.append(f"{verb} {match.group(5)}")
    return found


def _statements_of(client, db_session, headers, flag_key) -> List[str]:
    """The statements of one GET evaluate, after one unmeasured request (the
    key's last use is written by the first of each minute)."""
    _evaluate(client, headers, flag_key)
    engine = db_session.get_bind()
    statements: List[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        _evaluate(client, headers, flag_key)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return _shapes(statements)


@pytest.mark.regression
def test_a_production_keys_evaluation_issues_0_48_0s_statements(
    client, db_session, keys, make_flag
):
    """k = 0: the key's environment is on the row ``get_api_key`` read, and a
    production evaluation reads no side row, however many there are."""
    flag = make_flag()
    counts = [_statements_of(client, db_session, keys["production"], flag.key)]
    configure(db_session, flag, "staging")
    configure(db_session, flag, "development")
    counts.append(_statements_of(client, db_session, keys["production"], flag.key))
    assert counts == [PRODUCTION_STATEMENTS["GET evaluate"]] * 2


@pytest.mark.parametrize("environment", ["staging", "development"])
@pytest.mark.parametrize("configured", [False, True])
def test_a_key_of_another_environment_issues_one_more_read_and_no_write(
    client, db_session, keys, make_flag, environment, configured
):
    """k = 1: the flag's side row in that environment, one statement whether
    or not there is one, and however many environments the flag has."""
    flag = make_flag()
    if configured:
        for each in ("staging", "development"):
            configure(db_session, flag, each)
    statements = _statements_of(client, db_session, keys[environment], flag.key)
    assert statements == OTHER_STATEMENTS["GET evaluate"]


def _statements_of_get(client, db_session, headers, path) -> List[str]:
    """The statements of one GET of *path*, after one unmeasured request."""
    client.get(path, headers=headers)
    engine, found = db_session.get_bind(), []

    def record(conn, cursor, statement, parameters, context, executemany):
        if not statement.startswith("SET search_path"):  # the test session's own
            found.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert client.get(path, headers=headers).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return found


def test_the_ruleset_reads_the_side_rows_in_one_statement(
    client, db_session, keys, make_flag
):
    for _ in range(3):
        configure(db_session, make_flag(), "staging")
    production, staging = (
        _shapes(_statements_of_get(client, db_session, keys[environment], RULESET))
        for environment in ("production", "staging")
    )
    assert production.count("SELECT feature_flags") == 1
    assert "SELECT flag_environment_settings" not in production
    assert staging == [*production, "SELECT flag_environment_settings"]


@pytest.mark.regression
def test_the_bulk_flags_read_the_side_rows_in_the_flags_query(
    client, db_session, keys, make_flag
):
    """``GET /feature-flags/user/{user_id}``: production reads no side row, and
    another environment reads the flags and their side rows in one joined query
    in place of the flags query, whatever the number of flags, and records
    nothing."""
    path = f"{FLAGS}/user/u-1"
    for _ in range(3):
        flag = make_flag()
        for environment in ("staging", "development"):
            configure(db_session, flag, environment)

    production = _statements_of_get(client, db_session, keys["production"], path)
    assert "SELECT feature_flags" in _shapes(production)
    assert [s for s in production if "flag_environment_settings" in s] == []

    for environment in ("staging", "development"):
        other = _statements_of_get(client, db_session, keys[environment], path)
        assert _shapes(other) == [
            "SELECT api_keys",
            "SELECT users",
            "SELECT feature_flags",
        ]
        assert re.search(r"\bJOIN (?:\w+\.)?flag_environment_settings\b", other[-1])


# ---------------------------------------------------------------------------
# The contract vectors, as staging's flags
# ---------------------------------------------------------------------------


def _has_lone_surrogate(value) -> bool:
    if isinstance(value, str):
        return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)
    if isinstance(value, list):
        return any(_has_lone_surrogate(item) for item in value)
    if isinstance(value, dict):
        return any(
            _has_lone_surrogate(key) or _has_lone_surrogate(item)
            for key, item in value.items()
        )
    return False


@pytest.fixture
def staging_corpus(db_session, owner):
    """The vectors' 161 flags, configured in staging only: each one's status,
    rules and percentage are staging's, and production has the flag off, with
    no rules, at 0% (an archived flag is archived, in every environment)."""
    vectors = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    flag_keys = [spec["key"] for spec in vectors["stored_flags"]]

    def remove() -> None:
        db_session.rollback()
        db_session.query(FeatureFlag).filter(FeatureFlag.key.in_(flag_keys)).delete(
            synchronize_session=False
        )
        db_session.query(Segment).filter(
            Segment.id == uuid.UUID(VECTOR_SEGMENT)
        ).delete(synchronize_session=False)
        db_session.commit()

    remove()
    for spec in vectors["stored_flags"]:
        archived = spec["status"] == "ARCHIVED"
        flag = FeatureFlag(
            key=spec["key"],
            name=spec["key"][:100],
            status=(
                FeatureFlagStatus.ARCHIVED if archived else FeatureFlagStatus.INACTIVE
            ),
            owner_id=owner.id,
            rollout_percentage=0,
            targeting_rules=None,
            value_type=spec.get("value_type", "boolean"),
            on_value=spec.get("on_value"),
            off_value=spec.get("off_value"),
        )
        db_session.add(flag)
        db_session.flush()
        db_session.add(
            FlagEnvironmentSetting(
                feature_flag_id=flag.id,
                environment="staging",
                is_active=spec["status"] == "ACTIVE",
                rollout_percentage=spec["rollout_percentage"],
                targeting_rules=spec["targeting_rules"],
            )
        )
    db_session.add(
        Segment(
            id=uuid.UUID(VECTOR_SEGMENT),
            name="Vector segment",
            kind=SegmentKind.ID_LIST.value,
            status=SegmentStatus.ACTIVE,
            owner_id=owner.id,
        )
    )
    db_session.flush()
    for member in VECTOR_SEGMENT_MEMBERS[VECTOR_SEGMENT]:
        db_session.add(
            SegmentMember(segment_id=uuid.UUID(VECTOR_SEGMENT), member_id=member)
        )
    db_session.commit()
    yield vectors
    remove()


@pytest.mark.regression
def test_a_staging_keys_ruleset_is_the_vectors_ruleset(client, staging_corpus, keys):
    served = client.get(RULESET, headers=keys["staging"]).json()
    entries = {entry["key"]: entry for entry in served["flags"]}
    assert (served["schema"], served["bucketing"]) == (1, "md5-mod100-v1")
    expected = staging_corpus["ruleset"]["flags"]
    assert len(expected) == 161
    for entry in expected:
        assert entries[entry["key"]] == entry

    # Production serves every one of them off.
    production = client.get(RULESET, headers=keys["production"]).json()
    for entry in production["flags"]:
        if entry["key"] in entries and entry["key"] in {e["key"] for e in expected}:
            assert entry["active"] is False, entry


@pytest.mark.regression
def test_a_staging_key_is_answered_every_vector_a_url_can_carry(
    client, db_session, staging_corpus, keys
):
    """2231 of the 2242 cases, through ``GET /evaluate`` with a real staging
    key. The other 11 hold a lone surrogate, which no client can put in a URL,
    and are not sent, as in the production run of the same file."""
    checked = values = skipped = 0
    wrong = []
    rows_before = db_session.query(RawMetric).count()
    for case in staging_corpus["cases"]:
        if _has_lone_surrogate(case["user_id"]) or _has_lone_surrogate(case["context"]):
            assert not case["must_local"], case["id"]
            skipped += 1
            continue
        answer = _evaluate(
            client, keys["staging"], case["flag"], case["user_id"], case["context"]
        )
        got = {field: answer[field] for field in ("enabled", "reason")}
        if got != case["expected"]:
            wrong.append((case["id"], got, case["expected"]))
        if "expected_value" in case:
            values += 1
            served = json.dumps(answer["value"], sort_keys=True)
            if served != json.dumps(case["expected_value"], sort_keys=True):
                wrong.append((case["id"], served, case["expected_value"]))
        checked += 1
    assert not wrong, f"{len(wrong)} wrong; first {wrong[:5]}"
    assert (checked, skipped, values) == (2231, 11, 51)
    total = len(staging_corpus["cases"])
    assert checked + skipped == total == staging_corpus["counts"]["cases"] == 2242
    # Not one of them was recorded, and production answers each flag off.
    db_session.rollback()
    assert db_session.query(RawMetric).count() == rows_before
    for spec in staging_corpus["stored_flags"]:
        answer = _evaluate(client, keys["production"], spec["key"])
        assert _short(answer) == [False, "inactive"], spec["key"]
