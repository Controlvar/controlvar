"""What an SSO sign-in may do to accounts: the configuration's domain only, and
an existing account's role changes only through a mapped group.

Against Postgres, not a mock: the lookup is case-insensitive in SQL, and a mock
returns whatever it is given whatever the filter says.
"""

from __future__ import annotations

import uuid
from typing import Iterator
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.api import deps
from backend.app.db.session import get_db as _session_get_db
from backend.app.main import app
from backend.app.models.user import User, UserRole
from modules.backend.app.models.sso_config import SSOConfig, SSOProviderType
from modules.backend.app.services import sso_service

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

BASE = "/api/v1/auth/sso"


def _domain() -> str:
    return f"scope-{uuid.uuid4().hex[:10]}.example.com"


def _config(db: Session, domain: str, provider=SSOProviderType.SAML, **kw) -> SSOConfig:
    cfg = SSOConfig(
        org_name="Scope Org",
        org_domain=domain,
        provider_type=provider,
        entity_id=kw.pop("entity_id", "client-id"),
        client_secret="secret",
        sso_url="https://idp.example.com/sso",
        x509_certificate="MIIC...cert",
        role_mapping=kw.pop("role_mapping", {}),
        is_enforced=False,
        is_active=True,
    )
    db.add(cfg)
    db.commit()
    return cfg


def _user(db: Session, email: str, role=UserRole.ADMIN, superuser=False) -> User:
    user = User(
        username=f"u_{uuid.uuid4().hex[:10]}",
        email=email,
        hashed_password="not-a-real-hash",
        is_active=True,
        is_superuser=superuser,
        role=role,
    )
    db.add(user)
    db.commit()
    return user


def _info(email: str, groups=None) -> dict:
    return {"email": email, "name_id": email, "groups": groups or [], "raw": {}}


def _count(db: Session, email: str) -> int:
    return db.query(User).filter(func.lower(User.email) == email.lower()).count()


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


def test_a_sign_in_with_no_mapped_group_keeps_an_admins_role(db_session: Session):
    """The released defect: this demoted a non-superuser ADMIN to VIEWER."""
    domain = _domain()
    cfg = _config(db_session, domain)
    admin = _user(db_session, f"alice@{domain}", role=UserRole.ADMIN)

    result = sso_service.provision_user(db_session, _info(f"alice@{domain}"), cfg)

    db_session.expire_all()
    assert result.id == admin.id
    assert db_session.get(User, admin.id).role == UserRole.ADMIN


def test_a_mapped_group_still_sets_the_role(db_session: Session):
    domain = _domain()
    cfg = _config(db_session, domain, role_mapping={"readers": "viewer"})
    admin = _user(db_session, f"dave@{domain}", role=UserRole.ADMIN)

    sso_service.provision_user(db_session, _info(f"dave@{domain}", ["readers"]), cfg)

    db_session.expire_all()
    assert db_session.get(User, admin.id).role == UserRole.VIEWER


def test_superuser_is_never_changed(db_session: Session):
    """A superuser account is not signed in through SSO: the sign-in is refused
    before a mapped group is applied, so the account is as it was."""
    domain = _domain()
    cfg = _config(db_session, domain, role_mapping={"readers": "viewer"})
    root = _user(db_session, f"root@{domain}", role=UserRole.ADMIN, superuser=True)

    with pytest.raises(sso_service.SSORefusal) as exc_info:
        sso_service.provision_user(
            db_session, _info(f"ROOT@{domain}", ["readers"]), cfg
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == sso_service.SUPERUSER_ACCOUNT_DETAIL
    assert exc_info.value.sso_error == sso_service.SSO_ACCOUNT
    db_session.expire_all()
    row = db_session.get(User, root.id)
    assert (row.is_superuser, row.role) == (True, UserRole.ADMIN)


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


def test_an_email_outside_the_domain_is_refused_and_nothing_is_written(
    db_session: Session,
):
    cfg = _config(db_session, _domain())
    other = _domain()
    victim = _user(db_session, f"erin@{other}", role=UserRole.ADMIN)

    for email in (f"erin@{other}", f"newcomer@{other}"):
        with pytest.raises(HTTPException) as exc_info:
            sso_service.provision_user(db_session, _info(email), cfg)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == sso_service.EMAIL_DOMAIN_DETAIL

    db_session.expire_all()
    assert db_session.get(User, victim.id).role == UserRole.ADMIN
    assert _count(db_session, f"newcomer@{other}") == 0


def test_a_subdomain_is_not_the_domain(db_session: Session):
    domain = _domain()
    cfg = _config(db_session, domain)
    with pytest.raises(HTTPException) as exc_info:
        sso_service.provision_user(db_session, _info(f"frank@eu.{domain}"), cfg)
    assert exc_info.value.detail == sso_service.EMAIL_DOMAIN_DETAIL


@pytest.mark.parametrize("stored", ["  {d}  ", "@{d}", "{D}"])
def test_the_configured_domain_is_compared_normalised(db_session: Session, stored):
    domain = _domain()
    cfg = _config(
        db_session, stored.format(d=domain, D=domain.upper()).replace("{D}", "")
    )
    user = sso_service.provision_user(db_session, _info(f"gina@{domain}"), cfg)
    assert user.email == f"gina@{domain}"


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_a_mixed_case_account_is_linked_not_duplicated(db_session: Session):
    domain = _domain()
    cfg = _config(db_session, domain)
    legacy = _user(db_session, f"Bob@{domain.upper()}", role=UserRole.DEVELOPER)

    result = sso_service.provision_user(db_session, _info(f"bob@{domain}"), cfg)

    assert result.id == legacy.id
    assert _count(db_session, f"bob@{domain}") == 1


def test_the_database_refuses_a_second_account_differing_only_in_case(
    db_session: Session,
):
    """Two accounts can no longer match one sign-in: ``ix_users_email_lower``.

    The refusal of more than one match is pinned with a stubbed query in
    ``unit/services/test_sso_provision_ambiguity.py``; a real pair cannot be
    built in the shared test schema without dropping the index.
    """
    domain = _domain()
    _user(db_session, f"carol@{domain}")

    with pytest.raises(IntegrityError, match="ix_users_email_lower"):
        _user(db_session, f"Carol@{domain}")
    db_session.rollback()
    assert _count(db_session, f"carol@{domain}") == 1


# ---------------------------------------------------------------------------
# Email shape
# ---------------------------------------------------------------------------


def test_a_non_ascii_email_is_refused_before_it_is_lower_cased(db_session: Session):
    """U+212A KELVIN SIGN lower-cases to ASCII "k"."""
    cfg = _config(db_session, "kraken-" + uuid.uuid4().hex[:8] + ".com")
    lookalike = "bob@K" + cfg.org_domain[1:]
    assert lookalike.lower().endswith(cfg.org_domain)  # the trap is real
    with pytest.raises(HTTPException) as exc_info:
        sso_service.provision_user(db_session, _info(lookalike), cfg)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == sso_service.EMAIL_INVALID_DETAIL


@pytest.mark.parametrize("email", ["", "   ", "no-at-sign", "@{d}", "henry@", None, 42])
def test_a_malformed_email_is_refused(db_session: Session, email):
    domain = _domain()
    cfg = _config(db_session, domain)
    value = email.format(d=domain) if isinstance(email, str) else email
    with pytest.raises(HTTPException) as exc_info:
        sso_service.provision_user(db_session, {"email": value, "groups": []}, cfg)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == sso_service.EMAIL_INVALID_DETAIL


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------


@pytest.fixture
def anonymous(db_session: Session) -> Iterator[TestClient]:
    """Only the database is overridden; authentication is real."""
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


def _saml_post(client: TestClient, cfg: SSOConfig, email: str):
    parsed = {
        "email": email,
        "name_id": email,
        "groups": [],
        "attributes": {},
        "first_name": None,
        "last_name": None,
    }
    with patch(
        "modules.backend.app.services.sso_service.parse_saml_response",
        return_value=parsed,
    ):
        return client.post(f"{BASE}/saml/{cfg.id}/acs", data={"SAMLResponse": "eA=="})


def test_the_saml_acs_refuses_an_assertion_for_another_domain(
    anonymous: TestClient, db_session: Session
):
    cfg = _config(db_session, _domain())
    resp = _saml_post(anonymous, cfg, f"ivan@{_domain()}")
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == sso_service.EMAIL_DOMAIN_DETAIL


def test_a_saml_acs_token_is_still_not_a_session(
    anonymous: TestClient, db_session: Session
):
    """Pins the ACS token as unusable until SAML gets its own review (#123).
    Issuing `create_local_access_token` there instead makes this fail."""
    domain = _domain()
    cfg = _config(db_session, domain)
    resp = _saml_post(anonymous, cfg, f"judy@{domain}")
    assert resp.status_code == 200, resp.text
    me = anonymous.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {resp.json()['access_token']}"},
    )
    assert me.status_code == 401, me.text


@pytest.mark.parametrize("provider", ["microsoft", "azure_ad", "onelogin"])
def test_unsupported_providers_are_refused_at_login_and_callback(
    anonymous: TestClient, db_session: Session, provider
):
    cfg = _config(db_session, _domain(), provider=SSOProviderType(provider))
    login = anonymous.get(
        f"{BASE}/oidc/{provider}/login",
        params={"org_domain": cfg.org_domain},
        follow_redirects=False,
    )
    callback = anonymous.get(
        f"{BASE}/oidc/{provider}/callback", params={"code": "c", "state": "s"}
    )
    for resp in (login, callback):
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == sso_service.UNSUPPORTED_PROVIDER_DETAIL


def test_accounts_are_found_by_email_never_by_the_providers_subject(
    db_session: Session,
):
    """A row created earlier under a subject is not handed to a sign-in under
    another email, and the unique column is a refusal, not a 500 (#122)."""
    domain = _domain()
    cfg = _config(db_session, domain)
    first = sso_service.provision_user(
        db_session, {**_info(f"kim@{domain}"), "sub": "subject-1"}, cfg
    )

    with pytest.raises(HTTPException) as exc_info:
        sso_service.provision_user(
            db_session, {**_info(f"lee@{domain}"), "sub": "subject-1"}, cfg
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == sso_service.IDENTITY_TAKEN_DETAIL
    assert _count(db_session, f"lee@{domain}") == 0
    db_session.expire_all()
    assert db_session.get(User, first.id).email == f"kim@{domain}"
