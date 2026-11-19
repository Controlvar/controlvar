"""The tracking routes answer a linked id as its person (#449).

Once ``identity_aliases.link(a1, u1)`` has stored that an anonymous id and a
known id are one person, every route that assigns or records for an
experiment resolves the id it is sent:

* ``/tracking/assign`` and ``/assign/batch`` answer the variant of the
  person's assignment stored first, and write no second row (IA-S1 to S5);
* ``GET /tracking/assignments/{user_id}`` lists the person's assignments, one
  per experiment, each under the id that was sent (IA-S6, D5);
* an experiment that is not running answers its status and records nothing
  (IA-S7);
* ``/track``, ``/batch`` and ``/events`` store an event that names an
  experiment under the person's holder, tagged with its variant, and claim
  its ``event_id`` for the id as sent (IA-E1 to E3, D4);
* holdout membership, mutual exclusion and a bandit's arm follow the person
  (IA-H1 to H3);
* rows that already exist stay, and the person is answered with the earliest
  (IA-C1, IA-M7);
* the resolution costs a bounded number of statements, however many ids the
  person has (IA-C8), and flags are not resolved at all (IA-F1).

Every response echoes the id that was sent. No request can create a link yet;
the tests create them through ``identity_aliases.link``.
"""

from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, List

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.models.assignment import Assignment
from backend.app.models.bandit_state import BanditState
from backend.app.models.event import Event
from backend.app.models.experiment import ExperimentStatus, ExperimentType
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.holdout_population import HoldoutPopulation
from backend.app.models.mutual_exclusion_group import (
    MutualExclusionGroup,
    MutualExclusionGroupStatus,
)
from backend.app.models.user import UserRole
from backend.app.services import identity_aliases
from backend.app.services.event_matching import count_converting_users
from backend.app.services.global_holdout_service import GlobalHoldoutService
from backend.app.services.mutual_exclusion_service import MutualExclusionService
from backend.tests.integration.api.route_helpers import (
    api_client,
    api_key,
    local_sign_in,
    make_user,
)
from backend.tests.integration.identity_alias_world import (
    World,
    assign,
    held_and_free,
    measurable_holdout,
    no_active_holdout,
    track,
    variant_of,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SCHEMA = "test_experimentation"
ITEM_KEYS = {
    "id",
    "user_id",
    "experiment_id",
    "experiment_name",
    "variant_id",
    "variant_name",
    "is_control",
    "created_at",
}


@pytest.fixture
def world(db_session, make_experiment):
    with no_active_holdout(db_session):
        w = World(db_session, make_experiment)
        yield w
        w.close()


def _naive_utc(delta: timedelta = timedelta()) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + delta


def _exposures(world: World, exp, ids) -> List[str]:
    return [e.user_id for e in world.events(exp, ids) if e.event_type == "exposure"]


# ---------------------------------------------------------------------------
# Assign (IA-S1 to IA-S7)
# ---------------------------------------------------------------------------


def test_a_known_id_answers_the_variant_its_anonymous_id_holds(admin_client, world):
    """IA-S1."""
    exp = world.experiment("S1")
    a1, u1 = world.split_pair(exp)
    x = world.hashed_variant(exp, a1)
    assert assign(admin_client, exp, a1)["variant_name"] == x
    world.link(a1, u1)

    answer = assign(admin_client, exp, u1)

    assert answer["user_id"] == u1
    assert answer["variant_name"] == x, (
        f"u1 answered {answer['variant_name']} (its own hash), expected {x} "
        "(a1's assignment)"
    )
    rows = world.rows(exp, [a1, u1])
    assert [r.user_id for r in rows] == [a1], (
        f"persons in experiment: {len(rows)}, expected 1"
    )
    # Seen twice, both times recorded for the row's holder.
    assert _exposures(world, exp, [a1, u1]) == [a1, a1]


def test_an_anonymous_id_and_a_second_device_answer_the_known_ids_variant(
    admin_client, world
):
    """IA-S2: resolution runs from the anonymous side as well."""
    exp = world.experiment("S2")
    u1 = world.id("known")
    x = world.hashed_variant(exp, u1)
    a1 = world.hashed_elsewhere(exp, x, "anon")
    a2 = world.hashed_elsewhere(exp, x, "second")
    assert assign(admin_client, exp, u1)["variant_name"] == x

    world.link(a1, u1)
    first = assign(admin_client, exp, a1)
    world.link(a2, u1)
    second = assign(admin_client, exp, a2)

    assert (first["user_id"], second["user_id"]) == (a1, a2)
    assert first["variant_name"] == x, f"a1 answered its own hash, expected {x}"
    assert second["variant_name"] == x, f"a2 answered its own hash, expected {x}"
    assert [r.user_id for r in world.rows(exp, [u1, a1, a2])] == [u1]


def test_a_person_with_no_row_is_assigned_once_under_the_known_id(admin_client, world):
    """IA-S3: linked before either id is assigned, the known id first."""
    exp = world.experiment("S3")
    a1, u1 = world.split_pair(exp)
    x = world.hashed_variant(exp, u1)
    world.link(a1, u1)

    answers = [assign(admin_client, exp, u)["variant_name"] for u in (u1, a1)]

    assert answers == [x, x], f"a1 and u1 answered {answers}, expected {x} twice"
    assert [r.user_id for r in world.rows(exp, [a1, u1])] == [u1]


def test_the_earliest_assignment_wins_for_both_ids(admin_client, world):
    """IA-S4: two rows stored before the link; both ids answer the first,
    every time, and both rows stay."""
    exp = world.experiment("S4")
    a1, u1 = world.id("anon"), world.id("known")
    world.store_assignment(exp, a1, "control", _naive_utc(-timedelta(hours=2)))
    world.store_assignment(exp, u1, "treatment", _naive_utc(-timedelta(hours=1)))
    world.link(a1, u1)

    for _ in range(3):
        for user_id in (u1, a1):
            answer = assign(admin_client, exp, user_id)
            assert answer["variant_name"] == "control", (
                f"{user_id} answered {answer['variant_name']}; earliest is control"
            )
    assert [r.user_id for r in world.rows(exp, [a1, u1])] == [a1, u1]


@pytest.fixture
def batch_key(db_session, monkeypatch):
    """A client with no dependency override but the database, and an
    ``sdk:ruleset`` key whose owner can change flags and experiments."""
    local_sign_in(monkeypatch)
    with api_client(db_session) as client:
        owner = make_user(db_session, UserRole.DEVELOPER, tag="alias")
        yield client, api_key(client, owner, scopes=["sdk:ruleset"])


def test_the_batch_answers_a_linked_id_with_its_persons_variant(world, batch_key):
    """IA-S5: a new user beside it is hashed; the batch records no view."""
    client, key = batch_key
    exp = world.experiment("S5")
    a1, u1 = world.split_pair(exp)
    x = world.hashed_variant(exp, a1)
    first = client.post(
        "/api/v1/tracking/assign",
        json={"experiment_key": exp.key, "user_id": a1},
        headers=key,
    )
    assert first.status_code == 200, first.text
    world.link(a1, u1)
    newcomer = world.id("fresh")

    response = client.post(
        "/api/v1/tracking/assign/batch",
        json={
            "experiment_key": exp.key,
            "users": [{"user_id": u1}, {"user_id": newcomer}],
        },
        headers=key,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    names = {vid: v["name"] for vid, v in body["variants"].items()}
    answered = {a["user_id"]: names[a["variant_id"]] for a in body["assignments"]}
    assert answered == {u1: x, newcomer: world.hashed_variant(exp, newcomer)}, (
        f"batch answered u1 {answered.get(u1)}, expected {x}"
    )
    assert [r.user_id for r in world.rows(exp, [a1, u1, newcomer])] == [
        a1,
        newcomer,
    ]
    assert _exposures(world, exp, [a1, u1, newcomer]) == [a1]


def test_the_assignments_list_answers_a_linked_id_with_one_row_per_experiment(
    admin_client, world
):
    """IA-S6 (D5): the holder rule in each experiment, under the id sent."""
    e1, e2, e3 = (world.experiment(f"S6 {i}") for i in (1, 2, 3))
    a1, u1 = world.id("anon"), world.id("known")
    t0 = _naive_utc(-timedelta(hours=3))
    held = {
        e1.id: world.store_assignment(e1, a1, "treatment", t0),
        e2.id: world.store_assignment(e2, u1, "control", t0),
        e3.id: world.store_assignment(e3, a1, "control", t0),
    }
    world.store_assignment(e3, u1, "treatment", t0 + timedelta(hours=1))
    world.link(a1, u1)
    ours = {str(e.id) for e in (e1, e2, e3)}

    for asked in (u1, a1):
        response = admin_client.get(f"/api/v1/tracking/assignments/{asked}")
        assert response.status_code == 200, response.text
        items = [i for i in response.json() if i["experiment_id"] in ours]
        assert len(items) == 3, f"{len(items)} assignments expected 3: {items}"
        for item in items:
            assert set(item) == ITEM_KEYS
            assert item["user_id"] == asked
        got = {i["experiment_id"]: (i["id"], i["variant_name"]) for i in items}
        assert got == {
            str(e.id): (str(held[e.id].id), variant_name)
            for e, variant_name in ((e1, "treatment"), (e2, "control"), (e3, "control"))
        }

    lone = world.id("lone")
    world.store_assignment(e1, lone, "control", t0)
    alone = admin_client.get(f"/api/v1/tracking/assignments/{lone}").json()
    assert [(i["user_id"], i["variant_name"]) for i in alone] == [(lone, "control")]


def test_a_paused_experiment_answers_a_linked_id_with_its_status(admin_client, world):
    """IA-S7: nothing is recorded for either id."""
    exp = world.experiment("S7", status=ExperimentStatus.PAUSED)
    a1, u1 = world.id("anon"), world.id("known")
    world.link(a1, u1)

    answer = assign(admin_client, exp, u1)

    assert (answer["assigned"], answer["reason"]) == (False, "paused")
    assert answer["user_id"] == u1
    assert world.rows(exp, [a1, u1]) == []
    assert world.events(exp, [a1, u1]) == []


# ---------------------------------------------------------------------------
# Events (IA-E1 to IA-E3)
# ---------------------------------------------------------------------------


def test_a_known_ids_conversion_counts_for_its_persons_variant(
    admin_client, db_session, world
):
    """IA-E1: u1 tracks with the experiment's key and was never assigned."""
    exp = world.experiment("E1")
    a1, u1 = world.split_pair(exp)
    x = variant_of(exp, world.hashed_variant(exp, a1))
    assign(admin_client, exp, a1)
    world.link(a1, u1)

    answer = track(admin_client, exp, u1, value=20.0)

    assert answer["user_id"] == u1
    assert answer["variant_id"] == str(x.id), (
        f"u1 event variant_id {answer['variant_id']}, expected {x.name}"
    )
    stored = [
        (e.user_id, e.variant_id) for e in world.events(exp, [a1, u1], "purchase")
    ]
    assert stored == [(a1, x.id)]
    converters = sum(
        count_converting_users(db_session, exp.id, v.id, "purchase")
        for v in exp.variants
    )
    assert converters == 1


def test_the_event_batch_stores_a_known_ids_events_under_its_person(
    admin_client, world
):
    """IA-E2, ``/tracking/batch``."""
    exp = world.experiment("E2 batch")
    a1, u1 = world.split_pair(exp)
    x = variant_of(exp, world.hashed_variant(exp, a1))
    assign(admin_client, exp, a1)
    world.link(a1, u1)

    response = admin_client.post(
        "/api/v1/tracking/batch",
        json={
            "events": [
                {
                    "event_type": name,
                    "event_name": name,
                    "user_id": u1,
                    "experiment_key": exp.key,
                }
                for name in ("purchase", "purchase", "page_view")
            ]
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["success_count"] == 3
    stored = [e for e in world.events(exp, [a1, u1]) if e.event_type != "exposure"]
    tagged = [e for e in stored if e.user_id == a1 and e.variant_id == x.id]
    assert len(stored) == 3
    assert len(tagged) == 3, f"{len(tagged)} of 3 events tagged"


def test_an_event_by_ids_is_stored_under_its_person_with_the_variant_given(
    admin_client, world
):
    """IA-E2, ``POST /tracking/events``: the variant is the caller's."""
    exp = world.experiment("E2 ids")
    a1, u1 = world.split_pair(exp)
    assign(admin_client, exp, a1)
    world.link(a1, u1)
    given = variant_of(exp, "treatment")

    response = admin_client.post(
        "/api/v1/tracking/events",
        json={
            "event_type": "purchase",
            "event_name": "purchase",
            "user_id": u1,
            "experiment_id": str(exp.id),
            "variant_id": str(given.id),
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["user_id"] == u1
    stored = [
        (e.user_id, e.variant_id) for e in world.events(exp, [a1, u1], "purchase")
    ]
    assert stored == [(a1, given.id)]


def test_an_event_id_is_stored_once_per_id_as_sent(admin_client, world):
    """IA-E3: the same event_id under a1, then under u1 after the link, is two
    rows; u1 sending it again is a duplicate of u1's."""
    exp = world.experiment("E3")
    a1, u1 = world.split_pair(exp)
    assign(admin_client, exp, a1)
    order = f"order-{uuid.uuid4().hex[:8]}"
    first = track(admin_client, exp, a1, value=5.0, event_id=order)
    world.link(a1, u1)

    second = track(admin_client, exp, u1, value=5.0, event_id=order)
    retry = track(admin_client, exp, u1, value=5.0, event_id=order)

    assert (first["duplicate"], second["duplicate"], retry["duplicate"]) == (
        False,
        False,
        True,
    )
    assert retry["id"] == second["id"] != first["id"]
    assert (second["user_id"], retry["user_id"]) == (u1, u1)
    stored = world.events(exp, [a1, u1], "purchase")
    assert [e.user_id for e in stored] == [a1, a1]


# ---------------------------------------------------------------------------
# Holdout, mutual exclusion, bandits (IA-H1 to IA-H3)
# ---------------------------------------------------------------------------


def test_holdout_membership_comes_from_the_persons_first_population_row(
    admin_client, db_session, world
):
    """IA-H1, the other way round from REGRESSION 3: a1 is recorded outside
    the holdout, so u1, whom the hash would hold out, is assigned, and the
    person stays one population row."""
    e1, e2 = world.experiment("H1 first"), world.experiment("H1 second")
    with measurable_holdout(db_session) as holdout:
        u1, a1 = held_and_free(holdout, world)
        assert assign(admin_client, e1, a1)["reason"] == "assigned"
        world.link(a1, u1)

        answer = assign(admin_client, e2, u1)

        population = (
            db_session.query(HoldoutPopulation.user_id, HoldoutPopulation.in_holdout)
            .filter(
                HoldoutPopulation.holdout_id == holdout.id,
                HoldoutPopulation.user_id.in_([a1, u1]),
            )
            .all()
        )
        assert (answer["assigned"], answer["reason"]) == (True, "assigned"), (
            f"u1 held out while a1 is not; population {population}"
        )
        assert population == [(a1, False)]
        assert [r.user_id for r in world.rows(e2, [a1, u1])] == [u1]


@pytest.fixture
def exclusion_group(db_session):
    group = MutualExclusionGroup(
        name=f"alias-meg-{uuid.uuid4().hex[:8]}",
        description="identity alias exclusion",
        traffic_allocation=1.0,
        status=MutualExclusionGroupStatus.ACTIVE,
    )
    db_session.add(group)
    db_session.commit()
    db_session.refresh(group)
    made = []
    yield group, made
    db_session.rollback()
    for exp in made:
        exp.mutual_exclusion_group_id = None
    db_session.commit()
    db_session.query(MutualExclusionGroup).filter(
        MutualExclusionGroup.id == group.id
    ).delete()
    db_session.commit()


def test_mutual_exclusion_holds_across_a_persons_ids(
    admin_client, db_session, world, exclusion_group
):
    """IA-H2: the group's hash picks E2 for u1, but a1 is enrolled in E1."""
    group, made = exclusion_group
    e1 = world.experiment("H2 one", mutual_exclusion_group_id=group.id)
    e2 = world.experiment("H2 two", mutual_exclusion_group_id=group.id)
    made.extend([e1, e2])
    service = MutualExclusionService(db_session)

    def picked(experiment, prefix):
        while True:
            user_id = world.id(prefix)
            if service.select_experiment_for_user(user_id, group.id) == experiment.id:
                return user_id

    a1, u1 = picked(e1, "anon"), picked(e2, "known")
    assert assign(admin_client, e1, a1)["reason"] == "assigned"
    world.link(a1, u1)

    answer = assign(admin_client, e2, u1)

    assert (answer["assigned"], answer["reason"]) == (False, "mutual_exclusion"), (
        "u1 enrolled in E2 while a1 holds E1"
    )
    assert world.rows(e2, [a1, u1]) == []


def _bandit_weights(db_session, exp, weights) -> None:
    state = (
        db_session.query(BanditState)
        .filter(BanditState.experiment_id == exp.id)
        .first()
    )
    by_id = {str(variant_of(exp, name).id): w for name, w in weights.items()}
    if state is None:
        db_session.add(
            BanditState(
                experiment_id=exp.id,
                algorithm="thompson_sampling",
                variant_weights=by_id,
                total_pulls=0,
            )
        )
    else:
        state.variant_weights = by_id
    db_session.commit()


def test_a_bandit_keeps_the_arm_a_linked_person_holds(admin_client, db_session, world):
    """IA-H3: the weights now send every new user to treatment."""
    exp = world.experiment(
        "H3",
        experiment_type=ExperimentType.BANDIT,
        optimization_type="thompson_sampling",
    )
    a1, u1 = world.id("anon"), world.id("known")
    _bandit_weights(db_session, exp, {"control": 1.0, "treatment": 0.0})
    assert assign(admin_client, exp, a1)["variant_name"] == "control"
    _bandit_weights(db_session, exp, {"control": 0.0, "treatment": 1.0})
    world.link(a1, u1)

    answer = assign(admin_client, exp, u1)

    assert answer["variant_name"] == "control", "u1 routed to treatment by weight"
    pulls = {
        v.name: sum(1 for r in world.rows(exp, [a1, u1]) if r.variant_id == v.id)
        for v in exp.variants
    }
    assert pulls == {"control": 1, "treatment": 0}


# ---------------------------------------------------------------------------
# Rows that already exist (IA-C1, IA-M7)
# ---------------------------------------------------------------------------


def test_a_link_racing_the_known_ids_first_assignment(
    admin_client, db_session, world, monkeypatch
):
    """IA-C1: the link commits in another session between u1's read of its
    person and its insert, so u1 stores a row of its own. Both rows stay
    (counted per row), and from then on both ids answer the earlier one."""
    exp = world.experiment("C1")
    a1, u1 = world.split_pair(exp)
    x = world.hashed_variant(exp, a1)
    assign(admin_client, exp, a1)
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False)
    original = GlobalHoldoutService.is_user_in_holdout
    raced = []

    def link_meanwhile(self, user_id, *args, **kwargs):
        if user_id == u1 and not raced:
            other = factory()
            other.execute(text(f"SET search_path TO {SCHEMA}"))
            try:
                raced.append(identity_aliases.link(other, a1, u1))
            finally:
                other.close()
        return original(self, user_id, *args, **kwargs)

    monkeypatch.setattr(GlobalHoldoutService, "is_user_in_holdout", link_meanwhile)
    during = assign(admin_client, exp, u1)
    monkeypatch.setattr(GlobalHoldoutService, "is_user_in_holdout", original)

    assert raced == [True]
    assert during["variant_name"] == world.hashed_variant(exp, u1)
    after = [assign(admin_client, exp, u)["variant_name"] for u in (u1, a1)]
    assert after == [x, x], f"u1 answers {after[0]} after the race, expected {x}"
    assert [r.user_id for r in world.rows(exp, [a1, u1])] == [a1, u1]


def test_a_row_a_release_without_links_wrote_is_answered_by_the_earliest(
    admin_client, world
):
    """IA-M7: during a rolling upgrade a task that ignores links stores u1 a
    row of its own after the link; this release answers both ids with a1's,
    and both rows stay, counted per row."""
    exp = world.experiment("M7")
    a1, u1 = world.split_pair(exp)
    x = world.hashed_variant(exp, a1)
    assign(admin_client, exp, a1)
    world.link(a1, u1)
    world.store_assignment(exp, u1, world.hashed_variant(exp, u1), _naive_utc())

    answers = [assign(admin_client, exp, u)["variant_name"] for u in (u1, a1)]

    assert answers == [x, x], f"u1 answers its N-1 row ({answers[0]}), expected {x}"
    assert [r.user_id for r in world.rows(exp, [a1, u1])] == [a1, u1]


# ---------------------------------------------------------------------------
# Cost (IA-C8) and flags (IA-F1)
# ---------------------------------------------------------------------------


@contextmanager
def _statements(engine) -> Iterator[List[str]]:
    seen: List[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    sa_event.listen(engine, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        sa_event.remove(engine, "before_cursor_execute", before)


def _cost(client, engine, exp, user_id) -> int:
    with _statements(engine) as seen:
        assign(client, exp, user_id)
    return len(seen)


#: Statements of one ``/tracking/assign``, no holdout active: an id never
#: linked (NO_HOLDOUT_COUNTS in test_tracking_api.py), and a linked id, new
#: and sticky. The person is read once, and for a linked id the holder's row
#: is read in place of the id's own, whatever the number of the person's ids.
UNLINKED_NEW, UNLINKED_STICKY = 14, 11
LINKED_NEW, LINKED_STICKY = 14, 11


def test_resolution_costs_the_same_statements_however_many_ids(
    admin_client, db_session, world
):
    """IA-C8: exact counts, with one link and with 20 more (fan-in)."""
    engine = db_session.get_bind()
    lone = world.id("lone")
    plain = world.experiment("C8 plain")
    assert _cost(admin_client, engine, plain, lone) == UNLINKED_NEW
    assert _cost(admin_client, engine, plain, lone) == UNLINKED_STICKY

    u1 = world.id("known")
    world.link(world.id("anon"), u1)
    costs = []
    for _ in range(2):
        exp = world.experiment("C8 linked")
        costs.append(
            (_cost(admin_client, engine, exp, u1), _cost(admin_client, engine, exp, u1))
        )
        for i in range(20):
            world.link(world.id(f"fan{i:02d}"), u1)
    assert costs == [(LINKED_NEW, LINKED_STICKY)] * 2, (
        f"statements {costs}, expected {(LINKED_NEW, LINKED_STICKY)} each"
    )


def _flag_bucket(user_id: str, flag_key: str) -> int:
    digest = hashlib.md5(f"{user_id}:{flag_key}".encode(), usedforsecurity=False)
    return int(digest.hexdigest(), 16) % 100


@pytest.fixture
def half_flag(db_session, admin_user):
    flag = FeatureFlag(
        key=f"alias-flag-{uuid.uuid4().hex[:8]}",
        name="Alias flag",
        status=FeatureFlagStatus.ACTIVE,
        owner_id=admin_user.id,
        rollout_percentage=50,
    )
    db_session.add(flag)
    db_session.commit()
    db_session.refresh(flag)
    yield flag
    db_session.rollback()
    db_session.query(FeatureFlag).filter(FeatureFlag.id == flag.id).delete()
    db_session.commit()


def test_flags_are_not_resolved_through_a_link(admin_client, world, half_flag):
    """IA-F1: evaluation stays md5(user_id:flag_key) of the id sent."""
    a1 = next(
        u
        for u in (world.id("anon") for _ in range(200))
        if _flag_bucket(u, half_flag.key) < 50
    )
    u1 = next(
        u
        for u in (world.id("known") for _ in range(200))
        if _flag_bucket(u, half_flag.key) >= 50
    )
    world.link(a1, u1)

    served = {}
    for user_id in (a1, u1):
        response = admin_client.get(
            f"/api/v1/feature-flags/evaluate/{half_flag.key}",
            params={"user_id": user_id},
        )
        assert response.status_code == 200, response.text
        served[user_id] = response.json()["enabled"]

    assert served == {a1: True, u1: False}, "u1 answered a1's rollout bucket"
