"""``GET /api/v1/feature-flags/``: ``search`` and ``total``.

``search`` matches part of a flag's name or key, whatever its case, and
``total`` counts every flag that matches, not only the ones on the page.
"""

from __future__ import annotations

import uuid

import pytest

from backend.app.models.feature_flag import FeatureFlag

pytestmark = [pytest.mark.integration]


@pytest.fixture
def marker(db_session):
    """A word no other flag carries; the flags holding it are removed after."""
    word = f"srch{uuid.uuid4().hex[:8]}"
    yield word
    db_session.rollback()
    db_session.query(FeatureFlag).filter(
        FeatureFlag.key.ilike(f"%{word}%") | FeatureFlag.name.ilike(f"%{word}%")
    ).delete(synchronize_session=False)
    db_session.commit()


def test_search_matches_part_of_the_name_or_the_key_in_any_case_and_total_counts_them(
    admin_client, make_feature_flag, marker
):
    by_key = make_feature_flag(key=f"checkout-{marker}-a", name="First")
    by_name = make_feature_flag(
        key=f"flag-{uuid.uuid4().hex[:8]}", name=f"Second {marker}"
    )
    make_feature_flag(key=f"other-{uuid.uuid4().hex[:8]}", name="Unrelated")

    resp = admin_client.get(
        "/api/v1/feature-flags/", params={"search": marker.upper(), "limit": 1}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["total"] == 2

    everything = admin_client.get(
        "/api/v1/feature-flags/", params={"search": marker[2:].upper()}
    ).json()
    assert {item["key"] for item in everything["items"]} == {by_key.key, by_name.key}
    assert everything["total"] == 2
