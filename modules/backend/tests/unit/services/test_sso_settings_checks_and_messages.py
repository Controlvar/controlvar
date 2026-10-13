"""Single sign-on: the settings, what a SAML assertion must pass, and the messages (#1142).

The SAML cases run python3-saml itself on responses signed when the test runs
(the helpers of ``test_saml_library_validation.py``); each one changes one
thing about a response the control accepts. The OIDC cases call the service
and the routes with the database mocked.
"""

from __future__ import annotations

import ast
import base64
import datetime
import logging
import pathlib
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend.app.api import deps
from backend.app.core.config import settings as core_settings
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.user import UserRole
from modules.backend.app.api.v1.endpoints import sso as sso_endpoint
from modules.backend.app.models.sso_config import SSOConfig, SSOProviderType
from modules.backend.app.services import sso_service
from modules.backend.app.settings import ModulesSettings
from modules.backend.tests.unit.services import test_saml_library_validation as saml

REPO = pathlib.Path(__file__).resolve().parents[5]
TREES = [REPO / "backend" / "app", REPO / "modules" / "backend" / "app"]
SAML_NS = saml.SAML_NS
SAMLP_NS = saml.SAMLP_NS
ALICE = saml.ALICE


# ---------------------------------------------------------------------------
# The module
# ---------------------------------------------------------------------------


def test_sso_is_a_module_of_the_full_profile_under_apache_2():
    manifest = (REPO / "modules-manifest.txt").read_text()
    assert "modules/backend/app/services/sso_service.py" in manifest
    reuse = (REPO / "REUSE.toml").read_text()
    # One rule for everything but the SDKs: modules/ included.
    assert reuse.count("[[annotations]]") == 2
    assert (
        'path = ["**"]' in reuse and 'SPDX-License-Identifier = "Apache-2.0"' in reuse
    )
    assert 'path = ["sdk/**"]' in reuse
    for path in (REPO / "modules").rglob("*.py"):
        head = path.read_text(errors="ignore")[:400]
        assert "SPDX-License-Identifier" not in head or "Apache-2.0" in head, path
    # OpenID Connect providers, and GitHub's OAuth 2 (it has no ID token).
    assert {"okta", "google", "github"} <= set(sso_service._OIDC_PROVIDERS)
    assert not sso_service.uses_id_token("github")
    assert sso_service.uses_id_token("okta") and sso_service.uses_id_token("google")


def test_an_sso_session_is_a_local_token_that_cognito_does_not_accept(monkeypatch):
    # The dashboard's exchange answers the session a password sign-in issues.
    exchange = ast.unparse(
        next(
            f
            for f in ast.walk(
                ast.parse(pathlib.Path(sso_endpoint.__file__).read_text())
            )
            if isinstance(f, ast.AsyncFunctionDef) and f.name == "sso_exchange"
        )
    )
    assert "create_local_access_token(user)" in exchange
    user = SimpleNamespace(id=uuid.uuid4(), email="bob@acme.com", role=UserRole.VIEWER)
    token = create_local_access_token(user)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
        id=user.id, is_active=True
    )
    monkeypatch.setattr(core_settings, "DEV_AUTH_BYPASS", False, raising=False)

    monkeypatch.setattr(core_settings, "AUTH_PROVIDER", "local")
    assert deps.get_current_user(token=token, db=db).id == user.id

    monkeypatch.setattr(core_settings, "AUTH_PROVIDER", "cognito")
    with patch.object(
        deps.auth_service,
        "get_user_with_groups",
        side_effect=ValueError("not a Cognito token"),
    ):
        with pytest.raises(HTTPException) as refused:
            deps.get_current_user(token=token, db=db)
    assert refused.value.status_code == 401


# ---------------------------------------------------------------------------
# Groups and roles
# ---------------------------------------------------------------------------


def test_only_okta_asks_for_groups_among_the_sign_in_providers():
    scopes = {
        p: sso_service._OIDC_PROVIDERS[p]["scope"].split()
        for p in ("okta", "google", "github")
    }
    assert "groups" in scopes["okta"]
    assert "groups" not in scopes["google"] and "groups" not in scopes["github"]

    config = SimpleNamespace(role_mapping={"admins": "admin"})
    # A user info with no groups maps nothing: a new account is a viewer.
    assert sso_service.mapped_role(config, []) is None
    assert sso_service.map_role(config, []) == "viewer"


def test_role_mapping_keys_match_exactly_and_default_to_empty():
    config = SimpleNamespace(role_mapping={"Admins": "admin"})
    assert sso_service.mapped_role(config, ["admins", "ADMINS"]) is None
    assert sso_service.mapped_role(config, ["Admins"]) == "admin"
    created = sso_endpoint.SSOConfigCreate(
        org_name="Acme",
        org_domain="acme.com",
        provider_type="okta",
        entity_id="client-1",
    )
    assert created.role_mapping == {}


def test_entity_id_is_the_client_id_of_an_oidc_sign_in():
    config = SimpleNamespace(
        provider_type=SSOProviderType.GOOGLE, entity_id="client-123", sso_url=None
    )
    url = sso_service.build_oidc_authorization_url(
        config, "google", "https://app.example.com/cb", "state", "v" * 43
    )
    assert "client_id=client-123" in url


def test_provisioning_cannot_be_turned_off_and_is_active_stops_sign_in():
    fields = set(ModulesSettings.model_fields)
    assert not [f for f in fields if "JIT" in f or "PROVISION" in f]
    assert "is_active" in SSOConfig.__table__.c

    inactive = MagicMock(spec=SSOConfig)
    inactive.id = uuid.uuid4()
    inactive.provider_type = SSOProviderType.SAML
    inactive.is_active = False
    with patch.object(sso_service, "get_sso_config_by_id", return_value=inactive):
        with pytest.raises(HTTPException) as refused:
            sso_endpoint._active_saml_config(MagicMock(), inactive.id)
    assert refused.value.status_code == 404
    assert refused.value.detail == f"SSO config '{inactive.id}' is not active"


# ---------------------------------------------------------------------------
# The OIDC routes
# ---------------------------------------------------------------------------


def _request():
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
        }
    )


@pytest.mark.asyncio
async def test_the_api_sign_in_compares_org_domain_exactly():
    first = SimpleNamespace(
        provider_type=SSOProviderType.OKTA, is_active=True, org_domain="acme.com"
    )
    second = SimpleNamespace(
        provider_type=SSOProviderType.OKTA, is_active=True, org_domain="beta.io"
    )
    started = []

    def start(config, provider):
        started.append(config)
        return MagicMock()

    with (
        patch.object(sso_service, "list_sso_configs", return_value=[first, second]),
        patch.object(sso_service, "start_oidc_login", side_effect=start),
        patch.object(sso_endpoint, "_redirect_to_provider", return_value="redirect"),
    ):
        with pytest.raises(HTTPException) as refused:
            await sso_endpoint.oidc_login(
                "okta", _request(), org_domain="Beta.IO", db=None
            )
        assert refused.value.status_code == 404
        await sso_endpoint.oidc_login("okta", _request(), org_domain="beta.io", db=None)
        await sso_endpoint.oidc_login("okta", _request(), org_domain=None, db=None)
    assert started == [second, first]


def test_the_dashboard_sign_in_needs_domain_return_to_and_handoff():
    origin = "https://app.example.com"
    good = {"domain": "acme.com", "return_to": origin, "handoff": "a" * 43}
    app.dependency_overrides[deps.get_db] = lambda: MagicMock()
    try:
        with patch.object(
            sso_endpoint,
            "_dashboard_origin_for",
            side_effect=lambda r: r if r == origin else None,
        ):
            client = TestClient(app, follow_redirects=False)
            for missing in good:
                params = {k: v for k, v in good.items() if k != missing}
                response = client.get("/api/v1/auth/sso/login", params=params)
                assert response.status_code == 400, missing
            for bad in (
                {"handoff": "short"},
                {"domain": "not a domain"},
                {"return_to": "https://evil.example"},
            ):
                response = client.get("/api/v1/auth/sso/login", params={**good, **bad})
                assert response.status_code == 400, bad
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


def test_callback_refusals_and_failures_are_logged(caplog):
    refusal = sso_service.SSORefusal(403, "nope", sso_service.SSO_DOMAIN)
    with caplog.at_level(logging.WARNING, logger=sso_endpoint.logger.name):
        sso_endpoint._log_refusal("callback", refusal, refusal.sso_error)
    (record,) = [r for r in caplog.records if "refused" in r.getMessage()]
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    for part in ("sso_error=sso_domain", "status=403", "detail='nope'", "request_id="):
        assert part in message, part

    tree = ast.parse(pathlib.Path(sso_endpoint.__file__).read_text())
    (callback,) = [
        f
        for f in ast.walk(tree)
        if isinstance(f, ast.AsyncFunctionDef) and f.name == "_redirect_mode_callback"
    ]
    handlers = [h for h in ast.walk(callback) if isinstance(h, ast.ExceptHandler)]
    generic = [
        h for h in handlers if isinstance(h.type, ast.Name) and h.type.id == "Exception"
    ]
    assert generic and all(
        any(isinstance(n, ast.Attribute) and n.attr == "exception" for n in ast.walk(h))
        for h in generic
    ), "an unexpected error is logged with logger.exception (ERROR with traceback)"


# ---------------------------------------------------------------------------
# Settings and the compose stack
# ---------------------------------------------------------------------------


def test_the_saml_sp_settings_and_their_defaults():
    defaults = ModulesSettings(_env_file=None)
    assert defaults.SAML_SP_ENTITY_ID == "https://experimently.example.com"
    assert (
        defaults.SAML_SP_ACS_URL == "https://experimently.example.com/auth/sso/saml/acs"
    )
    routes = {getattr(r, "path", "") for r in app.routes}
    paths = set(app.openapi()["paths"])
    assert "/auth/sso/saml/acs" not in routes | paths


def test_the_acs_url_is_one_setting_for_every_configuration():
    seen = []

    class Auth:
        def __init__(self, request, settings):
            seen.append(settings)

        def process_response(self):
            raise RuntimeError("stop here")

    with patch.object(sso_service, "OneLogin_Saml2_Auth", Auth):
        for _ in range(2):
            with pytest.raises(HTTPException):
                sso_service._parse_saml_response_with_library(
                    saml._config("cert"), base64.b64encode(b"<x/>").decode()
                )
    urls = {s["sp"]["assertionConsumerService"]["url"] for s in seen}
    assert urls == {sso_service.settings.SAML_SP_ACS_URL}
    assert {s["sp"]["entityId"] for s in seen} == {
        sso_service.settings.SAML_SP_ENTITY_ID
    }
    assert all(s["strict"] is True for s in seen)


def test_provider_client_ids_and_secrets_are_configuration_fields_and_the_oidc_settings_are_unread():
    assert {"entity_id", "client_secret"} <= set(SSOConfig.__table__.c.keys())
    unread = [f for f in ModulesSettings.model_fields if f.startswith("OIDC_")]
    assert sorted(unread) == sorted(
        f"OIDC_{p}_{k}"
        for p in ("GOOGLE", "GITHUB", "MICROSOFT")
        for k in ("CLIENT_ID", "CLIENT_SECRET")
    )
    for tree in TREES:
        for path in tree.rglob("*.py"):
            if path.name == "settings.py" and "modules" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                name = (
                    node.attr
                    if isinstance(node, ast.Attribute)
                    else (
                        node.value
                        if isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        else None
                    )
                )
                assert name not in unread, f"{path} reads {name}"


def test_compose_passes_the_api_only_the_variables_it_names():
    api = yaml.safe_load((REPO / "docker-compose.yml").read_text())["services"]["api"]
    assert "env_file" not in api
    assert isinstance(api["environment"], dict)
    assert "PUBLIC_BASE_URL" not in api["environment"]


def test_saml_comes_from_python3_saml_in_the_modules_lock():
    requirements = (REPO / "modules" / "requirements.txt").read_text()
    lock = (REPO / "modules" / "requirements.lock").read_text()
    assert "python3-saml==" in requirements
    assert "\npython3-saml==" in lock and "\nxmlsec==" in lock
    dockerfile = (REPO / "backend" / "Dockerfile").read_text()
    assert "pip install --require-hashes -r /tmp/modules.lock" in dockerfile
    assert (
        "COPY --from=modules-src /modules/requirements.lock /tmp/modules.lock"
        in dockerfile
    )
    import xmlsec

    # The binding is built against libxmlsec1 and libxml2.
    assert xmlsec.get_libxmlsec_version() and xmlsec.get_libxml_version()


# ---------------------------------------------------------------------------
# SAML assertion checks (python3-saml, real signatures)
# ---------------------------------------------------------------------------

IDP = saml.IDP_ENTITY_ID


def _instant(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _assertion(
    *,
    issuer=IDP,
    audience="default",
    recipient="default",
    not_before=-60,
    not_on_or_after=300,
    name_id=ALICE,
    attributes=(("groups", "analysts"),),
):
    now = datetime.datetime.now(datetime.timezone.utc)
    if audience == "default":
        audience = sso_service.settings.SAML_SP_ENTITY_ID
    if recipient == "default":
        recipient = sso_service.settings.SAML_SP_ACS_URL
    later = _instant(now + datetime.timedelta(seconds=max(not_on_or_after, 300)))
    assertion_id = f"_{uuid.uuid4().hex}"
    audience_xml = (
        "<saml:AudienceRestriction>"
        f"<saml:Audience>{audience}</saml:Audience>"
        "</saml:AudienceRestriction>"
        if audience
        else ""
    )
    recipient_xml = f' Recipient="{recipient}"' if recipient else ""
    values = "".join(
        f'<saml:Attribute Name="{k}"><saml:AttributeValue>{v}</saml:AttributeValue></saml:Attribute>'
        for k, v in attributes
    )
    return (
        f'<saml:Assertion xmlns:saml="{SAML_NS}" ID="{assertion_id}" Version="2.0"'
        f' IssueInstant="{_instant(now)}">'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        "<saml:Subject>"
        f"<saml:NameID>{name_id}</saml:NameID>"
        '<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'<saml:SubjectConfirmationData NotOnOrAfter="{later}"{recipient_xml}/>'
        "</saml:SubjectConfirmation>"
        "</saml:Subject>"
        f'<saml:Conditions NotBefore="{_instant(now + datetime.timedelta(seconds=not_before))}"'
        f' NotOnOrAfter="{_instant(now + datetime.timedelta(seconds=not_on_or_after))}">'
        f"{audience_xml}</saml:Conditions>"
        f'<saml:AuthnStatement AuthnInstant="{_instant(now)}" SessionIndex="{assertion_id}">'
        "<saml:AuthnContext><saml:AuthnContextClassRef>"
        "urn:oasis:names:tc:SAML:2.0:ac:classes:Password"
        "</saml:AuthnContextClassRef></saml:AuthnContext>"
        "</saml:AuthnStatement>"
        + (
            f"<saml:AttributeStatement>{values}</saml:AttributeStatement>"
            if values
            else ""
        )
        + "</saml:Assertion>"
    )


def _response(assertion, destination="default"):
    if destination == "default":
        destination = sso_service.settings.SAML_SP_ACS_URL
    now = _instant(datetime.datetime.now(datetime.timezone.utc))
    destination_xml = f' Destination="{destination}"' if destination else ""
    return (
        f'<samlp:Response xmlns:samlp="{SAMLP_NS}" xmlns:saml="{SAML_NS}"'
        f' ID="_{uuid.uuid4().hex}" Version="2.0" IssueInstant="{now}"{destination_xml}>'
        f"<saml:Issuer>{IDP}</saml:Issuer>"
        '<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>'
        "</samlp:Status>" + assertion + "</samlp:Response>"
    )


@pytest.fixture(scope="module")
def key():
    assert sso_service._SAML_AVAILABLE is True, "python3-saml is not importable"
    return saml._new_signing_key("idp.example.com")


def _parse(key, assertion_kwargs=None, destination="default", certificate=None):
    assertion = saml._signed(_assertion(**(assertion_kwargs or {})), key)
    return saml._parse(
        certificate or key.certificate_pem, _response(assertion, destination)
    )


ACCEPTED = {
    "control": {},
    "no-audience": {"assertion": {"audience": None}},
    "no-destination-or-recipient": {
        "assertion": {"recipient": None},
        "destination": None,
    },
    "not-before-four-minutes-ahead": {"assertion": {"not_before": 240}},
    "expired-four-minutes-ago": {
        "assertion": {"not_on_or_after": -240, "not_before": -900}
    },
}
REFUSED = {
    "issuer-is-not-the-entity-id": {
        "assertion": {"issuer": "https://other-idp.example.com"}
    },
    "audience-is-not-this-service": {
        "assertion": {"audience": "https://someone-else.example.com"}
    },
    "destination-is-not-the-acs-url": {
        "destination": "https://elsewhere.example.com/acs"
    },
    "recipient-is-not-the-acs-url": {
        "assertion": {"recipient": "https://elsewhere.example.com/acs"}
    },
    "not-before-six-minutes-ahead": {"assertion": {"not_before": 360}},
    "expired-six-minutes-ago": {
        "assertion": {"not_on_or_after": -360, "not_before": -900}
    },
}


@pytest.mark.parametrize("case", list(ACCEPTED))
def test_an_assertion_that_passes_every_check_is_accepted(key, case):
    spec = ACCEPTED[case]
    info = _parse(key, spec.get("assertion"), spec.get("destination", "default"))
    assert info["email"] == ALICE


@pytest.mark.parametrize("case", list(REFUSED))
def test_each_check_refuses_an_assertion_that_fails_it(key, case):
    spec = REFUSED[case]
    with pytest.raises(HTTPException) as refused:
        _parse(key, spec.get("assertion"), spec.get("destination", "default"))
    assert refused.value.status_code == 401
    assert refused.value.detail.startswith("SAML validation failed: ")


def test_the_certificate_may_have_or_lack_its_pem_lines(key):
    bare = "".join(
        line for line in key.certificate_pem.splitlines() if "CERTIFICATE" not in line
    )
    assert "BEGIN" not in bare
    assert _parse(key, certificate=bare)["email"] == ALICE
    assert _parse(key, certificate=key.certificate_pem)["email"] == ALICE


def test_the_email_is_the_name_id_with_an_at_else_the_email_attribute_else_the_claim(
    key,
):
    claim = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
    assert (
        _parse(key, {"name_id": ALICE, "attributes": [("email", "x@y.z")]})["email"]
        == ALICE
    )
    assert (
        _parse(
            key,
            {
                "name_id": "u-123",
                "attributes": [("email", "bob@example.com"), (claim, "c@d.e")],
            },
        )["email"]
        == "bob@example.com"
    )
    assert (
        _parse(key, {"name_id": "u-123", "attributes": [(claim, "carol@example.com")]})[
            "email"
        ]
        == "carol@example.com"
    )


def test_only_the_http_post_binding_and_no_authentication_request():
    acs = [
        r
        for r in app.routes
        if getattr(r, "path", "") == "/api/v1/auth/sso/saml/{config_id}/acs"
    ] or [
        ctx
        for r in app.routes
        for ctx in (getattr(r, "effective_route_contexts", lambda: ())() or ())
        if ctx.path == "/api/v1/auth/sso/saml/{config_id}/acs"
    ]
    assert {m for r in acs for m in r.methods} == {"POST"}
    source = pathlib.Path(sso_service.__file__).read_text()
    assert source.count("bindings:HTTP-POST") >= 2
    assert "HTTP-Artifact" not in source
    # IdP-initiated: nothing builds or sends an AuthnRequest.
    for tree in TREES:
        for path in tree.rglob("*.py"):
            text = path.read_text()
            assert "AuthnRequest(" not in text, path
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    assert not (
                        node.func.attr == "login"
                        and "Saml2" in ast.unparse(node.func.value)
                    ), path


def test_a_configuration_holds_one_certificate():
    assert str(SSOConfig.__table__.c.x509_certificate.type) == "TEXT"
    source = pathlib.Path(sso_service.__file__).read_text()
    assert '"x509cert": config.x509_certificate' in source
    assert "x509certMulti" not in source


# ---------------------------------------------------------------------------
# The messages the troubleshooting section quotes
# ---------------------------------------------------------------------------


def test_an_expired_sign_in_says_so(monkeypatch):
    config = MagicMock(spec=SSOConfig)
    config.id = uuid.uuid4()
    config.provider_type = SSOProviderType.OKTA
    past = sso_service.time.time() - sso_service.OIDC_STATE_TTL_SECONDS - 3600
    monkeypatch.setattr(sso_service.time, "time", lambda: past)
    started = sso_service.start_oidc_login(config, "okta")
    monkeypatch.undo()
    with pytest.raises(sso_service.SSORefusal) as refused:
        sso_service.redeem_oidc_state(started.cookie_value, started.state)
    assert refused.value.detail == "OIDC sign-in expired; start it again"
    assert refused.value.sso_error == "sso_expired"


@pytest.mark.asyncio
async def test_an_unverified_or_off_domain_email_says_so():
    config = MagicMock(spec=SSOConfig)
    config.id = uuid.uuid4()
    config.provider_type = SSOProviderType.OKTA
    config.org_domain = "acme.com"
    claims = {"sub": "s-1", "email": "bob@acme.com", "email_verified": False}
    with pytest.raises(sso_service.SSORefusal) as unverified:
        await sso_service.verified_email(config, "okta", claims, {"sub": "s-1"}, "at")
    assert unverified.value.detail == (
        "The identity provider has not verified this account's email address"
    )
    assert unverified.value.sso_error == "sso_unverified"

    with pytest.raises(sso_service.SSORefusal) as off_domain:
        sso_service.provision_user(MagicMock(), {"email": "bob@eu.acme.com"}, config)
    assert off_domain.value.detail == (
        "This account's email is not in the domain this single sign-on is configured for"
    )
    assert off_domain.value.sso_error == "sso_domain"


def test_a_second_configuration_for_a_domain_says_so():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = MagicMock(
        spec=SSOConfig
    )
    with pytest.raises(HTTPException) as refused:
        sso_service.create_sso_config(
            db,
            {
                "org_name": "Acme",
                "org_domain": " @ACME.com",
                "provider_type": SSOProviderType.GOOGLE,
                "entity_id": "client-1",
            },
        )
    assert refused.value.status_code == 409
    assert refused.value.detail == "An SSO configuration already exists for this domain"


def test_saml_without_its_library_says_so(monkeypatch):
    monkeypatch.setattr(sso_service, "_SAML_AVAILABLE", False)
    monkeypatch.setattr(core_settings, "ENVIRONMENT", "production")
    with pytest.raises(HTTPException) as refused:
        sso_service._require_saml_support()
    assert refused.value.status_code == 501
    assert refused.value.detail.startswith(
        "SAML single sign-on is not available in this deployment: "
    )


def test_the_sign_in_cookie_is_host_only_secure_http_only_and_lasts_ten_minutes():
    from starlette.responses import Response

    response = Response()
    sso_endpoint._set_state_cookie(
        response, "value", sso_service.OIDC_STATE_TTL_SECONDS
    )
    cookie = response.headers["set-cookie"]
    parts = [p.strip().lower() for p in cookie.split(";")]
    # __Host-: no Domain attribute, path /, Secure -- the cookie belongs to
    # the one host that set it.
    assert cookie.startswith("__Host-experimently_oidc=")
    assert "secure" in parts and "httponly" in parts and "samesite=lax" in parts
    assert "path=/" in parts and not [p for p in parts if p.startswith("domain=")]
    assert sso_service.OIDC_STATE_TTL_SECONDS == 600


def test_every_sso_route_is_under_api_v1():
    paths = {
        ctx.path
        for route in app.routes
        for ctx in (getattr(route, "effective_route_contexts", lambda: ())() or ())
        if getattr(getattr(ctx, "endpoint", None), "__module__", None)
        == sso_endpoint.__name__
    }
    assert paths and all(p.startswith("/api/v1/auth/sso/") for p in paths), paths
