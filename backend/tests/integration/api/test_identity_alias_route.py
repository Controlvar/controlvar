"""``POST /api/v1/tracking/alias`` (beta) against a real database (#449).

The route links an anonymous id to a known id through
``identity_aliases.link``, the one writer of ``identity_aliases``. Only
``deps.get_db`` is overridden: keys are created through
``POST /api/v1/api-keys`` and checked for real.

* Its answers: 200 with ``created`` true and then false for the same link
  (IA-C2); 409 with a fixed text for each rule a link breaks (IA-C3, C4, C6);
  the request model's 422 for equal ids (IA-C5), the two shared placeholders
  in either field (IA-C7) and a missing, empty, over-long or unstorable field
  (IA-K3). No answer contains either id, and nothing is written on a refusal.
* Its 401s and 403s are those of ``POST /api/v1/tracking/assign/batch`` up to
  and including ``require_sdk_ruleset_key``, in the same order and words, and
  come before the body is read (IA-K1). The batch route's experiment-owner
  rule is not taken: the full profile's custom roles pin that
  (``modules/backend/tests/integration/rbac/test_rbac_alias_key.py``).
* A link written through the route is the one the tracking routes resolve.
* Without the ``identity_aliases`` table (after its downgrade, before the API
  is rolled back) the routes that read links answer 500.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional

import pytest
from sqlalchemy import text

from backend.app.api.sdk_scope import MISSING_SCOPE_DETAIL, OWNER_ROLE_DETAIL
from backend.app.models.api_key import APIKey
from backend.app.models.event import Event
from backend.app.models.identity_alias import IdentityAlias
from backend.app.models.user import UserRole
from backend.app.services.identity_aliases import (
    MAX_ANONYMOUS_IDS_PER_USER,
    link,
    person_of,
)
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from backend.tests.integration.identity_alias_world import (
    World,
    no_active_holdout,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

URL = "/api/v1/tracking/alias"
BATCH = "/api/v1/tracking/assign/batch"
KEYS = "/api/v1/api-keys"
SCHEMA = "test_experimentation"

#: The 409 texts, word for word.
LINKED_ELSEWHERE = (
    "That anonymous_id is already linked to a different user_id. An anonymous_id "
    "can be linked to one user_id."
)
HAS_LINKS = (
    "That anonymous_id already has other ids linked to it, so it cannot itself be "
    "linked. Links are one level deep; check that the two ids are not swapped."
)
USER_ID_LINKED = (
    "That user_id is itself linked to another user_id, so nothing can be linked to "
    "it. Links are one level deep; link to the user_id it is linked to, or check "
    "that the two ids are not swapped."
)
AT_CAP = (
    "That user_id already has 100 anonymous_ids linked to it, the most allowed, so "
    "this anonymous_id was not linked."
)
EQUAL_IDS = "anonymous_id and user_id must differ."
PLACEHOLDER = (
    "must identify one visitor or customer: 'anonymous' and 'anon' are placeholders "
    "shared by many and cannot be linked"
)


@pytest.fixture(autouse=True)
def _local_sign_in(monkeypatch):
    local_sign_in(monkeypatch)


class Ids:
    """Fresh ids for one test; the links naming any of them are removed after."""

    def __init__(self, db_session) -> None:
        self.db = db_session
        self.made: List[str] = []

    def __call__(self, prefix: str) -> str:
        value = f"{prefix}-{uuid.uuid4().hex[:10]}"
        self.made.append(value)
        return value

    def links(self) -> List[tuple]:
        self.db.expire_all()
        rows = (
            self.db.query(IdentityAlias)
            .filter(
                IdentityAlias.anonymous_id.in_(self.made)
                | IdentityAlias.user_id.in_(self.made)
            )
            .order_by(IdentityAlias.anonymous_id)
            .all()
        )
        return [(row.anonymous_id, row.user_id) for row in rows]

    def close(self) -> None:
        self.db.rollback()
        if self.made:
            self.db.query(IdentityAlias).filter(
                IdentityAlias.anonymous_id.in_(self.made)
                | IdentityAlias.user_id.in_(self.made)
            ).delete(synchronize_session=False)
            self.db.commit()


@pytest.fixture
def ids(db_session):
    made = Ids(db_session)
    yield made
    made.close()


@pytest.fixture
def client(db_session):
    with api_client(db_session) as test_client:
        yield test_client


def _key(client, user, scopes=None) -> Dict[str, str]:
    body = {"name": f"alias-{uuid.uuid4().hex[:6]}"}
    if scopes is not None:
        body["scopes"] = scopes
    response = client.post(KEYS, json=body, headers=auth(user))
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def server_key(client, db_session) -> Dict[str, str]:
    """An ``sdk:ruleset`` key whose owner, a DEVELOPER, can change flags."""
    owner = make_user(db_session, UserRole.DEVELOPER, tag="alias")
    return {"X-API-Key": _key(client, owner, ["sdk:ruleset"])["key"]}


def _alias(client, headers, anonymous_id, user_id):
    return client.post(
        URL, json={"anonymous_id": anonymous_id, "user_id": user_id}, headers=headers
    )


def _no_id_in(response, *values: str) -> None:
    for value in values:
        assert value not in response.text, f"the answer repeats an id: {response.text}"


# ---------------------------------------------------------------------------
# 200: a link is stored once (IA-C2)
# ---------------------------------------------------------------------------


def test_a_link_is_stored_once_and_answered_again_with_created_false(
    client, server_key, ids
):
    a, u = ids("anon"), ids("known")

    first = _alias(client, server_key, a, u)
    again = _alias(client, server_key, a, u)

    assert first.status_code == 200, first.text
    assert first.json() == {"anonymous_id": a, "user_id": u, "created": True}
    assert again.status_code == 200, again.text
    assert again.json() == {"anonymous_id": a, "user_id": u, "created": False}
    assert ids.links() == [(a, u)]


def test_a_second_anonymous_id_is_linked_to_the_same_known_id(client, server_key, ids):
    a1, a2, u = ids("anon"), ids("second"), ids("known")
    assert _alias(client, server_key, a1, u).json()["created"] is True
    assert _alias(client, server_key, a2, u).json()["created"] is True
    assert sorted(ids.links()) == sorted([(a1, u), (a2, u)])
    person = person_of(ids.db, a2)
    assert person.known_id == u and set(person.ids) == {u, a1, a2}


def test_a_link_made_through_the_route_is_the_one_the_tracking_routes_follow(
    client, server_key, db_session, make_experiment, ids
):
    """The known id is answered the variant its anonymous id holds, and its
    purchase after the link is stored under that id, tagged with it."""
    with no_active_holdout(db_session):
        world = World(db_session, make_experiment)
        try:
            exp = world.experiment("Alias route")
            a1, u1 = world.split_pair(exp)
            ids.made.extend([a1, u1])
            owner = make_user(db_session, UserRole.DEVELOPER, tag="alias")
            plain = {"X-API-Key": _key(client, owner)["key"]}
            body = {"experiment_key": exp.key, "user_id": a1}
            held = client.post("/api/v1/tracking/assign", json=body, headers=plain)
            assert held.status_code == 200, held.text

            linked = _alias(client, server_key, a1, u1)
            assert linked.json()["created"] is True, linked.text

            body = {"experiment_key": exp.key, "user_id": u1}
            answer = client.post("/api/v1/tracking/assign", json=body, headers=plain)
            assert answer.status_code == 200, answer.text
            assert answer.json()["variant_id"] == held.json()["variant_id"]
            assert answer.json()["user_id"] == u1
            assert [r.user_id for r in world.rows(exp, [a1, u1])] == [a1]

            event = {
                "event_type": "purchase",
                "event_name": "purchase",
                "user_id": u1,
                "experiment_key": exp.key,
            }
            tracked = client.post("/api/v1/tracking/track", json=event, headers=plain)
            assert tracked.status_code == 200, tracked.text
            assert tracked.json()["user_id"] == u1
            stored = world.events(exp, [a1, u1], name="purchase")
            assert [(e.user_id, str(e.variant_id)) for e in stored] == [
                (a1, held.json()["variant_id"])
            ]

            # An event that names no experiment stays under the id that sent it.
            history = {k: v for k, v in event.items() if k != "experiment_key"}
            history["event_name"] = history["event_type"] = "page_view"
            untagged = client.post(
                "/api/v1/tracking/track", json=history, headers=plain
            )
            assert untagged.status_code == 200, untagged.text
            db_session.expire_all()
            rows = (
                db_session.query(Event)
                .filter(Event.user_id.in_([a1, u1]), Event.event_name == "page_view")
                .all()
            )
            assert [(e.user_id, e.experiment_id) for e in rows] == [(u1, None)]
        finally:
            world.close()


# ---------------------------------------------------------------------------
# 409: the rules a link can break (IA-C3, IA-C4, IA-C6)
# ---------------------------------------------------------------------------


def test_an_anonymous_id_linked_to_one_known_id_is_not_moved(client, server_key, ids):
    a, u1, u2 = ids("anon"), ids("known"), ids("other")
    assert _alias(client, server_key, a, u1).status_code == 200

    refused = _alias(client, server_key, a, u2)

    assert refused.status_code == 409, refused.text
    assert refused.json() == {"detail": LINKED_ELSEWHERE}
    _no_id_in(refused, a, u1, u2)
    assert ids.links() == [(a, u1)]


def test_a_known_id_with_links_is_not_itself_linked(client, server_key, ids):
    """A chain: u has an anonymous id, so u cannot be linked to k."""
    a, u, k = ids("anon"), ids("known"), ids("next")
    assert _alias(client, server_key, a, u).status_code == 200

    refused = _alias(client, server_key, u, k)

    assert refused.status_code == 409, refused.text
    assert refused.json() == {"detail": HAS_LINKS}
    _no_id_in(refused, a, u, k)
    assert ids.links() == [(a, u)]


def test_the_two_ids_swapped_are_refused(client, server_key, ids):
    """A cycle: after (a -> u), (u -> a) is refused."""
    a, u = ids("anon"), ids("known")
    assert _alias(client, server_key, a, u).status_code == 200

    refused = _alias(client, server_key, u, a)

    assert refused.status_code == 409, refused.text
    assert refused.json() == {"detail": HAS_LINKS}
    _no_id_in(refused, a, u)
    assert ids.links() == [(a, u)]


def test_nothing_is_linked_to_an_id_linked_as_an_anonymous_id(client, server_key, ids):
    a, u, x = ids("anon"), ids("known"), ids("third")
    assert _alias(client, server_key, a, u).status_code == 200

    refused = _alias(client, server_key, x, a)

    assert refused.status_code == 409, refused.text
    assert refused.json() == {"detail": USER_ID_LINKED}
    _no_id_in(refused, a, u, x)
    assert ids.links() == [(a, u)]


def test_a_known_id_takes_100_anonymous_ids_and_not_the_101st(
    client, server_key, db_session, ids
):
    u = ids("known")
    for _ in range(MAX_ANONYMOUS_IDS_PER_USER - 1):
        assert link(db_session, ids("seeded"), u) is True
    hundredth, extra = ids("hundredth"), ids("extra")

    accepted = _alias(client, server_key, hundredth, u)
    refused = _alias(client, server_key, extra, u)

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["created"] is True
    assert refused.status_code == 409, refused.text
    assert refused.json() == {"detail": AT_CAP}
    _no_id_in(refused, u, extra)
    linked = ids.links()
    assert len(linked) == MAX_ANONYMOUS_IDS_PER_USER
    assert all(known == u for _, known in linked)
    assert extra not in {anonymous for anonymous, _ in linked}


# ---------------------------------------------------------------------------
# 422: the request model (IA-C5, IA-C7, IA-K3)
# ---------------------------------------------------------------------------


def _detail(response) -> list:
    assert response.status_code == 422, response.text
    body = response.json()
    assert isinstance(body["detail"], list), body
    return [(tuple(e["loc"]), e["msg"]) for e in body["detail"]]


def test_two_equal_ids_are_refused(client, server_key, ids):
    same = ids("same")
    refused = _alias(client, server_key, same, same)
    assert _detail(refused) == [(("body",), EQUAL_IDS)]
    _no_id_in(refused, same)
    assert ids.links() == []


@pytest.mark.parametrize("field", ["anonymous_id", "user_id"])
@pytest.mark.parametrize("placeholder", ["anonymous", "anon"])
def test_a_shared_placeholder_is_refused_in_either_field(
    client, server_key, ids, field, placeholder
):
    other = ids("visitor")
    body = {"anonymous_id": other, "user_id": other, field: placeholder}
    refused = client.post(URL, json=body, headers=server_key)
    assert _detail(refused) == [(("body", field), PLACEHOLDER)]
    _no_id_in(refused, other)
    assert ids.links() == []
    assert (
        ids.db.query(IdentityAlias)
        .filter(
            (IdentityAlias.anonymous_id == placeholder)
            | (IdentityAlias.user_id == placeholder)
        )
        .count()
        == 0
    )


def test_three_visitors_sending_the_placeholder_stay_one_unlinked_id(
    client, server_key, ids
):
    for _ in range(3):
        refused = _alias(client, server_key, "anonymous", ids("customer"))
        assert _detail(refused) == [(("body", "anonymous_id"), PLACEHOLDER)]
    assert ids.links() == []
    person = person_of(ids.db, "anonymous")
    assert (person.known_id, person.ids) == ("anonymous", ("anonymous",))


@pytest.mark.parametrize("near", ["Anonymous", "ANON", "anon ", "anonymous-1"])
def test_only_the_two_exact_placeholders_are_refused(client, server_key, ids, near):
    """The comparison is exact: anything else is an id like any other."""
    u = ids("known")
    ids.made.append(near)
    accepted = _alias(client, server_key, near, u)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["created"] is True


@pytest.mark.parametrize("field", ["anonymous_id", "user_id"])
def test_a_missing_empty_or_too_long_field_is_refused(client, server_key, ids, field):
    other = ids("visitor")
    base = {"anonymous_id": ids("anon"), "user_id": ids("known")}
    missing = {k: v for k, v in base.items() if k != field}
    for body in (missing, {**base, field: ""}, {**base, field: "x" * 256}):
        refused = client.post(URL, json=body, headers=server_key)
        assert [loc for loc, _ in _detail(refused)] == [("body", field)], body
        _no_id_in(refused, other, *base.values())
    assert ids.links() == []


def test_255_characters_is_accepted(client, server_key, ids):
    long_id = "y" * 245 + uuid.uuid4().hex[:10]
    ids.made.append(long_id)
    accepted = _alias(client, server_key, long_id, ids("known"))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["anonymous_id"] == long_id


@pytest.mark.parametrize("field", ["anonymous_id", "user_id"])
@pytest.mark.parametrize("bad", ["\u0000", "\ud800"], ids=["nul", "lone-surrogate"])
def test_text_the_database_cannot_store_is_refused(client, server_key, ids, field, bad):
    prefix = ids("stored")
    body = {"anonymous_id": ids("anon"), "user_id": ids("known")}
    body[field] = prefix + bad
    # json.dumps escapes both as JSON does (\u0000, \ud800); the client's own
    # encoder would try to write the lone surrogate as UTF-8.
    refused = client.post(
        URL,
        content=json.dumps(body),
        headers={**server_key, "Content-Type": "application/json"},
    )
    assert _detail(refused) == [
        (
            ("body", field),
            f"Value error, {field} must not contain NUL or unpaired surrogate "
            "characters",
        )
    ]
    _no_id_in(refused, prefix)
    assert ids.links() == []


# ---------------------------------------------------------------------------
# 401 and 403: those of /assign/batch, in the same order and words (IA-K1)
# ---------------------------------------------------------------------------


def _expire(db_session, key_id: str) -> None:
    row = db_session.query(APIKey).filter(APIKey.id == uuid.UUID(key_id)).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()


def _deactivate(db_session, key_id: str) -> None:
    row = db_session.query(APIKey).filter(APIKey.id == uuid.UUID(key_id)).one()
    row.is_active = False
    db_session.commit()


def _presented(kind: str, client, db_session) -> Optional[Dict[str, str]]:
    """The ``X-API-Key`` header for one kind of key, or None for no header."""
    if kind == "no header":
        return None
    if kind == "unknown key":
        return {"X-API-Key": "eptk_not-a-real-key"}
    role, superuser = {
        "an ADMIN's key": (UserRole.ADMIN, False),
        "a superuser VIEWER's key": (UserRole.VIEWER, True),
    }.get(kind, (UserRole.DEVELOPER, False))
    owner = make_user(db_session, role, superuser=superuser, tag="alias")
    scopes = {
        "plain key": None,
        "label-only key": ["read", "write"],
        "mcp:read-only key": ["mcp:read"],
        "SDK:RULESET key": ["SDK:RULESET"],
    }.get(kind, ["sdk:ruleset"])
    created = _key(client, owner, scopes)
    if kind == "expired key":
        _expire(db_session, created["id"])
    elif kind == "deactivated key":
        _deactivate(db_session, created["id"])
    elif kind == "deleted key":
        gone = client.delete(f"{KEYS}/{created['id']}", headers=auth(owner))
        assert gone.status_code in (200, 204), gone.text
    elif kind == "inactive owner":
        owner.is_active = False
        db_session.commit()
    elif kind in ("an owner made ANALYST", "an owner made VIEWER"):
        owner.role = UserRole.ANALYST if "ANALYST" in kind else UserRole.VIEWER
        db_session.commit()
    return {"X-API-Key": created["key"]}


#: kind -> (status, detail), the same on both routes.
REFUSED = {
    "no header": (401, "API key missing"),
    "unknown key": (401, "Invalid API Key"),
    "expired key": (401, "Invalid API Key"),
    "deactivated key": (401, "Invalid API Key"),
    "deleted key": (401, "Invalid API Key"),
    "inactive owner": (401, "Invalid API Key"),
    "plain key": (403, MISSING_SCOPE_DETAIL),
    "label-only key": (403, MISSING_SCOPE_DETAIL),
    "mcp:read-only key": (403, MISSING_SCOPE_DETAIL),
    "SDK:RULESET key": (403, MISSING_SCOPE_DETAIL),
    "an owner made ANALYST": (403, OWNER_ROLE_DETAIL),
    "an owner made VIEWER": (403, OWNER_ROLE_DETAIL),
}

ACCEPTED = ["a DEVELOPER's key", "an ADMIN's key", "a superuser VIEWER's key"]


def _batch_body() -> dict:
    return {
        "experiment_key": f"alias-parity-{uuid.uuid4().hex[:8]}",
        "users": [{"user_id": f"parity-{uuid.uuid4().hex[:8]}"}],
    }


@pytest.mark.parametrize("kind", sorted(REFUSED))
def test_the_route_refuses_each_key_as_batch_assignment_does(
    client, db_session, ids, kind
):
    headers = _presented(kind, client, db_session)
    a, u = ids("anon"), ids("known")
    status, detail = REFUSED[kind]

    alias = _alias(client, headers, a, u)
    batch = client.post(BATCH, json=_batch_body(), headers=headers)

    assert (alias.status_code, alias.json()) == (status, {"detail": detail})
    assert (batch.status_code, batch.json()) == (status, {"detail": detail})
    _no_id_in(alias, a, u)
    assert ids.links() == []


@pytest.mark.parametrize("kind", ACCEPTED)
def test_a_server_key_whose_owner_can_change_flags_is_accepted(
    client, db_session, ids, kind
):
    headers = _presented(kind, client, db_session)
    a, u = ids("anon"), ids("known")

    alias = _alias(client, headers, a, u)
    batch = client.post(BATCH, json=_batch_body(), headers=headers)

    assert alias.status_code == 200, alias.text
    assert alias.json() == {"anonymous_id": a, "user_id": u, "created": True}
    # The batch route takes the same key, and answers for its own body.
    assert batch.status_code == 404, batch.text
    assert ids.links() == [(a, u)]


@pytest.mark.parametrize(
    "kind, status", [("no header", 401), ("plain key", 403)], ids=["401", "403"]
)
def test_the_key_is_checked_before_the_body(client, db_session, kind, status):
    """An empty body with no key is 401, and with a plain key 403, as on
    batch assignment: the key is refused before the body is read."""
    headers = _presented(kind, client, db_session)
    for path in (URL, BATCH):
        response = client.post(path, json={}, headers=headers)
        assert response.status_code == status, (path, response.text)


def test_the_presented_key_decides_not_another_key_of_the_same_owner(
    client, db_session, ids
):
    owner = make_user(db_session, UserRole.DEVELOPER, tag="alias")
    scoped = {"X-API-Key": _key(client, owner, ["sdk:ruleset"])["key"]}
    plain = {"X-API-Key": _key(client, owner)["key"]}
    a, u = ids("anon"), ids("known")

    refused = _alias(client, plain, a, u)
    assert (refused.status_code, refused.json()) == (
        403,
        {"detail": MISSING_SCOPE_DETAIL},
    )
    assert _alias(client, scoped, a, u).status_code == 200


# ---------------------------------------------------------------------------
# The window after t449's downgrade, before the API is rolled back
# ---------------------------------------------------------------------------


@contextmanager
def _without_the_table(db_session) -> Iterator[None]:
    """``identity_aliases`` gone, as its downgrade leaves it, and put back."""
    db_session.rollback()
    engine = db_session.get_bind()
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        conn.execute(
            text(
                f"ALTER TABLE {SCHEMA}.identity_aliases RENAME TO identity_aliases_parked"
            )
        )
    try:
        yield
    finally:
        db_session.rollback()
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '5s'"))
            conn.execute(
                text(
                    f"ALTER TABLE {SCHEMA}.identity_aliases_parked "
                    "RENAME TO identity_aliases"
                )
            )


def test_without_the_table_the_routes_that_read_links_answer_500(
    client, server_key, db_session, make_experiment, ids
):
    """Between ``t449_identity_aliases``' downgrade and the API's rollback, the
    release still serving reads the dropped table: assign, an event that names
    an experiment and the assignments list answer 500 for any id, a batch of
    events reports each such event in ``errors``, and an event with no
    experiment is stored as before."""
    with no_active_holdout(db_session):
        world = World(db_session, make_experiment)
        try:
            exp = world.experiment("Alias window")
            user = world.id("window")
            ids.made.append(user)
            owner = make_user(db_session, UserRole.DEVELOPER, tag="alias")
            plain = {"X-API-Key": _key(client, owner)["key"]}
            tagged = {
                "event_type": "purchase",
                "event_name": "purchase",
                "user_id": user,
                "experiment_key": exp.key,
            }
            untagged = {k: v for k, v in tagged.items() if k != "experiment_key"}
            before = db_session.query(Event).filter(Event.user_id == user).count()

            with _without_the_table(db_session):
                assigned = client.post(
                    "/api/v1/tracking/assign",
                    json={"experiment_key": exp.key, "user_id": user},
                    headers=plain,
                )
                tracked = client.post(
                    "/api/v1/tracking/track", json=tagged, headers=plain
                )
                listed = client.get(
                    f"/api/v1/tracking/assignments/{user}", headers=plain
                )
                batched = client.post(
                    "/api/v1/tracking/batch",
                    json={"events": [tagged, untagged]},
                    headers=plain,
                )
                history = client.post(
                    "/api/v1/tracking/track", json=untagged, headers=plain
                )
                anonymous = ids("anon")
                linked = _alias(client, server_key, anonymous, user)

            assert assigned.status_code == 500, assigned.text
            assert tracked.status_code == 500, tracked.text
            assert listed.status_code == 500, listed.text
            assert linked.status_code == 500, linked.text
            assert batched.status_code == 200, batched.text
            assert (
                batched.json()["success_count"],
                batched.json()["failure_count"],
                [e["index"] for e in batched.json()["errors"]],
            ) == (1, 1, [0])
            assert history.status_code == 200, history.text
            for answer in (assigned, tracked, listed, linked):
                _no_id_in(answer, user, anonymous)
            db_session.expire_all()
            assert db_session.query(Event).filter(Event.user_id == user).count() == (
                before + 2
            )
            assert world.rows(exp, [user]) == []
        finally:
            world.close()
