"""The flag, rollout and segment guides, through the API and a real database.

* creating, changing and turning on a flag take a signed-in user: a request
  with a real API key and no token is answered 401 and changes nothing;
* raising a flag's rollout percentage adds users and takes none away;
* rules the API refuses are reported one problem at a time, the first found;
* a rollout schedule is refused with 422 when its target percentages
  decrease, when two stages share a stage order or the orders leave a gap,
  or when a stage order is above 1000; orders that start above 1 are kept;
* an ID-list segment holds at most 1,000,000 IDs, at the real limit;
* the per-ID limit counts characters, not bytes.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from backend.app.models.user import UserRole
from backend.tests.integration.api.route_helpers import (
    SCHEMA,
    api_client,
    api_key,
    auth,
    local_sign_in,
    make_user,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

FLAGS = "/api/v1/feature-flags"


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def admin(db_session):
    return make_user(db_session, UserRole.ADMIN, superuser=True, tag="flagguide")


def _flag(client, owner, **fields):
    suffix = uuid.uuid4().hex[:8]
    body = {"key": f"guide-{suffix}", "name": "Guide flag", **fields}
    resp = client.post(f"{FLAGS}/", json=body, headers=auth(owner))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def test_an_api_key_does_not_create_change_or_turn_on_a_flag(client, admin):
    key = api_key(client, admin)
    flag = _flag(client, admin, rollout_percentage=0)
    flag_id = flag["id"]
    attempts = [
        ("POST", f"{FLAGS}/", {"key": f"by-key-{flag_id[:8]}", "name": "By key"}),
        ("POST", FLAGS, {"key": f"by-key2-{flag_id[:8]}", "name": "By key"}),
        ("PUT", f"{FLAGS}/{flag_id}", {"rollout_percentage": 100, "is_active": True}),
        ("POST", f"{FLAGS}/{flag_id}/activate", None),
        ("POST", f"{FLAGS}/{flag_id}/enable", None),
        ("POST", f"{FLAGS}/{flag_id}/toggle", None),
    ]
    for method, path, body in attempts:
        resp = client.request(method, path, json=body, headers=key)
        assert resp.status_code == 401, (method, path, resp.text)
    # The key works where an SDK uses it, and the flag is unchanged.
    evaluated = client.get(
        f"{FLAGS}/evaluate/{flag['key']}", params={"user_id": "u-1"}, headers=key
    )
    assert evaluated.status_code == 200, evaluated.text
    stored = client.get(f"{FLAGS}/{flag_id}", headers=auth(admin)).json()
    assert stored["is_active"] is False and stored["rollout_percentage"] == 0
    listed = client.get(f"{FLAGS}/", headers=auth(admin)).json()
    keys = {item["key"] for item in listed.get("items", listed)}
    assert not {f"by-key-{flag_id[:8]}", f"by-key2-{flag_id[:8]}"} & keys


def _enabled(client, key, flag_key, users):
    on = set()
    for user_id in users:
        resp = client.get(
            f"{FLAGS}/evaluate/{flag_key}", params={"user_id": user_id}, headers=key
        )
        assert resp.status_code == 200, resp.text
        if resp.json()["enabled"]:
            on.add(user_id)
    return on


def test_raising_the_rollout_adds_users_and_takes_none_away(client, admin):
    key = api_key(client, admin)
    flag = _flag(client, admin, rollout_percentage=5, is_active=True)
    users = [f"rollout-user-{i}" for i in range(600)]
    seen = {}
    for percentage in (5, 50, 100):
        resp = client.put(
            f"{FLAGS}/{flag['id']}",
            json={"rollout_percentage": percentage},
            headers=auth(admin),
        )
        assert resp.status_code == 200, resp.text
        seen[percentage] = _enabled(client, key, flag["key"], users)
    # Each user lands on the same side: the same answer on a second read.
    assert _enabled(client, key, flag["key"], users[:100]) == set(users[:100])
    assert seen[5] <= seen[50] <= seen[100] == set(users)
    assert 0.02 * len(users) < len(seen[5]) < 0.09 * len(users)
    assert 0.42 * len(users) < len(seen[50]) < 0.58 * len(users)


def test_only_the_first_problem_in_the_rules_is_reported(client, admin):
    flag = _flag(client, admin)
    conditions = [
        {"attribute": "plan", "operator": "equal", "value": "x"},
        {"attribute": "country", "operator": "is", "value": "US"},
        {"attribute": "pl an", "operator": "equals", "value": "x"},
        {"attribute": "tier", "operator": "greater_than", "value": "abc"},
    ]
    rules = {
        "logical_operator": "AND",
        "groups": [{"logical_operator": "AND", "conditions": conditions}],
    }
    resp = client.put(
        f"{FLAGS}/{flag['id']}", json={"targeting_rules": rules}, headers=auth(admin)
    )
    assert resp.status_code == 422, resp.text
    [error] = resp.json()["detail"]
    assert error["msg"] == (
        "Value error, groups[0].conditions[0].operator: unknown operator"
    ), error
    # With that one fixed, the next problem is the one reported.
    conditions[0]["operator"] = "equals"
    resp = client.put(
        f"{FLAGS}/{flag['id']}", json={"targeting_rules": rules}, headers=auth(admin)
    )
    assert resp.status_code == 422, resp.text
    [error] = resp.json()["detail"]
    assert error["msg"] == (
        "Value error, groups[0].conditions[1].operator: unknown operator"
    ), error


def _schedule_body(flag_id, stages):
    start = datetime.now(timezone.utc) + timedelta(days=1)
    return {
        "name": f"Guide schedule {uuid.uuid4().hex[:6]}",
        "feature_flag_id": flag_id,
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=30)).isoformat(),
        "stages": [
            {
                "name": f"stage {order}",
                "stage_order": order,
                "target_percentage": percentage,
                "trigger_type": "manual",
            }
            for order, percentage in stages
        ],
    }


@pytest.mark.parametrize(
    "stages, accepted",
    [
        ([(1, 10), (2, 50), (3, 100)], True),
        ([(5, 10), (6, 50), (7, 100)], True),
        ([(0, 10), (1, 50)], True),
        ([(999, 10), (1000, 50)], True),
        ([(1, 10), (2, 50), (3, 25)], False),
        ([(1, 10), (3, 50)], False),
        ([(1, 10), (1, 50)], False),
        ([(1000, 10), (1001, 50)], False),
    ],
    ids=["1-2-3", "5-6-7", "0-1", "999-1000", "decreasing", "gap", "repeat", "1001"],
)
def test_which_schedules_are_refused_with_422(client, admin, stages, accepted):
    flag = _flag(client, admin)
    resp = client.post(
        "/api/v1/rollout-schedules/",
        json=_schedule_body(flag["id"], stages),
        headers=auth(admin),
    )
    if accepted:
        assert resp.status_code == 201, resp.text
        orders = sorted(s["stage_order"] for s in resp.json()["stages"])
        assert orders == [order for order, _ in stages]
    else:
        assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


def _id_list(client, admin):
    resp = client.post(
        "/api/v1/segments",
        json={"name": f"Guide ids {uuid.uuid4().hex[:8]}", "kind": "id_list"},
        headers=auth(admin),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _add(client, admin, segment_id, ids, **kwargs):
    return client.post(
        f"/api/v1/segments/{segment_id}/members",
        json={"add": ids},
        headers=auth(admin),
        **kwargs,
    )


def test_a_segment_holds_at_most_one_million_ids(client, admin, db_session):
    segment_id = _id_list(client, admin)
    try:
        db_session.execute(
            text(
                f"INSERT INTO {SCHEMA}.segment_members (segment_id, member_id) "
                "SELECT CAST(:s AS uuid), 'pre-' || g FROM generate_series(1, 999990) g"
            ),
            {"s": segment_id},
        )
        db_session.commit()
        at_the_cap = _add(client, admin, segment_id, [f"new-{i}" for i in range(10)])
        assert at_the_cap.status_code == 200, at_the_cap.text
        assert at_the_cap.json() == {
            "added": 10,
            "already_members": 0,
            "member_count": 1_000_000,
        }
        # An ID already in the segment does not count again; a new one does.
        over = _add(client, admin, segment_id, ["new-3", "one-more"])
        assert over.status_code == 422, over.text
        assert over.json()["detail"] == (
            "this segment would have 1,000,001 members; a segment holds at most "
            "1,000,000"
        )
        again = _add(client, admin, segment_id, ["new-3"])
        assert again.status_code == 200, again.text
        assert again.json()["member_count"] == 1_000_000
    finally:
        db_session.rollback()
        db_session.execute(
            text(f"DELETE FROM {SCHEMA}.segment_members WHERE segment_id = :s"),
            {"s": segment_id},
        )
        db_session.commit()


def test_the_id_limit_counts_characters_not_bytes(client, admin):
    segment_id = _id_list(client, admin)
    # 255 characters of two and three bytes each: one ID each.
    for char in ("é", "€"):
        resp = _add(client, admin, segment_id, [char * 255])
        assert resp.status_code == 200, resp.text
        refused = _add(client, admin, segment_id, [char * 256])
        assert refused.status_code == 422, refused.text

    ascii_ids = [f"{i:05d}" + "a" * 250 for i in range(10_000)]
    body = json.dumps({"add": ascii_ids}).encode()
    assert 2_550_000 < len(body) < 2_650_000
    resp = client.post(
        f"/api/v1/segments/{segment_id}/members",
        content=body,
        headers={**auth(admin), "content-type": "application/json"},
    )
    assert resp.status_code == 200, resp.text[:300]

    # As the page says: with long non-ASCII IDs, send fewer per request.
    wide = [f"{i:05d}" + "€" * 250 for i in range(10_000)]
    body = json.dumps({"add": wide}, ensure_ascii=False).encode()
    resp = client.post(
        f"/api/v1/segments/{segment_id}/members",
        content=body,
        headers={**auth(admin), "content-type": "application/json"},
    )
    assert resp.status_code == 413, resp.text[:300]
    body = json.dumps({"add": wide[:2_000]}, ensure_ascii=False).encode()
    resp = client.post(
        f"/api/v1/segments/{segment_id}/members",
        content=body,
        headers={**auth(admin), "content-type": "application/json"},
    )
    assert resp.status_code == 200, resp.text[:300]
