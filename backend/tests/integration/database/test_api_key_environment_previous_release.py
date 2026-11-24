"""The previous release's API key routes on this schema (#2011).

While a container still runs 0.48.0 against the upgraded database, during the
rolling upgrade and after a rollback of the API alone, it has no environments:

1. asked for a staging key, it ignores the field: it stores a production key,
   and neither its answer nor its list names an environment. This release,
   asked the same on the same schema, makes a staging key. The dashboard
   compares the answer with what was asked, and revokes a key that does not
   match with the same ``DELETE``, which 0.48.0 serves;
2. the rollback runbook's steps work there: its list has ``created_at`` and no
   ``environment``; an ADMIN deletes another user's key; and the SQL
   alternative deactivates exactly the keys outside production, which 0.48.0
   then refuses with 401 while it still serves the production ones.

The previous release is ``test_flag_environments_migration``'s copy of this
tree, with what this release adds to the key routes taken out of it as well.
Each piece taken out is counted, so a copy that still has one fails here.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from sqlalchemy import text

from backend.app.models.user import User
from backend.tests.integration.database import (
    test_flag_environments_migration as migration,
)
from backend.tests.integration.database.test_flag_environments_migration import (
    BOTH_PROFILES,
    UPGRADED,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

KEYS = "/api/v1/api-keys"

#: What 0.48.0 answers a create with, and one listed key.
CREATED_BEFORE = {"id", "name", "key", "prefix", "created_at", "expires_at"}
LISTED_BEFORE = {
    "id",
    "name",
    "description",
    "scopes",
    "is_active",
    "user_id",
    "created_at",
    "expires_at",
    "last_used_at",
}

#: The rollback runbook's SQL, with the schema named.
RUNBOOK_SQL = (
    "UPDATE \"{schema}\".api_keys SET is_active=false WHERE environment <> 'production'"
)

#: What this release adds to the key routes: (file, pattern, replacement, how
#: many there are).
_KEY_ROUTE_ADDITIONS = (
    (
        ("backend", "app", "schemas", "api_key.py"),
        r"(?ms)^    # Declared before ``scopes``.*?^    \)\n",
        "",
        1,
    ),
    (
        ("backend", "app", "schemas", "api_key.py"),
        r'(?ms)^    @field_validator\("scopes", mode="after"\)\n    @classmethod\n'
        r"    def _mcp_read_is_for_production.*?^        return v\n",
        "",
        1,
    ),
    (
        ("backend", "app", "schemas", "api_key.py"),
        r"(?m)^    environment: FlagEnvironmentName = Field\(\n        \.\.\., .*\n    \)\n",
        "",
        2,
    ),
    (
        ("backend", "app", "api", "v1", "endpoints", "api_keys.py"),
        r"(?m)^ +environment=(?:key|body|api_key)\.environment,\n",
        "",
        3,
    ),
    (
        ("backend", "app", "models", "api_key.py"),
        r'(?m)^        environment="production",\n(?=    \):)',
        "",
        1,
    ),
    (
        ("backend", "app", "models", "api_key.py"),
        r"(?m)^            environment=environment,\n",
        "",
        1,
    ),
    (
        ("backend", "app", "services", "audit_service.py"),
        r'\("name", "scopes", "expires_at", "user_id", "environment"\)',
        '("name", "scopes", "expires_at", "user_id")',
        1,
    ),
)


@pytest.fixture
def scratch_schema(test_db):
    yield from migration._scratch(test_db, "keyenv")


def _previous_release(profile: str, tmp_path) -> Path:
    """0.48.0, its API key routes included."""
    tree = migration._previous_release(profile, tmp_path)
    for parts, pattern, replacement, count in _KEY_ROUTE_ADDITIONS:
        module = tree.joinpath(*parts)
        source, found = re.subn(
            pattern, replacement, module.read_text(encoding="utf-8")
        )
        assert found == count, f"{found} of {count} in {module.name}: {pattern}"
        module.write_text(source, encoding="utf-8")
    return tree


def _bearer(user: User) -> dict:
    return {"Authorization": f"Bearer {migration._token(user)}"}


def _answers(tree, schema: str, tmp_path, *requests) -> list:
    return migration._client(tree, schema, tmp_path, list(requests))["answers"]


@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_previous_release_makes_a_production_key_when_asked_for_staging(
    profile, test_db, scratch_schema, tmp_path
):
    release_n = migration._at_head(test_db, profile, UPGRADED, scratch_schema, tmp_path)
    user = migration._user(test_db, scratch_schema)
    release_n_minus_1 = _previous_release(profile, tmp_path)
    create = [
        "POST",
        KEYS,
        _bearer(user),
        {"name": "asked for staging", "environment": "staging"},
    ]
    read = ["GET", KEYS, _bearer(user), None]

    (status, old), (listed_status, listed) = _answers(
        release_n_minus_1, scratch_schema, tmp_path, create, read
    )
    print(f"[{profile}] 0.48.0 asked for a staging key: {status} {sorted(old)}")
    assert status == 201, old
    # No field names an environment, and the key is a production key.
    assert set(old) == CREATED_BEFORE
    assert listed_status == 200, listed
    (item,) = listed
    assert set(item) == LISTED_BEFORE
    stored = migration._key_environments(test_db, scratch_schema)
    assert stored == {old["id"]: ("production", True)}

    # This release, on the same schema, is asked the same.
    (status, new), (listed_status, listed) = _answers(
        release_n, scratch_schema, tmp_path, create, read
    )
    assert status == 201, new
    assert new["environment"] == "staging"
    assert {item["id"]: item["environment"] for item in listed} == {
        old["id"]: "production",
        new["id"]: "staging",
    }
    stored = migration._key_environments(test_db, scratch_schema)
    assert stored == {
        old["id"]: ("production", True),
        new["id"]: ("staging", True),
    }

    # What the dashboard does with an answer that does not match what it asked
    # for: the revoke is served by the release that made the key.
    ((status, answer),) = _answers(
        release_n_minus_1,
        scratch_schema,
        tmp_path,
        ["DELETE", f"{KEYS}/{old['id']}", _bearer(user), None],
    )
    assert status == 204, answer
    stored = migration._key_environments(test_db, scratch_schema)
    assert stored == {new["id"]: ("staging", True)}
    shutil.rmtree(release_n_minus_1, ignore_errors=True)


@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_rollback_runbooks_steps_work_on_the_previous_release(
    profile, test_db, scratch_schema, tmp_path
):
    migration._at_head(test_db, profile, UPGRADED, scratch_schema, tmp_path)
    admin = migration._user(test_db, scratch_schema, superuser=False)
    owner = migration._user(test_db, scratch_schema)
    migration._insert_flag(test_db, scratch_schema, "runbook-flag", "ACTIVE", 100)
    keys = {
        name: migration._insert_key(test_db, scratch_schema, owner.id, environment)
        for name, environment in (
            ("production", "production"),
            ("staging", "staging"),
            ("development", "development"),
            ("deleted", "staging"),
        )
    }
    release_n_minus_1 = _previous_release(profile, tmp_path)

    def evaluated() -> dict:
        answers = _answers(
            release_n_minus_1,
            scratch_schema,
            tmp_path,
            *(
                migration._evaluate("runbook-flag", "u-1", plaintext)
                for _, plaintext in keys.values()
            ),
        )
        return dict(zip(keys, (status for status, _ in answers)))

    # 0.48.0 serves every one of them, the keys outside production included.
    assert evaluated() == dict.fromkeys(keys, 200)

    # An ADMIN who is not a superuser lists every key and deletes one. The list
    # gives when each was created, and no environment.
    (listed_status, listed), (deleted_status, _) = _answers(
        release_n_minus_1,
        scratch_schema,
        tmp_path,
        ["GET", f"{KEYS}?all=true", _bearer(admin), None],
        ["DELETE", f"{KEYS}/{keys['deleted'][0]}", _bearer(admin), None],
    )
    assert listed_status == 200, listed
    assert {item["id"] for item in listed} == {key_id for key_id, _ in keys.values()}
    for item in listed:
        assert set(item) == LISTED_BEFORE
        assert item["created_at"]
    assert deleted_status == 204
    assert evaluated() == {
        "production": 200,
        "staging": 200,
        "development": 200,
        "deleted": 401,
    }

    # The SQL alternative: the keys outside production stop, production's do not.
    with test_db.begin() as conn:
        changed = conn.execute(text(RUNBOOK_SQL.format(schema=scratch_schema)))
    assert changed.rowcount == 2
    assert evaluated() == {
        "production": 200,
        "staging": 401,
        "development": 401,
        "deleted": 401,
    }
    shutil.rmtree(release_n_minus_1, ignore_errors=True)
