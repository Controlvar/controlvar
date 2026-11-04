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


def test_the_permission_check_reads_grants_only_through_the_capability():
    """The built-in role first, then (full profile) what custom roles and
    direct grants hold, read through one capability; never a workspace role,
    and never a module table named in the core."""
    pattern = re.compile(
        r"custom_role|CustomRole|direct_permission|DirectPermission|workspace", re.I
    )
    for rel in ("backend/app/core/permissions.py", "backend/app/api/deps.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not pattern.search(text), rel
    permissions = (ROOT / "backend/app/core/permissions.py").read_text(encoding="utf-8")
    assert "optional_modules.granted_permissions_provider()" in permissions
    assert "in granted_permissions(user)" in permissions


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
    "flutter": (
        "sdk/flutter/lib/src/client.dart",
        [("Future<EvalResult> evaluateFlag(", "Future<bool> isEnabled(")],
        [("Future<Assignment?> getAssignment(", "Future<String?> getVariant(")],
    ),
}

#: A block comment, or a line comment (not the `//` of a URL's scheme).
_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\w])//[^\n]*", re.S)
#: Any word that starts with "context": `context`, and a helper such as
#: `contextQuery(` that builds it (#1520), which `\bcontext\b` did not see.
_CONTEXT = re.compile(r"\bcontext", re.I)


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
    """iOS, Android and Flutter evaluate a flag with the user id alone (#438;
    React Native sends the attributes since #1520), so a flag rule on
    attributes cannot match from them; each sends the attributes as the context
    of an experiment assignment. When this fails, the SDK has started to send a
    context on flag evaluation: say so on the pages whose registries name this
    test."""
    rel, evaluate, assign = NATIVE_SDKS[sdk]
    evaluation = _native_code(rel, evaluate)
    assert "feature-flags/evaluate" in evaluation, f"{sdk}: no evaluate request read"
    assert not _CONTEXT.search(evaluation), f"{sdk} sends a context on flag evaluation"
    # The same scan finds the context the SDK does send, so it reads real code.
    assignment = _native_code(rel, assign)
    assert "tracking/assign" in assignment, f"{sdk}: no assign request read"
    assert _CONTEXT.search(assignment), f"{sdk}: no context on assignment"


#: Every directory under sdk/ whose flag evaluation sends the user's attributes
#: as the ``context`` of ``/api/v1/feature-flags/evaluate/{key}``: its file, its
#: flag-evaluation code as (the line that starts it, the line that ends it)
#: pairs, and the code token that hands the attributes to that request.
SENDS = {
    "python": (
        "sdk/python/controlvar/client.py",
        [("def get_feature_flag(", "def get_string_value(")],
        "_context_param(user_attributes)",
    ),
    "js": (
        "sdk/js/src/client.ts",
        [
            (
                "async evaluateFlag(flagKey: string, user: UserContext)",
                "async isFeatureEnabled(",
            )
        ],
        "contextQuery(attributes)",
    ),
    "react-native": (
        "sdk/react-native/src/client.ts",
        [("async evaluateFlag(", "async isFeatureEnabled(")],
        "contextQuery(attributes)",
    ),
    "go": (
        "sdk/go/client.go",
        [("func (c *client) EvaluateFlag(", "func contextQuery(")],
        "contextQuery(user.Attributes)",
    ),
    "react": (
        "sdk/react/src/client/ExperimentationClient.ts",
        [("export function evaluatePath(", "export function assignBody(")],
        "contextQuery(user.attributes)",
    ),
    "openfeature": (
        "sdk/openfeature/src/ExperimentationProvider.ts",
        [("private async evaluate(", "private metadataFor(")],
        "attributesOf(context",
    ),
    "openfeature-python": (
        "sdk/openfeature-python/src/controlvar_openfeature/provider.py",
        [("def _lookup(", "def _resolve(")],
        "_attributes_of(",
    ),
    # Not an SDK, as the SDK guide's table says. It evaluates a flag for a user
    # through the same route, with the attributes as ``context`` in the body.
    "mcp": (
        "sdk/mcp/src/tools.ts",
        [("async ({ flag_key, user_id, context }) =>", "EVALUATION_FIELDS,")],
        "{ user_id, context }",
    ),
    # Since #1723: the URL is built in evaluateUrl, which evaluateFeatureFlag calls.
    "java": (
        "sdk/java/core/src/main/java/com/controlvar/sdk/ExperimentationClient.java",
        [("private HttpUrl evaluateUrl(", "private static String withoutQuery(")],
        'addQueryParameter("context"',
    ),
    # Since #1722.
    "dotnet": (
        "sdk/dotnet/src/Controlvar/ExperimentationClient.cs",
        [
            (
                "public async Task<FlagEvaluationResult> EvaluateFlagAsync(",
                "if (string.IsNullOrEmpty(result.Key))",
            )
        ],
        'query["context"]',
    ),
}

#: Every directory under sdk/ whose flag evaluation sends the user id alone, so
#: a flag rule on attributes cannot match from it: the issue that tracks it, its
#: file, and its code from the evaluate signature through the request call as
#: (the line that starts it, the first line after the call) pairs.
DOES_NOT_SEND = {
    "ruby": (
        1720,
        "sdk/ruby/lib/controlvar/client.rb",
        [("def evaluate_flag(", "data = {} unless data.is_a?(Hash)")],
    ),
    "php": (
        1720,
        "sdk/php/src/ExperimentationClient.php",
        [("public function evaluateFlag(", "} catch (ExperimentationException) {")],
    ),
    "elixir": (
        1720,
        "sdk/elixir/lib/controlvar/client.ex",
        [("def evaluate_flag(", "{:ok, data} when is_map(data) ->")],
    ),
    "edge": (
        1720,
        "sdk/edge/src/client.ts",
        [("async evaluateFlag(", "const evaluation: FlagEvaluation = {")],
    ),
    "ios": (
        438,
        "sdk/ios/Sources/Controlvar/ExperimentationClient.swift",
        [("public func evaluateFlag(", "let result = EvalResult(")],
    ),
    "android": (
        438,
        "sdk/android/sdk/src/main/kotlin/com/controlvar/android/ExperimentationClient.kt",
        [
            ("suspend fun evaluateFlag(", "} catch (e: ExperimentationException) {"),
            ("private fun evaluateFlagPath(", 'return "${url.encodedPath}'),
        ],
    ),
    "flutter": (
        438,
        "sdk/flutter/lib/src/client.dart",
        [("Future<EvalResult> evaluateFlag(", "final result = EvalResult.fromJson(")],
    ),
}

#: What sending a context looks like in these languages: a query parameter, a
#: JSON or map key, the helpers the senders use, a Ruby symbol or Elixir atom.
_SENDS_A_CONTEXT = (
    "context=",
    '"context"',
    "'context'",
    "contextQuery",
    "_context_param",
    ":context",
    "context:",
)

#: A whole-line ``#`` comment (Python, Ruby, Elixir). A ``#`` after code is
#: kept: in Ruby and Elixir it is as often string interpolation, ``#{...}``.
_HASH_COMMENT = re.compile(r"(?m)^[ \t]*#[^\n]*")


def _evaluation_code(rel: str, bounds) -> tuple[str, str | None]:
    """The code of each (start, end) pair in ``rel``, comments removed, and
    what is wrong with the bounds, if anything. ``start`` must be in the file
    once; the code runs from it to the first ``end`` after it."""
    path = ROOT / rel
    if not path.is_file():
        return "", f"{rel} is not there"
    text = path.read_text(encoding="utf-8")
    strip = _HASH_COMMENT if rel.endswith((".py", ".rb", ".ex")) else _COMMENT
    parts = []
    for start, end in bounds:
        if text.count(start) != 1:
            return "", f"{rel}: {start!r} is not there once"
        head = text.index(start)
        tail = text.find(end, head + len(start))
        if tail == -1:
            return "", f"{rel}: no {end!r} after {start!r}"
        parts.append(strip.sub("", text[head:tail]))
    return "\n".join(parts), None


def test_every_sdk_directory_is_classified_by_whether_flag_evaluation_sends_attributes():
    """Every directory under sdk/ is in ``SENDS`` or ``DOES_NOT_SEND``: whether
    its flag evaluation sends the user's attributes as ``context``, so that a
    flag rule on attributes can match from it. A new directory fails until it
    is classified.

    An SDK in ``SENDS`` must still hand the attributes to its evaluate request:
    its marker, the code token that does so, is in its flag-evaluation code.
    An SDK in ``DOES_NOT_SEND`` must still send the user id alone: its code
    from the evaluate signature through the request call names the evaluate
    route and ``user_id`` and none of ``_SENDS_A_CONTEXT``. When one moves,
    move its entry, update its issue and change its page in the same pull
    request.

    The limit: this reads code tokens and runs no SDK. A helper that returns
    nothing (Python's ``_context_param`` returning None, Go's ``contextQuery``
    returning "") leaves this test green by construction. That behaviour is
    held by the SDKs' own unit tests, to which the SDK guide's sentence "The
    JavaScript, Python, React and Go SDKs send it on both calls" is pinned
    (sdk-guide.md.toml).
    """
    directories = sorted(
        path.name for path in (ROOT / "sdk").iterdir() if path.is_dir()
    )
    assert len(directories) > 10, (
        f"the walk found too few SDK directories: {directories}"
    )
    assert not set(SENDS) & set(DOES_NOT_SEND), (
        "an SDK is in both SENDS and DOES_NOT_SEND"
    )

    failures = [
        f"classified SDK directory is gone: {name}"
        for name in sorted((set(SENDS) | set(DOES_NOT_SEND)) - set(directories))
    ]
    for sdk in directories:
        if sdk in SENDS:
            rel, bounds, marker = SENDS[sdk]
            code, problem = _evaluation_code(rel, bounds)
            if problem:
                failures.append(f"{sdk}: {problem}")
            elif marker not in code:
                failures.append(
                    f"{sdk}: no context on flag evaluation ({marker!r} is not in"
                    f" its flag-evaluation code in {rel})"
                )
        elif sdk in DOES_NOT_SEND:
            issue, rel, bounds = DOES_NOT_SEND[sdk]
            code, problem = _evaluation_code(rel, bounds)
            if problem:
                failures.append(f"{sdk}: {problem}")
                continue
            if "feature-flags/evaluate" not in code:
                failures.append(
                    f"{sdk}: no evaluate request read in {rel}"
                    f" (#{issue}: if it now sends, move it to SENDS)"
                )
            if "user_id" not in code:
                failures.append(
                    f"{sdk}: no user_id in its evaluate request in {rel}"
                    f" (#{issue}: if it now sends, move it to SENDS)"
                )
            sent = [token for token in _SENDS_A_CONTEXT if token in code]
            if sent:
                failures.append(
                    f"{sdk} sends a context on flag evaluation (update #{issue} and"
                    f" move {sdk} to SENDS): {sent[0]!r} in {rel}"
                )
        else:
            failures.append(f"unclassified SDK directory: {sdk}")
    assert not failures, "\n".join(failures)
