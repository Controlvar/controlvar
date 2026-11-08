"""The link statement in ``docs/cognito_integration.md`` works as written.

The statement is read from the page itself (``link_statement``), its
placeholders filled the way the page says, and run against PostgreSQL. It
links an existing account with a password; the next Cognito sign-in with that
user ID reaches that account.
"""

from __future__ import annotations

import logging
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.db.session import get_db as session_get_db
from backend.app.main import app
from backend.app.models.user import User, UserRole
from backend.tests.integration.cognito_identity import identity
from backend.tests.integration.conftest import HASHED_PASSWORD
from backend.tests.unit.docs.test_cognito_integration_docs import (
    EMAIL,
    SCHEMA,
    SUB,
    link_statement,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

SIGN_IN_LOGGER = "backend.app.auth.cognito_sign_in"
TEST_SCHEMA = "test_experimentation"


def _filled(sub: str, email: str) -> str:
    return (
        link_statement()
        .replace(SCHEMA, TEST_SCHEMA)
        .replace(SUB, sub)
        .replace(EMAIL, email)
    )


def _run(engine, sql: str) -> list:
    with engine.begin() as conn:
        return [dict(row._mapping) for row in conn.execute(text(sql))]


def _account(db_session, sfx: str) -> User:
    user = User(
        username=f"linkme_{sfx}",
        email=f"Link.Me.{sfx}@Example.com",
        hashed_password=HASHED_PASSWORD,
        role=UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_the_documented_statement_links_an_account_a_sign_in_then_reaches(
    db_session, test_db, monkeypatch, caplog
):
    sfx = uuid.uuid4().hex[:10]
    account = _account(db_session, sfx)
    sub = str(uuid.uuid4())

    # The page tells the operator to type the address; any letter case works.
    rows = _run(test_db, _filled(sub, account.email.lower()))

    assert rows == [
        {
            "id": account.id,
            "username": account.username,
            "email": account.email,
            "external_id": f"cognito:{sub}",
        }
    ]

    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    monkeypatch.setattr(settings, "SYNC_ROLES_ON_LOGIN", False)

    def override_get_db():
        yield db_session

    saved = dict(app.dependency_overrides)
    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[session_get_db] = override_get_db
    try:
        with mock_aws():
            monkeypatch.setattr(
                deps.auth_service,
                "get_user_with_groups",
                lambda token: identity(
                    f"pool_name_{sfx}", sub, f"pool.{sfx}@example.com"
                ),
            )
            with caplog.at_level(logging.WARNING, logger=SIGN_IN_LOGGER):
                response = TestClient(app, raise_server_exceptions=False).get(
                    "/api/v1/users/me", headers={"Authorization": "Bearer a-token"}
                )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(account.id)
    assert response.json()["username"] == account.username
    assert [r for r in caplog.records if r.name == SIGN_IN_LOGGER] == []


def test_the_documented_statement_returns_no_row_for_an_unknown_address(test_db):
    sfx = uuid.uuid4().hex[:10]
    assert _run(test_db, _filled(str(uuid.uuid4()), f"nobody.{sfx}@example.com")) == []


def test_linking_a_second_account_to_the_same_user_names_external_id(
    db_session, test_db
):
    """The page says an error naming ``external_id`` means another account is
    already linked to that Cognito user."""
    sub = str(uuid.uuid4())
    first = _account(db_session, uuid.uuid4().hex[:10])
    second = _account(db_session, uuid.uuid4().hex[:10])
    assert len(_run(test_db, _filled(sub, first.email))) == 1

    with pytest.raises(IntegrityError) as refused:
        _run(test_db, _filled(sub, second.email))

    assert "external_id" in str(refused.value.orig)
