"""
A breakdown counts each user in one segment, so its rows add up to the totals
(#1776), on both breakdown routes:

* ``GET /api/v1/results/{id}?breakdown=<dimension>``
* ``GET /api/v1/experiments/{id}/segmented-results/<dimension>``

The rule (docs/api/dimensional-analysis.md): every assigned user is in exactly
one row. A user's segment is the value the user was first assigned with, or
else the first value tracked for them: the top-level key's text on the user's
first event of this experiment that carries a non-empty value, exposures
first, then in the order the server received the events (``updated_at``), then
by event id. A user with no such value is in ``unknown``. A user's conversions
count in their own row under the headline's definition, so per variant the
rows' ``sample_size`` and ``conversions`` add up to the headline's. A
breakdown lists the 20 values with the most users and counts the rest, and any
value longer than 255 characters, under ``other``. Each segment's p-value is
Fisher's exact test against the control. A breakdown that cannot be computed
is an error, never cached; a dimension containing a NUL character answers 400.

Every case checks exact rows, ``{segment: {arm: (users, converting users)}}``
with every arm listed, on both routes, and that they add up to the headline.
Rows that only add up could all be ``unknown``.

The experiments have FIXED ids: an arm chosen by ``/tracking/assign`` is a hash
of the experiment's id, and where a test assigns through the route it reads
the arms back from the assignment rows. Every event whose order matters sets
``updated_at`` itself. Rows are deleted in fixture teardown, because the shared
test database is not truncated between tests.
"""

import fnmatch
import importlib
import json
import logging
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from typing import Any, Dict, Iterable, List, Optional, Tuple
from unittest.mock import patch
from urllib.parse import quote

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import insert, text
from sqlalchemy import null as sa_null

from backend.app.api.sdk_scope import require_batch_assign_key
from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.main import app
from backend.app.models.analysis_snapshot import AnalysisSnapshot
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)
from backend.app.services.analysis_snapshot_service import snapshot_day
from backend.app.services.dimensional_analysis_service import (
    DimensionalAnalysisService,
)
from backend.app.services.event_matching import EXPOSURE_EVENT_TYPES

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

DIM = "platform"
PURCHASE = "Purchase"
SIGNUP = "Signup"
ARMS = ("control", "treatment")
#: Naive UTC, as ``events.updated_at`` stores it.
BASE = datetime(2026, 11, 1, 12, 0, 0)
USER_PREFIX = "bk1776-"
#: Stands for SQL NULL in ``event_metadata`` (Python ``None`` stores JSON null).
SQL_NULL = "SQL NULL"
NUL_TEXT = "The dimension must not contain a NUL character."
RESULTS_FAILURE = "Could not compute the experiment's results"
SEGMENTED_FAILURE = "Could not load the segmented results"
SEGMENT_MODULE = "backend.app.services.segment_breakdown"

Rows = Dict[str, Dict[str, Tuple[int, int]]]


def _ids(seq: int) -> Tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """The fixed experiment, control and treatment ids of experiment ``seq``."""
    return (
        uuid.UUID(f"17760000-0000-4000-8000-{seq:012d}"),
        uuid.UUID(f"17760000-0000-4000-8001-{seq:012d}"),
        uuid.UUID(f"17760000-0000-4000-8002-{seq:012d}"),
    )


def _delete_experiment(db, experiment_id: uuid.UUID) -> None:
    db.rollback()
    for model in (AnalysisSnapshot, Event, Assignment, Metric, Variant):
        db.query(model).filter(model.experiment_id == experiment_id).delete(
            synchronize_session=False
        )
    db.query(Experiment).filter(Experiment.id == experiment_id).delete(
        synchronize_session=False
    )
    db.commit()


@pytest.fixture
def make_breakdown_experiment(db_session, make_experiment):
    """``make(seq, metrics=...)``: an ACTIVE 50/50 experiment with fixed ids.

    ``metrics`` is ``[(name, event_name), ...]``; the first is the primary.
    """
    created: List[uuid.UUID] = []

    def make(seq: int = 1, metrics=((PURCHASE, "purchase"),)):
        experiment_id, control_id, treatment_id = _ids(seq)
        _delete_experiment(db_session, experiment_id)
        experiment = make_experiment(
            id=experiment_id,
            name=f"Breakdown one segment per user {seq}",
            key=f"bk1776-{seq}",
            status=ExperimentStatus.ACTIVE,
            experiment_type=ExperimentType.A_B,
            start_date=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        created.append(experiment_id)
        rows: List[Any] = [
            Variant(
                id=control_id,
                experiment_id=experiment_id,
                name="control",
                is_control=True,
                traffic_allocation=50,
            ),
            Variant(
                id=treatment_id,
                experiment_id=experiment_id,
                name="treatment",
                is_control=False,
                traffic_allocation=50,
            ),
        ]
        for index, (name, event_name) in enumerate(metrics):
            rows.append(
                Metric(
                    experiment_id=experiment_id,
                    name=name,
                    event_name=event_name,
                    metric_type=MetricType.CONVERSION,
                    is_primary=index == 0,
                )
            )
        db_session.add_all(rows)
        db_session.commit()
        db_session.refresh(experiment)
        return experiment

    yield make
    for experiment_id in created:
        _delete_experiment(db_session, experiment_id)
    db_session.query(Event).filter(Event.user_id.like(f"{USER_PREFIX}%")).delete(
        synchronize_session=False
    )
    db_session.commit()


@pytest.fixture
def exp(make_breakdown_experiment):
    return make_breakdown_experiment(1)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

_KINDS = {
    "exposure": ("exposure", "variant_exposure"),
    "experiment_exposure": ("experiment_exposure", "variant_exposure"),
    "purchase": ("purchase", "purchase"),
    "signup": ("signup", "signup"),
    "page_view": ("page_view", "page_view"),
}


def _arm(experiment, name: str) -> Variant:
    (variant,) = [v for v in experiment.variants if v.name == name]
    return variant


def _event(
    experiment,
    arm: Optional[str],
    user: str,
    kind: str,
    metadata: Any = None,
    *,
    index: int = 0,
    created: Optional[str] = None,
    received: Optional[datetime] = None,
    event_id: Optional[uuid.UUID] = None,
    tagged_arm: Optional[str] = None,
    untagged: bool = False,
) -> Event:
    """One event of ``user``; ``metadata`` None is JSON null, SQL_NULL is SQL NULL.

    The event is tagged with ``tagged_arm`` (default ``arm``), or with no
    experiment and no variant when ``untagged``. Without ``created`` and
    ``received`` the ``index``-th event of a user is one minute (client time)
    and one second (server time) after the one before.
    """
    event_type, event_name = _KINDS[kind]
    variant_name = tagged_arm or arm
    fields: Dict[str, Any] = {
        "event_type": event_type,
        "event_name": event_name,
        "user_id": f"{USER_PREFIX}{user}",
        "experiment_id": None if untagged else experiment.id,
        "variant_id": (
            None
            if untagged or variant_name is None
            else _arm(experiment, variant_name).id
        ),
        "value": 1.0 if kind in ("purchase", "signup") else None,
        "event_metadata": sa_null() if metadata == SQL_NULL else metadata,
        "created_at": created
        or (BASE + timedelta(minutes=index)).isoformat() + "+00:00",
        "updated_at": received or BASE + timedelta(seconds=index),
    }
    if event_id is not None:
        fields["id"] = event_id
    return Event(**fields)


def _assign(db, experiment, arm: str, user: str) -> None:
    db.add(
        Assignment(
            experiment_id=experiment.id,
            variant_id=_arm(experiment, arm).id,
            user_id=f"{USER_PREFIX}{user}",
        )
    )


def _user(db, experiment, arm: str, user: str, events: Iterable[Tuple]) -> None:
    """Assign ``user`` to ``arm``; ``events`` is ``[(kind, metadata), ...]``."""
    _assign(db, experiment, arm, user)
    for index, (kind, metadata) in enumerate(events):
        db.add(_event(experiment, arm, user, kind, metadata, index=index))
    db.commit()


def _bulk_users(db, experiment, users: List[Tuple[str, str, Optional[str], bool]]):
    """Bulk-insert ``(arm, user, value, converted)``.

    A user with a value has an exposure carrying it; a converting user has a
    purchase with no metadata, what the SDKs send for a call with no
    properties.
    """
    arm_ids = {name: _arm(experiment, name).id for name in ARMS}
    assignments = []
    events = []
    for arm, user, value, converted in users:
        user_id = f"{USER_PREFIX}{user}"
        assignments.append(
            {
                "id": uuid.uuid4(),
                "experiment_id": experiment.id,
                "variant_id": arm_ids[arm],
                "user_id": user_id,
            }
        )
        base = {
            "experiment_id": experiment.id,
            "variant_id": arm_ids[arm],
            "user_id": user_id,
        }
        if value is not None:
            events.append(
                dict(
                    base,
                    id=uuid.uuid4(),
                    event_type="exposure",
                    event_name="variant_exposure",
                    event_metadata={DIM: value},
                    created_at=BASE.isoformat() + "+00:00",
                    updated_at=BASE,
                )
            )
        if converted:
            events.append(
                dict(
                    base,
                    id=uuid.uuid4(),
                    event_type="purchase",
                    event_name="purchase",
                    value=1.0,
                    event_metadata=None,
                    created_at=(BASE + timedelta(minutes=1)).isoformat() + "+00:00",
                    updated_at=BASE + timedelta(seconds=1),
                )
            )
    db.execute(insert(Assignment), assignments)
    if events:
        db.execute(insert(Event), events)
    db.commit()


def _expected(users: Iterable[Tuple[str, str, bool]]) -> Rows:
    """Exact rows from ``(arm, segment, converted)`` per user, every arm listed."""
    rows: Dict[str, Dict[str, List[int]]] = {}
    for arm, segment, converted in users:
        row = rows.setdefault(segment, {name: [0, 0] for name in ARMS})
        row[arm][0] += 1
        row[arm][1] += int(converted)
    return {
        segment: {arm: (n, c) for arm, (n, c) in row.items()}
        for segment, row in rows.items()
    }


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


def _get_results(client, experiment, dimension=DIM, **params):
    query = {"breakdown": dimension, "use_cache": "false"}
    query.update(params)
    return client.get(f"/api/v1/results/{experiment.id}", params=query)


def _get_segmented(client, experiment, dimension=DIM, **params):
    query = {"skip_cache": "true"}
    query.update(params)
    return client.get(
        f"/api/v1/experiments/{experiment.id}/segmented-results/"
        f"{quote(dimension, safe='')}",
        params=query,
    )


def _results_rows(body) -> Rows:
    segments = body["breakdown"]["segments"]
    rows = {
        s["segment_value"]: {
            v["variant_name"]: (v["sample_size"], v["conversions"])
            for v in s["variants"]
        }
        for s in segments
    }
    assert len(rows) == len(segments), [s["segment_value"] for s in segments]
    return rows


def _segmented_rows(body, metric_name: str = PURCHASE) -> Rows:
    rows: Rows = {}
    for s in body["segments"]:
        (metric,) = [m for m in s["metrics"] if m["metric_name"] == metric_name]
        assert s["segment_value"] not in rows, s["segment_value"]
        rows[s["segment_value"]] = {
            v["variant_name"]: (v["sample_size"], v["conversions"])
            for v in metric["variants"]
        }
    return rows


def _headline(body, metric_name: str = PURCHASE) -> Dict[str, Tuple[int, int]]:
    (metric,) = [m for m in body["metrics"] if m["metric_name"] == metric_name]
    return {
        v["variant_name"]: (v["sample_size"], v["conversions"])
        for v in metric["variants"]
    }


def _both_routes(
    client, experiment, dimension=DIM, metric_name: str = PURCHASE
) -> Tuple[Dict[str, Rows], Dict[str, Tuple[int, int]]]:
    """Each route's rows, and the headline of ``metric_name``."""
    results = _get_results(client, experiment, dimension)
    assert results.status_code == 200, results.text
    segmented = _get_segmented(client, experiment, dimension)
    assert segmented.status_code == 200, segmented.text
    body = results.json()
    assert body["metrics"][0]["metric_name"] == metric_name, body["metrics"]
    return (
        {
            "/results": _results_rows(body),
            "/segmented-results": _segmented_rows(segmented.json(), metric_name),
        },
        _headline(body, metric_name),
    )


def _sums(rows: Rows, arm: str) -> Tuple[int, int]:
    return (
        sum(row[arm][0] for row in rows.values()),
        sum(row[arm][1] for row in rows.values()),
    )


def _assert_rows(routes: Dict[str, Rows], expected: Rows, headline) -> None:
    """Exact rows on both routes (so the routes agree), adding up to the headline."""
    failures = []
    for route, rows in routes.items():
        for arm in ARMS:
            if _sums(rows, arm) != headline[arm]:
                failures.append(
                    f"{route} {arm}: rows sum {_sums(rows, arm)} != {headline[arm]}"
                )
        if rows != expected:
            for segment in sorted(set(rows) | set(expected)):
                if rows.get(segment) != expected.get(segment):
                    failures.append(
                        f"{route} {segment!r}: {rows.get(segment)} "
                        f"!= {expected.get(segment)}"
                    )
    assert not failures, failures


# ---------------------------------------------------------------------------
# Shared fixtures of several cases
# ---------------------------------------------------------------------------


def _two_segments(db, experiment) -> Rows:
    """Four users, ios and android, keyed exposure and keyed purchase."""
    users = []
    for i, platform in enumerate(["ios", "ios", "android", "android"]):
        arm = ARMS[i % 2]
        _user(
            db,
            experiment,
            arm,
            f"d-{i}",
            [("exposure", {DIM: platform}), ("purchase", {DIM: platform})],
        )
        users.append((arm, platform, True))
    return _expected(users)


class _FakeRedis:
    """The handful of Redis calls CacheService makes, over one shared dict."""

    store: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def ping(self):
        return True

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value.encode() if isinstance(value, str) else value
        return True

    def keys(self, pattern):
        return [k for k in self.store if fnmatch.fnmatchcase(k, pattern)]

    def delete(self, *keys):
        return sum(1 for k in keys if self.store.pop(k, None) is not None)


def _snapshot_count(db, experiment) -> int:
    db.rollback()
    return (
        db.query(AnalysisSnapshot)
        .filter(AnalysisSnapshot.experiment_id == experiment.id)
        .count()
    )


def _segment_module():
    return importlib.import_module(SEGMENT_MODULE)


# ---------------------------------------------------------------------------
# V1: a user whose events name two segments is in one
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_user_with_events_in_two_segments_is_in_one(admin_client, db_session, exp):
    """An ios exposure and an android purchase: the user is ios, and converted."""
    _user(
        db_session,
        exp,
        "control",
        "u1",
        [("exposure", {DIM: "ios"}), ("purchase", {DIM: "android"})],
    )
    routes, headline = _both_routes(admin_client, exp)
    for route, rows in routes.items():
        in_rows = sorted(k for k, row in rows.items() if row["control"][0] > 0)
        assert len(in_rows) == 1, f"{route}: u1 is in {in_rows}, not one segment"
    _assert_rows(routes, _expected([("control", "ios", True)]), headline)


# ---------------------------------------------------------------------------
# V2: the order that decides a user's value
# ---------------------------------------------------------------------------

WHOLE_SECOND = "2026-11-01T12:00:00+00:00"
HALF_SECOND = "2026-11-01T12:00:00.5+00:00"


@pytest.mark.regression
@pytest.mark.parametrize(
    "first_received, want",
    [("android", "android"), ("ios", "ios")],
    ids=["a1-android-received-first", "a2-ios-received-first"],
)
def test_a_user_value_is_the_first_received_keyed_event_order(
    admin_client, db_session, exp, first_received, want
):
    """No keyed exposure; two keyed events in the same second of client time.

    ios is stamped ``12:00:00``, android ``12:00:00.5``. The one the server
    received first (``updated_at``) decides, in both directions, whatever the
    text order of the two timestamps is under the database's collation.
    """
    first = BASE
    second = BASE + timedelta(seconds=1)
    received = {
        "ios": first if first_received == "ios" else second,
        "android": first if first_received == "android" else second,
    }
    _assign(db_session, exp, "control", "o1")
    db_session.add_all(
        [
            _event(
                exp,
                "control",
                "o1",
                "page_view",
                {DIM: "ios"},
                created=WHOLE_SECOND,
                received=received["ios"],
            ),
            _event(
                exp,
                "control",
                "o1",
                "page_view",
                {DIM: "android"},
                created=HALF_SECOND,
                received=received["android"],
            ),
            _event(
                exp,
                "control",
                "o1",
                "purchase",
                None,
                received=BASE + timedelta(seconds=2),
            ),
        ]
    )
    db_session.commit()
    routes, headline = _both_routes(admin_client, exp)
    _assert_rows(routes, _expected([("control", want, True)]), headline)


@pytest.mark.regression
@pytest.mark.parametrize("exposure_type", EXPOSURE_EVENT_TYPES)
@pytest.mark.parametrize(
    "purchase_received",
    ["after-the-exposure", "before-the-exposure"],
)
def test_an_exposure_value_wins_over_an_earlier_keyed_event_order(
    admin_client, db_session, exp, purchase_received, exposure_type
):
    """Case A reversed: the android purchase carries an earlier client timestamp.

    The ios exposure still decides, whether the purchase was received after it
    or before it: exposures come first, and a client timestamp never moves a
    user's segment.
    """
    exposure_at = BASE + timedelta(hours=1)
    purchase_at = (
        exposure_at + timedelta(seconds=1)
        if purchase_received == "after-the-exposure"
        else BASE
    )
    _assign(db_session, exp, "treatment", "o2")
    db_session.add_all(
        [
            _event(
                exp,
                "treatment",
                "o2",
                exposure_type,
                {DIM: "ios"},
                created=exposure_at.isoformat() + "+00:00",
                received=exposure_at,
            ),
            _event(
                exp,
                "treatment",
                "o2",
                "purchase",
                {DIM: "android"},
                created=(BASE - timedelta(hours=1)).isoformat() + "+00:00",
                received=purchase_at,
            ),
        ]
    )
    db_session.commit()
    routes, headline = _both_routes(admin_client, exp)
    _assert_rows(routes, _expected([("treatment", "ios", True)]), headline)


SMALLER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
LARGER_ID = uuid.UUID("ffffffff-ffff-4fff-bfff-fffffffffffe")


@pytest.mark.regression
@pytest.mark.parametrize(
    "insert_first", ["smaller-id", "larger-id"], ids=["smaller-first", "larger-first"]
)
def test_a_user_value_on_equal_keys_is_the_smaller_event_id_tie(
    admin_client, db_session, exp, insert_first
):
    """Two keyed events equal in type, client time and server time.

    The event with the smaller id decides, inserted in either order. Its value,
    ``web``, sorts after the other's, so a tie broken by the value would pick
    ``android``.
    """
    _assign(db_session, exp, "control", "t1")
    db_session.commit()
    smaller = {"event_id": SMALLER_ID, "metadata": {DIM: "web"}}
    larger = {"event_id": LARGER_ID, "metadata": {DIM: "android"}}
    order = [smaller, larger] if insert_first == "smaller-id" else [larger, smaller]
    for spec in order:
        db_session.add(
            _event(
                exp,
                "control",
                "t1",
                "page_view",
                spec["metadata"],
                created=WHOLE_SECOND,
                received=BASE,
                event_id=spec["event_id"],
            )
        )
        db_session.commit()
    db_session.add(
        _event(
            exp, "control", "t1", "purchase", None, received=BASE + timedelta(seconds=5)
        )
    )
    db_session.commit()
    routes, headline = _both_routes(admin_client, exp)
    _assert_rows(routes, _expected([("control", "web", True)]), headline)


# ---------------------------------------------------------------------------
# V3: a conversion with no value counts in the user's segment
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "metadata",
    [None, {}, SQL_NULL, {DIM: None}, {DIM: ""}],
    ids=["json-null", "empty-object", "sql-null", "key-null", "empty-string"],
)
def test_a_conversion_without_the_key_counts_in_the_users_segment(
    admin_client, db_session, exp, metadata
):
    _user(
        db_session,
        exp,
        "control",
        "b1",
        [("exposure", {DIM: "ios"}), ("purchase", metadata)],
    )
    routes, headline = _both_routes(admin_client, exp)
    for route, rows in routes.items():
        assert rows.get("ios", {}).get("control") == (1, 1), (
            f"{route}: ios control is {rows.get('ios', {}).get('control')}, want (1, 1)"
        )
    _assert_rows(routes, _expected([("control", "ios", True)]), headline)


# ---------------------------------------------------------------------------
# V4: users with no value are in unknown
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_users_without_a_value_are_counted_under_unknown(admin_client, db_session, exp):
    """Eight ios users, one whose only event has no metadata, one with none."""
    users = []
    for i in range(8):
        arm = ARMS[i % 2]
        _user(
            db_session,
            exp,
            arm,
            f"c2-{i}",
            [("exposure", {DIM: "ios"}), ("purchase", {DIM: "ios"})],
        )
        users.append((arm, "ios", True))
    _user(db_session, exp, "control", "c2-bare-0", [("purchase", None)])
    _user(db_session, exp, "treatment", "c2-bare-1", [])
    users += [("control", "unknown", True), ("treatment", "unknown", False)]
    expected = _expected(users)
    assert expected["ios"] == {"control": (4, 4), "treatment": (4, 4)}
    assert expected["unknown"] == {"control": (1, 1), "treatment": (1, 0)}
    routes, headline = _both_routes(admin_client, exp)
    _assert_rows(routes, expected, headline)


@pytest.mark.regression
def test_a_user_whose_only_value_is_empty_is_counted_under_unknown(
    admin_client, db_session, exp
):
    _user(db_session, exp, "control", "e1", [("exposure", {DIM: ""})])
    _user(
        db_session,
        exp,
        "treatment",
        "e2",
        [("exposure", {DIM: "ios"}), ("purchase", None)],
    )
    routes, headline = _both_routes(admin_client, exp)
    _assert_rows(
        routes,
        _expected([("control", "unknown", False), ("treatment", "ios", True)]),
        headline,
    )


@pytest.mark.regression
def test_values_are_the_exact_text_of_a_top_level_key(admin_client, db_session, exp):
    """No trimming or change of case; a boolean, a number and an object are
    their JSON text; a nested key is not read, and a null value is no value."""
    cases = [
        ("control", "x1", {DIM: "ios"}, "ios"),
        ("treatment", "x2", {DIM: "iOS"}, "iOS"),
        ("control", "x3", {DIM: " ios "}, " ios "),
        ("treatment", "x4", {DIM: True}, "true"),
        ("control", "x5", {DIM: 5}, "5"),
        ("treatment", "x6", {DIM: {"os": "ios"}}, '{"os": "ios"}'),
        ("control", "x7", {"device": {DIM: "ios"}}, "unknown"),
        ("treatment", "x8", {DIM: None}, "unknown"),
    ]
    for arm, user, metadata, _ in cases:
        _user(db_session, exp, arm, user, [("exposure", metadata), ("purchase", None)])
    routes, headline = _both_routes(admin_client, exp)
    _assert_rows(
        routes,
        _expected((arm, segment, True) for arm, _, _, segment in cases),
        headline,
    )


# ---------------------------------------------------------------------------
# V5: the SDK path, through the real tracking routes
# ---------------------------------------------------------------------------

SDK_USERS = {
    "e-0": "ios",
    "e-1": "ios",
    "e-2": "android",
    "e-3": "android",
    **{f"e-{i}": ("ios" if i % 2 else "android") for i in range(4, 12)},
    "e-noctx": None,
}


def _arms_by_user(db, experiment) -> Dict[str, str]:
    db.rollback()
    names = {v.id: v.name for v in experiment.variants}
    return {
        user_id[len(USER_PREFIX) :]: names[variant_id]
        for user_id, variant_id in db.query(Assignment.user_id, Assignment.variant_id)
        .filter(Assignment.experiment_id == experiment.id)
        .all()
    }


def _sdk_path(client, experiment) -> List[Tuple[str, str, bool]]:
    """Assign with the attribute as context; track with no metadata.

    Every other user converts through ``/tracking/track``, and two more
    through one ``/tracking/batch`` call. Returns ``(user, segment, converted)``.
    """
    for user, platform in SDK_USERS.items():
        body: Dict[str, Any] = {
            "experiment_key": experiment.key,
            "user_id": f"{USER_PREFIX}{user}",
        }
        if platform:
            body["context"] = {DIM: platform}
        response = client.post("/api/v1/tracking/assign", json=body)
        assert response.status_code == 200, response.text
        assert response.json().get("assigned", True), response.json()
    users = list(SDK_USERS)
    tracked = set(users[::2])
    for user in sorted(tracked):
        response = client.post(
            "/api/v1/tracking/track",
            json={
                "experiment_key": experiment.key,
                "user_id": f"{USER_PREFIX}{user}",
                "event_type": "purchase",
                "event_name": "purchase",
                "value": 1.0,
            },
        )
        assert response.status_code == 200, response.text
    batched = ["e-1", "e-3"]
    response = client.post(
        "/api/v1/tracking/batch",
        json={
            "events": [
                {
                    "experiment_key": experiment.key,
                    "user_id": f"{USER_PREFIX}{user}",
                    "event_type": "purchase",
                    "event_name": "purchase",
                    "value": 1.0,
                }
                for user in batched
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["success_count"] == len(batched), response.json()
    converted = tracked | set(batched)
    return [
        (user, platform or "unknown", user in converted)
        for user, platform in SDK_USERS.items()
    ]


@pytest.mark.regression
def test_the_sdk_path_puts_each_converter_in_its_assignment_context_segment(
    admin_client, db_session, exp
):
    users = _sdk_path(admin_client, exp)
    arms = _arms_by_user(db_session, exp)
    assert set(arms) == set(SDK_USERS)
    expected = _expected((arms[user], segment, conv) for user, segment, conv in users)
    # Only the user assigned without a context is unknown, and real segments
    # hold conversions.
    assert sum(n for n, _ in expected["unknown"].values()) == 1
    assert sum(c for row in ("ios", "android") for _, c in expected[row].values()) > 0
    routes, headline = _both_routes(admin_client, exp)
    _assert_rows(routes, expected, headline)


@pytest.mark.regression
def test_the_sdk_path_with_an_earlier_assignment_reads_the_exposure(
    admin_client, db_session, exp
):
    """An assignment row with no context and a keyed exposure: the exposure decides."""
    for user in ("e-pre-0", "e-pre-1"):
        _user(db_session, exp, "control", user, [("exposure", {DIM: "ios"})])
    for user in ("e-pre-0", "e-pre-1"):
        response = admin_client.post(
            "/api/v1/tracking/track",
            json={
                "experiment_key": exp.key,
                "user_id": f"{USER_PREFIX}{user}",
                "event_type": "purchase",
                "event_name": "purchase",
                "value": 1.0,
            },
        )
        assert response.status_code == 200, response.text
    db_session.rollback()
    contexts = [
        a.context
        for a in db_session.query(Assignment).filter(Assignment.experiment_id == exp.id)
    ]
    assert contexts == [None, None]
    routes, headline = _both_routes(admin_client, exp)
    for route, rows in routes.items():
        conversions = sum(c for _, c in rows.get("ios", {}).values())
        assert conversions == 2, f"{route}: ios conversions {conversions} != 2"
    _assert_rows(
        routes,
        _expected([("control", "ios", True), ("control", "ios", True)]),
        headline,
    )


# ---------------------------------------------------------------------------
# V6: what was right stays right (characterisation, not a regression test)
# ---------------------------------------------------------------------------

#: Recorded from a run on main (695e51c7): case C, both segments, Wilson
#: intervals at 0.05 / 2.
CASE_C_INTERVALS = {
    "ios": {
        "control": [0.1749814605872514, 0.9496464287740055],
        "treatment": [0.1749814605872514, 0.9496464287740055],
    },
    "android": {
        "control": [0.07713555800988448, 0.9228644419901155],
        "treatment": [0.07713555800988448, 0.9228644419901155],
    },
}


def test_consistent_values_keep_their_counts_and_intervals(
    admin_client, db_session, exp
):
    users = []
    for i in range(10):
        arm = ARMS[i % 2]
        platform = "ios" if i % 4 < 2 else "android"
        events = [("exposure", {DIM: platform})]
        if i < 6:
            events.append(("purchase", {DIM: platform}))
        _user(db_session, exp, arm, f"c-{i}", events)
        users.append((arm, platform, i < 6))
    expected = _expected(users)
    assert _sums(expected, "control") == (5, 3)
    assert _sums(expected, "treatment") == (5, 3)
    routes, headline = _both_routes(admin_client, exp)
    _assert_rows(routes, expected, headline)
    body = _get_results(admin_client, exp).json()
    intervals = {
        s["segment_value"]: {
            v["variant_name"]: v["confidence_interval"] for v in s["variants"]
        }
        for s in body["breakdown"]["segments"]
    }
    for segment, arms in CASE_C_INTERVALS.items():
        for arm, interval in arms.items():
            assert intervals[segment][arm] == pytest.approx(interval, rel=1e-9), (
                segment,
                arm,
            )


# ---------------------------------------------------------------------------
# V7: one rule for every metric
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_each_metric_counts_the_same_users_in_the_same_segments(
    admin_client, db_session, make_breakdown_experiment
):
    experiment = make_breakdown_experiment(
        1, metrics=((PURCHASE, "purchase"), (SIGNUP, "signup"))
    )
    _user(
        db_session,
        experiment,
        "control",
        "m1",
        [
            ("exposure", {DIM: "ios"}),
            ("purchase", {DIM: "android"}),
            ("signup", None),
        ],
    )
    _user(
        db_session,
        experiment,
        "control",
        "m2",
        [("exposure", {DIM: "android"}), ("signup", {DIM: "ios"})],
    )
    _user(
        db_session,
        experiment,
        "treatment",
        "m3",
        [("page_view", {DIM: "web"}), ("purchase", None)],
    )
    _user(db_session, experiment, "treatment", "m4", [])
    want = {
        PURCHASE: _expected(
            [
                ("control", "ios", True),
                ("control", "android", False),
                ("treatment", "web", True),
                ("treatment", "unknown", False),
            ]
        ),
        SIGNUP: _expected(
            [
                ("control", "ios", True),
                ("control", "android", True),
                ("treatment", "web", False),
                ("treatment", "unknown", False),
            ]
        ),
    }
    routes, headline = _both_routes(admin_client, experiment, metric_name=PURCHASE)
    _assert_rows(routes, want[PURCHASE], headline)
    body = _get_results(admin_client, experiment).json()
    segmented = _get_segmented(admin_client, experiment).json()
    signup_rows = _segmented_rows(segmented, SIGNUP)
    _assert_rows(
        {"/segmented-results": signup_rows}, want[SIGNUP], _headline(body, SIGNUP)
    )
    users = {
        segment: {arm: n for arm, (n, _) in row.items()}
        for segment, row in signup_rows.items()
    }
    assert users == {
        segment: {arm: n for arm, (n, _) in row.items()}
        for segment, row in routes["/segmented-results"].items()
    }


# ---------------------------------------------------------------------------
# V8: only this experiment's events place anyone, and only assigned users count
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_only_this_experiments_events_place_an_assigned_user(
    admin_client, db_session, make_breakdown_experiment
):
    experiment = make_breakdown_experiment(1)
    elsewhere = make_breakdown_experiment(2)
    # The same user id is ios in another experiment, and has no event here.
    _assign(db_session, experiment, "control", "x-other")
    _user(db_session, elsewhere, "control", "x-other", [("exposure", {DIM: "ios"})])
    # An exposure of this experiment tagged with the other arm still counts.
    _assign(db_session, experiment, "control", "x-arm")
    db_session.add_all(
        [
            _event(
                experiment,
                "control",
                "x-arm",
                "exposure",
                {DIM: "android"},
                tagged_arm="treatment",
            ),
            _event(experiment, "control", "x-arm", "purchase", None, index=1),
        ]
    )
    # An event with no experiment places nobody.
    _assign(db_session, experiment, "treatment", "x-untagged")
    db_session.add(
        _event(
            experiment,
            "treatment",
            "x-untagged",
            "page_view",
            {DIM: "ios"},
            untagged=True,
        )
    )
    db_session.commit()
    # A user with events here but no assignment is in no row.
    db_session.add_all(
        [
            _event(experiment, "control", "x-ghost", "exposure", {DIM: "ios"}),
            _event(experiment, "control", "x-ghost", "purchase", None, index=1),
        ]
    )
    db_session.commit()
    _user(
        db_session,
        experiment,
        "treatment",
        "x-ios",
        [("exposure", {DIM: "ios"}), ("purchase", None)],
    )
    routes, headline = _both_routes(admin_client, experiment)
    _assert_rows(
        routes,
        _expected(
            [
                ("control", "unknown", False),
                ("control", "android", True),
                ("treatment", "unknown", False),
                ("treatment", "ios", True),
            ]
        ),
        headline,
    )


# ---------------------------------------------------------------------------
# V9: a NUL character in the dimension
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_dimension_with_a_nul_character_is_refused_nul(
    admin_client, db_session, exp, caplog
):
    _two_segments(db_session, exp)
    dimension = "plat\x00form"
    # A valid cached answer filed under the refused dimension's own key: the
    # refusal must come before the cache is read, or this entry would answer 200.
    _FakeRedis.store = {}
    with patch("redis.Redis", _FakeRedis):
        warm = admin_client.get(f"/api/v1/results/{exp.id}", params={"breakdown": DIM})
    assert warm.status_code == 200, warm.text
    (warm_key,) = [k for k in _FakeRedis.store if k.startswith(f"results:{exp.id}:")]
    assert warm_key.endswith(f":{DIM}"), warm_key
    nul_key = warm_key[: -len(DIM)] + dimension
    _FakeRedis.store = {nul_key: _FakeRedis.store[warm_key]}
    seeded = dict(_FakeRedis.store)
    snapshots = _snapshot_count(db_session, exp)
    with caplog.at_level(logging.WARNING), patch("redis.Redis", _FakeRedis):
        results = admin_client.get(
            f"/api/v1/results/{exp.id}", params={"breakdown": dimension}
        )
        segmented = _get_segmented(admin_client, exp, dimension)
        unknown_results = admin_client.get(
            f"/api/v1/results/{uuid.uuid4()}", params={"breakdown": dimension}
        )
        unknown_segmented = admin_client.get(
            f"/api/v1/experiments/{uuid.uuid4()}/segmented-results/"
            f"{quote(dimension, safe='')}"
        )
    for name, response in (("/results", results), ("/segmented-results", segmented)):
        assert response.status_code == 400, f"{name}: {response.text}"
        assert response.json() == {"detail": NUL_TEXT}, f"{name}: {response.text}"
        assert "0x00" not in response.text
    # The refusal comes after the experiment is found.
    assert unknown_results.status_code == 404, unknown_results.text
    assert unknown_segmented.status_code == 404, unknown_segmented.text
    assert _FakeRedis.store == seeded, list(_FakeRedis.store)
    assert _snapshot_count(db_session, exp) == snapshots
    assert not [r for r in caplog.records if "DataError" in r.getMessage()]


# ---------------------------------------------------------------------------
# V10, V11: a breakdown that fails is an error, never cached or snapshotted
# ---------------------------------------------------------------------------


def _plant_failure(monkeypatch, kind: str) -> Dict[str, int]:
    """Make ``segment_breakdown.user_values`` fail; returns its firing count.

    ``python-error`` raises RuntimeError (never ValueError, which
    /segmented-results answers with 400). ``database-error`` makes
    PostgreSQL cancel the reader's own statement (a 1 ms
    ``statement_timeout`` and a 50 ms ``pg_sleep``), so a real database
    error reaches the route.
    """
    module = _segment_module()
    original = module.user_values
    fired = {"n": 0, "on": 1}

    def planted(*args, **kwargs):
        if not fired["on"]:
            return original(*args, **kwargs)
        fired["n"] += 1
        if kind == "python-error":
            raise RuntimeError("planted breakdown failure")
        db = kwargs["db"] if "db" in kwargs else args[0]
        db.execute(text("SET LOCAL statement_timeout = 1"))
        db.execute(text("SELECT pg_sleep(0.05)"))
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "user_values", planted)
    return fired


@pytest.mark.regression
@pytest.mark.parametrize("kind", ["python-error", "database-error"])
def test_a_breakdown_that_fails_answers_500_on_both_routes(
    admin_client, db_session, exp, monkeypatch, kind
):
    _two_segments(db_session, exp)
    fired = _plant_failure(monkeypatch, kind)

    results = _get_results(admin_client, exp)
    assert fired["n"] == 1, f"the planted failure fired {fired['n']} times on /results"
    assert results.status_code == 500, results.text
    assert set(results.json()) == {"detail"}, results.text
    assert results.json()["detail"].startswith(RESULTS_FAILURE), results.text

    segmented = _get_segmented(admin_client, exp)
    assert fired["n"] == 2, (
        f"the planted failure fired {fired['n'] - 1} times on /segmented-results"
    )
    assert segmented.status_code == 500, segmented.text
    assert segmented.json()["detail"].startswith(SEGMENTED_FAILURE), segmented.text
    assert "planted" not in results.text + segmented.text
    assert "statement timeout" not in results.text + segmented.text
    assert _snapshot_count(db_session, exp) == 0


@pytest.mark.regression
def test_a_failed_breakdown_is_not_cached(admin_client, db_session, exp, monkeypatch):
    expected = _two_segments(db_session, exp)
    fired = _plant_failure(monkeypatch, "python-error")
    _FakeRedis.store = {}
    with patch("redis.Redis", _FakeRedis):
        failed = admin_client.get(
            f"/api/v1/results/{exp.id}", params={"breakdown": DIM}
        )
        assert fired["n"] == 1, fired
        assert failed.status_code == 500, failed.text
        assert _FakeRedis.store == {}, list(_FakeRedis.store)
        fired["on"] = 0
        complete = admin_client.get(
            f"/api/v1/results/{exp.id}", params={"breakdown": DIM}
        )
    assert complete.status_code == 200, complete.text
    assert _results_rows(complete.json()) == expected
    assert len(_FakeRedis.store) == 1, list(_FakeRedis.store)


# ---------------------------------------------------------------------------
# V12: an answer cached by the previous engine is not served
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_breakdown_cached_by_engine_1_8_0_is_not_served(
    admin_client, db_session, exp
):
    expected = _two_segments(db_session, exp)
    _FakeRedis.store = {}
    with patch("redis.Redis", _FakeRedis):
        fresh = _get_results(admin_client, exp).json()
        (key,) = list(_FakeRedis.store)
        old_key = key.replace(f":{ENGINE_VERSION}:", ":1.8.0:", 1)
        assert old_key == f"results:{exp.id}:1.8.0:0.95:benjamini_hochberg:{DIM}"
        stale = dict(fresh, experiment_name="served from a 1.8.0 cache entry")
        stale["breakdown"] = dict(
            fresh["breakdown"],
            segments=[
                dict(s, segment_value="1.8.0")
                for s in fresh["breakdown"]["segments"][:1]
            ],
        )
        _FakeRedis.store = {old_key: json.dumps(stale).encode()}
        body = admin_client.get(
            f"/api/v1/results/{exp.id}", params={"breakdown": DIM}
        ).json()
    assert body["experiment_name"] == exp.name
    assert _results_rows(body) == expected


# ---------------------------------------------------------------------------
# V13: the listed values
# ---------------------------------------------------------------------------


def _max_listed() -> int:
    """K, the number of values a breakdown lists; the dashboard's caption says 20."""
    k = _segment_module().MAX_LISTED_SEGMENTS
    assert k == 20
    return k


def _values_seeded(db, experiment, values: List[Tuple[str, int]], order: str):
    """``(value, users)`` pairs, each user keyed on its exposure, every other
    one converting with no metadata, inserted in ``order``.

    User ids sort in the opposite order to the values, so neither the order
    of insertion nor the order of the user ids can stand in for the tie rule.
    """
    users = []
    for index, (value, n) in enumerate(values):
        rank = len(values) - index
        for i in range(n):
            arm = ARMS[i % 2]
            users.append((arm, f"cap-{rank:03d}-{i}", value, i % 2 == 0))
    if order == "descending":
        users.reverse()
    _bulk_users(db, experiment, users)
    return users


def _row_names(client, experiment) -> Dict[str, List[str]]:
    results = _get_results(client, experiment)
    assert results.status_code == 200, results.text
    segmented = _get_segmented(client, experiment)
    assert segmented.status_code == 200, segmented.text
    return {
        "/results": [
            s["segment_value"] for s in results.json()["breakdown"]["segments"]
        ],
        "/segmented-results": [
            s["segment_value"] for s in segmented.json()["segments"]
        ],
    }


@pytest.mark.regression
@pytest.mark.parametrize("order", ["ascending", "descending"])
@pytest.mark.parametrize("extra", [0, 1], ids=["k-values", "k-plus-one-values"])
def test_values_beyond_the_listed_ones_fold_into_other_cap(
    viewer_client, db_session, exp, order, extra
):
    """K values, each with the same users, list K rows and no ``other``; K + 1
    list K rows and ``other``, which holds the largest value's users."""
    k = _max_listed()
    values = [f"v{i:02d}" for i in range(k + extra)]
    users = _values_seeded(db_session, exp, [(v, 2) for v in values], order)
    kept = values[:k]
    expected = _expected(
        (arm, value if value in kept else "other", converted)
        for arm, _, value, converted in users
    )
    routes, headline = _both_routes(viewer_client, exp)
    _assert_rows(routes, expected, headline)
    for route, rows in routes.items():
        assert sorted(set(rows) - {"other"}) == kept, route
        assert ("other" in rows) == bool(extra), route
        assert len(rows) == k + extra, route


@pytest.mark.regression
def test_the_listed_values_are_those_with_the_most_users_cap(
    viewer_client, db_session, exp
):
    """The value with the fewest users folds, though it sorts first."""
    k = _max_listed()
    values = [("a-few", 1)] + [(f"v{i:02d}", 2) for i in range(k)]
    users = _values_seeded(db_session, exp, values, "ascending")
    expected = _expected(
        (arm, "other" if value == "a-few" else value, converted)
        for arm, _, value, converted in users
    )
    routes, headline = _both_routes(viewer_client, exp)
    _assert_rows(routes, expected, headline)


@pytest.mark.regression
def test_literal_other_and_unknown_take_no_listed_slot_cap(
    viewer_client, db_session, exp
):
    """A literal ``other`` is counted under other and a literal ``unknown``
    under unknown, with users that have no value: neither takes one of the K."""
    k = _max_listed()
    values = [(f"v{i:02d}", 1) for i in range(k)] + [("other", 3), ("unknown", 2)]
    users = _values_seeded(db_session, exp, values, "ascending")
    _bulk_users(db_session, exp, [("control", "cap-none", None, False)])
    expected = _expected(
        [(arm, value, converted) for arm, _, value, converted in users]
        + [("control", "unknown", False)]
    )
    assert len(expected) == k + 2
    routes, headline = _both_routes(viewer_client, exp)
    _assert_rows(routes, expected, headline)


# ---------------------------------------------------------------------------
# V14: the statistics of each row
# ---------------------------------------------------------------------------


def _fisher_two_sided(c_conv: int, c_n: int, t_conv: int, t_n: int) -> float:
    """Two-sided Fisher's exact test of the 2x2 table, from math.comb."""
    total = c_n + t_n
    converted = c_conv + t_conv

    def probability(x: int) -> float:
        return (
            math.comb(t_n, x)
            * math.comb(c_n, converted - x)
            / math.comb(total, converted)
        )

    observed = probability(t_conv)
    low, high = max(0, converted - c_n), min(converted, t_n)
    p = sum(
        probability(x)
        for x in range(low, high + 1)
        if probability(x) <= observed * (1 + 1e-7)
    )
    return min(1.0, p)


def _wilson(conversions: int, n: int, alpha: float) -> Tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    z = NormalDist().inv_cdf(1 - alpha / 2)
    p = conversions / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def test_the_independent_fisher_matches_the_published_values():
    assert _fisher_two_sided(0, 3, 3, 3) == pytest.approx(0.1, rel=1e-9)
    assert _fisher_two_sided(1, 5, 5, 5) == pytest.approx(0.047619, rel=1e-4)
    assert _fisher_two_sided(0, 20, 20, 20) == pytest.approx(1.45e-11, rel=1e-2)
    assert _fisher_two_sided(20, 200, 40, 200) == pytest.approx(0.00742, rel=1e-2)


@pytest.mark.regression
def test_each_segment_is_tested_with_fishers_exact_test(admin_client, db_session, exp):
    """Three values and a literal ``other``: four rows, so alpha is 0.05 / 4.

    3/3 against 0/3 is p = 0.10 and 20/20 against 0/20 is p = 1.45e-11:
    not significant and significant. 5/5 against 1/5 is 0.0476 (the z-test
    gave 0.0098, below the threshold).
    """
    cells = {
        "s20": {"control": (20, 0), "treatment": (20, 20)},
        "s3": {"control": (3, 0), "treatment": (3, 3)},
        "s5": {"control": (5, 1), "treatment": (5, 5)},
        "other": {"control": (2, 1), "treatment": (2, 1)},
    }
    for segment, arms in cells.items():
        for arm, (n, c) in arms.items():
            for i in range(n):
                events = [("exposure", {DIM: segment})]
                if i < c:
                    events.append(("purchase", {DIM: segment}))
                _user(db_session, exp, arm, f"f-{segment}-{arm}-{i}", events)
    response = _get_results(admin_client, exp)
    assert response.status_code == 200, response.text
    breakdown = response.json()["breakdown"]
    rows = len(breakdown["segments"])
    assert rows == 4
    alpha = 0.05 / rows
    assert breakdown["adjusted_alpha"] == pytest.approx(0.0125, rel=1e-12)
    for segment in breakdown["segments"]:
        arms = {v["variant_name"]: v for v in segment["variants"]}
        (c_n, c_conv), (t_n, t_conv) = (
            cells[segment["segment_value"]]["control"],
            cells[segment["segment_value"]]["treatment"],
        )
        assert arms["control"]["p_value"] is None
        assert arms["control"]["is_significant"] is False
        want = _fisher_two_sided(c_conv, c_n, t_conv, t_n)
        got = arms["treatment"]["p_value"]
        assert got == pytest.approx(want, rel=1e-6), (segment["segment_value"], got)
        assert arms["treatment"]["is_significant"] is (want < alpha)
        for arm, (n, c) in (("control", (c_n, c_conv)), ("treatment", (t_n, t_conv))):
            assert arms[arm]["confidence_interval"] == pytest.approx(
                list(_wilson(c, n, alpha)), rel=1e-9, abs=1e-12
            ), (segment["segment_value"], arm)
    by_value = {
        s["segment_value"]: {v["variant_name"]: v for v in s["variants"]}
        for s in breakdown["segments"]
    }
    assert by_value["s3"]["treatment"]["p_value"] == pytest.approx(0.1, rel=1e-6)
    assert by_value["s3"]["treatment"]["is_significant"] is False
    assert by_value["s20"]["treatment"]["p_value"] == pytest.approx(1.45e-11, rel=1e-2)
    assert by_value["s20"]["treatment"]["is_significant"] is True
    assert by_value["s5"]["treatment"]["is_significant"] is False


def test_a_segment_with_users_in_one_arm_only_has_no_p_value(
    admin_client, db_session, exp
):
    _user(db_session, exp, "treatment", "p1", [("exposure", {DIM: "ios"})])
    _user(db_session, exp, "control", "p2", [("exposure", {DIM: "android"})])
    _user(db_session, exp, "treatment", "p3", [("exposure", {DIM: "android"})])
    body = _get_results(admin_client, exp).json()
    by_value = {
        s["segment_value"]: {v["variant_name"]: v for v in s["variants"]}
        for s in body["breakdown"]["segments"]
    }
    assert by_value["ios"]["treatment"]["p_value"] is None
    assert by_value["ios"]["treatment"]["is_significant"] is False
    assert by_value["android"]["treatment"]["p_value"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# V15: saved analyses keep their numbers (a pin, not a regression test)
# ---------------------------------------------------------------------------


def test_an_earlier_days_snapshot_keeps_its_numbers(admin_client, db_session, exp):
    _two_segments(db_session, exp)
    yesterday = snapshot_day() - timedelta(days=1)
    db_session.add(
        AnalysisSnapshot(
            experiment_id=exp.id,
            kind="frequentist",
            engine_version="1.8.0",
            as_of=yesterday,
            payload={"breakdown": {"segments": [{"segment_value": "as recorded"}]}},
        )
    )
    db_session.commit()

    def stored():
        db_session.rollback()
        return {
            row.as_of: (row.engine_version, row.payload_text, row.payload)
            for row in db_session.execute(
                text(
                    "SELECT as_of, engine_version, payload::text AS payload_text, "
                    "payload FROM analysis_snapshots "
                    "WHERE experiment_id = :e AND kind = 'frequentist'"
                ),
                {"e": str(exp.id)},
            )
        }

    before = stored()
    response = _get_results(admin_client, exp)
    assert response.status_code == 200, response.text
    after = stored()
    assert after[yesterday] == before[yesterday]
    (today,) = [as_of for as_of in after if as_of != yesterday]
    assert today == snapshot_day()
    assert after[today][2]["breakdown"] == response.json()["breakdown"]


# ---------------------------------------------------------------------------
# V16: the segmented-results shape (a pin)
# ---------------------------------------------------------------------------


def test_the_segmented_results_keep_their_keys(admin_client, db_session, exp):
    _two_segments(db_session, exp)
    body = _get_segmented(admin_client, exp).json()
    assert set(body) == {"segments"}
    for segment in body["segments"]:
        assert set(segment) == {"segment_value", "metrics"}
        for metric in segment["metrics"]:
            assert set(metric) == {"metric_id", "metric_name", "variants"}
            for variant in metric["variants"]:
                assert set(variant) == {
                    "variant_id",
                    "variant_name",
                    "is_control",
                    "sample_size",
                    "conversions",
                    "conversion_rate",
                }
                n, c = variant["sample_size"], variant["conversions"]
                assert variant["conversion_rate"] == pytest.approx(
                    100 * c / n if n else 0
                )


# ---------------------------------------------------------------------------
# V19: users assigned in a batch
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_batch_assigned_user_is_placed_by_a_tracked_event_batch(
    admin_client, db_session, exp
):
    """/assign/batch's context sets no segment: a user with no keyed event is
    unknown, and one with a keyed track event is in its value."""
    app.dependency_overrides[require_batch_assign_key] = lambda: None
    response = admin_client.post(
        "/api/v1/tracking/assign/batch",
        json={
            "experiment_key": exp.key,
            "users": [
                {"user_id": f"{USER_PREFIX}b-ios", "context": {DIM: "ios"}},
                {"user_id": f"{USER_PREFIX}b-android", "context": {DIM: "ios"}},
            ],
        },
    )
    assert response.status_code == 200, response.text
    response = admin_client.post(
        "/api/v1/tracking/track",
        json={
            "experiment_key": exp.key,
            "user_id": f"{USER_PREFIX}b-android",
            "event_type": "purchase",
            "event_name": "purchase",
            "value": 1.0,
            "metadata": {DIM: "android"},
        },
    )
    assert response.status_code == 200, response.text
    arms = _arms_by_user(db_session, exp)
    assert set(arms) == {"b-ios", "b-android"}
    routes, headline = _both_routes(admin_client, exp)
    _assert_rows(
        routes,
        _expected(
            [(arms["b-ios"], "unknown", False), (arms["b-android"], "android", True)]
        ),
        headline,
    )


# ---------------------------------------------------------------------------
# V20, V21: which rows are listed, in which order
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_unknown_is_listed_only_when_a_user_has_no_value_unknown_absent(
    admin_client, db_session, exp
):
    _user(
        db_session,
        exp,
        "control",
        "n1",
        [("exposure", {DIM: "ios"}), ("purchase", None)],
    )
    _user(db_session, exp, "treatment", "n2", [("exposure", {DIM: "android"})])
    users = [("control", "ios", True), ("treatment", "android", False)]
    routes, headline = _both_routes(admin_client, exp)
    _assert_rows(routes, _expected(users), headline)
    _user(db_session, exp, "treatment", "n3", [])
    routes, headline = _both_routes(admin_client, exp)
    _assert_rows(routes, _expected(users + [("treatment", "unknown", False)]), headline)


@pytest.mark.regression
def test_rows_are_ordered_by_users_then_value_then_other_and_unknown(
    admin_client, db_session, exp
):
    """The values' order by users differs from their order as text, so a sort
    by value alone fails; and before #1776 /segmented-results listed no
    ``unknown``, so this fails there whatever order the values came back in."""
    _bulk_users(
        db_session,
        exp,
        [(ARMS[i % 2], f"r-c-{i}", "c", True) for i in range(5)]
        + [(ARMS[i % 2], f"r-b-{i}", "b", False) for i in range(3)]
        + [(ARMS[i % 2], f"r-a-{i}", "a", True) for i in range(3)]
        + [
            ("control", "r-other", "other", False),
            ("treatment", "r-none", None, False),
        ],
    )
    want = ["c", "a", "b", "other", "unknown"]
    first = _row_names(admin_client, exp)
    second = _row_names(admin_client, exp)
    assert first == second
    assert first == {"/results": want, "/segmented-results": want}


# ---------------------------------------------------------------------------
# V22: the breakdown reads events and assignments in separate statements
# ---------------------------------------------------------------------------

_EVENTS = re.compile(r"\bevents\b")
_ASSIGNMENTS = re.compile(r"\bassignments\b")


class _Recorder:
    def __init__(self, engine):
        self.engine = engine
        self.statements: List[str] = []

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)

    def __enter__(self):
        sa_event.listen(self.engine, "before_cursor_execute", self)
        return self

    def __exit__(self, *exc):
        sa_event.remove(self.engine, "before_cursor_execute", self)


def _both_tables(statements: Iterable[str]) -> List[str]:
    return [s for s in statements if _EVENTS.search(s) and _ASSIGNMENTS.search(s)]


@pytest.mark.regression
def test_a_breakdown_reads_events_and_assignments_separately(
    admin_client, db_session, exp
):
    _user(
        db_session,
        exp,
        "control",
        "u1",
        [("exposure", {DIM: "ios"}), ("purchase", {DIM: "android"})],
    )
    _sdk_path(admin_client, exp)
    engine = db_session.get_bind()
    with _Recorder(engine) as headline_only:
        response = admin_client.get(
            f"/api/v1/results/{exp.id}", params={"use_cache": "false"}
        )
        assert response.status_code == 200, response.text
    with _Recorder(engine) as results:
        assert _get_results(admin_client, exp).status_code == 200
    with _Recorder(engine) as segmented:
        assert _get_segmented(admin_client, exp).status_code == 200
    # The breakdown adds statements to the headline's, and every statement of
    # both requests is checked, the headline's included (it has none on main).
    added = [s for s in results.statements if s not in headline_only.statements]
    assert added, "no statement recorded for the /results breakdown"
    assert segmented.statements, "no statement recorded for /segmented-results"
    assert _both_tables(results.statements) == [], _both_tables(results.statements)
    assert _both_tables(segmented.statements) == [], _both_tables(segmented.statements)


# ---------------------------------------------------------------------------
# V23: a value longer than 255 characters is counted under other
# ---------------------------------------------------------------------------


def _length_limit() -> int:
    """The module's one value-length constant (the builder names it)."""
    module = _segment_module()
    names = [
        name
        for name, value in vars(module).items()
        if name.isupper() and name != "MAX_LISTED_SEGMENTS" and value == 255
    ]
    assert len(names) == 1, names
    return getattr(module, names[0])


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple, set, frozenset)) or hasattr(value, "_mapping"):
        for item in value:
            yield from _strings(item)


@pytest.mark.regression
def test_a_value_longer_than_255_characters_is_counted_under_other_long_values(
    admin_client, db_session, exp, monkeypatch
):
    limit = _length_limit()
    assert limit == 255
    longest = "é" * limit
    too_long = "é" * (limit + 1)
    assert len(longest.encode()) == 2 * limit
    module = _segment_module()
    original = module.user_values
    seen: List[str] = []

    def spy(*args, **kwargs):
        rows = original(*args, **kwargs)
        if hasattr(rows, "__next__"):  # an iterator is read once: keep a copy
            rows = list(rows)
        seen.extend(_strings(rows))
        return rows

    monkeypatch.setattr(module, "user_values", spy)
    _user(db_session, exp, "control", "l-255", [("exposure", {DIM: longest})])
    _user(db_session, exp, "treatment", "l-256", [("exposure", {DIM: too_long})])
    _user(
        db_session,
        exp,
        "control",
        "l-ios",
        [("exposure", {DIM: "ios"}), ("purchase", None)],
    )
    routes, headline = _both_routes(admin_client, exp)
    _assert_rows(
        routes,
        _expected(
            [
                ("control", longest, False),
                ("treatment", "other", False),
                ("control", "ios", True),
            ]
        ),
        headline,
    )
    assert longest in seen, "user_values returned no value"
    assert max(len(s) for s in seen) <= limit, max(len(s) for s in seen)


# ---------------------------------------------------------------------------
# V24, V25: the breakdown's reads and statistics do not grow with its values
# ---------------------------------------------------------------------------


def _many_values(db, experiment, n: int) -> None:
    """``n`` values with one user each, one in three converting, and one user
    with no value."""
    users = [(ARMS[i % 2], f"w-{i:03d}", f"v{i:03d}", i % 3 == 0) for i in range(n)] + [
        ("control", "w-none", None, False)
    ]
    _bulk_users(db, experiment, users)


@pytest.mark.regression
def test_a_breakdown_reads_in_a_fixed_number_of_statements(
    viewer_client, db_session, make_breakdown_experiment
):
    """21 values and 400 take the same number of statements, on both routes."""
    engine = db_session.get_bind()
    counts: Dict[str, Dict[int, int]] = {"/results": {}, "/segmented-results": {}}
    for seq, n in ((21, 21), (22, 400)):
        experiment = make_breakdown_experiment(seq)
        _many_values(db_session, experiment, n)
        for route, call in (
            ("/results", _get_results),
            ("/segmented-results", _get_segmented),
        ):
            assert call(viewer_client, experiment).status_code == 200
            with _Recorder(engine) as recorded:
                response = call(viewer_client, experiment)
            assert response.status_code == 200, response.text
            counts[route][n] = len(recorded.statements)
    for route, by_n in counts.items():
        assert by_n[21] > 0, route
        assert by_n[21] == by_n[400], f"{route}: {by_n}"


@pytest.mark.regression
def test_a_breakdown_computes_statistics_for_the_listed_rows_only(
    viewer_client, db_session, exp, monkeypatch
):
    """400 values reach compute_segment_results as 22 rows: the 20 values with
    the most users, other and unknown."""
    _many_values(db_session, exp, 400)
    original = DimensionalAnalysisService.compute_segment_results
    received: List[int] = []

    def spy(self, segments, *args, **kwargs):
        received.append(len(segments))
        return original(self, segments, *args, **kwargs)

    monkeypatch.setattr(DimensionalAnalysisService, "compute_segment_results", spy)
    original_hte = DimensionalAnalysisService.detect_hte
    hte_rows: List[List[str]] = []

    def hte_spy(self, segment_results):
        hte_rows.append([r.segment_value for r in segment_results])
        return original_hte(self, segment_results)

    monkeypatch.setattr(DimensionalAnalysisService, "detect_hte", hte_spy)
    response = _get_results(viewer_client, exp)
    assert response.status_code == 200, response.text
    assert received == [22], received
    # The cross-row test sees every returned row, other and unknown included.
    (hte_seen,) = hte_rows
    assert len(hte_seen) == 22, hte_seen
    assert {"other", "unknown"} <= set(hte_seen), hte_seen
    segmented = _get_segmented(viewer_client, exp)
    assert segmented.status_code == 200, segmented.text
    assert len(segmented.json()["segments"]) == 22
