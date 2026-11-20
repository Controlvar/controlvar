"""#449: one person under two ids is one user once the ids are linked.

Before resolution, a visitor who browsed under an anonymous id and then signed
in was two users: the known id got its own variant and its own assignment row,
a conversion it sent without assigning first was stored with no variant, and
the global holdout recorded the person in both arms. Each test links the ids
first through ``identity_aliases.link`` (no request can create a link yet),
and every assignment and event under the known id comes after the link.
"""

import pytest

from backend.app.models.holdout_population import HoldoutPopulation
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.event_matching import count_converting_users
from backend.tests.integration.identity_alias_world import (
    World,
    assign,
    held_and_free,
    no_active_holdout,
    track,
    variant_of,
)
from backend.tests.integration.identity_alias_world import (
    measurable_holdout as holdout_for_test,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


@pytest.fixture
def world(db_session, make_experiment):
    with no_active_holdout(db_session):
        w = World(db_session, make_experiment)
        yield w
        w.close()


def _converters(db_session, exp):
    return {
        v.name: count_converting_users(db_session, exp.id, v.id, "purchase")
        for v in exp.variants
    }


def _value_totals(db_session, exp):
    service = AnalysisService(db_session)
    return {
        v.name: service.value_totals(exp.id, v.id, "purchase") for v in exp.variants
    }


@pytest.mark.regression
def test_one_person_two_ids_counts_once(admin_client, db_session, world):
    exp = world.experiment("Two ids")
    a1, u1 = world.split_pair(exp)
    # Linked before either id is assigned: the person's assignment is made
    # under the known id and hashed on it, whichever id asks first.
    x = world.hashed_variant(exp, u1)
    other = world.hashed_variant(exp, a1)
    assert world.link(a1, u1) is True

    first = assign(admin_client, exp, a1)
    answered = assign(admin_client, exp, u1)
    track(admin_client, exp, a1, value=10.0)
    track(admin_client, exp, u1, value=20.0)

    assert (first["user_id"], answered["user_id"]) == (a1, u1)
    assert (first["variant_name"], answered["variant_name"]) == (x, x), (
        f"a1 answered {first['variant_name']} and u1 {answered['variant_name']}, "
        f"expected {x} for both (one person)"
    )
    persons = len(world.rows(exp, [a1, u1]))
    assert persons == 1, f"persons in experiment: {persons}, expected 1"
    assert _converters(db_session, exp) == {x: 1, other: 0}
    assert _value_totals(db_session, exp) == {x: [30.0], other: []}


@pytest.mark.regression
def test_conversion_after_sign_in_is_tagged_with_the_anonymous_variant(
    admin_client, db_session, world
):
    exp = world.experiment("Sign in")
    a1, u1 = world.split_pair(exp)
    x = world.hashed_variant(exp, a1)
    assert assign(admin_client, exp, a1)["variant_name"] == x
    world.link(a1, u1)

    # u1 converts without ever being assigned under its own id.
    answer = track(admin_client, exp, u1, value=20.0)

    assert answer["user_id"] == u1
    assert answer["variant_id"] == str(variant_of(exp, x).id), (
        f"u1 event variant_id {answer['variant_id']}, expected {x}"
    )
    assert sum(_converters(db_session, exp).values()) == 1


@pytest.mark.regression
def test_holdout_follows_the_person(admin_client, db_session, world):
    exp = world.experiment("Holdout person")
    with holdout_for_test(db_session) as holdout:
        _holdout_follows_the_person(admin_client, db_session, world, exp, holdout)


def _holdout_follows_the_person(admin_client, db_session, world, exp, holdout):
    a1, u1 = held_and_free(holdout, world)
    assert assign(admin_client, exp, a1)["reason"] == "holdout"
    world.link(a1, u1)

    answered = assign(admin_client, exp, u1)

    population = (
        db_session.query(HoldoutPopulation.in_holdout)
        .filter(
            HoldoutPopulation.holdout_id == holdout.id,
            HoldoutPopulation.user_id.in_([a1, u1]),
        )
        .order_by(HoldoutPopulation.first_seen_at)
        .all()
    )
    assert (answered["assigned"], answered["reason"]) == (False, "holdout"), (
        f"u1 assigned while a1 is held out; population {[p[0] for p in population]}"
    )
    assert world.rows(exp, [a1, u1]) == []
    assert [p[0] for p in population] == [True]
