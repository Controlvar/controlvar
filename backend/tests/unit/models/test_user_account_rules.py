"""What an operator can rely on when two accounts share an address.

The self-hosting migrations page has an operator choose which account keeps an
address and retire the other. These are the facts it rests on:

* the ``users`` table records no time of the last sign-in;
* the API refuses an address under a reserved domain (``.invalid``, ``.test``,
  ``.local``) in the body that edits an account, so an account given one could
  no longer be edited through it;
* deleting a user deletes the rows that belong to it and detaches the rest.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]


#: Every column of ``users``. None records when the account last signed in;
#: a column added here is a change to that answer, to be read before it is.
USERS_COLUMNS = [
    "created_at",
    "email",
    "external_id",
    "first_name",
    "hashed_password",
    "id",
    "is_active",
    "is_superuser",
    "last_name",
    "preferences",
    "role",
    "settings",
    "updated_at",
    "username",
]


@pytest.mark.regression
def test_there_is_no_last_sign_in_column():
    from backend.app.models.user import User

    assert sorted(User.__table__.columns.keys()) == USERS_COLUMNS


@pytest.mark.regression
@pytest.mark.parametrize("domain", ["invalid", "test", "local"])
def test_an_address_under_a_reserved_domain_is_refused_when_editing_an_account(
    domain,
):
    from backend.app.schemas.user import UserUpdate

    with pytest.raises(ValidationError):
        UserUpdate(username="retired_account", email=f"retired-x@acme.{domain}")
    retired = f"retired-{uuid.uuid4()}@acme.com"
    assert UserUpdate(username="retired_account", email=retired).email == retired


@pytest.mark.regression
def test_the_route_that_edits_an_account_takes_that_body_and_its_email():
    """``PUT /api/v1/admin/users/{user_id}``'s body is ``UserUpdate``, with the
    email required: an edit has to send the account's address."""
    from backend.app.main import app

    spec = app.openapi()
    put = spec["paths"]["/api/v1/admin/users/{user_id}"]["put"]
    schema = put["requestBody"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/UserUpdate"}, schema
    body = spec["components"]["schemas"]["UserUpdate"]
    assert {"email", "username"} <= set(body["required"]), body["required"]
    assert body["properties"]["email"]["format"] == "email"


#: Rows that belong to the user, and go with it.
DELETED_WITH_THE_USER = {
    ("api_keys", "user_id"),
    ("reports", "owner_id"),
    ("notification_preferences", "user_id"),
    ("user_role_association", "user_id"),
    ("direct_permission_grants", "user_id"),
    ("workspace_members", "user_id"),
    ("user_custom_roles", "user_id"),
}

_REFERENCES_PROBE = """
    import json

    from backend.app.core import hooks
    from backend.app.models import register_core_models
    from backend.app.modules_loader import load_modules

    Base = register_core_models()
    load_modules(with_routers=False)
    hooks.import_registered_models()
    found = []
    for table in Base.metadata.sorted_tables:
        for fk in table.foreign_keys:
            if fk.column.table.name == "users" and fk.column.name == "id":
                found.append([table.name, fk.parent.name, fk.ondelete])
    print(json.dumps(found))
"""


@pytest.mark.regression
def test_deleting_a_user_deletes_its_own_rows_and_detaches_everything_else():
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_REFERENCES_PROBE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    references = json.loads(result.stdout.strip().splitlines()[-1])
    named = {(table, column) for table, column, _ in references}
    # The core ones the page names are always there.
    assert {
        ("api_keys", "user_id"),
        ("reports", "owner_id"),
        ("notification_preferences", "user_id"),
        ("user_role_association", "user_id"),
        ("experiments", "owner_id"),
        ("feature_flags", "owner_id"),
        ("audit_logs", "user_id"),
    } <= named, sorted(named)
    for table, column, ondelete in references:
        expected = "CASCADE" if (table, column) in DELETED_WITH_THE_USER else "SET NULL"
        assert ondelete == expected, (table, column, ondelete)
