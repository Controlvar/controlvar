"""The link rules of ``identity_aliases`` (#449), against PostgreSQL.

``link`` is the one writer of ``identity_aliases`` and no request reaches it
yet, so the rules are pinned here at the service level: an existing link
answers ``False``; a second known id, a chain, a cycle and the 101st anonymous
id raise ``AliasConflict``, whose text names a rule and never an id; two equal
ids are refused by the table's check constraint. ``person_of`` of any id of a
person is the person's whole id set, which is also the set an erasure covers.
The advisory locks are taken in ``hashtext`` order, so two links that share a
key never deadlock, even when two different ids share a ``hashtext`` value.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.models.identity_alias import IDS_DIFFER_CHECK, IdentityAlias
from backend.app.services import identity_aliases
from backend.app.services.identity_aliases import (
    ANONYMOUS_ID_HAS_LINKS,
    ANONYMOUS_ID_LINKED_ELSEWHERE,
    CONFLICT_REASONS,
    MAX_ANONYMOUS_IDS_PER_USER,
    USER_ID_AT_CAP,
    USER_ID_IS_LINKED,
    AliasConflict,
    Person,
    holder_of,
    lock_keys,
    person_of,
)
from backend.tests.integration.identity_alias_world import World

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SCHEMA = "test_experimentation"

#: Two ids PostgreSQL's ``hashtext`` gives the same value (found by a search
#: among 300,000 ids), and an id whose text sorts between them, so text order
#: and key order differ for every pair of links below.
COLLIDING = ("visitor-232720", "visitor-32076")
BETWEEN = "visitor-3"


@pytest.fixture
def world(db_session, make_experiment):
    w = World(db_session, make_experiment)
    yield w
    w.close()


def _links(db_session, *ids) -> List[tuple]:
    db_session.expire_all()
    return sorted(
        (row.anonymous_id, row.user_id)
        for row in db_session.query(IdentityAlias).filter(
            IdentityAlias.anonymous_id.in_(ids) | IdentityAlias.user_id.in_(ids)
        )
    )


def _refusal(db_session, anonymous_id, user_id) -> AliasConflict:
    with pytest.raises(AliasConflict) as raised:
        identity_aliases.link(db_session, anonymous_id, user_id)
    conflict = raised.value
    assert conflict.reason in CONFLICT_REASONS
    assert anonymous_id not in str(conflict) and user_id not in str(conflict)
    return conflict


# ---------------------------------------------------------------------------
# person_of
# ---------------------------------------------------------------------------


def test_an_id_never_linked_is_a_person_of_one_id(db_session, world):
    lone = world.id("lone")
    person = person_of(db_session, lone)
    assert person == Person(lone, (lone,))
    assert person.linked is False


def test_every_id_of_a_star_gives_the_whole_person(db_session, world):
    """IA-P2: three anonymous ids and their known id are one 4-id set, read
    from any of the four: the set an erasure of that person covers."""
    u1 = world.id("known")
    anonymous = [world.id(f"anon{i}") for i in range(3)]
    for a in anonymous:
        assert world.link(a, u1) is True

    expected = (u1, *sorted(anonymous))
    for asked in (u1, *anonymous):
        person = person_of(db_session, asked)
        assert person.ids == expected, f"closure {set(person.ids)}, expected 4 ids"
        assert person.known_id == u1
        assert person.linked is True


# ---------------------------------------------------------------------------
# link: the rules (IA-C2 to IA-C6, service level)
# ---------------------------------------------------------------------------


def test_the_same_link_twice_is_created_then_not(db_session, world):
    """IA-C2."""
    a1, u1 = world.id("anon"), world.id("known")
    assert identity_aliases.link(db_session, a1, u1) is True
    assert identity_aliases.link(db_session, a1, u1) is False
    assert _links(db_session, a1, u1) == [(a1, u1)]


def test_an_anonymous_id_is_linked_to_one_known_id(db_session, world):
    """IA-C3."""
    a1, u1, u2 = world.id("anon"), world.id("known"), world.id("other")
    world.link(a1, u1)
    conflict = _refusal(db_session, a1, u2)
    assert conflict.reason == ANONYMOUS_ID_LINKED_ELSEWHERE
    assert _links(db_session, a1, u1, u2) == [(a1, u1)]


def test_links_are_one_level_deep(db_session, world):
    """IA-C4: no chain and no cycle, from either side."""
    a1, u1, u2, x = (world.id(p) for p in ("anon", "known", "other", "x"))
    world.link(a1, u1)

    # a known id with links is never linked itself (a chain) ...
    assert _refusal(db_session, u1, u2).reason == ANONYMOUS_ID_HAS_LINKS
    # ... nor linked back to its own anonymous id (a cycle) ...
    assert _refusal(db_session, u1, a1).reason == ANONYMOUS_ID_HAS_LINKS
    # ... and nothing is linked to an id linked as an anonymous id.
    assert _refusal(db_session, x, a1).reason == USER_ID_IS_LINKED
    assert _links(db_session, a1, u1, u2, x) == [(a1, u1)]


def test_two_equal_ids_are_refused_by_the_check_constraint(db_session, world):
    """IA-C5: nothing is written."""
    same = world.id("same")
    with pytest.raises(IntegrityError) as raised:
        identity_aliases.link(db_session, same, same)
    assert IDS_DIFFER_CHECK in str(raised.value.orig)
    assert _links(db_session, same) == []


def test_a_known_id_takes_100_anonymous_ids_and_not_101(db_session, world):
    """IA-C6, and the person query at the cap."""
    u1 = world.id("capped")
    anonymous = [world.id(f"a{i:03d}") for i in range(MAX_ANONYMOUS_IDS_PER_USER)]
    assert MAX_ANONYMOUS_IDS_PER_USER == 100
    created = [identity_aliases.link(db_session, a, u1) for a in anonymous]
    assert created == [True] * 100

    extra = world.id("a100")
    assert _refusal(db_session, extra, u1).reason == USER_ID_AT_CAP
    assert len(_links(db_session, u1)) == 100
    assert len(person_of(db_session, anonymous[0]).ids) == 101
    assert len(person_of(db_session, extra).ids) == 1


# ---------------------------------------------------------------------------
# holder_of
# ---------------------------------------------------------------------------


def test_the_holder_is_the_earliest_row_then_the_smallest_id(db_session, world):
    exp = world.experiment("Holder")
    a1, a2, u1 = world.id("a1"), world.id("a2"), world.id("u1")
    for a in (a1, a2):
        world.link(a, u1)
    person = person_of(db_session, u1)

    assert holder_of(db_session, person, exp.id) == (u1, None)

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = world.store_assignment(exp, u1, "treatment", t0 + timedelta(hours=1))
    assert holder_of(db_session, person, exp.id) == (u1, later)
    tied = [world.store_assignment(exp, a, "control", t0) for a in (a1, a2)]
    holder, row = holder_of(db_session, person, exp.id)
    first = min(tied, key=lambda r: str(r.id))
    assert (holder, row.id) == (first.user_id, first.id)


# ---------------------------------------------------------------------------
# The lock order: distinct hashtext keys, in numeric order
# ---------------------------------------------------------------------------


def test_lock_keys_are_distinct_and_in_numeric_order():
    assert lock_keys([7, -3, 7]) == [-3, 7]
    assert lock_keys([5, 5]) == [5]


@pytest.fixture
def session_factory(db_session):
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False)

    def make():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        session.commit()
        return session

    return make


def _hashtext(db_session, value: str) -> int:
    return db_session.execute(text("SELECT hashtext(:v)"), {"v": value}).scalar_one()


def _race(engine, session_factory, links) -> Dict[tuple, object]:
    """Run each ``(anonymous_id, user_id)`` link in its own thread and session.

    Each thread waits, after taking its first advisory lock, until every
    thread has taken one (or 3 s have passed: a thread blocked on its first
    lock never arrives). So two links whose first locks differ each hold one
    key when they ask for their second. Returns each link's result: True,
    False, or the exception it raised.
    """
    barrier = threading.Barrier(len(links), timeout=3)
    taken: Dict[int, int] = {}
    results: Dict[tuple, object] = {}

    def after(conn, cursor, statement, parameters, context, executemany):
        me = threading.get_ident()
        if me not in taken or "pg_advisory_xact_lock" not in statement:
            return
        taken[me] += 1
        if taken[me] == 1:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass

    def run(pair):
        taken[threading.get_ident()] = 0
        session = session_factory()
        try:
            results[pair] = identity_aliases.link(session, *pair)
        except Exception as exc:  # the result under test
            results[pair] = exc
        finally:
            session.close()

    event.listen(engine, "after_cursor_execute", after)
    try:
        threads = [threading.Thread(target=run, args=(pair,)) for pair in links]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads)
    finally:
        event.remove(engine, "after_cursor_execute", after)
    return results


def _deadlocks(results) -> List[str]:
    return [
        str(r.orig)
        for r in results.values()
        if isinstance(r, DBAPIError) and getattr(r.orig, "pgcode", None) == "40P01"
    ]


@pytest.fixture
def colliding_ids(db_session, world):
    """The fixed ids, with no link left over from an earlier test."""
    p, r = COLLIDING
    q = BETWEEN
    assert _hashtext(db_session, p) == _hashtext(db_session, r), "no collision"
    assert p < q < r, "text order would not cross"
    db_session.query(IdentityAlias).filter(
        IdentityAlias.anonymous_id.in_([p, q, r]) | IdentityAlias.user_id.in_([p, q, r])
    ).delete(synchronize_session=False)
    db_session.commit()
    world.ids.extend([p, q, r])
    return p, q, r


def test_two_links_sharing_a_colliding_key_do_not_deadlock(
    db_session, session_factory, colliding_ids
):
    """link(P, Q) and link(Q, R), P and R colliding: in text order each would
    hold one key and wait for the other's (40P01); in key order both take the
    same key first, so one waits and both finish. They are a chain, so one
    stores its link and the other is refused by the one-level rule."""
    p, q, r = colliding_ids
    results = _race(db_session.get_bind(), session_factory, [(p, q), (q, r)])

    assert _deadlocks(results) == []
    created = [pair for pair, v in results.items() if v is True]
    refused = [v.reason for v in results.values() if isinstance(v, AliasConflict)]
    assert len(created) == 1, results
    assert refused in ([ANONYMOUS_ID_HAS_LINKS], [USER_ID_IS_LINKED]), results
    assert _links(db_session, p, q, r) == created


def test_two_links_to_one_known_id_with_colliding_anonymous_ids_both_commit(
    db_session, session_factory, colliding_ids
):
    """link(P, Q) and link(R, Q): the same pair of keys, both links valid."""
    p, q, r = colliding_ids
    results = _race(db_session.get_bind(), session_factory, [(p, q), (r, q)])

    assert _deadlocks(results) == []
    assert results == {(p, q): True, (r, q): True}
    assert _links(db_session, p, q, r) == [(p, q), (r, q)]
