"""How a balanced experiment assigns its new users (#448).

* Ineligible users take no place: with a measurable global holdout and a
  mutual exclusion group, the seqs of every group are still exactly
  ``0 .. n - 1`` over the users assigned, and the holdout population records
  every user seen, as it does for any experiment.
* A sticky user keeps their variant and their group whatever ``context`` a
  later call sends, takes no place, and the exposure carries the new context;
  a list sent again assigns nobody new.
* A counter row another connection holds past the step's statement timeout:
  the waiting user is answered 200 by the hash, keeps their group with no
  seq, is counted under ``timeout`` and logged once; the group's places are
  untouched. The same from a ``BEFORE UPDATE`` trigger that sleeps, which a
  ``lock_timeout`` would not bound.
* An experiment that balances nothing is assigned exactly as before: every
  user gets the variant ``tests/ground_truth``'s own copy of the hash gives,
  and the allocator is never called. No assign sends a Redis command.
* A held counter row holds up nothing else, and the per-process limit holds:
  with the row held, ``MAX_BALANCED_WAITS + 8`` new users of that group are
  answered (the limit's worth wait in the thread pool, the rest are assigned
  by hash at once as ``busy``) while other requests, a flag evaluate among
  them, are answered; for ``/tracking/assign`` and ``/tracking/assign/batch``.
  No timing is asserted: the state is polled to, with a liveness bound. An
  uncontended balanced assign runs its counter statement off the event
  loop's thread.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Tuple

import httpx
import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.metrics import experiment_balance_fallback_total
from backend.app.db import session as db_session_module
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.event import Event, EventType
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    Metric,
    MetricType,
    Variant,
)
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.holdout_population import HoldoutPopulation
from backend.app.models.mutual_exclusion_group import (
    MutualExclusionGroup,
    MutualExclusionGroupStatus,
)
from backend.app.models.user import User, UserRole
from backend.app.services import balanced_allocation
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.global_holdout_service import GlobalHoldoutService
from backend.tests.integration.balance_checks import balance_rows
from backend.tests.unit.ground_truth.library import hashing

pytestmark = pytest.mark.integration

SCHEMA = "test_experimentation"
ASSIGN = "/api/v1/tracking/assign"
BATCH = "/api/v1/tracking/assign/batch"
TIER = {"attribute": "tier", "values": ["gold", "silver"]}
LOGGER = "backend.app.services.balanced_allocation"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_sign_in(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture(autouse=True)
def _no_active_holdout(db_session):
    """No global holdout for the test unless it makes one; restored after."""
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


@pytest.fixture
def session_factory(test_db):
    factory = sessionmaker(bind=test_db, autocommit=False, autoflush=False)

    def make():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return make


@pytest.fixture
def client(session_factory) -> Iterator[TestClient]:
    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


def _owner(db_session) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"bal_{suffix}",
        email=f"bal_{suffix}@balance.test",
        full_name="Balanced assignment",
        hashed_password="unused: this user signs in by key only",
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def key(db_session) -> Dict[str, str]:
    """An ``sdk:ruleset`` key of a developer: valid on /assign and /assign/batch."""
    _row, plaintext = APIKey.create_for_user(
        db_session,
        _owner(db_session).id,
        f"bal-{uuid.uuid4().hex[:8]}",
        scopes="sdk:ruleset",
    )
    return {"X-API-Key": plaintext}


def _experiment(
    db_session,
    admin_user,
    *,
    setting: Optional[dict] = TIER,
    allocations=(50, 50),
    group_id=None,
) -> Experiment:
    """An ACTIVE experiment with its variants, committed."""
    suffix = uuid.uuid4().hex[:8]
    experiment = Experiment(
        name=f"Balanced {suffix}",
        key=f"balanced-{suffix}",
        status=ExperimentStatus.ACTIVE,
        owner_id=admin_user.id,
        balance_within=setting,
        mutual_exclusion_group_id=group_id,
    )
    db_session.add(experiment)
    db_session.flush()
    for index, share in enumerate(allocations):
        db_session.add(
            Variant(
                experiment_id=experiment.id,
                name="control" if index == 0 else f"treatment{index}",
                is_control=index == 0,
                traffic_allocation=share,
            )
        )
    db_session.add(
        Metric(
            experiment_id=experiment.id,
            name="Purchase",
            event_name="purchase",
            metric_type=MetricType.CONVERSION,
            is_primary=True,
        )
    )
    db_session.commit()
    db_session.refresh(experiment)
    return experiment


def _assign(client, key, experiment, user_id, context=None):
    body = {"experiment_key": experiment.key, "user_id": user_id}
    if context is not None:
        body["context"] = context
    return client.post(ASSIGN, json=body, headers=key)


def _rows(session_factory, experiment_id):
    session = session_factory()
    try:
        return balance_rows(session, experiment_id)
    finally:
        session.close()


def _user_rows(session_factory, experiment_id, user_id) -> List[Tuple]:
    session = session_factory()
    try:
        return [
            tuple(row)
            for row in session.execute(
                text(
                    "SELECT variant_id, balance_group, balance_seq FROM assignments "
                    "WHERE experiment_id = :e AND user_id = :u"
                ),
                {"e": str(experiment_id), "u": user_id},
            )
        ]
    finally:
        session.close()


def _next_seq(session_factory, experiment_id, group) -> Optional[int]:
    session = session_factory()
    try:
        return session.execute(
            text(
                "SELECT next_seq FROM balance_group_counters "
                "WHERE experiment_id = :e AND balance_group = :g"
            ),
            {"e": str(experiment_id), "g": group},
        ).scalar()
    finally:
        session.close()


def _fallbacks(reason: str) -> float:
    return experiment_balance_fallback_total.labels(reason=reason)._value.get()


# ---------------------------------------------------------------------------
# Ineligible users take no place
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_ineligible_users_take_no_place(
    client, key, db_session, admin_user, session_factory
):
    group = MutualExclusionGroup(
        name=f"bal-meg-{uuid.uuid4().hex[:8]}",
        description="balanced eligibility",
        traffic_allocation=1.0,
        status=MutualExclusionGroupStatus.ACTIVE,
    )
    db_session.add(group)
    db_session.commit()
    balanced = _experiment(db_session, admin_user, group_id=group.id)
    _experiment(db_session, admin_user, setting=None, group_id=group.id)
    holdout = GlobalHoldoutService(db_session).create_holdout(
        name=f"bal-holdout-{uuid.uuid4().hex[:8]}",
        holdout_percentage=20,
        is_active=True,
    )
    assert holdout.is_measurable
    try:
        answers = {}
        tiers = ["gold", "silver", "bronze", None]
        for i in range(160):
            user_id = f"elig-{uuid.uuid4().hex[:10]}"
            tier = tiers[i % 4]
            response = _assign(
                client, key, balanced, user_id, {"tier": tier} if tier else None
            )
            assert response.status_code == 200, response.text
            answers[user_id] = (tier, response.json())

        reasons = {}
        for _tier, body in answers.values():
            reasons[body["reason"]] = reasons.get(body["reason"], 0) + 1
        assert reasons.get("holdout", 0) > 0 and reasons.get("mutual_exclusion", 0) > 0
        assigned = {u: t for u, (t, body) in answers.items() if body["assigned"]}

        found = _rows(session_factory, balanced.id)
        assert found.problems == [], found.problems[:10]
        assert found.rows == len(assigned)
        assert found.outside_blocks() == 0
        want = {"gold": 0, "silver": 0, "other": 0, "unknown": 0}
        for tier in assigned.values():
            want[
                {"gold": "gold", "silver": "silver", None: "unknown"}.get(tier, "other")
            ] += 1
        assert {g: rows.next_seq for g, rows in found.groups.items()} == {
            g: n for g, n in want.items() if n
        }

        session = session_factory()
        try:
            population = dict(
                session.query(HoldoutPopulation.user_id, HoldoutPopulation.in_holdout)
                .filter(
                    HoldoutPopulation.holdout_id == holdout.id,
                    HoldoutPopulation.user_id.in_(list(answers)),
                )
                .all()
            )
        finally:
            session.close()
        assert set(population) == set(answers), "a user seen is not in the population"
        assert sum(population.values()) == reasons["holdout"]
    finally:
        db_session.rollback()
        db_session.query(GlobalHoldout).filter(GlobalHoldout.id == holdout.id).delete()
        db_session.commit()


# ---------------------------------------------------------------------------
# Sticky users
# ---------------------------------------------------------------------------

DEVICE = {"attribute": "web_device"}


@pytest.mark.regression
def test_a_sticky_user_keeps_their_variant_and_group(
    client, key, db_session, admin_user, session_factory
):
    experiment = _experiment(db_session, admin_user, setting=DEVICE)
    others = [f"phone-{i}-{uuid.uuid4().hex[:8]}" for i in range(2)]
    for user_id in others:
        assert _assign(
            client, key, experiment, user_id, {"web_device": "phone"}
        ).json()["assigned"]
    user_id = f"sticky-{uuid.uuid4().hex[:10]}"

    first = _assign(client, key, experiment, user_id, {"web_device": "phone"})
    again = _assign(client, key, experiment, user_id, {"web_device": "desktop"})

    assert first.status_code == again.status_code == 200, again.text
    assert again.json()["variant_id"] == first.json()["variant_id"]
    ((variant_id, group, seq),) = _user_rows(session_factory, experiment.id, user_id)
    assert (str(variant_id), group, seq) == (first.json()["variant_id"], "phone", 2)
    found = _rows(session_factory, experiment.id)
    assert found.problems == [], found.problems
    desktop = found.groups.get("desktop")
    assert desktop is None, f"desktop rows {desktop.next_seq} != 0"
    assert found.groups["phone"].next_seq == 3

    session = session_factory()
    try:
        exposures = [
            properties
            for (properties,) in session.query(Event.event_metadata).filter(
                Event.experiment_id == experiment.id,
                Event.user_id == user_id,
                Event.event_type == EventType.EXPOSURE.value,
            )
        ]
    finally:
        session.close()
    assert {"web_device": "desktop"} in [
        {k: v for k, v in (p or {}).items() if k == "web_device"} for p in exposures
    ], exposures

    batch = client.post(
        BATCH,
        json={
            "experiment_key": experiment.key,
            "users": [
                {"user_id": u, "context": {"web_device": "desktop"}} for u in others
            ]
            + [{"user_id": user_id, "context": {"web_device": "tablet"}}],
        },
        headers=key,
    )
    assert batch.status_code == 200, batch.text
    assert _rows(session_factory, experiment.id).rows == 3, (
        "a sticky user got a new row"
    )
    assert _rows(session_factory, experiment.id).groups["phone"].next_seq == 3


@pytest.mark.regression
def test_a_list_and_single_assigns_share_each_groups_places(
    client, key, db_session, admin_user, session_factory
):
    """``/assign`` and ``/assign/batch`` take places from the same groups: a
    list sent in chunks between single assigns leaves each group's places
    exactly 0 .. n - 1, every row the place of its seq, at 50/50 the two
    variants of each group at most one user apart."""
    experiment = _experiment(db_session, admin_user)
    tiers = ["gold", "silver", "bronze", None]
    users = [(f"mix-{i:03d}-{uuid.uuid4().hex[:6]}", tiers[i % 4]) for i in range(90)]

    def context(tier):
        return {"tier": tier} if tier else {}

    for start in range(0, len(users), 30):
        chunk = users[start : start + 30]
        batch = client.post(
            BATCH,
            json={
                "experiment_key": experiment.key,
                "users": [{"user_id": u, "context": context(t)} for u, t in chunk[:20]],
            },
            headers=key,
        )
        assert batch.status_code == 200, batch.text
        assert batch.json()["counts"]["assigned"] == 20
        for user_id, tier in chunk[20:]:
            single = _assign(client, key, experiment, user_id, context(tier))
            assert single.status_code == 200, single.text
            assert single.json()["assigned"] is True

    found = _rows(session_factory, experiment.id)
    assert found.problems == [], found.problems
    assert found.rows == len(users)
    assert found.outside_blocks() == 0
    sizes = {"gold": 0, "silver": 0, "other": 0, "unknown": 0}
    for _user, tier in users:
        sizes[
            {"gold": "gold", "silver": "silver", None: "unknown"}.get(tier, "other")
        ] += 1
    for group, size in sizes.items():
        assert found.groups[group].next_seq == size
        assert found.gap(group, "control", "treatment1") == size % 2, group


# ---------------------------------------------------------------------------
# A counter row held past the step's timeout
# ---------------------------------------------------------------------------


@contextmanager
def _held_row(test_db, experiment_id, group: str) -> Iterator[threading.Event]:
    """Hold ``(experiment_id, group)``'s counter row from a second connection
    until the yielded event is set (or 60 s pass)."""
    held, release = threading.Event(), threading.Event()
    failure: List[BaseException] = []

    def hold():
        try:
            with test_db.connect() as connection:
                transaction = connection.begin()
                connection.execute(text(f"SET search_path TO {SCHEMA}"))
                connection.execute(
                    text(
                        "SELECT next_seq FROM balance_group_counters "
                        "WHERE experiment_id = :e AND balance_group = :g FOR UPDATE"
                    ),
                    {"e": str(experiment_id), "g": group},
                ).one()
                held.set()
                release.wait(60)
                transaction.rollback()
        except BaseException as exc:  # pragma: no cover - reported below
            failure.append(exc)
            held.set()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert held.wait(30), "the row was never held"
    assert not failure, failure
    try:
        yield release
    finally:
        release.set()
        thread.join(30)


def _seed_counter(session_factory, experiment_id, group: str, next_seq: int = 0):
    session = session_factory()
    try:
        session.execute(
            text(
                "INSERT INTO balance_group_counters (experiment_id, balance_group, "
                "next_seq) VALUES (:e, :g, :n)"
            ),
            {"e": str(experiment_id), "g": group, "n": next_seq},
        )
        session.commit()
    finally:
        session.close()


@pytest.mark.regression
def test_a_held_counter_row_is_answered_by_hash(
    client, key, db_session, admin_user, session_factory, test_db, caplog
):
    experiment = _experiment(db_session, admin_user)
    assert _assign(
        client, key, experiment, f"first-{uuid.uuid4().hex[:8]}", {"tier": "gold"}
    ).json()["assigned"]
    user_id = f"waiter-{uuid.uuid4().hex[:10]}"
    hashed = AssignmentService(db_session)._hash_user_to_variant(user_id, experiment)
    before = _fallbacks("timeout")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        with _held_row(test_db, experiment.id, "gold"):
            response = _assign(client, key, experiment, user_id, {"tier": "gold"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["assigned"], body["reason"]) == (True, "assigned")
    assert body["variant_id"] == str(hashed)
    assert _user_rows(session_factory, experiment.id, user_id) == [
        (hashed, "gold", None)
    ]
    assert _fallbacks("timeout") == before + 1
    lines = [r for r in caplog.records if str(experiment.id) in r.getMessage()]
    assert len(lines) == 1, [r.getMessage() for r in lines]
    assert user_id not in lines[0].getMessage()
    assert _next_seq(session_factory, experiment.id, "gold") == 1
    found = _rows(session_factory, experiment.id)
    assert found.problems == [], found.problems
    assert found.outside_blocks() == 1


@contextmanager
def _sleeping_update_trigger(session_factory, experiment_id) -> Iterator[None]:
    """A test-only ``BEFORE UPDATE`` trigger on the counters that sleeps 0.3 s
    for this experiment's rows: a statement that runs long holding no lock
    anyone else waits for."""
    name = f"bal_sleep_{uuid.uuid4().hex[:8]}"
    session = session_factory()
    try:
        session.execute(
            text(
                f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN IF NEW.experiment_id = '"
                + str(experiment_id)
                + "'::uuid THEN PERFORM pg_sleep(0.3); END IF; RETURN NEW; END $$"
            )
        )
        session.execute(
            text(
                f"CREATE TRIGGER {name} BEFORE UPDATE ON balance_group_counters "
                f"FOR EACH ROW EXECUTE FUNCTION {name}()"
            )
        )
        session.commit()
        yield
    finally:
        session.rollback()
        session.execute(
            text(f"DROP TRIGGER IF EXISTS {name} ON balance_group_counters")
        )
        session.execute(text(f"DROP FUNCTION IF EXISTS {name}()"))
        session.commit()
        session.close()


@pytest.mark.regression
def test_a_slow_counter_statement_falls_back_too(
    client, key, db_session, admin_user, session_factory
):
    """Deterministic: the counter row is seeded, so the step takes the UPDATE
    path and the trigger sleeps past the 250 ms statement timeout. A
    ``lock_timeout`` would not end it (no lock is waited for)."""
    experiment = _experiment(db_session, admin_user)
    _seed_counter(session_factory, experiment.id, "silver", 0)
    user_id = f"slow-{uuid.uuid4().hex[:10]}"
    before = _fallbacks("timeout")

    with _sleeping_update_trigger(session_factory, experiment.id):
        response = _assign(client, key, experiment, user_id, {"tier": "silver"})

    assert response.status_code == 200, response.text
    assert response.json()["assigned"] is True
    ((_variant, group, seq),) = _user_rows(session_factory, experiment.id, user_id)
    assert group == "silver"
    assert seq is None, f"balance_seq {seq} is not None"
    assert _fallbacks("timeout") == before + 1
    assert _next_seq(session_factory, experiment.id, "silver") == 0


@pytest.mark.regression
def test_an_experiment_with_no_key_is_answered_by_hash(
    client, key, db_session, admin_user, session_factory
):
    """A balanced experiment found without its block-order key (a row written
    past the ORM) is assigned outside the blocks, never a 500."""
    experiment = _experiment(db_session, admin_user)
    db_session.execute(
        text("UPDATE experiments SET balance_seed = NULL WHERE id = :id"),
        {"id": str(experiment.id)},
    )
    db_session.commit()
    user_id = f"noseed-{uuid.uuid4().hex[:10]}"
    before = _fallbacks("no_seed")

    response = _assign(client, key, experiment, user_id, {"tier": "gold"})

    assert response.status_code == 200, response.text
    assert response.json()["assigned"] is True
    ((_variant, group, seq),) = _user_rows(session_factory, experiment.id, user_id)
    assert (group, seq) == ("gold", None)
    assert _fallbacks("no_seed") == before + 1


# ---------------------------------------------------------------------------
# An experiment that balances nothing is assigned as before
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_an_unbalanced_experiment_is_assigned_by_the_hash_alone(
    client, key, db_session, admin_user, session_factory, monkeypatch
):
    experiment = _experiment(
        db_session, admin_user, setting=None, allocations=(34, 33, 33)
    )
    calls: List[str] = []
    for name in ("take_seq", "slot_variant", "group_of"):
        real = getattr(balanced_allocation, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(balanced_allocation, name, spy)
    allocations = [("control", 34), ("treatment1", 33), ("treatment2", 33)]
    golden = hashing  # tests/ground_truth's copy, not the product's
    users = [f"gt-{i:04d}-{uuid.uuid4().hex[:6]}" for i in range(240)]

    mismatches = 0
    for user_id in users:
        response = _assign(client, key, experiment, user_id, {"tier": "gold"})
        assert response.status_code == 200, response.text
        if response.json()["variant_name"] != golden.variant_for(
            user_id, experiment.key, allocations
        ):
            mismatches += 1

    assert mismatches == 0, f"mismatches {mismatches} != 0"
    assert calls == [], f"the allocator was called {len(calls)} times"
    found = _rows(session_factory, experiment.id)
    assert found.rows == len(users)
    assert set(found.groups) == {None}, "an unbalanced row carries a group"


@pytest.mark.regression
@pytest.mark.parametrize("setting", [None, TIER], ids=["unbalanced", "balanced"])
def test_no_assign_sends_a_redis_command(
    client, key, db_session, admin_user, monkeypatch, setting
):
    sent: List[tuple] = []
    real = redis.Redis.execute_command

    def counting(self, *args, **kwargs):
        sent.append(args[:1])
        return real(self, *args, **kwargs)

    monkeypatch.setattr(redis.Redis, "execute_command", counting)
    experiment = _experiment(db_session, admin_user, setting=setting)

    for _ in range(5):
        response = _assign(
            client, key, experiment, f"redis-{uuid.uuid4().hex[:10]}", {"tier": "gold"}
        )
        assert response.status_code == 200, response.text

    assert sent == [], sent


# ---------------------------------------------------------------------------
# A held counter row holds up nothing else, and the limit holds
# ---------------------------------------------------------------------------

#: A liveness bound for reaching the held state, never asserted as a time.
LIVENESS_SECONDS = 20
HELD_STEP_TIMEOUT_MS = 5000


@pytest.fixture
def production_pool(test_db, monkeypatch):
    """The production pool's shape (20 + 20) on the test database, behind both
    ``deps.SessionLocal`` and ``db.session.SessionLocal``."""
    engine = create_engine(test_db.url, pool_size=20, max_overflow=20, pool_timeout=30)
    assert isinstance(engine.pool, QueuePool)

    @event.listens_for(engine, "connect")
    def _search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET search_path TO {SCHEMA}")
        cursor.close()

    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(deps, "SessionLocal", factory)
    monkeypatch.setattr(db_session_module, "SessionLocal", factory)
    try:
        yield engine
    finally:
        engine.dispose()


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


@pytest.mark.regression
@pytest.mark.parametrize("route", ["assign", "assign/batch"])
async def test_a_held_counter_row_holds_up_nothing_else(
    route,
    production_pool,
    key,
    db_session,
    admin_user,
    session_factory,
    test_db,
    make_feature_flag,
    monkeypatch,
):
    monkeypatch.setattr(
        balanced_allocation, "BALANCE_STEP_TIMEOUT_MS", HELD_STEP_TIMEOUT_MS
    )
    experiment = _experiment(db_session, admin_user)
    _seed_counter(session_factory, experiment.id, "gold", 0)
    flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=100)
    senders = balanced_allocation.MAX_BALANCED_WAITS + 8
    users = [f"held-{i:02d}-{uuid.uuid4().hex[:8]}" for i in range(senders)]
    busy_before, timeout_before = _fallbacks("busy"), _fallbacks("timeout")
    balanced_allocation.reset_balanced_wait_peak()

    def send(http, user_id):
        if route == "assign":
            return http.post(
                ASSIGN,
                json={
                    "experiment_key": experiment.key,
                    "user_id": user_id,
                    "context": {"tier": "gold"},
                },
                headers=key,
            )
        return http.post(
            BATCH,
            json={
                "experiment_key": experiment.key,
                "users": [{"user_id": user_id, "context": {"tier": "gold"}}],
            },
            headers=key,
        )

    with _held_row(test_db, experiment.id, "gold") as release:
        async with _http() as http:
            pending = [asyncio.create_task(send(http, u)) for u in users]
            reached = False
            deadline = time.monotonic() + LIVENESS_SECONDS
            while time.monotonic() < deadline:
                waits = balanced_allocation.balanced_waits()["current"]
                busy = _fallbacks("busy") - busy_before
                if waits == balanced_allocation.MAX_BALANCED_WAITS and busy == 8:
                    reached = True
                    break
                await asyncio.sleep(0.02)
            evaluate = await http.get(
                f"/api/v1/feature-flags/evaluate/{flag.key}",
                params={"user_id": f"u-{uuid.uuid4().hex[:12]}"},
                headers=key,
            )
            live = await http.get("/health/live")
            ready = await http.get("/health")
            release.set()
            answers = await asyncio.gather(*pending)

    peak = balanced_allocation.balanced_waits()["peak"]
    timeouts = _fallbacks("timeout") - timeout_before
    busy = _fallbacks("busy") - busy_before
    assert reached, (
        f"reached False, timeout {int(timeouts)}, peak {peak}, busy {int(busy)}"
    )
    assert evaluate.status_code == 200, evaluate.text
    assert live.status_code == 200, live.text
    assert ready.status_code in (200, 503), ready.text
    assert [a.status_code for a in answers] == [200] * senders, [
        a.text for a in answers
    ]
    assert peak == balanced_allocation.MAX_BALANCED_WAITS, f"peak {peak} > 4"
    assert (int(busy), int(timeouts)) == (8, 0)

    found = _rows(session_factory, experiment.id)
    assert found.problems == [], found.problems
    gold = found.groups["gold"]
    assert sorted(gold.seqs) == [0, 1, 2, 3]
    assert sum(gold.outside.values()) == 8, "a busy row lost its group"
    assert found.rows == senders


@pytest.mark.regression
async def test_the_counter_statement_runs_off_the_event_loop(
    production_pool, key, db_session, admin_user
):
    loop_thread = threading.get_ident()
    seen: List[Tuple[int, str]] = []
    lock = threading.Lock()

    @event.listens_for(production_pool, "before_cursor_execute")
    def _statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        with lock:
            seen.append((threading.get_ident(), statement))

    experiment = _experiment(db_session, admin_user)
    async with _http() as http:
        response = await http.post(
            ASSIGN,
            json={
                "experiment_key": experiment.key,
                "user_id": f"loop-{uuid.uuid4().hex[:10]}",
                "context": {"tier": "gold"},
            },
            headers=key,
        )
    event.remove(production_pool, "before_cursor_execute", _statement)

    assert response.status_code == 200, response.text
    counter = [t for t, s in seen if "balance_group_counters" in s]
    assert counter, "no counter statement seen: the listener is on the wrong engine"
    assert loop_thread not in counter, (
        "the counter INSERT ran on the event loop's thread"
    )
