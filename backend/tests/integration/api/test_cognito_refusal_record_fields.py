"""What a refused Cognito sign-in's record carries besides its ``reason``.

Each refusal writes one WARNING record to ``backend.app.auth.cognito_sign_in``.
It also carries ``cognito_username`` and ``sub`` and, where an account was
involved, ``row_id``: that account's id. Driven through ``GET
/api/v1/users/me`` with the Cognito boundary stubbed, against real Postgres
(the ``signin`` harness of ``test_cognito_sign_in_linking.py``).
"""

from __future__ import annotations

import logging
import uuid

import pytest

from backend.tests.integration.api.test_cognito_sign_in_linking import (
    SIGN_IN_LOGGER,
    _admin_row,
    signin,
)
from backend.tests.integration.cognito_identity import identity

pytestmark = [pytest.mark.integration, pytest.mark.regression]


def _record(signin, caplog):
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=SIGN_IN_LOGGER):
        response = signin.client.get(
            "/api/v1/users/me", headers={"Authorization": "Bearer a-token"}
        )
    assert response.status_code == 401
    (record,) = [r for r in caplog.records if r.name == SIGN_IN_LOGGER]
    assert record.levelname == "WARNING"
    return record


def test_a_refusal_involving_an_account_names_it_by_row_id(signin, caplog):
    admin = _admin_row(signin.db)
    sub = str(uuid.uuid4())
    signin.use(identity("admin", sub, f"x.{uuid.uuid4().hex[:8]}@example.com"))
    record = _record(signin, caplog)
    assert record.reason == "local_password"
    assert record.cognito_username == "admin"
    assert record.sub == sub
    assert record.row_id == str(admin.id)


def test_a_refusal_with_no_account_carries_no_row_id(signin, caplog):
    sub = str(uuid.uuid4())
    username = f"noemail_{uuid.uuid4().hex[:8]}"
    signin.use(identity(username, sub, None))
    record = _record(signin, caplog)
    assert record.reason == "no_email"
    assert record.cognito_username == username
    assert record.sub == sub
    assert record.row_id is None
