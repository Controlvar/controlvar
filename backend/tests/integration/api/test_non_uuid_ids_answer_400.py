"""An id that is not a UUID answers 400, not 500 (#959).

``GET``, ``PUT`` and ``DELETE /api/v1/users/{user_id}`` parsed the id as a
UUID only when it held a ``-``; any other id (``abc``, ``me``) went to the
query as text, and PostgreSQL's refusal reached the client as a 500. Every id
is now parsed, and one that is not a UUID answers 400 "Invalid user ID
format", the answer ``abc-def`` already got:

* ``GET``: a superuser's request (anyone else gets 403 "Not enough
  permissions" first, as before; ``GET /api/v1/users/me`` is its own route);
* ``PUT`` and ``DELETE``: every caller, before the account is looked up, so
  ``DELETE /api/v1/users/me`` is just an id that is not a UUID.

``POST /api/v1/llm-experiments/{experiment_id}/evaluate`` passed the body's
``evaluation_id`` to ``UUID()`` unchecked. An id that is not a UUID now
answers 400 "Invalid evaluation_id format", the judge route's answer, after
the permission check.

Real sign-in through local tokens; the only override is the database session.
"""

from __future__ import annotations

import uuid
from typing import Any, Tuple

import pytest
from sqlalchemy.orm import Session

from backend.app.models.user import User, UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

V1 = "/api/v1"
TAG = "id959"
INVALID_USER_ID = (400, {"detail": "Invalid user ID format"})
INVALID_EVALUATION_ID = (400, {"detail": "Invalid evaluation_id format"})
FORBIDDEN = (403, {"detail": "Not enough permissions"})
USER_NOT_FOUND = (404, {"detail": "User not found"})

#: Ids that are not UUIDs: without a "-" (the 500), and with one (already 400).
NOT_UUIDS = ["abc", "abc-def"]


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def superuser(db_session):
    return make_user(db_session, UserRole.ADMIN, superuser=True, tag=TAG)


@pytest.fixture
def viewer(db_session):
    return make_user(db_session, UserRole.VIEWER, tag=TAG)


def _answer(response) -> Tuple[int, Any]:
    """(status, JSON body); a 500's body is plain text, kept as text."""
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text


def _accounts(db_session: Session) -> int:
    db_session.rollback()
    return db_session.query(User).count()


def _update_body(user: User) -> dict:
    """A body UserUpdate accepts (make_user's ``.test`` addresses are not)."""
    return {"username": user.username, "email": "after@example.com"}


@pytest.mark.regression
@pytest.mark.parametrize("user_id", NOT_UUIDS)
def test_a_superuser_gets_400_for_an_id_that_is_not_a_uuid(
    client, db_session, superuser, user_id
):
    headers = auth(superuser)
    before = _accounts(db_session)

    got = client.get(f"{V1}/users/{user_id}", headers=headers)
    put = client.put(
        f"{V1}/users/{user_id}", headers=headers, json=_update_body(superuser)
    )
    deleted = client.delete(f"{V1}/users/{user_id}", headers=headers)

    assert [_answer(got), _answer(put), _answer(deleted)] == [INVALID_USER_ID] * 3
    assert _accounts(db_session) == before


@pytest.mark.regression
@pytest.mark.parametrize("user_id", NOT_UUIDS)
def test_a_viewer_gets_403_on_get_and_400_on_put_and_delete(
    client, db_session, viewer, user_id
):
    headers = auth(viewer)
    before = _accounts(db_session)

    got = client.get(f"{V1}/users/{user_id}", headers=headers)
    put = client.put(
        f"{V1}/users/{user_id}", headers=headers, json=_update_body(viewer)
    )
    deleted = client.delete(f"{V1}/users/{user_id}", headers=headers)

    assert [_answer(got), _answer(put), _answer(deleted)] == [
        FORBIDDEN,
        INVALID_USER_ID,
        INVALID_USER_ID,
    ]
    assert _accounts(db_session) == before


@pytest.mark.regression
@pytest.mark.parametrize("role", ["superuser", "viewer"])
def test_me_is_an_id_that_is_not_a_uuid_on_put_and_delete(
    client, db_session, superuser, viewer, role
):
    caller = superuser if role == "superuser" else viewer
    before = _accounts(db_session)
    put = client.put(f"{V1}/users/me", headers=auth(caller), json=_update_body(caller))
    deleted = client.delete(f"{V1}/users/me", headers=auth(caller))
    assert [_answer(put), _answer(deleted)] == [INVALID_USER_ID] * 2
    assert _accounts(db_session) == before


def test_get_users_me_still_returns_the_callers_account(client, viewer):
    response = client.get(f"{V1}/users/me", headers=auth(viewer))
    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(viewer.id)


def test_a_uuid_with_no_account_still_answers_404(client, superuser):
    headers = auth(superuser)
    missing = uuid.uuid4()
    answers = [
        client.get(f"{V1}/users/{missing}", headers=headers),
        client.put(
            f"{V1}/users/{missing}", headers=headers, json=_update_body(superuser)
        ),
        client.delete(f"{V1}/users/{missing}", headers=headers),
        # The dashes are optional in a UUID; the id is still read as one.
        client.get(f"{V1}/users/{missing.hex}", headers=headers),
    ]
    assert [_answer(r) for r in answers] == [USER_NOT_FOUND] * 4


def test_an_id_without_dashes_still_names_its_account(client, superuser, viewer):
    response = client.get(f"{V1}/users/{viewer.id.hex}", headers=auth(superuser))
    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(viewer.id)


@pytest.mark.regression
@pytest.mark.parametrize("evaluation_id", ["", "abc", "not-a-uuid"])
def test_llm_evaluate_answers_400_for_an_evaluation_id_that_is_not_a_uuid(
    client, superuser, evaluation_id
):
    response = client.post(
        f"{V1}/llm-experiments/{uuid.uuid4()}/evaluate",
        headers=auth(superuser),
        json={"evaluation_id": evaluation_id},
    )
    assert _answer(response) == INVALID_EVALUATION_ID


def test_llm_evaluate_checks_permission_first_and_still_answers_404(
    client, superuser, viewer
):
    experiment = uuid.uuid4()
    refused = client.post(
        f"{V1}/llm-experiments/{experiment}/evaluate",
        headers=auth(viewer),
        json={"evaluation_id": ""},
    )
    missing = client.post(
        f"{V1}/llm-experiments/{experiment}/evaluate",
        headers=auth(superuser),
        json={"evaluation_id": str(uuid.uuid4())},
    )
    assert [_answer(refused), _answer(missing)] == [
        FORBIDDEN,
        (404, {"detail": "Evaluation not found"}),
    ]
