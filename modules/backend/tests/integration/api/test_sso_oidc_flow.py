"""An OIDC sign-in, end to end, against a provider in another process (#66).

The browser is the test: it starts the login, follows the redirect to the fake
provider over real HTTP, comes back to the callback with the cookie the login
set, and uses the token it gets. The API reaches the provider over real HTTP
too, through whichever branch this environment has -- ``requests``, and
authlib where it is installed (CI's Module Tests install
``modules/requirements.txt``, so both run there).

What this pins:

* the state cookie and the URL ``state`` round-trip through a provider;
* PKCE: the provider refuses the code without the verifier from the cookie;
* the token the callback returns is accepted by ``GET /api/v1/auth/me`` -- it
  used to carry no ``iss`` and was accepted by nothing;
* replaying a finished callback is refused, by the provider, on the code.

The provider failing to start is a failure, not a skip: a skipped flow test is
indistinguishable from a passing one in a summary line.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from backend.app.api import deps
from backend.app.db.session import get_db as _session_get_db
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.user import User, UserRole
from modules.backend.app.models.sso_config import SSOConfig, SSOProviderType
from modules.backend.app.services import sso_service

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_db,
    pytest.mark.regression,
]

PROVIDER_SCRIPT = Path(__file__).with_name("fake_oidc_provider.py")
CLIENT_ID = "flow-client"
CLIENT_SECRET = "flow-secret"


@pytest.fixture
def domain() -> str:
    """The configuration's domain; a sign-in must present an email in it."""
    return f"flow-{uuid.uuid4().hex[:8]}.example.com"


@pytest.fixture
def email(domain) -> str:
    return f"oidc-flow-{uuid.uuid4().hex[:8]}@{domain}"


@pytest.fixture
def id_token_mode() -> str:
    """How the fake provider's ID token behaves; overridden per test."""
    return "good"


@pytest.fixture
def groups() -> str:
    """The provider's user info ``groups``, comma-separated; overridden per test."""
    return ""


@pytest.fixture
def role_mapping() -> dict:
    return {}


@pytest.fixture
def provider(email, id_token_mode, groups) -> Iterator[str]:
    proc = subprocess.Popen(
        [sys.executable, str(PROVIDER_SCRIPT), CLIENT_ID, CLIENT_SECRET, email],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "FAKE_OIDC_ID_TOKEN": id_token_mode,
            "FAKE_OIDC_GROUPS": groups,
        },
    )
    first_line: list[str] = []
    reader = threading.Thread(
        target=lambda: first_line.append(proc.stdout.readline()), daemon=True
    )
    reader.start()
    reader.join(timeout=20)
    line = first_line[0] if first_line else ""
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
def config(db_session: Session, provider: str, domain: str, role_mapping) -> SSOConfig:
    cfg = SSOConfig(
        org_name="Flow Org",
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


@pytest.fixture(params=["requests", "authlib"])
def branch(request, monkeypatch) -> str:
    if request.param == "authlib":
        pytest.importorskip("authlib")
        if not sso_service._AUTHLIB_AVAILABLE:
            pytest.skip("authlib is not importable by the service")
    else:
        monkeypatch.setattr(sso_service, "_AUTHLIB_AVAILABLE", False)
    return request.param


def _sign_in(browser: TestClient, config: SSOConfig):
    """Start, visit the provider, and return (callback path, cookie header)."""
    login = browser.get(
        "/api/v1/auth/sso/oidc/okta/login",
        params={"org_domain": config.org_domain},
        follow_redirects=False,
    )
    assert login.status_code == 302, login.text
    (set_cookie,) = login.headers.get_list("set-cookie")
    cookie = set_cookie.split(";", 1)[0]
    assert cookie.startswith(f"{sso_service.OIDC_STATE_COOKIE}=")
    browser.cookies.clear()

    at_provider = requests.get(
        login.headers["location"], allow_redirects=False, timeout=10
    )
    assert at_provider.status_code == 302, at_provider.text
    back = urlparse(at_provider.headers["location"])
    return f"{back.path}?{back.query}", cookie


def test_a_sign_in_completes_and_its_token_is_accepted(browser, config, email, branch):
    callback, cookie = _sign_in(browser, config)

    resp = browser.get(callback, headers={"cookie": cookie})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["email"] == email

    me = browser.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert me.status_code == 200, me.text
    assert me.json()["email"] == email


def test_a_sign_in_writes_one_user_login_and_one_user_create(
    browser, config, email, db_session
):
    """#221: the callback that answers with a token is the sign-in, and the
    first one creates the account: one entry each, by the account."""
    callback, cookie = _sign_in(browser, config)
    resp = browser.get(callback, headers={"cookie": cookie})
    assert resp.status_code == 200, resp.text
    user_id = uuid.UUID(resp.json()["user_id"])

    db_session.expire_all()
    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.entity_id == user_id)
        .order_by(AuditLog.action_type)
        .all()
    )
    assert [(r.action_type, r.user_id, r.user_email) for r in rows] == [
        ("user_create", user_id, email),
        ("user_login", user_id, email),
    ]
    assert rows[1].new_value == '{"provider": "sso"}'


def test_a_replayed_callback_is_refused_by_the_provider(browser, config, branch):
    callback, cookie = _sign_in(browser, config)
    first = browser.get(callback, headers={"cookie": cookie})
    assert first.status_code == 200, first.text

    replay = browser.get(callback, headers={"cookie": cookie})
    assert replay.status_code == 400, replay.text
    assert replay.json()["detail"] == "OIDC token exchange failed (invalid_grant)"
    assert "access_token" not in replay.text


def test_the_callback_needs_the_cookie_that_carries_the_verifier(
    browser, config, branch
):
    """Without the cookie there is no verifier, so an intercepted code is useless."""
    callback, _ = _sign_in(browser, config)
    resp = browser.get(callback)
    assert resp.status_code == 400, resp.text
    assert "access_token" not in resp.text


@pytest.mark.parametrize(
    ("id_token_mode", "reason"),
    [
        ("wrong-nonce", "nonce"),
        ("wrong-iss", "iss"),
        ("wrong-aud", "aud"),
        ("expired", "exp"),
        ("none", "missing"),
    ],
)
def test_an_id_token_that_is_not_this_logins_is_refused(
    browser, config, branch, id_token_mode, reason
):
    """C2n: the ID token from the exchange must name this client, this provider,
    this login's nonce, and not have expired."""
    callback, cookie = _sign_in(browser, config)
    resp = browser.get(callback, headers={"cookie": cookie})
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == f"OIDC ID token was not accepted ({reason})"
    assert "access_token" not in resp.text


@pytest.mark.parametrize(
    ("id_token_mode", "status", "detail"),
    [
        ("unverified-email", 403, sso_service.EMAIL_UNVERIFIED_DETAIL),
        ("other-domain", 403, sso_service.EMAIL_DOMAIN_DETAIL),
        ("sub-mismatch", 400, sso_service.IDENTITY_MISMATCH_DETAIL),
    ],
)
def test_a_sign_in_must_present_an_email_the_provider_verified(
    browser, config, branch, id_token_mode, status, detail
):
    """V2: the email comes from the ID token, verified, in the config's domain."""
    callback, cookie = _sign_in(browser, config)
    resp = browser.get(callback, headers={"cookie": cookie})
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"] == detail
    assert "access_token" not in resp.text


@pytest.mark.parametrize("id_token_mode", ["userinfo-email-differs"])
def test_the_id_tokens_email_wins_over_the_user_infos(
    browser, config, email, branch, id_token_mode
):
    callback, cookie = _sign_in(browser, config)
    resp = browser.get(callback, headers={"cookie": cookie})
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == email


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


@pytest.mark.parametrize(
    ("role_mapping", "groups"),
    [({}, ""), ({"readers": "viewer"}, "readers")],
    ids=["no-group-mapped", "group-mapped-to-viewer"],
)
def test_a_superuser_account_is_refused_and_left_as_it_was(
    browser, config, email, db_session, role_mapping, groups
):
    """No token, whether or not a group is mapped; a mapped group is not
    applied, so the role and the superuser flag are as they were, and no
    audit entry is written."""
    root = _account(db_session, email, UserRole.ADMIN, superuser=True)
    callback, cookie = _sign_in(browser, config)

    resp = browser.get(callback, headers={"cookie": cookie})

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == sso_service.SUPERUSER_ACCOUNT_DETAIL
    assert "access_token" not in resp.text
    db_session.expire_all()
    row = db_session.get(User, root.id)
    assert (row.role, row.is_superuser) == (UserRole.ADMIN, True)
    assert db_session.query(AuditLog).filter(AuditLog.entity_id == root.id).all() == []


@pytest.mark.parametrize("role_mapping", [{"readers": "viewer"}])
@pytest.mark.parametrize("groups", ["readers"])
@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.VIEWER])
def test_an_admin_or_a_viewer_in_the_domain_still_signs_in(
    browser, config, email, db_session, role_mapping, groups, role
):
    """The control: the same sign-in for an account that is not a superuser."""
    account = _account(db_session, email, role, superuser=False)
    callback, cookie = _sign_in(browser, config)

    resp = browser.get(callback, headers={"cookie": cookie})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The mapped group applies to them.
    assert (body["user_id"], body["role"]) == (str(account.id), "viewer")
    me = browser.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert me.status_code == 200, me.text
    assert me.json()["email"] == email
