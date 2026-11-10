"""A feature flag key ending in a newline is refused (#1806).

The key validator matched ``^[a-z0-9][a-z0-9_-]*$`` with ``re.match``, and in
Python ``$`` also matches just before a final newline, so ``"search_v2\\n"``
passed and was stored with the newline in it. The key is now matched with
``re.fullmatch``, so it is refused with the same message as any other invalid
key.

Through the real create and update routes, with a DEVELOPER and a real
database: the refusal is an exact 422 body, nothing is stored, and the plain
key is still created.
"""

import uuid

import pytest

from backend.app.models.feature_flag import FeatureFlag
from backend.tests.integration.conftest import make_client_for_user

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

COLLECTION = "/api/v1/feature-flags/"

#: Typed out, so a change to the validator's message fails here.
KEY_MSG = (
    "Value error, Key must be lowercase alphanumeric characters, hyphens, "
    "or underscores only"
)


def _refused(resp, sent: str) -> None:
    """The one exact 422 body: the key's own message, whatever was sent."""
    assert resp.status_code == 422, resp.text
    assert resp.json() == {
        "detail": [
            {
                "type": "value_error",
                "loc": ["body", "key"],
                "msg": KEY_MSG,
                "ctx": {"error": {}},
            }
        ]
    }


@pytest.mark.regression
def test_create_refuses_a_key_ending_in_a_newline(db_session, developer_user):
    client = make_client_for_user(db_session, developer_user)
    base = f"nl-{uuid.uuid4().hex[:8]}"
    sent = base + "\n"

    resp = client.post(COLLECTION, json={"key": sent, "name": "x"})

    _refused(resp, sent)
    db_session.expire_all()
    assert (
        db_session.query(FeatureFlag).filter(FeatureFlag.key.in_([base, sent])).count()
        == 0
    )


@pytest.mark.regression
def test_update_refuses_a_key_ending_in_a_newline(db_session, developer_user):
    client = make_client_for_user(db_session, developer_user)
    base = f"nl-{uuid.uuid4().hex[:8]}"
    created = client.post(COLLECTION, json={"key": base, "name": "x"})
    assert created.status_code == 201, created.text
    flag_id = created.json()["id"]

    sent = base + "-v2\n"
    resp = client.put(f"{COLLECTION}{flag_id}", json={"key": sent})

    _refused(resp, sent)
    db_session.expire_all()
    stored = db_session.query(FeatureFlag).filter(FeatureFlag.id == flag_id).one()
    assert stored.key == base


def test_create_still_accepts_a_plain_key(db_session, developer_user):
    client = make_client_for_user(db_session, developer_user)
    key = f"search_v2-{uuid.uuid4().hex[:8]}"

    resp = client.post(COLLECTION, json={"key": key, "name": "x"})

    assert resp.status_code == 201, resp.text
    assert resp.json()["key"] == key
    db_session.expire_all()
    assert db_session.query(FeatureFlag).filter(FeatureFlag.key == key).count() == 1


@pytest.mark.parametrize(
    "sent",
    ["a\nb", "Search V2", "search_v2\n\n", "\nsearch_v2", "search_v2 "],
    ids=[
        "inner-newline",
        "uppercase-and-space",
        "two-newlines",
        "leading-newline",
        "trailing-space",
    ],
)
def test_create_still_refuses_other_invalid_keys_with_the_same_message(
    db_session, developer_user, sent
):
    client = make_client_for_user(db_session, developer_user)

    resp = client.post(COLLECTION, json={"key": sent, "name": "x"})

    _refused(resp, sent)
