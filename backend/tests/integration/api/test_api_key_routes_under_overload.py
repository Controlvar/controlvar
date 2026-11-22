"""API-key routes answer every request of a burst larger than they can serve
at once, and answer again after it (#1728).

Driven through the real app with httpx's ``ASGITransport``, as
``test_api_key_request_connection.py`` is: the real ``deps.get_db``,
``deps.get_api_key``, dependencies and handlers. Only the session factory is
pointed at a small QueuePool engine on the test database (2 + 2 connections,
``pool_timeout`` 1 s), and the event loop's default worker limit is set to 4
for the test, inside the running loop. A burst of 24 concurrent requests is
more than that serves at once: each route must give every request its normal
answer. ``/tracking/assign`` is also driven for an experiment that balances
its split within groups (#448): a balanced burst is answered, and every user
of it takes a place in their group's blocks.

Listeners on the engine count checkouts and checkins. Their peak shows that
the burst used every connection, and they record the thread of each checkout:
none of these routes checks out on the event loop's thread. They see
checkouts only: a query on a connection the request already holds is not
counted, wherever it runs, so a route that only reads passes this count
whatever thread its queries run on. The split URL assign route's own reads,
before it asks for the assignment, are such queries.

The two assign rows send a new user each time, and the request before the
burst must be answered on the assigned path (``reason`` ``assigned``), so the
burst stores an assignment and a view per request rather than taking a path
that records nothing.

The event tracking and error report rows send valid bodies, each event with
an ``event_id`` of its own, and the request before the burst must show that
the route did its normal work: a ``/track`` event carries the variant of the
user the fixture assigned, a batch stores every event in it, the user's
assignments list the experiment, and a stored error report is stamped
``"server"`` (the fixture's key has ``sdk:ruleset``). After the burst, the
rows that store something must have stored one row per event or report sent.
The ``errors`` and ``errors-batch`` rows still send ``{}``, refused after the
key check.

The two routes a web page calls (#1545) take a key with no scopes: the
fixture gives them one, a running visual experiment whose page rule names a
host of its own, and a live preview link of it. ``POST /tracking/visual``
assigns a new visitor on every call, so each call commits and reads again. It
is also driven for a visual experiment that balances within groups, assigned
inside the page answer's worker: that burst is answered too, and every
visitor of it takes a place in their group's blocks.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, List, Optional

import anyio
import anyio.from_thread
import anyio.to_thread
import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from starlette.concurrency import run_in_threadpool

from backend.app.api import deps
from backend.app.core.security import hash_api_key
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Variant,
)
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.metrics.metric import ErrorLog, RawMetric
from backend.app.models.sdk_evaluation_count import (
    SdkEvaluationFlagCount,
    SdkEvaluationKeyCount,
)
from backend.app.models.visual_token import PREVIEW, VisualToken
from backend.app.services import balanced_allocation

pytestmark = pytest.mark.integration

SCHEMA = "test_experimentation"
#: The balanced experiment's setting (#448).
TIER = {"attribute": "tier", "values": ["gold", "silver"]}
#: The counter step's statement timeout during the test, raised so that a
#: slow runner cannot turn a place in the blocks into a fallback; it is not
#: what the burst row is about.
STEP_TIMEOUT_MS = 30_000
POOL_SIZE = 2
MAX_OVERFLOW = 2
CONNECTIONS = POOL_SIZE + MAX_OVERFLOW
WORKERS = 4
BURST = 24
#: The whole burst must finish within this, or the test fails rather than hangs.
BURST_DEADLINE_SECONDS = 60
#: How long teardown waits for every connection to come back.
DRAIN_SECONDS = 30


@dataclass
class PoolWatch:
    """What the engine's checkout and checkin listeners saw."""

    engine: Any
    loop_thread: Optional[int] = None
    checked_out: int = 0
    peak: int = 0
    checkout_threads: List[int] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def restart(self) -> None:
        """Count the peak and the checkout threads from now on."""
        with self.lock:
            self.peak = self.checked_out
            self.checkout_threads.clear()

    def on_loop_thread(self) -> int:
        with self.lock:
            return sum(1 for t in self.checkout_threads if t == self.loop_thread)


@dataclass
class SdkSetup:
    """A server-side key of an active developer, and what the routes act on."""

    headers: dict
    flag_key: str
    flag_id: Any
    experiment_key: str
    experiment_id: Any
    #: A user assigned to the experiment's treatment, and that variant.
    assigned_user: str
    assigned_variant_id: Any
    #: An ACTIVE experiment that balances its split within ``tier`` (#448).
    balanced_experiment_key: str
    balanced_experiment_id: Any
    #: The same developer's key with no scopes, the one a web page carries.
    page_headers: dict
    #: The host a running visual experiment's page rule names.
    visual_host: str
    #: A live preview link of that experiment's treatment.
    preview_token: str
    #: The host of a running visual experiment that balances within ``tier``.
    balanced_visual_host: str
    balanced_visual_id: Any
    #: An ACTIVE split URL experiment whose entry is on a host of its own.
    split_url_key: str
    split_url_host: str


#: The split URL experiment's entry path; its other variant is on ``-v2``.
SPLIT_URL_PATH = "/overload"


@pytest.fixture
def small_pool(test_db, monkeypatch):
    """Point ``deps.get_db`` at a 2 + 2 QueuePool engine (``pool_timeout`` 1 s)
    on the test database, with checkout and checkin listeners."""
    engine = create_engine(
        test_db.url,
        pool_pre_ping=True,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=1,
    )
    assert isinstance(engine.pool, QueuePool)
    watch = PoolWatch(engine=engine)

    @event.listens_for(engine, "connect")
    def _search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET search_path TO {SCHEMA}")
        cursor.close()

    @event.listens_for(engine, "checkout")
    def _checkout(_dbapi_connection, _record, _proxy):
        with watch.lock:
            watch.checked_out += 1
            watch.peak = max(watch.peak, watch.checked_out)
            watch.checkout_threads.append(threading.get_ident())

    @event.listens_for(engine, "checkin")
    def _checkin(_dbapi_connection, _record):
        with watch.lock:
            watch.checked_out -= 1

    monkeypatch.setattr(
        deps,
        "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    try:
        yield watch
    finally:
        deadline = time.monotonic() + DRAIN_SECONDS
        while engine.pool.checkedout() and time.monotonic() < deadline:
            time.sleep(0.05)
        engine.dispose()


@pytest.fixture
def no_active_holdout(db_session):
    """No global holdout during the test, so every new user is assigned;
    restored after."""
    parked = (
        db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active.is_(True)).all()
    )
    for row in parked:
        row.is_active = False
    db_session.commit()
    yield
    db_session.rollback()
    for row in parked:
        db_session.merge(row).is_active = True
    db_session.commit()


def _experiment(
    db_session, admin_user, balance_within=None, visual_host=None, split_url_host=None
) -> Experiment:
    """An ACTIVE 50/50 experiment with two variants, committed. With
    *visual_host*, a visual experiment whose page rule names that host, and
    whose treatment hides an element. With *split_url_host*, a split URL
    experiment whose entry is :data:`SPLIT_URL_PATH` on that host and whose
    other variant is on ``-v2``."""
    suffix = uuid.uuid4().hex[:8]
    experiment_type, split_url_config = ExperimentType.A_B, None
    if split_url_host is not None:
        entry = f"https://{split_url_host}{SPLIT_URL_PATH}"
        experiment_type = ExperimentType.SPLIT_URL
        split_url_config = {
            "variants": [
                {"name": "control", "url": entry, "traffic_allocation": 50},
                {"name": "treatment", "url": f"{entry}-v2", "traffic_allocation": 50},
            ]
        }
    visual_config = changes = None
    if visual_host is not None:
        page = {"match": "prefix", "url": f"https://{visual_host}/"}
        visual_config = {
            "version": 1,
            "editor_url": f"https://{visual_host}/",
            "pages": [page],
            "goals": [],
        }
        changes = {"version": 1, "changes": [{"op": "hide", "selector": "#promo"}]}
    experiment = Experiment(
        name=f"Overload {suffix}",
        key=f"overload-{suffix}",
        description="created by test_api_key_routes_under_overload",
        hypothesis="every request of a burst is answered",
        status=ExperimentStatus.ACTIVE,
        experiment_type=experiment_type,
        owner_id=admin_user.id,
        balance_within=balance_within,
        visual_config=visual_config,
        split_url_config=split_url_config,
    )
    db_session.add(experiment)
    db_session.commit()
    db_session.refresh(experiment)
    for name, is_control in (("control", True), ("treatment", False)):
        db_session.add(
            Variant(
                experiment_id=experiment.id,
                name=name,
                description=name,
                is_control=is_control,
                traffic_allocation=50,
                configuration={},
                visual_changes=None if is_control else changes,
            )
        )
    db_session.commit()
    return experiment


@pytest.fixture
def sdk(db_session, developer_user, make_feature_flag, admin_user):
    """A key with ``sdk:ruleset`` of an active developer, an ACTIVE flag, an
    ACTIVE experiment with two variants and a user assigned to its treatment,
    and an ACTIVE one that balances its split within ``tier``; and for the
    page routes, the same developer's key
    with no scopes, an ACTIVE visual experiment on a host of its own, a live
    preview link of its treatment, and an ACTIVE visual experiment that
    balances within ``tier`` on another host; and an ACTIVE split URL
    experiment with two URL variants whose entry is on a host of its own."""
    key, plaintext = APIKey.create_for_user(
        db_session,
        developer_user.id,
        f"overload-{uuid.uuid4().hex[:8]}",
        scopes="sdk:ruleset",
    )
    page_key, page_plaintext = APIKey.create_for_user(
        db_session, developer_user.id, f"overload-page-{uuid.uuid4().hex[:8]}"
    )
    flag = make_feature_flag(
        key=f"overload-{uuid.uuid4().hex[:8]}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
    )
    experiment = _experiment(db_session, admin_user)
    treatment = (
        db_session.query(Variant)
        .filter(Variant.experiment_id == experiment.id, Variant.is_control.is_(False))
        .one()
    )
    assigned_user = _new_user()
    db_session.add(
        Assignment(
            experiment_id=experiment.id, variant_id=treatment.id, user_id=assigned_user
        )
    )
    db_session.commit()
    balanced = _experiment(db_session, admin_user, balance_within=TIER)
    host = f"overload-{uuid.uuid4().hex[:8]}.example.com"
    visual = _experiment(db_session, admin_user, visual_host=host)
    balanced_host = f"overload-{uuid.uuid4().hex[:8]}.example.com"
    balanced_visual = _experiment(
        db_session, admin_user, balance_within=TIER, visual_host=balanced_host
    )
    split_url_host = f"overload-{uuid.uuid4().hex[:8]}.example.com"
    split_url = _experiment(db_session, admin_user, split_url_host=split_url_host)
    preview_token = secrets.token_urlsafe(32)
    db_session.add(
        VisualToken(
            kind=PREVIEW,
            token_hash=hash_api_key(preview_token),
            experiment_id=visual.id,
            variant_name="treatment",
            user_id=developer_user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    )
    db_session.commit()
    yield SdkSetup(
        headers={"X-API-Key": plaintext},
        flag_key=flag.key,
        flag_id=flag.id,
        experiment_key=experiment.key,
        experiment_id=experiment.id,
        assigned_user=assigned_user,
        assigned_variant_id=treatment.id,
        balanced_experiment_key=balanced.key,
        balanced_experiment_id=balanced.id,
        page_headers={"X-API-Key": page_plaintext},
        visual_host=host,
        preview_token=preview_token,
        balanced_visual_host=balanced_host,
        balanced_visual_id=balanced_visual.id,
        split_url_key=split_url.key,
        split_url_host=split_url_host,
    )
    db_session.rollback()
    for created in (experiment, balanced, visual, balanced_visual, split_url):
        for model in (Event, Assignment, Variant, VisualToken):
            db_session.query(model).filter(model.experiment_id == created.id).delete()
        db_session.query(Experiment).filter(Experiment.id == created.id).delete()
    db_session.query(RawMetric).filter(RawMetric.feature_flag_id == flag.id).delete()
    db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id == flag.id).delete()
    for model in (SdkEvaluationKeyCount, SdkEvaluationFlagCount):
        db_session.query(model).filter(model.flag_key == flag.key).delete()
    db_session.query(FeatureFlag).filter(FeatureFlag.id == flag.id).delete()
    db_session.query(APIKey).filter(APIKey.id.in_([key.id, page_key.id])).delete()
    db_session.commit()


Send = Callable[[httpx.AsyncClient, SdkSetup], Awaitable[httpx.Response]]


def _ruleset(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.get("/api/v1/sdk/ruleset", headers=sdk.headers)


def _evaluations(http: httpx.AsyncClient, sdk: SdkSetup):
    now = datetime.now(timezone.utc)
    entry = {
        "flag_key": sdk.flag_key,
        "count": 1,
        "enabled_count": 0,
        "window_start": (now - timedelta(seconds=30)).isoformat(),
        "window_end": now.isoformat(),
    }
    return http.post(
        "/api/v1/tracking/evaluations",
        json={"evaluations": [entry]},
        headers=sdk.headers,
    )


def _new_user() -> str:
    return f"u-{uuid.uuid4().hex[:12]}"


def _assign(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.post(
        "/api/v1/tracking/assign",
        json={"experiment_key": sdk.experiment_key, "user_id": _new_user()},
        headers=sdk.headers,
    )


def _split_url_assign(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.post(
        "/api/v1/tracking/split-url/assign",
        json={
            "experiment_key": sdk.split_url_key,
            "user_id": _new_user(),
            "host": sdk.split_url_host,
            "path": SPLIT_URL_PATH,
            "user_agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Firefox/128.0",
        },
        headers=sdk.headers,
    )


def _assign_batch(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.post(
        "/api/v1/tracking/assign/batch",
        json={
            "experiment_key": sdk.experiment_key,
            "users": [{"user_id": f"u-{uuid.uuid4().hex[:12]}"}],
        },
        headers=sdk.headers,
    )


def _errors(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.post("/api/v1/tracking/errors", json={}, headers=sdk.headers)


def _errors_batch(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.post("/api/v1/tracking/errors/batch", json={}, headers=sdk.headers)


#: Events each ``tracking-batch`` request sends, and reports each
#: ``errors-batch-valid`` request sends.
BATCH_EVENTS = 5
BATCH_REPORTS = 2


def _new_event_id() -> str:
    return f"e-{uuid.uuid4().hex}"


def _event(sdk: SdkSetup) -> dict:
    """An event of the user the fixture assigned, with an event_id of its own."""
    return {
        "event_type": "overload",
        "user_id": sdk.assigned_user,
        "experiment_key": sdk.experiment_key,
        "event_id": _new_event_id(),
    }


def _track(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.post("/api/v1/tracking/track", json=_event(sdk), headers=sdk.headers)


def _events(http: httpx.AsyncClient, sdk: SdkSetup):
    """An event by ids, of a new user, naming the experiment and its treatment."""
    return http.post(
        "/api/v1/tracking/events",
        json={
            "event_name": "overload",
            "event_type": "overload",
            "user_id": _new_user(),
            "experiment_id": str(sdk.experiment_id),
            "variant_id": str(sdk.assigned_variant_id),
            "event_id": _new_event_id(),
        },
        headers=sdk.headers,
    )


def _tracking_batch(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.post(
        "/api/v1/tracking/batch",
        json={"events": [_event(sdk) for _ in range(BATCH_EVENTS)]},
        headers=sdk.headers,
    )


def _user_assignments(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.get(
        f"/api/v1/tracking/assignments/{sdk.assigned_user}", headers=sdk.headers
    )


def _report(sdk: SdkSetup) -> dict:
    return {
        "feature_flag_key": sdk.flag_key,
        "user_id": _new_user(),
        "error_type": "overload",
        "message": "reported in a burst",
    }


def _errors_valid(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.post("/api/v1/tracking/errors", json=_report(sdk), headers=sdk.headers)


def _errors_batch_valid(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.post(
        "/api/v1/tracking/errors/batch",
        json={"errors": [_report(sdk) for _ in range(BATCH_REPORTS)]},
        headers=sdk.headers,
    )


def _split_url_routes(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.get("/api/v1/tracking/split-url/routes", headers=sdk.headers)


def _evaluate(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.get(
        f"/api/v1/feature-flags/evaluate/{sdk.flag_key}",
        params={"user_id": f"u-{uuid.uuid4().hex[:12]}"},
        headers=sdk.headers,
    )


def _assign_balanced(http: httpx.AsyncClient, sdk: SdkSetup):
    """A new user of the experiment that balances within ``tier``."""
    return http.post(
        "/api/v1/tracking/assign",
        json={
            "experiment_key": sdk.balanced_experiment_key,
            "user_id": f"u-{uuid.uuid4().hex[:12]}",
            "context": {"tier": "gold"},
        },
        headers=sdk.headers,
    )


def _every_user_took_a_place_in_the_blocks(
    db_session, sdk: SdkSetup, experiment_id: Any = None
) -> None:
    """After the balanced row: every request assigned a new user in group
    ``gold``, each with a place, the places exactly 0 .. n - 1."""
    db_session.expire_all()
    rows = (
        db_session.query(Assignment.balance_group, Assignment.balance_seq)
        .filter(
            Assignment.experiment_id == (experiment_id or sdk.balanced_experiment_id)
        )
        .all()
    )
    sent = BURST + 2
    assert len(rows) == sent, f"{len(rows)} users assigned, want {sent}"
    groups = Counter(group for group, _ in rows)
    assert groups == {"gold": sent}, f"groups {dict(groups)}, want {{'gold': {sent}}}"
    seqs = sorted(seq for _, seq in rows if seq is not None)
    assert seqs == list(range(sent)), (
        f"{sent - len(seqs)} users outside the blocks; places {seqs}"
    )


def _visual(http: httpx.AsyncClient, sdk: SdkSetup):
    """A new visitor on the visual experiment's page: an assignment and an
    exposure are written on every call."""
    return http.post(
        "/api/v1/tracking/visual",
        json={
            "user_id": f"u-{uuid.uuid4().hex[:12]}",
            "host": sdk.visual_host,
            "path": "/",
            "format_version": 1,
        },
        headers=sdk.page_headers,
    )


def _visual_preview(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.post(
        "/api/v1/tracking/visual/preview",
        json={"token": sdk.preview_token, "host": sdk.visual_host, "path": "/"},
        headers=sdk.page_headers,
    )


def _visual_balanced(http: httpx.AsyncClient, sdk: SdkSetup):
    """A new visitor in ``tier`` gold on the balanced visual experiment's page:
    assigned inside the page answer's worker thread."""
    return http.post(
        "/api/v1/tracking/visual",
        json={
            "user_id": f"u-{uuid.uuid4().hex[:12]}",
            "host": sdk.balanced_visual_host,
            "path": "/",
            "format_version": 1,
            "context": {"tier": "gold"},
        },
        headers=sdk.page_headers,
    )


def _every_visitor_took_a_place_in_the_blocks(db_session, sdk: SdkSetup) -> None:
    """After the balanced visual row: as for the balanced row."""
    _every_user_took_a_place_in_the_blocks(db_session, sdk, sdk.balanced_visual_id)


def _one_event_stored_per_event_sent(per_request: int):
    """After an event row: the experiment's stored events, one per event sent
    (each request before, in and after the burst sends *per_request*)."""

    def check(db_session, sdk: SdkSetup) -> None:
        db_session.expire_all()
        stored = (
            db_session.query(Event)
            .filter(Event.experiment_id == sdk.experiment_id)
            .count()
        )
        want = (BURST + 2) * per_request
        assert stored == want, f"{stored} events stored, want {want}"

    return check


def _one_report_stored_per_report_sent(per_request: int):
    """After an error report row: the flag's stored reports, one per report
    sent, each stamped ``"server"``."""

    def check(db_session, sdk: SdkSetup) -> None:
        db_session.expire_all()
        sources = Counter(
            (row.request_data or {}).get("source")
            for row in db_session.query(ErrorLog).filter(
                ErrorLog.feature_flag_id == sdk.flag_id
            )
        )
        want = (BURST + 2) * per_request
        assert sources == {"server": want}, (
            f"stored reports by source {dict(sources)}, want {{'server': {want}}}"
        )

    return check


def _tagged_with_the_assigned_variant(before: httpx.Response, db_session, sdk):
    variant, want = before.json().get("variant_id"), str(sdk.assigned_variant_id)
    assert variant == want, (
        f"the event's variant_id {variant!r}, want the assigned user's {want!r}"
    )


def _stored_by_ids(before: httpx.Response, db_session, sdk):
    answer, want = before.json(), str(sdk.experiment_id)
    assert answer.get("experiment_id") == want, answer
    assert answer.get("duplicate") is False, answer


def _every_event_of_the_batch_stored(before: httpx.Response, db_session, sdk):
    counts = {
        name: before.json().get(name)
        for name in ("success_count", "failure_count", "duplicate_count")
    }
    want = {"success_count": BATCH_EVENTS, "failure_count": 0, "duplicate_count": 0}
    assert counts == want, f"{counts}, want {want}: {before.text}"


def _the_experiment_is_listed(before: httpx.Response, db_session, sdk):
    listed = [entry.get("experiment_id") for entry in before.json()]
    want = [str(sdk.experiment_id)]
    assert listed == want, f"the assigned user's experiments {listed}, want {want}"


def _stored_as_a_server_report(before: httpx.Response, db_session, sdk):
    row = db_session.get(ErrorLog, uuid.UUID(before.json()["id"]))
    source = (row.request_data or {}).get("source") if row is not None else None
    assert source == "server", f"the stored report's source {source!r}, want 'server'"


def _every_report_of_the_batch_stored(before: httpx.Response, db_session, sdk):
    counts = {
        name: before.json().get(name) for name in ("success_count", "failure_count")
    }
    want = {"success_count": BATCH_REPORTS, "failure_count": 0}
    assert counts == want, f"{counts}, want {want}: {before.text}"


#: (label, request, the route's normal status, a check after the burst or
#: None). The ``errors`` and ``errors-batch`` rows send ``{}``, refused after
#: the key check; ``errors-valid`` and ``errors-batch-valid`` send valid
#: reports, which are stored. ``assign-balanced`` sends new users of an
#: experiment that balances within groups (#448): a balanced burst is
#: answered, and every user of it takes a place in their group's blocks.
ROUTES = [
    pytest.param("GET /api/v1/sdk/ruleset", _ruleset, 200, None, id="ruleset"),
    pytest.param(
        "POST /api/v1/tracking/evaluations",
        _evaluations,
        201,
        None,
        id="evaluations",
    ),
    pytest.param("POST /api/v1/tracking/assign", _assign, 200, None, id="assign"),
    pytest.param(
        "POST /api/v1/tracking/split-url/assign",
        _split_url_assign,
        200,
        None,
        id="split-url-assign",
        marks=pytest.mark.modules,
    ),
    pytest.param(
        "POST /api/v1/tracking/assign/batch",
        _assign_batch,
        200,
        None,
        id="assign-batch",
    ),
    pytest.param(
        "POST /api/v1/tracking/assign (balanced)",
        _assign_balanced,
        200,
        _every_user_took_a_place_in_the_blocks,
        id="assign-balanced",
    ),
    pytest.param("POST /api/v1/tracking/errors", _errors, 422, None, id="errors"),
    pytest.param(
        "POST /api/v1/tracking/errors/batch",
        _errors_batch,
        422,
        None,
        id="errors-batch",
    ),
    pytest.param(
        "POST /api/v1/tracking/track",
        _track,
        200,
        _one_event_stored_per_event_sent(1),
        id="track",
    ),
    pytest.param(
        "POST /api/v1/tracking/events",
        _events,
        200,
        _one_event_stored_per_event_sent(1),
        id="events",
    ),
    pytest.param(
        "POST /api/v1/tracking/batch",
        _tracking_batch,
        200,
        _one_event_stored_per_event_sent(BATCH_EVENTS),
        id="tracking-batch",
    ),
    pytest.param(
        "GET /api/v1/tracking/assignments/{user_id}",
        _user_assignments,
        200,
        None,
        id="assignments",
    ),
    pytest.param(
        "POST /api/v1/tracking/errors (a valid report)",
        _errors_valid,
        201,
        _one_report_stored_per_report_sent(1),
        id="errors-valid",
    ),
    pytest.param(
        "POST /api/v1/tracking/errors/batch (valid reports)",
        _errors_batch_valid,
        200,
        _one_report_stored_per_report_sent(BATCH_REPORTS),
        id="errors-batch-valid",
    ),
    pytest.param("POST /api/v1/tracking/visual", _visual, 200, None, id="visual"),
    pytest.param(
        "POST /api/v1/tracking/visual/preview",
        _visual_preview,
        200,
        None,
        id="visual-preview",
    ),
    pytest.param(
        "POST /api/v1/tracking/visual (balanced)",
        _visual_balanced,
        200,
        _every_visitor_took_a_place_in_the_blocks,
        id="visual-balanced",
    ),
    pytest.param(
        "GET /api/v1/tracking/split-url/routes",
        _split_url_routes,
        200,
        None,
        id="split-url-routes",
        marks=pytest.mark.modules,
    ),
]

#: The ``reason`` the request before the burst must carry, by label.
REASON_BEFORE = {
    "POST /api/v1/tracking/assign": "assigned",
    "POST /api/v1/tracking/split-url/assign": "assigned",
}

#: What the answer to the request before the burst must show, by label: that
#: the route did its normal work rather than an early answer.
CHECK_BEFORE = {
    "POST /api/v1/tracking/track": _tagged_with_the_assigned_variant,
    "POST /api/v1/tracking/events": _stored_by_ids,
    "POST /api/v1/tracking/batch": _every_event_of_the_batch_stored,
    "GET /api/v1/tracking/assignments/{user_id}": _the_experiment_is_listed,
    "POST /api/v1/tracking/errors (a valid report)": _stored_as_a_server_report,
    "POST /api/v1/tracking/errors/batch (valid reports)": (
        _every_report_of_the_batch_stored
    ),
}


@contextlib.asynccontextmanager
async def _worker_limit(tokens: int):
    """Set the running loop's default worker limit to *tokens*, then restore it."""
    limiter = anyio.to_thread.current_default_thread_limiter()
    saved = limiter.total_tokens
    limiter.total_tokens = tokens
    try:
        yield
    finally:
        limiter.total_tokens = saved


def _limit_seen_by_a_worker() -> float:
    """Run in a worker: the default worker limit of the loop it serves."""
    return anyio.from_thread.run_sync(
        lambda: anyio.to_thread.current_default_thread_limiter().total_tokens
    )


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _outcome(result: Any) -> Any:
    if isinstance(result, httpx.Response):
        return result.status_code
    return type(result).__name__


async def _gather(requests: List[Awaitable[httpx.Response]]) -> List[Any]:
    """Every request at once; the status of each, or its exception's class."""
    with anyio.fail_after(BURST_DEADLINE_SECONDS):
        results = await asyncio.gather(*requests, return_exceptions=True)
    return [_outcome(result) for result in results]


async def _drain(watch: PoolWatch) -> None:
    deadline = time.monotonic() + DRAIN_SECONDS
    while watch.engine.pool.checkedout() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


@pytest.mark.regression
@pytest.mark.parametrize("label, send, status, check", ROUTES)
async def test_every_request_of_a_burst_is_answered(
    label: str,
    send: Send,
    status: int,
    check: Optional[Callable[[Any, SdkSetup], None]],
    small_pool: PoolWatch,
    sdk: SdkSetup,
    no_active_holdout,
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(balanced_allocation, "BALANCE_STEP_TIMEOUT_MS", STEP_TIMEOUT_MS)
    watch = small_pool
    watch.loop_thread = threading.get_ident()
    async with _worker_limit(WORKERS), _client() as http:
        try:
            # The limit is the one the app's workers run under.
            assert await run_in_threadpool(_limit_seen_by_a_worker) == WORKERS
            before = await send(http, sdk)
            assert before.status_code == status, (
                f"{label} before the burst: {before.status_code} {before.text}"
            )
            if label in REASON_BEFORE:
                reason = before.json().get("reason")
                assert reason == REASON_BEFORE[label], (
                    f"{label} before the burst: reason {reason!r}, want "
                    f"{REASON_BEFORE[label]!r}: {before.text}"
                )
            if label in CHECK_BEFORE:
                CHECK_BEFORE[label](before, db_session, sdk)
            watch.restart()
            outcomes = await _gather([send(http, sdk) for _ in range(BURST)])
            peak = watch.peak
            on_loop = watch.on_loop_thread()
            after = await send(http, sdk)
        finally:
            await _drain(watch)

    tally = Counter(outcomes)
    assert tally == {status: BURST}, (
        f"{BURST} concurrent {label} over {CONNECTIONS} connections and "
        f"{WORKERS} workers: {dict(tally)}, want {{{status}: {BURST}}}"
    )
    assert peak == CONNECTIONS, (
        f"peak checkouts during the burst {peak}, want {CONNECTIONS}: the burst "
        "did not use every connection, so it proves nothing"
    )
    assert on_loop == 0, (
        f"{on_loop} checkouts on the event loop's thread during {BURST} {label}"
    )
    # A sanity check only: in-process, the client and the app end together.
    assert after.status_code == status, (
        f"{label} after the burst: {after.status_code} {after.text}"
    )
    if check is not None:
        check(db_session, sdk)


@pytest.mark.regression
async def test_a_ruleset_burst_leaves_flag_evaluation_answering(
    small_pool: PoolWatch, sdk: SdkSetup
):
    rulesets, evaluations = 16, 8
    async with _worker_limit(WORKERS), _client() as http:
        try:
            for send in (_ruleset, _evaluate):
                response = await send(http, sdk)
                assert response.status_code == 200, response.text
            outcomes = await _gather(
                [_ruleset(http, sdk) for _ in range(rulesets)]
                + [_evaluate(http, sdk) for _ in range(evaluations)]
            )
        finally:
            await _drain(small_pool)

    ruleset_tally = Counter(outcomes[:rulesets])
    evaluate_tally = Counter(outcomes[rulesets:])
    assert evaluate_tally == {200: evaluations}, (
        f"{evaluations} GET /api/v1/feature-flags/evaluate/{{flag_key}} sent with "
        f"{rulesets} GET /api/v1/sdk/ruleset: {dict(evaluate_tally)}, "
        f"want {{200: {evaluations}}}"
    )
    assert ruleset_tally == {200: rulesets}, (
        f"{rulesets} GET /api/v1/sdk/ruleset sent with {evaluations} flag "
        f"evaluations: {dict(ruleset_tally)}, want {{200: {rulesets}}}"
    )
