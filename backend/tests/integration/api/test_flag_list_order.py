"""``GET /api/v1/feature-flags/`` pages in a stable order, oldest first (#1612).

The list query had LIMIT/OFFSET and no ORDER BY, so a page followed heap order,
which an UPDATE moves: a script paging the list could repeat a flag or miss one.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.dialects import postgresql

from backend.app.crud import crud_feature_flag
from backend.app.models.feature_flag import FeatureFlag

pytestmark = [pytest.mark.integration]


@pytest.fixture
def marker(db_session):
    """A word every flag of the test carries (in its key); removed after."""
    word = f"ord{uuid.uuid4().hex[:8]}"
    yield word
    db_session.rollback()
    db_session.query(FeatureFlag).filter(FeatureFlag.key.ilike(f"%{word}%")).delete(
        synchronize_session=False
    )
    db_session.commit()


def _three_flags_with_the_oldest_updated(make_feature_flag, db_session, marker):
    a = make_feature_flag(key=f"{marker}-a")
    b = make_feature_flag(key=f"{marker}-b")
    c = make_feature_flag(key=f"{marker}-c")
    # An UPDATE writes a new row version, which can move A in heap order.
    a.rollout_percentage = 5
    db_session.commit()
    return a, b, c


@pytest.mark.regression
def test_the_first_page_is_the_oldest_flag_even_after_it_was_updated(
    admin_client, make_feature_flag, db_session, marker
):
    a, _b, _c = _three_flags_with_the_oldest_updated(
        make_feature_flag, db_session, marker
    )

    resp = admin_client.get(
        "/api/v1/feature-flags/", params={"search": marker, "limit": 1}
    )
    assert resp.status_code == 200, resp.text
    assert [item["key"] for item in resp.json()["items"]] == [a.key]


@pytest.mark.regression
def test_walking_the_pages_returns_every_flag_exactly_once_oldest_first(
    admin_client, make_feature_flag, db_session, marker
):
    a, b, c = _three_flags_with_the_oldest_updated(
        make_feature_flag, db_session, marker
    )

    seen = []
    for skip in range(3):
        resp = admin_client.get(
            "/api/v1/feature-flags/",
            params={"search": marker, "limit": 1, "skip": skip},
        )
        assert resp.status_code == 200, resp.text
        seen.extend(item["key"] for item in resp.json()["items"])
    assert seen == [a.key, b.key, c.key]


@pytest.mark.regression
def test_the_list_query_orders_by_created_at_then_id(db_session, monkeypatch):
    """The deterministic half: the SQL itself carries the ORDER BY."""
    captured = []
    real = type(db_session).execute

    def spy(self, statement, *args, **kwargs):
        captured.append(statement)
        return real(self, statement, *args, **kwargs)

    monkeypatch.setattr(type(db_session), "execute", spy)
    crud_feature_flag.get_multi(db_session, skip=0, limit=1)

    assert captured, "get_multi issued no statement through Session.execute"
    sql = str(
        captured[-1].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "ORDER BY" in sql
    order = sql.split("ORDER BY", 1)[1]
    assert order.index("created_at") < order.index(".id")
