"""The erase covers the whole person of a linked id (#2042 with #449, Q-18).

``identity_aliases`` (#449) links anonymous ids to a known id. An erase of any
id of a linked person erases every id of that person: the set is read before
anything is deleted (afterwards it is the one id), read with no row limit (a
person linked past ``link``'s cap by other means is still erased whole), and
the links themselves go last, in the final transaction, under the advisory
keys ``link`` takes, so a link and an erase never interleave:

* (i) links naming the id as ``anonymous_id`` or as ``user_id`` are deleted;
* (ii) the set is read before the links are deleted;
* (iii) a1 assigned, ``link(a1, u1)``, u1 tracks a purchase (stored under a1):
  erasing u1 leaves no row of either;
* (iv) the final transaction takes ``link``'s keys, one statement per key in
  ascending order, imported from ``identity_aliases``; a ``link`` waiting on
  them stores its link after the erase commits; a link that grows the set
  while the erase runs makes the answer ``complete: false``.

The real ``link`` and ``person_of`` of ``backend.app.services.identity_aliases``
are used throughout. The world, the clients and the helpers are the erase's own
tests' (``test_app_user_erase_api.py``); the fixtures are imported by name so
that pytest finds them in this module. No wall clock is asserted: the waits are on
``pg_locks`` and on the test's own events, bounded only so that a broken
build fails instead of hanging.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

import pytest
from sqlalchemy import event, text

from backend.app.models.event import Event
from backend.app.services import app_user_erasure
from backend.app.services.app_user_erasure import ERASED_TABLES, erasure_ids
from backend.app.services.identity_aliases import (
    LOCK_SPACE,
    MAX_ANONYMOUS_IDS_PER_USER,
    link,
    person_of,
)
from backend.tests.integration.api.route_helpers import SCHEMA
from backend.tests.integration.api.test_app_user_erase_api import (
    _counts,
    _erase,
    _event,
    _needle,
    _outside_the_holdout,
    _seed,
    client,
    developer,
    engine,
    fresh,
    superuser,
    world,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

ORDER = [table.name for table in ERASED_TABLES]


def _events(db_session, *ids: str) -> None:
    db_session.add_all(_event(user_id) for user_id in ids)
    db_session.commit()


def _left(fresh, ids: List[str]) -> Dict[str, int]:
    """Rows per erased table for each id that still has any."""
    found = {}
    for user_id in ids:
        counts = {k: v for k, v in _counts(fresh, [user_id]).items() if v}
        if counts:
            found[user_id] = counts
    return found


def _hashes(engine, ids: List[str]) -> List[int]:
    with engine.connect() as conn:
        return [
            conn.execute(text("SELECT hashtext(:i)"), {"i": i}).scalar_one()
            for i in ids
        ]


@pytest.mark.regression
def test_an_anonymous_ids_rows_held_for_the_person_are_erased(
    client, db_session, fresh, world, superuser, developer
):
    """(ii) and (iii): u1's purchase is stored under a1 (#449's holder), so an
    erase over u1 alone would leave it; the erase covers the person."""
    a1 = _outside_the_holdout(world)
    u1 = f"{a1}-known"
    response = client.post(
        "/api/v1/tracking/assign",
        json={"experiment_key": world.experiment["key"], "user_id": a1},
        headers=world.key,
    )
    assert response.status_code == 200, response.text
    assert link(db_session, a1, u1) is True
    response = client.post(
        "/api/v1/tracking/track",
        json={
            "event_type": "purchase",
            "user_id": u1,
            "experiment_key": world.experiment["key"],
            "event_id": f"evt-{a1}",
        },
        headers=world.key,
    )
    assert response.status_code == 200, response.text
    assert _counts(fresh, [a1])["events"] >= 2  # the exposure and the purchase
    assert _counts(fresh, [u1, a1])["identity_aliases"] == 1

    response = _erase(client, superuser, u1)
    assert response.status_code == 200, response.text
    left = _counts(fresh, [a1])
    assert left["events"] == 0, f"events under a1: {left['events']} != 0"
    assert _left(fresh, [a1, u1]) == {}
    body = response.json()
    assert body["complete"] is True
    assert body["ids_erased"] == 2
    assert body["deleted"]["identity_aliases"] == 1


@pytest.mark.regression
def test_links_naming_the_id_in_either_column_are_deleted(
    client, db_session, fresh, superuser
):
    """(i): erasing the known id deletes its links (``user_id``); erasing an
    anonymous id deletes the link naming it (``anonymous_id``) and the
    person's other links."""
    u1 = _needle()
    a1, a2 = f"{u1}-a1", f"{u1}-a2"
    link(db_session, a1, u1)
    link(db_session, a2, u1)
    _events(db_session, u1, a1, a2)
    before = _counts(fresh, [u1, a1, a2])

    response = _erase(client, superuser, a2)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ids_erased"] == 3
    assert body["deleted"] == before
    assert body["deleted"]["identity_aliases"] == 2
    assert body["deleted"]["events"] == 3
    assert _left(fresh, [u1, a1, a2]) == {}


@pytest.mark.regression
def test_a_person_at_the_cap_is_erased_whole(client, db_session, fresh, superuser):
    """Through ``link`` a known id holds at most 100 anonymous ids; the
    erase's set equals ``person_of``'s, all 101 ids."""
    u1 = _needle()
    anonymous = [f"{u1}-{i:03d}" for i in range(MAX_ANONYMOUS_IDS_PER_USER)]
    for a in anonymous:
        assert link(db_session, a, u1) is True
    ids = [u1, *anonymous]
    _events(db_session, *ids)
    assert set(erasure_ids(db_session, anonymous[7])) == set(
        person_of(db_session, anonymous[7]).ids
    )
    db_session.rollback()

    response = _erase(client, superuser, u1)
    assert response.status_code == 200, response.text
    assert response.json()["ids_erased"] == 101
    left = _left(fresh, ids)
    assert left == {}, f"rows for {len(left)} of {len(ids)} ids remain"


@pytest.mark.regression
def test_a_person_linked_past_the_cap_by_hand_is_erased_whole(
    client, db_session, fresh, superuser
):
    """150 links inserted directly, past ``link``'s cap: ``person_of`` stops
    at 101 rows, the erase does not."""
    u1 = _needle()
    anonymous = [f"{u1}-{i:03d}" for i in range(150)]
    db_session.execute(
        text(
            f"INSERT INTO {SCHEMA}.identity_aliases (anonymous_id, user_id) VALUES (:a, :u)"
        ),
        [{"a": a, "u": u1} for a in anonymous],
    )
    db_session.commit()
    ids = [u1, *anonymous]
    _events(db_session, *ids)

    response = _erase(client, superuser, anonymous[0])
    assert response.status_code == 200, response.text
    assert response.json()["ids_erased"] == 151
    assert response.json()["deleted"]["identity_aliases"] == 150
    left = _left(fresh, ids)
    assert left == {}, f"rows for {len(left)} of {len(ids)} ids remain"


@pytest.fixture
def advisory_statements(engine):
    """Every ``pg_advisory_xact_lock`` statement on the engine, with its
    parameters, in the order sent."""
    seen: List[Any] = []

    def record(conn, cursor, sql, parameters, context, executemany):
        if "pg_advisory_xact_lock" in sql:
            seen.append((" ".join(sql.split()), parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.mark.regression
def test_the_keys_are_links_taken_one_statement_each_ascending(
    client, db_session, engine, superuser, advisory_statements
):
    """(iv): the keys ``link`` takes for each id, in ascending numeric order,
    one statement per key, in ``link``'s key space."""
    u1 = _needle()
    anonymous = [f"{u1}-{i}" for i in range(6)]
    for a in anonymous:
        link(db_session, a, u1)
    advisory_statements.clear()

    response = _erase(client, superuser, u1)
    assert response.status_code == 200, response.text
    assert response.json()["complete"] is True

    expected = sorted(set(_hashes(engine, [u1, *anonymous])))
    assert len(advisory_statements) == len(expected), advisory_statements
    keys = []
    for sql, parameters in advisory_statements:
        assert sql.count("pg_advisory_xact_lock") == 1, sql
        assert "ORDER BY" not in sql.upper(), sql
        assert parameters["space"] == LOCK_SPACE
        keys.append(parameters["key"])
    assert keys == expected, f"order {keys} != sorted"


def _wait_for_advisory_waiter(engine) -> bool:
    """True once a session waits for one of ``link``'s keys.

    ``pg_locks`` alone, read in a new transaction each time: an advisory lock
    carries its database, and statistics views such as ``pg_stat_activity``
    are a snapshot taken once per transaction, which would miss a session
    that connected after the first look.
    """
    with engine.connect() as watcher:
        for _ in range(1200):
            waiting = watcher.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE NOT granted "
                    "AND locktype = 'advisory' AND classid = :space AND database = "
                    "(SELECT oid FROM pg_database WHERE datname = current_database())"
                ),
                {"space": LOCK_SPACE},
            ).scalar_one()
            watcher.rollback()
            if waiting:
                return True
            time.sleep(0.05)
    return False


@pytest.mark.regression
def test_the_erase_waits_while_another_session_holds_a_persons_key(
    client, db_session, engine, fresh, superuser, monkeypatch
):
    """(iv): session A holds the key of u1 in ``link``'s key space; the erase
    waits in its final transaction and finishes only after A commits. The
    final transaction's reserve is raised so its statement timeout cannot end
    the wait first."""
    monkeypatch.setattr(app_user_erasure, "FINAL_RESERVE_S", 10)
    u1 = _needle()
    a1 = f"{u1}-a1"
    link(db_session, a1, u1)
    _events(db_session, u1, a1)
    (key,) = _hashes(engine, [u1])

    holder = engine.connect()
    held = holder.begin()
    holder.execute(
        text("SELECT pg_advisory_xact_lock(:space, :key)"),
        {"space": LOCK_SPACE, "key": key},
    )
    result: Dict[str, Any] = {}
    worker = threading.Thread(
        target=lambda: result.update(response=_erase(client, superuser, u1))
    )
    worker.start()
    try:
        assert _wait_for_advisory_waiter(engine), "the erase never waited on the key"
        assert worker.is_alive(), "erase finished while link's key was held"
        assert "response" not in result
    finally:
        held.commit()
        holder.close()
        worker.join(60)

    response = result["response"]
    assert response.status_code == 200, response.text
    assert response.json()["complete"] is True
    assert _left(fresh, [u1, a1]) == {}


@pytest.mark.regression
def test_a_link_waiting_on_the_erase_is_stored_after_it(
    client, db_session, engine, fresh, superuser, monkeypatch
):
    """(iv) with I2a's real ``link``: while the erase holds the person's keys,
    a ``link`` to u1 waits; it stores its link only after the erase commits,
    as data sent after the erase."""
    u1 = _needle()
    a1, a9 = f"{u1}-a1", f"{u1}-a9"
    link(db_session, a1, u1)
    _events(db_session, u1, a1)

    keys_held, go = threading.Event(), threading.Event()
    calls = {"n": 0}

    def pausing(db, user_id):
        calls["n"] += 1
        if calls["n"] == 2:  # the re-read, inside the final transaction
            keys_held.set()
            go.wait(60)
        return erasure_ids(db, user_id)

    monkeypatch.setattr(app_user_erasure, "erasure_ids", pausing)
    result: Dict[str, Any] = {}
    eraser = threading.Thread(
        target=lambda: result.update(response=_erase(client, superuser, u1))
    )
    eraser.start()
    assert keys_held.wait(60), "the erase never reached its final transaction"

    def linker() -> None:
        session = fresh()
        try:
            result["linked"] = link(session, a9, u1)
        finally:
            session.close()

    linking = threading.Thread(target=linker)
    linking.start()
    try:
        assert _wait_for_advisory_waiter(engine), "link never waited on the erase"
        assert "linked" not in result
    finally:
        go.set()
        eraser.join(60)
        linking.join(60)

    assert result["response"].status_code == 200, result["response"].text
    assert result["response"].json()["complete"] is True
    assert result["linked"] is True
    assert _counts(fresh, [a1])["identity_aliases"] == 0
    assert _counts(fresh, [a9])["identity_aliases"] == 1


@pytest.mark.regression
def test_a_link_made_during_the_erase_answers_complete_false_then_a_resend_erases_it(
    client, db_session, fresh, superuser, monkeypatch
):
    """The set grew between the first read and the final transaction: no link
    is deleted and the answer is ``complete: false``; the next call's set has
    the new id and erases its rows."""
    u1 = _needle()
    a1, a3 = f"{u1}-a1", f"{u1}-a3"
    link(db_session, a1, u1)
    _events(db_session, u1, a1, a3)
    calls = {"n": 0}

    def growing(db, user_id):
        calls["n"] += 1
        found = erasure_ids(db, user_id)
        if calls["n"] == 1:  # a link committed after the call read the set
            session = fresh()
            try:
                link(session, a3, u1)
            finally:
                session.close()
        return found

    monkeypatch.setattr(app_user_erasure, "erasure_ids", growing)
    response = _erase(client, superuser, u1)
    assert response.status_code == 200, response.text
    assert response.json()["complete"] is False
    assert response.json()["deleted"]["identity_aliases"] == 0
    assert _counts(fresh, [u1])["identity_aliases"] == 2
    assert _counts(fresh, [a3])["events"] == 1

    monkeypatch.setattr(app_user_erasure, "erasure_ids", erasure_ids)
    again = _erase(client, superuser, u1)
    assert again.status_code == 200, again.text
    assert again.json()["complete"] is True
    assert again.json()["ids_erased"] == 3
    assert _left(fresh, [u1, a1, a3]) == {}
    db_session.query(Event).filter(Event.user_id.in_([u1, a1, a3])).delete()
    db_session.commit()


@pytest.mark.regression
def test_a_call_that_stops_early_keeps_the_links_and_a_resend_erases_the_person(
    client, db_session, fresh, superuser, monkeypatch
):
    """Links last: a call whose chunk phase does not finish deletes no link,
    so the re-send's set still holds every id of the person."""
    u1 = _needle()
    a1 = f"{u1}-a1"
    link(db_session, a1, u1)
    _events(db_session, u1, a1)
    real = app_user_erasure._erase_table

    def out_of_budget(db, table, ids, until):
        if table.name == "events":
            return 0, "budget"
        return real(db, table, ids, until)

    monkeypatch.setattr(app_user_erasure, "_erase_table", out_of_budget)
    response = _erase(client, superuser, u1)
    assert response.status_code == 200, response.text
    assert response.json()["complete"] is False
    assert response.json()["deleted"]["identity_aliases"] == 0
    assert _counts(fresh, [u1])["identity_aliases"] == 1, (
        "links deleted on an incomplete call"
    )

    monkeypatch.setattr(app_user_erasure, "_erase_table", real)
    again = _erase(client, superuser, u1)
    assert again.status_code == 200, again.text
    assert again.json()["complete"] is True
    assert again.json()["ids_erased"] == 2
    assert _left(fresh, [u1, a1]) == {}, "a1's rows orphaned"
