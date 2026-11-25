"""``backend.app.api.key_environment``: the presented key's flag environment (#2011).

No database: ``deps.get_api_key`` runs on a stub session. The routes are in
``backend/tests/integration/api/test_key_environment_routes.py``.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request

from backend.app.api import deps, key_environment, page_key, sdk_scope
from backend.app.api.key_environment import (
    presented_environment,
    production_only_detail,
    refuse_non_production,
)
from backend.app.core import flag_environments
from backend.app.models.api_key import APIKey
from backend.app.models.user import User
from backend.app.services import flag_environment_reads

pytestmark = [pytest.mark.unit]

#: The 403, word for word.
STAGING = (
    "This API key belongs to the staging environment. Experiments and event "
    "tracking run in production only; use a production key."
)
DEVELOPMENT = (
    "This API key belongs to the development environment. Experiments and event "
    "tracking run in production only; use a production key."
)


def _request(**state) -> Request:
    request = Request({"type": "http", "headers": []})
    for name, value in state.items():
        setattr(request.state, name, value)
    return request


@pytest.mark.parametrize("environment", ["production", "staging", "development"])
def test_the_environment_is_the_one_recorded(environment):
    assert presented_environment(_request(flag_environment=environment)) == environment


def test_a_request_with_nothing_recorded_is_productions():
    """Only a test that replaces ``deps.get_api_key`` leaves it unrecorded."""
    assert presented_environment(_request()) == "production"


def test_a_production_key_is_not_refused():
    assert refuse_non_production("production") is None


@pytest.mark.parametrize(
    ("environment", "detail"), [("staging", STAGING), ("development", DEVELOPMENT)]
)
def test_the_refusal_is_one_403_with_one_text(environment, detail):
    assert production_only_detail(environment) == detail
    with pytest.raises(HTTPException) as refused:
        refuse_non_production(environment)
    assert (refused.value.status_code, refused.value.detail) == (403, detail)
    assert refused.value.headers is None


@pytest.mark.parametrize("environment", ["Production", "prod", "", None, "qa"])
def test_anything_but_production_is_refused(environment):
    """Fail closed: the rule admits exactly ``production``."""
    with pytest.raises(HTTPException):
        refuse_non_production(environment)


# ---------------------------------------------------------------------------
# What get_api_key records
# ---------------------------------------------------------------------------


class _Row:
    """An ``APIKey`` row whose environment can be read only before the commit
    of ``update_last_used``, which would expire it (and read it again)."""

    def __init__(self, environment: str, *, valid: bool = True) -> None:
        self.is_valid, self.user_id, self.id = valid, "u", "k"
        self._environment, self.committed = environment, False

    @property
    def environment(self) -> str:
        assert not self.committed, "read after update_last_used committed"
        return self._environment

    def update_last_used(self, _db) -> bool:
        self.committed = True
        return True


def _session(row, user) -> MagicMock:
    db = MagicMock()
    answers = {APIKey: row, User: user}
    db.query.side_effect = lambda model: MagicMock(
        **{"filter.return_value.first.return_value": answers[model]}
    )
    return db


@pytest.mark.parametrize("environment", ["production", "staging", "development"])
def test_get_api_key_records_the_environment_of_the_row_it_read(environment):
    row, user = _Row(environment), SimpleNamespace(is_active=True)
    request = _request()
    db = _session(row, user)
    assert deps.get_api_key(db=db, api_key_header="k", request=request) is user
    assert row.committed
    assert presented_environment(request) == environment
    # Two reads: the key's row and its owner. The environment costs none.
    assert [call.args for call in db.query.call_args_list] == [(APIKey,), (User,)]


def test_get_api_key_still_works_as_a_plain_function():
    """The MCP read routes call it without a request (``read_scope``)."""
    row, user = _Row("staging"), SimpleNamespace(is_active=True)
    assert deps.get_api_key(db=_session(row, user), api_key_header="k") is user


@pytest.mark.parametrize(
    ("row", "user"),
    [
        (None, SimpleNamespace(is_active=True)),
        (_Row("staging", valid=False), SimpleNamespace(is_active=True)),
        (_Row("staging"), SimpleNamespace(is_active=False)),
        (_Row("staging"), None),
    ],
    ids=["unknown", "invalid", "inactive-owner", "no-owner"],
)
def test_a_refused_key_records_nothing(row, user):
    request = _request()
    with pytest.raises(HTTPException) as refused:
        deps.get_api_key(db=_session(row, user), api_key_header="k", request=request)
    assert refused.value.status_code == 401
    assert not hasattr(request.state, "flag_environment")


# ---------------------------------------------------------------------------
# Never the deployment's ENVIRONMENT
# ---------------------------------------------------------------------------


def _names(source: str) -> set:
    tree = ast.parse(source)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found |= {node.name.rsplit(".", 1)[-1], node.asname or ""}
        elif isinstance(node, ast.ImportFrom):
            found.add((node.module or "").rsplit(".", 1)[-1])
        elif isinstance(node, ast.Constant) and node.value == "ENVIRONMENT":
            found.add("ENVIRONMENT")
    return found


#: What reading the deployment's settings needs one of.
SETTINGS = {"settings", "config", "environ", "getenv", "ENVIRONMENT", "APP_ENV"}


@pytest.mark.regression
@pytest.mark.parametrize(
    "module",
    [flag_environments, key_environment, flag_environment_reads],
    ids=lambda module: module.__name__.rsplit(".", 1)[-1],
)
def test_the_resolver_never_reads_the_deployments_settings(module):
    names = _names(Path(module.__file__).read_text(encoding="utf-8"))
    assert not names & SETTINGS, (module.__name__, names & SETTINGS)


@pytest.mark.regression
@pytest.mark.parametrize(
    "function",
    [
        deps.get_api_key,
        sdk_scope.require_production_ruleset_key,
        page_key.require_page_key,
    ],
    ids=lambda function: function.__name__,
)
def test_what_reads_the_keys_environment_never_reads_the_deployments(function):
    assert not _names(inspect.getsource(function)) & SETTINGS


def test_the_settings_reader_sees_a_settings_read():
    """Guard the guard: each way of reading the deployment's ENVIRONMENT."""
    for source in (
        "from backend.app.core.config import settings\nx = settings.ENVIRONMENT",
        "import backend.app.core.config as c\nx = c.settings.ENVIRONMENT",
        "import os\nx = os.environ['ENVIRONMENT']",
        "import os\nx = os.getenv('APP_ENV')",
    ):
        assert _names(source) & SETTINGS, source
