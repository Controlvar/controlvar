"""Sign in with SSO from the dashboard (C2b, #66), through the real callback.

The browser is the test client, the provider is ``fake_oidc_provider.py`` in
its own process, reached over real HTTP -- as in ``test_sso_oidc_flow.py``.
What this pins, against ``launch-readiness/stream-c/c2b/spec-v3.md``:

* ``GET /api/v1/auth/sso/login`` -- every parameter required, a missing or
  malformed one a 400 JSON answer and never an unbound login; ``return_to``
  checked against the *dashboard* origins, not the CORS list;
* the callback's redirect mode -- one ``sso_error`` code per refusal on
  main, in the order the spec gives, each reached through the real callback;
  the four redirect parameters and nothing else;
* the hand-off -- the fragment carries a 60 s code bound to the tab's
  secret, never the access token; ``POST /exchange`` and its bounds;
* the 302 carries its own literal ``Set-Cookie``; the request id in the URL
  is the response's ``X-Request-ID``.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Iterator, List, Optional
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings as core_settings
from backend.app.db.session import get_db as _session_get_db
from backend.app.main import app
from backend.app.middleware.rate_limiter import RATE_LIMIT_CONFIG
from backend.app.models.audit_log import AuditLog
from backend.app.models.user import User, UserRole
from backend.tests.integration.email_lower_index import (
    # A session-scoped autouse fixture: imported, it takes effect here.
    email_lower_index_survives_the_session,
    without_email_lower_index,
)
from modules.backend.app.models.sso_config import SSOConfig, SSOProviderType
from modules.backend.app.services import sso_service

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

PROVIDER_SCRIPT = Path(__file__).with_name("fake_oidc_provider.py")
CLIENT_ID = "dash-client"
CLIENT_SECRET = "dash-secret"
LOGIN = "/api/v1/auth/sso/login"
EXCHANGE = "/api/v1/auth/sso/exchange"
DASH = "https://dash.example.com"

#: The known-answer vector (spec-v3 §1): the secret is the 32 bytes 00..1f,
#: and HH was computed once with
#:   openssl dgst -sha256 -binary <those bytes> | openssl base64 -A | tr '+/' '-_' | tr -d '='
#: The dashboard's jest suite pins the same pair.
SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
HH = "Yw3NKWbEM2aRElRIu7JbT_QSpJxzLbLIq8G4WBvXEN0"

EXPIRING_COOKIE = (
    f'{sso_service.OIDC_STATE_COOKIE}=""; HttpOnly; Max-Age=0; Path=/; '
    "SameSite=lax; Secure"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def domain() -> str:
    return f"dash-{uuid.uuid4().hex[:8]}.example.com"


@pytest.fixture
def email(domain) -> str:
    return f"dash-user-{uuid.uuid4().hex[:8]}@{domain}"


@pytest.fixture
def mode() -> str:
    """The fake provider's mode; overridden per test."""
    return "good"


@pytest.fixture
def groups() -> str:
    return ""


@pytest.fixture
def provider(email, mode, groups) -> Iterator[str]:
    proc = subprocess.Popen(
        [sys.executable, str(PROVIDER_SCRIPT), CLIENT_ID, CLIENT_SECRET, email],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "FAKE_OIDC_ID_TOKEN": mode, "FAKE_OIDC_GROUPS": groups},
    )
    first: List[str] = []
    reader = threading.Thread(
        target=lambda: first.append(proc.stdout.readline()), daemon=True
    )
    reader.start()
    reader.join(timeout=20)
    line = first[0] if first else ""
    if not line.startswith("READY "):
        proc.kill()
        _, err = proc.communicate(timeout=5)
        pytest.fail(f"the fake OIDC provider did not start: {line!r} {err}")
    try:
        yield f"http://127.0.0.1:{int(line.split()[1])}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def role_mapping() -> Dict[str, str]:
    return {}


@pytest.fixture
def config(db_session: Session, provider: str, domain: str, role_mapping) -> SSOConfig:
    cfg = SSOConfig(
        org_name="Dashboard Org",
        org_domain=domain,
        provider_type=SSOProviderType.OKTA,
        entity_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        sso_url=provider,
        role_mapping=role_mapping,
        is_enforced=False,
        is_active=True,
    )
    db_session.add(cfg)
    db_session.commit()
    return cfg


@pytest.fixture
def browser(db_session: Session) -> Iterator[TestClient]:
    """An anonymous client: only the database is overridden, not authentication."""
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )

    def override_get_db():
        session = factory()
        session.execute(text("SET search_path TO test_experimentation"))
        try:
            yield session
        finally:
            session.close()

    saved = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[_session_get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


@pytest.fixture
def dashboard(monkeypatch) -> str:
    """One configured dashboard origin, and no PUBLIC_BASE_URL."""
    monkeypatch.setattr(core_settings, "DASHBOARD_ORIGINS", [DASH])
    monkeypatch.setattr(core_settings, "PUBLIC_BASE_URL", None)
    return DASH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start(
    browser: TestClient,
    domain: str,
    *,
    return_to: Optional[str] = DASH,
    handoff: Optional[str] = HH,
):
    params = {"domain": domain, "return_to": return_to, "handoff": handoff}
    return browser.get(
        LOGIN,
        params={k: v for k, v in params.items() if v is not None},
        follow_redirects=False,
    )


def _cookie_of(resp) -> str:
    (set_cookie,) = resp.headers.get_list("set-cookie")
    cookie = set_cookie.split(";", 1)[0]
    assert cookie.startswith(f"{sso_service.OIDC_STATE_COOKIE}=")
    return cookie


def _claims_of(cookie: str) -> Dict:
    value = cookie.split("=", 1)[1]
    return jwt.decode(value, options={"verify_signature": False})


def _to_provider_and_back(browser: TestClient, domain: str, return_to: str = DASH):
    """Start from the dashboard, visit the provider; (callback path, cookie)."""
    login = _start(browser, domain, return_to=return_to)
    assert login.status_code == 302, login.text
    cookie = _cookie_of(login)
    browser.cookies.clear()
    at_provider = requests.get(
        login.headers["location"], allow_redirects=False, timeout=10
    )
    assert at_provider.status_code == 302, at_provider.text
    back = urlparse(at_provider.headers["location"])
    return f"{back.path}?{back.query}", cookie


def _sign_in(browser: TestClient, domain: str):
    callback, cookie = _to_provider_and_back(browser, domain)
    return browser.get(callback, headers={"cookie": cookie}, follow_redirects=False)


def _error_of(resp, origin: str = DASH) -> Dict[str, str]:
    """The `/login?sso_error=...` redirect's parameters, after checking its shape."""
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    assert location.startswith(f"{origin}/login?"), location
    assert resp.headers.get_list("set-cookie") == [EXPIRING_COOKIE]
    assert "access_token" not in location and "#" not in location
    params = {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}
    assert set(params) <= {"sso_error", "provider", "idp_error", "request_id"}
    return params


def _code_of(resp, origin: str = DASH) -> str:
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    prefix = f"{origin}/sso/complete#code="
    assert location.startswith(prefix), location
    return location[len(prefix) :]


# ---------------------------------------------------------------------------
# §1 The hand-off encoding
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_hand_off_hash_matches_the_known_answer_vector():
    """PE9: base64url(SHA-256(the 32 decoded bytes)), computed with openssl."""
    assert base64.urlsafe_b64decode(SECRET + "=") == bytes(range(32))
    assert sso_service.handoff_hash(SECRET) == HH
    assert len(SECRET) == len(HH) == 43


@pytest.mark.parametrize(
    "secret",
    ["", SECRET[:-1], SECRET + "A", SECRET[:-1] + "+", SECRET[:-1] + "=", None, 43],
)
def test_the_hand_off_hash_refuses_anything_but_43_base64url_characters(secret):
    assert not sso_service.is_handoff_value(secret)


# ---------------------------------------------------------------------------
# §6 /api/v1/auth/sso/login: the contract
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "handoff", [None, "", HH[:-1], HH + "x", HH[:-1] + "+", HH[:-1] + "%"]
)
def test_a_missing_or_malformed_handoff_is_a_400_never_a_login(
    browser, config, dashboard, handoff
):
    """PE8: without the hand-off hash there is nothing to bind the code to."""
    resp = _start(browser, config.org_domain, handoff=handoff)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "handoff must be 43 base64url characters"
    assert "set-cookie" not in resp.headers
    assert "location" not in resp.headers


@pytest.mark.parametrize(
    "value", [None, "", "exa mple.com", "example.com/x", "exämple.com", "a" * 254]
)
def test_a_missing_or_malformed_domain_is_a_400(browser, config, dashboard, value):
    resp = _start(browser, value)
    assert resp.status_code == 400, resp.text
    assert "set-cookie" not in resp.headers


def test_the_domain_is_lower_cased(browser, config, dashboard):
    resp = _start(browser, config.org_domain.upper())
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith(config.sso_url)


@pytest.mark.regression
@pytest.mark.parametrize(
    "return_to",
    [
        None,
        "",
        "https://evil.example",
        "https://dash.example.com.evil.example",
        "https://dash.example.com\\@evil.example",
        "https://user@dash.example.com",
        "https://dash.example.com/next",
        "https://dash.example.com/?x=1",
        "https://dash.example.com/#x",
        "https://dash.example.com:99999",
        "https://dash.example.com ",
        "https://dash.example.com\t",
        "https://dash.example.com\x00",
        "http://dash.example.com",
        "*",
    ],
)
def test_return_to_must_be_a_configured_dashboard_origin(
    browser, config, dashboard, return_to
):
    resp = _start(browser, config.org_domain, return_to=return_to)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == (
        "return_to is not a dashboard origin this API accepts"
    )
    assert "set-cookie" not in resp.headers


@pytest.mark.parametrize(
    "spelling", ["https://dash.example.com/", "HTTPS://DASH.example.com:443"]
)
def test_the_cookie_keeps_the_list_entry_not_the_request_spelling(
    browser, config, dashboard, spelling
):
    resp = _start(browser, config.org_domain, return_to=spelling)
    assert resp.status_code == 302, resp.text
    claims = _claims_of(_cookie_of(resp))
    assert claims["rt"] == DASH
    assert claims["hh"] == HH


@pytest.mark.regression
def test_the_demo_apps_are_cors_origins_but_not_dashboard_origins(
    browser, config, monkeypatch
):
    """§2: the dashboard list is not the CORS list. :3200 is ShopLab."""
    monkeypatch.setattr(core_settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(core_settings, "DASHBOARD_ORIGINS", [])
    monkeypatch.setattr(core_settings, "PUBLIC_BASE_URL", None)
    assert "http://localhost:3200" in core_settings.cors_allowed_origins
    shoplab = _start(browser, config.org_domain, return_to="http://localhost:3200")
    streampulse = _start(browser, config.org_domain, return_to="http://localhost:3300")
    dashboard = _start(browser, config.org_domain, return_to="http://localhost:3100")
    assert shoplab.status_code == 400, shoplab.text
    assert streampulse.status_code == 400, streampulse.text
    assert dashboard.status_code == 302, dashboard.text


def test_a_dashboard_login_sets_the_cookie_for_ttl_plus_five_minutes(
    browser, config, dashboard
):
    resp = _start(browser, config.org_domain)
    assert resp.status_code == 302, resp.text
    (set_cookie,) = resp.headers.get_list("set-cookie")
    assert f"Max-Age={sso_service.OIDC_STATE_TTL_SECONDS + 300};" in set_cookie
    claims = _claims_of(_cookie_of(resp))
    # The login itself still expires after the TTL; only the cookie outlives it.
    assert claims["exp"] - claims["iat"] == sso_service.OIDC_STATE_TTL_SECONDS
    query = parse_qs(urlparse(resp.headers["location"]).query)
    # The IdP-registered redirect_uri is unchanged.
    assert query["redirect_uri"] == [
        "http://testserver/api/v1/auth/sso/oidc/okta/callback"
    ]


def test_an_unknown_domain_goes_back_as_sso_not_configured(browser, dashboard):
    resp = _start(browser, f"nobody-{uuid.uuid4().hex[:8]}.example.com")
    params = _error_of(resp)
    assert params["sso_error"] == "sso_not_configured"


def test_a_saml_only_domain_goes_back_as_sso_saml_only(
    browser, db_session, dashboard, domain
):
    db_session.add(
        SSOConfig(
            org_name="SAML Org",
            org_domain=domain,
            provider_type=SSOProviderType.SAML,
            entity_id="https://idp.example.com",
            sso_url="https://idp.example.com/sso",
            is_active=True,
        )
    )
    db_session.commit()
    assert _error_of(_start(browser, domain))["sso_error"] == "sso_saml_only"


def test_two_configs_for_one_domain_is_sso_not_configured_and_names_them(
    browser, db_session, dashboard, domain, caplog
):
    """EM12: exact uniqueness is enforced, normalised uniqueness is not."""
    rows = [
        SSOConfig(
            org_name=f"Dup {n}",
            org_domain=spelling,
            provider_type=SSOProviderType.OKTA,
            entity_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            sso_url="https://idp.example.com/oauth2/default",
            is_active=True,
        )
        for n, spelling in enumerate([domain, f" {domain.upper()} "])
    ]
    db_session.add_all(rows)
    db_session.commit()
    with caplog.at_level(logging.WARNING):
        resp = _start(browser, domain)
    assert _error_of(resp)["sso_error"] == "sso_not_configured"
    assert all(str(r.id) in caplog.text for r in rows)


def test_an_unsupported_provider_goes_back_as_sso_failed(
    browser, db_session, dashboard, domain
):
    db_session.add(
        SSOConfig(
            org_name="MS Org",
            org_domain=domain,
            provider_type=SSOProviderType.MICROSOFT,
            entity_id="ms-client",
            client_secret="ms-secret",
            is_active=True,
        )
    )
    db_session.commit()
    params = _error_of(_start(browser, domain))
    assert params["sso_error"] == "sso_failed"
    assert params["provider"] == "microsoft"


# ---------------------------------------------------------------------------
# The callback's success, the hand-off and the exchange
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_dashboard_sign_in_hands_off_a_code_not_the_token(
    browser, config, dashboard, email
):
    resp = _sign_in(browser, config.org_domain)
    code = _code_of(resp)
    # §6: the 302 carries its own literal Set-Cookie.
    assert resp.headers.get_list("set-cookie") == [EXPIRING_COOKIE]
    assert len(code) <= sso_service.MAX_HANDOFF_CODE_LENGTH
    claims = jwt.decode(code, options={"verify_signature": False})
    assert claims["aud"] == "experimently:sso-handoff"
    assert claims["exp"] - claims["iat"] == 60
    assert claims["hh"] == HH
    assert set(claims) == {"aud", "iat", "exp", "sub", "hh", "jti"}

    exchanged = browser.post(EXCHANGE, json={"code": code, "secret": SECRET})
    assert exchanged.status_code == 200, exchanged.text
    assert exchanged.headers["cache-control"] == "no-store"
    body = exchanged.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == email
    # The token is nowhere in the redirect.
    assert body["access_token"] not in resp.headers["location"]

    me = browser.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200, me.text
    assert me.json()["email"] == email


@pytest.mark.regression
def test_a_code_without_its_secret_is_refused(browser, config, dashboard):
    code = _code_of(_sign_in(browser, config.org_domain))
    other = base64.urlsafe_b64encode(bytes(32)).rstrip(b"=").decode()
    for body in (
        {"code": code},
        {"code": code, "secret": other},
        {"code": code, "secret": HH},  # the hash itself is not the secret
    ):
        resp = browser.post(EXCHANGE, json=body)
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == sso_service.HANDOFF_REFUSED_DETAIL
        assert "access_token" not in resp.text


@pytest.mark.regression
def test_a_code_is_dead_after_sixty_seconds(browser, config, dashboard):
    """A code issued 61 s ago, with its right secret: refused (no leeway)."""
    callback, cookie = _to_provider_and_back(browser, config.org_domain)
    real = time.time()
    with patch.object(sso_service, "time") as clock:
        clock.time.return_value = real - 61
        resp = browser.get(callback, headers={"cookie": cookie}, follow_redirects=False)
    code = _code_of(resp)
    exchanged = browser.post(EXCHANGE, json={"code": code, "secret": SECRET})
    assert exchanged.status_code == 400, exchanged.text
    assert "access_token" not in exchanged.text


def test_a_code_within_its_sixty_seconds_is_not_single_use(browser, config, dashboard):
    """Stated in the spec: no server state, so the secret is the replay guard."""
    code = _code_of(_sign_in(browser, config.org_domain))
    for _ in range(2):
        resp = browser.post(EXCHANGE, json={"code": code, "secret": SECRET})
        assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "body",
    [
        {"code": "x" * 2049, "secret": SECRET},
        {"code": "", "secret": SECRET},
        {"code": 7, "secret": SECRET},
        {"code": "a.b.\u00e9", "secret": SECRET},
        {"code": "a.b.c", "secret": SECRET[:-1]},
        {"code": "a.b.c", "secret": SECRET + "A"},
        {"code": "a.b.c", "secret": SECRET[:-1] + "/"},
        {"secret": SECRET},
        [],
        "code",
    ],
)
def test_the_exchange_body_is_bounded(browser, body):
    resp = browser.post(EXCHANGE, json=body)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == sso_service.HANDOFF_REFUSED_DETAIL


@pytest.mark.regression
def test_a_lone_surrogate_in_the_code_is_a_400_not_a_500(browser):
    """JSON may escape a lone surrogate; the server then holds an unencodable str.

    Sent as raw JSON text, the way an attacker would, because the test client
    refuses to encode the surrogate itself.
    """
    body = '{"code": "a.b.\\ud800", "secret": "%s"}' % SECRET
    resp = browser.post(
        "/api/v1/auth/sso/exchange",
        content=body.encode("ascii"),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == sso_service.HANDOFF_REFUSED_DETAIL


def test_a_body_that_is_not_json_is_a_400(browser):
    resp = browser.post(
        EXCHANGE, content=b"{not json", headers={"content-type": "application/json"}
    )
    assert resp.status_code == 400, resp.text


def test_a_code_for_a_deactivated_user_is_refused(
    browser, config, dashboard, db_session, email
):
    code = _code_of(_sign_in(browser, config.org_domain))
    db_session.expire_all()
    user = db_session.query(User).filter(User.email == email).one()
    user.is_active = False
    db_session.commit()
    resp = browser.post(EXCHANGE, json={"code": code, "secret": SECRET})
    assert resp.status_code == 400, resp.text


def test_the_exchange_is_rate_limited_like_the_password_login():
    assert RATE_LIMIT_CONFIG[EXCHANGE] == RATE_LIMIT_CONFIG["/api/v1/auth/login"]


# ---------------------------------------------------------------------------
# §5/§10 One code per refusal, through the real callback
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("access-denied", "sso_idp_error"),
        ("wrong-nonce", "sso_failed"),
        ("wrong-iss", "sso_failed"),
        ("wrong-aud", "sso_failed"),
        ("expired", "sso_failed"),
        ("none", "sso_failed"),
        # The fake's no-email mode drops `email` AND `email_verified`: an
        # okta sign-in with no verified email is unverified, not invalid.
        ("no-email", "sso_unverified"),
        ("unverified-email", "sso_unverified"),
        ("empty-email", "sso_email"),
        ("non-ascii-email", "sso_email"),
        ("malformed-email", "sso_email"),
        ("other-domain", "sso_domain"),
        ("sub-mismatch", "sso_account"),
    ],
)
def test_each_refusal_goes_back_with_its_own_code(
    browser, config, dashboard, mode, expected
):
    params = _error_of(_sign_in(browser, config.org_domain))
    assert params["sso_error"] == expected
    assert params["provider"] == "okta"
    if expected == "sso_idp_error":
        assert params["idp_error"] == "access_denied"
    else:
        assert "idp_error" not in params


@pytest.mark.regression
def test_the_idp_error_reaches_the_url_only_as_an_oauth_code(
    browser, config, dashboard
):
    """PE10: `error=<script>` does not reach the Location."""
    login = _start(browser, config.org_domain)
    cookie = _cookie_of(login)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    resp = browser.get(
        "/api/v1/auth/sso/oidc/okta/callback",
        params={"error": "<script>alert(1)</script>", "state": state},
        headers={"cookie": cookie},
        follow_redirects=False,
    )
    params = _error_of(resp)
    assert params["sso_error"] == "sso_idp_error"
    assert "idp_error" not in params
    location = resp.headers["location"]
    assert "script" not in location.lower()


def test_a_callback_with_no_code_is_sso_failed(browser, config, dashboard):
    login = _start(browser, config.org_domain)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    resp = browser.get(
        "/api/v1/auth/sso/oidc/okta/callback",
        params={"state": state},
        headers={"cookie": _cookie_of(login)},
        follow_redirects=False,
    )
    assert _error_of(resp)["sso_error"] == "sso_failed"


def test_a_state_mismatch_is_sso_state_at_the_logins_own_origin(
    browser, config, monkeypatch
):
    """Another login's valid cookie: its `rt` is ours, so it is used."""
    second = "https://second.example.com"
    monkeypatch.setattr(core_settings, "DASHBOARD_ORIGINS", [DASH, second])
    monkeypatch.setattr(core_settings, "PUBLIC_BASE_URL", None)
    mine = _start(browser, config.org_domain, return_to=second)
    callback, _ = _to_provider_and_back(browser, config.org_domain)
    resp = browser.get(
        callback, headers={"cookie": _cookie_of(mine)}, follow_redirects=False
    )
    assert _error_of(resp, second)["sso_error"] == "sso_state"


@pytest.mark.regression
def test_an_expired_sign_in_is_sso_expired(browser, config, dashboard):
    """PE2/EM4: the clock at login is 700 s ago, past the 600 s TTL and the
    30 s leeway; the cookie (Max-Age TTL+300) is still sent."""
    real = time.time()
    with patch.object(sso_service, "time") as clock:
        clock.time.return_value = real - 700
        callback, cookie = _to_provider_and_back(browser, config.org_domain)
    resp = browser.get(callback, headers={"cookie": cookie}, follow_redirects=False)
    assert _error_of(resp)["sso_error"] == "sso_expired"


@pytest.mark.parametrize("change", ["deleted", "inactive"])
def test_a_config_gone_since_the_login_is_sso_not_configured(
    browser, config, dashboard, db_session, change
):
    callback, cookie = _to_provider_and_back(browser, config.org_domain)
    if change == "deleted":
        db_session.delete(config)
    else:
        config.is_active = False
    db_session.commit()
    resp = browser.get(callback, headers={"cookie": cookie}, follow_redirects=False)
    assert _error_of(resp)["sso_error"] == "sso_not_configured"


def test_a_callback_on_another_providers_path_is_sso_not_configured(
    browser, config, dashboard
):
    callback, cookie = _to_provider_and_back(browser, config.org_domain)
    resp = browser.get(
        callback.replace("/oidc/okta/", "/oidc/google/"),
        headers={"cookie": cookie},
        follow_redirects=False,
    )
    params = _error_of(resp)
    assert params["sso_error"] == "sso_not_configured"
    assert params["provider"] == "google"


def test_a_replayed_callback_is_sso_failed_at_the_provider(browser, config, dashboard):
    callback, cookie = _to_provider_and_back(browser, config.org_domain)
    first = browser.get(callback, headers={"cookie": cookie}, follow_redirects=False)
    _code_of(first)
    replay = browser.get(callback, headers={"cookie": cookie}, follow_redirects=False)
    assert _error_of(replay)["sso_error"] == "sso_failed"


def test_a_plain_http_provider_outside_test_is_sso_failed(
    browser, config, dashboard, monkeypatch
):
    callback, cookie = _to_provider_and_back(browser, config.org_domain)
    monkeypatch.setattr(core_settings, "ENVIRONMENT", "development")
    resp = browser.get(callback, headers={"cookie": cookie}, follow_redirects=False)
    assert _error_of(resp)["sso_error"] == "sso_failed"


def test_an_unsupported_provider_at_the_callback_is_sso_failed(
    browser, db_session, dashboard, domain
):
    cfg = SSOConfig(
        org_name="MS Org",
        org_domain=domain,
        provider_type=SSOProviderType.MICROSOFT,
        entity_id="ms-client",
        client_secret="ms-secret",
        is_active=True,
    )
    db_session.add(cfg)
    db_session.commit()
    started = sso_service.start_oidc_login(cfg, "microsoft", return_to=DASH, handoff=HH)
    resp = browser.get(
        "/api/v1/auth/sso/oidc/microsoft/callback",
        params={"code": "c", "state": started.state},
        headers={"cookie": f"{sso_service.OIDC_STATE_COOKIE}={started.cookie_value}"},
        follow_redirects=False,
    )
    assert _error_of(resp)["sso_error"] == "sso_failed"


def test_an_identity_linked_to_another_account_is_sso_account(
    browser, config, dashboard, db_session, email
):
    db_session.add(
        User(
            username=f"taken-{uuid.uuid4().hex[:8]}",
            email=f"someone-else-{uuid.uuid4().hex[:6]}@example.org",
            hashed_password="x",
            is_active=True,
            external_id=f"sub-{email}",
        )
    )
    db_session.commit()
    params = _error_of(_sign_in(browser, config.org_domain))
    assert params["sso_error"] == "sso_account"
    assert params["request_id"]


def test_two_accounts_with_this_email_is_sso_account(
    browser, config, dashboard, db_session, email
):
    """Two accounts match one sign-in: the dashboard is told ``sso_account``.

    Since #343 the database refuses such a pair (``ix_users_email_lower``), so
    it is built with the index dropped, and the rows deleted and the index
    re-created on the way out (``without_email_lower_index``).
    """
    with without_email_lower_index(db_session) as added:
        for spelling in (email, email.upper()):
            user = User(
                username=f"dup-{uuid.uuid4().hex[:8]}",
                email=spelling,
                hashed_password="x",
                is_active=True,
            )
            db_session.add(user)
            db_session.flush()
            added.append(user.id)
        db_session.commit()
        assert _error_of(_sign_in(browser, config.org_domain))["sso_error"] == (
            "sso_account"
        )


def test_a_deactivated_account_is_sso_inactive(
    browser, config, dashboard, db_session, email
):
    db_session.add(
        User(
            username=f"off-{uuid.uuid4().hex[:8]}",
            email=email,
            hashed_password="x",
            is_active=False,
            role=UserRole.VIEWER,
        )
    )
    db_session.commit()
    assert _error_of(_sign_in(browser, config.org_domain))["sso_error"] == (
        "sso_inactive"
    )


@pytest.mark.regression
@pytest.mark.parametrize("role_mapping", [{"g": "superadmin"}])
@pytest.mark.parametrize("groups", ["g"])
def test_an_unexpected_error_is_sso_failed_with_the_request_id(
    browser, config, dashboard, caplog, role_mapping, groups
):
    """§5 step 11 and §7: a 302, not a 500, and the id an admin searches for."""
    with caplog.at_level(logging.ERROR):
        resp = _sign_in(browser, config.org_domain)
    params = _error_of(resp)
    assert params["sso_error"] == "sso_failed"
    request_id = resp.headers["x-request-id"]
    assert params["request_id"] == request_id
    logged = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and request_id in r.getMessage()
    ]
    assert logged, caplog.text
    assert logged[0].exc_info is not None  # with its traceback


def test_a_request_id_that_is_not_id_shaped_is_left_out(browser, config, dashboard):
    resp = browser.get(
        LOGIN,
        params={
            "domain": f"nobody-{uuid.uuid4().hex[:6]}.example.com",
            "return_to": DASH,
            "handoff": HH,
        },
        headers={"X-Request-ID": "<b>not an id</b>"},
        follow_redirects=False,
    )
    assert "request_id" not in _error_of(resp)


def test_google_hd_outside_the_domain_is_sso_domain(
    browser, db_session, dashboard, domain
):
    cfg = SSOConfig(
        org_name="Google Org",
        org_domain=domain,
        provider_type=SSOProviderType.GOOGLE,
        entity_id="g-client",
        client_secret="g-secret",
        is_active=True,
    )
    db_session.add(cfg)
    db_session.commit()
    started = sso_service.start_oidc_login(cfg, "google", return_to=DASH, handoff=HH)
    claims = {
        "sub": "g-sub",
        "email": f"someone@{domain}",
        "email_verified": True,
        "hd": "elsewhere.example",
    }
    with (
        patch.object(
            sso_service,
            "exchange_oidc_code",
            AsyncMock(return_value={"access_token": "t", "id_token": "i"}),
        ),
        patch.object(sso_service, "verify_id_token", return_value=claims),
        patch.object(
            sso_service,
            "get_oidc_user_info",
            AsyncMock(return_value={"sub": "g-sub", "email": claims["email"]}),
        ),
    ):
        resp = browser.get(
            "/api/v1/auth/sso/oidc/google/callback",
            params={"code": "c", "state": started.state},
            headers={
                "cookie": f"{sso_service.OIDC_STATE_COOKIE}={started.cookie_value}"
            },
            follow_redirects=False,
        )
    assert _error_of(resp)["sso_error"] == "sso_domain"


def test_github_with_no_verified_address_in_the_domain_is_sso_unverified(
    browser, db_session, dashboard, domain, monkeypatch
):
    cfg = SSOConfig(
        org_name="GitHub Org",
        org_domain=domain,
        provider_type=SSOProviderType.GITHUB,
        entity_id="gh-client",
        client_secret="gh-secret",
        is_active=True,
    )
    db_session.add(cfg)
    db_session.commit()
    started = sso_service.start_oidc_login(cfg, "github", return_to=DASH, handoff=HH)

    class Emails:
        is_redirect = False

        def raise_for_status(self):
            return None

        def json(self):
            return [{"email": f"me@{domain}", "verified": False, "primary": True}]

    monkeypatch.setattr(requests, "get", lambda *a, **k: Emails())
    with (
        patch.object(
            sso_service,
            "exchange_oidc_code",
            AsyncMock(return_value={"access_token": "t"}),
        ),
        patch.object(
            sso_service,
            "get_oidc_user_info",
            AsyncMock(return_value={"sub": "1", "email": f"me@{domain}"}),
        ),
    ):
        resp = browser.get(
            "/api/v1/auth/sso/oidc/github/callback",
            params={"code": "c", "state": started.state},
            headers={
                "cookie": f"{sso_service.OIDC_STATE_COOKIE}={started.cookie_value}"
            },
            follow_redirects=False,
        )
    assert _error_of(resp)["sso_error"] == "sso_unverified"


# ---------------------------------------------------------------------------
# §4 No cookie, or one that is not ours: the primary dashboard origin
# ---------------------------------------------------------------------------


@pytest.fixture
def public_base(monkeypatch) -> str:
    origin = "https://app.example.com"
    monkeypatch.setattr(core_settings, "PUBLIC_BASE_URL", origin)
    monkeypatch.setattr(core_settings, "DASHBOARD_ORIGINS", [])
    return origin


@pytest.mark.regression
def test_no_cookie_goes_to_the_primary_origin_as_sso_state(
    browser, config, public_base
):
    """PE5, EM SEQUENCE: with PUBLIC_BASE_URL set, no cookie -> /login?sso_error."""
    callback, _ = _to_provider_and_back(browser, config.org_domain, public_base)
    resp = browser.get(callback, follow_redirects=False)
    assert _error_of(resp, public_base)["sso_error"] == "sso_state"


@pytest.mark.regression
def test_a_cookie_that_is_not_ours_never_chooses_the_destination(
    browser, config, public_base
):
    """PE5: a forged cookie's `rt` is not trusted; the primary origin is used."""
    callback, _ = _to_provider_and_back(browser, config.org_domain, public_base)
    now = int(time.time())
    forged = jwt.encode(
        {
            "aud": "experimently:oidc-state",
            "iat": now,
            "exp": now + 600,
            "st": parse_qs(urlparse(callback).query)["state"][0],
            "cv": "v" * 43,
            "nn": "n",
            "cfg": str(config.id),
            "prv": "okta",
            "rt": "https://evil.example",
            "hh": HH,
        },
        "not-this-apis-key-" + "x" * 32,
        algorithm="HS256",
    )
    resp = browser.get(
        callback,
        headers={"cookie": f"{sso_service.OIDC_STATE_COOKIE}={forged}"},
        follow_redirects=False,
    )
    assert _error_of(resp, public_base)["sso_error"] == "sso_state"
    assert "evil" not in resp.headers["location"]


def test_the_first_dashboard_origin_is_primary(browser, config, monkeypatch):
    monkeypatch.setattr(core_settings, "PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.setattr(core_settings, "DASHBOARD_ORIGINS", [DASH])
    callback, _ = _to_provider_and_back(browser, config.org_domain)
    resp = browser.get(callback, follow_redirects=False)
    assert _error_of(resp, DASH)["sso_error"] == "sso_state"


def test_a_replay_after_success_has_no_cookie_and_is_sso_state(
    browser, config, public_base
):
    callback, cookie = _to_provider_and_back(browser, config.org_domain, public_base)
    first = browser.get(callback, headers={"cookie": cookie}, follow_redirects=False)
    _code_of(first, public_base)
    # The first callback expired the cookie, so the browser sends none.
    replay = browser.get(callback, follow_redirects=False)
    assert _error_of(replay, public_base)["sso_error"] == "sso_state"


def test_no_cookie_and_no_dashboard_origin_is_the_json_400(
    browser, config, monkeypatch
):
    """Development with neither DASHBOARD_ORIGINS nor PUBLIC_BASE_URL: JSON."""
    monkeypatch.setattr(core_settings, "PUBLIC_BASE_URL", None)
    monkeypatch.setattr(core_settings, "DASHBOARD_ORIGINS", [])
    resp = browser.get(
        "/api/v1/auth/sso/oidc/okta/callback",
        params={"code": "c", "state": "s"},
        follow_redirects=False,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == (
        "OIDC sign-in was not started in this browser, or its cookie was not sent"
    )


# ---------------------------------------------------------------------------
# An account's email address is changed only by an administrator
# ---------------------------------------------------------------------------

MEMBER_PASSWORD = "Str0ng-Passw0rd"


def _member_with_token(browser: TestClient, db_session: Session):
    """A local VIEWER (not a superuser) and a real bearer token for them."""
    import bcrypt

    suffix = uuid.uuid4().hex[:8]
    member = User(
        username=f"member-{suffix}",
        email=f"member-{suffix}@example.org",
        hashed_password=bcrypt.hashpw(
            MEMBER_PASSWORD.encode("utf-8"), bcrypt.gensalt(rounds=4)
        ).decode(),
        is_active=True,
        is_superuser=False,
        role=UserRole.VIEWER,
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    login = browser.post(
        "/api/v1/auth/login",
        json={"email": member.email, "password": MEMBER_PASSWORD},
    )
    assert login.status_code == 200, login.text
    return member, login.json()["access_token"]


def _change_email(browser: TestClient, member: User, token: str, new_email: str):
    return browser.put(
        f"/api/v1/users/{member.id}",
        json={"username": member.username, "email": new_email},
        headers={"Authorization": f"Bearer {token}"},
    )


def _exchange(browser: TestClient, resp) -> Dict:
    exchanged = browser.post(EXCHANGE, json={"code": _code_of(resp), "secret": SECRET})
    assert exchanged.status_code == 200, exchanged.text
    return exchanged.json()["user"]


@pytest.mark.regression
@pytest.mark.parametrize("role_mapping", [{"g": "admin"}])
@pytest.mark.parametrize("groups", ["g"])
def test_a_user_cannot_change_their_email_to_an_address_sso_signs_in(
    browser, config, dashboard, db_session, email, role_mapping, groups
):
    """The first SSO sign-in for an address gets an account of its own.

    Before that sign-in, a local non-superuser tries to change their own
    email to the address. The change is refused, so the sign-in creates a
    new account with the mapped role, and the local account keeps its email
    address and its role.
    """
    member, token = _member_with_token(browser, db_session)
    changed = _change_email(browser, member, token, email)

    signed_in = _exchange(browser, _sign_in(browser, config.org_domain))
    assert signed_in["id"] != str(member.id)
    assert signed_in["email"] == email
    assert signed_in["role"] == "ADMIN"

    db_session.expire_all()
    row = db_session.query(User).filter(User.id == member.id).one()
    assert (row.email, row.role) == (member.email, UserRole.VIEWER)
    assert changed.status_code == 403, changed.text


@pytest.mark.regression
def test_a_case_variant_email_change_does_not_stop_the_next_sso_sign_in(
    browser, config, dashboard, db_session, email
):
    """Once an address has signed in with SSO, a local user cannot take the
    upper-case copy of it; the next SSO sign-in finds exactly one account."""
    first = _exchange(browser, _sign_in(browser, config.org_domain))

    member, token = _member_with_token(browser, db_session)
    changed = _change_email(browser, member, token, email.upper())

    again = _exchange(browser, _sign_in(browser, config.org_domain))
    assert again["id"] == first["id"]
    assert changed.status_code == 403, changed.text


# ---------------------------------------------------------------------------
# A superuser account signs in with a password, not through SSO
# ---------------------------------------------------------------------------


def _account(db_session: Session, email: str, role: UserRole, superuser: bool) -> User:
    user = User(
        username=f"acct-{uuid.uuid4().hex[:8]}",
        email=email,
        hashed_password="x",
        is_active=True,
        is_superuser=superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.mark.regression
@pytest.mark.parametrize(
    ("role_mapping", "groups"),
    [({}, ""), ({"readers": "viewer"}, "readers")],
    ids=["no-group-mapped", "group-mapped-to-viewer"],
)
def test_a_superuser_account_is_sso_account_and_is_left_as_it_was(
    browser, config, dashboard, db_session, email, caplog, role_mapping, groups
):
    """No hand-off code, whether or not a group is mapped; a mapped group is
    not applied, so the role and the superuser flag are as they were, and no
    audit entry is written. The API log says why."""
    root = _account(db_session, email, UserRole.ADMIN, superuser=True)

    with caplog.at_level(logging.WARNING):
        resp = _sign_in(browser, config.org_domain)

    params = _error_of(resp)
    assert params["sso_error"] == "sso_account"
    assert params["request_id"]
    assert "#code=" not in resp.headers["location"]
    db_session.expire_all()
    row = db_session.get(User, root.id)
    assert (row.role, row.is_superuser) == (UserRole.ADMIN, True)
    assert db_session.query(AuditLog).filter(AuditLog.entity_id == root.id).all() == []
    assert any(
        str(root.id) in r.getMessage() and "superuser" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    ), caplog.text


@pytest.mark.regression
@pytest.mark.parametrize("role_mapping", [{"readers": "viewer"}])
@pytest.mark.parametrize("groups", ["readers"])
@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.VIEWER])
def test_an_admin_or_a_viewer_in_the_domain_still_signs_in(
    browser, config, dashboard, db_session, email, role_mapping, groups, role
):
    """The control: the same sign-in for an account that is not a superuser."""
    account = _account(db_session, email, role, superuser=False)

    signed_in = _exchange(browser, _sign_in(browser, config.org_domain))

    assert (signed_in["id"], signed_in["email"]) == (str(account.id), email)
    # The mapped group applies to them.
    assert signed_in["role"] == "VIEWER"
