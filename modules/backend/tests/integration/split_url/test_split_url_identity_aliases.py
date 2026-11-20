"""Split URL routing for linked ids (#449).

``POST /api/v1/tracking/split-url/assign`` answers a visitor through
``/tracking/assign``'s own function, so a visitor linked to an assigned id is
sent where that id was sent, and gets no row of its own (IA-S8, with a browser
User-Agent). Nothing in ``modules/backend/app`` changed for it.

Two paths read the id they are given, as they did, and are pinned that way
(stated limits): the crawler branch, which answers a bot by the hash of the id and
records nothing, and the preview, ``GET /api/v1/experiments/{id}/split-url/
preview``, which answers the id's own stored row or hash. For a linked id the
preview can therefore differ from what the route answers.

Links are written through ``identity_aliases.link``; every id here is fresh
and the links are deleted afterwards.
"""

from __future__ import annotations

import uuid
from typing import Dict, Iterator, List

import pytest
from sqlalchemy.orm import Session

from backend.app.models.assignment import Assignment
from backend.app.models.identity_alias import IdentityAlias
from backend.app.services import identity_aliases
from backend.app.services.assignment_service import AssignmentService

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

ASSIGN = "/api/v1/tracking/split-url/assign"
BROWSER = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"
)
CRAWLER = "Mozilla/5.0 (compatible; Googlebot/2.1)"


@pytest.fixture
def linked(db_session: Session) -> Iterator[List[str]]:
    """The ids a test links; their links are deleted afterwards."""
    ids: List[str] = []
    yield ids
    db_session.rollback()
    db_session.query(IdentityAlias).filter(
        IdentityAlias.anonymous_id.in_(ids) | IdentityAlias.user_id.in_(ids)
    ).delete(synchronize_session=False)
    db_session.commit()


def _visit(client, key: str, user_id: str, user_agent: str = BROWSER) -> Dict:
    response = client.post(
        ASSIGN,
        json={
            "experiment_key": key,
            "user_id": user_id,
            "host": "e.com",
            "path": "/checkout",
            "user_agent": user_agent,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _preview(client, experiment, user_id: str) -> str:
    response = client.get(
        f"/api/v1/experiments/{experiment.id}/split-url/preview",
        params={"user_id": user_id},
    )
    assert response.status_code == 200, response.text
    return response.json()["variant_name"]


def _person(db_session, experiment, linked):
    """a1 and u1, fresh, that the hash sends to different variants; a1 is
    then routed, and u1 linked to it. Returns (a1, u1, a1's variant, u1's
    hash variant)."""
    hashed = AssignmentService(db_session)._hash_user_to_variant
    names = {v.id: v.name for v in experiment.variants}
    a1 = f"split-anon-{uuid.uuid4().hex[:10]}"
    u1 = next(
        u
        for u in (f"split-known-{uuid.uuid4().hex[:10]}" for _ in range(200))
        if hashed(u, experiment) != hashed(a1, experiment)
    )
    linked.extend([a1, u1])
    return a1, u1, names[hashed(a1, experiment)], names[hashed(u1, experiment)]


def _rows(db_session, experiment, ids) -> List[str]:
    db_session.expire_all()
    return sorted(
        a.user_id
        for a in db_session.query(Assignment).filter(
            Assignment.experiment_id == experiment.id, Assignment.user_id.in_(ids)
        )
    )


def test_a_linked_visitor_is_sent_where_its_person_was(
    admin_client, split_experiment, db_session, linked
):
    """IA-S8: a browser visitor linked to a routed id is answered that id's
    variant, and no row is stored for it."""
    key = f"alias-split-{uuid.uuid4().hex[:8]}"
    experiment = split_experiment(key)
    a1, u1, held, own = _person(db_session, experiment, linked)
    assert _visit(admin_client, key, a1)["variant_name"] == held
    assert identity_aliases.link(db_session, a1, u1) is True

    answer = _visit(admin_client, key, u1)

    assert answer["variant_name"] == held, (
        f"split-url answered a new variant ({answer['variant_name']}) for an "
        "aliased visitor"
    )
    assert answer["assigned"] is True
    assert _rows(db_session, experiment, [a1, u1]) == [a1]
    assert own != held


def test_the_crawler_branch_reads_the_id_it_is_sent(
    admin_client, split_experiment, db_session, linked
):
    """A stated non-resolution: a crawler sent as u1 is answered u1's own
    hash, as any crawler is, and nothing is recorded for it."""
    key = f"alias-bot-{uuid.uuid4().hex[:8]}"
    experiment = split_experiment(key)
    a1, u1, held, own = _person(db_session, experiment, linked)
    _visit(admin_client, key, a1)
    identity_aliases.link(db_session, a1, u1)

    answer = _visit(admin_client, key, u1, user_agent=CRAWLER)

    assert (answer["reason"], answer["assigned"]) == ("bot", False)
    assert answer["variant_name"] == own != held
    assert _rows(db_session, experiment, [a1, u1]) == [a1]


def test_the_preview_of_a_linked_id_reads_its_own_row_or_hash(
    admin_client, split_experiment, db_session, linked
):
    """An inverted pin: for a linked id the preview answers what
    the id's own stored row, else its hash, gives, not what the route gives.
    stability.md's claim that the two agree holds for ids never linked."""
    key = f"alias-preview-{uuid.uuid4().hex[:8]}"
    experiment = split_experiment(key)
    a1, u1, held, own = _person(db_session, experiment, linked)
    _visit(admin_client, key, a1)
    identity_aliases.link(db_session, a1, u1)

    # u1 holds no row: the preview hashes u1.
    assert _preview(admin_client, experiment, u1) == own
    assert _visit(admin_client, key, u1)["variant_name"] == held

    # A row u1 got before the link (stored straight, as a release that knows
    # nothing of links stores it): the preview answers it, the route a1's.
    b1, v1, b_held, v_own = _person(db_session, experiment, linked)
    _visit(admin_client, key, b1)
    db_session.add(
        Assignment(
            experiment_id=experiment.id,
            user_id=v1,
            variant_id=next(v.id for v in experiment.variants if v.name == v_own),
        )
    )
    db_session.commit()
    identity_aliases.link(db_session, b1, v1)
    assert _preview(admin_client, experiment, v1) == v_own
    assert _visit(admin_client, key, v1)["variant_name"] == b_held != v_own
