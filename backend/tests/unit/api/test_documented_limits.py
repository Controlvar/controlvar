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
from typing import get_args

import pytest
from pydantic import BaseModel, ValidationError

from backend.app.core.flag_values import served_value
from backend.app.models.api_key import APIKey
from backend.app.models.audit_log import EntityType
from backend.app.models.experiment import Experiment
from backend.app.models.feature_flag import FeatureFlag
from backend.app.schemas.auth import LoginRequest
from backend.app.schemas.experiment import ExperimentCreate
from backend.app.schemas.feature_flag import FeatureFlagCreate, FeatureFlagUpdate
from backend.app.schemas.results import SampleSizeResult
from backend.app.schemas.tracking import AssignmentRequest
from backend.app.services.audit_service import (
    AUDIT_NAME_ONLY_FIELDS,
    AUDIT_VALUE_FIELDS,
)

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


def _route_contexts():
    """Every mounted HTTP route's context, the included routers' too.

    FastAPI keeps ``include_router`` as a lazy entry, so ``app.routes`` alone
    holds a handful of routes; ``effective_route_contexts`` flattens them, as
    ``backend/tests/smoke/test_flag_change_routes_guarded.py`` reads them.
    """
    from fastapi.routing import APIRoute

    from backend.app.main import app

    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        yield from contexts() if callable(contexts) else contexts


@pytest.fixture(scope="module")
def routes():
    """(method, path) of every mounted HTTP route, the included routers' too."""
    found = []
    for ctx in _route_contexts():
        for method in getattr(ctx, "methods", None) or ():
            found.append((method, ctx.path))
    assert len(found) > 100, "the scan found too few routes: it is broken"
    return found


# ---------------------------------------------------------------------------
# A flag's split has 3 to 10 values (#2012); variants stay refused (#1497)
# ---------------------------------------------------------------------------
def _split(count):
    weights = [100 // count] * count
    weights[0] += 100 - sum(weights)
    return [{"value": f"v{n}", "weight": w} for n, w in enumerate(weights)]


@pytest.mark.parametrize("model", [FeatureFlagCreate, FeatureFlagUpdate])
def test_a_split_has_three_to_ten_values_and_variants_stay_refused(model):
    """Replaces "a flag request with variants is refused": a string, number or
    json flag serves a split of 3 to 10 values, and ``variants`` is still not a
    field. The update's split is checked against the stored flag."""
    from backend.app.core.flag_values import (
        MAX_SPLIT_ENTRIES,
        MIN_SPLIT_ENTRIES,
        serving_errors,
    )

    assert (MIN_SPLIT_ENTRIES, MAX_SPLIT_ENTRIES) == (3, 10)
    body = {"key": "k", "name": "n", "variants": [{"key": "a"}, {"key": "b"}]}
    with pytest.raises(ValidationError, match="variants"):
        model(**body)
    for count, accepted in ((2, False), (3, True), (10, True), (11, False)):
        refused = serving_errors("string", {"split": _split(count)}, None)
        assert (not refused) is accepted, count
    created = FeatureFlagCreate(
        key="k",
        name="n",
        value_type="string",
        on_value="a",
        off_value="b",
        split=_split(3),
    )
    assert len(created.split) == 3
    with pytest.raises(ValidationError, match="at least 3"):
        FeatureFlagCreate(
            key="k",
            name="n",
            value_type="string",
            on_value="a",
            off_value="b",
            split=_split(2),
        )
    with pytest.raises(ValidationError, match="at most 10"):
        FeatureFlagCreate(
            key="k",
            name="n",
            value_type="string",
            on_value="a",
            off_value="b",
            split=_split(11),
        )


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


def test_the_alias_route_is_the_one_call_that_links_two_user_ids(routes):
    """There is no identify call (#449). ``POST /api/v1/tracking/alias``
    (beta) links an anonymous id to a known id, and no other route joins two
    ids: an assignment still names one user id, so a user who signs in under
    a new id that was never linked is a new user to ``/tracking/assign``."""
    assert set(AssignmentRequest.model_fields) == {
        "experiment_key",
        "user_id",
        "context",
    }
    pattern = re.compile(r"identify|alias|merge", re.I)
    assert [route for route in routes if pattern.search(route[1])] == [
        ("POST", "/api/v1/tracking/alias")
    ]


# ---------------------------------------------------------------------------
# Environments are for feature flags and API keys, not experiments
# ---------------------------------------------------------------------------
API_KEYS = "/api/v1/api-keys"


def _models_in(annotation) -> set:
    """The Pydantic models in *annotation*, inside List, Optional and Union."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return {annotation}
    found = set()
    for argument in get_args(annotation):
        found |= _models_in(argument)
    return found


def _names_in(model, seen) -> set:
    """Every field of *model*, by name and alias, and those of the models its
    fields hold."""
    if model in seen:
        return set()
    seen.add(model)
    names = set()
    for name, field in model.model_fields.items():
        names |= {name, field.alias, field.validation_alias, field.serialization_alias}
        for nested in _models_in(field.annotation):
            names |= _names_in(nested, seen)
    return {name for name in names if isinstance(name, str)}


def _parameters(dependant) -> set:
    """Every query, path, header and cookie parameter of a route, by name and
    alias, through its dependencies."""
    names = set()
    for group in ("query", "path", "header", "cookie"):
        for param in getattr(dependant, f"{group}_params"):
            names |= {param.name, param.alias}
    for dependency in dependant.dependencies:
        names |= _parameters(dependency)
    return names


def _api_key_route_names() -> set:
    """Every name the mounted ``/api/v1/api-keys`` routes take or answer with:
    parameters, and the fields of the request and response models, read from
    the routes rather than named here."""
    names, models, methods = set(), set(), set()
    for ctx in _route_contexts():
        if ctx.path != API_KEYS and not ctx.path.startswith(f"{API_KEYS}/"):
            continue
        methods |= set(ctx.methods or ())
        names |= _parameters(ctx.dependant)
        models |= _models_in(ctx.response_model)
        if ctx.body_field is not None:
            models |= _models_in(ctx.body_field.field_info.annotation)
    assert {"GET", "POST", "DELETE"} <= methods, methods
    found = {model.__name__ for model in models}
    assert {"APIKeyCreate", "APIKeyRead", "APIKeyCreated"} <= found, found
    for model in models:
        names |= _names_in(model, set())
    return names


def _environment_names(names) -> set:
    assert names
    return {name for name in names if "env" in name.lower()}


@pytest.mark.parametrize("model", [FeatureFlag, Experiment])
def test_a_flag_row_and_an_experiment_carry_no_environment(model):
    """An experiment runs in production only. A flag's row is its production
    settings; the other two environments' are rows of their own."""
    assert _environment_names(model.__table__.columns.keys()) == set()


def test_an_api_key_carries_one_environment_wherever_a_user_meets_it():
    """``api_keys`` stores one, the routes take and answer it, and the audit
    trail records it (#2011). A second such name fails here, and the pages
    say what it is."""
    audited = set(AUDIT_VALUE_FIELDS[EntityType.API_KEY])
    audited |= set(AUDIT_NAME_ONLY_FIELDS[EntityType.API_KEY])
    stored = APIKey.__table__.columns.keys()
    for names in (stored, _api_key_route_names(), audited):
        assert _environment_names(names) == {"environment"}


def test_no_route_changes_an_api_key(routes):
    """So none changes a key's environment."""
    assert {
        (method, path)
        for method, path in routes
        if path == API_KEYS or path.startswith(f"{API_KEYS}/")
    } == {
        ("GET", API_KEYS),
        ("POST", API_KEYS),
        ("DELETE", f"{API_KEYS}/{{key_id}}"),
    }


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
#: Every route of two-step sign-in (#1812): an account's own (any role, local
#: provider), the code step of a sign-in, and a superuser's reset.
TWO_STEP_ROUTES = {
    ("GET", "/api/v1/users/me/2fa"),
    ("POST", "/api/v1/users/me/2fa/enroll"),
    ("POST", "/api/v1/users/me/2fa/confirm"),
    ("POST", "/api/v1/users/me/2fa/disable"),
    ("POST", "/api/v1/users/me/2fa/recovery-codes"),
    ("POST", "/api/v1/auth/mfa/verify"),
    ("POST", "/api/v1/auth/mfa/enroll"),
    ("POST", "/api/v1/auth/mfa/enroll/confirm"),
    ("DELETE", "/api/v1/admin/users/{user_id}/2fa"),
}

#: The one setting that requires two-step sign-in for roles.
TWO_STEP_ROLES_SETTING = "LOCAL_AUTH_2FA_REQUIRED_ROLES"


def test_a_role_can_require_two_step_sign_in_on_compose_helm_and_cdk(
    routes,
):
    """limitations.md and the README: an account turns it on for itself, with
    an authenticator app, and an operator can require it for roles with one
    setting, ``LOCAL_AUTH_2FA_REQUIRED_ROLES``. The production compose file
    passes it to the API and the Helm chart takes it through ``api.extraEnv``
    (``test_chart_render.py`` renders the documented example); the AWS CDK
    app passes it from the ``local_auth_2fa_required_roles`` context
    (``infrastructure/tests/test_two_step_roles_context.py`` synthesises it)."""
    from backend.app.core.config import Settings

    # The password step still takes an email and a password alone; the code
    # is a later request's.
    assert set(LoginRequest.model_fields) == {"email", "password"}
    pattern = re.compile(r"mfa|totp|otp\b|2fa|two-factor|second-factor|webauthn", re.I)
    assert {(m, p) for m, p in routes if pattern.search(p)} == TWO_STEP_ROUTES
    policy = re.compile(r"2FA|TWO_FACTOR|TOTP|MFA|SECOND_FACTOR", re.I)
    assert [name for name in Settings.model_fields if policy.search(name)] == [
        TWO_STEP_ROLES_SETTING
    ]
    compose = (ROOT / "deploy" / "compose" / "compose.yml").read_text(encoding="utf-8")
    assert f"{TWO_STEP_ROLES_SETTING}: ${{{TWO_STEP_ROLES_SETTING}:-}}" in compose
    (chart,) = sorted((ROOT / "charts").glob("*/values.yaml"))
    assert "extraEnv: []" in chart.read_text(encoding="utf-8")
    cdk = ROOT / "infrastructure" / "cdk" / "stacks" / "fargate_service_stack.py"
    cdk_source = cdk.read_text(encoding="utf-8")
    assert TWO_STEP_ROLES_SETTING in cdk_source
    assert "local_auth_2fa_required_roles" in cdk_source


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
def test_one_route_erases_an_app_users_data_and_none_sets_retention(routes):
    """The superuser erase (#2042) is the one route of its kind: an exact list
    of one, so a second erase or a retention route fails here."""
    pattern = re.compile(r"retention|erase|erasure|gdpr|forget|purge", re.I)
    assert sorted((m, p) for m, p in routes if pattern.search(p)) == [
        ("POST", "/api/v1/admin/app-users/erase")
    ]
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
#: A block comment, or a line comment (not the `//` of a URL's scheme).
_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\w])//[^\n]*", re.S)


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
    # Part of #1720: the attributes as compact JSON, through the same encode as
    # user_id.
    "ruby": (
        "sdk/ruby/lib/controlvar/client.rb",
        [("def evaluate_flag(", "data = {} unless data.is_a?(Hash)")],
        '"&context=#{encode(context)}"',
    ),
    # Part of #1720: the attributes as compact JSON, through rawurlencode, the
    # encoder user_id already goes through.
    "php": (
        "sdk/php/src/ExperimentationClient.php",
        [("public function evaluateFlag(", "} catch (ExperimentationException) {")],
        "'&context=' . rawurlencode($context)",
    ),
    # Part of #1720: the attributes as contextQuery writes them (a copy of the
    # JavaScript SDK's), after user_id.
    "edge": (
        "sdk/edge/src/client.ts",
        [("async evaluateFlag(", "const evaluation: FlagEvaluation = {")],
        "?user_id=${encode(userId)}${context}",
    ),
    # Part of #1720: the attributes as compact JSON, through the same encode as
    # user_id.
    "elixir": (
        "sdk/elixir/lib/controlvar/client.ex",
        [("def evaluate_flag(", "{:ok, data} when is_map(data) ->")],
        '"&context=#{encode(context_json)}"',
    ),
    # Since #438's iOS change: evaluateFlag builds the query itself, so that
    # `user_id` comes before `context`.
    "ios": (
        "sdk/ios/Sources/Controlvar/ExperimentationClient.swift",
        [("public func evaluateFlag(", "// MARK: - Experiments")],
        '"&context=\\(HTTPClient.encodeComponent(context))"',
    ),
    # Since #438's Android change: evaluateFlagPath adds `context` after
    # `user_id`, written by flagContext (sorted keys) and encoded by OkHttp.
    "android": (
        "sdk/android/sdk/src/main/kotlin/com/controlvar/android/ExperimentationClient.kt",
        [
            ("suspend fun evaluateFlag(", "suspend fun getAssignment("),
            ("private fun evaluateFlagPath(", "private fun send("),
        ],
        'addQueryParameter("context", context)',
    ),
    # Since #438's Flutter change: evaluateFlag hands the attributes to the
    # evaluate request as `context`, after `user_id`.
    "flutter": (
        "sdk/flutter/lib/src/client.dart",
        [("Future<EvalResult> evaluateFlag(", "Future<bool> isEnabled(")],
        "if (context.isNotEmpty) 'context': context",
    ),
}

#: Every directory under sdk/ that evaluates no flag at all: its source
#: directory, which must not name the evaluate route.
EVALUATES_NO_FLAG = {
    # #1545: the visual experiments' browser script asks which visual changes a
    # visitor gets, never a flag.
    "visual": "sdk/visual/src",
}

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
    """Every directory under sdk/ is in ``SENDS`` or ``EVALUATES_NO_FLAG``, and
    in exactly one. One in ``SENDS`` has a flag evaluation that sends the
    user's attributes as ``context``, so that a flag rule on attributes can
    match from it. A new directory fails until it is listed, with the marker
    that shows it does. One that evaluates no flag is in ``EVALUATES_NO_FLAG``,
    and fails the day its source names the evaluate route.

    An SDK in ``SENDS`` must still hand the attributes to its evaluate request:
    its marker, the code token that does so, is in its flag-evaluation code.

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
    assert not set(SENDS) & set(EVALUATES_NO_FLAG), (
        "an SDK is in both SENDS and EVALUATES_NO_FLAG"
    )

    classified = set(SENDS) | set(EVALUATES_NO_FLAG)
    failures = [
        f"classified SDK directory is gone: {name}"
        for name in sorted(classified - set(directories))
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
        elif sdk in EVALUATES_NO_FLAG:
            source = ROOT / EVALUATES_NO_FLAG[sdk]
            files = sorted(p for p in source.rglob("*") if p.is_file())
            if not files:
                failures.append(f"{sdk}: no source file under {EVALUATES_NO_FLAG[sdk]}")
            for path in files:
                if "feature-flags/evaluate" in path.read_text(encoding="utf-8"):
                    failures.append(
                        f"{sdk} evaluates a flag ({path.relative_to(ROOT)}): move it"
                        " to SENDS"
                    )
        else:
            failures.append(f"unclassified SDK directory: {sdk}")
    assert not failures, "\n".join(failures)
