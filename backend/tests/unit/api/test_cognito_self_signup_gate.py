"""
Self sign-up under Cognito answers 404 unless ``COGNITO_SELF_SIGNUP_ENABLED``
is true (T94).

``POST /api/v1/auth/signup`` and ``/confirm`` carry
``_require_cognito_self_signup``: the provider check as a sub-dependency, then
the setting.  So:

* under ``local`` (or any provider but ``cognito``) both answer the same 404
  as the other Cognito-only routes, ``COGNITO_ONLY_DETAIL``, whatever the
  setting says;
* under ``cognito`` with the setting false or left at its default, both
  answer 404 with their own fixed detail, before the body is validated and
  before any ``CognitoAuthService`` or AWS client exists;
* under ``cognito`` with the setting true the request reaches the service.

The other three Cognito-only routes (forgot-password, reset-password, refresh)
do not depend on the setting.

Beyond the routes, two inventories keep a new path to sign-up from appearing
unnoticed: the public members of ``CognitoAuthService`` are classified, and
every place in ``backend/`` and ``modules/`` (tests excluded) that names
``sign_up``/``confirm_sign_up`` or Cognito's ``SignUp``/``ConfirmSignUp``
operations is pinned, read from the filesystem with ``ast``.

No database and no modules are needed.  The AWS credential chain is blanked,
the service is a recording stub, and every boto3 client creation is recorded.
"""

import ast
import inspect
import os
from pathlib import Path
from typing import Any, List, Set, Tuple
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.app.api.v1.endpoints import auth as auth_endpoints
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.services.auth_service import CognitoAuthService
from backend.tests.unit.api.test_cognito_only_routes import (
    FIXED as COGNITO_ONLY_FIXED,
)
from backend.tests.unit.api.test_cognito_only_routes import (
    LOW_LEVEL_SESSION,
    PREFIX,
    STUB_CALLS,
    VALID_BODIES,
    RecordingCognitoService,
    _mounted_routes,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

#: Spelled out, not imported: a change to the message has to change this test.
SELF_SIGNUP_OFF = (
    "Endpoint not available: self sign-up is turned off "
    "(COGNITO_SELF_SIGNUP_ENABLED is not true). "
    "An administrator creates users in the Cognito user pool."
)

SIGNUP = f"{PREFIX}/signup"
CONFIRM = f"{PREFIX}/confirm"
SELF_SIGNUP_ROUTES = (SIGNUP, CONFIRM)
SIBLINGS = (
    f"{PREFIX}/forgot-password",
    f"{PREFIX}/reset-password",
    f"{PREFIX}/refresh",
)
BODY_KINDS = ("valid", "empty-object", "no-body")

REPO_ROOT = Path(__file__).resolve().parents[4]


def _route_name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


@pytest.fixture
def recorders(monkeypatch) -> List[Tuple[Any, ...]]:
    """Blank the AWS chain and record every Cognito service and AWS client."""
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    clients: List[Tuple[Any, ...]] = []

    def record_client(self, *args: Any, **kwargs: Any) -> MagicMock:
        clients.append(args)
        return MagicMock()

    monkeypatch.setattr(LOW_LEVEL_SESSION, "create_client", record_client)
    RecordingCognitoService.constructed = []
    RecordingCognitoService.calls = []
    monkeypatch.setattr(auth_endpoints, "CognitoAuthService", RecordingCognitoService)
    return clients


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _post(client: TestClient, path: str, body_kind: str):
    if body_kind == "valid":
        return client.post(path, json=VALID_BODIES[path])
    if body_kind == "empty-object":
        return client.post(path, json={})
    return client.post(path)


def _assert_refused(response, detail: str, recorders) -> None:
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": detail}
    assert RecordingCognitoService.constructed == []
    assert RecordingCognitoService.calls == []
    assert recorders == []


# ---------------------------------------------------------------------------
# U1-U4, U6: the provider x setting matrix on the two routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body_kind", BODY_KINDS)
@pytest.mark.parametrize("path", SELF_SIGNUP_ROUTES, ids=_route_name)
def test_cognito_default_answers_fixed_404(
    path, body_kind, client, recorders, monkeypatch
):
    """U1 (regression): the setting is left at its default.

    It does not touch ``COGNITO_SELF_SIGNUP_ENABLED`` at all, so the default
    is what is tested (and the test does not fail for the wrong reason on
    code that lacks the attribute).  The no-body and ``{}`` cells show the
    refusal comes before body validation (U6); ``constructed == []`` shows it
    comes before the service is built.
    """
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")

    _assert_refused(_post(client, path, body_kind), SELF_SIGNUP_OFF, recorders)


@pytest.mark.parametrize("body_kind", BODY_KINDS)
@pytest.mark.parametrize("path", SELF_SIGNUP_ROUTES, ids=_route_name)
def test_cognito_setting_false_answers_fixed_404(
    path, body_kind, client, recorders, monkeypatch
):
    """U2: an explicit False is refused the same way."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    monkeypatch.setattr(settings, "COGNITO_SELF_SIGNUP_ENABLED", False)

    _assert_refused(_post(client, path, body_kind), SELF_SIGNUP_OFF, recorders)


@pytest.mark.parametrize("path", SELF_SIGNUP_ROUTES, ids=_route_name)
def test_cognito_setting_true_reaches_the_service(path, client, recorders, monkeypatch):
    """U3: with the setting on, exactly the one service call is made."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    monkeypatch.setattr(settings, "COGNITO_SELF_SIGNUP_ENABLED", True)
    method, body, expected_status = STUB_CALLS[path]

    response = _post(client, path, "valid")

    assert response.status_code == expected_status, response.text
    assert response.json() == body
    assert RecordingCognitoService.constructed == [None]
    assert [name for name, _ in RecordingCognitoService.calls] == [method]
    assert recorders == []


@pytest.mark.parametrize(
    "enabled", [True, False], ids=["setting-true", "setting-false"]
)
@pytest.mark.parametrize("body_kind", BODY_KINDS)
@pytest.mark.parametrize("path", SELF_SIGNUP_ROUTES, ids=_route_name)
def test_local_provider_keeps_the_cognito_only_404(
    path, body_kind, enabled, client, recorders, monkeypatch
):
    """U4: under ``local`` the setting changes nothing, byte for byte."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "COGNITO_SELF_SIGNUP_ENABLED", enabled)

    response = _post(client, path, body_kind)

    _assert_refused(response, COGNITO_ONLY_FIXED, recorders)
    assert response.content == (
        b'{"detail":"' + COGNITO_ONLY_FIXED.encode("utf-8") + b'"}'
    )


# ---------------------------------------------------------------------------
# U7: the three sibling routes do not read the setting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("enabled", [None, False, True], ids=["unset", "false", "true"])
@pytest.mark.parametrize("path", SIBLINGS, ids=_route_name)
def test_sibling_routes_ignore_the_setting(
    path, enabled, client, recorders, monkeypatch
):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    if enabled is not None:
        monkeypatch.setattr(settings, "COGNITO_SELF_SIGNUP_ENABLED", enabled)
    method, _, expected_status = STUB_CALLS[path]

    response = _post(client, path, "valid")

    assert response.status_code == expected_status, response.text
    assert [name for name, _ in RecordingCognitoService.calls] == [method]


# ---------------------------------------------------------------------------
# U8 / I1: the gate sits, mounted, on exactly the two routes
# ---------------------------------------------------------------------------


def _calls(dependant, target) -> bool:
    """True if ``dependant`` or any sub-dependency calls ``target``."""
    return any(
        dep.call is target or _calls(dep, target) for dep in dependant.dependencies
    )


def test_exactly_signup_and_confirm_carry_the_self_signup_gate():
    """I1/U8: read from the mounted app, so a route that is unmounted when the
    setting is off (an answer of ``{"detail":"Not Found"}``) cannot pass."""
    routes = list(_mounted_routes())
    gated = {
        (path, method)
        for path, method, ctx in routes
        if _calls(ctx.dependant, auth_endpoints._require_cognito_self_signup)
    }

    assert gated == {(SIGNUP, "POST"), (CONFIRM, "POST")}


def test_the_self_signup_gate_runs_the_provider_check_first():
    """The provider check is a sub-dependency of the gate, so under ``local``
    the answer stays ``COGNITO_ONLY_DETAIL`` and the five-route pin in
    test_cognito_only_routes.py still sees both routes."""
    gate = auth_endpoints._require_cognito_self_signup
    params = list(inspect.signature(gate).parameters.values())
    assert [p.default.dependency for p in params] == [
        auth_endpoints._require_cognito_provider
    ]


# ---------------------------------------------------------------------------
# I2: the service's public members are classified
# ---------------------------------------------------------------------------

SELF_SERVICE = {"sign_up", "confirm_sign_up"}
OTHER_MEMBERS = {
    "client",
    "confirm_forgot_password",
    "forgot_password",
    "get_user",
    "get_user_with_groups",
    # Sign-out (POST /auth/logout under Cognito), after the token check.
    "global_sign_out",
    "refresh_token",
    "sign_in",
}


def test_every_public_service_member_is_classified():
    """A new self-service method (``resend_confirmation_code``, say) must be
    classified here, and then gated, before it can ship."""
    public = {name for name in vars(CognitoAuthService) if not name.startswith("_")}
    assert public == SELF_SERVICE | OTHER_MEMBERS
    assert not SELF_SERVICE & OTHER_MEMBERS


# ---------------------------------------------------------------------------
# I3: every place in backend/ and modules/ that can reach sign-up
# ---------------------------------------------------------------------------

#: Attribute names and string constants that reach Cognito sign-up: the
#: service methods, boto3's method names, and Cognito's operation names (what
#: ``client._make_api_call("SignUp", ...)`` takes).
SIGN_UP_ATTRS = frozenset({"sign_up", "confirm_sign_up"})
SIGN_UP_STRINGS = frozenset({"sign_up", "confirm_sign_up", "SignUp", "ConfirmSignUp"})

#: Directories never scanned: tests, caches, dependencies, build output.
PRUNED_DIRS = frozenset(
    {"tests", "__pycache__", "node_modules", "venv", ".venv", "cdk.out"}
)

#: (file, enclosing function, what was found).  The two routes are the gated
#: ones in I1; the two service methods are what they call.
EXPECTED_SITES: Set[Tuple[str, str, str]] = {
    ("backend/app/api/v1/endpoints/auth.py", "confirm_signup", "confirm_sign_up"),
    ("backend/app/api/v1/endpoints/auth.py", "signup", "sign_up"),
    (
        "backend/app/services/auth_service.py",
        "CognitoAuthService.confirm_sign_up",
        "confirm_sign_up",
    ),
    ("backend/app/services/auth_service.py", "CognitoAuthService.sign_up", "sign_up"),
}


def sign_up_sites(root: Path) -> Set[Tuple[str, str, str]]:
    """Every attribute named ``sign_up``/``confirm_sign_up`` and every string
    constant naming sign-up, under ``root/backend`` and ``root/modules``.

    Walks the filesystem (not git), so it also runs in core_build.sh's copy,
    which has no ``.git`` and no ``modules/``.  A string constant is reported
    as ``'<value>'`` so it reads apart from an attribute.
    """
    found: Set[Tuple[str, str, str]] = set()
    for top in ("backend", "modules"):
        base = root / top
        if not base.is_dir():
            continue  # a core tree has no modules/
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in PRUNED_DIRS)
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                path = Path(dirpath) / filename
                rel = path.relative_to(root).as_posix()
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
                _collect(tree, [], rel, found)
    return found


def _collect(node: ast.AST, scope: List[str], rel: str, found: set) -> None:
    for child in ast.iter_child_nodes(node):
        inner = scope
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            inner = scope + [child.name]
        where = ".".join(scope) or "<module>"
        if isinstance(child, ast.Attribute) and child.attr in SIGN_UP_ATTRS:
            found.add((rel, where, child.attr))
        if (
            isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and child.value in SIGN_UP_STRINGS
        ):
            found.add((rel, where, repr(child.value)))
        _collect(child, inner, rel, found)


def test_every_sign_up_site_is_pinned():
    assert sign_up_sites(REPO_ROOT) == EXPECTED_SITES


@pytest.mark.parametrize(
    "source, expected",
    [
        ("svc.sign_up(username='x')", ("helper", "sign_up")),
        ("getattr(svc, 'confirm_sign_up')()", ("helper", "'confirm_sign_up'")),
        ("functools.partial(svc.confirm_sign_up)", ("helper", "confirm_sign_up")),
        ("svc.client._make_api_call('SignUp', {})", ("helper", "'SignUp'")),
        ("op = 'ConfirmSignUp'", ("helper", "'ConfirmSignUp'")),
    ],
    ids=["call", "getattr", "partial", "make_api_call", "operation-name"],
)
def test_the_scan_sees_each_shape(tmp_path, source, expected):
    """Not vacuous: each way of reaching sign-up is reported, with its function."""
    planted = tmp_path / "backend" / "app" / "planted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(f"def helper(svc):\n    {source}\n", encoding="utf-8")
    (tmp_path / "backend" / "tests").mkdir()
    (tmp_path / "backend" / "tests" / "ignored.py").write_text(
        "svc.sign_up()\n", encoding="utf-8"
    )

    assert sign_up_sites(tmp_path) == {("backend/app/planted.py", *expected)}
