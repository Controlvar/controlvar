"""What the product does not do, pinned where the code decides it.

The Limitations page, the README's short list and the getting-started pages
say what Controlvar does not do and what happens instead. Each test holds
one of those statements against the schema, the model, the mounted routes or
the source that decides it. When one of these starts to fail, the product has
gained the thing the page says it lacks: change the page in the same pull
request (its registry under ``docs/_claims/`` names this test).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.core.flag_values import served_value
from backend.app.models.api_key import APIKey
from backend.app.models.experiment import Experiment
from backend.app.models.feature_flag import FeatureFlag
from backend.app.schemas.auth import LoginRequest
from backend.app.schemas.experiment import ExperimentCreate
from backend.app.schemas.feature_flag import FeatureFlagCreate, FeatureFlagUpdate
from backend.app.schemas.results import SampleSizeResult
from backend.app.schemas.tracking import AssignmentRequest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

ROOT = Path(__file__).resolve().parents[4]
APP = ROOT / "backend" / "app"


def _app_sources():
    roots = [APP]
    if (ROOT / "modules" / "backend" / "app").is_dir():
        roots.append(ROOT / "modules" / "backend" / "app")
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def routes():
    """(method, path) of every mounted HTTP route, the included routers' too.

    FastAPI keeps ``include_router`` as a lazy entry, so ``app.routes`` alone
    holds a handful of routes; ``effective_route_contexts`` flattens them, as
    ``backend/tests/smoke/test_flag_change_routes_guarded.py`` reads them.
    """
    from fastapi.routing import APIRoute

    from backend.app.main import app

    found = []
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            for method in getattr(ctx, "methods", None) or ():
                found.append((method, ctx.path))
    assert len(found) > 100, "the scan found too few routes: it is broken"
    return found


# ---------------------------------------------------------------------------
# A feature flag serves two values, never a split among three or more (#1497)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("model", [FeatureFlagCreate, FeatureFlagUpdate])
def test_a_flag_request_with_variants_is_refused(model):
    body = {"key": "k", "name": "n", "variants": [{"key": "a"}, {"key": "b"}]}
    with pytest.raises(ValidationError, match="variants"):
        model(**body)


def test_a_boolean_flag_serves_false_when_off_and_a_value_flag_its_off_value():
    """Inverted by #1497 from "a flag serves false when off and nothing else".

    ``default_value`` still accepts only false, for every flag (D40), and a
    boolean flag still serves false when it is off. A string, number or json
    flag serves its ``off_value``.
    """
    assert FeatureFlagCreate(key="k", name="n").default_value is False
    with pytest.raises(ValidationError, match="default_value"):
        FeatureFlagCreate(key="k", name="n", default_value=True)
    with pytest.raises(ValidationError, match="default_value"):
        FeatureFlagCreate(key="k", name="n", default_value="blue")
    with pytest.raises(ValidationError, match="off_value"):
        FeatureFlagCreate(key="k", name="n", off_value="blue")
    assert served_value(FeatureFlag(key="k", value_type="boolean"), False) == (
        True,
        False,
    )

    created = FeatureFlagCreate(
        key="k", name="n", value_type="string", on_value="a", off_value="blue"
    )
    flag = FeatureFlag(
        key="k",
        value_type=created.value_type,
        on_value=created.on_value,
        off_value=created.off_value,
    )
    assert served_value(flag, False) == (True, "blue")
    assert served_value(flag, True) == (True, "a")


def test_flag_evaluation_reads_no_holdout():
    source = (APP / "services" / "feature_flag_service.py").read_text(encoding="utf-8")
    assert "holdout" not in source.lower()


def test_a_flag_has_no_results_route(routes):
    flag_routes = [
        path for _, path in routes if path.startswith("/api/v1/feature-flags")
    ]
    assert flag_routes, "no feature-flag route was found"
    assert [p for p in flag_routes if "result" in p or "analys" in p] == []


# ---------------------------------------------------------------------------
# Experiments and results
# ---------------------------------------------------------------------------
def _experiment(variants):
    return ExperimentCreate(
        name="e",
        hypothesis="h",
        experiment_type="a_b",
        variants=variants,
        metrics=[{"name": "m", "event_name": "m", "metric_type": "conversion"}],
    )


def test_an_experiment_may_have_one_control_and_several_treatments():
    experiment = _experiment(
        [
            {"name": "control", "is_control": True, "traffic_allocation": 34},
            {"name": "b", "is_control": False, "traffic_allocation": 33},
            {"name": "c", "is_control": False, "traffic_allocation": 33},
        ]
    )
    assert [v.is_control for v in experiment.variants] == [True, False, False]


def test_variant_weights_under_100_are_refused_with_the_documented_message():
    with pytest.raises(ValidationError) as caught:
        _experiment(
            [
                {"name": "control", "is_control": True, "traffic_allocation": 45},
                {"name": "b", "is_control": False, "traffic_allocation": 45},
            ]
        )
    assert "Traffic allocations must sum to 100% (currently 90%)" in str(caught.value)


def test_the_experiment_has_no_traffic_allocation_of_its_own():
    assert "traffic_allocation" not in Experiment.__table__.columns


def test_every_metric_is_planned_as_a_conversion():
    field = SampleSizeResult.model_fields["analysed_as"]
    assert field.default == "conversion"
    assert field.annotation.__args__ == ("conversion",)


def test_no_call_says_that_two_user_ids_are_one_user(routes):
    """There is no identify or alias call: an assignment names one user id,
    and no route joins two, so a user who signs in under a new id is a new
    user to ``/tracking/assign``."""
    assert set(AssignmentRequest.model_fields) == {
        "experiment_key",
        "user_id",
        "context",
    }
    pattern = re.compile(r"identify|alias|merge", re.I)
    assert [path for _, path in routes if pattern.search(path)] == []


# ---------------------------------------------------------------------------
# One deployment is one environment
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("model", [FeatureFlag, Experiment, APIKey])
def test_flags_experiments_and_keys_carry_no_environment(model):
    columns = set(model.__table__.columns.keys())
    assert columns, model
    assert not {c for c in columns if "env" in c.lower()}, columns


# ---------------------------------------------------------------------------
# What the API writes
# ---------------------------------------------------------------------------
def test_the_api_writes_no_kinesis_stream_firehose_or_opensearch_domain():
    pattern = re.compile(
        r"client\(\s*['\"](?:kinesis|firehose)['\"]|opensearch|OpenSearch",
    )
    hits = [rel for rel, text in _app_sources() if pattern.search(text)]
    assert hits == []


def test_assignment_and_tracking_write_no_counter():
    for rel in (
        "backend/app/api/v1/endpoints/tracking.py",
        "backend/app/services/assignment_service.py",
        "backend/app/services/event_service.py",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "increment_counter" not in text and "DynamoDBCounter" not in text, rel


# ---------------------------------------------------------------------------
# Sign-in and access
# ---------------------------------------------------------------------------
def test_a_local_sign_in_takes_an_email_and_a_password_alone(routes):
    assert set(LoginRequest.model_fields) == {"email", "password"}
    pattern = re.compile(r"mfa|totp|otp\b|2fa|two-factor|second-factor|webauthn", re.I)
    assert [path for _, path in routes if pattern.search(path)] == []


def test_the_permission_check_reads_only_the_built_in_role():
    pattern = re.compile(
        r"custom_role|CustomRole|direct_permission|DirectPermission|workspace", re.I
    )
    for rel in ("backend/app/core/permissions.py", "backend/app/api/deps.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not pattern.search(text), rel


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def test_no_route_sets_retention_or_erases_a_users_data(routes):
    pattern = re.compile(r"retention|erase|erasure|gdpr|forget|purge", re.I)
    assert [path for _, path in routes if pattern.search(path)] == []
    deletes = [
        path
        for method, path in routes
        if method == "DELETE"
        and path.startswith(("/api/v1/tracking", "/api/v1/events"))
    ]
    assert deletes == []


def test_nothing_calls_the_event_purge():
    callers = [
        rel
        for rel, text in _app_sources()
        if "purge_old_events(" in text and "def purge_old_events(" not in text
    ]
    assert callers == []


# ---------------------------------------------------------------------------
# Counts the pages quote
# ---------------------------------------------------------------------------
def test_the_rules_engine_has_more_than_20_operators():
    from backend.app.schemas.targeting_rule import OperatorType

    assert len(OperatorType) > 20
    for name in ("SEMANTIC_VERSION", "GEO_DISTANCE", "TIME_WINDOW", "JSON_PATH"):
        assert name in OperatorType.__members__, name


def test_there_are_four_built_in_roles():
    from backend.app.models.user import UserRole

    assert {r.name for r in UserRole} == {"ADMIN", "DEVELOPER", "ANALYST", "VIEWER"}


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------
def test_the_weights_divide_the_100_buckets_between_the_variants_in_order(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from backend.app.services import assignment_service

    control = SimpleNamespace(id="control", traffic_allocation=50)
    treatment = SimpleNamespace(id="treatment", traffic_allocation=50)
    experiment = SimpleNamespace(id="e", key="k", variants=[control, treatment])
    service = assignment_service.AssignmentService(MagicMock())
    picked = {}
    for bucket in (0, 49, 50, 99):
        monkeypatch.setattr(assignment_service, "bucket_of", lambda u, k, b=bucket: b)
        picked[bucket] = service._hash_user_to_variant("u", experiment)
    assert picked == {0: "control", 49: "control", 50: "treatment", 99: "treatment"}


def test_a_rollout_percentage_is_the_share_of_users_turned_on():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from backend.app.services.feature_flag_service import FeatureFlagService

    service = FeatureFlagService(db=MagicMock())
    users = [f"user-{i}" for i in range(2000)]

    def share(percentage):
        flag = SimpleNamespace(key="share", rollout_percentage=percentage)
        return sum(service._evaluate_percentage_rollout(flag, u) for u in users) / len(
            users
        )

    assert share(0) == 0 and share(100) == 1
    assert 0.08 < share(10) < 0.12


def test_an_api_key_expires_only_when_given_an_expiry():
    from datetime import datetime, timedelta

    assert APIKey(is_active=True, expires_at=None).is_valid
    later = datetime.utcnow() + timedelta(days=3650)
    assert APIKey(is_active=True, expires_at=later).is_valid
    past = datetime.utcnow() - timedelta(seconds=1)
    assert not APIKey(is_active=True, expires_at=past).is_valid


def test_assignment_hashes_the_experiment_key_and_the_user(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from backend.app.services import assignment_service

    seen = []

    def bucket_of(user_id, namespace):
        seen.append((user_id, namespace))
        return 0

    monkeypatch.setattr(assignment_service, "bucket_of", bucket_of)
    experiment = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        key="homepage_button_colour",
        variants=[SimpleNamespace(id="control", traffic_allocation=100)],
    )
    assignment_service.AssignmentService(MagicMock())._hash_user_to_variant(
        "user-123", experiment
    )
    assert seen == [("user-123", "homepage_button_colour")]


def test_the_audit_trail_is_append_only(routes):
    audit_routes = [(m, p) for m, p in routes if "audit" in p]
    assert audit_routes, "no audit route was found"
    assert [(m, p) for m, p in audit_routes if m in {"PUT", "PATCH", "DELETE"}] == []
    pattern = re.compile(
        r"query\(\s*(?:AuditLog|ComplianceAuditEvent)\s*\)[^;]{0,400}?\.(?:delete|update)\(",
        re.S,
    )
    assert [rel for rel, text in _app_sources() if pattern.search(text)] == []


#: Every function that deletes rows older than a cut-off. Events, assignments
#: and audit records are not among them; the event purge has no caller
#: (test_nothing_calls_the_event_purge).
AGE_DELETIONS = {
    "backend/app/services/analysis_snapshot_service.py::purge_expired_history",
    "backend/app/services/event_service.py::purge_old_events",
    "backend/app/services/sdk_evaluation_service.py::_prune",
}


def _deletes_by_age(tree):
    import ast

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        deletes = any(
            isinstance(n, ast.Call)
            and (
                (isinstance(n.func, ast.Attribute) and n.func.attr == "delete")
                or (isinstance(n.func, ast.Name) and n.func.id == "delete")
            )
            for n in ast.walk(node)
        )
        by_age = any(
            isinstance(n, ast.Compare)
            and any(isinstance(op, (ast.Lt, ast.LtE)) for op in n.ops)
            and re.search(r"cutoff|timedelta|now\(", ast.unparse(n), re.I)
            for n in ast.walk(node)
        )
        if deletes and by_age:
            yield node.name


def test_only_analysis_history_and_evaluation_counters_are_deleted_by_age():
    import ast

    found = {
        f"{rel}::{name}"
        for rel, text in _app_sources()
        for name in _deletes_by_age(ast.parse(text))
    }
    assert found == AGE_DELETIONS


# ---------------------------------------------------------------------------
# SDKs
# ---------------------------------------------------------------------------
#: Where each native mobile SDK builds two requests: its file, then the code
#: that evaluates a flag and the code that assigns a user to an experiment,
#: each as (the line that starts it, the line after it) pairs.
NATIVE_SDKS = {
    "ios": (
        "sdk/ios/Sources/Controlvar/ExperimentationClient.swift",
        [("public func evaluateFlag(", "// MARK: - Experiments")],
        [("public func getAssignment(", "// MARK: - Tracking")],
    ),
    "android": (
        "sdk/android/sdk/src/main/kotlin/com/controlvar/android/ExperimentationClient.kt",
        [
            ("suspend fun evaluateFlag(", "suspend fun getAssignment("),
            ("private fun evaluateFlagPath(", "private fun send("),
        ],
        [("suspend fun getAssignment(", "suspend fun track(")],
    ),
    "react-native": (
        "sdk/react-native/src/client.ts",
        [("async evaluateFlag(", "async isFeatureEnabled(")],
        [("async getAssignment(", "async getVariant(")],
    ),
    "flutter": (
        "sdk/flutter/lib/src/client.dart",
        [("Future<EvalResult> evaluateFlag(", "Future<bool> isEnabled(")],
        [("Future<Assignment?> getAssignment(", "Future<String?> getVariant(")],
    ),
}

#: A block comment, or a line comment (not the `//` of a URL's scheme).
_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\w])//[^\n]*", re.S)
_CONTEXT = re.compile(r"\bcontext\b", re.I)


def _native_code(rel: str, bounds) -> str:
    text = (ROOT / rel).read_text(encoding="utf-8")
    parts = []
    for start, end in bounds:
        assert text.count(start) == 1, f"{rel}: {start!r} is not there once"
        assert text.count(end) == 1, f"{rel}: {end!r} is not there once"
        head = text.index(start)
        tail = text.index(end)
        assert head < tail, f"{rel}: {end!r} comes before {start!r}"
        parts.append(_COMMENT.sub("", text[head:tail]))
    return "\n".join(parts)


@pytest.mark.parametrize("sdk", sorted(NATIVE_SDKS))
def test_the_native_sdks_send_no_context_on_flag_evaluation(sdk):
    """iOS, Android, React Native and Flutter evaluate a flag with the user id
    alone (#438), so a flag rule on attributes cannot match from them; each
    sends the attributes as the context of an experiment assignment. When this
    fails, the SDK has started to send a context on flag evaluation: say so on
    the pages whose registries name this test."""
    rel, evaluate, assign = NATIVE_SDKS[sdk]
    evaluation = _native_code(rel, evaluate)
    assert "feature-flags/evaluate" in evaluation, f"{sdk}: no evaluate request read"
    assert not _CONTEXT.search(evaluation), f"{sdk} sends a context on flag evaluation"
    # The same scan finds the context the SDK does send, so it reads real code.
    assignment = _native_code(rel, assign)
    assert "tracking/assign" in assignment, f"{sdk}: no assign request read"
    assert _CONTEXT.search(assignment), f"{sdk}: no context on assignment"
