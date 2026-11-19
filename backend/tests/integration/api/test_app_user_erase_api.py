"""``POST /api/v1/admin/app-users/erase`` on PostgreSQL (#2042, beta).

The real route, signed in with local tokens (the only override is
``deps.get_db``). Every test erases only a needle id it made itself
(``erase-needle-<uuid4 hex>``), seeded through the tracking, error and segment
routes where a route writes the table, and through the ORM where none does
(``raw_metrics``, ``llm_evaluations``, ``feature_flag_overrides``). The
expected tables are read from ``ERASED_TABLES``, never retyped.

What is held:

* nothing of the user is left in any table but one older audit entry that
  mentions the id, and the counts are exact (a re-send answers zeros);
* a timed-out chunk (a real 57014: another session holds a row) answers
  ``complete: false`` with exactly what is gone, and a re-send finishes; an
  unplanned failure after the start entry answers 500 and a re-send finishes;
* no start entry, no delete; the final transaction failing (a refused insert
  or a real 57014) is a 500 with the start entry only, never a 200;
* a table counts as done only when nothing of it is left, even when another
  session deleted part of it while the erase waited;
* exactly the id given is erased; only a superuser may call it;
* two audit entries per call, holding counts only; the other trails untouched;
* results still answer after a variant's last user is erased, earlier
  analysis snapshots keep their numbers, a re-sent event is stored, a returning
  user is assigned afresh; the budget ends a call and a re-send resumes;
* every chunk runs under ``enable_seqscan = off`` and the statement shape its
  table needs, and no erase statement plans a sequential scan;
* the id is in no log line: on a 200, a ``complete: false`` and a 500.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import pytest
from sqlalchemy import event, func, or_, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from backend.app.core.config import settings
from backend.app.models.analysis_snapshot import AnalysisSnapshot
from backend.app.models.assignment import Assignment
from backend.app.models.audit_log import ActionType, AuditLog, EntityType
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment
from backend.app.models.feature_flag import (
    FeatureFlag,
    FeatureFlagOverride,
    FeatureFlagStatus,
)
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.llm_experiment import (
    LLMEvaluation,
    LLMEvaluationMetric,
    LLMExperiment,
    LLMProvider,
    LLMTaskType,
    LLMVariant,
)
from backend.app.models.metrics.metric import RawMetric
from backend.app.models.segment import Segment
from backend.app.models.user import UserRole
from backend.app.services import app_user_erasure
from backend.app.services.app_user_erasure import (
    CHUNK_TIMEOUT_MS,
    ERASED_TABLES,
    apply_settings,
    statements,
)
from backend.app.services.audit_service import AuditService
from backend.app.services.global_holdout_service import (
    GlobalHoldoutService,
    holdout_bucket,
)
from backend.app.services.identity_aliases import link
from backend.tests.integration.api.route_helpers import (
    SCHEMA,
    api_client,
    api_key,
    auth,
    create_experiment,
    local_sign_in,
    make_user,
    start,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

ERASE = "/api/v1/admin/app-users/erase"
P = "erase2042"
ORDER = [table.name for table in ERASED_TABLES]
COLUMN = {table.name: table.column for table in ERASED_TABLES}
NOTHING = dict.fromkeys(ORDER, 0)
NOT_STARTED = "The erase could not be recorded, so nothing was erased"
NOT_FINISHED = "Part of the data may already be erased; send the request again"


def _needle() -> str:
    return f"erase-needle-{uuid.uuid4().hex}"


def _event(user_id: str) -> Event:
    """A stored purchase of *user_id*, in no experiment."""
    return Event(
        event_type="purchase",
        event_name="purchase",
        user_id=user_id,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local(monkeypatch):
    local_sign_in(monkeypatch)
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def engine(db_session):
    return db_session.get_bind()


@pytest.fixture
def fresh(engine):
    """A new session reading committed rows only."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def open_session():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return open_session


@pytest.fixture
def superuser(db_session):
    return make_user(db_session, UserRole.ADMIN, superuser=True, tag=P)


@pytest.fixture
def developer(db_session):
    return make_user(db_session, UserRole.DEVELOPER, tag=P)


@dataclass
class World:
    experiment: Dict[str, Any]
    key: Dict[str, str]
    flag: FeatureFlag
    segment_id: str
    llm_experiment_id: Any
    llm_variant_id: Any
    holdout: GlobalHoldout


@pytest.fixture
def world(client, db_session, developer):
    """An ACTIVE experiment, an SDK key, an ACTIVE flag, an id-list segment,
    an LLM experiment and an active, measurable global holdout (any holdout
    already active is parked and restored afterwards)."""
    suffix = uuid.uuid4().hex[:8]
    experiment = create_experiment(client, developer)
    start(client, experiment, developer)
    key = api_key(client, developer)
    flag = FeatureFlag(
        key=f"{P}-{suffix}",
        name=f"{P} {suffix}",
        status=FeatureFlagStatus.ACTIVE,
        owner_id=developer.id,
        rollout_percentage=100,
    )
    llm = LLMExperiment(
        name=f"{P} {suffix}",
        task_type=LLMTaskType.CHAT_COMPLETION,
        evaluation_metric=LLMEvaluationMetric.HUMAN_RATING,
    )
    db_session.add_all([flag, llm])
    db_session.commit()
    variant = LLMVariant(
        llm_experiment_id=llm.id,
        name="control",
        is_control=True,
        provider=LLMProvider.OPENAI,
        model_name="model",
        prompt_template="{q}",
    )
    db_session.add(variant)
    db_session.commit()
    response = client.post(
        "/api/v1/segments",
        json={"name": f"{P} {suffix}", "kind": "id_list"},
        headers=auth(developer),
    )
    assert response.status_code in (200, 201), response.text
    segment_id = response.json()["id"]
    parked = [
        row.id
        for row in db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active)
    ]
    db_session.execute(
        update(GlobalHoldout)
        .where(GlobalHoldout.id.in_(parked))
        .values(is_active=False)
    )
    db_session.commit()
    holdout = GlobalHoldoutService(db_session).create_holdout(
        name=f"{P}-{suffix}", holdout_percentage=1, is_active=True
    )
    yield World(experiment, key, flag, segment_id, llm.id, variant.id, holdout)
    db_session.rollback()
    db_session.query(GlobalHoldout).filter(GlobalHoldout.id == holdout.id).delete()
    db_session.execute(
        update(GlobalHoldout).where(GlobalHoldout.id.in_(parked)).values(is_active=True)
    )
    db_session.query(LLMExperiment).filter(LLMExperiment.id == llm.id).delete()
    db_session.query(Segment).filter(Segment.id == segment_id).delete()
    db_session.query(FeatureFlag).filter(FeatureFlag.id == flag.id).delete()
    experiment_id = experiment["id"]
    for model in (AnalysisSnapshot, Event, Assignment):
        db_session.query(model).filter(model.experiment_id == experiment_id).delete(
            synchronize_session=False
        )
    db_session.commit()


def _outside_the_holdout(world: World) -> str:
    """A needle the 1% holdout does not hold out, so it is assigned."""
    while True:
        needle = _needle()
        if holdout_bucket(needle, world.holdout.hash_salt) >= 1:
            return needle


def _seed(client, db_session, world: World, developer, needle: str) -> None:
    """Rows of *needle* in every erased table."""
    key = world.experiment["key"]
    response = client.post(
        "/api/v1/tracking/assign",
        json={
            "experiment_key": key,
            "user_id": needle,
            "context": {"email": f"{needle}@example.test"},
        },
        headers=world.key,
    )
    assert response.status_code == 200, response.text
    assert response.json().get("assigned", True) is not False, response.text
    response = client.post(
        "/api/v1/tracking/track",
        json={
            "event_type": "purchase",
            "user_id": needle,
            "experiment_key": key,
            "value": 20.0,
            "event_id": f"evt-{uuid.uuid4()}",
            "metadata": {"note": needle},
        },
        headers=world.key,
    )
    assert response.status_code == 200, response.text
    response = client.post(
        "/api/v1/tracking/errors",
        json={
            "feature_flag_key": world.flag.key,
            "user_id": needle,
            "error_type": "crash",
            "message": "boom",
            "metadata": {"device": needle},
        },
        headers=world.key,
    )
    assert response.status_code == 201, response.text
    response = client.post(
        f"/api/v1/segments/{world.segment_id}/members",
        json={"add": [needle]},
        headers=auth(developer),
    )
    assert response.status_code == 200, response.text
    db_session.add_all(
        [
            RawMetric(
                metric_type="evaluation",
                feature_flag_id=world.flag.id,
                user_id=needle,
                meta_data={"user": needle},
            ),
            LLMEvaluation(
                llm_experiment_id=world.llm_experiment_id,
                variant_id=world.llm_variant_id,
                user_id=needle,
                input_variables={"who": needle},
            ),
            FeatureFlagOverride(
                feature_flag_id=world.flag.id, user_id=needle, value=True
            ),
        ]
    )
    db_session.commit()


def _counts(fresh, ids: Iterable[str]) -> Dict[str, int]:
    """Rows per erased table whose id column is one of *ids*, committed."""
    ids = list(ids)
    session = fresh()
    try:
        found = {}
        for table in ERASED_TABLES:
            t = table.model.__table__
            match = or_(*(t.c[column].in_(ids) for column in table.columns))
            found[table.name] = session.execute(
                select(func.count()).select_from(t).where(match)
            ).scalar_one()
        return found
    finally:
        session.close()


def _sweep(fresh, needle: str) -> Dict[str, int]:
    """Rows mentioning *needle* anywhere, per table of the schema."""
    session = fresh()
    try:
        tables = (
            session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :s AND table_type = 'BASE TABLE'"
                ),
                {"s": SCHEMA},
            )
            .scalars()
            .all()
        )
        assert len(tables) >= len(ORDER), tables
        found = {}
        for name in sorted(tables):
            n = session.execute(
                text(
                    f'SELECT count(*) FROM {SCHEMA}."{name}" AS r WHERE r::text LIKE :p'
                ),
                {"p": f"%{needle}%"},
            ).scalar_one()
            if n:
                found[name] = n
        return found
    finally:
        session.close()


def _erase(client, user, user_id: str):
    return client.post(ERASE, json={"user_id": user_id}, headers=auth(user))


def _entries(fresh, receipt: str) -> List[AuditLog]:
    session = fresh()
    try:
        return (
            session.query(AuditLog)
            .filter(AuditLog.entity_id == uuid.UUID(receipt))
            .order_by(AuditLog.timestamp, AuditLog.action_type.desc())
            .all()
        )
    finally:
        session.close()


def _erase_entries_by(fresh, user) -> int:
    session = fresh()
    try:
        return (
            session.query(AuditLog)
            .filter(
                AuditLog.user_id == user.id,
                AuditLog.entity_type == EntityType.APP_USER.value,
            )
            .count()
        )
    finally:
        session.close()


def _final(fresh, receipt: str) -> Optional[Dict[str, Any]]:
    finals = [
        e
        for e in _entries(fresh, receipt)
        if e.action_type == ActionType.APP_USER_ERASED.value
    ]
    assert len(finals) <= 1
    return json.loads(finals[0].new_value) if finals else None


@pytest.fixture
def trigger(db_session):
    """``trigger(table, when, action)``: a BEFORE trigger on *table* running
    *action* (PL/pgSQL) *when* its condition holds; dropped afterwards."""
    made = []

    def make(table: str, event_name: str, condition: str, action: str) -> None:
        name = f"erase2042_{len(made)}_{uuid.uuid4().hex[:6]}"
        db_session.execute(
            text(
                f"CREATE FUNCTION {SCHEMA}.{name}() RETURNS trigger LANGUAGE plpgsql "
                f"AS $$ BEGIN IF {condition} THEN {action}; END IF; "
                f"IF TG_OP = 'DELETE' THEN RETURN OLD; END IF; RETURN NEW; END $$"
            )
        )
        db_session.execute(
            text(
                f"CREATE TRIGGER {name} BEFORE {event_name} ON {SCHEMA}.{table} "
                f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{name}()"
            )
        )
        db_session.commit()
        made.append((table, name))

    def drop() -> None:
        db_session.rollback()
        while made:
            table, name = made.pop()
            db_session.execute(
                text(f"DROP TRIGGER IF EXISTS {name} ON {SCHEMA}.{table}")
            )
            db_session.execute(text(f"DROP FUNCTION IF EXISTS {SCHEMA}.{name}()"))
        db_session.commit()

    make.drop = drop
    try:
        yield make
    finally:
        drop()


# --- what is deleted ------------------------------------------------------------


@pytest.mark.regression
def test_nothing_of_the_user_remains_in_any_table(
    client, db_session, fresh, world, superuser, developer
):
    needle = _outside_the_holdout(world)
    _seed(client, db_session, world, developer, needle)
    # A link (#449): an anonymous id of the same person, in identity_aliases.
    link(db_session, f"{needle}-anonymous", needle)
    # An older entry that mentions the id: the residual, never erased.
    AuditService.record(
        db_session,
        actor=superuser,
        action=ActionType.SEGMENT_UPDATE,
        entity_type=EntityType.SEGMENT,
        entity_id=uuid.UUID(world.segment_id),
        entity_name=f"{P} mention",
        after={"note": needle},
    )
    db_session.commit()
    # One results read: today's analysis snapshot, which must hold no id.
    response = client.get(
        f"/api/v1/results/{world.experiment['id']}",
        params={"use_cache": "false"},
        headers=auth(developer),
    )
    assert response.status_code == 200, response.text

    before = _counts(fresh, [needle])
    assert list(before) == ORDER
    assert all(n >= 1 for n in before.values()), before

    response = _erase(client, superuser, needle)
    assert response.status_code == 200, response.text
    assert response.json()["complete"] is True

    remains = _sweep(fresh, needle)
    assert remains == {"audit_logs": 1}, f"needle remains: {remains}"


@pytest.mark.regression
def test_the_response_counts_each_table_exactly(
    client, db_session, fresh, world, superuser, developer
):
    needle = _outside_the_holdout(world)
    _seed(client, db_session, world, developer, needle)
    before = _counts(fresh, [needle])

    response = _erase(client, superuser, needle)
    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body["deleted"]) == ORDER, (
        f"missing from counts: {set(ORDER) - set(body['deleted'])}"
    )
    assert body["deleted"] == before
    assert body["ids_erased"] == 1
    assert body["complete"] is True
    assert set(body) == {"receipt_id", "complete", "ids_erased", "deleted"}

    again = _erase(client, superuser, needle)
    assert again.status_code == 200, again.text
    assert again.json()["deleted"] == NOTHING
    assert again.json()["complete"] is True
    assert again.json()["receipt_id"] != body["receipt_id"]


# --- chunks, failures and the record ---------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("held", [t.name for t in ERASED_TABLES if not t.last])
def test_a_failure_part_way_is_reported_and_resumable(
    held, client, db_session, engine, fresh, world, superuser, developer, monkeypatch
):
    """Another session holds one of the needle's rows in table *held*; the
    chunk on it times out (a real 57014), so the call answers
    ``complete: false`` with exactly what is gone, and a re-send finishes."""
    needle = _outside_the_holdout(world)
    _seed(client, db_session, world, developer, needle)
    before = _counts(fresh, [needle])
    monkeypatch.setattr(app_user_erasure, "CHUNK_TIMEOUT_MS", 300)

    holder = engine.connect()
    transaction = holder.begin()
    try:
        holder.execute(text(f"SET search_path TO {SCHEMA}"))
        held_rows = holder.execute(
            text(
                f'SELECT 1 FROM {SCHEMA}."{held}" WHERE "{COLUMN[held]}" = :n FOR UPDATE'
            ),
            {"n": needle},
        ).all()
        assert held_rows
        response = _erase(client, superuser, needle)
    finally:
        transaction.rollback()
        holder.close()

    assert response.status_code == 200, response.text
    body = response.json()
    stop = ORDER.index(held)
    gone = {name: (before[name] if i < stop else 0) for i, name in enumerate(ORDER)}
    assert body["complete"] is False, f"complete {body['complete']} != False"
    assert body["deleted"] == gone
    assert _counts(fresh, [needle]) == {
        name: (0 if i < stop else before[name]) for i, name in enumerate(ORDER)
    }
    assert _final(fresh, body["receipt_id"]) == {
        "deleted": gone,
        "ids_erased": 1,
        "complete": False,
    }

    again = _erase(client, superuser, needle)
    assert again.status_code == 200, again.text
    assert again.json()["complete"] is True
    assert {
        name: body["deleted"][name] + again.json()["deleted"][name] for name in ORDER
    } == before
    assert _counts(fresh, [needle]) == NOTHING


@pytest.mark.regression
def test_an_unplanned_failure_part_way_answers_500_and_a_resend_finishes(
    client, db_session, fresh, world, superuser, developer, trigger
):
    needle = _outside_the_holdout(world)
    _seed(client, db_session, world, developer, needle)
    before = _counts(fresh, [needle])
    trigger(
        "assignments",
        "DELETE",
        f"OLD.user_id = '{needle}'",
        "RAISE EXCEPTION 'erase2042 refused'",
    )
    entries_before = _erase_entries_by(fresh, superuser)

    response = _erase(client, superuser, needle)
    assert response.status_code == 500, response.text
    assert response.json()["detail"].startswith(NOT_FINISHED)
    assert _erase_entries_by(fresh, superuser) == entries_before + 1
    stop = ORDER.index("assignments")
    assert _counts(fresh, [needle]) == {
        name: (0 if i < stop else before[name]) for i, name in enumerate(ORDER)
    }

    trigger.drop()
    again = _erase(client, superuser, needle)
    assert again.status_code == 200, again.text
    assert again.json()["complete"] is True
    assert _counts(fresh, [needle]) == NOTHING


@pytest.mark.regression
def test_no_start_entry_erases_nothing(
    client, db_session, fresh, world, superuser, developer, trigger
):
    needle = _outside_the_holdout(world)
    _seed(client, db_session, world, developer, needle)
    before = _counts(fresh, [needle])
    entries_before = _erase_entries_by(fresh, superuser)
    trigger("audit_logs", "INSERT", "true", "RAISE EXCEPTION 'erase2042 refused'")

    response = _erase(client, superuser, needle)

    after = _counts(fresh, [needle])
    assert after == before, f"rows deleted with no record: {before} -> {after}"
    assert response.status_code == 500, response.text
    assert response.json()["detail"].startswith(NOT_STARTED)
    trigger.drop()
    assert _erase_entries_by(fresh, superuser) == entries_before


@pytest.mark.regression
@pytest.mark.parametrize("how", ["refused", "timeout"])
def test_a_failed_final_transaction_is_a_500_with_the_start_entry_only(
    how, client, db_session, fresh, world, superuser, developer, trigger, monkeypatch
):
    """An error in the final transaction, a real 57014 included,
    never answers 200."""
    needle = _outside_the_holdout(world)
    _seed(client, db_session, world, developer, needle)
    entries_before = _erase_entries_by(fresh, superuser)
    final = f"NEW.action_type = '{ActionType.APP_USER_ERASED.value}'"
    if how == "refused":
        trigger("audit_logs", "INSERT", final, "RAISE EXCEPTION 'erase2042 refused'")
    else:
        monkeypatch.setattr(app_user_erasure, "FINAL_RESERVE_S", 0.3)
        trigger("audit_logs", "INSERT", final, "PERFORM pg_sleep(5)")

    response = _erase(client, superuser, needle)

    assert response.status_code == 500, response.text
    assert response.json()["detail"].startswith(NOT_FINISHED)
    trigger.drop()
    assert _erase_entries_by(fresh, superuser) == entries_before + 1
    again = _erase(client, superuser, needle)
    assert again.status_code == 200, again.text
    assert again.json()["complete"] is True
    assert _counts(fresh, [needle]) == NOTHING


@pytest.mark.regression
@pytest.mark.parametrize("by_b_wanted", [1000, 500])
def test_a_table_is_done_only_when_nothing_of_it_is_left(
    client, db_session, engine, fresh, superuser, by_b_wanted
):
    """Session B deletes 1,000 (or 500) of the needle's events and holds
    its transaction; the erase waits on those rows, B commits, and the erase
    answers complete only with nothing left. With 500, the erase's short chunk
    deletes the other 500 rows, so a check made only after an empty short
    chunk would call the table done with rows left. No wall clock is
    asserted."""
    needle = _needle()
    db_session.add_all(_event(needle) for _ in range(1500))
    db_session.commit()
    before = _counts(fresh, [needle])
    assert before["events"] == 1500

    b = engine.connect()
    b_transaction = b.begin()
    b.execute(text(f"SET search_path TO {SCHEMA}"))
    b.execute(text("SET LOCAL enable_seqscan = off"))
    by_b = b.execute(
        text(
            "DELETE FROM events WHERE id IN "
            "(SELECT id FROM events WHERE user_id = :n LIMIT :k)"
        ),
        {"n": needle, "k": by_b_wanted},
    ).rowcount
    assert by_b == by_b_wanted

    result: Dict[str, Any] = {}

    def run() -> None:
        result["response"] = _erase(client, superuser, needle)

    worker = threading.Thread(target=run)
    worker.start()
    watcher = engine.connect()
    try:
        waited = False
        for _ in range(1200):
            waiting = watcher.execute(
                text(
                    "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a "
                    "ON a.pid = l.pid WHERE NOT l.granted "
                    "AND a.datname = current_database()"
                )
            ).scalar_one()
            # pg_stat_activity is a snapshot taken once per transaction: end
            # it, so the next look sees a session that connected since.
            watcher.rollback()
            if waiting:
                waited = True
                break
            time.sleep(0.05)
        assert waited, "the erase never waited on session B's rows"
    finally:
        b_transaction.commit()
        b.close()
        watcher.close()
        worker.join(60)

    response = result["response"]
    assert response.status_code == 200, response.text
    body = response.json()
    left = _counts(fresh, [needle])["events"]
    if body["complete"]:
        assert left == 0, f"complete True with {left} rows left"
    assert body["deleted"]["events"] + by_b + left == 1500
    assert body["complete"] is True
    assert left == 0


@pytest.mark.regression
def test_only_the_exact_id_is_erased(client, db_session, fresh, superuser):
    base = f"erase-exact-{uuid.uuid4().hex}"
    ids = {
        "exact": base,
        "upper": base.upper(),
        "leading space": f" {base}",
        "trailing space": f"{base} ",
        "percent": f"{base[:-4]}%",
        "underscore": f"{base[:-1]}_",
    }
    db_session.add_all(_event(user_id) for user_id in ids.values() for _ in range(2))
    db_session.commit()

    for name in ("exact", "upper", "leading space", "percent"):
        response = _erase(client, superuser, ids[name])
        assert response.status_code == 200, response.text
        assert response.json()["deleted"]["events"] == 2, name

    left = {name: _counts(fresh, [user_id])["events"] for name, user_id in ids.items()}
    assert left == {
        "exact": 0,
        "upper": 0,
        "leading space": 0,
        "trailing space": 2,
        "percent": 0,
        "underscore": 2,
    }, f"other users' rows deleted: {left}"
    for user_id in ids.values():
        db_session.query(Event).filter(Event.user_id == user_id).delete()
    db_session.commit()


@pytest.mark.parametrize(
    "role", [UserRole.ADMIN, UserRole.DEVELOPER, UserRole.ANALYST, UserRole.VIEWER]
)
def test_only_a_superuser_may_erase(role, client, db_session, fresh, superuser):
    needle = _needle()
    db_session.add(_event(needle))
    db_session.commit()
    user = make_user(db_session, role, tag=P)

    response = _erase(client, user, needle)
    assert response.status_code == 403, f"{role.name}: {response.status_code} != 403"
    assert response.json()["detail"] == "Not enough permissions"
    assert _counts(fresh, [needle])["events"] == 1

    for scopes in (["sdk:ruleset"], ["mcp:read"]):
        key = api_key(client, superuser, scopes=scopes)
        response = client.post(ERASE, json={"user_id": needle}, headers=key)
        assert response.status_code == 401, (scopes, response.status_code)
    assert _counts(fresh, [needle])["events"] == 1

    response = _erase(client, superuser, needle)
    assert response.status_code == 200, response.text
    assert _counts(fresh, [needle])["events"] == 0


@pytest.mark.regression
def test_each_call_writes_a_start_and_a_final_entry_with_counts_only(
    client, db_session, fresh, world, superuser, developer
):
    needle = _outside_the_holdout(world)
    _seed(client, db_session, world, developer, needle)
    before = _counts(fresh, [needle])
    entries_before = _erase_entries_by(fresh, superuser)

    response = _erase(client, superuser, needle)
    assert response.status_code == 200, response.text
    receipt = response.json()["receipt_id"]
    assert _erase_entries_by(fresh, superuser) == entries_before + 2

    start_entry, final_entry = _entries(fresh, receipt)
    for entry in (start_entry, final_entry):
        assert entry.entity_type == "app_user"
        assert str(entry.entity_id) == receipt
        assert entry.entity_name == "app user"
        assert entry.user_id == superuser.id
        assert entry.user_email == superuser.email
        assert entry.old_value is None
        assert entry.reason is None
        assert needle not in json.dumps(
            {c: str(getattr(entry, c)) for c in AuditLog.__table__.columns.keys()}
        )
    assert start_entry.action_type == "app_user_erase_started"
    assert start_entry.new_value is None
    assert final_entry.action_type == "app_user_erased"
    assert json.loads(final_entry.new_value) == {
        "deleted": before,
        "ids_erased": 1,
        "complete": True,
    }
    # The page's query finds both, and only these two.
    listed = client.get(
        "/api/v1/audit-logs/", params={"entity_id": receipt}, headers=auth(superuser)
    )
    assert listed.status_code == 200, listed.text
    assert sorted(item["action_type"] for item in listed.json()["items"]) == [
        "app_user_erase_started",
        "app_user_erased",
    ]
    assert needle not in listed.text

    # A call that finds nothing records too: two entries, every count 0.
    nothing = _erase(client, superuser, _needle())
    assert nothing.status_code == 200, nothing.text
    assert nothing.json()["deleted"] == NOTHING
    assert _erase_entries_by(fresh, superuser) == entries_before + 4
    assert _final(fresh, nothing.json()["receipt_id"])["deleted"] == NOTHING


def test_the_audit_trails_are_untouched_but_for_the_two_entries(
    client, db_session, fresh, superuser
):
    needle = _needle()
    db_session.add(_event(needle))
    AuditService.record(
        db_session,
        actor=superuser,
        action=ActionType.SEGMENT_UPDATE,
        entity_type=EntityType.SEGMENT,
        entity_id=uuid.uuid4(),
        entity_name=f"{P} mention",
        after={"note": needle},
    )
    db_session.commit()

    def totals() -> Dict[str, int]:
        session = fresh()
        try:
            found = {}
            for table in ("audit_logs", "audit_events_v2", "phi_audit_logs"):
                if session.execute(
                    text("SELECT to_regclass(:t)"), {"t": f"{SCHEMA}.{table}"}
                ).scalar():
                    found[table] = session.execute(
                        text(f"SELECT count(*) FROM {SCHEMA}.{table}")
                    ).scalar_one()
            return found
        finally:
            session.close()

    before = totals()
    response = _erase(client, superuser, needle)
    assert response.status_code == 200, response.text
    after = totals()
    assert {t: after[t] - before[t] for t in before} == {
        t: (2 if t == "audit_logs" else 0) for t in before
    }
    assert _sweep(fresh, needle) == {"audit_logs": 1}


# --- results afterwards -------------------------------------------------------------


def _assign_rows(db_session, experiment: Dict[str, Any], users: Dict[str, str]) -> None:
    """``{user_id: variant name}`` as assignment rows of *experiment*."""
    variants = {v["name"]: v["id"] for v in experiment["variants"]}
    db_session.add_all(
        Assignment(
            experiment_id=experiment["id"],
            variant_id=variants[variant],
            user_id=user_id,
        )
        for user_id, variant in users.items()
    )
    db_session.commit()


def test_erasing_a_variants_last_user_keeps_results_answering(
    client, db_session, world, superuser, developer
):
    experiment = world.experiment
    db_session.execute(
        update(Experiment)
        .where(Experiment.id == experiment["id"])
        .values(sequential_testing_enabled=True)
    )
    loner = _needle()
    _assign_rows(db_session, experiment, {_needle(): "control", loner: "treatment"})

    response = _erase(client, superuser, loner)
    assert response.status_code == 200, response.text
    assert response.json()["deleted"]["assignments"] == 1

    for path, params in (
        (f"/api/v1/results/{experiment['id']}", {"use_cache": "false"}),
        (f"/api/v1/results/{experiment['id']}/sequential", {}),
        (f"/api/v1/holdout/{world.holdout.id}/results", {"metric": "purchase"}),
    ):
        response = client.get(path, params=params, headers=auth(developer))
        assert response.status_code == 200, f"{path}: {response.status_code} != 200"


def _users_in(payload: Dict[str, Any]) -> int:
    """Users the stored frequentist results counted, over all variants."""
    metric = payload["metrics"][0]
    return sum(variant["sample_size"] for variant in metric["variants"])


def test_earlier_analysis_snapshots_keep_their_numbers(
    client, db_session, fresh, world, superuser, developer
):
    """Today's snapshot is replaced by the next computation;
    yesterday's keeps counting the erased user."""
    experiment = world.experiment
    users = {_needle(): ("control" if i % 2 else "treatment") for i in range(10)}
    _assign_rows(db_session, experiment, users)

    def results() -> None:
        response = client.get(
            f"/api/v1/results/{experiment['id']}",
            params={"use_cache": "false"},
            headers=auth(developer),
        )
        assert response.status_code == 200, response.text

    results()
    db_session.execute(
        update(AnalysisSnapshot)
        .where(AnalysisSnapshot.experiment_id == experiment["id"])
        .values(as_of=AnalysisSnapshot.as_of - timedelta(days=1))
    )
    db_session.commit()

    response = _erase(client, superuser, next(iter(users)))
    assert response.status_code == 200, response.text
    results()

    session = fresh()
    try:
        rows = (
            session.query(AnalysisSnapshot)
            .filter(
                AnalysisSnapshot.experiment_id == experiment["id"],
                AnalysisSnapshot.kind == "frequentist",
            )
            .order_by(AnalysisSnapshot.as_of)
            .all()
        )
    finally:
        session.close()
    assert [_users_in(row.payload) for row in rows] == [10, 9]


@pytest.mark.regression
def test_an_event_sent_again_after_an_erase_is_stored_again(
    client, db_session, fresh, world, superuser
):
    needle = _needle()
    body = {
        "event_type": "purchase",
        "user_id": needle,
        "experiment_key": world.experiment["key"],
        "event_id": f"evt-{uuid.uuid4()}",
    }
    track = "/api/v1/tracking/track"
    assert client.post(track, json=body, headers=world.key).status_code == 200
    assert _counts(fresh, [needle])["events"] == 1
    assert _counts(fresh, [needle])["event_dedup_keys"] == 1

    response = _erase(client, superuser, needle)
    assert response.status_code == 200, response.text
    assert response.json()["deleted"]["event_dedup_keys"] == 1

    assert client.post(track, json=body, headers=world.key).status_code == 200
    assert _counts(fresh, [needle])["events"] == 1, "the re-sent event was refused"


def test_a_returning_user_is_assigned_afresh(
    client, db_session, fresh, world, superuser
):
    needle = _outside_the_holdout(world)

    def assign() -> str:
        response = client.post(
            "/api/v1/tracking/assign",
            json={"experiment_key": world.experiment["key"], "user_id": needle},
            headers=world.key,
        )
        assert response.status_code == 200, response.text
        return response.json()["variant_name"]

    first = assign()
    response = _erase(client, superuser, needle)
    assert response.status_code == 200, response.text
    assert response.json()["deleted"]["assignments"] == 1

    assert assign() == first
    counts = _counts(fresh, [needle])
    assert counts["assignments"] == 1
    session = fresh()
    try:
        exposures = (
            session.query(Event)
            .filter(Event.user_id == needle, Event.event_type == "exposure")
            .count()
        )
    finally:
        session.close()
    assert exposures == 1


# --- the budget, the statements and the plans ----------------------------------


@pytest.mark.regression
def test_the_budget_ends_a_call_and_a_resend_resumes(
    client, db_session, fresh, superuser, monkeypatch
):
    """The module clock (never ``time.monotonic``) jumps past the budget
    after the first chunk."""
    needle = _needle()
    db_session.add_all(_event(needle) for _ in range(5))
    db_session.commit()
    monkeypatch.setattr(app_user_erasure, "CHUNK_ROWS", 2)
    ticks = iter([1000.0, 1000.0])
    monkeypatch.setattr(app_user_erasure, "_clock", lambda: next(ticks, 1018.0))

    response = _erase(client, superuser, needle)
    assert response.status_code == 200, response.text
    assert response.json()["complete"] is False, "complete True != False"
    assert response.json()["deleted"]["events"] == 2

    monkeypatch.setattr(app_user_erasure, "_clock", time.monotonic)
    again = _erase(client, superuser, needle)
    assert again.status_code == 200, again.text
    assert again.json()["complete"] is True
    assert again.json()["deleted"]["events"] == 3
    assert _counts(fresh, [needle])["events"] == 0


@pytest.fixture
def recorder(engine):
    """Every statement on the engine, with commits and rollbacks between."""
    seen: List[tuple] = []

    def statement(conn, cursor, sql, parameters, context, executemany):
        seen.append(("sql", " ".join(sql.split()), parameters))

    def commit(conn):
        seen.append(("commit", "", None))

    def rollback(conn):
        seen.append(("rollback", "", None))

    event.listen(engine, "before_cursor_execute", statement)
    event.listen(engine, "commit", commit)
    event.listen(engine, "rollback", rollback)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", statement)
        event.remove(engine, "commit", commit)
        event.remove(engine, "rollback", rollback)


@pytest.mark.regression
def test_every_chunk_turns_sequential_scans_off_and_uses_its_tables_shape(
    client, db_session, world, superuser, developer, recorder
):
    """The settings before every DELETE, in the same transaction, and
    the per-parent statements through the parent's primary key."""
    needle = _outside_the_holdout(world)
    _seed(client, db_session, world, developer, needle)
    recorder.clear()

    response = _erase(client, superuser, needle)
    assert response.status_code == 200, response.text

    deletes = {}
    since_boundary: List[tuple] = []
    for kind, sql, parameters in recorder:
        if kind != "sql":
            since_boundary = []
            continue
        if sql.startswith("DELETE FROM"):
            table = sql.split()[2].split(".")[-1]
            settings_seen = [
                params
                for _, s, params in since_boundary
                if "set_config" in s and isinstance(params, dict)
            ]
            values = [v for params in settings_seen for v in params.values()]
            assert "enable_seqscan" in values and "off" in values, (
                f"no enable_seqscan off before: {sql[:80]}"
            )
            assert "statement_timeout" in values, sql[:80]
            deletes.setdefault(table, []).append(sql)
        since_boundary.append((kind, sql, parameters))

    assert set(deletes) == set(ORDER)
    for table in ERASED_TABLES:
        for sql in deletes[table.name]:
            if table.last:
                assert (
                    f"{table.name}.user_id IN" in sql
                    and f"OR {SCHEMA}.{table.name}.anonymous_id IN" in sql
                ), sql
            elif table.parent is not None:
                column, parent = table.parent
                assert (
                    f"{column} IN (SELECT {SCHEMA}.{parent.__table__.name}.id" in sql
                ), f"statement lacks {column} IN (SELECT ...): {sql}"
            elif table.name == "event_dedup_keys":
                assert ".dedup_key) IN (SELECT" in sql, sql
            else:
                assert f"{table.name}.id IN (SELECT" in sql, sql


def _literal(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_no_erase_statement_plans_a_sequential_scan(fresh):
    """EXPLAIN of each chunk, check and per-parent statement under
    the chunk's settings, on this database's (small) tables. The tables are
    analysed first: with statistics that say a table is small, the default
    planner reads it with a sequential scan, so only the chunk's own
    ``enable_seqscan = off`` keeps the scan off it."""
    session = fresh()
    try:
        for table in ERASED_TABLES:
            session.execute(text(f"ANALYZE {SCHEMA}.{table.name}"))
            if table.parent is not None:
                session.execute(
                    text(f"ANALYZE {SCHEMA}.{table.parent[1].__table__.name}")
                )
        session.commit()
        for table in ERASED_TABLES:
            for statement in statements(table, ["erase-needle-explain"]):
                apply_settings(session, CHUNK_TIMEOUT_MS)
                plan = "\n".join(
                    row[0]
                    for row in session.connection().exec_driver_sql(
                        "EXPLAIN " + _literal(statement)
                    )
                )
                session.rollback()
                assert f"Seq Scan on {table.name}" not in plan, plan
    finally:
        session.close()


# --- logs ------------------------------------------------------------------------


@pytest.mark.regression
def test_the_id_is_in_no_log_line(
    client, db_session, engine, fresh, superuser, trigger, caplog, monkeypatch
):
    """On a 200, a ``complete: false`` and a 500: the needle in no message and
    no argument of any record; the 500 path's own line is captured."""
    caplog.set_level(logging.DEBUG)

    def seeded() -> str:
        needle = _needle()
        db_session.add_all(_event(needle) for _ in range(3))
        db_session.commit()
        return needle

    needles = []
    # 200
    needles.append(seeded())
    assert _erase(client, superuser, needles[-1]).status_code == 200
    # complete: false (a real statement timeout)
    needles.append(seeded())
    monkeypatch.setattr(app_user_erasure, "CHUNK_TIMEOUT_MS", 200)
    holder = engine.connect()
    held = holder.begin()
    try:
        holder.execute(
            text(f"SELECT 1 FROM {SCHEMA}.events WHERE user_id = :n FOR UPDATE"),
            {"n": needles[-1]},
        ).all()
        response = _erase(client, superuser, needles[-1])
    finally:
        held.rollback()
        holder.close()
    assert response.status_code == 200 and response.json()["complete"] is False
    # 500 after the start entry
    needles.append(seeded())
    trigger(
        "events",
        "DELETE",
        f"OLD.user_id = '{needles[-1]}'",
        "RAISE EXCEPTION 'erase2042 refused'",
    )
    assert _erase(client, superuser, needles[-1]).status_code == 500
    trigger.drop()

    messages = [record.getMessage() for record in caplog.records]
    assert any("App user erase failed (" in m for m in messages), messages[-20:]
    for record in caplog.records:
        text_of_record = " ".join(
            [record.getMessage(), repr(record.args), repr(record.__dict__)]
        )
        for needle in needles:
            assert needle not in text_of_record, (
                f"needle in log record: {record.name} {record.getMessage()[:120]}"
            )
    for needle in needles:
        db_session.query(Event).filter(Event.user_id == needle).delete()
    db_session.commit()
