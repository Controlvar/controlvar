"""Who may call the four metrics routes.

``GET /api/v1/metrics/summary``, ``/aggregated`` and ``/errors`` answer every
active account, whatever its role. ``POST /api/v1/metrics/aggregate`` answers
only a superuser; every other account, an ADMIN included, gets
``403 {"detail": "Not enough permissions"}``. It used to answer every role.

An inactive account is refused with ``400 {"detail": "Inactive user"}`` on all
four. The local provider already refused it inside ``get_current_user``; with
``AUTH_PROVIDER=cognito`` the account linked to the Cognito user ID was
answered whether it was active or not, so the Cognito cases are the ones that
show the active-account check runs. An active linked account is answered by
its role, which shows the stub signs in.

Driven through the real routes on PostgreSQL with real local tokens; the only
dependency override is ``deps.get_db``. With Cognito, only the identity
provider (``deps.auth_service.get_user_with_groups``) is stubbed, with the
payload ``backend/tests/integration/cognito_identity.py`` builds. Every
request names a feature flag no row has, so the aggregation writes nothing
that another test reads.
"""

from __future__ import annotations

import uuid
from typing import Dict, List, Optional, Tuple

import pytest

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.models.user import User, UserRole
from backend.app.services.cognito_accounts import cognito_external_id
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from backend.tests.integration.cognito_identity import identity

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

TAG = "metrics_access"

V1 = "/api/v1/metrics"
READS: List[Tuple[str, str]] = [
    ("GET", f"{V1}/summary"),
    ("GET", f"{V1}/aggregated"),
    ("GET", f"{V1}/errors"),
]
AGGREGATE: Tuple[str, str] = ("POST", f"{V1}/aggregate")
ROUTES: List[Tuple[str, str]] = READS + [AGGREGATE]

INACTIVE = (400, {"detail": "Inactive user"})
NOT_ENOUGH = (403, {"detail": "Not enough permissions"})

#: The roles of an account that is not a superuser.
ROLES = [UserRole.ADMIN, UserRole.DEVELOPER, UserRole.ANALYST, UserRole.VIEWER]

#: Every account a role cell names: the four roles, and a superuser.
ACCOUNTS = [role.value for role in ROLES] + ["superuser"]

COGNITO_TOKEN = "cognito-access-token"
COGNITO_BEARER = {"Authorization": f"Bearer {COGNITO_TOKEN}"}


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def local(monkeypatch):
    local_sign_in(monkeypatch)


class CognitoStub:
    """``get_user_with_groups``: answers the payload it is given."""

    def __init__(self) -> None:
        self.payload: Optional[dict] = None

    def __call__(self, token: str) -> dict:
        assert token == COGNITO_TOKEN, token
        assert self.payload is not None
        return self.payload


@pytest.fixture
def cognito(monkeypatch):
    stub = CognitoStub()
    # The development sign-in off, as for the local cases; then Cognito.
    local_sign_in(monkeypatch)
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    monkeypatch.setattr(settings, "SYNC_ROLES_ON_LOGIN", False)
    monkeypatch.setattr(deps.auth_service, "get_user_with_groups", stub)
    return stub


def _account(db_session, name: str, *, active: bool = True) -> User:
    if name == "superuser":
        user = make_user(db_session, UserRole.ADMIN, superuser=True, tag=TAG)
    else:
        user = make_user(db_session, UserRole(name), tag=TAG)
    if not active:
        user.is_active = False
        db_session.commit()
    return user


def _linked(db_session, stub: CognitoStub, name: str, *, active: bool) -> User:
    """An account linked to a Cognito user ID, and the stub answering for it."""
    user = _account(db_session, name, active=active)
    sub = f"sub-{uuid.uuid4().hex}"
    user.external_id = cognito_external_id(sub)
    db_session.commit()
    stub.payload = identity(user.username, sub, user.email, [])
    return user


def _answers(client, headers: Dict[str, str]) -> Dict[str, Tuple[int, object]]:
    """Each route's status and body. The flag is one no row has."""
    params = {"feature_flag_id": str(uuid.uuid4())}
    answers = {}
    for method, path in ROUTES:
        resp = client.request(method, path, params=params, headers=headers)
        answers[f"{method} {path}"] = (resp.status_code, resp.json())
    return answers


def _key(route: Tuple[str, str]) -> str:
    return f"{route[0]} {route[1]}"


@pytest.mark.parametrize("name", ACCOUNTS)
def test_every_active_account_reads_the_three_metrics_routes(
    client, local, db_session, name
):
    answers = _answers(client, auth(_account(db_session, name)))
    statuses = {route: answers[_key(route)][0] for route in READS}
    assert statuses == dict.fromkeys(statuses, 200), answers


@pytest.mark.regression
@pytest.mark.parametrize("role", ROLES, ids=[role.value for role in ROLES])
def test_only_a_superuser_may_start_an_aggregation(client, local, db_session, role):
    answers = _answers(client, auth(_account(db_session, role.value)))
    assert answers[_key(AGGREGATE)] == NOT_ENOUGH


def test_a_superuser_starts_an_aggregation(client, local, db_session):
    answers = _answers(client, auth(_account(db_session, "superuser")))
    status, body = answers[_key(AGGREGATE)]
    assert status == 200, body
    assert body == {"status": "success", "aggregated_records": 0, "period": "day"}


@pytest.mark.parametrize("name", ["developer", "superuser"])
def test_an_inactive_local_account_is_refused_on_the_four_metrics_routes(
    client, local, db_session, name
):
    user = _account(db_session, name, active=False)
    answers = _answers(client, auth(user))
    assert answers == dict.fromkeys(answers, INACTIVE)


@pytest.mark.regression
@pytest.mark.parametrize("name", ["developer", "superuser"])
def test_an_inactive_cognito_account_is_refused_on_the_four_metrics_routes(
    client, cognito, db_session, name
):
    _linked(db_session, cognito, name, active=False)
    answers = _answers(client, COGNITO_BEARER)
    assert answers == dict.fromkeys(answers, INACTIVE)


@pytest.mark.parametrize("name", ["developer", "superuser"])
def test_an_active_cognito_account_is_answered_by_its_role(
    client, cognito, db_session, name
):
    _linked(db_session, cognito, name, active=True)
    answers = _answers(client, COGNITO_BEARER)
    statuses = {route: status for route, (status, _) in answers.items()}
    aggregate = 200 if name == "superuser" else 403
    assert statuses == {
        **{_key(route): 200 for route in READS},
        _key(AGGREGATE): aggregate,
    }, answers
