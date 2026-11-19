"""``LOCAL_AUTH_2FA_REQUIRED_ROLES``: what it accepts, and who it applies to (#1812).

The contract: unset, ``""`` (what Compose passes for an unset
``${LOCAL_AUTH_2FA_REQUIRED_ROLES:-}``) and blank mean no role; entries are
trimmed; each must be exactly ``ADMIN``, ``DEVELOPER``, ``ANALYST`` or
``VIEWER``; anything else -- ``ADMIN,``, ``,ADMIN``, ``admin``, ``ADMNI`` --
stops the API starting, naming the entry and the four names. The settings
class refuses it (``config.py``), and so does the container's start-up check
(``preflight.py``), in every environment.

A typo that silently meant "no role" would leave every account of the role
signing in with the password alone while the operator believes otherwise, so
each refusal below is a planted typo that must stop the start.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.app.core import preflight
from backend.app.core.config import Settings, settings
from backend.app.core.settings_rules import (
    TWO_FACTOR_ROLE_NAMES,
    TWO_FACTOR_ROLES_SETTING,
    parse_two_factor_roles,
    two_factor_roles_error,
)
from backend.app.core.two_factor_policy import two_factor_required
from backend.app.models.user import UserRole

pytestmark = [pytest.mark.unit, pytest.mark.regression]

_KEY = "a1" * 32
#: A production environment the image starts with (core profile).
GOOD = {
    "ENVIRONMENT": "production",
    "SECRET_KEY": _KEY,
    "FIRST_SUPERUSER_PASSWORD": "Xq7-first-admin-password",
    "PUBLIC_BASE_URL": "https://controlvar.example.com",
    "POSTGRES_SERVER": "db.internal",
}

#: Accepted: the value as the environment gives it, and the roles it means.
ACCEPTED = [
    (None, []),
    ("", []),
    ("   ", []),
    ("ADMIN", ["ADMIN"]),
    (" ADMIN ", ["ADMIN"]),
    ("ADMIN,DEVELOPER", ["ADMIN", "DEVELOPER"]),
    ("ADMIN, VIEWER", ["ADMIN", "VIEWER"]),
    ("ANALYST,ADMIN,ANALYST", ["ANALYST", "ADMIN"]),
    ("ADMIN,DEVELOPER,ANALYST,VIEWER", ["ADMIN", "DEVELOPER", "ANALYST", "VIEWER"]),
]
ACCEPTED_IDS = [
    "unset",
    "empty",
    "blank",
    "one",
    "trimmed",
    "two",
    "space-after-comma",
    "twice",
    "all-four",
]

#: Refused: the value, and the text the refusal must contain.
REFUSED = [
    ("ADMIN,", "has an empty entry"),
    (",ADMIN", "has an empty entry"),
    ("ADMIN,,VIEWER", "has an empty entry"),
    (" , ", "has an empty entry"),
    ("admin", "entry 'admin' is not a role"),
    ("Admin", "entry 'Admin' is not a role"),
    ("ADMNI", "entry 'ADMNI' is not a role"),
    ("ADMIN,SUPERUSER", "entry 'SUPERUSER' is not a role"),
    ('["ADMIN"]', "entry '[\"ADMIN\"]' is not a role"),
]
REFUSED_IDS = [
    "trailing-comma",
    "leading-comma",
    "double-comma",
    "commas-only",
    "lower-case",
    "title-case",
    "misspelt",
    "not-a-role",
    "json-array",
]


def _settings(value):
    """The settings class built with ``value`` as the environment's string."""
    if value is None:
        return Settings()
    return Settings(**{TWO_FACTOR_ROLES_SETTING: value})


def _preflight(value, environment="production"):
    env = dict(GOOD, ENVIRONMENT=environment)
    if value is not None:
        env[TWO_FACTOR_ROLES_SETTING] = value
    return preflight.check(
        env, full_profile=False, root=Path("/nonexistent-preflight-root")
    )


def test_the_names_are_the_roles():
    assert set(TWO_FACTOR_ROLE_NAMES) == {role.name for role in UserRole}
    assert len(TWO_FACTOR_ROLE_NAMES) == len(UserRole)


@pytest.mark.parametrize("value, roles", ACCEPTED, ids=ACCEPTED_IDS)
def test_accepted_values_and_the_roles_they_mean(value, roles):
    assert parse_two_factor_roles(value) == roles
    assert two_factor_roles_error(value) is None
    assert _settings(value).LOCAL_AUTH_2FA_REQUIRED_ROLES == roles
    assert _preflight(value) == []


def test_unset_is_no_role_by_default():
    assert Settings.model_fields[TWO_FACTOR_ROLES_SETTING].default == []


@pytest.mark.parametrize("value, says", REFUSED, ids=REFUSED_IDS)
def test_a_typo_stops_the_start_naming_the_entry_and_the_four_names(value, says):
    with pytest.raises(ValidationError) as refused:
        _settings(value)
    message = str(refused.value)
    assert TWO_FACTOR_ROLES_SETTING in message
    assert says in message
    assert "ADMIN, DEVELOPER, ANALYST, VIEWER" in message


@pytest.mark.parametrize("value, says", REFUSED, ids=REFUSED_IDS)
@pytest.mark.parametrize("environment", ["development", "staging", "production"])
def test_the_start_up_check_refuses_the_same_in_every_environment(
    value, says, environment
):
    problems = _preflight(value, environment)
    assert len(problems) == 1, problems
    assert problems[0].startswith(TWO_FACTOR_ROLES_SETTING)
    assert says in problems[0]
    assert "ADMIN, DEVELOPER, ANALYST, VIEWER" in problems[0]


def test_the_start_up_check_prints_it_and_exits_78(capsys):
    env = dict(GOOD, **{TWO_FACTOR_ROLES_SETTING: "admin"})
    assert preflight.main(env) == preflight.EX_CONFIG
    printed = capsys.readouterr().err
    assert "LOCAL_AUTH_2FA_REQUIRED_ROLES entry 'admin' is not a role" in printed


def test_a_list_given_in_code_is_checked_entry_by_entry():
    assert parse_two_factor_roles(["VIEWER", " ADMIN "]) == ["VIEWER", "ADMIN"]
    with pytest.raises(ValueError, match="has an empty entry"):
        parse_two_factor_roles(["ADMIN", ""])
    with pytest.raises(ValueError, match="is not a role"):
        parse_two_factor_roles(["admin"])
    with pytest.raises(ValueError, match="comma-separated"):
        parse_two_factor_roles(7)


# ---------------------------------------------------------------------------
# Who it applies to
# ---------------------------------------------------------------------------


def _account(role, *, password="hash", superuser=False):
    return SimpleNamespace(role=role, hashed_password=password, is_superuser=superuser)


@pytest.fixture
def required(monkeypatch):
    def set_roles(value):
        monkeypatch.setattr(
            settings, TWO_FACTOR_ROLES_SETTING, parse_two_factor_roles(value)
        )

    return set_roles


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.name)
def test_no_role_is_required_by_default(role, required):
    required(None)
    assert two_factor_required(_account(role)) is False


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.name)
def test_exactly_the_listed_roles_are_required(role, required):
    required("ADMIN,ANALYST")
    expected = role.name in ("ADMIN", "ANALYST")
    assert two_factor_required(_account(role)) is expected


def test_a_superuser_of_a_listed_role_is_required_too(required):
    required("ADMIN")
    assert two_factor_required(_account(UserRole.ADMIN, superuser=True)) is True
    assert two_factor_required(_account(UserRole.VIEWER, superuser=True)) is False


@pytest.mark.parametrize("password", [None, ""], ids=["null", "empty"])
def test_an_account_without_a_password_is_never_required(password, required):
    """It signs in through SSO, which asks for no code: the requirement is
    where the password is checked."""
    required("ADMIN,DEVELOPER,ANALYST,VIEWER")
    assert two_factor_required(_account(UserRole.ADMIN, password=password)) is False
