"""``GET /api/v1/holdout/{holdout_id}/results`` against the real database (#445).

What each test pins, and the change that turns it red:

* A13 window edges: an event at the user's own ``first_seen_at`` counts, one
  microsecond earlier does not, one at ``window_end`` does not (``>=`` made
  ``>`` drops the first).
* Receive time: an event the server received before the user was first seen
  does not count (drop the bound and it does); events received after the
  holdout ended, and held-out users assigned after it, change nothing in an
  ended holdout's response (drop either upper bound and it changes).
* C8 exclusion: a user assigned 1 s before their own ``first_seen_at`` is in
  neither group; a user assigned through ``assign_user`` after it is in theirs
  (compare with ``activated_at`` and the first stays in).
* C7: an analysed held-out user assigned inside the window is counted and
  named in the notice; an assignment after ``window_end`` is not, and an
  excluded held-out user is not (count every held-out user and they are).
* Untagged events count like tagged ones.
* The reasons, the minimum of 100 per group, access for all four roles, the
  route order against ``/check/{user_id}``, the notice copy, 404 and 422s.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, update

from backend.app.core.analysis_status import ANALYSIS_STATUS
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant
from backend.app.models.global_holdout import LEGACY_HOLDOUT_SALT, GlobalHoldout
from backend.app.models.holdout_population import HoldoutPopulation
from backend.app.models.user import User, UserRole
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.event_matching import EXPOSURE_EVENT_TYPES
from backend.app.services.global_holdout_service import (
    GlobalHoldoutService,
    holdout_bucket,
)
from backend.app.services.holdout_results import holdout_results
from backend.tests.integration.conftest import HASHED_PASSWORD, make_client_for_user

pytestmark = [pytest.mark.integration]

UTC = timezone.utc
METRIC = "purchase"


def naive(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(tzinfo=None)


class World:
    """Holdouts, population rows, events and assignments for one test.

    Everything it writes is keyed by a per-test user prefix or holdout id and
    removed afterwards.
    """

    def __init__(self, db, owner):
        self.db = db
        self.owner = owner
        self.prefix = f"hr-{uuid.uuid4().hex[:10]}-"
        self.holdout_ids: list = []
        self.experiment_ids: list = []
        self._experiments: list = []

    def user(self, label: str) -> str:
        return f"{self.prefix}{label}"

    def holdout(
        self,
        *,
        activated_at=None,
        deactivated_at=None,
        salt=None,
        percentage=5,
    ) -> GlobalHoldout:
        holdout = GlobalHoldout(
            name=f"results-{uuid.uuid4().hex[:10]}",
            holdout_percentage=percentage,
            is_active=False,
            activated_at=naive(activated_at) if activated_at else None,
            deactivated_at=naive(deactivated_at) if deactivated_at else None,
        )
        if salt is not None:
            holdout.hash_salt = salt
        self.db.add(holdout)
        self.db.commit()
        self.holdout_ids.append(holdout.id)
        return holdout

    def members(self, holdout, rows) -> None:
        """``rows``: ``(user_id, in_holdout, first_seen)`` with an aware time."""
        self.db.execute(
            insert(HoldoutPopulation),
            [
                {
                    "holdout_id": holdout.id,
                    "user_id": user_id,
                    "in_holdout": in_holdout,
                    "first_seen_at": first_seen,
                }
                for user_id, in_holdout, first_seen in rows
            ],
        )
        self.db.commit()

    def event(self, user_id, created, received=None, *, name=METRIC, experiment=None):
        """An event whose own time is ``created`` (aware) and receive time is
        ``received`` (aware; ``created`` when left out)."""
        event = Event(
            event_type=name,
            event_name=name,
            user_id=user_id,
            created_at=created,
            updated_at=naive(received or created),
            experiment_id=experiment.id if experiment is not None else None,
        )
        self.db.add(event)
        self.db.commit()
        return event

    def experiment(self, index: int = 0) -> Experiment:
        while len(self._experiments) <= index:
            suffix = uuid.uuid4().hex[:8]
            experiment = Experiment(
                name=f"Holdout results {suffix}",
                key=f"holdout-results-{suffix}",
                status=ExperimentStatus.ACTIVE,
                owner_id=self.owner.id,
            )
            self.db.add(experiment)
            self.db.flush()
            for name, is_control in (("control", True), ("treatment", False)):
                self.db.add(
                    Variant(
                        experiment_id=experiment.id,
                        name=name,
                        is_control=is_control,
                        traffic_allocation=50,
                    )
                )
            self.db.commit()
            self.db.refresh(experiment)
            self._experiments.append(experiment)
            self.experiment_ids.append(experiment.id)
        return self._experiments[index]

    def assignment(self, user_id, created, *, experiment_index=0) -> Assignment:
        experiment = self.experiment(experiment_index)
        variant = next(v for v in experiment.variants if v.is_control)
        assignment = Assignment(
            user_id=user_id,
            experiment_id=experiment.id,
            variant_id=variant.id,
            created_at=naive(created),
            updated_at=naive(created),
        )
        self.db.add(assignment)
        self.db.commit()
        return assignment

    def cleanup(self) -> None:
        db = self.db
        db.rollback()
        db.query(Event).filter(Event.user_id.startswith(self.prefix)).delete(
            synchronize_session=False
        )
        db.query(Assignment).filter(Assignment.user_id.startswith(self.prefix)).delete(
            synchronize_session=False
        )
        db.query(Event).filter(Event.experiment_id.in_(self.experiment_ids)).delete(
            synchronize_session=False
        )
        db.query(Assignment).filter(
            Assignment.experiment_id.in_(self.experiment_ids)
        ).delete(synchronize_session=False)
        db.query(Variant).filter(Variant.experiment_id.in_(self.experiment_ids)).delete(
            synchronize_session=False
        )
        db.query(Experiment).filter(Experiment.id.in_(self.experiment_ids)).delete(
            synchronize_session=False
        )
        # holdout_population rows go with their holdout (ON DELETE CASCADE).
        db.query(GlobalHoldout).filter(GlobalHoldout.id.in_(self.holdout_ids)).delete(
            synchronize_session=False
        )
        db.commit()


@pytest.fixture
def world(db_session, admin_user):
    w = World(db_session, admin_user)
    yield w
    w.cleanup()


#: An ended holdout's window: activated T0, deactivated END.
T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
END = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)
FIRST_SEEN = datetime(2026, 9, 2, 8, 30, 15, 123456, tzinfo=UTC)


def _results(client, holdout, metric=METRIC):
    resp = client.get(
        f"/api/v1/holdout/{holdout.id}/results", params={"metric": metric}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _computed(db, holdout, metric=METRIC):
    """The service's answer on the test session, before the minimum is applied
    to the counts: callers read ``groups``."""
    return holdout_results(db, holdout, metric)


def _group(body, name):
    return next(g for g in body["groups"] if g["group"] == name)


def _converted(world, holdout, user_id) -> bool:
    """Whether ``user_id`` (the only member of their arm) counts as converted."""
    body = _computed(world.db, holdout)
    groups = {g["group"]: g for g in body["groups"]}
    in_holdout = world.db.get(HoldoutPopulation, (holdout.id, user_id)).in_holdout
    group = groups["in_holdout" if in_holdout else "not_in_holdout"]
    assert group["users"] == 1, group
    return group["conversions"] == 1


# ---------------------------------------------------------------------------
# A13: the window's edges, and the receive-time lower bound
# ---------------------------------------------------------------------------


class TestWindow:
    @pytest.mark.regression
    def test_an_event_at_first_seen_counts(self, world):
        holdout = world.holdout(activated_at=T0, deactivated_at=END)
        user = world.user("exact")
        world.members(holdout, [(user, False, FIRST_SEEN)])
        world.event(user, FIRST_SEEN)

        assert _converted(world, holdout, user)

    def test_an_event_one_microsecond_before_first_seen_does_not(self, world):
        holdout = world.holdout(activated_at=T0, deactivated_at=END)
        user = world.user("early")
        world.members(holdout, [(user, False, FIRST_SEEN)])
        world.event(user, FIRST_SEEN - timedelta(microseconds=1), FIRST_SEEN)

        assert not _converted(world, holdout, user)

    def test_an_event_at_window_end_does_not(self, world):
        holdout = world.holdout(activated_at=T0, deactivated_at=END)
        user = world.user("end")
        world.members(holdout, [(user, True, FIRST_SEEN)])
        world.event(user, END, END - timedelta(seconds=1))

        assert not _converted(world, holdout, user)

    def test_an_event_received_before_first_seen_does_not_count(self, world):
        """Its own time is in the window (a fast device clock), but the server
        received it before the user was first seen."""
        holdout = world.holdout(activated_at=T0, deactivated_at=END)
        user = world.user("futuredated")
        world.members(holdout, [(user, False, FIRST_SEEN)])
        world.event(
            user, FIRST_SEEN + timedelta(hours=2), FIRST_SEEN - timedelta(minutes=5)
        )

        assert not _converted(world, holdout, user)

    def test_a_population_row_written_at_window_end_is_left_out(self, world):
        holdout = world.holdout(activated_at=T0, deactivated_at=END)
        world.members(
            holdout,
            [(world.user("in"), False, FIRST_SEEN), (world.user("late"), False, END)],
        )

        assert _group(_computed(world.db, holdout), "not_in_holdout")["users"] == 1

    def test_another_event_name_or_a_view_record_does_not_count(self, world):
        holdout = world.holdout(activated_at=T0, deactivated_at=END)
        user = world.user("other")
        world.members(holdout, [(user, False, FIRST_SEEN)])
        world.event(user, FIRST_SEEN + timedelta(hours=1), name="signup")
        view = Event(
            event_type=EXPOSURE_EVENT_TYPES[0],
            event_name=METRIC,
            user_id=user,
            created_at=FIRST_SEEN + timedelta(hours=1),
            updated_at=naive(FIRST_SEEN + timedelta(hours=1)),
        )
        world.db.add(view)
        world.db.commit()

        assert not _converted(world, holdout, user)


class TestUntagged:
    @pytest.mark.regression
    def test_an_untagged_event_counts_like_a_tagged_one(self, world):
        holdout = world.holdout(activated_at=T0, deactivated_at=END)
        tagged, untagged = world.user("tagged"), world.user("untagged")
        world.members(
            holdout, [(tagged, False, FIRST_SEEN), (untagged, False, FIRST_SEEN)]
        )
        later = FIRST_SEEN + timedelta(hours=3)
        event = world.event(tagged, later, experiment=world.experiment())
        world.event(untagged, later)

        before = _group(_computed(world.db, holdout), "not_in_holdout")
        world.db.execute(
            update(Event).where(Event.id == event.id).values(experiment_id=None)
        )
        world.db.commit()
        after = _group(_computed(world.db, holdout), "not_in_holdout")

        assert before["conversions"] == after["conversions"] == 2


# ---------------------------------------------------------------------------
# An ended holdout: events received after it ended change nothing
# ---------------------------------------------------------------------------


class TestEnded:
    @pytest.mark.regression
    def test_late_events_and_late_assignments_change_nothing(self, world, admin_client):
        holdout = world.holdout(activated_at=T0, deactivated_at=END)
        held = [world.user(f"h{i}") for i in range(100)]
        rest = [world.user(f"r{i}") for i in range(100)]
        world.members(
            holdout,
            [(u, True, FIRST_SEEN) for u in held]
            + [(u, False, FIRST_SEEN) for u in rest],
        )
        for u in held[:3] + rest[:9]:
            world.event(u, FIRST_SEEN + timedelta(days=1))
        before = admin_client.get(
            f"/api/v1/holdout/{holdout.id}/results", params={"metric": METRIC}
        )
        assert before.status_code == 200, before.text
        assert before.json()["difference"] is not None

        # Its own time is inside the window; the server got it after the end.
        world.event(held[50], END - timedelta(hours=1), END + timedelta(minutes=10))
        world.event(rest[50], END - timedelta(days=1), END + timedelta(days=2))
        # A held-out user legitimately assigned after the holdout ended.
        world.assignment(held[60], END + timedelta(hours=1))

        after = admin_client.get(
            f"/api/v1/holdout/{holdout.id}/results", params={"metric": METRIC}
        )
        assert after.content == before.content


# ---------------------------------------------------------------------------
# C8: assigned before first seen -> in neither group
# ---------------------------------------------------------------------------


@pytest.fixture
def active_measurable_holdout(db_session):
    """An active holdout activated through the service; any other active one
    is parked with a plain UPDATE and restored afterwards."""
    parked = [
        r.id for r in db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active)
    ]
    db_session.execute(
        update(GlobalHoldout)
        .where(GlobalHoldout.id.in_(parked))
        .values(is_active=False)
    )
    db_session.commit()
    holdout = GlobalHoldoutService(db_session).create_holdout(
        name=f"results-active-{uuid.uuid4().hex[:8]}",
        holdout_percentage=20,
        is_active=True,
    )
    assert holdout.is_measurable
    yield holdout
    db_session.rollback()
    db_session.query(GlobalHoldout).filter(GlobalHoldout.id == holdout.id).delete()
    db_session.execute(
        update(GlobalHoldout).where(GlobalHoldout.id.in_(parked)).values(is_active=True)
    )
    db_session.commit()


class TestExclusion:
    @pytest.mark.regression
    def test_assigned_one_second_before_first_seen_is_in_neither_group(self, world):
        holdout = world.holdout(activated_at=T0, deactivated_at=END)
        user = world.user("pre")
        world.members(holdout, [(user, True, FIRST_SEEN)])
        # After activation, before this user's own first-seen time.
        world.assignment(user, FIRST_SEEN - timedelta(seconds=1))

        body = _computed(world.db, holdout)

        assert _group(body, "in_holdout")["users"] == 0
        assert _group(body, "in_holdout")["excluded_users"] == 1
        assert _group(body, "not_in_holdout")["excluded_users"] == 0
        assert body["excluded_users"] == 1

    @pytest.mark.regression
    def test_assigned_by_assign_user_after_first_seen_is_in_the_group(
        self, world, active_measurable_holdout
    ):
        """The real write order: ``assign_user`` records the population row,
        then flushes the assignment, so ``first_seen_at`` comes first."""
        holdout = active_measurable_holdout
        experiment = world.experiment()
        user = None
        while user is None:
            candidate = world.user(uuid.uuid4().hex[:6])
            if (
                holdout_bucket(candidate, holdout.hash_salt)
                >= holdout.holdout_percentage
            ):
                user = candidate
        answer = AssignmentService(world.db).assign_user(user, experiment.id)
        assert answer["assigned"] is True

        body = holdout_results(world.db, holdout, METRIC)

        assert _group(body, "not_in_holdout")["users"] == 1
        assert body["excluded_users"] == 0


# ---------------------------------------------------------------------------
# C7: held-out users assigned while the holdout was active
# ---------------------------------------------------------------------------


class TestAssignedWhileHeldOut:
    @pytest.mark.regression
    def test_an_assignment_inside_the_window_is_counted_and_named(
        self, world, admin_client
    ):
        holdout = world.holdout(activated_at=T0, deactivated_at=END)
        user = world.user("contaminated")
        world.members(holdout, [(user, True, FIRST_SEEN)])
        world.assignment(user, FIRST_SEEN + timedelta(days=1))

        body = _results(admin_client, holdout)

        assert body["holdout_users_with_assignments"] == 1
        assert body["analysis_notice"].startswith(ANALYSIS_STATUS["holdout"].notice)
        assert (
            "1 user in the holdout was assigned to an experiment while it was active"
            in body["analysis_notice"]
        )
        # It stays in its group (intent to treat).
        assert _group(body, "in_holdout")["users"] == 1

    @pytest.mark.regression
    def test_an_assignment_after_window_end_is_not(self, world, admin_client):
        holdout = world.holdout(activated_at=T0, deactivated_at=END)
        user = world.user("after")
        world.members(holdout, [(user, True, FIRST_SEEN)])
        world.assignment(user, END + timedelta(seconds=1))

        body = _results(admin_client, holdout)

        assert body["holdout_users_with_assignments"] == 0
        assert body["analysis_notice"] == ANALYSIS_STATUS["holdout"].notice

    @pytest.mark.regression
    def test_an_excluded_held_out_user_is_not_counted(self, world):
        holdout = world.holdout(activated_at=T0, deactivated_at=END)
        user = world.user("excluded")
        world.members(holdout, [(user, True, FIRST_SEEN)])
        world.assignment(user, FIRST_SEEN - timedelta(hours=1), experiment_index=0)
        world.assignment(user, FIRST_SEEN + timedelta(hours=1), experiment_index=1)

        body = _computed(world.db, holdout)

        assert body["excluded_users"] == 1
        assert body["holdout_users_with_assignments"] == 0


# ---------------------------------------------------------------------------
# Reasons and the minimum
# ---------------------------------------------------------------------------


def _populate(world, holdout, n_in, n_out, conv_in=0, conv_out=0):
    held = [world.user(f"h{i}") for i in range(n_in)]
    rest = [world.user(f"r{i}") for i in range(n_out)]
    first_seen = (naive(datetime.now(UTC)) - timedelta(days=2)).replace(tzinfo=UTC)
    world.members(
        holdout,
        [(u, True, first_seen) for u in held] + [(u, False, first_seen) for u in rest],
    )
    for u in held[:conv_in] + rest[:conv_out]:
        world.event(u, first_seen + timedelta(hours=1))
    return held, rest


def _recent():
    return datetime.now(UTC) - timedelta(days=5)


class TestReasons:
    @pytest.mark.regression
    def test_a_legacy_holdout_is_never_computed(self, world, admin_client):
        holdout = world.holdout(activated_at=_recent(), salt=LEGACY_HOLDOUT_SALT)
        _populate(world, holdout, 100, 100, 5, 5)

        body = _results(admin_client, holdout)

        assert body["unavailable_reason"] == "activated_before_measurement"
        assert body["groups"] == [] and body["difference"] is None
        assert body["excluded_users"] is None
        assert body["holdout_users_with_assignments"] is None
        assert body["window_end"] is None
        assert body["message"]

    def test_the_row_the_upgrade_ended_is_activated_before_measurement(
        self, world, admin_client
    ):
        holdout = world.holdout(deactivated_at=_recent())

        body = _results(admin_client, holdout)

        assert body["unavailable_reason"] == "activated_before_measurement"
        assert body["groups"] == []

    def test_a_holdout_not_activated(self, world, admin_client):
        holdout = world.holdout()

        body = _results(admin_client, holdout)

        assert body["unavailable_reason"] == "not_activated"
        assert body["groups"] == [] and body["difference"] is None
        assert body["message"] == (
            "This holdout has not been activated since its users began to be "
            "recorded. Activate it to start measuring."
        )

    @pytest.mark.regression
    def test_99_users_in_a_group_is_too_few(self, world, admin_client):
        holdout = world.holdout(activated_at=_recent())
        _populate(world, holdout, 99, 300, 2, 10)

        body = _results(admin_client, holdout)

        assert body["unavailable_reason"] == "too_few_users"
        assert body["difference"] is None
        assert [g["users"] for g in body["groups"]] == [99, 300]
        assert [g["group"] for g in body["groups"]] == ["in_holdout", "not_in_holdout"]
        assert body["message"].startswith(
            "Not enough users yet: 99 in the holdout and 300 not in it. "
            "Results appear when each group has at least 100."
        )

    @pytest.mark.regression
    def test_100_users_in_each_group_is_enough(self, world, admin_client):
        holdout = world.holdout(activated_at=_recent())
        _populate(world, holdout, 100, 100, 2, 10)

        body = _results(admin_client, holdout)

        assert body["unavailable_reason"] is None and body["message"] is None
        difference = body["difference"]
        assert difference is not None
        assert difference["absolute"] == pytest.approx(0.08)
        assert _group(body, "in_holdout")["conversion_rate"] == pytest.approx(0.02)
        assert body["excluded_users"] == 0
        assert body["window_end"] is not None

    def test_no_events_for_the_metric(self, world, admin_client):
        holdout = world.holdout(activated_at=_recent())
        _populate(world, holdout, 100, 100, 3, 3)

        body = _results(admin_client, holdout, metric="purchse")

        assert body["unavailable_reason"] == "no_events"
        assert body["difference"] is None
        assert body["message"] == (
            "No 'purchse' events were recorded for either group in this "
            "window. Check the event name."
        )
        assert [g["conversions"] for g in body["groups"]] == [0, 0]


# ---------------------------------------------------------------------------
# Access, route order, copy, 404 and 422
# ---------------------------------------------------------------------------


def _role_user(db, role):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"hr_{role.value}_{suffix}",
        email=f"hr_{role.value}_{suffix}@int.test",
        hashed_password=HASHED_PASSWORD,
        is_active=True,
        is_superuser=False,
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


class TestAccess:
    @pytest.mark.regression
    @pytest.mark.parametrize(
        "role",
        [UserRole.ADMIN, UserRole.DEVELOPER, UserRole.ANALYST, UserRole.VIEWER],
    )
    def test_every_role_can_read(self, world, db_session, role):
        holdout = world.holdout()
        client = make_client_for_user(db_session, _role_user(db_session, role))

        resp = client.get(
            f"/api/v1/holdout/{holdout.id}/results", params={"metric": METRIC}
        )

        assert resp.status_code == 200, resp.text

    def test_no_token_is_401(self, world, db_session):
        from fastapi.testclient import TestClient

        from backend.app.main import app

        holdout = world.holdout()
        app.dependency_overrides.clear()
        resp = TestClient(app).get(
            f"/api/v1/holdout/{holdout.id}/results", params={"metric": METRIC}
        )
        assert resp.status_code == 401, resp.text


class TestRequest:
    @pytest.mark.regression
    def test_check_still_answers_for_a_user_called_results(self, developer_client):
        resp = developer_client.get("/api/v1/holdout/check/results")

        assert resp.status_code == 200, resp.text
        assert resp.json()["user_id"] == "results"

    def test_an_unknown_holdout_is_404(self, admin_client):
        missing = uuid.uuid4()
        resp = admin_client.get(
            f"/api/v1/holdout/{missing}/results", params={"metric": METRIC}
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == f"Holdout {missing} not found"

    @pytest.mark.parametrize(
        "path, params",
        [
            ("/api/v1/holdout/{id}/results", {}),
            ("/api/v1/holdout/{id}/results", {"metric": ""}),
            ("/api/v1/holdout/{id}/results", {"metric": "x" * 256}),
            ("/api/v1/holdout/not-a-uuid/results", {"metric": METRIC}),
        ],
    )
    def test_422(self, world, admin_client, path, params):
        holdout = world.holdout()
        resp = admin_client.get(path.format(id=holdout.id), params=params)
        assert resp.status_code == 422, resp.text

    @pytest.mark.regression
    def test_the_notice_names_what_the_holdout_does_not_cover(
        self, world, admin_client
    ):
        holdout = world.holdout()

        body = _results(admin_client, holdout)

        notice = body["analysis_notice"]
        assert body["analysis_status"] == "beta"
        assert notice == ANALYSIS_STATUS["holdout"].notice
        lowered = notice.lower()
        assert "feature flags and split-url experiments ignore the holdout" in lowered
        assert "first seen while the holdout was active" in lowered
        assert "stay valid however often you check" in lowered


# ---------------------------------------------------------------------------
# W-LA: every naive comparison holds under a non-UTC process and session
# ---------------------------------------------------------------------------

LA = "America/Los_Angeles"


@pytest.fixture
def la_process():
    """The process in Los Angeles time: ``TZ`` alone changes nothing until
    ``time.tzset()``."""
    import os
    import time

    saved = os.environ.get("TZ")
    os.environ["TZ"] = LA
    time.tzset()
    yield
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


@pytest.fixture
def la_session(db_session, la_process):
    """A session on a connection whose PostgreSQL TimeZone is Los Angeles.

    libpq does not read ``TZ``, so the session's zone is set on the
    connection itself.
    """
    from sqlalchemy.orm import Session

    connection = db_session.get_bind().connect()
    connection.exec_driver_sql(f"SET TIME ZONE '{LA}'")
    connection.commit()
    session = Session(bind=connection)
    yield session
    session.close()
    connection.close()


class TestNonUtcTimeZone:
    @pytest.mark.regression
    def test_the_four_naive_comparisons_hold_in_los_angeles(self, world, la_session):
        from zoneinfo import ZoneInfo

        from sqlalchemy import func, select

        # The process and the query's own session are both in LA time.
        utc_now = datetime.now(UTC)
        local_minus_utc = datetime.now() - utc_now.replace(tzinfo=None)
        # LA's offset at this instant. Passing the naive UTC wall time to
        # utcoffset() reads it as LA local time, which picks the wrong offset
        # for seven hours after each US clock change (02:00-09:00 UTC on the
        # first Sunday of November, 03:00-10:00 UTC on the second of March).
        expected = utc_now.astimezone(ZoneInfo(LA)).utcoffset()
        assert round(local_minus_utc.total_seconds() / 60) == round(
            expected.total_seconds() / 60
        )
        assert expected.total_seconds() < 0
        assert (
            la_session.execute(select(func.current_setting("TimeZone"))).scalar() == LA
        )

        holdout = world.holdout(activated_at=T0, deactivated_at=END)
        late_receipt, early_receipt = world.user("late-receipt"), world.user("early")
        pre_assigned, assigned = world.user("pre-assigned"), world.user("assigned")
        world.members(
            holdout,
            [
                (late_receipt, False, FIRST_SEEN),
                (early_receipt, False, FIRST_SEEN),
                (pre_assigned, True, FIRST_SEEN),
                (assigned, True, FIRST_SEEN),
            ],
        )
        hour = timedelta(hours=1)
        # 1. received 1 h before window_end: converts
        world.event(late_receipt, END - hour, END - hour)
        # 2. received 1 h before first seen (own time inside): does not
        world.event(early_receipt, FIRST_SEEN + 2 * hour, FIRST_SEEN - hour)
        # 3. assigned 1 h before first seen: excluded
        world.assignment(pre_assigned, FIRST_SEEN - hour)
        # 4. held out, assigned 1 h before window_end: counted
        world.assignment(assigned, END - hour)

        body = holdout_results(la_session, holdout, METRIC)

        rest, held = _group(body, "not_in_holdout"), _group(body, "in_holdout")
        observed = {
            # 1 converts and 2 does not
            "rest_users_conversions": (rest["users"], rest["conversions"]),
            # 3 is excluded, 4 is analysed
            "held_users_excluded": (held["users"], held["excluded_users"]),
            # 4 is counted
            "holdout_users_with_assignments": body["holdout_users_with_assignments"],
        }
        assert observed == {
            "rest_users_conversions": (2, 1),
            "held_users_excluded": (1, 1),
            "holdout_users_with_assignments": 1,
        }


class TestStatementTimeout:
    @pytest.mark.regression
    def test_the_counting_statement_runs_under_a_local_statement_timeout(
        self, world, db_session
    ):
        """``set_config('statement_timeout', ..., true)`` (SET LOCAL) runs in
        the same transaction, just before the counting statement, and the
        setting is in force when it runs."""
        from sqlalchemy import event, text

        from backend.app.services import holdout_results as module

        holdout = world.holdout(activated_at=T0, deactivated_at=END)
        world.members(holdout, [(world.user("u"), False, FIRST_SEEN)])
        seen = []

        def record(conn, cursor, statement, parameters, context, executemany):
            seen.append(statement)

        bind = db_session.get_bind()
        event.listen(bind, "before_cursor_execute", record)
        try:
            holdout_results(db_session, holdout, METRIC)
            in_force = db_session.execute(
                text("SELECT setting FROM pg_settings WHERE name = 'statement_timeout'")
            ).scalar()
        finally:
            event.remove(bind, "before_cursor_execute", record)
            db_session.rollback()

        timeout_at = next(i for i, s in enumerate(seen) if "set_config" in s)
        count_at = next(i for i, s in enumerate(seen) if "WITH pop AS" in s)
        assert timeout_at == count_at - 1
        assert in_force == str(module.STATEMENT_TIMEOUT_MS)
