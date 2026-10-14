"""The mutual exclusion check is made per request (#1142).

Each request reads the group's active experiments, and the user's stored
assignments, as they are committed when it runs; nothing locks them across
requests. So two requests for the same new user, one started while only A is
active and one after B was activated, can both enrol the user while the first
has not committed: the user ends up in A and in B. Requests made one after the
other refuse the second enrolment.

The group, its experiments and the users are fixed (the fixtures of
``test_mutual_exclusion_sticky.py``), so the hash puts the users in the same
slots on every run.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.models.experiment import ExperimentStatus
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.mutual_exclusion_service import MutualExclusionService
from backend.tests.integration.services.test_mutual_exclusion_sticky import (
    EXP_A,
    EXP_B,
    GROUP_ID,
    _set,
    _users_in,
    group_setup,
    owner,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]


@pytest.fixture
def session_factory(db_session):
    """Sessions for the requests; all rolled back and closed before teardown,
    so a failed assertion cannot leave one holding locks the cleanup waits on."""
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )
    made = []

    def make():
        session = factory()
        session.execute(text("SET search_path TO test_experimentation"))
        made.append(session)
        return session

    yield make
    for session in made:
        session.rollback()
        session.close()


def _users_in_b_slot(db_session, count):
    """Users the hash puts in B's slot once A and B are both active."""
    service = MutualExclusionService(db_session)
    found = []
    i = 0
    while len(found) < count:
        user_id = f"me-per-request-{i:04d}"
        if service.select_experiment_for_user(user_id, GROUP_ID) == EXP_B:
            found.append(user_id)
        i += 1
    return found


@pytest.mark.usefixtures("group_setup")
def test_two_requests_in_flight_across_an_activation_can_both_enrol(
    db_session, session_factory
):
    _set(db_session, EXP_B, status=ExperimentStatus.ACTIVE)
    in_flight, one_after_another = _users_in_b_slot(db_session, 2)
    _set(db_session, EXP_B, status=ExperimentStatus.DRAFT)

    # Request 1, for both users, while only A is active: enrolled in A, and
    # (for in_flight) not yet committed when request 2 runs.
    first = session_factory()
    slow = AssignmentService(first)
    real_commit = first.commit
    first.commit = first.flush  # the request is still in flight
    answer = slow.assign_user(in_flight, EXP_A, track_exposure=False)
    assert answer["assigned"] is True
    first.commit = real_commit
    done = AssignmentService(session_factory())
    assert done.assign_user(one_after_another, EXP_A, track_exposure=False)["assigned"]

    # B is activated, and request 2 asks for B.
    _set(db_session, EXP_B, status=ExperimentStatus.ACTIVE)
    second = AssignmentService(session_factory())
    racing = second.assign_user(in_flight, EXP_B, track_exposure=False)
    later = second.assign_user(one_after_another, EXP_B, track_exposure=False)

    # Made one after the other, the second request is refused ...
    assert later["assigned"] is False
    assert later["reason"] == "mutual_exclusion"
    # ... while in flight, both succeed.
    assert racing["assigned"] is True
    real_commit()
    db_session.expire_all()
    assert in_flight in _users_in(db_session, EXP_A)
    assert in_flight in _users_in(db_session, EXP_B)
    assert one_after_another in _users_in(db_session, EXP_A)
    assert one_after_another not in _users_in(db_session, EXP_B)
