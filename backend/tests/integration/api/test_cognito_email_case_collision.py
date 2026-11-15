"""Cognito first sign-in with another account's address in other letter case (#343).

With ``AUTH_PROVIDER=cognito``, a Cognito identity no account is linked to
gets a new account on its first sign-in -- unless another account already
holds its email address, in any letter case.  That sign-in is refused with
the existing "Could not validate credentials" 401 -- not a 500 -- and the
refusal record says ``email_taken``: the sign-in's own check refused it,
before the ``lower(email)`` unique index would have.  Nothing is created,
and the existing account is left exactly as it was.

Real database: the account and the index are in PostgreSQL, not a mock.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.models.user import User, UserRole
from backend.tests.integration.conftest import HASHED_PASSWORD

pytestmark = [pytest.mark.integration, pytest.mark.regression]


def _snapshot(db_session: Session, user_id) -> tuple:
    db_session.rollback()
    row = db_session.get(User, user_id, populate_existing=True)
    return (
        row.email,
        row.username,
        row.role,
        row.is_superuser,
        row.is_active,
        row.external_id,
        row.updated_at,
    )


SIGN_IN_LOGGER = "backend.app.auth.cognito_sign_in"


def test_a_case_variant_on_first_cognito_sign_in_is_refused_with_401(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, caplog
):
    suffix = uuid.uuid4().hex[:8]
    existing = User(
        username=f"local_{suffix}",
        email=f"Pat.Lee.{suffix}@example.com",
        hashed_password=HASHED_PASSWORD,
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db_session.add(existing)
    db_session.commit()
    before = _snapshot(db_session, existing.id)

    newcomer = f"cog_{suffix}"
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    monkeypatch.setattr(
        deps.auth_service,
        "get_user_with_groups",
        lambda token: {
            "username": newcomer,
            "attributes": {
                "sub": str(uuid.uuid4()),
                "email": existing.email.lower(),
                "email_verified": "true",
            },
            "groups": ["Admins"],
        },
    )

    with caplog.at_level(logging.WARNING, logger=SIGN_IN_LOGGER):
        with pytest.raises(HTTPException) as exc_info:
            deps.get_current_user(token="a-cognito-access-token", db=db_session)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Could not validate credentials"
    assert [
        getattr(record, "reason", None)
        for record in caplog.records
        if record.name == SIGN_IN_LOGGER
    ] == ["email_taken"]
    db_session.rollback()
    assert db_session.query(User).filter(User.username == newcomer).count() == 0
    assert (
        db_session.query(User)
        .filter(func.lower(User.email) == existing.email.lower())
        .count()
        == 1
    )
    assert _snapshot(db_session, existing.id) == before
