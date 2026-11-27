"""A metric's conversion window through the experiments API (#1934).

``conversion_window_hours`` is sent on create and on a draft's ``PUT``, and
answered on create, ``PUT``, ``GET``, the list and the clone; the audit entry
of a ``PUT`` that changes it names ``metrics``. Anything but a whole number of
hours from 1 to 8760, or null, answers 422 with Pydantic's own text at
``["body", "metrics", i, "conversion_window_hours"]``, and the table's check
refuses a stored 0. A ``PUT`` that sends the metrics without it stores none.
Once the experiment has left draft no role, a superuser included, changes it.

Two metrics on one event, one with a 24-hour window and one without, created
through the API on ``metric_window_world``'s first twenty rows: every results
view answers 9 converting users for the first and 16 for the second.

The example of ``docs/guides/how-events-count.md``'s "Conversion window": a
1-hour window, a user assigned through ``/tracking/assign``, a purchase
stamped two hours after the assignment counts 0, and one stamped now counts 1.
"""

from __future__ import annotations

import fnmatch
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints import results as results_module
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.core.database_config import get_schema_name
from backend.app.main import app
from backend.app.models.analysis_snapshot import AnalysisSnapshot
from backend.app.models.assignment import Assignment
from backend.app.models.audit_log import AuditLog
from backend.app.models.event import Event
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    Metric,
    Variant,
)
from backend.app.models.user import User, UserRole
from backend.app.services.analysis_service import AnalysisService
from backend.tests.integration import metric_window_world as mw
from backend.tests.integration.api.test_converting_users_consistency import (
    _bandit_counts,
    _bayesian_counts,
    _breakdown_conversions,
    _cumulative_counts,
    _results_counts,
    _segmented_conversions,
    _sequential_inputs,
    _streaming_counts,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

BASE = "/api/v1/experiments"
FIELD = "conversion_window_hours"

TWO_VARIANTS = [
    {"name": "control", "is_control": True, "traffic_allocation": 50},
    {"name": "treatment", "is_control": False, "traffic_allocation": 50},
]

#: (value sent, the 422's entry at the second metric's field)
REFUSED = [
    (
        0,
        {
            "type": "greater_than_equal",
            "msg": "Input should be greater than or equal to 1",
            "ctx": {"ge": 1},
        },
    ),
    (
        -1,
        {
            "type": "greater_than_equal",
            "msg": "Input should be greater than or equal to 1",
            "ctx": {"ge": 1},
        },
    ),
    (
        8761,
        {
            "type": "less_than_equal",
            "msg": "Input should be less than or equal to 8760",
            "ctx": {"le": 8760},
        },
    ),
    (1.5, {"type": "int_type", "msg": "Input should be a valid integer"}),
    (True, {"type": "int_type", "msg": "Input should be a valid integer"}),
    ("24", {"type": "int_type", "msg": "Input should be a valid integer"}),
    ("168", {"type": "int_type", "msg": "Input should be a valid integer"}),
]

#: The twenty rows with a 24-hour window: 9 converting users; without: 16.
IN_A_DAY = {"control": {"n": 10, "converted": 5}, "treatment": {"n": 9, "converted": 4}}
EVER = {"control": {"n": 10, "converted": 8}, "treatment": {"n": 9, "converted": 8}}


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture
def made(db_session) -> Iterator[List]:
    """The ids of the experiments a test makes, removed afterwards."""
    ids: List = []
    yield ids
    db_session.rollback()
    found = list(ids)
    for model in (AnalysisSnapshot, Event, Assignment, Metric, Variant):
        db_session.query(model).filter(model.experiment_id.in_(found)).delete(
            synchronize_session=False
        )
    db_session.query(AuditLog).filter(AuditLog.entity_id.in_(found)).delete(
        synchronize_session=False
    )
    db_session.query(Experiment).filter(Experiment.id.in_(found)).delete(
        synchronize_session=False
    )
    db_session.commit()


def _metric(name: str, hours: Any = None, **fields) -> Dict[str, Any]:
    metric = {
        "name": name,
        "event_name": "purchase",
        "metric_type": "conversion",
        **fields,
    }
    if hours is not None:
        metric[FIELD] = hours
    return metric


def _body(*metrics: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": f"window {uuid.uuid4().hex[:8]}",
        "experiment_type": "a_b",
        "variants": TWO_VARIANTS,
        "metrics": list(metrics),
    }


def _windows(answer: Dict[str, Any]) -> Dict[str, Optional[int]]:
    return {m["name"]: m[FIELD] for m in answer["metrics"]}


def _stored(db_session, experiment_id) -> Dict[str, Optional[int]]:
    db_session.expire_all()
    rows = db_session.query(Metric).filter(
        Metric.experiment_id == uuid.UUID(str(experiment_id))
    )
    return {m.name: m.conversion_window_hours for m in rows}


def _create(client, made, *metrics) -> Dict[str, Any]:
    response = client.post(f"{BASE}/", json=_body(*metrics))
    assert response.status_code == 201, response.text
    made.append(uuid.UUID(response.json()["id"]))
    return response.json()


# --- what is refused and what is taken ------------------------------------------


@pytest.mark.parametrize("value, entry", REFUSED, ids=[repr(r[0]) for r in REFUSED])
def test_create_and_put_refuse_anything_but_1_to_8760_hours(
    admin_client, db_session, made, value, entry
):
    first = _metric("Signup", is_primary=True)
    created = _create(admin_client, made, first)
    expected = {
        "detail": [{**entry, "loc": ["body", "metrics", 1, FIELD]}]
    }  # Pydantic's own text, without the input

    refused = admin_client.post(
        f"{BASE}/", json=_body(first, _metric("Purchase", hours=value))
    )
    assert (refused.status_code, refused.json()) == (422, expected)

    put = admin_client.put(
        f"{BASE}/{created['id']}",
        json={"metrics": [first, _metric("Purchase", hours=value)]},
    )
    assert (put.status_code, put.json()) == (422, expected)
    assert _stored(db_session, created["id"]) == {"Signup": None}


@pytest.mark.parametrize("hours", [1, 8760])
def test_create_and_put_take_the_bounds(admin_client, db_session, made, hours):
    created = _create(
        admin_client, made, _metric("Purchase", hours=hours, is_primary=True)
    )
    assert _windows(created) == {"Purchase": hours}
    put = admin_client.put(
        f"{BASE}/{created['id']}",
        json={"metrics": [_metric("Purchase", hours=9 if hours == 1 else hours - 1)]},
    )
    assert put.status_code == 200, put.text
    assert _stored(db_session, created["id"]) == {
        "Purchase": 9 if hours == 1 else hours - 1
    }


def test_the_table_refuses_a_stored_0(admin_client, db_session, made):
    created = _create(admin_client, made, _metric("Purchase", hours=24))
    with pytest.raises(IntegrityError, match="check_metric_conversion_window"):
        db_session.execute(
            text(
                f"UPDATE {get_schema_name()}.metrics SET {FIELD} = 0 "
                "WHERE experiment_id = :id"
            ),
            {"id": created["id"]},
        )
    db_session.rollback()
    assert _stored(db_session, created["id"]) == {"Purchase": 24}


# --- the round trip ----------------------------------------------------------------


def test_create_put_get_list_clone_and_audit_carry_the_window(
    admin_client, db_session, made
):
    created = _create(
        admin_client,
        made,
        _metric("Purchase in a week", hours=168, is_primary=True),
        _metric("Purchase ever"),
    )
    experiment_id = created["id"]
    expected = {"Purchase in a week": 168, "Purchase ever": None}
    assert _windows(created) == expected
    assert _stored(db_session, experiment_id) == expected

    read = admin_client.get(f"{BASE}/{experiment_id}")
    assert read.status_code == 200, read.text
    assert _windows(read.json()) == expected
    listed = admin_client.get(f"{BASE}/", params={"search": created["name"]})
    assert listed.status_code == 200, listed.text
    (item,) = [i for i in listed.json()["items"] if i["id"] == experiment_id]
    assert _windows(item) == expected

    put = admin_client.put(
        f"{BASE}/{experiment_id}",
        json={
            "metrics": [
                _metric("Purchase in a week", hours=24, is_primary=True),
                _metric("Purchase ever"),
            ]
        },
    )
    assert put.status_code == 200, put.text
    changed = {"Purchase in a week": 24, "Purchase ever": None}
    assert _windows(put.json()) == changed
    assert _stored(db_session, experiment_id) == changed

    db_session.expire_all()
    (entry,) = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.entity_id == uuid.UUID(experiment_id),
            AuditLog.action_type == "experiment_update",
        )
        .all()
    )
    assert json.loads(entry.new_value) == {"changed_fields": ["metrics"]}

    clone = admin_client.post(f"{BASE}/{experiment_id}/clone")
    assert clone.status_code in (200, 201), clone.text
    made.append(uuid.UUID(clone.json()["id"]))
    assert _windows(clone.json()) == changed
    assert _stored(db_session, clone.json()["id"]) == changed


@pytest.mark.regression
def test_a_changed_window_is_not_answered_from_the_cache(
    admin_client, db_session, made, monkeypatch
):
    """The results cache's key names the engine and the settings, not the
    metrics: a PUT that changes a draft's window drops the experiment's cached
    answers, so the next read is computed with the new window. Another
    experiment's cached answer stays."""
    store: Dict[str, str] = {}

    class Cache:
        """The route's own client (a read, a write)."""

        def get(self, key):
            return store.get(key)

        def set(self, key, value, expire=0):
            store[key] = value

    class AsyncRedis:
        """The client the update clears it with, on the event loop."""

        async def scan_iter(self, match):
            for key in [k for k in store if fnmatch.fnmatchcase(k, match)]:
                yield key

        async def delete(self, *keys):
            for key in keys:
                store.pop(key, None)

        async def aclose(self):
            pass

    monkeypatch.setattr(results_module, "_get_cache_service", lambda: Cache())
    monkeypatch.setattr(results_module, "_async_cache_client", lambda: AsyncRedis())
    created = _create(
        admin_client, made, _metric("Purchase", hours=24, is_primary=True)
    )
    other = _create(admin_client, made, _metric("Signup", hours=24, is_primary=True))

    def window(experiment_id) -> Optional[int]:
        response = admin_client.get(f"/api/v1/results/{experiment_id}")
        assert response.status_code == 200, response.text
        (metric,) = response.json()["metrics"]
        return metric[FIELD]

    assert (window(created["id"]), window(other["id"])) == (24, 24)
    assert sorted(k.split(":")[1] for k in store) == sorted(
        [created["id"], other["id"]]
    )

    put = admin_client.put(
        f"{BASE}/{created['id']}",
        json={"metrics": [_metric("Purchase", hours=168, is_primary=True)]},
    )
    assert put.status_code == 200, put.text
    assert [k.split(":")[1] for k in store] == [other["id"]]
    assert window(created["id"]) == 168


@pytest.mark.regression
def test_a_draft_put_without_the_window_stores_none(admin_client, db_session, made):
    """A PUT replaces the metrics, so a metric sent without the field loses
    its window, as one sent without lower_is_better is no longer lower-is-better."""
    created = _create(
        admin_client,
        made,
        _metric("Purchase", hours=168, is_primary=True, lower_is_better=True),
    )
    assert _stored(db_session, created["id"]) == {"Purchase": 168}

    put = admin_client.put(
        f"{BASE}/{created['id']}",
        json={"metrics": [_metric("Purchase", is_primary=True)]},
    )
    assert put.status_code == 200, put.text
    assert _windows(put.json()) == {"Purchase": None}
    assert _stored(db_session, created["id"]) == {"Purchase": None}
    assert put.json()["metrics"][0]["lower_is_better"] is False


# --- fixed once the experiment leaves draft ------------------------------------------

#: role name -> (UserRole, is_superuser)
ROLES = {
    "developer": (UserRole.DEVELOPER, False),
    "admin": (UserRole.ADMIN, False),
    "analyst": (UserRole.ANALYST, False),
    "viewer": (UserRole.VIEWER, False),
    "superuser": (UserRole.ADMIN, True),
}


@pytest.fixture
def token_client(db_session) -> Iterator[TestClient]:
    """A client whose requests carry a real local token: only the database is
    overridden, so each role's own answer is the application's."""
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )

    def override_get_db():
        session = factory()
        session.execute(text("SET search_path TO test_experimentation"))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


@pytest.fixture
def users(db_session) -> Iterator[Dict[str, User]]:
    made: Dict[str, User] = {}
    for name, (role, superuser) in ROLES.items():
        suffix = uuid.uuid4().hex[:8]
        user = User(
            username=f"window_{name}_{suffix}",
            email=f"window_{name}_{suffix}@window.test",
            full_name=f"Window {name}",
            hashed_password="unused: this user signs in by token only",
            is_active=True,
            is_superuser=superuser,
            role=role,
        )
        db_session.add(user)
        made[name] = user
    db_session.commit()
    yield made
    db_session.rollback()
    ids = [u.id for u in made.values()]
    db_session.query(AuditLog).filter(AuditLog.user_id.in_(ids)).delete(
        synchronize_session=False
    )
    db_session.commit()


def _auth(user: User) -> Dict[str, str]:
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


@pytest.mark.parametrize("stop", ["start", "pause"])
def test_no_role_changes_the_window_once_the_experiment_left_draft(
    token_client, db_session, users, made, stop
):
    developer = users["developer"]
    response = token_client.post(
        f"{BASE}/",
        json=_body(_metric("Purchase", hours=168, is_primary=True)),
        headers=_auth(developer),
    )
    assert response.status_code == 201, response.text
    experiment_id = response.json()["id"]
    made.append(uuid.UUID(experiment_id))
    for step in ("start", "pause")[: 1 + (stop == "pause")]:
        moved = token_client.post(
            f"{BASE}/{experiment_id}/{step}", headers=_auth(developer)
        )
        assert moved.status_code == 200, moved.text
    state = "active" if stop == "start" else "paused"

    answers = {}
    for name, user in users.items():
        put = token_client.put(
            f"{BASE}/{experiment_id}",
            json={"metrics": [_metric("Purchase", hours=24, is_primary=True)]},
            headers=_auth(user),
        )
        answers[name] = put.status_code, put.json().get("detail")
    forbidden = answers["analyst"]
    assert forbidden[0] == 403 and answers["viewer"] == forbidden
    assert {k: v for k, v in answers.items() if k not in ("analyst", "viewer")} == {
        "developer": (400, f"Cannot update experiments in {state} status"),
        "admin": (400, f"Cannot update experiments in {state} status"),
        "superuser": (400, f"Cannot update metrics for experiments in {state} status"),
    }
    assert _stored(db_session, experiment_id) == {"Purchase": 168}


# --- two metrics on one event, created through the API ------------------------------


def _world(admin_client, db_session, made, metrics) -> mw.World:
    """An experiment created through the API with *metrics*, made active as
    ``metric_window_world.build`` makes its own, holding the twenty rows."""
    created = _create(admin_client, made, *metrics)
    experiment = db_session.get(Experiment, uuid.UUID(created["id"]))
    experiment.status = ExperimentStatus.ACTIVE
    experiment.start_date = mw.S - mw.DAY
    experiment.sequential_testing_enabled = True
    experiment.sequential_testing_config = {"method": "msprt"}
    experiment.bayesian_enabled = True
    experiment.bayesian_config = {"prior_family": "beta", "alpha": 1.0, "beta": 1.0}
    db_session.commit()
    variants = {v.name: v for v in experiment.variants}
    world = mw.World(experiment, variants["control"], variants["treatment"])
    return mw.add_rows(db_session, world, mw.TWENTY_ROWS)


def _get(client, path, **params) -> Dict[str, Any]:
    response = client.get(path, params={"use_cache": "false", **params})
    assert response.status_code == 200, response.text
    return response.json()


def _named(results, name) -> Dict[str, Dict[str, int]]:
    (metric,) = [m for m in results["metrics"] if m["metric_name"] == name]
    return {
        v["variant_name"]: {"n": v["sample_size"], "converted": v["conversions"]}
        for v in metric["variants"]
    }


def test_two_metrics_created_through_the_api_count_9_and_16_in_every_view(
    admin_client, db_session, made
):
    world = _world(
        admin_client,
        db_session,
        made,
        [
            _metric("Purchase in a day", hours=24, is_primary=True),
            _metric("Purchase ever"),
        ],
    )
    exp = world.experiment
    base = f"/api/v1/results/{exp.id}"

    # The views that list every metric: each its own window.
    results = _get(admin_client, base, breakdown="country")
    alias = _get(admin_client, f"/api/v1/experiments/{exp.id}/results")
    batched = AnalysisService(db_session).get_experiment_results_many([exp.id])
    for view in (results, alias, batched[str(exp.id)]):
        assert _named(view, "Purchase in a day") == IN_A_DAY
        assert _named(view, "Purchase ever") == EVER
    assert results["summary"]["total_conversions"] == 16

    # The views of the primary metric: its 24 hours.
    assert _results_counts(results) == IN_A_DAY
    assert _bayesian_counts(results["bayesian_results"]) == IN_A_DAY
    assert _breakdown_conversions(results) == IN_A_DAY
    assert _bayesian_counts(_get(admin_client, f"{base}/bayesian")) == IN_A_DAY
    assert _sequential_inputs(admin_client, exp) == IN_A_DAY
    # The daily series answers for the first metric it lists unless it is
    # asked for one, so each is asked for by id.
    ids = {m.name: str(m.id) for m in exp.metric_definitions}
    for name, expected in (("Purchase in a day", IN_A_DAY), ("Purchase ever", EVER)):
        daily = _get(admin_client, f"{base}/daily", metric_id=ids[name])
        assert _cumulative_counts(daily) == expected, name
    sample_size = _get(admin_client, f"{base}/sample-size")
    assert (sample_size["baseline_users"], sample_size["baseline_rate"]) == (
        10,
        pytest.approx(5 / 10),
    )
    cuped = {
        m["metric_name"]: m for m in _get(admin_client, f"{base}/cuped")["metrics"]
    }
    assert (
        cuped["Purchase in a day"]["adjusted_control_mean"],
        cuped["Purchase in a day"]["adjusted_treatment_mean"],
    ) == (pytest.approx(5 / 10), pytest.approx(4 / 9))
    assert _streaming_counts(db_session, exp) == IN_A_DAY
    assert _bandit_counts(db_session, exp) == IN_A_DAY

    # The breakdown of every metric.
    segmented = AnalysisService(db_session).get_segmented_results(
        experiment_id=exp.id, segment_by="country"
    )
    per_metric: Dict[str, int] = {}
    for segment in segmented["segments"]:
        for metric in segment["metrics"]:
            per_metric[metric["metric_name"]] = per_metric.get(
                metric["metric_name"], 0
            ) + sum(v["conversions"] for v in metric["variants"])
    assert per_metric == {"Purchase in a day": 9, "Purchase ever": 16}
    assert _segmented_conversions(db_session, exp) == {
        "control": {"n": 20, "converted": 13},
        "treatment": {"n": 18, "converted": 12},
    }

    # The export and the report: the primary metric's conversions.
    export = admin_client.get(
        "/api/v1/export/variants",
        params={
            "format": "json",
            "start_date": (exp.created_at - timedelta(seconds=1)).isoformat(),
        },
    )
    assert export.status_code == 200, export.text
    assert {
        r["variant_name"]: {"n": r["assignments"], "converted": r["conversions"]}
        for r in export.json()
        if r["experiment_id"] == str(exp.id)
    } == IN_A_DAY
    report = _get(admin_client, f"/api/v1/export/reports/experiments/{exp.id}")
    assert {
        r["variant_name"]: {"n": r["assignments"], "converted": r["conversions"]}
        for r in report["variants"]
    } == IN_A_DAY

    # What the API stored, read last so that a create that drops the field
    # fails on the numbers above first.
    assert _stored(db_session, exp.id) == {
        "Purchase in a day": 24,
        "Purchase ever": None,
    }


# --- what the pages say a window does ----------------------------------------------


def _late_converters() -> tuple:
    """Ten users in each arm: 3 in control buy an hour after their assignment,
    4 in treatment 30 hours after theirs."""
    rows = []
    for number in range(1, 21):
        arm = "control" if number <= 10 else "treatment"
        buys = (number in (1, 3, 5)) or (number in (11, 13, 15, 17))
        at = mw.HOUR if arm == "control" else 30 * mw.HOUR
        rows.append(
            mw.Row(
                f"u{number:02d}",
                arm,
                (mw.Sent(at),) if buys else (),
                buys and arm == "control",
                "buys an hour in" if arm == "control" else "buys 30 hours in",
            )
        )
    return tuple(rows)


def test_a_change_that_makes_people_convert_later_can_show_the_opposite_sign(
    admin_client, db_session, admin_user, made
):
    """With no window the treatment converts more users (4 of 10 against 3);
    with a 24-hour window, which its users' purchases come after, it converts
    none, and its difference from the control changes sign."""
    world = mw.build(
        db_session,
        admin_user.id,
        rows=_late_converters(),
        metrics=[
            mw.metric("Purchase in a day", is_primary=True),
            mw.metric("Purchase ever", hours=None),
        ],
    )
    made.append(world.experiment.id)
    results = _get(admin_client, f"/api/v1/results/{world.experiment.id}")
    ever = _named(results, "Purchase ever")
    day = _named(results, "Purchase in a day")
    assert (ever["control"]["converted"], ever["treatment"]["converted"]) == (3, 4)
    assert (day["control"]["converted"], day["treatment"]["converted"]) == (3, 0)

    def treatment_difference(name) -> float:
        (metric,) = [m for m in results["metrics"] if m["metric_name"] == name]
        (treatment,) = [
            v for v in metric["variants"] if v["variant_name"] == "treatment"
        ]
        return treatment["absolute_difference"]

    assert treatment_difference("Purchase ever") == pytest.approx(0.1)
    assert treatment_difference("Purchase in a day") == pytest.approx(-0.3)


def test_cuped_history_takes_no_window(admin_client, db_session, admin_user, made):
    """The outcome of a windowed metric is counted in its window; its history,
    the week before each assignment, is the same as a metric's with none: u04,
    u07 and u08 sent a purchase before their assignment, 3 of 19 users."""
    world = mw.build(
        db_session,
        admin_user.id,
        rows=mw.TWENTY_ROWS,
        metrics=[
            mw.metric("Purchase in a day", is_primary=True),
            mw.metric("Purchase ever", hours=None),
        ],
        variance_reduction_config={"method": "cuped", "covariate_lookback_days": 7},
    )
    made.append(world.experiment.id)
    body = _get(admin_client, f"/api/v1/results/{world.experiment.id}/cuped")
    cuped = {m["metric_name"]: m for m in body["metrics"]}
    assert cuped["Purchase in a day"]["covariate_coverage_pct"] == pytest.approx(
        300 / 19, abs=0.01
    )
    assert cuped["Purchase ever"]["covariate_coverage_pct"] == pytest.approx(
        300 / 19, abs=0.01
    )
    # The outcomes: 5 of 10 and 4 of 9 in the window, 8 of 10 and 8 of 9 without.
    assert cuped["Purchase in a day"]["unadjusted_effect"] == pytest.approx(
        4 / 9 - 5 / 10
    )
    assert cuped["Purchase ever"]["unadjusted_effect"] == pytest.approx(8 / 9 - 8 / 10)


# --- the example on the How events count page ---------------------------------------


def test_the_how_events_count_example_a_1_hour_window(admin_client, db_session, made):
    created = _create(
        admin_client,
        made,
        _metric("Purchase within an hour", hours=1, is_primary=True),
    )
    started = admin_client.post(f"{BASE}/{created['id']}/start")
    assert started.status_code == 200, started.text
    key = created["key"]

    assigned = admin_client.post(
        "/api/v1/tracking/assign", json={"experiment_key": key, "user_id": "ana"}
    )
    assert assigned.status_code == 200, assigned.text
    db_session.expire_all()
    (assignment,) = db_session.query(Assignment).filter(
        Assignment.experiment_id == uuid.UUID(created["id"])
    )
    at = assignment.created_at.replace(tzinfo=timezone.utc)

    def conversions() -> int:
        results = _get(admin_client, f"/api/v1/results/{created['id']}")
        (metric,) = results["metrics"]
        assert metric[FIELD] == 1
        return sum(v["conversions"] for v in metric["variants"])

    def purchase(**stamp) -> None:
        tracked = admin_client.post(
            "/api/v1/tracking/track",
            json={
                "event_type": "purchase",
                "user_id": "ana",
                "experiment_key": key,
                **stamp,
            },
        )
        assert tracked.status_code == 200, tracked.text

    purchase(timestamp=(at + timedelta(hours=2)).isoformat())
    assert conversions() == 0
    purchase()  # stamped when it is received: now, inside the hour
    assert datetime.now(timezone.utc) < at + timedelta(hours=1)
    assert conversions() == 1
