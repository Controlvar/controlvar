"""``GET /api/v1/results/{experiment_id}/segments/{segment_id}`` (#1933, beta).

One experiment's main results counted only for its assigned users whose user
id is, by exact string match, in one ID-list segment. These tests pin:

* the counts, on fixtures whose answers are worked out by hand (Fixture M):
  non-members, members never assigned and ids that differ by a space or a
  capital are not counted; every read is filtered;
* the contract: the answer equals ``GET /results/{id}?use_cache=false`` on a
  database where every row of a user outside the segment has been deleted,
  and a segment of every assigned user answers the unfiltered numbers except
  ``summary.total_events`` (which also counts events from users with no
  assignment). Both are compared against the real ``AnalysisService`` on the
  same rows, so a change to the unfiltered computation that
  ``backend/app/services/segment_results.py`` does not mirror fails here;
* the sample ratio check of the members alone, and the whole experiment's
  failure overriding the recommendation;
* membership is read once per request, as it is now, and nothing is cached,
  snapshotted or Bayesian;
* the refusals, their order and their exact texts, with real local sign-in;
* the other results routes ignore a ``segment_id`` parameter;
* one computation per API process: a second answers 503 with Retry-After.

Every request signs in with a real local JWT of a non-superuser VIEWER (or a
user with no role), unless a test says otherwise.
"""

from __future__ import annotations

import importlib
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import event as sa_event
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints import results as results_module
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
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
from backend.app.models.segment import Segment, SegmentMember, SegmentStatus
from backend.app.models.user import User, UserRole
from backend.app.schemas.results import MetricResult
from backend.app.services import segment_results as segment_results_service
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.srm_service import SRM_RECOMMENDATION_REASON
from backend.app.services.sufficient_stats_analysis import (
    BinomialVariant,
    binomial_metric_result,
)

# The results cache's Redis (database 15, emptied), the fixture the read-rule
# table uses: in CI it is required, locally (REDIS_PORT=1) the test skips.
from backend.tests.integration.api.test_experiment_data_read_rule import (
    redis_handle,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SCHEMA = "test_experimentation"
CANNOT_VIEW = "You don't have permission to view experiments"
BASE = datetime(2026, 11, 2, 9, 0, 0)
FROZEN_AT = "2026-11-16T12:00:00Z"


def _url(experiment_id: Any, segment_id: Any) -> str:
    return f"/api/v1/results/{experiment_id}/segments/{segment_id}"


# ---------------------------------------------------------------------------
# Sign-in: real local JWTs, no override but the database
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_sign_in(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


def _session(engine):
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.execute(text(f"SET search_path TO {SCHEMA}"))
    return session


@pytest.fixture(scope="module")
def people(test_db):
    """A VIEWER and a DEVELOPER (non-superusers), a superuser, and a user with
    no role (written by raw SQL: the ORM stores VIEWER for None)."""
    session = _session(test_db)
    suffix = uuid.uuid4().hex[:8]

    def make(name, role, is_superuser=False):
        user = User(
            username=f"segres_{name}_{suffix}",
            email=f"segres_{name}_{suffix}@segment-results.test",
            full_name="Segment results",
            hashed_password="unused: these users sign in by token only",
            is_active=True,
            is_superuser=is_superuser,
            role=role or UserRole.VIEWER,
        )
        session.add(user)
        return user

    users = {
        "viewer": make("viewer", UserRole.VIEWER),
        "developer": make("developer", UserRole.DEVELOPER),
        "superuser": make("superuser", UserRole.ADMIN, True),
        "no_role": make("no_role", None),
    }
    session.commit()
    session.execute(
        text("UPDATE users SET role = NULL WHERE id = :id"),
        {"id": users["no_role"].id},
    )
    session.commit()
    session.refresh(users["no_role"])
    assert users["no_role"].role is None
    try:
        yield users
    finally:
        session.close()


def _auth(user) -> Dict[str, str]:
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


@pytest.fixture
def api(db_session) -> Iterator[TestClient]:
    engine = db_session.get_bind()

    def override_get_db():
        session = _session(engine)
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


# ---------------------------------------------------------------------------
# Building experiments and segments
# ---------------------------------------------------------------------------


@dataclass
class World:
    experiment_id: uuid.UUID
    variants: Dict[str, uuid.UUID]
    metrics: Dict[str, uuid.UUID]
    assigned: Dict[str, str]
    segments: Dict[str, uuid.UUID] = field(default_factory=dict)


#: (name, is_control, traffic_allocation)
TWO_ARMS = [("C", True, 50), ("T", False, 50)]

#: (name, event_name, metric_type, is_primary, lower_is_better, minimum)
PURCHASE = ("Purchase", "purchase", MetricType.CONVERSION, True, False, None)
REVENUE = ("Revenue", "revenue", MetricType.REVENUE, False, False, None)
SESSIONS = ("Sessions", "session", MetricType.COUNT, False, False, None)
LOAD_TIME = ("Load time", "load_time", MetricType.DURATION, False, True, None)


class Builder:
    """Creates experiments and segments, and removes them after the test."""

    def __init__(self, db_session, owner_id):
        self.db = db_session
        self.owner_id = owner_id
        self.experiments: List[uuid.UUID] = []
        self.segments: List[uuid.UUID] = []

    def experiment(
        self,
        variants: Sequence[Tuple[str, bool, int]],
        metrics: Sequence[tuple],
        assigned: Dict[str, str],
        events: Sequence[tuple] = (),
        **fields: Any,
    ) -> World:
        """``events``: (user, event_type, event_name, value, tag), where tag is
        None for the user's own variant, a variant name, or "untagged"."""
        suffix = uuid.uuid4().hex[:8]
        experiment = Experiment(
            name=f"Segment results {suffix}",
            key=f"segres-{suffix}",
            description="Segment results (#1933)",
            hypothesis="Counted for the members only",
            status=ExperimentStatus.ACTIVE,
            experiment_type=ExperimentType.A_B,
            owner_id=self.owner_id,
            start_date=datetime(2026, 11, 1),
            **fields,
        )
        self.db.add(experiment)
        self.db.flush()
        self.experiments.append(experiment.id)
        variant_ids: Dict[str, uuid.UUID] = {}
        for index, (name, is_control, share) in enumerate(variants):
            variant = Variant(
                experiment_id=experiment.id,
                name=name,
                is_control=is_control,
                traffic_allocation=share,
                created_at=datetime(2026, 11, 1, 0, 0, index),
            )
            self.db.add(variant)
            self.db.flush()
            variant_ids[name] = variant.id
        metric_ids: Dict[str, uuid.UUID] = {}
        for name, event_name, metric_type, primary, lower, minimum in metrics:
            extra = {} if minimum is None else {"minimum_sample_size": minimum}
            metric = Metric(
                experiment_id=experiment.id,
                name=name,
                event_name=event_name,
                metric_type=metric_type,
                is_primary=primary,
                lower_is_better=lower,
                **extra,
            )
            self.db.add(metric)
            self.db.flush()
            metric_ids[name] = metric.id
        for index, (user_id, variant_name) in enumerate(sorted(assigned.items())):
            self.db.add(
                Assignment(
                    experiment_id=experiment.id,
                    variant_id=variant_ids[variant_name],
                    user_id=user_id,
                    created_at=BASE + timedelta(minutes=index),
                )
            )
        for index, (user_id, event_type, event_name, value, tag) in enumerate(events):
            if tag == "untagged":
                variant_id = None
            elif tag is not None:
                variant_id = variant_ids[tag]
            else:
                variant_id = variant_ids[assigned[user_id]]
            at = BASE + timedelta(days=1 + index % 5, minutes=index)
            self.db.add(
                Event(
                    event_type=event_type,
                    event_name=event_name,
                    user_id=user_id,
                    experiment_id=experiment.id,
                    variant_id=variant_id,
                    value=value,
                    created_at=at.isoformat() + "Z",
                )
            )
        self.db.commit()
        return World(experiment.id, variant_ids, metric_ids, dict(assigned))

    def segment(
        self,
        members: Sequence[str],
        kind: str = "id_list",
        status: SegmentStatus = SegmentStatus.ACTIVE,
        name: Optional[str] = None,
    ) -> uuid.UUID:
        segment = Segment(
            name=name or f"segres list {uuid.uuid4().hex[:6]}",
            kind=kind,
            status=status,
            owner_id=self.owner_id,
            rules=(
                {
                    "logical_operator": "AND",
                    "groups": [
                        {
                            "logical_operator": "AND",
                            "conditions": [
                                {
                                    "attribute": "device",
                                    "operator": "equals",
                                    "value": "desktop",
                                }
                            ],
                        }
                    ],
                }
                if kind == "rules"
                else None
            ),
        )
        self.db.add(segment)
        self.db.flush()
        self.segments.append(segment.id)
        for member in members:
            self.db.add(SegmentMember(segment_id=segment.id, member_id=member))
        self.db.commit()
        return segment.id

    def add_members(self, segment_id: uuid.UUID, members: Sequence[str]) -> None:
        for member in members:
            self.db.add(SegmentMember(segment_id=segment_id, member_id=member))
        self.db.commit()

    def remove_members(self, segment_id: uuid.UUID, members: Sequence[str]) -> None:
        self.db.query(SegmentMember).filter(
            SegmentMember.segment_id == segment_id,
            SegmentMember.member_id.in_(list(members)),
        ).delete(synchronize_session=False)
        self.db.commit()

    def prune_outside(self, world: World, members: Sequence[str]) -> None:
        """Delete, and commit, every assignment and event row of this
        experiment from a user outside P (the members assigned to it)."""
        keep = [m for m in members if m in world.assigned]
        for model in (Event, Assignment):
            self.db.query(model).filter(
                model.experiment_id == world.experiment_id,
                ~model.user_id.in_(keep),
            ).delete(synchronize_session=False)
        self.db.commit()

    def cleanup(self) -> None:
        self.db.rollback()
        for experiment_id in self.experiments:
            for model in (AnalysisSnapshot, Event, Assignment, Metric, Variant):
                self.db.query(model).filter(
                    model.experiment_id == experiment_id
                ).delete(synchronize_session=False)
            self.db.query(Experiment).filter(Experiment.id == experiment_id).delete(
                synchronize_session=False
            )
        for segment_id in self.segments:
            self.db.query(SegmentMember).filter(
                SegmentMember.segment_id == segment_id
            ).delete(synchronize_session=False)
            self.db.query(Segment).filter(Segment.id == segment_id).delete(
                synchronize_session=False
            )
        self.db.commit()


@pytest.fixture
def build(db_session, people) -> Iterator[Builder]:
    builder = Builder(db_session, people["developer"].id)
    try:
        yield builder
    finally:
        builder.cleanup()


# --- Fixture M: the hand-computed one ------------------------------------------------

M_ASSIGNED = {
    **dict.fromkeys(["u01", "u02", "u03", "u07", "u08", "u12"], "C"),
    **dict.fromkeys(["u04", "u05", "u06", "u09", "u10", "u11"], "T"),
}
M_EVENTS = [
    ("u01", "purchase", "purchase", 1.0, None),
    ("u01", "purchase", "purchase", 1.0, None),
    ("u04", "purchase", "purchase", 1.0, None),
    ("u05", "purchase", "purchase", 1.0, None),
    ("u07", "purchase", "purchase", 1.0, None),
    ("u09", "purchase", "purchase", 1.0, None),
    ("u10", "purchase", "purchase", 1.0, None),
    ("u11", "purchase", "purchase", 1.0, None),
    ("u01", "revenue", "revenue", 5.0, None),
    ("u04", "revenue", "revenue", 10.0, None),
    ("u05", "revenue", "revenue", 30.0, None),
    ("u09", "revenue", "revenue", 100.0, None),
    # x14 has no assignment and sends a purchase tagged with the experiment.
    ("x14", "purchase", "purchase", 1.0, "untagged"),
]
#: Segment S: six assigned members, one never assigned, and two ids that
#: differ from an assigned user's by a space and by a capital.
M_MEMBERS = ["u01", "u02", "u03", "u04", "u05", "u06", "m13", " u07", "U08"]
#: P: the members assigned to the experiment.
M_P = ["u01", "u02", "u03", "u04", "u05", "u06"]

#: The PE's additions (PE1933-2): (i) a revenue value beyond VALUE_BOUND and
#: one with none, from members; (ii) a count metric's event with no value;
#: (iii) an exposure carrying a metric's event name, from a member; (iv) a
#: member's conversion tagged with a variant they are not assigned to; and a
#: lower-is-better metric.
M_EXTRA_EVENTS = [
    ("u02", "revenue", "revenue", 2e12, None),
    ("u06", "revenue", "revenue", None, None),
    ("u01", "session", "session", None, None),
    ("u01", "session", "session", 1.0, None),
    ("u04", "session", "session", 1.0, None),
    ("u04", "session", "session", 1.0, None),
    ("u10", "session", "session", 1.0, None),
    ("u02", "exposure", "purchase", None, None),
    # u03's only event: a conversion tagged with T, whom u03 is not assigned
    # to, so u03 converts on nothing.
    ("u03", "purchase", "purchase", 1.0, "T"),
    ("u01", "load_time", "load_time", 3.0, None),
    ("u02", "load_time", "load_time", 2.5, None),
    ("u04", "load_time", "load_time", 1.0, None),
    ("u06", "load_time", "load_time", 0.5, None),
    ("u09", "load_time", "load_time", 0.7, None),
]


def fixture_m(build: Builder, extras: bool = False, **fields) -> World:
    metrics = [PURCHASE, REVENUE] + ([SESSIONS, LOAD_TIME] if extras else [])
    events = M_EVENTS + (M_EXTRA_EVENTS if extras else [])
    world = build.experiment(TWO_ARMS, metrics, M_ASSIGNED, events, **fields)
    world.segments["S"] = build.segment(M_MEMBERS)
    return world


# --- Fixture M-all: three variants, every metric kind, a significant
# lower-is-better primary metric ----------------------------------------------

THREE_ARMS = [("C", True, 34), ("T1", False, 33), ("T2", False, 33)]
ERRORS = ("Errors", "error", MetricType.CONVERSION, True, True, 20)
M_ALL_METRICS = [
    ERRORS,
    PURCHASE[:3] + (False, False, None),
    REVENUE,
    SESSIONS,
    LOAD_TIME,
]


def _m_all_users() -> Dict[str, str]:
    users: Dict[str, str] = {}
    for prefix, arm in (("c", "C"), ("a", "T1"), ("b", "T2")):
        for i in range(1, 21):
            users[f"{prefix}{i:02d}"] = arm
    return users


M_ALL_ASSIGNED = _m_all_users()


def _m_all_events() -> List[tuple]:
    events: List[tuple] = []
    errors = {"c": 18, "a": 2, "b": 10}
    purchases = {"c": 5, "a": 8, "b": 6}
    for prefix in ("c", "a", "b"):
        for i in range(1, 21):
            user = f"{prefix}{i:02d}"
            if i <= errors[prefix]:
                events.append((user, "error", "error", None, None))
            if i <= purchases[prefix]:
                events.append((user, "purchase", "purchase", 1.0, None))
                revenue = float(10 * i + {"c": 1, "a": 3, "b": 7}[prefix])
                events.append((user, "revenue", "revenue", revenue, None))
            if i % 3 == 0:
                events.append((user, "session", "session", 1.0, None))
            if i % 2 == 0:
                load = round(0.25 * (i % 7) + 0.5, 2)
                events.append((user, "load_time", "load_time", load, None))
    events += [
        ("c06", "revenue", "revenue", 2e12, None),  # (i) beyond VALUE_BOUND
        ("a09", "revenue", "revenue", None, None),  # (i) no value
        ("c01", "session", "session", None, None),  # (ii) a count with no value
        ("c07", "exposure", "purchase", None, None),  # (iii)
        # (iv) c19 converts on nothing else: a conversion tagged with T1.
        ("c19", "purchase", "purchase", 1.0, "T1"),
        ("x14", "purchase", "purchase", 1.0, "untagged"),  # no assignment
    ]
    return events


def fixture_m_all(build: Builder, **fields) -> World:
    return build.experiment(
        THREE_ARMS, M_ALL_METRICS, M_ALL_ASSIGNED, _m_all_events(), **fields
    )


#: A proper subset of M-all: 15 of each arm's 20 users, plus an id never
#: assigned.
M_ALL_SUBSET = [
    f"{prefix}{i:02d}" for prefix in ("c", "a", "b") for i in range(1, 16)
] + ["zz-never-assigned"]


# ---------------------------------------------------------------------------
# Reading answers
# ---------------------------------------------------------------------------


def _get(api, url, user, **params) -> Dict[str, Any]:
    response = api.get(url, params=params, headers=_auth(user))
    assert response.status_code == 200, response.text
    return response.json()


def _metric(body: Dict[str, Any], name: str) -> Dict[str, Any]:
    (metric,) = [m for m in body["metrics"] if m["metric_name"] == name]
    return metric


def _arm(metric: Dict[str, Any], variant_id: Any) -> Dict[str, Any]:
    (variant,) = [v for v in metric["variants"] if v["variant_id"] == str(variant_id)]
    return variant


def _hand_binomial(
    db_session,
    world: World,
    metric_name: str,
    counts: Dict[str, Tuple[int, int]],
    confidence_level: float,
    correction_method: str,
) -> Dict[str, Any]:
    """``binomial_metric_result`` on hand-counted (users, converted) per arm,
    as the response would serialise it."""
    metric = db_session.get(Metric, world.metrics[metric_name])
    variants = (
        db_session.query(Variant)
        .filter(Variant.experiment_id == world.experiment_id)
        .order_by(Variant.created_at, Variant.id)
        .all()
    )
    result = binomial_metric_result(
        [
            (
                BinomialVariant(str(v.id), v.name, bool(v.is_control)),
                counts[v.name][0],
                counts[v.name][1],
            )
            for v in variants
        ],
        1.0 - confidence_level,
        correction_method,
        metric=metric,
    )
    return MetricResult(**result).model_dump(mode="json")


# --- comparing two answers ---------------------------------------------------------

#: The unfiltered function each part of the answer mirrors, for the failure.
MIRRORS = (
    (".summary.total", "AnalysisService.calculate_experiment_summary"),
    (".summary.duration_days", "AnalysisService.calculate_experiment_summary"),
    (".summary", "AnalysisService._summarise_decision and the #880 rule"),
    (".srm", "srm_service.compute_srm_for_experiment"),
    (".sample_size_adequate", "AnalysisService._sample_size_adequate"),
)


def _mirror(path: str, segment_body: Dict[str, Any]) -> str:
    if path.startswith(".metrics["):
        index = int(path[len(".metrics[") : path.index("]")])
        metric = segment_body["metrics"][index]
        if metric.get("analysis") == "mean":
            if metric.get("metric_type") == "count":
                return "AnalysisService.event_counts"
            return "AnalysisService.value_totals"
        return "AnalysisService.calculate_metric_results"
    for prefix, name in MIRRORS:
        if path.startswith(prefix):
            return name
    return "AnalysisService.get_experiment_results"


def _differences(a: Any, b: Any, path: str = "") -> List[str]:
    """Paths where two answers differ: integers, strings and keys exactly,
    floats to a relative 1e-12."""
    if isinstance(a, bool) or isinstance(b, bool):
        return [] if a is b else [f"{path}: {a!r} != {b!r}"]
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, int) and isinstance(b, int):
            return [] if a == b else [f"{path}: {a!r} != {b!r}"]
        if math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=1e-15):
            return []
        return [f"{path}: {a!r} != {b!r}"]
    if type(a) is not type(b):
        return [f"{path}: {a!r} != {b!r}"]
    if isinstance(a, dict):
        out: List[str] = []
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                out.append(f"{path}.{key}: present in one answer only")
            else:
                out += _differences(a[key], b[key], f"{path}.{key}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}: {len(a)} items != {len(b)} items"]
        out = []
        for index, (x, y) in enumerate(zip(a, b)):
            out += _differences(x, y, f"{path}[{index}]")
        return out
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]


COMPARED = (
    "metrics",
    "summary",
    "srm",
    "sample_size_adequate",
    "confidence_level",
    "correction_method",
)


def _compare(segment_body, unfiltered_body, masked: Sequence[str] = ()) -> List[str]:
    a = {key: segment_body[key] for key in COMPARED}
    b = {key: unfiltered_body[key] for key in COMPARED}
    for path in masked:
        section, key = path.split(".")
        a[section] = {k: v for k, v in a[section].items() if k != key}
        b[section] = {k: v for k, v in b[section].items() if k != key}
    return [
        f"{difference} (segment_results mirrors {_mirror(difference, segment_body)}:"
        " mirror the change)"
        for difference in _differences(a, b)
    ]


# --- spies -----------------------------------------------------------------------------


class _CacheSpy:
    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.gets: List[str] = []
        self.sets: List[str] = []

    def get(self, key: str):
        self.gets.append(key)
        return self.store.get(key)

    def set(self, key: str, value: str, expire: int = 0) -> None:
        self.sets.append(key)
        self.store[key] = value


@pytest.fixture
def cache_spy(monkeypatch) -> _CacheSpy:
    spy = _CacheSpy()
    monkeypatch.setattr(results_module, "_get_cache_service", lambda: spy)
    return spy


@pytest.fixture
def resolver_calls(monkeypatch) -> List[Any]:
    calls: List[Any] = []
    real = segment_results_service.resolve_population

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(segment_results_service, "resolve_population", spy)
    return calls


def _frozen():
    """The clock frozen for an equality of two bodies with ``computed_at``.
    The engine imports statsmodels (and pandas) on first use, which does not
    finish under a frozen clock, so it is imported first."""
    importlib.import_module("statsmodels.stats.power")
    return freeze_time(FROZEN_AT, real_asyncio=True)


# ===========================================================================
# B. What the route computes
# ===========================================================================


def test_fixture_m_matches_the_hand_counts(api, build, db_session, people):
    """V7, V9, V10: the members' counts, worked out by hand. u07 and u08 are
    in the list only as " u07" and "U08", m13 is never assigned, and x14 has
    no assignment."""
    world = fixture_m(build)
    body = _get(api, _url(world.experiment_id, world.segments["S"]), people["viewer"])
    purchase = _metric(body, "Purchase")
    assert _arm(purchase, world.variants["C"])["sample_size"] == 3
    assert _arm(purchase, world.variants["C"])["conversions"] == 1
    assert _arm(purchase, world.variants["T"])["sample_size"] == 3
    assert _arm(purchase, world.variants["T"])["conversions"] == 2
    revenue = _metric(body, "Revenue")
    assert _arm(revenue, world.variants["C"])["mean"] == pytest.approx(5.0 / 3)
    assert _arm(revenue, world.variants["T"])["mean"] == pytest.approx(40.0 / 3)
    summary = body["summary"]
    assert (
        summary["total_users"],
        summary["total_events"],
        summary["total_conversions"],
    ) == (6, 7, 3)
    assert body["segment"]["member_count"] == 9
    assert body["segment"]["experiment_users"] == 12
    assert sorted(body["srm"]["observed"].values()) == [3, 3]
    assert body["srm"]["p_value"] == 1.0
    # The same estimator on the hand counts.
    expected = _hand_binomial(
        db_session,
        world,
        "Purchase",
        {"C": (3, 1), "T": (3, 2)},
        body["confidence_level"],
        body["correction_method"],
    )
    assert purchase == expected


def test_every_read_site_is_filtered(api, build, people):
    """V8: one assertion per read site, on Fixture M with the PE's additions,
    worked out by hand; and per metric, the users add up to the members of the
    sample ratio check and to summary.total_users."""
    world = fixture_m(build, extras=True)
    body = _get(api, _url(world.experiment_id, world.segments["S"]), people["viewer"])
    c, t = world.variants["C"], world.variants["T"]
    # Converters: u03's T-tagged purchase and u02's exposure named purchase
    # are not conversions.
    purchase = _metric(body, "Purchase")
    assert (_arm(purchase, c)["conversions"], _arm(purchase, t)["conversions"]) == (
        1,
        2,
    )
    # Value totals: u02's 2e12 and u06's missing value add nothing.
    revenue = _metric(body, "Revenue")
    assert _arm(revenue, c)["mean"] == pytest.approx(5.0 / 3, rel=1e-12)
    assert _arm(revenue, t)["mean"] == pytest.approx(40.0 / 3, rel=1e-12)
    # Event counts: u01's event with no value counts; u10 is not a member.
    sessions = _metric(body, "Sessions")
    assert _arm(sessions, c)["mean"] == pytest.approx(2.0 / 3, rel=1e-12)
    assert _arm(sessions, t)["mean"] == pytest.approx(2.0 / 3, rel=1e-12)
    # A lower-is-better duration: u09 is not a member.
    load_time = _metric(body, "Load time")
    assert _arm(load_time, c)["mean"] == pytest.approx(5.5 / 3, rel=1e-12)
    assert _arm(load_time, t)["mean"] == pytest.approx(0.5, rel=1e-12)
    # Totals: every event of the six (19, of every type and tag); the users
    # converting on any metric under their own variant (u03 does not).
    assert (
        body["summary"]["total_users"],
        body["summary"]["total_events"],
        body["summary"]["total_conversions"],
    ) == (6, 19, 5)
    assert sorted(body["srm"]["observed"].values()) == [3, 3]
    total = body["summary"]["total_users"]
    assert total == 6
    assert sum(body["srm"]["observed"].values()) == total
    for metric in body["metrics"]:
        assert sum(v["sample_size"] for v in metric["variants"]) == total, metric[
            "metric_name"
        ]
        for variant in metric["variants"]:
            assert variant["conversions"] <= variant["sample_size"], metric[
                "metric_name"
            ]


def test_ids_match_exactly(api, build, people):
    """V9: " u07" and "U08" are not u07 and u08."""
    world = fixture_m(build)
    body = _get(api, _url(world.experiment_id, world.segments["S"]), people["viewer"])
    assert _arm(_metric(body, "Purchase"), world.variants["C"])["sample_size"] == 3


def test_members_never_assigned_are_not_counted(api, build, people):
    """V10: m13, " u07" and "U08" are in the list and count in member_count,
    not in the users."""
    world = fixture_m(build)
    body = _get(api, _url(world.experiment_id, world.segments["S"]), people["viewer"])
    assert body["summary"]["total_users"] == 6
    assert body["segment"]["member_count"] == 9


def test_membership_is_now_and_never_cached(
    api, build, people, redis_handle, monkeypatch
):
    """V11: with the results cache on (proved by the unfiltered read writing
    its key), each read counts the list as it is when it runs, and no key
    names the segment."""
    monkeypatch.setattr(settings, "CACHE_ENABLED", True)
    world = fixture_m(build)
    segment_id = world.segments["S"]
    viewer = people["viewer"]
    _get(api, f"/api/v1/results/{world.experiment_id}", viewer)
    keys = list(redis_handle.scan_iter(match=f"results:{world.experiment_id}:*"))
    assert keys, (
        "the unfiltered read wrote nothing to Redis: the cache is not on, and "
        "this test would pass whether or not segment answers are cached"
    )
    url = _url(world.experiment_id, segment_id)
    assert _get(api, url, viewer)["summary"]["total_users"] == 6
    build.add_members(segment_id, ["u07"])
    assert _get(api, url, viewer)["summary"]["total_users"] == 7
    build.remove_members(segment_id, ["u07", "u01"])
    assert _get(api, url, viewer)["summary"]["total_users"] == 5
    named = [key for key in redis_handle.scan_iter() if str(segment_id) in key]
    assert named == []


def test_membership_is_resolved_once_per_request(api, build, people, resolver_calls):
    """V13: one membership read for every metric (four here)."""
    world = fixture_m(build, extras=True)
    body = _get(api, _url(world.experiment_id, world.segments["S"]), people["viewer"])
    assert len(body["metrics"]) >= 3
    assert len(resolver_calls) == 1


def _statements(db_session, api, url, user) -> List[str]:
    statements: List[str] = []
    engine = db_session.get_bind()

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa_event.listen(engine, "before_cursor_execute", record)
    try:
        _get(api, url, user)
    finally:
        sa_event.remove(engine, "before_cursor_execute", record)
    return statements


def test_membership_is_read_page_by_page(api, build, people, monkeypatch):
    """A list longer than one page (pages of 2 here, of 10,000 in production)
    is read in full, each id once: the same answer as one page."""
    world = fixture_m(build, extras=True)
    url = _url(world.experiment_id, world.segments["S"])
    one_page = _get(api, url, people["viewer"])
    monkeypatch.setattr(segment_results_service, "MEMBER_PAGE", 2)
    paged = _get(api, url, people["viewer"])
    assert paged["segment"]["member_count"] == 9
    assert _compare(paged, one_page) == []
    assert paged["segment"] == one_page["segment"]


def test_the_query_count_does_not_grow_with_the_list(api, build, db_session, people):
    """V14: a list of 6 members and one of 60 cost the same statements, and no
    statement reads events with assignments, or the list with assignments."""
    world = fixture_m(build, extras=True)
    small = build.segment(M_P)
    large = build.segment(M_P + [f"n{i:03d}" for i in range(54)])
    few = _statements(
        db_session, api, _url(world.experiment_id, small), people["viewer"]
    )
    many = _statements(
        db_session, api, _url(world.experiment_id, large), people["viewer"]
    )
    assert few, "no SQL was captured"
    n_few, n_many = len(few), len(many)
    assert n_many == n_few
    joined = [
        s
        for s in few + many
        if ".assignments" in s and (".events" in s or ".segment_members" in s)
    ]
    assert joined == [], joined


def test_zero_overlap_an_empty_list_and_members_in_one_arm(api, build, people):
    """V15: no assigned member, no member at all, and members only in the
    control all answer 200. A variant with no member has sample_size 0 and
    the engine's fields for an empty arm (PE1933-5)."""
    world = fixture_m(build)
    viewer = people["viewer"]
    unassigned = build.segment(["nobody-1", "nobody-2"])
    body = _get(api, _url(world.experiment_id, unassigned), viewer)
    assert body["summary"]["total_users"] == 0
    assert body["segment"]["member_count"] == 2
    assert body["srm"] is None

    empty = build.segment([])
    body = _get(api, _url(world.experiment_id, empty), viewer)
    assert body["segment"]["member_count"] == 0
    assert body["summary"]["total_users"] == 0

    control_only = build.segment(["u01", "u02", "u03"])
    body = _get(api, _url(world.experiment_id, control_only), viewer)
    treatment = _arm(_metric(body, "Purchase"), world.variants["T"])
    assert treatment["sample_size"] == 0
    assert treatment["p_value"] == 1.0
    assert treatment["relative_improvement_pct"] == -100.0
    mean_treatment = _arm(_metric(body, "Revenue"), world.variants["T"])
    assert mean_treatment["sample_size"] == 0
    assert mean_treatment["p_value"] is None


def test_a_segment_of_every_assigned_user_answers_the_unfiltered_numbers(
    api, build, people
):
    """V38 (PE1933-1, -2): a segment holding every assigned user answers what
    GET /results/{id} answers, except summary.total_events: the unfiltered
    answer also counts the events tagged with the experiment from users with
    no assignment (x14's one here)."""
    world = fixture_m_all(build)
    segment_id = build.segment(sorted(M_ALL_ASSIGNED))
    viewer = people["viewer"]
    unfiltered = _get(
        api, f"/api/v1/results/{world.experiment_id}", viewer, use_cache="false"
    )
    segment = _get(api, _url(world.experiment_id, segment_id), viewer)
    # The fixture exercises what it is meant to: three arms, every metric
    # kind, and a lower-is-better primary metric whose treatment wins.
    assert len(unfiltered["metrics"][0]["variants"]) == 3
    assert {m["metric_type"] for m in unfiltered["metrics"]} == {
        "conversion",
        "revenue",
        "count",
        "duration",
    }
    assert unfiltered["metrics"][0]["lower_is_better"] is True
    assert unfiltered["summary"]["recommendation"] == "SHIP_VARIANT"
    assert segment["summary"]["total_events"] == (
        unfiltered["summary"]["total_events"] - 1
    )
    assert _compare(segment, unfiltered, masked=["summary.total_events"]) == []


PRUNED = {
    "M": (False, M_MEMBERS),
    "M_with_additions": (True, M_MEMBERS),
    "M_all_subset": (None, M_ALL_SUBSET),
}


@pytest.mark.parametrize("case", list(PRUNED))
def test_the_answer_is_the_unfiltered_one_on_a_pruned_database(
    api, build, people, case
):
    """V39 (PE1933-3): the segment's answer, then every assignment and event
    row of this experiment from a user outside P deleted and committed, then
    GET /results/{id}?use_cache=false: the same numbers. The recommendation
    is masked only when the whole experiment's check warns (it never does
    here, which is asserted)."""
    extras, members = PRUNED[case]
    world = fixture_m_all(build) if extras is None else fixture_m(build, extras=extras)
    segment_id = build.segment(members)
    viewer = people["viewer"]
    segment = _get(api, _url(world.experiment_id, segment_id), viewer)
    build.prune_outside(world, members)
    unfiltered = _get(
        api, f"/api/v1/results/{world.experiment_id}", viewer, use_cache="false"
    )
    masked = []
    if segment["segment"]["experiment_srm_warning"]:
        masked = ["summary.recommendation", "summary.recommendation_reason"]
    assert segment["segment"]["experiment_srm_warning"] is False
    assert _compare(segment, unfiltered, masked=masked) == []


# ===========================================================================
# C. The sample ratio check inside the segment
# ===========================================================================


def _split(prefix: str, arm: str, count: int, start: int = 1) -> Dict[str, str]:
    return {f"{prefix}{i:03d}": arm for i in range(start, start + count)}


def test_srm_is_the_members_own_check(api, build, people):
    """V16, Fixture S2: members 30 C / 6 T, everyone 60 / 60."""
    assigned = {
        **_split("s2c", "C", 60),
        **_split("s2t", "T", 60),
    }
    world = build.experiment(TWO_ARMS, [PURCHASE], assigned)
    members = [f"s2c{i:03d}" for i in range(1, 31)] + [
        f"s2t{i:03d}" for i in range(1, 7)
    ]
    segment_id = build.segment(members)
    body = _get(api, _url(world.experiment_id, segment_id), people["viewer"])
    c, t = str(world.variants["C"]), str(world.variants["T"])
    assert body["srm"]["observed"] == {c: 30, t: 6}
    assert body["srm"]["expected"] == {c: 18.0, t: 18.0}
    assert body["srm"]["chi2"] == pytest.approx(16.0)
    assert body["srm"]["p_value"] == pytest.approx(6.33424836662399e-05, rel=1e-9)
    assert body["srm"]["warning"] is True
    assert body["segment"]["experiment_srm_warning"] is False
    assert body["summary"]["recommendation"] == "INCONCLUSIVE"


def test_the_whole_experiments_failure_overrides_the_recommendation(api, build, people):
    """V17 (PE1933-4), Fixture S3: everyone 80 C / 40 T (the whole check
    warns), members 20 / 20 with conversions 2 and 15, and a primary metric
    whose minimum sample size is 20, so the engine alone would ship."""
    assigned = {
        **_split("s3c", "C", 80),
        **_split("s3t", "T", 40),
    }
    members = [f"s3c{i:03d}" for i in range(1, 21)] + [
        f"s3t{i:03d}" for i in range(1, 21)
    ]
    events = [(f"s3c{i:03d}", "purchase", "purchase", 1.0, None) for i in (1, 2)]
    events += [(f"s3t{i:03d}", "purchase", "purchase", 1.0, None) for i in range(1, 16)]
    primary = PURCHASE[:5] + (20,)
    world = build.experiment(TWO_ARMS, [primary], assigned, events)
    segment_id = build.segment(members)
    body = _get(api, _url(world.experiment_id, segment_id), people["viewer"])
    assert body["sample_size_adequate"] is True
    assert body["summary"]["has_winner"] is True
    assert body["srm"]["warning"] is False
    assert body["segment"]["experiment_srm_warning"] is True
    assert body["summary"]["recommendation"] == "INCONCLUSIVE"
    assert body["summary"]["recommendation_reason"] == (
        SRM_RECOMMENDATION_REASON.format(p_value=0.0002607296328553165)
    )


def test_a_bandit_has_no_srm(api, build, people):
    """V18."""
    world = fixture_m(build, optimization_type="thompson_sampling")
    body = _get(api, _url(world.experiment_id, world.segments["S"]), people["viewer"])
    assert body["srm"] is None
    assert body["segment"]["experiment_srm_warning"] is None


# ===========================================================================
# D. Correction, label, nothing persisted
# ===========================================================================


def test_the_correction_runs_across_the_segments_treatments(
    api, build, db_session, people
):
    """V19, Fixture M3: with three arms the correction moves the numbers, and
    the answer is the estimator's on the members' counts."""
    assigned = {
        **_split("m3c", "C", 24),
        **_split("m3a", "T1", 24),
        **_split("m3b", "T2", 24),
    }
    converted = {"C": 4, "T1": 12, "T2": 7}
    events = []
    for prefix, arm in (("m3c", "C"), ("m3a", "T1"), ("m3b", "T2")):
        events += [
            (f"{prefix}{i:03d}", "purchase", "purchase", 1.0, None)
            for i in range(1, converted[arm] + 1)
        ]
        # Users 21-24 of every arm are not members, and every one converts.
        events += [
            (f"{prefix}{i:03d}", "purchase", "purchase", 1.0, None)
            for i in range(21, 25)
        ]
    world = build.experiment(THREE_ARMS, [PURCHASE], assigned, events)
    members = [
        f"{prefix}{i:03d}" for prefix in ("m3c", "m3a", "m3b") for i in range(1, 21)
    ]
    segment_id = build.segment(members)
    counts = {arm: (20, n) for arm, n in converted.items()}
    body = _get(
        api,
        _url(world.experiment_id, segment_id),
        people["viewer"],
        correction_method="bonferroni",
    )
    corrected = _hand_binomial(
        db_session, world, "Purchase", counts, body["confidence_level"], "bonferroni"
    )
    uncorrected = _hand_binomial(
        db_session, world, "Purchase", counts, body["confidence_level"], "none"
    )
    assert corrected != uncorrected, "precondition: the correction moves nothing"
    assert body["correction_method"] == "bonferroni"
    assert body["metrics"][0] == corrected


def _keys(node: Any) -> List[str]:
    if isinstance(node, dict):
        out = list(node)
        for value in node.values():
            out += _keys(value)
        return out
    if isinstance(node, list):
        out = []
        for value in node:
            out += _keys(value)
        return out
    return []


def test_the_answer_is_labelled_and_carries_no_whole_experiment_block(
    api, build, people
):
    """V20 (Q1): is_exploratory is exactly True; the membership is current;
    sequential_testing, bayesian_results and breakdown are not keys at all,
    on an experiment with Bayesian and sequential on; and no key anywhere is
    engine_version, so nothing here stamps the engine."""
    world = fixture_m(
        build,
        bayesian_enabled=True,
        bayesian_config={"prior_family": "beta", "alpha": 1.0, "beta": 1.0},
        sequential_testing_enabled=True,
        sequential_testing_config={"method": "msprt"},
    )
    viewer = people["viewer"]
    unfiltered = _get(
        api, f"/api/v1/results/{world.experiment_id}", viewer, use_cache="false"
    )
    assert unfiltered["bayesian_results"] is not None
    assert unfiltered["sequential_testing"] is not None
    body = _get(api, _url(world.experiment_id, world.segments["S"]), viewer)
    assert body["is_exploratory"] is True
    assert body["segment"]["membership"] == "current"
    assert body["segment"]["kind"] == "id_list"
    for key in ("sequential_testing", "bayesian_results", "breakdown"):
        assert key not in body, key
    assert "engine_version" not in _keys(body)


def test_nothing_is_computed_for_bayesian_and_no_snapshot_is_written(
    api, build, db_session, people, monkeypatch
):
    """V21: the stored Bayesian decision and the analysis snapshots are as the
    unfiltered read left them, read through a fresh session; and neither the
    Bayesian analysis nor the snapshot writer is called. (The request's
    session is never committed, so the decision a Bayesian computation
    flushes would not reach the database: the call count is what sees it.)"""
    world = fixture_m(
        build,
        bayesian_enabled=True,
        bayesian_config={"prior_family": "beta", "alpha": 1.0, "beta": 1.0},
        sequential_testing_enabled=True,
        sequential_testing_config={"method": "msprt"},
    )
    viewer = people["viewer"]
    _get(api, f"/api/v1/results/{world.experiment_id}", viewer, use_cache="false")
    db_session.execute(
        text("UPDATE experiments SET bayesian_decision = 'sentinel' WHERE id = :id"),
        {"id": world.experiment_id},
    )
    db_session.commit()
    before = (
        db_session.query(AnalysisSnapshot)
        .filter(AnalysisSnapshot.experiment_id == world.experiment_id)
        .count()
    )
    calls = {"bayesian": 0, "snapshot": 0}
    real_bayesian = AnalysisService.compute_bayesian_results
    real_snapshot = results_module.record_snapshot

    def bayesian(self, *args, **kwargs):
        calls["bayesian"] += 1
        return real_bayesian(self, *args, **kwargs)

    def snapshot(*args, **kwargs):
        calls["snapshot"] += 1
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(AnalysisService, "compute_bayesian_results", bayesian)
    monkeypatch.setattr(results_module, "record_snapshot", snapshot)

    _get(api, _url(world.experiment_id, world.segments["S"]), viewer)

    assert calls == {"bayesian": 0, "snapshot": 0}
    fresh = _session(db_session.get_bind())
    try:
        decision = fresh.execute(
            text("SELECT bayesian_decision FROM experiments WHERE id = :id"),
            {"id": world.experiment_id},
        ).scalar_one()
        assert decision == "sentinel"
        rows = (
            fresh.query(AnalysisSnapshot)
            .filter(AnalysisSnapshot.experiment_id == world.experiment_id)
            .all()
        )
        assert len(rows) == before
        (frequentist,) = [r for r in rows if r.kind == "frequentist"]
        assert frequentist.payload["summary"]["total_users"] == 12
    finally:
        fresh.close()


def test_the_segment_route_never_touches_the_cache(api, build, people, cache_spy):
    """V4b: the segment route reads and writes no cache entry; the unfiltered
    route, on the same spy, reads one and writes one."""
    world = fixture_m(build)
    viewer = people["viewer"]
    _get(api, _url(world.experiment_id, world.segments["S"]), viewer)
    assert (cache_spy.gets, cache_spy.sets) == ([], [])
    _get(api, f"/api/v1/results/{world.experiment_id}", viewer)
    assert (len(cache_spy.gets), len(cache_spy.sets)) == (1, 1)


def test_the_unfiltered_request_never_reads_membership(
    api, build, people, resolver_calls
):
    """V3's resolver row: GET /results/{id} and its alias take the old path."""
    world = fixture_m(build)
    viewer = people["viewer"]
    _get(api, f"/api/v1/results/{world.experiment_id}", viewer, use_cache="false")
    _get(api, f"/api/v1/experiments/{world.experiment_id}/results", viewer)
    assert resolver_calls == []


# ===========================================================================
# E. Refusals and acceptances (a real local sign-in, non-superusers)
# ===========================================================================


def test_an_unknown_segment_is_404(api, build, people):
    """V22."""
    world = fixture_m(build)
    response = api.get(
        _url(world.experiment_id, uuid.uuid4()), headers=_auth(people["viewer"])
    )
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Segment not found"}


@pytest.mark.parametrize(
    "status",
    [SegmentStatus.ARCHIVED, SegmentStatus.INACTIVE],
    ids=["archived", "inactive"],
)
def test_an_archived_or_inactive_list_is_accepted(api, build, people, status):
    """V23, V24 (D2): the same numbers as for the list when it is active, and
    the status says which it is."""
    world = fixture_m(build)
    viewer = people["viewer"]
    active = _get(api, _url(world.experiment_id, world.segments["S"]), viewer)
    other = build.segment(M_MEMBERS, status=status)
    body = _get(api, _url(world.experiment_id, other), viewer)
    assert body["segment"]["status"] == status.value
    assert active["segment"]["status"] == "active"
    assert _compare(body, active) == []


def test_a_rules_segment_is_refused(api, build, people):
    """V25: 422 at the segment's path parameter, with the fixed text."""
    world = fixture_m(build)
    rules = build.segment([], kind="rules")
    response = api.get(
        _url(world.experiment_id, rules), headers=_auth(people["viewer"])
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == [
        {
            "loc": ["path", "segment_id"],
            "msg": segment_results_service.RULES_SEGMENT_REFUSED,
            "type": "value_error",
        }
    ]


def test_breakdown_use_cache_and_segment_id_are_ignored(api, build, people, cache_spy):
    """V26: the route takes neither breakdown nor use_cache; sent, they change
    nothing, there is no breakdown key, and no cache is read or written."""
    world = fixture_m(build)
    other = build.segment(["u10"])
    url = _url(world.experiment_id, world.segments["S"])
    viewer = people["viewer"]
    with _frozen():
        plain = _get(api, url, viewer)
        sent = _get(
            api,
            url,
            viewer,
            breakdown="device",
            use_cache="false",
            segment_id=str(other),
        )
    assert sent == plain
    assert "breakdown" not in sent
    assert (cache_spy.gets, cache_spy.sets) == ([], [])


@pytest.mark.parametrize(
    "which", ["segment_id", "experiment_id"], ids=["segment", "experiment"]
)
def test_a_malformed_id_is_fastapis_422(api, build, people, which):
    """V27 (PE1933-11): FastAPI's own 422, at the path parameter. Like every
    route's, it repeats the submitted value in ``input``."""
    world = fixture_m(build)
    experiment = "not-a-uuid" if which == "experiment_id" else world.experiment_id
    segment = "not-a-uuid" if which == "segment_id" else world.segments["S"]
    response = api.get(_url(experiment, segment), headers=_auth(people["viewer"]))
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "uuid_parsing"
    assert error["loc"] == ["path", which]


def test_the_order_of_the_checks(api, build, db_session, people):
    """V28: the read rule first; then the experiment; then the segment. An
    experiment with a metric and no control is the main route's 404."""
    world = fixture_m(build)
    unknown_experiment, unknown_segment = uuid.uuid4(), uuid.uuid4()

    refused = api.get(
        _url(unknown_experiment, unknown_segment), headers=_auth(people["no_role"])
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == CANNOT_VIEW

    viewer = _auth(people["viewer"])
    for segment in (world.segments["S"], unknown_segment):
        response = api.get(_url(unknown_experiment, segment), headers=viewer)
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": "Experiment not found"}

    no_control = build.experiment(
        [("A", False, 50), ("B", False, 50)], [PURCHASE], {"u01": "A", "u02": "B"}
    )
    response = api.get(
        _url(no_control.experiment_id, world.segments["S"]), headers=viewer
    )
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Experiment not found"}


# ===========================================================================
# G. The other views ignore segment_id
# ===========================================================================

#: route -> the status it answers this experiment (the positive control: the
#: comparison below means nothing between two refusals).
OTHER_ROUTES = {
    "/api/v1/results/{id}": 200,
    "/api/v1/results/{id}/daily": 200,
    "/api/v1/results/{id}/sample-size": 200,
    "/api/v1/results/{id}/sequential": 200,
    "/api/v1/results/{id}/bayesian": 200,
    "/api/v1/results/{id}/cuped": 200,
    "/api/v1/results/{id}/balanced-groups": 409,
    "/api/v1/experiments/{id}/results": 200,
    "/api/v1/experiments/{id}/daily-results": 200,
    "/api/v1/experiments/{id}/segmented-results/tier": 200,
}


@pytest.mark.parametrize("route", OTHER_ROUTES)
def test_the_other_routes_ignore_segment_id(api, build, people, cache_spy, route):
    """V30: the same status and body with ?segment_id=<S> as without, under a
    frozen clock, and no top-level segment key. Only the segment route
    filters."""
    world = fixture_m(
        build,
        bayesian_enabled=True,
        bayesian_config={"prior_family": "beta", "alpha": 1.0, "beta": 1.0},
        sequential_testing_enabled=True,
        sequential_testing_config={"method": "msprt"},
    )
    url = route.replace("{id}", str(world.experiment_id))
    with _frozen():
        # Signed under the frozen clock: a token issued before it would be
        # expired at it, and two 401s are equal.
        headers = _auth(people["superuser"])
        plain = api.get(url, headers=headers)
        cache_spy.store.clear()
        sent = api.get(
            url, params={"segment_id": str(world.segments["S"])}, headers=headers
        )
    assert plain.status_code == OTHER_ROUTES[route], plain.text
    assert sent.status_code == plain.status_code, sent.text
    assert sent.content == plain.content
    if sent.headers.get("content-type", "").startswith("application/json"):
        body = sent.json()
        if isinstance(body, dict):
            assert "segment" not in body


def test_the_export_report_ignores_segment_id(api, build, people):
    """V30's export row, one call (the export routes share one counter of 10 a
    minute) and not under a frozen clock (FastAPI cannot build the export
    route's datetime parameters under freezegun's datetime): sent a
    segment_id, the report answers for all users, with no segment in it."""
    world = fixture_m(build)
    response = api.get(
        f"/api/v1/export/reports/experiments/{world.experiment_id}",
        params={"segment_id": str(world.segments["S"])},
        headers=_auth(people["superuser"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "segment" not in body
    (row,) = body["experiments"]
    assert (row["total_assignments"], row["total_events"]) == (12, 13)
    assert sorted(v["assignments"] for v in body["variants"]) == [6, 6]


# ===========================================================================
# One computation per API process (EM1933-4)
# ===========================================================================


def test_a_second_computation_waits_its_turn(api, build, people, monkeypatch):
    """V40: while the slot is held the route answers 503 with the fixed text
    and Retry-After: 5; released, 200. A computation that fails gives the slot
    back: the next request is answered."""
    world = fixture_m(build)
    url = _url(world.experiment_id, world.segments["S"])
    viewer = _auth(people["viewer"])
    slot = segment_results_service.COMPUTE_SLOT

    assert slot.acquire(blocking=False), "the slot was already held"
    try:
        busy = api.get(url, headers=viewer)
    finally:
        slot.release()
    assert busy.status_code == 503, busy.text
    assert busy.json() == {"detail": segment_results_service.BUSY}
    assert busy.headers["Retry-After"] == "5"

    assert api.get(url, headers=viewer).status_code == 200

    def broken(*_args, **_kwargs):
        raise RuntimeError("planted")

    with monkeypatch.context() as patch:
        patch.setattr(segment_results_service, "resolve_population", broken)
        failed = api.get(url, headers=viewer)
    assert failed.status_code == 500, failed.text
    after = api.get(url, headers=viewer)
    assert after.status_code == 200, after.text


def test_a_refused_request_never_takes_the_slot(api, build, people):
    """V40: the refusals (403, 404, 422) are answered while the slot is held,
    as they would be otherwise: none of them waits for it or takes it."""
    world = fixture_m(build)
    rules = build.segment([], kind="rules")
    slot = segment_results_service.COMPUTE_SLOT
    assert slot.acquire(blocking=False), "the slot was already held"
    try:
        answers = [
            api.get(
                _url(world.experiment_id, world.segments["S"]),
                headers=_auth(people["no_role"]),
            ).status_code,
            api.get(
                _url(uuid.uuid4(), world.segments["S"]), headers=_auth(people["viewer"])
            ).status_code,
            api.get(
                _url(world.experiment_id, uuid.uuid4()), headers=_auth(people["viewer"])
            ).status_code,
            api.get(
                _url(world.experiment_id, rules), headers=_auth(people["viewer"])
            ).status_code,
        ]
    finally:
        slot.release()
    assert answers == [403, 404, 404, 422]
