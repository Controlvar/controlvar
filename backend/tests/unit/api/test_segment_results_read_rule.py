"""A segment's results admit exactly the callers who may read the segment (#1933).

``GET /api/v1/results/{experiment_id}/segments/{segment_id}`` is admitted by
the results router's ``require_experiment_read``. Reading the segment itself
(``GET /api/v1/segments/{id}``) is admitted by ``segments._require_permission``
with READ. Both come down to ``check_permission(user, EXPERIMENT, READ)``
today, so a caller who can read one can read the other: the route shows no
segment's members to someone the segment's own route would refuse. This pins
that the two predicates admit the same callers, for every shipped role, a
user with no role and a superuser; if segments ever get a rule of their own,
it fails until the route is revisited.

It also pins what an API without the route answers its path (an older server
behind a newer client): FastAPI's 404, never the unfiltered results.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest
from fastapi import HTTPException

from backend.app.api import deps
from backend.app.api.v1.endpoints import segments
from backend.app.core.permissions import Action
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.unit]

#: caller -> (role, superuser, admitted)
CALLERS = {
    "superuser": (UserRole.ADMIN, True, True),
    "admin": (UserRole.ADMIN, False, True),
    "developer": (UserRole.DEVELOPER, False, True),
    "analyst": (UserRole.ANALYST, False, True),
    "viewer": (UserRole.VIEWER, False, True),
    "no_role": (None, False, False),
}


def _user(role: Optional[UserRole], is_superuser: bool) -> User:
    # A transient row: no session, so no custom role or grant is read, and a
    # role of None stays None (only an insert writes the VIEWER default).
    return User(
        username="segment-read-rule",
        email="segment-read-rule@example.test",
        hashed_password="unused",
        is_active=True,
        is_superuser=is_superuser,
        role=role,
    )


def _refused(call) -> bool:
    try:
        call()
    except HTTPException as exc:
        assert exc.status_code == 403
        return True
    return False


@pytest.mark.parametrize("caller", list(CALLERS))
def test_segments_read_and_results_read_admit_the_same_callers(caller):
    role, is_superuser, admitted = CALLERS[caller]
    user = _user(role, is_superuser)
    assert user.role is role
    segment_refused = _refused(lambda: segments._require_permission(user, Action.READ))
    results_refused = _refused(
        lambda: asyncio.run(deps.require_experiment_read(current_user=user))
    )
    assert segment_refused == results_refused, (
        f"{caller}: segments' read refuses={segment_refused}, "
        f"the results read refuses={results_refused}"
    )
    assert results_refused is (not admitted), caller


def test_a_server_without_the_route_answers_404_not_found():
    """What an API older than the route answers, and what a client of one
    sees: the same results router without the segment route answers its path
    with FastAPI's ``404 {"detail": "Not Found"}``, before any check, never
    with the experiment's unfiltered results."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.app.api.v1.endpoints import results

    segment_path = "/{experiment_id}/segments/{segment_id}"
    assert any(getattr(r, "path", None) == segment_path for r in results.router.routes)
    older = FastAPI()
    for route in results.router.routes:
        if getattr(route, "path", None) != segment_path:
            older.router.routes.append(route)
    experiment_id = "00000000-0000-4000-8000-000000000001"
    segment_id = "00000000-0000-4000-8000-000000000002"
    response = TestClient(older).get(f"/{experiment_id}/segments/{segment_id}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
